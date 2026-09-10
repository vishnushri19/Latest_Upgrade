from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .bigip_client import BigIPClient


def discover_devices(client: BigIPClient) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Discover devices in the BIG-IP trust domain.

    Returns:
      - local_device_name (best-effort)
      - devices list (raw device objects from /mgmt/tm/cm/device)

    Notes:
      - Different BIG-IP versions expose slightly different fields.
      - V1 uses safe heuristics without assuming a fixed schema.
    """
    payload = client.devices()
    devices = payload.get("items", []) if isinstance(payload, dict) else []

    local_name: Optional[str] = None

    # Heuristic 1: explicit self flag (seen on some builds)
    for d in devices:
        if d.get("selfDevice") is True:
            local_name = d.get("name")
            break

    # Heuristic 2: match management IP/address against the host we connected to
    if local_name is None:
        connected_host = client.base_url.replace("https://", "").strip()
        for d in devices:
            mgmt = d.get("managementIp") or d.get("managementAddress")
            if isinstance(mgmt, str) and connected_host in mgmt:
                local_name = d.get("name")
                break

    return local_name, devices


def summarize_devices(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Produce a compact, safe summary of devices for reporting.
    """
    summary: List[Dict[str, Any]] = []
    for d in devices:
        summary.append(
            {
                "name": d.get("name"),
                "hostname": d.get("hostname"),
                "managementIp": d.get("managementIp") or d.get("managementAddress"),
                "failoverState": d.get("failoverState"),
                "version": d.get("version"),
            }
        )
    return summary
