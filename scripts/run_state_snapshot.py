from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from f5upgrade.bigip_client import BigIPClient
from f5upgrade.config import load_settings
from f5upgrade.ha import manage_auto_sync
from f5upgrade.state_collector import StateCollector


def _safe_host(host: str) -> str:
    return host.replace(":", "_").replace("/", "_").replace(".", "_")


def _count_rows(state: Dict[str, Any], key: str) -> int:
    value = state.get(key, [])
    return len(value) if isinstance(value, list) else 0


def main() -> int:
    cfg = load_settings()

    phase = os.getenv("SNAPSHOT_PHASE", "pre").strip().lower() or "pre"
    if phase not in ("pre", "post"):
        raise ValueError("SNAPSHOT_PHASE must be either 'pre' or 'post'")

    client = BigIPClient(
        host=cfg.host,
        username=cfg.username,
        password=cfg.password,
        verify_tls=cfg.verify_tls,
        timeout=cfg.timeout,
    )

    collector = StateCollector(
        client,
        crq_number=cfg.crq_number,
        phase=phase,
    )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_host = _safe_host(cfg.host)

    out_dir = Path("outputs") / cfg.crq_number / "state"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{phase}_state_{safe_host}_{timestamp}.json"
    xlsx_path = out_dir / f"{phase}_state_{safe_host}_{timestamp}.xlsx"

    print("=" * 80)
    print("BIG-IP State Snapshot Collection")
    print("=" * 80)
    print(f"Host: {cfg.host}")
    print(f"Phase: {phase}")
    print(f"JSON output: {json_path}")
    print(f"Excel output: {xlsx_path}")
    print("=" * 80)

    if phase == "post":
        manage_auto_sync(
            client,
            enable=True,
            prompt=(
                "Auto-sync is disabled. Re-enable auto-sync before "
                "collecting the post-upgrade state?"
            ),
        )

    collector.display_ltm_health_summary()
    state = collector.collect_all_state()
    collector.save_operational_evidence(state, out_dir.parent)

    collector.save_snapshot_json(state, json_path)
    excel_written = collector.save_snapshot_excel(state, xlsx_path)

    print("\nSnapshot collection completed.")
    print(f"JSON written: {json_path}")

    if excel_written:
        print(f"Excel written: {xlsx_path}")
    else:
        print("Excel not written: pandas/openpyxl is not installed in this virtual environment.")

    print("\nCollected object counts:")
    print(f" - Virtual servers: {_count_rows(state, 'virtual_servers')}")
    print(f" - Pools: {_count_rows(state, 'pools')}")
    print(f" - Nodes: {_count_rows(state, 'nodes')}")
    print(f" - Device groups: {_count_rows(state, 'device_groups')}")
    print(f" - Management routes: {_count_rows(state, 'mgmt_routes')}")
    print(f" - TMM routes: {_count_rows(state, 'tmm_routes')}")
    print(f" - Route domains: {_count_rows(state, 'route_domains')}")
    print(f" - Interfaces: {_count_rows(state, 'interface_stats')}")
    print(f" - System performance rows: {_count_rows(state, 'system_performance')}")

    print("\nImportant:")
    print(" - Keep this snapshot for pre/post comparison.")
    print(" - Do not commit outputs/state files to GitHub without sanitizing them.")
    print(" - These files may contain IPs, hostnames, routes, and operational details.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
