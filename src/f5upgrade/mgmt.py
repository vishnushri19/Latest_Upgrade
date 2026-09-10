from __future__ import annotations

from typing import Dict, List

from .bigip_client import BigIPClient


def resolve_management_addresses(client: BigIPClient) -> List[Dict[str, str]]:
    """
    Resolve management addresses for all devices in the HA group.

    Returns a list of dicts:
      {
        "name": "<device-name>",
        "management_ip": "<mgmt-ip-or-address>",
      }

    Notes:
      - Uses /mgmt/tm/cm/device
      - Best-effort extraction across BIG-IP versions
      - Read-only, no assumptions about routing reachability
    """
    payload = client.devices()
    items = payload.get("items", []) if isinstance(payload, dict) else []

    resolved: List[Dict[str, str]] = []

    for d in items:
        mgmt = (
            d.get("managementIp")
            or d.get("managementAddress")
            or d.get("address")
        )
        if isinstance(mgmt, str):
            resolved.append(
                {
                    "name": d.get("name", "unknown"),
                    "management_ip": mgmt,
                }
            )

    return resolved
