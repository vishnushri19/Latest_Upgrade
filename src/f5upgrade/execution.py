from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .bigip_client import BigIPClient
from .ha import get_failover_role
from .report import CheckResult


@dataclass(frozen=True)
class ImagePresenceResult:
    found: bool
    matched_name: Optional[str]
    details: Dict[str, Any]


@dataclass(frozen=True)
class VolumeState:
    found: bool
    volume: str
    version: str = ""
    status: str = ""
    build: str = ""
    active: str = ""
    source: str = ""
    raw: str = ""
    error: str = ""


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _remote_command_output(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    return str(response.get("commandResult", "")).strip()


def _extract_version_from_image_name(image_iso_name: str) -> str:
    """Extract version from names such as: BIGIP-17.5.1.9-0.0.12.iso"""
    match = re.search(r"BIGIP-(\d+(?:\.\d+)+)-", image_iso_name or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _is_fatal_tmsh_output(output: str) -> bool:
    """Detect tmsh output that indicates the command was rejected."""
    text = (output or "").lower()
    fatal_markers = [
        "data input error",
        "syntax error",
        "operation is not supported",
        "can't ",
        "cannot ",
        "failed",
        "error:",
        "no such file",
        "not found",
        "could not locate",
        "was not found",
    ]
    return any(marker in text for marker in fatal_markers)


def _available_iso_names(client: BigIPClient) -> List[str]:
    response = client.run_bash(
        "ls -1 /shared/images/*.iso 2>/dev/null | sed 's#^.*/##' | sort",
        timeout=60,
    )
    return [
        line.strip()
        for line in _remote_command_output(response).splitlines()
        if line.strip() and os.path.basename(line.strip()) == line.strip()
    ]


def _available_space_kib(client: BigIPClient) -> Tuple[int, str]:
    response = client.run_bash("df -Pk /shared/images", timeout=60)
    output = _remote_command_output(response)
    data_lines = [
        line.split()
        for line in output.splitlines()
        if line.strip() and not line.lower().startswith("filesystem")
    ]
    if not data_lines or len(data_lines[-1]) < 5:
        raise ValueError("Could not parse free space for /shared/images.")
    try:
        return int(data_lines[-1][3]), output
    except ValueError as exc:
        raise ValueError("Free-space value from df was not numeric.") from exc


def _parse_volume_from_tmsh_status(output: str, volume: str) -> VolumeState:
    """Parse one volume row from: tmsh show sys software status"""
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(volume):
            continue

        parts = line.split()
        version = parts[2] if len(parts) > 2 else ""
        build = parts[3] if len(parts) > 3 else ""
        active = parts[4] if len(parts) > 4 else ""
        lower = line.lower()

        if "installing" in lower:
            pct_match = re.search(r"installing\s+([\d.]+)\s+pct", line, re.IGNORECASE)
            status = f"installing {pct_match.group(1)} pct" if pct_match else "installing"
        elif "complete" in lower:
            status = "complete"
        elif "failed" in lower:
            status = "failed"
        elif "error" in lower:
            status = "error"
        else:
            status = " ".join(parts[5:]) if len(parts) > 5 else ""

        return VolumeState(
            found=True,
            volume=volume,
            version=version,
            status=status,
            build=build,
            active=active,
            source="tmsh_status",
            raw=line,
        )

    return VolumeState(
        found=False,
        volume=volume,
        source="tmsh_status",
        raw=output or "",
        error=f"Volume {volume} was not found in tmsh software status output.",
    )


def _get_volume_state(client: BigIPClient, volume: str) -> VolumeState:
    """Read software volume state via REST with tmsh fallback."""
    try:
        payload = client.get(f"/mgmt/tm/sys/software/volume/{volume}", timeout=20)
        return VolumeState(
            found=True,
            volume=volume,
            version=str(payload.get("version", "")),
            status=str(payload.get("status", "")),
            build=str(payload.get("build", "")),
            active=str(payload.get("active", "")),
            source="icontrol_volume",
            raw=str(payload),
        )
    except Exception as rest_error:
        try:
            response = client.run_bash("tmsh show sys software status", timeout=30)
            output = _remote_command_output(response)
            parsed = _parse_volume_from_tmsh_status(output, volume)
            if parsed.found:
                return parsed
            return VolumeState(
                found=False,
                volume=volume,
                source="icontrol_then_tmsh_status",
                raw=output,
                error=f"{type(rest_error).__name__}: {rest_error}",
            )
        except Exception as tmsh_error:
            return VolumeState(
                found=False,
                volume=volume,
                source="icontrol_then_tmsh_status",
                error=f"REST error: {type(rest_error).__name__}: {rest_error}; tmsh status error: {type(tmsh_error).__name__}: {tmsh_error}",
            )


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Image checks use fixed internal retry settings so customer deployments do
# not need additional environment variables.
IMAGE_CHECK_TIMEOUT_SECONDS = 60
IMAGE_CHECK_MAX_RETRIES = 3
IMAGE_CHECK_RETRY_DELAY_SECONDS = 10

# Install submission is intentionally single-attempt. A timeout can occur
# after BIG-IP accepted the command, so blind retries could submit the same
# non-idempotent install multiple times.
INSTALL_SUBMIT_TIMEOUT_SECONDS = 120
INSTALL_STATE_VERIFY_ATTEMPTS = 3
INSTALL_STATE_VERIFY_DELAY_SECONDS = 10


def check_image_present(
    client: BigIPClient,
    image_name_contains: str,
    *,
    ssh_user: Optional[str] = None,
    ssh_control_path: Optional[str] = None,
) -> ImagePresenceResult:
    """Verify that the ISO exists under /shared/images.

    The filesystem check prefers an existing SSH ControlMaster and retains
    REST as a fallback. This prevents a temporary icrd outage from reporting
    a present image as missing.
    """
    needle = (image_name_contains or "").strip().lower()
    available_images = []
    rest_error = ""

    # When an authenticated ControlMaster exists, the filesystem listing
    # below is authoritative. Avoid an unnecessary REST inventory request
    # that can time out while BIG-IP is busy.
    if not (ssh_user and ssh_control_path):
        try:
            payload = client.software_images()
            items = payload.get("items", []) if isinstance(payload, dict) else []
            available_images = [
                str(item.get("name"))
                for item in items
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception as exc:
            available_images = []
            rest_error = f"{type(exc).__name__}: {exc}"

    try:
        response = _run_readonly_bash_prefer_ssh(
            client,
            "ls -1 /shared/images/ 2>/dev/null | head -n 500",
            timeout=IMAGE_CHECK_TIMEOUT_SECONDS,
            max_retries=IMAGE_CHECK_MAX_RETRIES,
            retry_delay=IMAGE_CHECK_RETRY_DELAY_SECONDS,
            ssh_user=ssh_user,
            ssh_control_path=ssh_control_path,
        )
        output = _remote_command_output(response)
        filesystem_images = [
            line.strip()
            for line in output.splitlines()
            if line.strip().lower().endswith(".iso")
        ]
    except Exception as exc:
        return ImagePresenceResult(
            found=False,
            matched_name=None,
            details={
                "query": image_name_contains,
                "method": "filesystem",
                "available_images": available_images,
                "rest_error": rest_error,
                "ssh_fallback_attempted": bool(ssh_user),
                "ssh_fallback_reused_existing_connection": bool(
                    ssh_control_path
                ),
                "error": f"Could not inspect /shared/images: {type(exc).__name__}: {exc}",
            },
        )

    filesystem_matches = [image for image in filesystem_images if needle in image.lower()]
    if filesystem_matches:
        matched = filesystem_matches[0]
        return ImagePresenceResult(
            found=True,
            matched_name=matched,
            details={
                "query": image_name_contains,
                "method": "filesystem",
                "matched_image": matched,
                "filesystem_images": filesystem_images,
                "rest_available_images": available_images,
                "rest_error": rest_error,
            },
        )

    return ImagePresenceResult(
        found=False,
        matched_name=None,
        details={
            "query": image_name_contains,
            "method": "filesystem",
            "filesystem_images": filesystem_images,
            "rest_available_images": available_images,
            "rest_error": rest_error,
            "rest_inventory_may_be_stale": any(needle in image.lower() for image in available_images),
        },
    )


LICENSE_DATE_TIMEOUT_SECONDS = int(
    os.environ.get("LICENSE_DATE_TIMEOUT_SECONDS", "60")
)
LICENSE_DATE_MAX_RETRIES = int(
    os.environ.get("LICENSE_DATE_MAX_RETRIES", "2")
)
LICENSE_DATE_RETRY_DELAY_SECONDS = int(
    os.environ.get("LICENSE_DATE_RETRY_DELAY_SECONDS", "15")
)


def _run_bash_via_ssh(
    ssh_host: str,
    ssh_user: str,
    command: str,
    timeout: int,
    control_path: Optional[str] = None,
) -> Dict[str, str]:
    """
    Run a read-only bash command over SSH and wrap stdout the same way
    BigIPClient.run_bash() would (as 'commandResult').
    """
    ssh_opts = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={min(timeout, 30)}",
    ]
    if control_path:
        ssh_opts += [
            "-o",
            f"ControlPath={control_path}",
            "-o",
            "ControlMaster=auto",
        ]
    ssh_command = [
        "ssh",
        *ssh_opts,
        f"{ssh_user}@{ssh_host}",
        f"bash -lc {shlex.quote(command)}",
    ]
    completed = subprocess.run(
        ssh_command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (exit {completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return {"commandResult": completed.stdout}


def _run_bash_with_retry(
    client: BigIPClient,
    command: str,
    *,
    timeout: int,
    max_retries: int,
    retry_delay: int,
    ssh_host: Optional[str] = None,
    ssh_user: Optional[str] = None,
    ssh_control_path: Optional[str] = None,
) -> Any:
    """
    Run a bash command via REST, retrying on transient timeout/connection
    errors. If all REST attempts fail and SSH fallback details are provided,
    fall back to a direct SSH command.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.run_bash(command, timeout=timeout)
        except (
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
        except requests.exceptions.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code not in (502, 503, 504):
                raise
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue

    if ssh_host and ssh_user:
        try:
            return _run_bash_via_ssh(
                ssh_host,
                ssh_user,
                command,
                timeout=timeout,
                control_path=ssh_control_path,
            )
        except Exception as ssh_exc:
            raise RuntimeError(
                f"REST fallback via SSH also failed: {ssh_exc}"
            ) from (last_exc or ssh_exc)

    if last_exc:
        raise last_exc
    raise RuntimeError("Unreachable: run_bash retry loop exited without a result.")


def _run_readonly_bash_prefer_ssh(
    client: BigIPClient,
    command: str,
    *,
    timeout: int,
    max_retries: int,
    retry_delay: int,
    ssh_user: Optional[str] = None,
    ssh_control_path: Optional[str] = None,
) -> Any:
    """Use an authenticated SSH master first for read-only shell commands."""
    if ssh_user and ssh_control_path:
        try:
            return _run_bash_via_ssh(
                client.host,
                ssh_user,
                command,
                timeout=timeout,
                control_path=ssh_control_path,
            )
        except Exception as ssh_exc:
            print(
                "\n[i] Authenticated SSH read failed; falling back to "
                f"iControl REST: {type(ssh_exc).__name__}: {ssh_exc}"
            )

    return _run_bash_with_retry(
        client,
        command,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        ssh_host=client.host if ssh_user else None,
        ssh_user=ssh_user,
        ssh_control_path=ssh_control_path,
    )


def _is_ambiguous_submission_error(error: Exception) -> bool:
    """Return True when BIG-IP may have accepted a timed-out REST request."""
    if isinstance(
        error,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return True
    if isinstance(error, requests.exceptions.HTTPError):
        response = getattr(error, "response", None)
        return getattr(response, "status_code", None) in (502, 503, 504)
    return False


def _get_volume_state_via_ssh(
    host: str,
    user: str,
    control_path: Optional[str],
    volume: str,
) -> VolumeState:
    """Read target-volume state through the authenticated SSH connection."""
    response = _run_bash_via_ssh(
        host,
        user,
        "tmsh show sys software status",
        timeout=30,
        control_path=control_path,
    )
    return _parse_volume_from_tmsh_status(
        _remote_command_output(response),
        volume,
    )


def _volume_indicates_install_started(
    before: VolumeState,
    after: VolumeState,
    expected_version: str,
) -> bool:
    """Determine whether state changed enough to prove submission started."""
    if not after.found:
        return False

    status = after.status.lower()
    in_progress = any(
        marker in status
        for marker in (
            "installing",
            "testing",
            "copying",
            "pending",
            "waiting",
            "validating",
        )
    )
    version_matches = bool(
        expected_version and expected_version in after.version
    )

    if not before.found:
        return in_progress or version_matches

    state_changed = (
        before.version != after.version
        or before.status != after.status
        or before.build != after.build
        or before.raw != after.raw
    )
    return state_changed and (in_progress or version_matches)


def check_license_dates(
    client: BigIPClient,
    image_name: str,
    ssh_user: Optional[str] = None,
    ssh_control_path: Optional[str] = None,
) -> CheckResult:
    """Validate the ISO license-check date against the device service date."""
    result_id = "LIC-001"
    result_name = "ISO and service-check dates validated"
    remote_iso = f"/shared/images/{image_name}"
    timeout = LICENSE_DATE_TIMEOUT_SECONDS
    max_retries = LICENSE_DATE_MAX_RETRIES
    retry_delay = LICENSE_DATE_RETRY_DELAY_SECONDS
    ssh_host = client.host if ssh_user else None
    stage = "version_date lookup"

    print("\n[LIC-001] Validating ISO license-check date...")
    print(f"Image: {image_name}")
    if ssh_user and ssh_control_path:
        print("Validation transport: authenticated SSH ControlMaster")

    try:
        version_response = _run_readonly_bash_prefer_ssh(
            client,
            "isoinfo -f -R -i "
            f"{_shell_quote(remote_iso)} | grep -m1 'version_date'",
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            ssh_user=ssh_user,
            ssh_control_path=ssh_control_path,
        )
        version_path = next(
            (
                line.strip()
                for line in _remote_command_output(version_response).splitlines()
                if "version_date" in line
            ),
            "",
        )
        if not version_path:
            raise ValueError("ISO version_date entry was not found.")

        stage = "ISO license-check date read"
        date_response = _run_readonly_bash_prefer_ssh(
            client,
            "isoinfo -R -i "
            f"{_shell_quote(remote_iso)} -x {_shell_quote(version_path)}",
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            ssh_user=ssh_user,
            ssh_control_path=ssh_control_path,
        )
        iso_date = _extract_yyyymmdd(_remote_command_output(date_response))
        if not iso_date:
            raise ValueError("ISO license-check date was not found.")

        stage = "service-check date read"
        service_response = _run_readonly_bash_prefer_ssh(
            client,
            "grep -F 'Service check date' /config/bigip.license",
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            ssh_user=ssh_user,
            ssh_control_path=ssh_control_path,
        )
        service_date = _extract_yyyymmdd(
            _remote_command_output(service_response)
        )
        if not service_date:
            raise ValueError("BIG-IP service-check date was not found.")

        reactivation_required = service_date < iso_date
        status = "FAIL" if reactivation_required else "PASS"
        print(f"ISO license-check date: {iso_date}")
        print(f"Device service-check date: {service_date}")
        print(
            "License reactivation required: "
            f"{'Yes' if reactivation_required else 'No'}"
        )
        print(f"[LIC-001] {status}")
        return CheckResult(
            id=result_id,
            category="License and Platform Readiness",
            name=result_name,
            status=status,
            details={
                "image": image_name,
                "iso_license_check_date": iso_date,
                "service_check_date": service_date,
                "license_reactivation_required": reactivation_required,
                "error": (
                    "License reactivation is required before upgrade."
                    if reactivation_required
                    else ""
                ),
            },
        )
    except Exception as exc:
        print(f"[LIC-001] FAIL during {stage}: {type(exc).__name__}: {exc}")
        return CheckResult(
            id=result_id,
            category="License and Platform Readiness",
            name=result_name,
            status="FAIL",
            details={
                "image": image_name,
                "stage": stage,
                "timeout_seconds": timeout,
                "max_retries": max_retries,
                "ssh_fallback_attempted": bool(ssh_host and ssh_user),
                "ssh_fallback_reused_existing_connection": bool(
                    ssh_control_path
                ),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def _extract_yyyymmdd(output: str) -> str:
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", output or "")
    return match.group(1) if match else ""


def _is_hotfix_image(image_name: str) -> bool:
    return os.path.basename(image_name).lower().startswith("hotfix-bigip-")


def _execution_role_allowed(role: str, allow_standalone: bool) -> bool:
    """Allow STANDBY, or ACTIVE only for a topology-verified standalone."""
    normalized_role = (role or "").strip().lower()
    return normalized_role == "standby" or (
        allow_standalone and normalized_role == "active"
    )


def _execution_mode(role: str, allow_standalone: bool) -> str:
    """Return a human-readable execution mode for evidence and messages."""
    if allow_standalone and (role or "").strip().lower() == "active":
        return "standalone"
    return "ha-standby"


def prepare_install_storage(
    client: BigIPClient,
    target_volume: str,
    *,
    require_upload_space: bool,
    expected_image_contains: str = "",
    skip_iso_cleanup: bool = False,
    allow_standalone: bool = False,
) -> CheckResult:
    """Display storage state and safely prepare an inactive target volume."""
    result_id = "EXEC-STORAGE-001"
    result_name = "Storage and target volume readiness"
    details: Dict[str, Any] = {"target_volume": target_volume}

    try:
        role = (get_failover_role(client) or "").lower()
    except Exception as exc:
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={"error": f"Could not determine failover role: {exc}"},
        )

    if not _execution_role_allowed(role, allow_standalone):
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Storage preparation requires an HA STANDBY device or "
                    "a topology-verified standalone device."
                ),
                "role": role,
                "allow_standalone": allow_standalone,
            },
        )

    details["role"] = role
    details["execution_mode"] = _execution_mode(role, allow_standalone)

    if skip_iso_cleanup:
        print(
            "\nRequested upgrade image is already present in /shared/images/. "
            "Skipping ISO cleanup and upload-space validation."
        )
        details["skipped_iso_cleanup"] = True
    else:
        image_response = client.run_bash("ls -lh /shared/images/ 2>/dev/null | head -n 500", timeout=60)
        image_listing = _remote_command_output(image_response)
        details["image_listing"] = image_listing
        print("\nImages under /shared/images:")
        print(image_listing or "(none)")

        if require_upload_space:
            required_kib = 8 * 1024 * 1024
            while True:
                try:
                    available_kib, df_output = _available_space_kib(client)
                except ValueError as exc:
                    return CheckResult(
                        id=result_id,
                        category="Execution Readiness",
                        name=result_name,
                        status="FAIL",
                        details={**details, "error": str(exc)},
                    )
                details["disk_usage"] = df_output
                available_gib = available_kib / (1024 * 1024)
                print(f"\n/shared/images filesystem free space: {available_gib:.2f} GiB (minimum 8.00 GiB)")
                if available_kib >= required_kib:
                    break

                image_names = _available_iso_names(client)
                if not image_names:
                    return CheckResult(
                        id=result_id,
                        category="Execution Readiness",
                        name=result_name,
                        status="FAIL",
                        details={
                            **details,
                            "available_gib": round(available_gib, 2),
                            "required_gib": 8,
                            "error": "Insufficient free space to upload the ISO and no ISO files remain for cleanup.",
                        },
                    )

                print("\nNumbered ISO files:")
                for index, name in enumerate(image_names, 1):
                    print(f"  {index}. {name}")
                if not sys.stdin.isatty():
                    return CheckResult(
                        id=result_id,
                        category="Execution Readiness",
                        name=result_name,
                        status="FAIL",
                        details={
                            **details,
                            "available_gib": round(available_gib, 2),
                            "required_gib": 8,
                            "error": "ISO cleanup requires confirmation.",
                        },
                    )

                answer = input("Free space is below 8 GiB. Delete ISO files? Enter numbers separated by commas: ").strip()
                try:
                    selected = sorted({int(item.strip()) for item in answer.split(",")})
                    if not selected or any(index < 1 or index > len(image_names) for index in selected):
                        raise IndexError
                    selected_names = [image_names[index - 1] for index in selected]
                except (ValueError, IndexError):
                    return CheckResult(
                        id=result_id,
                        category="Execution Readiness",
                        name=result_name,
                        status="FAIL",
                        details={**details, "error": "Invalid ISO deletion selection."},
                    )

                print("Selected ISO files:")
                for name in selected_names:
                    print(f"  - {name}")
                confirm = input("Confirm deletion? (y/N): ").strip().lower()
                if confirm not in ("y", "yes"):
                    return CheckResult(
                        id=result_id,
                        category="Execution Readiness",
                        name=result_name,
                        status="FAIL",
                        details={**details, "error": "ISO deletion was not confirmed while space was insufficient."},
                    )

                for name in selected_names:
                    delete_response = client.run_bash(f"rm -f -- {_shell_quote('/shared/images/' + name)}", timeout=60)
                    delete_output = _remote_command_output(delete_response)
                    if _is_fatal_tmsh_output(delete_output):
                        return CheckResult(
                            id=result_id,
                            category="Execution Readiness",
                            name=result_name,
                            status="FAIL",
                            details={
                                **details,
                                "deleted_iso": name,
                                "delete_output": delete_output,
                                "error": f"Could not delete ISO file {name}.",
                            },
                        )
                details.setdefault("deleted_iso_files", []).extend(selected_names)

    volume_response = client.run_bash("tmsh show sys software", timeout=60)
    volume_listing = _remote_command_output(volume_response)
    details["volume_listing"] = volume_listing
    print("\nAvailable software volumes:")
    print(volume_listing or "(none)")

    existing = _get_volume_state(client, target_volume)
    if not existing.found:
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="PASS",
            details=details,
        )

    if existing.active.strip().lower() in ("yes", "true", "active"):
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={
                **details,
                "volume_state": existing.__dict__,
                "error": f"{target_volume} is the active boot volume and cannot be deleted.",
            },
        )

    if (
        expected_image_contains
        and expected_image_contains.lower() in existing.version.lower()
        and "complete" in existing.status.lower()
    ):
        print(
            f"\nTarget volume {target_volume} already contains the requested "
            f"version {existing.version}; it will be reused."
        )
        details["volume_state"] = existing.__dict__
        details["reused_volume"] = True
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="PASS",
            details=details,
        )

    print(f"\nTarget volume {target_volume} contains version {existing.version or '(unknown)'} with status '{existing.status}'.")
    if not sys.stdin.isatty():
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={**details, "volume_state": existing.__dict__, "error": "Target volume cleanup requires confirmation."},
        )

    answer = input(f"Delete and recreate inactive volume {target_volume}? (y/N): ").strip().lower()
    if answer not in ("y", "yes"):
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={**details, "volume_state": existing.__dict__, "error": "Target volume replacement was not confirmed."},
        )

    delete_output = _remote_command_output(
        client.run_bash(f"tmsh delete sys software volume {_shell_quote(target_volume)}", timeout=120)
    )
    if _is_fatal_tmsh_output(delete_output):
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={**details, "volume_state": existing.__dict__, "delete_output": delete_output},
        )

    details["deleted_volume"] = target_volume
    return CheckResult(
        id=result_id,
        category="Execution Readiness",
        name=result_name,
        status="PASS",
        details=details,
    )


def exec_upload_iso_standby(
    client: BigIPClient,
    host: str,
    scp_user: str,
    iso_local_path: str,
    *,
    allow_standalone: bool = False,
    ssh_control_path: Optional[str] = None,
) -> CheckResult:
    """Upload the ISO to /shared/images on a standby BIG-IP."""
    result_id = "EXEC-UPLOAD-ISO-001"
    result_name = "Upload ISO to /shared/images (standby only)"

    role = (get_failover_role(client) or "").lower()
    if not _execution_role_allowed(role, allow_standalone):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing upload because the device is neither HA "
                    "STANDBY nor a topology-verified standalone device."
                ),
                "role": role,
                "allow_standalone": allow_standalone,
            },
        )

    if not iso_local_path:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"error": "ISO_LOCAL_PATH is empty."},
        )

    iso_local_path = os.path.expanduser(iso_local_path)
    if not os.path.isfile(iso_local_path):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"error": "ISO_LOCAL_PATH does not exist or is not a file.", "iso_local_path": iso_local_path},
        )

    basename = os.path.basename(iso_local_path)
    remote_path = f"/shared/images/{basename}"
    destination = f"{scp_user}@{host}:/shared/images/"
    local_size = os.path.getsize(iso_local_path)
    local_sha256 = _sha256_file(iso_local_path)

    command = ["scp", "-O"]
    if ssh_control_path:
        command.extend(
            [
                "-o",
                f"ControlPath={ssh_control_path}",
                "-o",
                "ControlMaster=auto",
                "-o",
                "BatchMode=yes",
            ]
        )
    command.extend([iso_local_path, destination])

    print("\nUploading ISO to BIG-IP /shared/images ...")
    print(f"Source: {iso_local_path}")
    print(f"Destination: {destination}")
    print("Using legacy SCP protocol with -O; remote timestamps are not preserved.")

    started = time.monotonic()
    try:
        completed = subprocess.run(command, timeout=3600)
    except subprocess.TimeoutExpired:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"error": "SCP upload timed out.", "command": " ".join(command)},
        )
    except Exception as exc:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"error": str(exc), "command": " ".join(command)},
        )

    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": "SCP upload failed.",
                "command": " ".join(command),
                "returncode": completed.returncode,
                "elapsed_seconds": round(elapsed, 3),
                "reused_ssh_controlmaster": bool(ssh_control_path),
            },
        )

    verify_command = f"test -f {_shell_quote(remote_path)} && stat -c %s {_shell_quote(remote_path)} && sha256sum {_shell_quote(remote_path)}"
    try:
        verify_response = client.run_bash(verify_command, timeout=120)
        verify_output = _remote_command_output(verify_response)

        if not verify_output:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={"error": "Remote ISO verification returned no output.", "remote_path": remote_path},
            )

        remote_size = None
        remote_sha256 = None
        for line in verify_output.splitlines():
            line = line.strip()
            if line.isdigit():
                remote_size = int(line)
                continue
            hash_match = re.search(r"\b([0-9a-fA-F]{64})\b", line)
            if hash_match:
                remote_sha256 = hash_match.group(1).lower()

        if remote_size is None or remote_sha256 is None:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={"error": "Could not determine remote ISO size or hash.", "remote_path": remote_path},
            )

        if remote_size != local_size:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={"error": "Remote ISO size does not match local size.", "remote_path": remote_path, "local_size": local_size, "remote_size": remote_size},
            )

        if remote_sha256 != local_sha256:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={"error": "Remote ISO SHA-256 does not match local hash.", "remote_path": remote_path, "local_sha256": local_sha256, "remote_sha256": remote_sha256},
            )

        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="PASS",
            details={
                "role": role,
                "remote_path": remote_path,
                "local_path": iso_local_path,
                "local_size": local_size,
                "remote_size": remote_size,
                "sha256": local_sha256,
                "elapsed_seconds": round(elapsed, 3),
                "reused_ssh_controlmaster": bool(ssh_control_path),
            },
        )
    except Exception as exc:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"error": "Exception while verifying uploaded ISO.", "remote_path": remote_path, "exception": str(exc)},
        )


def exec_upload_files_standby(
    client: BigIPClient,
    host: str,
    scp_user: str,
    iso_local_paths: List[str],
    *,
    allow_standalone: bool = False,
    ssh_control_path: Optional[str] = None,
) -> List[CheckResult]:
    """Upload multiple ISO files sequentially, verifying each upload."""
    return [
        exec_upload_iso_standby(
            client,
            host=host,
            scp_user=scp_user,
            iso_local_path=path,
            allow_standalone=allow_standalone,
            ssh_control_path=ssh_control_path,
        )
        for path in iso_local_paths
    ]


def exec_install_standby(
    client: BigIPClient,
    image_iso_name: str,
    target_volume: str,
    *,
    force_install: bool = False,
    create_volume: Optional[bool] = None,
    ssh_user: Optional[str] = None,
    ssh_control_path: Optional[str] = None,
    allow_standalone: bool = False,
) -> CheckResult:
    """Submit image installation to the target standby volume.

    The install command retries transient REST failures and then falls
    back to the existing SSH ControlMaster when REST remains unavailable,
    matching the pattern used for the image-presence check. This prevents
    a temporary icrd outage from failing the install submission outright.
    """
    result_id = "EXEC-INSTALL-001"
    result_name = "Install image to standby volume (no reboot)"

    role = (get_failover_role(client) or "").lower()
    if not _execution_role_allowed(role, allow_standalone):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing install because the device is neither HA "
                    "STANDBY nor a topology-verified standalone device."
                ),
                "role": role,
                "allow_standalone": allow_standalone,
            },
        )

    expected_version = _extract_version_from_image_name(image_iso_name)
    existing = _get_volume_state(client, target_volume)

    if existing.found:
        status = existing.status.lower()
        version_matches = bool(expected_version and expected_version in existing.version)
        progress_states = ["installing", "complete", "testing", "copying", "pending", "waiting", "validating"]

        if not force_install and version_matches and any(state in status for state in progress_states):
            print(f"\nTarget volume {target_volume} already exists with version {existing.version} and status '{existing.status}'.")
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "chosen_volume": target_volume,
                    "note": "Target volume already exists with the expected version.",
                    "volume_state": existing.__dict__,
                },
            )

        if not force_install:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "chosen_volume": target_volume,
                    "error": "Target volume already exists but does not contain the expected target version.",
                    "expected_version": expected_version,
                    "volume_state": existing.__dict__,
                },
            )

    image_name = os.path.basename(image_iso_name.strip())
    if not image_name or image_name != image_iso_name.strip():
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"role": role, "image": image_iso_name, "chosen_volume": target_volume, "error": "Image name must be a filename located directly under /shared/images."},
        )

    install_type = "hotfix" if _is_hotfix_image(image_name) else "image"
    if create_volume is None:
        create_volume = not existing.found

    image_path = f"/shared/images/{image_name}"
    tmsh_command = f"cd /shared/images && tmsh install sys software {install_type} {_shell_quote(image_name)} volume {_shell_quote(target_volume)}"
    if create_volume:
        tmsh_command += " create-volume"

    print("\nSubmitting BIG-IP software install command:")
    print(tmsh_command)
    print("Install progress will be monitored in the next step.")

    submission_method = "rest-single-attempt"
    ambiguous_rest_error = ""

    try:
        # Do not use _run_bash_with_retry here. Installation is
        # non-idempotent and must never be blindly resubmitted after a
        # timeout with an unknown server-side outcome.
        response = client.run_bash_once(
            tmsh_command,
            timeout=INSTALL_SUBMIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if not _is_ambiguous_submission_error(exc):
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "install_type": install_type,
                    "image_path": image_path,
                    "chosen_volume": target_volume,
                    "tmsh": tmsh_command,
                    "error": str(exc),
                    "submission_method": submission_method,
                    "automatic_resubmission": False,
                },
            )

        ambiguous_rest_error = f"{type(exc).__name__}: {exc}"
        print(
            "\n[i] Install submission returned an ambiguous REST error. "
            "Checking target-volume state before any fallback submission."
        )

        observed_state = VolumeState(
            found=False,
            volume=target_volume,
            source="not_checked",
        )
        state_source = observed_state.source

        # Allow BIG-IP time to publish the newly created or transitioning
        # volume. An immediate single check can race the install worker and
        # incorrectly conclude that the command never started.
        for verify_attempt in range(1, INSTALL_STATE_VERIFY_ATTEMPTS + 1):
            observed_state = _get_volume_state(client, target_volume)
            state_source = observed_state.source
            if _volume_indicates_install_started(
                existing,
                observed_state,
                expected_version,
            ):
                break

            if ssh_user:
                try:
                    observed_state = _get_volume_state_via_ssh(
                        client.host,
                        ssh_user,
                        ssh_control_path,
                        target_volume,
                    )
                    state_source = "ssh_tmsh_status"
                except Exception as state_exc:
                    state_source = (
                        "state verification failed: "
                        f"{type(state_exc).__name__}: {state_exc}"
                    )

                if _volume_indicates_install_started(
                    existing,
                    observed_state,
                    expected_version,
                ):
                    break

            if verify_attempt < INSTALL_STATE_VERIFY_ATTEMPTS:
                time.sleep(INSTALL_STATE_VERIFY_DELAY_SECONDS)

        if _volume_indicates_install_started(
            existing,
            observed_state,
            expected_version,
        ):
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "install_type": install_type,
                    "image_path": image_path,
                    "chosen_volume": target_volume,
                    "tmsh": tmsh_command,
                    "note": (
                        "REST response was ambiguous, but target-volume "
                        "state confirmed that installation started. The "
                        "command was not resubmitted."
                    ),
                    "ambiguous_rest_error": ambiguous_rest_error,
                    "verified_volume_state": observed_state.__dict__,
                    "state_verification_source": state_source,
                    "automatic_resubmission": False,
                },
            )

        if not ssh_user:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "install_type": install_type,
                    "image_path": image_path,
                    "chosen_volume": target_volume,
                    "tmsh": tmsh_command,
                    "error": (
                        "Install outcome is unknown after an ambiguous REST "
                        "error. No SSH connection was available, so the "
                        "command was not resubmitted. Verify the target "
                        "volume manually before retrying."
                    ),
                    "ambiguous_rest_error": ambiguous_rest_error,
                    "verified_volume_state": observed_state.__dict__,
                    "state_verification_source": state_source,
                    "automatic_resubmission": False,
                },
            )

        try:
            # State verification found no evidence that REST started the
            # installation. Submit exactly once over the existing SSH path.
            response = _run_bash_via_ssh(
                client.host,
                ssh_user,
                tmsh_command,
                timeout=INSTALL_SUBMIT_TIMEOUT_SECONDS,
                control_path=ssh_control_path,
            )
            submission_method = "ssh-after-state-verification"
        except Exception as ssh_exc:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "install_type": install_type,
                    "image_path": image_path,
                    "chosen_volume": target_volume,
                    "tmsh": tmsh_command,
                    "error": f"Verified SSH submission failed: {ssh_exc}",
                    "ambiguous_rest_error": ambiguous_rest_error,
                    "verified_volume_state": observed_state.__dict__,
                    "state_verification_source": state_source,
                    "automatic_resubmission": False,
                },
            )

    output = _remote_command_output(response)
    if _is_fatal_tmsh_output(output):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "role": role,
                "image": image_iso_name,
                "install_type": install_type,
                "image_path": image_path,
                "chosen_volume": target_volume,
                "tmsh": tmsh_command,
                "error": "BIG-IP rejected the install command.",
                "command_result": output,
                "submission_method": submission_method,
                "ambiguous_rest_error": ambiguous_rest_error,
                "automatic_resubmission": False,
            },
        )

    return CheckResult(
        id=result_id,
        category="Execution",
        name=result_name,
        status="PASS",
        details={
            "role": role,
            "image": image_iso_name,
            "install_type": install_type,
            "image_path": image_path,
            "chosen_volume": target_volume,
            "tmsh": tmsh_command,
            "note": (
                "Install command submitted once. Completion is verified "
                "by volume readiness polling."
            ),
            "command_result": output,
            "submission_method": submission_method,
            "ambiguous_rest_error": ambiguous_rest_error,
            "automatic_resubmission": False,
            "ssh_fallback_reused_existing_connection": bool(
                ssh_control_path
            ),
        },
    )


def exec_volume_ready(
    client: BigIPClient,
    volume: str,
    expect_version_contains: str,
    timeout_sec: int = 3600,
    interval_sec: int = 20,
) -> CheckResult:
    """Wait for target-volume installation to complete."""
    result_id = "EXEC-VOL-READY-001"
    result_name = "Target volume ready"
    deadline = time.time() + timeout_sec
    last: Dict[str, Any] = {"volume": volume, "version": "", "status": "", "source": "", "error": ""}

    print(f"\nWaiting for target volume {volume} to become ready.")
    print(f"Expected version contains: {expect_version_contains}")

    while time.time() < deadline:
        state = _get_volume_state(client, volume)
        remaining = max(0, int(deadline - time.time()))

        last = {
            "volume": state.volume,
            "version": state.version,
            "status": state.status,
            "build": state.build,
            "active": state.active,
            "source": state.source,
            "raw": state.raw,
            "error": state.error,
            "seconds_remaining": remaining,
        }

        if not state.found:
            print(f"[{_now()}] Volume {volume} is not visible yet. Remaining: {remaining}s")
            time.sleep(interval_sec)
            continue

        status = state.status.lower()
        version_matches = bool(expect_version_contains and expect_version_contains in state.version)

        print(f"[{_now()}] Volume {volume}: version='{state.version}', status='{state.status}', source='{state.source}', remaining={remaining}s")

        if "failed" in status or "error" in status:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={"error": "BIG-IP reports a failed/error state for the target volume.", **last, "expected_version": expect_version_contains},
            )

        progress_states = ["installing", "testing", "copying", "pending", "waiting", "validating"]
        if any(state_name in status for state_name in progress_states):
            time.sleep(interval_sec)
            continue

        if "complete" in status:
            if not version_matches:
                return CheckResult(
                    id=result_id,
                    category="Execution",
                    name=result_name,
                    status="FAIL",
                    details={"error": "Target volume is complete but the version does not match.", **last, "expected_version": expect_version_contains},
                )

            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={"note": "Target volume installation is complete and the version matches.", **last, "expected_version": expect_version_contains},
            )

        time.sleep(interval_sec)

    return CheckResult(
        id=result_id,
        category="Execution",
        name=result_name,
        status="FAIL",
        details={"error": "Timeout waiting for target volume installation to complete.", **last, "expected_version": expect_version_contains, "timeout_sec": timeout_sec},
    )


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _is_expected_reboot_disconnect(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("remotedisconnected", "remote end closed connection", "connection aborted", "connection reset", "badstatusline"))


def exec_reboot_to_volume_standby(
    client: BigIPClient,
    volume: str,
    *,
    allow_standalone: bool = False,
) -> CheckResult:
    """Reboot the standby BIG-IP into the target volume."""
    result_id = "EXEC-REBOOT-TO-VOL-001"
    result_name = "Reboot to target volume (standby only)"

    role = (get_failover_role(client) or "").lower()
    if not _execution_role_allowed(role, allow_standalone):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing reboot because the device is neither HA "
                    "STANDBY nor a topology-verified standalone device."
                ),
                "role": role,
                "volume": volume,
                "allow_standalone": allow_standalone,
            },
        )

    tmsh_command = f"tmsh reboot volume {volume}"
    mode = _execution_mode(role, allow_standalone)
    print(f"\nRebooting {mode.upper()} device into target volume {volume}.")
    print(tmsh_command)

    try:
        response = client.run_bash(tmsh_command, timeout=60)
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="PASS",
            details={"role": role, "volume": volume, "tmsh": tmsh_command, "command_result": _remote_command_output(response)},
        )
    except Exception as exc:
        if _is_expected_reboot_disconnect(exc):
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={"role": role, "volume": volume, "tmsh": tmsh_command, "note": "Disconnect during reboot is expected.", "exception": str(exc)},
            )
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={"role": role, "volume": volume, "tmsh": tmsh_command, "error": str(exc)},
        )


def exec_wait_postboot(
    client: BigIPClient,
    expect_version_contains: str,
    timeout_sec: int = 900,
    interval_sec: int = 15,
    allow_standalone: bool = False,
) -> CheckResult:
    """Validate version and the expected HA-standby or standalone role."""
    result_id = "VAL-POSTBOOT-001"
    result_name = "Post-boot validation"
    deadline = time.time() + timeout_sec
    last_error = ""
    last_seen: Dict[str, Any] = {}

    print("\nWaiting for BIG-IP to return after reboot.")

    while time.time() < deadline:
        remaining = max(0, int(deadline - time.time()))
        try:
            version_payload = client.system_version()
            role = get_failover_role(client)
            last_seen = {"system_version": version_payload, "role": role}
            version_text = str(version_payload)

            print(f"[{_now()}] Device reachable. role='{role}', remaining={remaining}s")

            if expect_version_contains and expect_version_contains not in version_text:
                last_error = "Version does not match expectation yet."
            elif not _execution_role_allowed(role or "", allow_standalone):
                expected_role = "ACTIVE (standalone)" if allow_standalone else "STANDBY"
                last_error = (
                    f"Device is not in the expected {expected_role} role yet. "
                    f"Current role: {role}"
                )
            else:
                mode = _execution_mode(role or "", allow_standalone)
                return CheckResult(
                    id=result_id,
                    category="Validation",
                    name=result_name,
                    status="PASS",
                    details={
                        "note": (
                            "Device is reachable with the expected version "
                            f"and valid {mode} role."
                        ),
                        "execution_mode": mode,
                        **last_seen,
                    },
                )
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            print(f"[{_now()}] Device is not reachable yet. Remaining: {remaining}s")
        except Exception as exc:
            last_error = str(exc)
            print(f"[{_now()}] Waiting after reboot. Last error: {last_error}. Remaining: {remaining}s")

        time.sleep(interval_sec)

    return CheckResult(
        id=result_id,
        category="Validation",
        name=result_name,
        status="FAIL",
        details={"error": "Timeout waiting for post-boot readiness.", "last_error": last_error, "last_seen": last_seen, "timeout_sec": timeout_sec},
    )
