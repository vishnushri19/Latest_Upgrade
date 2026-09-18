from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from f5upgrade.bigip_client import BigIPClient
from f5upgrade.config import load_settings
from f5upgrade.diff_engine import DiffEngine
from f5upgrade.ha import manage_auto_sync
from f5upgrade.state_collector import StateCollector


def _safe_host(host: str) -> str:
    return host.replace(":", "_").replace("/", "_").replace(".", "_")


def run_collection(
    collector: StateCollector,
    host: str,
    phase: str,
    output_root: Path,
    do_backup: bool = False,
) -> Path:
    safe_h = _safe_host(host)
    out_dir = output_root / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{phase}_state_{safe_h}.json"
    xlsx_path = out_dir / f"{phase}_state_{safe_h}.xlsx"

    print(f"\n[*] Collecting {phase.upper()} upgrade state from {host} (all 13 REST tables)...")
    collector.display_ltm_health_summary()
    state = collector.collect_all_state()
    collector.save_operational_evidence(state, output_root)

    collector.save_snapshot_json(state, json_path)
    print(f"[+] Saved {phase.upper()} state JSON: {json_path}")

    excel_ok = collector.save_snapshot_excel(state, xlsx_path)
    if excel_ok:
        print(f"[+] Saved {phase.upper()} state Excel: {xlsx_path}")
    else:
        print(f"[*] Note: pandas not installed; skipped Excel export (JSON saved).")

    # Inventory summary on CLI
    vs_count = len(state.get("virtual_servers", []))
    pool_count = len(state.get("pools", []))
    node_count = len(state.get("nodes", []))
    print(f"[i] Summary: {vs_count} Virtual Servers, {pool_count} Pools, {node_count} Nodes.")

    if do_backup and phase == "pre":
        print("\n[*] Saving all partition configuration before backups...")
        collector.save_configuration_partitions()
        print("[+] Configuration saved before backups.")
        print("\n[*] Creating verified UCS & SCF backups on BIG-IP...")
        print("    Naming format: $(echo $HOSTNAME | cut -d'.' -f1)-$(date +%H%M-%m%d%y)")
        backups = collector.create_backups()
        ucs_out = backups.get("ucs", {}).get("output", "")
        scf_out = backups.get("scf", {}).get("output", "")
        print("[+] Backup execution results:")
        if ucs_out:
            print(f"--- UCS Output ---\n{ucs_out.strip()}\n")
        if scf_out:
            print(f"--- SCF Output ---\n{scf_out.strip()}\n")

    return json_path


def run_diff(
    pre_json_path: Path,
    post_json_path: Path,
    host: str,
    output_root: Path,
) -> int:
    print(f"\n[*] Comparing Pre vs Post Upgrade States...")
    print(f"    Pre file:  {pre_json_path}")
    print(f"    Post file: {post_json_path}")

    with open(pre_json_path, "r", encoding="utf-8") as f:
        pre_state = json.load(f)

    with open(post_json_path, "r", encoding="utf-8") as f:
        post_state = json.load(f)

    engine = DiffEngine(pre_state, post_state)
    diff_results = engine.compare()

    # Save Diff JSON & Markdown
    out_dir = output_root
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_h = _safe_host(host)

    diff_json_path = out_dir / f"diff_report_{safe_h}.json"
    diff_md_path = out_dir / f"diff_report_{safe_h}.md"

    with open(diff_json_path, "w", encoding="utf-8") as f:
        json.dump(diff_results, f, indent=2, ensure_ascii=False)

    md_content = engine.generate_markdown_report(diff_results, host)
    with open(diff_md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\n[+] Wrote Diff Report (Markdown): {diff_md_path}")
    print(f"[+] Wrote Diff Report (JSON):     {diff_json_path}")

    print("\n" + engine.generate_cli_summary_table(diff_results) + "\n")

    # Print summary to console
    summary = diff_results.get("summary", {})
    crit = summary.get("critical_regressions", 0)
    warn = summary.get("warnings", 0)

    print("\n=======================================================")
    if diff_results["overall_status"] == "PASS":
        print("  🟢 UPGRADE VALIDATION RESULT: PASS (No Critical Regressions)")
    else:
        print(f"  🔴 UPGRADE VALIDATION RESULT: FAIL ({crit} Critical Regressions Detected)")
    print(f"  Critical Regressions: {crit} | Warnings/Changes: {warn}")
    print("=======================================================\n")

    if crit > 0:
        all_diffs = (
            diff_results.get("virtual_servers", [])
            + diff_results.get("pools", [])
            + diff_results.get("nodes", [])
            + diff_results.get("tmm_routes", [])
            + diff_results.get("interfaces", [])
        )
        for d in all_diffs:
            if d.get("severity") == "CRITICAL":
                print(f"  - 🔴 [{d.get('category', '').upper()}] {d.get('name')}: {d.get('details')}")
        print("")

    return 0 if diff_results["overall_status"] == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="F5 BIG-IP Pre/Post Automated State Collector and Difference Engine"
    )
    parser.add_argument(
        "--phase",
        choices=["pre", "post"],
        help="Capture state for specified upgrade phase (pre or post)",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Take timestamped UCS and SCF backups during pre-check",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare pre vs post states and generate diff report",
    )
    parser.add_argument(
        "--pre-file",
        type=Path,
        help="Explicit path to pre_state.json",
    )
    parser.add_argument(
        "--post-file",
        type=Path,
        help="Explicit path to post_state.json",
    )

    args = parser.parse_args()

    cfg = load_settings()
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
        phase=args.phase or "snapshot",
    )

    safe_h = _safe_host(cfg.host)
    output_root = Path("outputs") / cfg.crq_number
    snapshots_dir = output_root / "snapshots"

    if args.phase:
        if args.phase == "post":
            manage_auto_sync(
                client,
                enable=True,
                prompt=(
                    "Auto-sync is disabled. Re-enable auto-sync before "
                    "collecting the post-upgrade state?"
                ),
            )
        run_collection(
            collector,
            host=cfg.host,
            phase=args.phase,
            output_root=output_root,
            do_backup=args.backup or (args.phase == "pre"),
        )

    if args.compare:
        pre_file = args.pre_file or (snapshots_dir / f"pre_state_{safe_h}.json")
        post_file = args.post_file or (snapshots_dir / f"post_state_{safe_h}.json")

        if not pre_file.exists():
            print(f"[-] Error: Pre-state file not found at {pre_file}. Run with --phase pre first.")
            return 1
        if not post_file.exists():
            print(f"[-] Error: Post-state file not found at {post_file}. Run with --phase post first.")
            return 1

        return run_diff(pre_file, post_file, cfg.host, output_root)

    if not args.phase and not args.compare:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
