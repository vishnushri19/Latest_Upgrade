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
    check_license_dates,
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
        ssh_control_path: Optional[str] = None,
    ) -> None:
        self.client = client
        self.options = options or FlowOptions()
        if settings is None:
            raise ValueError("Settings is required")
        self.settings = settings
        # Optional path to an already-open, authenticated SSH ControlMaster
        # socket (see scripts/run_upgrade_node.py). When set, LIC-001's SSH
        # fallback reuses this connection instead of opening a new one,
        # avoiding an unexpected mid-flow password prompt.
        self.ssh_control_path = ssh_control_path
        self._preflight_results: Optional[List[CheckResult]] = None
        self._storage_result: Optional[CheckResult] = None
        self._upload_paths: List[str] = []
        self._combined_ehf = False
        self._required_images_present = False

    def preflight(self) -> List[CheckResult]:
        """Run and cache all checks that must complete before backups."""
        if self._preflight_results is not None:
            return list(self._preflight_results)

        results: List[CheckResult] = []

        # --- Version + role discovery (for idempotence and safety) ---
        version_str = ""
        try:
            ver_payload = self.client.system_version()
            version_str = str(ver_payload)
        except Exception:
            try:
                resp = self.client.run_bash("cat /VERSION 2>/dev/null || tmsh -q show sys version", timeout=15)
                version_str = str(resp.get("commandResult", ""))
            except Exception:
                pass

        if not version_str:
            results.append(
                CheckResult(
                    id="FLOW-VER-001",
                    category="Upgrade Flow",
                    name="Current version lookup failed",
                    status="FAIL",
                    details={"error": "Could not determine system version via REST or TMSH."},
                )
            )
            if self.should_stop(results):
                self._preflight_results = results
                return list(results)

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
                self._preflight_results = results
                return list(results)

        # --- Standalone detection: a device with no HA peer(s) in its trust
        # domain has no "standby" to fail over to. tmsh/REST report such a
        # device as "active" (there is no other state without a peer), so we
        # must not apply the HA active/standby lock in that case. If 2+
        # devices are present, this is a real HA pair/cluster and the strict
        # active/standby lock below still applies.
        #
        # Safety note: client.devices() itself falls back to raw tmsh bash
        # text (no "items" key) if the REST call fails. We must NOT
        # misinterpret that fallback as "0 devices" -> "standalone", since
        # that would incorrectly bypass the active/standby lock on a real HA
        # pair whose REST endpoint happens to be flaky. Only trust the
        # device count when the REST payload actually returned "items".
        is_standalone = False
        device_count: Optional[int] = None
        try:
            payload = self.client.devices()
            if not isinstance(payload, dict) or "items" not in payload:
                raise RuntimeError(
                    "devices() did not return a structured 'items' list "
                    "(REST endpoint may have fallen back to raw tmsh text); "
                    "cannot reliably determine device count."
                )
            devices = payload.get("items") or []
            device_count = len(devices)
            is_standalone = device_count <= 1
        except Exception as e:
            # If discovery fails or is unreliable, fail safe: do NOT assume
            # standalone. The existing active/standby lock still applies
            # below.
            results.append(
                CheckResult(
                    id="FLOW-STANDALONE-001",
                    category="Upgrade Flow",
                    name="Standalone/HA topology detection failed",
                    status="RISK_ACCEPTED",
                    details={
                        "note": "Could not determine trust-domain device count; treating device as HA (active/standby lock still applies).",
                        "error": str(e),
                    },
                )
            )

        if is_standalone:
            print(
                f"\n[i] Device {self.client.host} has no HA peer in its trust domain "
                f"(device_count={device_count}). Treating as STANDALONE; the "
                "active/standby lock does not apply."
            )

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
            self._preflight_results = results
            return list(results)

        # 2) If ACTIVE, do not run upgrade steps (but still run prechecks)
        #    Skip this lock entirely for standalone devices (no HA peer),
        #    since "active" is the only state such a device can report and
        #    there is no standby to fail over to.
        if role == "active" and not is_standalone:
            # Prechecks on active node
            print(f"\n[*] Running prechecks on active node {self.client.host}...")
            precheck_results = run_prechecks(self.client)
            results.extend(precheck_results)
            if self.should_stop(results):
                self._preflight_results = results
                return list(results)

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
            self._preflight_results = results
            return list(results)

        # From here on, either role is STANDBY (or unknown but not 'active'),
        # or the device is a standalone unit where the active/standby lock
        # does not apply.
        print(f"\n" + "=" * 75)
        if is_standalone:
            print(f"🟢 Target device {self.client.host} is STANDALONE (no HA peer). Proceeding with upgrade execution...")
        else:
            print(f"🟢 Target device {self.client.host} is STANDBY. Proceeding with upgrade execution...")
        print("=" * 75)

        # --- Prechecks (always run before upgrade) ---
        precheck_results = run_prechecks(self.client)
        results.extend(precheck_results)
        if self.should_stop(results):
            self._preflight_results = results
            return list(results)

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
            self._preflight_results = results
            return list(results)

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
            self._preflight_results = results
            return list(results)

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
            self._preflight_results = results
            return list(results)

        # --- Storage and target-volume safety ---
        self._combined_ehf = bool(
            self.settings.base_iso_local_path
            and self.settings.hotfix_iso_local_path
        )
        self._upload_paths = [
            path
            for path in (
                self.settings.base_iso_local_path,
                self.settings.hotfix_iso_local_path,
            )
            if path
        ] or ([self.settings.iso_local_path] if self.settings.iso_local_path else [])
        expected_names = [
            os.path.basename(path)
            for path in self._upload_paths
        ]
        required_images_present = bool(expected_names)
        for image_name in expected_names:
            image = check_image_present(
                self.client,
                image_name,
                ssh_user=self.settings.scp_user,
                ssh_control_path=self.ssh_control_path,
            )
            required_images_present = required_images_present and image.found
        if not expected_names:
            required_images_present = True
        self._required_images_present = required_images_present
        self._storage_result = prepare_install_storage(
            self.client,
            self.settings.target_volume,
            require_upload_space=(
                self.settings.auto_upload_iso
                and bool(self._upload_paths)
                and not required_images_present
            ),
            expected_image_contains=self.settings.target_image_contains,
            skip_iso_cleanup=required_images_present,
        )
        results.append(self._storage_result)
        self._preflight_results = results
        return list(results)

    def run(self) -> List[CheckResult]:
        results = self.preflight()
        if self.should_stop(results):
            return results

        if any(result.id == "FLOW-SKIP-001" for result in results):
            return results

        storage_result = self._storage_result
        if storage_result is None:
            return results

        combined_ehf = self._combined_ehf
        upload_paths = self._upload_paths

        # --- Execution: ensure image present on standby ---
        if (
            self.settings.auto_upload_iso
            and upload_paths
            and not self._required_images_present
        ):
            upload_results = exec_upload_files_standby(
                self.client,
                host=self.settings.host,
                scp_user=self.settings.scp_user,
                iso_local_paths=upload_paths,
            )
            results.extend(upload_results)
            if self.should_stop(results):
                return results
        elif self._required_images_present:
            print(
                "\nRequired upgrade image(s) already present in "
                "/shared/images/. Skipping ISO upload."
            )

        expected_names = [
            os.path.basename(path)
            for path in upload_paths
        ]
        if combined_ehf:
            expected_names = [expected_names[-1]]
        img = check_image_present(
            self.client,
            expected_names[0]
            if expected_names
            else self.settings.target_image_contains,
            ssh_user=self.settings.scp_user,
            ssh_control_path=self.ssh_control_path,
        )
        if combined_ehf:
            base_name = os.path.basename(self.settings.base_iso_local_path)
            base_img = check_image_present(
                self.client,
                base_name,
                ssh_user=self.settings.scp_user,
                ssh_control_path=self.ssh_control_path,
            )
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

        license_image_name = (
            os.path.basename(self.settings.base_iso_local_path)
            if combined_ehf
            else (img.matched_name or "")
        )
        license_result = check_license_dates(
            self.client,
            license_image_name,
            ssh_user=(
                getattr(self.settings, "scp_user", "")
                or self.settings.username
            ),
            ssh_control_path=self.ssh_control_path,
        )
        results.append(license_result)
        if self.should_stop(results):
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
