from __future__ import annotations

import shlex
import sys
from typing import Any, Optional
from urllib.parse import quote

from .bigip_client import BigIPClient


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


def manage_auto_sync(
    client: BigIPClient,
    *,
    enable: bool,
    prompt: str,
) -> bool:
    """
    Optionally enable or disable auto-sync for all configured device groups.

    Returns True when the requested transition was completed or was not
    needed. A declined prompt is treated as an intentional no-op.
    """
    states = _device_group_auto_sync_states(client)
    current_state = "disabled" if enable else "enabled"
    target_state = "enabled" if enable else "disabled"
    groups = [
        name
        for name, state in states.items()
        if state == current_state
    ]
    if not groups:
        print(
            f"[i] Auto-sync is already {'enabled' if enable else 'disabled'} "
            "for all device groups."
        )
        return True

    print("\n" + prompt)
    print("Affected device groups:")
    for group in groups:
        print(f"  - {group}")

    if not sys.stdin.isatty():
        print("[i] Non-interactive terminal; leaving auto-sync unchanged.")
        return True

    try:
        answer = input("Continue? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("[i] Auto-sync unchanged.")
        return True

    if answer not in ("y", "yes"):
        print("[i] Auto-sync unchanged; continuing.")
        return True

    state = "enabled" if enable else "disabled"
    for group in groups:
        client.patch(
            f"/mgmt/tm/cm/device-group/{quote(group, safe='')}",
            {"autoSync": state},
        )

    if not enable:
        for group in groups:
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
        for group in groups
        if final_states.get(group, "") != target_state
    }
    if mismatched:
        raise RuntimeError(
            f"Auto-sync verification failed. Expected {state} for "
            f"{groups}, mismatched states: {mismatched}"
        )

    print(f"[+] Auto-sync {state} and verified.")
    return True
