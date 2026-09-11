from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

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

    return str(
        response.get("commandResult", "")
    ).strip()


def _extract_version_from_image_name(
    image_iso_name: str,
) -> str:
    """
    Extract a version from names such as:
    BIGIP-17.5.1.9-0.0.12.iso
    """
    match = re.search(
        r"BIGIP-(\d+(?:\.\d+)+)-",
        image_iso_name or "",
        re.IGNORECASE,
    )

    return match.group(1) if match else ""


def _is_fatal_tmsh_output(output: str) -> bool:
    """
    Detect tmsh output that indicates the command was rejected.
    """
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

    return any(
        marker in text
        for marker in fatal_markers
    )


def _parse_volume_from_tmsh_status(
    output: str,
    volume: str,
) -> VolumeState:
    """
    Parse one volume row from:

      tmsh show sys software status
    """
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
            pct_match = re.search(
                r"installing\s+([\d.]+)\s+pct",
                line,
                re.IGNORECASE,
            )

            if pct_match:
                status = (
                    f"installing "
                    f"{pct_match.group(1)} pct"
                )
            else:
                status = "installing"

        elif "complete" in lower:
            status = "complete"

        elif "failed" in lower:
            status = "failed"

        elif "error" in lower:
            status = "error"

        else:
            status = (
                " ".join(parts[5:])
                if len(parts) > 5
                else ""
            )

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
        error=(
            f"Volume {volume} was not found in "
            "tmsh software status output."
        ),
    )


def _get_volume_state(
    client: BigIPClient,
    volume: str,
) -> VolumeState:
    """
    Read software volume state.

    REST is attempted first. If the volume is not indexed yet, fall back
    to tmsh show sys software status.
    """
    try:
        payload = client.get(
            f"/mgmt/tm/sys/software/volume/{volume}"
        )

        return VolumeState(
            found=True,
            volume=volume,
            version=str(
                payload.get("version", "")
            ),
            status=str(
                payload.get("status", "")
            ),
            build=str(
                payload.get("build", "")
            ),
            active=str(
                payload.get("active", "")
            ),
            source="icontrol_volume",
            raw=str(payload),
        )

    except Exception as rest_error:
        try:
            response = client.run_bash(
                "tmsh show sys software status"
            )

            output = _remote_command_output(response)

            parsed = _parse_volume_from_tmsh_status(
                output,
                volume,
            )

            if parsed.found:
                return parsed

            return VolumeState(
                found=False,
                volume=volume,
                source="icontrol_then_tmsh_status",
                raw=output,
                error=(
                    f"{type(rest_error).__name__}: "
                    f"{rest_error}"
                ),
            )

        except Exception as tmsh_error:
            return VolumeState(
                found=False,
                volume=volume,
                source="icontrol_then_tmsh_status",
                error=(
                    f"REST error: "
                    f"{type(rest_error).__name__}: "
                    f"{rest_error}; "
                    f"tmsh status error: "
                    f"{type(tmsh_error).__name__}: "
                    f"{tmsh_error}"
                ),
            )


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()

    with open(path, "rb") as file_handle:
        for block in iter(
            lambda: file_handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def check_image_present(
    client: BigIPClient,
    image_name_contains: str,
) -> ImagePresenceResult:
    """
    Verify that the ISO physically exists under /shared/images.

    The filesystem result is authoritative. REST image inventory is retained
    only as supplemental information because stale REST records can otherwise
    cause the upload step to be skipped.
    """
    needle = (
        image_name_contains or ""
    ).strip().lower()

    available_images = []
    rest_error = ""

    try:
        payload = client.software_images()

        items = (
            payload.get("items", [])
            if isinstance(payload, dict)
            else []
        )

        available_images = [
            str(item.get("name"))
            for item in items
            if isinstance(item, dict)
            and item.get("name")
        ]

    except Exception as exc:
        available_images = []
        rest_error = (
            f"{type(exc).__name__}: {exc}"
        )

    try:
        response = client.run_bash(
            "ls -1 /shared/images/ "
            "2>/dev/null | "
            "head -n 500",
            timeout=60,
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
                "error": (
                    "Could not inspect /shared/images: "
                    f"{type(exc).__name__}: {exc}"
                ),
            },
        )

    filesystem_matches = [
        image
        for image in filesystem_images
        if needle in image.lower()
    ]

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
            "rest_inventory_may_be_stale": any(
                needle in image.lower()
                for image in available_images
            ),
        },
    )


def _is_hotfix_image(image_name: str) -> bool:
    return os.path.basename(image_name).lower().startswith("hotfix-bigip-")


def prepare_install_storage(
    client: BigIPClient,
    target_volume: str,
    *,
    require_upload_space: bool,
    expected_image_contains: str = "",
) -> CheckResult:
    """
    Display storage state and safely prepare an inactive target volume.

    The filesystem containing /shared/images must have at least 8 GiB free
    when an upload is requested. The active boot volume is never deleted.
    """
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

    if role != "standby":
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={"error": "Storage preparation requires a STANDBY device.", "role": role},
        )

    if require_upload_space:
        response = client.run_bash(
            "df -Pk /shared/images",
            timeout=60,
        )
        df_output = _remote_command_output(response)
        details["disk_usage"] = df_output
        data_lines = [
            line.split()
            for line in df_output.splitlines()
            if line.strip() and not line.lower().startswith("filesystem")
        ]
        if not data_lines or len(data_lines[-1]) < 5:
            return CheckResult(
                id=result_id,
                category="Execution Readiness",
                name=result_name,
                status="FAIL",
                details={**details, "error": "Could not parse free space for /shared/images."},
            )
        try:
            available_kib = int(data_lines[-1][3])
        except ValueError:
            return CheckResult(
                id=result_id,
                category="Execution Readiness",
                name=result_name,
                status="FAIL",
                details={**details, "error": "Free-space value from df was not numeric."},
            )
        required_kib = 8 * 1024 * 1024
        available_gib = available_kib / (1024 * 1024)
        print(
            f"\n/shared/images filesystem free space: {available_gib:.2f} GiB "
            "(minimum 8.00 GiB)"
        )
        if available_kib < required_kib:
            return CheckResult(
                id=result_id,
                category="Execution Readiness",
                name=result_name,
                status="FAIL",
                details={
                    **details,
                    "available_gib": round(available_gib, 2),
                    "required_gib": 8,
                    "error": "Insufficient free space to upload the ISO.",
                },
            )

    image_response = client.run_bash(
        "ls -lh /shared/images/ 2>/dev/null | head -n 500",
        timeout=60,
    )
    image_listing = _remote_command_output(image_response)
    details["image_listing"] = image_listing
    print("\nImages under /shared/images:")
    print(image_listing or "(none)")

    image_names_response = client.run_bash(
        "find /shared/images -maxdepth 1 -type f -name '*.iso' -printf '%f\\n' "
        "| sort",
        timeout=60,
    )
    image_names = [
        line.strip()
        for line in _remote_command_output(image_names_response).splitlines()
        if line.strip() and os.path.basename(line.strip()) == line.strip()
    ]
    if image_names:
        print("\nNumbered ISO files:")
        for index, name in enumerate(image_names, 1):
            print(f"  {index}. {name}")
        if sys.stdin.isatty():
            answer = input(
                "Delete any ISO files? Enter numbers separated by commas, or press Enter to keep all: "
            ).strip()
            if answer:
                try:
                    selected = sorted({int(item.strip()) for item in answer.split(",")})
                    if any(index < 1 or index > len(image_names) for index in selected):
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
                if confirm in ("y", "yes"):
                    for name in selected_names:
                        client.run_bash(
                            f"rm -f -- {_shell_quote('/shared/images/' + name)}",
                            timeout=60,
                        )

    volume_response = client.run_bash(
        "tmsh show sys software",
        timeout=60,
    )
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

    print(
        f"\nTarget volume {target_volume} contains version "
        f"{existing.version or '(unknown)'} with status '{existing.status}'."
    )
    if not sys.stdin.isatty():
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={**details, "volume_state": existing.__dict__, "error": "Target volume cleanup requires confirmation."},
        )

    answer = input(
        f"Delete and recreate inactive volume {target_volume}? (y/N): "
    ).strip().lower()
    if answer not in ("y", "yes"):
        return CheckResult(
            id=result_id,
            category="Execution Readiness",
            name=result_name,
            status="FAIL",
            details={**details, "volume_state": existing.__dict__, "error": "Target volume replacement was not confirmed."},
        )

    delete_output = _remote_command_output(
        client.run_bash(
            f"tmsh delete sys software volume {_shell_quote(target_volume)}",
            timeout=120,
        )
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
) -> CheckResult:
    """
    Upload the ISO to /shared/images on a standby BIG-IP.

    SCP uses uppercase -O to force legacy SCP instead of SFTP.
    The uploaded file is verified using size and SHA-256.
    """
    result_id = "EXEC-UPLOAD-ISO-001"
    result_name = (
        "Upload ISO to /shared/images "
        "(standby only)"
    )

    role = (
        get_failover_role(client)
        or ""
    ).lower()

    if role != "standby":
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing upload because device "
                    "is not STANDBY."
                ),
                "role": role,
            },
        )

    if not iso_local_path:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": "ISO_LOCAL_PATH is empty."
            },
        )

    iso_local_path = os.path.expanduser(
        iso_local_path
    )

    if not os.path.isfile(iso_local_path):
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "ISO_LOCAL_PATH does not exist "
                    "or is not a file."
                ),
                "iso_local_path": iso_local_path,
            },
        )

    basename = os.path.basename(
        iso_local_path
    )

    remote_path = f"/shared/images/{basename}"
    destination = (
        f"{scp_user}@{host}:/shared/images/"
    )

    local_size = os.path.getsize(
        iso_local_path
    )

    local_sha256 = _sha256_file(
        iso_local_path
    )

    command = [
        "scp",
        "-O",
        iso_local_path,
        destination,
    ]

    print(
        "\nUploading ISO to BIG-IP /shared/images ..."
    )
    print(
        f"Source: {iso_local_path}"
    )
    print(
        f"Destination: {destination}"
    )
    print(
        "Using legacy SCP protocol with -O; remote timestamps are not preserved."
    )

    started = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            timeout=3600,
        )

    except subprocess.TimeoutExpired:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "SCP upload timed out."
                ),
                "command": " ".join(command),
            },
        )

    except Exception as exc:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": str(exc),
                "command": " ".join(command),
            },
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
                "elapsed_seconds": round(
                    elapsed,
                    3,
                ),
            },
        )

    verify_command = (
        f"test -f {_shell_quote(remote_path)} && "
        f"stat -c %s {_shell_quote(remote_path)} && "
        f"sha256sum {_shell_quote(remote_path)}"
    )

    try:
        verify_response = client.run_bash(
            verify_command,
            timeout=120,
        )

        verify_output = _remote_command_output(
            verify_response
        )

        if not verify_output:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "Remote ISO verification returned "
                        "no output."
                    ),
                    "remote_path": remote_path,
                },
            )

        remote_size = None
        remote_sha256 = None

        for line in verify_output.splitlines():
            line = line.strip()

            if line.isdigit():
                remote_size = int(line)
                continue

            hash_match = re.search(
                r"\b([0-9a-fA-F]{64})\b",
                line,
            )

            if hash_match:
                remote_sha256 = (
                    hash_match.group(1).lower()
                )

        if remote_size is None:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "Could not determine remote ISO size."
                    ),
                    "remote_path": remote_path,
                    "verification_output": verify_output,
                },
            )

        if remote_sha256 is None:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "Could not determine remote ISO "
                        "SHA-256."
                    ),
                    "remote_path": remote_path,
                    "verification_output": verify_output,
                },
            )

        if remote_size != local_size:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "Remote ISO size does not match "
                        "local ISO size."
                    ),
                    "remote_path": remote_path,
                    "local_size": local_size,
                    "remote_size": remote_size,
                },
            )

        if remote_sha256 != local_sha256:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "Remote ISO SHA-256 does not match "
                        "local ISO SHA-256."
                    ),
                    "remote_path": remote_path,
                    "local_sha256": local_sha256,
                    "remote_sha256": remote_sha256,
                },
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
                "elapsed_seconds": round(
                    elapsed,
                    3,
                ),
            },
        )

    except Exception as exc:
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Exception while verifying uploaded ISO."
                ),
                "remote_path": remote_path,
                "exception": str(exc),
            },
        )


def exec_install_standby(
    client: BigIPClient,
    image_iso_name: str,
    target_volume: str,
) -> CheckResult:
    """
    Submit image installation to the target standby volume.
    """
    result_id = (
        "EXEC-INSTALL-001"
    )

    result_name = (
        "Install image to standby volume "
        "(no reboot)"
    )

    role = (
        get_failover_role(client)
        or ""
    ).lower()

    if role != "standby":
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing install because device "
                    "is not STANDBY."
                ),
                "role": role,
            },
        )

    expected_version = (
        _extract_version_from_image_name(
            image_iso_name
        )
    )

    existing = _get_volume_state(
        client,
        target_volume,
    )

    if existing.found:
        status = existing.status.lower()
        version_matches = bool(
            expected_version
            and expected_version in existing.version
        )

        progress_states = [
            "installing",
            "complete",
            "testing",
            "copying",
            "pending",
            "waiting",
            "validating",
        ]

        if version_matches and any(
            state in status
            for state in progress_states
        ):
            print(
                f"\nTarget volume {target_volume} already "
                f"exists with version {existing.version} "
                f"and status '{existing.status}'."
            )

            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={
                    "role": role,
                    "image": image_iso_name,
                    "chosen_volume": target_volume,
                    "note": (
                        "Target volume already exists with "
                        "the expected version."
                    ),
                    "volume_state": existing.__dict__,
                },
            )

        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "role": role,
                "image": image_iso_name,
                "chosen_volume": target_volume,
                "error": (
                    "Target volume already exists but does "
                    "not contain the expected target version."
                ),
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
            details={
                "role": role,
                "image": image_iso_name,
                "chosen_volume": target_volume,
                "error": (
                    "Image name must be a filename located directly "
                    "under /shared/images."
                ),
            },
        )

    install_type = "hotfix" if _is_hotfix_image(image_name) else "image"

    # BIG-IP 17.5 accepts the image filename with the volume property. Run
    # from /shared/images so tmsh can resolve the file without treating an
    # absolute path as a slot ID.
    image_path = f"/shared/images/{image_name}"
    tmsh_command = (
        f"cd /shared/images && "
        f"tmsh install sys software {install_type} "
        f"{_shell_quote(image_name)} "
        f"volume {_shell_quote(target_volume)} "
        f"create-volume"
    )

    print(
        "\nSubmitting BIG-IP software install command:"
    )
    print(tmsh_command)
    print(
        "Install progress will be monitored "
        "in the next step."
    )

    try:
        response = client.run_bash(
            tmsh_command
        )

        output = _remote_command_output(
            response
        )

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
                    "error": (
                        "BIG-IP rejected the install "
                        "command."
                    ),
                    "command_result": output,
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
                    "Install command submitted. Completion "
                    "is verified by volume readiness polling."
                ),
                "command_result": output,
            },
        )

    except Exception as exc:
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
            },
        )


def exec_volume_ready(
    client: BigIPClient,
    volume: str,
    expect_version_contains: str,
    timeout_sec: int = 3600,
    interval_sec: int = 20,
) -> CheckResult:
    """
    Wait for target-volume installation to complete.
    """
    result_id = "EXEC-VOL-READY-001"
    result_name = "Target volume ready"

    deadline = time.time() + timeout_sec

    last: Dict[str, Any] = {
        "volume": volume,
        "version": "",
        "status": "",
        "source": "",
        "error": "",
    }

    print(
        f"\nWaiting for target volume {volume} "
        "to become ready."
    )

    print(
        f"Expected version contains: "
        f"{expect_version_contains}"
    )

    while time.time() < deadline:
        state = _get_volume_state(
            client,
            volume,
        )

        remaining = max(
            0,
            int(deadline - time.time()),
        )

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
            print(
                f"[{_now()}] Volume {volume} is not "
                f"visible yet. Remaining: {remaining}s"
            )

            time.sleep(interval_sec)
            continue

        status = state.status.lower()

        version_matches = bool(
            expect_version_contains
            and expect_version_contains in state.version
        )

        print(
            f"[{_now()}] Volume {volume}: "
            f"version='{state.version}', "
            f"status='{state.status}', "
            f"source='{state.source}', "
            f"remaining={remaining}s"
        )

        if "failed" in status or "error" in status:
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="FAIL",
                details={
                    "error": (
                        "BIG-IP reports a failed/error "
                        "state for the target volume."
                    ),
                    **last,
                    "expected_version": (
                        expect_version_contains
                    ),
                },
            )

        progress_states = [
            "installing",
            "testing",
            "copying",
            "pending",
            "waiting",
            "validating",
        ]

        if any(
            state_name in status
            for state_name in progress_states
        ):
            time.sleep(interval_sec)
            continue

        if "complete" in status:
            if not version_matches:
                return CheckResult(
                    id=result_id,
                    category="Execution",
                    name=result_name,
                    status="FAIL",
                    details={
                        "error": (
                            "Target volume is complete but "
                            "the version does not match."
                        ),
                        **last,
                        "expected_version": (
                            expect_version_contains
                        ),
                    },
                )

            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={
                    "note": (
                        "Target volume installation is complete "
                        "and the version matches."
                    ),
                    **last,
                    "expected_version": (
                        expect_version_contains
                    ),
                },
            )

        time.sleep(interval_sec)

    return CheckResult(
        id=result_id,
        category="Execution",
        name=result_name,
        status="FAIL",
        details={
            "error": (
                "Timeout waiting for target volume "
                "installation to complete."
            ),
            **last,
            "expected_version": (
                expect_version_contains
            ),
            "timeout_sec": timeout_sec,
        },
    )


def _now() -> str:
    return time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _is_expected_reboot_disconnect(
    error: Exception,
) -> bool:
    text = str(error).lower()

    return any(
        marker in text
        for marker in (
            "remotedisconnected",
            "remote end closed connection",
            "connection aborted",
            "connection reset",
            "badstatusline",
        )
    )


def exec_reboot_to_volume_standby(
    client: BigIPClient,
    volume: str,
) -> CheckResult:
    """
    Reboot the standby BIG-IP into the target volume.
    """
    result_id = "EXEC-REBOOT-TO-VOL-001"
    result_name = (
        "Reboot to target volume "
        "(standby only)"
    )

    role = (
        get_failover_role(client)
        or ""
    ).lower()

    if role != "standby":
        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "error": (
                    "Refusing reboot because device "
                    "is not STANDBY."
                ),
                "role": role,
                "volume": volume,
            },
        )

    tmsh_command = (
        f"tmsh reboot volume {volume}"
    )

    print(
        f"\nRebooting STANDBY device into "
        f"target volume {volume}."
    )
    print(tmsh_command)

    try:
        response = client.run_bash(
            tmsh_command
        )

        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="PASS",
            details={
                "role": role,
                "volume": volume,
                "tmsh": tmsh_command,
                "command_result": (
                    _remote_command_output(response)
                ),
            },
        )

    except Exception as exc:
        if _is_expected_reboot_disconnect(exc):
            return CheckResult(
                id=result_id,
                category="Execution",
                name=result_name,
                status="PASS",
                details={
                    "role": role,
                    "volume": volume,
                    "tmsh": tmsh_command,
                    "note": (
                        "Disconnect during reboot "
                        "is expected."
                    ),
                    "exception": str(exc),
                },
            )

        return CheckResult(
            id=result_id,
            category="Execution",
            name=result_name,
            status="FAIL",
            details={
                "role": role,
                "volume": volume,
                "tmsh": tmsh_command,
                "error": str(exc),
            },
        )


def exec_wait_postboot(
    client: BigIPClient,
    expect_version_contains: str,
    timeout_sec: int = 900,
    interval_sec: int = 15,
) -> CheckResult:
    """
    Wait until the device is reachable after reboot, is running the expected
    version, and is back in STANDBY.
    """
    result_id = "VAL-POSTBOOT-001"
    result_name = "Post-boot validation"

    deadline = time.time() + timeout_sec
    last_error = ""
    last_seen: Dict[str, Any] = {}

    print(
        "\nWaiting for BIG-IP to return after reboot."
    )

    while time.time() < deadline:
        remaining = max(
            0,
            int(deadline - time.time()),
        )

        try:
            version_payload = client.system_version()
            role = get_failover_role(client)

            last_seen = {
                "system_version": version_payload,
                "role": role,
            }

            version_text = str(
                version_payload
            )

            print(
                f"[{_now()}] Device reachable. "
                f"role='{role}', "
                f"remaining={remaining}s"
            )

            if (
                expect_version_contains
                and expect_version_contains not in version_text
            ):
                last_error = (
                    "Version does not match expectation yet."
                )

            elif (
                role or ""
            ).lower() != "standby":
                last_error = (
                    f"Device is not STANDBY yet. "
                    f"Current role: {role}"
                )

            else:
                return CheckResult(
                    id=result_id,
                    category="Validation",
                    name=result_name,
                    status="PASS",
                    details={
                        "note": (
                            "Device is reachable with the expected "
                            "version and standby role."
                        ),
                        **last_seen,
                    },
                )

        except requests.exceptions.RequestException as exc:
            last_error = str(exc)

            print(
                f"[{_now()}] Device is not reachable yet. "
                f"Remaining: {remaining}s"
            )

        except Exception as exc:
            last_error = str(exc)

            print(
                f"[{_now()}] Waiting after reboot. "
                f"Last error: {last_error}. "
                f"Remaining: {remaining}s"
            )

        time.sleep(interval_sec)

    return CheckResult(
        id=result_id,
        category="Validation",
        name=result_name,
        status="FAIL",
        details={
            "error": (
                "Timeout waiting for post-boot readiness."
            ),
            "last_error": last_error,
            "last_seen": last_seen,
            "timeout_sec": timeout_sec,
        },
    )