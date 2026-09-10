from __future__ import annotations

import os
from pathlib import Path

from f5upgrade.bigip_client import BigIPClient
from f5upgrade.config import load_settings
from f5upgrade.discovery import discover_devices
from f5upgrade.ha import get_failover_role
from f5upgrade.ha_recovery import (
    exec_config_sync_to_group,
    exec_failover_test_standby,
    val_sync_status,
    val_traffic_group_1,
    val_trust_domain,
)
from f5upgrade.report import to_report, write_json, write_markdown


def _client_for(host: str, settings) -> BigIPClient:
    return BigIPClient(
        host=host,
        username=settings.username,
        password=settings.password,
        verify_tls=settings.verify_tls,
        timeout=settings.timeout,
    )


def main() -> int:
    settings = load_settings()

    # Env toggles
    force_sync = os.getenv("FORCE_CONFIGSYNC", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    dg = os.getenv("DEVICE_GROUP", "Failover").strip() or "Failover"
    do_failover_test = os.getenv("ENABLE_FAILOVER_TEST", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    # Base client (user-selected host)
    client = _client_for(settings.host, settings)
    results = []

    # Validations on the selected host
    results.append(val_trust_domain(client))
    results.append(val_sync_status(client))
    results.append(val_traffic_group_1(client))

    # Optional: force config-sync (runs on selected host)
    if force_sync:
        results.append(exec_config_sync_to_group(client, dg))
        results.append(val_sync_status(client))

    # Optional: failover test (must run on ACTIVE, so auto-pick active)
    if do_failover_test:
        role = (get_failover_role(client) or "").lower()
        if role != "active":
            # Discover peer(s) and try the other device
            local_name, devices = discover_devices(client)
            _ = local_name  # reserved for future use

            # Discovery may not contain mgmt IPs reliably,
            # so we fall back to the mgmt list env var if provided.
            peer_host = os.getenv("PEER_BIGIP_HOST", "").strip()

            if not peer_host:
                # Best-effort: if user has both management IPs, require PEER_BIGIP_HOST
                # This will refuse with role details on the non-ACTIVE.
                results.append(exec_failover_test_standby(client))
            else:
                peer_client = _client_for(peer_host, settings)
                # Run failover on peer (expected to be active)
                results.append(exec_failover_test_standby(peer_client))
        else:
            results.append(exec_failover_test_standby(client))

        # After failover attempt, validate TG ownership again on selected host
        results.append(val_traffic_group_1(client))

    report = to_report(
        results, meta={"bigip_host": settings.host, "mode": "ha_recovery"}
    )

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    safe_host = settings.host.replace(":", "_").replace("/", "_")
    json_path = out_dir / f"ha_recovery_report_{safe_host}.json"
    md_path = out_dir / f"ha_recovery_report_{safe_host}.md"

    write_json(str(json_path), report)
    write_markdown(str(md_path), report)

    print(f"Wrote report: {json_path}")
    print(f"Wrote markdown: {md_path}")
    print(f"Overall status: {report['overall_status']}")
    print(f"Summary: {report['summary']}")

    return 0 if report["overall_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
