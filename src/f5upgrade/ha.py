from __future__ import annotations

import shlex
import sys
from typing import Any, Optional

from .bigip_client import BigIPClient
from .discovery import discover_devices


AUTO_SYNC_EXCLUDED_GROUPS = frozenset({"device_trust_group"})


def get_failover_role(client: BigIPClient) -> Optional[str]:
    """
    Best-effort extraction of local failover role from /mgmt/tm/cm/failover-status.

    Returns:
      "active" | "standby" | None

    Notes:
      - BIG-IP payload shape varies by version/build.
      - We try multiple common patterns safely by scanning all string fields.
    """
    payload = client.failover_state()

    def _search(obj: Any) -> Optional[str]:
        if obj is None:
            return None

        if isinstance(obj, str):
            s = obj.lower()
            if "active" in s:
                return "active"
            if "standby" in s:
                return "standby"
            return None

        if isinstance(obj, dict):
            for v in obj.values():
                r = _search(v)
                if r:
                    return r
            return None

        if isinstance(obj, list):
            for v in obj:
                r = _search(v)
                if r:
                    return r
            return None

        return None

    return _search(payload)


def _device_group_auto_sync_states(
    client: BigIPClient,
) -> dict[str, str]:
    payload = client.get("/mgmt/tm/cm/device-group")
    groups = payload.get("items", []) if isinstance(payload, dict) else []
    result: dict[str, str] = {}

    for group in groups:
        if not isinstance(group, dict) or not group.get("name"):
            continue
        name = str(group["name"])
        if name in AUTO_SYNC_EXCLUDED_GROUPS:
            continue
        result[name] = str(
            group.get("autoSync", "")
        ).strip().lower()

    return result


def _local_device_group(
    client: BigIPClient,
    states: dict[str, str],
) -> Optional[str]:
    """
    Select the single device-group associated with this local device.

    Device-group names in this environment begin with the local device
    hostname (for example, ``bip2...`` or ``bip0...``). Never fall back to
    all groups: a discovery failure must not broaden the scope of an
    auto-sync change.
    """
    local_name, devices = discover_devices(client)
    candidates = [local_name or ""]

    for device in devices:
        if device.get("name") == local_name:
            candidates.extend(
                [
                    str(device.get("hostname") or ""),
                    str(device.get("name") or ""),
                ]
            )

    prefixes = {
        value.strip().lower().split(".", 1)[0]
        for value in candidates
        if value and value.strip()
    }
    matches = [
        name
        for name in states
        if any(name.lower().startswith(prefix) for prefix in prefixes)
    ]

    if len(matches) > 1:
        raise RuntimeError(
            "More than one local device-group matched the local device "
            f"{sorted(matches)}. Refusing to change auto-sync."
        )
    if not matches:
        print(
            "[i] No device-group matching the local device hostname was "
            "found; leaving auto-sync unchanged."
        )
        return None
    return matches[0]


def manage_auto_sync(
    client: BigIPClient,
    *,
    enable: bool,
    prompt: str,
    groups: Optional[list[str]] = None,
) -> list[str]:
    """
    Optionally change auto-sync for the selected device groups.

    When ``groups`` is omitted, only the group whose name starts with the
    local device hostname is selected. The returned names are the groups
    changed by this invocation, allowing a later phase to restore only those
    groups.
    A declined prompt is treated as an intentional no-op.
    """
    states = _device_group_auto_sync_states(client)
    current_state = "disabled" if enable else "enabled"
    target_state = "enabled" if enable else "disabled"
    requested_groups = groups
    if requested_groups is None:
        local_group = _local_device_group(client, states)
        requested_groups = [local_group] if local_group else []
    groups_to_change = [
        name
        for name in requested_groups
        if states.get(name) == current_state
    ]
    if not groups_to_change:
        print(
            f"[i] Auto-sync is already {'enabled' if enable else 'disabled'} "
            "for the applicable device groups."
        )
        return []

    print("\n" + prompt)
    print("Affected device groups:")
    for group in groups_to_change:
        print(f"  - {group}")

    if not sys.stdin.isatty():
        print("[i] Non-interactive terminal; leaving auto-sync unchanged.")
        return []

    try:
        answer = input("Continue? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("[i] Auto-sync unchanged.")
        return []

    if answer not in ("y", "yes"):
        print("[i] Auto-sync unchanged; continuing.")
        return []

    state = "enabled" if enable else "disabled"
    for group in groups_to_change:
        response = client.run_bash(
            "tmsh modify cm device-group "
            f"{shlex.quote(group)} auto-sync {state}"
        )
        output = str(response.get("commandResult", "")).strip()
        if output and any(
            marker in output.lower()
            for marker in ("error", "failed", "syntax", "invalid", "conflict")
        ):
            raise RuntimeError(
                f"Could not set auto-sync {state} for {group}: {output}"
            )

    if not enable:
        for group in groups_to_change:
            command = (
                "tmsh run cm config-sync to-group "
                f"{shlex.quote(group)}"
            )
            response = client.run_bash(command)
            output = str(response.get("commandResult", "")).strip()
            if output and any(
                marker in output.lower()
                for marker in ("error", "failed", "syntax", "invalid", "conflict")
            ):
                raise RuntimeError(
                    f"Could not sync configuration to {group}: {output}"
                )

    final_states = _device_group_auto_sync_states(client)
    mismatched = {
        group: final_states.get(group, "")
        for group in groups_to_change
        if final_states.get(group, "") != target_state
    }
    if mismatched:
        raise RuntimeError(
            f"Auto-sync verification failed. Expected {state} for "
            f"{groups_to_change}, mismatched states: {mismatched}"
        )

    print(f"[+] Auto-sync {state} and verified.")
    return groups_to_change
