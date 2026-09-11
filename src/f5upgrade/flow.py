from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from .bigip_client import BigIPClient
from .checks import run_prechecks
from .config import Settings
from .execution import (
    check_image_present,
    exec_install_standby,
    exec_upload_files_standby,
    exec_reboot_to_volume_standby,
    exec_volume_ready,
    exec_wait_postboot,
    prepare_install_storage,
)
from .ha import get_failover_role
from .report import CheckResult


@dataclass(frozen=True)
class FlowOptions:
    allow_risk_accepted: bool = True
    fail_fast: bool = True
    require_standby: bool = True
    postboot_timeout_sec: int = 900
    postboot_interval_sec: int = 15


class UpgradeFlow:
    def __init__(
        self,
        client: BigIPClient,
        options: Optional[FlowOptions] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.client = client
        self.options = options or FlowOptions()
        if settings is None:
            raise ValueError("Settings is required")
        self.settings = settings

    def run(self) -> List[CheckResult]:
        results: List[CheckResult] = []

        # --- Version + role discovery (for idempotence and safety) ---
        try:
            ver_payload = self.client.system_version()
            version_str = str(ver_payload)
        except Exception as e:
            version_str = ""
            results.append(
                CheckResult(
                    id="FLOW-VER-001",
                    category="Upgrade Flow",
                    name="Current version lookup failed",
                    status="FAIL",
                    details={"error": str(e)},
                )
            )
            if self.should_stop(results):
                return results

        try:
            role = (get_failover_role(self.client) or "").lower()
        except Exception as e:
            role = ""
            results.append(
                CheckResult(
                    id="FLOW-ROLE-001",
                    category="Upgrade Flow",
                    name="Current failover role lookup failed",
                    status="FAIL",
                    details={"error": str(e)},
                )
            )
            if self.should_stop(results):
                return results

        target = (self.settings.target_image_contains or "").strip()

        # 1) If already on target version, skip EXEC steps (idempotence)
        if target and target in version_str:
            print(f"\n[+] Device {self.client.host} is already running target version ({target}). Skipping upgrade installation.")
            results.append(
                CheckResult(
                    id="FLOW-SKIP-001",
                    category="Upgrade Flow",
                    name="Target image already installed; skipping upgrade steps",
                    status="PASS",
                    details={
                        "note": "Device is already on target image; EXEC-* steps were not run.",
                        "current_version": version_str,
                        "role": role,
                        "target_contains": target,
                    },
                )
            )
            return results

        # 2) If ACTIVE, do not run upgrade steps (but still run prechecks)
        if role == "active":
            # Prechecks on active node
            print(f"\n[*] Running prechecks on active node {self.client.host}...")
            precheck_results = run_prechecks(self.client)
            results.extend(precheck_results)
            if self.should_stop(results):
                return results

            print("\n" + "=" * 75)
            print("🔒 [SAFETY BLAST-RADIUS LOCK ACTIVATED]")
            print(f"Device {self.client.host} is currently the ACTIVE node for /Common/traffic-group-1!")
            print("Destructive upgrade steps (ISO upload, install, reboot) are SKIPPED on active nodes.")
            print("-" * 75)
            print("👉 TO PROCEED WITH UPGRADE:")
            print("   1. Point to your STANDBY node first:")
            print("      export BIGIP_HOST=<standby_peer_ip>")
            print("      make upgrade-node")
            print("   OR")
            print("   2. Failover this node to standby first:")
            print(f'      ssh {self.settings.username}@{self.client.host} "tmsh run sys failover standby traffic-group /Common/traffic-group-1"')
            print("=" * 75 + "\n")

            results.append(
                CheckResult(
                    id="FLOW-SKIP-002",
                    category="Upgrade Flow",
                    name="Device is ACTIVE; upgrade halted by design",
                    status="FAIL",
                    details={
                        "error": "Upgrade halted: Target device is ACTIVE. Upgrade MUST be run on the STANDBY node first.",
                        "current_version": version_str,
                        "role": role,
                        "target_contains": target,
                    },
                )
            )
            return results

        # From here on, we assume role is STANDBY (or unknown but not 'active')
        print(f"\n" + "=" * 75)
        print(f"🟢 Target device {self.client.host} is STANDBY. Proceeding with upgrade execution...")
        print("=" * 75)

        # --- Prechecks (always run before upgrade) ---
        precheck_results = run_prechecks(self.client)
        results.extend(precheck_results)
        if self.should_stop(results):
            return results

        results.append(
            CheckResult(
                id="FLOW-010",
                category="Upgrade Flow",
                name="Phase 0 complete (readiness confirmed)",
                status="PASS",
                details={"note": "Environment passed all prechecks."},
            )
        )
        if self.should_stop(results):
            return results

        # --- Discovery (unchanged) ---
        try:
            from .discovery import discover_devices

            local_name, devices = discover_devices(self.client)
            results.append(
                CheckResult(
                    id="DISC-001",
                    category="Discovery",
                    name="Device inventory discovered",
                    status="PASS",
                    details={
                        "local_device": local_name,
                        "device_count": len(devices),
                        "devices": [d.get("name") for d in devices],
                    },
                )
            )
        except Exception as e:
            results.append(
                CheckResult(
                    id="DISC-001",
                    category="Discovery",
                    name="Device inventory discovered",
                    status="FAIL",
                    details={"error": str(e)},
                )
            )
            return results

        try:
            from .mgmt import resolve_management_addresses

            mgmt_list = resolve_management_addresses(self.client)
            results.append(
                CheckResult(
                    id="DISC-002",
                    category="Discovery",
                    name="Management addresses resolved",
                    status="PASS",
                    details={
                        "count": len(mgmt_list),
                        "management_addresses": mgmt_list,
                    },
                )
            )
        except Exception as e:
            results.append(
                CheckResult(
                    id="DISC-002",
                    category="Discovery",
                    name="Management addresses resolved",
                    status="FAIL",
                    details={"error": str(e)},
                )
            )
            return results

        # --- Execution: prepare storage before image upload/install ---
        combined_ehf = bool(
            self.settings.base_iso_local_path
            and self.settings.hotfix_iso_local_path
        )
        upload_paths = [
            path
            for path in (
                self.settings.base_iso_local_path,
                self.settings.hotfix_iso_local_path,
            )
            if path
        ] or ([self.settings.iso_local_path] if self.settings.iso_local_path else [])
        storage_result = prepare_install_storage(
            self.client,
            self.settings.target_volume,
            require_upload_space=self.settings.auto_upload_iso and bool(upload_paths),
            expected_image_contains=self.settings.target_image_contains,
        )
        results.append(storage_result)
        if self.should_stop(results):
            return results

        # --- Execution: ensure image present on standby ---
        if self.settings.auto_upload_iso and upload_paths:
            upload_results = exec_upload_files_standby(
                self.client,
                host=self.settings.host,
                scp_user=self.settings.scp_user,
                iso_local_paths=upload_paths,
            )
            results.extend(upload_results)
            if self.should_stop(results):
                return results

        expected_names = [
            os.path.basename(path)
            for path in upload_paths
        ]
        if combined_ehf:
            expected_names = [expected_names[-1]]
        img = check_image_present(
            self.client,
            expected_names[0] if expected_names else self.settings.target_image_contains,
        )
        if combined_ehf:
            base_name = os.path.basename(self.settings.base_iso_local_path)
            base_img = check_image_present(self.client, base_name)
            results.append(
                CheckResult(
                    id="EXEC-BASE-IMG-001",
                    category="Execution Readiness",
                    name="Matching base image presence verified",
                    status="PASS" if base_img.found else "FAIL",
                    details=base_img.details,
                )
            )
            if not base_img.found or self.should_stop(results):
                return results
        results.append(
            CheckResult(
                id="EXEC-IMG-001",
                category="Execution Readiness",
                name="Target image presence verified",
                status="PASS" if img.found else "FAIL",
                details=img.details,
            )
        )
        if not img.found or self.should_stop(results):
            return results

        if not img.found or not self._confirm_install(
            img.matched_name or "",
            self.settings.target_volume,
        ):
            results.append(
                CheckResult(
                    id="EXEC-CONFIRM-001",
                    category="Execution Readiness",
                    name="Operator confirmed target image installation",
                    status="FAIL",
                    details={
                        "error": (
                            "Installation was not confirmed. No install "
                            "or reboot action was started."
                        ),
                        "image": img.matched_name,
                        "target_volume": self.settings.target_volume,
                    },
                )
            )
            return results

        install_name = img.matched_name or ""
        force_install = combined_ehf or (
            bool(install_name)
            and install_name.lower().startswith("hotfix-bigip-")
        )
        volume_state = storage_result.details.get("volume_state", {})
        create_volume = not bool(volume_state) and not bool(
            storage_result.details.get("deleted_volume")
        )

        # Install to target volume (standby only)
        results.append(
            exec_install_standby(
                self.client,
                install_name,
                self.settings.target_volume,
                force_install=force_install,
                create_volume=create_volume,
            )
        )
        if self.should_stop(results):
            return results

        # Wait for volume completion
        results.append(
            exec_volume_ready(
                self.client,
                self.settings.target_volume,
                self.settings.target_image_contains,
            )
        )
        if self.should_stop(results):
            return results

        # Reboot to target volume
        results.append(
            exec_reboot_to_volume_standby(
                self.client,
                self.settings.target_volume,
            )
        )
        if self.should_stop(results):
            return results

        # Post-boot validation (device reachable, version matches, role ok)
        results.append(
            exec_wait_postboot(
                self.client,
                expect_version_contains=self.settings.target_image_contains,
                timeout_sec=self.options.postboot_timeout_sec,
                interval_sec=self.options.postboot_interval_sec,
            )
        )

        return results

    @staticmethod
    def _confirm_install(image_name: str, target_volume: str) -> bool:
        """Require an explicit operator confirmation before installation."""
        print(
            f"\nImage already exists in /shared/images/: {image_name}\n"
            f"Target installation volume: {target_volume}"
        )

        if not sys.stdin.isatty():
            print(
                "Installation confirmation requires an interactive terminal."
            )
            return False

        try:
            answer = input(
                "Continue with image installation and standby reboot? (y/N): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        return answer in ("y", "yes")

    def should_stop(self, results: List[CheckResult]) -> bool:
        if self.options.fail_fast and any(r.status == "FAIL" for r in results):
            return True
        if not self.options.allow_risk_accepted and any(
            r.status == "RISK_ACCEPTED" for r in results
        ):
            return True
        return False
