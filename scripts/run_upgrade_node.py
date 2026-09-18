from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from f5upgrade.bigip_client import BigIPClient
from f5upgrade.config import load_settings
from f5upgrade.diff_engine import DiffEngine
from f5upgrade.flow import FlowOptions, UpgradeFlow
from f5upgrade.ha import manage_auto_sync
from f5upgrade.report import to_report, write_json, write_markdown
from f5upgrade.state_collector import StateCollector

from run_backup_artifacts import SSHSession, _run_backup_task


BACKUP_TIMEOUT = 1800
COPY_TIMEOUT = 1800
SSH_TIMEOUT = 60
# Pause after each completed backup stage (UCS/SCF/QKView/ASMQKView) to let
# the device settle before starting the next backup task.
BACKUP_STAGE_WAIT_SECONDS = int(
    os.environ.get("BACKUP_STAGE_WAIT_SECONDS", "60")
)


def _safe_host(host: str) -> str:
    return (
        host.replace(":", "_")
        .replace("/", "_")
        .replace(".", "_")
    )


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _remote_command_output(response: Any) -> str:
    if not isinstance(response, dict):
        return ""

    return str(
        response.get("commandResult", "")
    ).strip()


def _get_remote_file_size(
    client: BigIPClient,
    remote_path: str,
) -> int:
    command = (
        f"test -f {_shell_quote(remote_path)} && "
        f"stat -c %s {_shell_quote(remote_path)}"
    )

    response = client.run_bash(
        command,
        timeout=SSH_TIMEOUT,
    )

    output = _remote_command_output(response)

    sizes = [
        int(line.strip())
        for line in output.splitlines()
        if line.strip().isdigit()
    ]

    if not sizes:
        return 0

    return sizes[-1]


def _stage_remote_file(
    client: BigIPClient,
    remote_path: str,
    staged_path: str,
) -> bool:
    """
    Stage a UCS/SCF file under /var/tmp before SCP download.
    """
    command = (
        f"test -f {_shell_quote(remote_path)} && "
        f"cp -f {_shell_quote(remote_path)} "
        f"{_shell_quote(staged_path)} && "
        f"chmod 644 {_shell_quote(staged_path)} && "
        f"stat -c %s {_shell_quote(staged_path)}"
    )

    response = client.run_bash(
        command,
        timeout=SSH_TIMEOUT,
    )

    output = _remote_command_output(response)

    sizes = [
        int(line.strip())
        for line in output.splitlines()
        if line.strip().isdigit()
    ]

    return bool(sizes and sizes[-1] > 0)


def _remove_remote_file(
    client: BigIPClient,
    remote_path: str,
) -> None:
    try:
        client.run_bash(
            f"rm -f {_shell_quote(remote_path)}",
            timeout=SSH_TIMEOUT,
        )
    except Exception as exc:
        print(
            f"[!] Could not remove temporary remote file "
            f"{remote_path}: {exc}"
        )


def download_backup_file(
    client: BigIPClient,
    session: SSHSession,
    remote_path: str,
    local_path: Path,
) -> bool:
    """
    Download a BIG-IP backup file using legacy SCP.

    UCS and SCF files are staged to /var/tmp before download.
    Files already under /var/tmp, such as QKView files, are copied directly.
    """
    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = Path(remote_path).name

    if not filename:
        print(f"[-] Invalid remote path: {remote_path}")
        return False

    remote_size = _get_remote_file_size(
        client,
        remote_path,
    )

    if remote_size <= 0:
        print(
            f"[-] Remote file does not exist or is empty: "
            f"{remote_path}"
        )
        return False

    staged_path: Optional[str] = None
    scp_source = remote_path

    if remote_path.startswith(
        (
            "/var/local/ucs/",
            "/var/local/scf/",
        )
    ):
        staged_path = (
            f"/var/tmp/f5upgrade-"
            f"{os.getpid()}-"
            f"{int(time.time())}-"
            f"{filename}"
        )

        print(
            f"Staging protected artifact to {staged_path}..."
        )

        if not _stage_remote_file(
            client,
            remote_path,
            staged_path,
        ):
            print(
                f"[-] Could not stage remote artifact: "
                f"{remote_path}"
            )
            return False

        scp_source = staged_path

    try:
        print(
            f"Copying {remote_path} to {local_path} "
            f"using legacy SCP..."
        )

        started = time.monotonic()

        result = session.scp_from(
            remote_file=scp_source,
            local_dir=local_path.parent,
            timeout_sec=COPY_TIMEOUT,
        )

        elapsed = time.monotonic() - started

        if result.returncode != 0:
            error_text = (
                (result.stderr or "").strip()
                or (result.stdout or "").strip()
                or "No SCP error returned."
            )

            print(
                f"[-] Copy failed in {elapsed:.1f}s: "
                f"{remote_path}"
            )
            print(f"    {error_text}")
            return False

        downloaded_path = (
            local_path.parent
            / Path(scp_source).name
        )

        if not downloaded_path.exists():
            print(
                f"[-] SCP reported success, but the local file "
                f"was not found: {downloaded_path}"
            )
            return False

        local_size = downloaded_path.stat().st_size

        if local_size != remote_size:
            print(
                f"[-] File size mismatch for {remote_path}: "
                f"remote={remote_size} bytes, "
                f"local={local_size} bytes"
            )
            return False

        # For UCS/SCF files, SCP writes the staged filename locally,
        # so move it to the requested final filename.
        #
        # For QKView files, scp_source and local_path have the same
        # basename, so downloaded_path == local_path. Do not unlink
        # or rename the file in that case.
        if downloaded_path != local_path:
            downloaded_path.replace(local_path)

        megabytes = local_size / (1024 * 1024)

        print(
            f"[+] Copy PASS in {elapsed:.1f}s: "
            f"{remote_path} "
            f"({megabytes:.2f} MB)"
        )

        return True

    except subprocess.TimeoutExpired:
        print(
            f"[-] Copy timed out after {COPY_TIMEOUT} seconds: "
            f"{remote_path}"
        )
        return False

    except Exception as exc:
        print(
            f"[-] Copy failed for {remote_path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False

    finally:
        if staged_path:
            print(
                f"Removing temporary staged file: "
                f"{staged_path}"
            )
            _remove_remote_file(
                client,
                staged_path,
            )


def create_and_download_backups(
    client: BigIPClient,
    settings: Any,
    backups_dir: Path,
    safe_host: str,
    session: SSHSession,
) -> bool:
    """
    Create and download UCS, SCF, QKView, and optional ASMQKView artifacts.

    The SSH session is opened and closed by the caller (main()) so that the
    same authenticated, multiplexed connection can be reused later for the
    LIC-001 SSH fallback without prompting for a second password.
    """
    print(
        "\n[*] Generating verified UCS, SCF, QKView, "
        "and ASMQKView backups on BIG-IP..."
    )

    try:
        hostname_result = session.run(
            "echo $HOSTNAME",
            timeout_sec=30,
        )

        hostname = (
            hostname_result.stdout or ""
        ).strip()

        if not hostname:
            hostname = safe_host

        short_hostname = (
            hostname.split(".")[0].strip()
            or safe_host
        )

        timestamp = datetime.now().strftime(
            "%Y%m%d-%H%M%S"
        )

        backup_name = (
            f"{_safe_host(short_hostname)}-{timestamp}"
        )

        backup_commands = [
            (
                "UCS Backup",
                (
                    f"tmsh save sys ucs {backup_name}; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"ls -1 "
                    f"/var/local/ucs/{backup_name}.ucs"
                ),
            ),
            (
                "SCF Backup",
                (
                    f"tmsh save sys config file "
                    f"{backup_name} no-passphrase; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/local/scf "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-name '{backup_name}*' "
                    f"-print"
                ),
            ),
            (
                "QKView",
                (
                    f"marker=/var/tmp/{backup_name}.qk.marker; "
                    f"touch \"$marker\"; "
                    f"qkview -s0; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/tmp "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-iname '*qkview*' "
                    f"-newer \"$marker\" "
                    f"-print; "
                    f"rm -f \"$marker\""
                ),
            ),
            (
                "ASMQKView",
                (
                    f"marker=/var/tmp/{backup_name}.asm.marker; "
                    f"touch \"$marker\"; "
                    f"asmqkview; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/tmp "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-iname '*asm*qkview*' "
                    f"-newer \"$marker\" "
                    f"-print; "
                    f"rm -f \"$marker\""
                ),
            ),
        ]

        remote_files: List[str] = []

        for index, (name, command) in enumerate(backup_commands):
            result = _run_backup_task(
                session=session,
                name=name,
                command=command,
                timeout_sec=BACKUP_TIMEOUT,
            )

            for path in result.get(
                "remote_files",
                [],
            ):
                if path not in remote_files:
                    remote_files.append(path)

            is_last_stage = index == len(backup_commands) - 1
            if not is_last_stage and BACKUP_STAGE_WAIT_SECONDS > 0:
                print(
                    f"[*] {name} complete. Waiting "
                    f"{BACKUP_STAGE_WAIT_SECONDS}s before starting "
                    f"the next backup stage..."
                )
                time.sleep(BACKUP_STAGE_WAIT_SECONDS)

        verified_files: List[str] = []

        for remote_file in remote_files:
            size = _get_remote_file_size(
                client,
                remote_file,
            )

            if size > 0:
                verified_files.append(remote_file)
            else:
                print(
                    f"[!] Ignoring nonexistent or empty backup "
                    f"path: {remote_file}"
                )

        required_types = {
            "ucs": False,
            "scf_base": False,
            "scf_tar": False,
            "qkview": False,
        }

        for remote_file in verified_files:
            basename = Path(remote_file).name

            if remote_file.startswith("/var/local/ucs/"):
                required_types["ucs"] = True

            elif remote_file.startswith("/var/local/scf/"):
                if basename.endswith(".tar"):
                    required_types["scf_tar"] = True
                else:
                    required_types["scf_base"] = True

            elif remote_file.startswith("/var/tmp/"):
                if basename.lower().endswith(".qkview"):
                    required_types["qkview"] = True

        missing = [
            name
            for name, present in required_types.items()
            if not present
        ]

        if missing:
            print(
                "\n[-] Required backup artifacts are missing:"
            )

            for item in missing:
                print(f"    - {item}")

            print(
                "[-] Upgrade stopped before Stage 2."
            )
            return False

        print("\n" + "=" * 75)
        print("BIG-IP Backups Created on Device:")

        for remote_file in verified_files:
            print(f"  - {remote_file}")

        print("-" * 75)
        print(
            "[*] Downloading backup artifacts before "
            "starting the upgrade flow..."
        )

        download_failures: List[str] = []

        for remote_file in verified_files:
            local_path = (
                backups_dir
                / Path(remote_file).name
            )

            success = download_backup_file(
                client=client,
                session=session,
                remote_path=remote_file,
                local_path=local_path,
            )

            if not success:
                download_failures.append(remote_file)

        if download_failures:
            print(
                "\n[-] Backup download failed for:"
            )

            for remote_file in download_failures:
                print(f"    - {remote_file}")

            print(
                "[-] Upgrade stopped because the local "
                "backup set is incomplete."
            )
            return False

        print(
            f"\n[+] Local backups saved to: "
            f"{backups_dir.resolve()}"
        )

        return True

    finally:
        # Session lifecycle (open/close) is owned by main(), so the same
        # authenticated connection can be reused for later SSH fallbacks.
        pass


def main() -> int:
    settings = load_settings()
    safe_host = _safe_host(settings.host)

    client = BigIPClient(
        host=settings.host,
        username=settings.username,
        password=settings.password,
        verify_tls=settings.verify_tls,
        timeout=settings.timeout,
    )

    collector = StateCollector(
        client,
        crq_number=settings.crq_number,
        phase="pre",
    )

    output_dir = Path("outputs") / settings.crq_number
    snapshots_dir = output_dir / "snapshots"
    backups_dir = output_dir / "backups"

    snapshots_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    backups_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("\n" + "=" * 80)
    print("  F5 BIG-IP AUTOMATED NODE UPGRADE PIPELINE")
    print(f"  Target Host:   {settings.host}")
    print(f"  Target Image:  {settings.target_image_contains}")
    print(f"  Target Volume: {settings.target_volume}")
    print("=" * 80)

    flow = UpgradeFlow(
        client=client,
        options=FlowOptions(
            allow_risk_accepted=True,
            fail_fast=True,
            require_standby=True,
        ),
        settings=settings,
    )

    print(
        "\n[PREFLIGHT] Validating standby role, configuration, discovery, "
        "storage, and target-volume readiness..."
    )
    preflight_results = flow.preflight()
    if flow.should_stop(preflight_results):
        report = to_report(
            preflight_results,
            meta={
                "bigip_host": settings.host,
                "mode": "preflight",
            },
        )
        flow_json = output_dir / f"upgrade_flow_report_{safe_host}.json"
        flow_md = output_dir / f"upgrade_flow_report_{safe_host}.md"
        write_json(str(flow_json), report)
        write_markdown(str(flow_md), report)
        print(
            "\n[-] Upgrade preflight did not pass. "
            f"Overall status: {report['overall_status']}"
        )
        print(f"[-] See report details at: {flow_md}")
        print(
            "[-] Halting workflow before pre-upgrade snapshots, "
            "auto-sync changes, or backups.\n"
        )
        return 2

    # -------------------------------------------------------------------------
    # STAGE 1: PRE-UPGRADE STATE COLLECTION AND BACKUPS
    # -------------------------------------------------------------------------
    print(
        "\n[STAGE 1/4] Collecting Pre-Upgrade State "
        "& Generating Backups..."
    )

    pre_json = (
        snapshots_dir
        / f"pre_state_{safe_host}.json"
    )

    pre_xlsx = (
        snapshots_dir
        / f"pre_state_{safe_host}.xlsx"
    )

    pre_state = collector.collect_all_state()
    collector.save_operational_evidence(pre_state, output_dir)

    collector.save_snapshot_json(
        pre_state,
        pre_json,
    )

    collector.save_snapshot_excel(
        pre_state,
        pre_xlsx,
    )

    virtual_server_count = len(
        pre_state.get("virtual_servers", [])
    )

    pool_count = len(
        pre_state.get("pools", [])
    )

    node_count = len(
        pre_state.get("nodes", [])
    )

    print(
        f"[+] Saved Pre-State Snapshot: {pre_json}"
    )

    print(
        f"[i] Baseline Objects: "
        f"{virtual_server_count} Virtual Servers, "
        f"{pool_count} Pools, "
        f"{node_count} Nodes."
    )

    auto_sync_groups = manage_auto_sync(
        client,
        enable=False,
        prompt=(
            "Auto-sync is enabled. Disable auto-sync before creating "
            "the UCS/SCF backups and sync the current configuration?"
        ),
    )

    print("\n[*] Saving all partition configuration before backups...")
    try:
        save_output = collector.save_configuration_partitions()
        print(f"[+] Configuration saved before backups: {save_output or 'PASS'}")
    except RuntimeError as exc:
        print(f"[-] {exc}")
        return 2

    scp_user = getattr(
        settings,
        "scp_user",
        None,
    ) or settings.username

    # Opened here (once) and reused for both the backup artifacts and, via
    # ssh_control_path below, for the LIC-001 SSH fallback in Stage 2 — this
    # avoids a second, unexpected interactive password prompt mid-flow.
    # The password already collected for the REST client (settings.password)
    # is reused for this SSH connection too (only if scp_user matches the
    # REST username), so the operator is prompted for a password only once
    # for the whole run instead of once per connection type.
    ssh_session = SSHSession(
        user=scp_user,
        host=settings.host,
        password=(
            settings.password if scp_user == settings.username else None
        ),
    )
    ssh_session.open()
    flow.ssh_control_path = ssh_session.control_path

    backups_ok = create_and_download_backups(
        client=client,
        settings=settings,
        backups_dir=backups_dir,
        safe_host=safe_host,
        session=ssh_session,
    )

    if not backups_ok:
        ssh_session.close()
        return 2

    # -------------------------------------------------------------------------
    # STAGE 2: UPGRADE FLOW EXECUTION
    # -------------------------------------------------------------------------
    print(
        "\n[STAGE 2/4] Executing Upgrade Flow "
        "(Upload, Install, and Reboot)..."
    )

    StateCollector(client).display_ltm_health_summary()

    try:
        results = flow.run()
    finally:
        # The LIC-001 SSH fallback (if it ran) was the last consumer of the
        # shared backup SSH session; close it now regardless of outcome.
        ssh_session.close()

    report = to_report(
        results,
        meta={
            "bigip_host": settings.host,
            "mode": "flow",
        },
    )

    flow_json = (
        output_dir
        / f"upgrade_flow_report_{safe_host}.json"
    )

    flow_md = (
        output_dir
        / f"upgrade_flow_report_{safe_host}.md"
    )

    write_json(
        str(flow_json),
        report,
    )

    write_markdown(
        str(flow_md),
        report,
    )

    if report["overall_status"] != "PASS":
        print(
            "\n[-] Upgrade Flow did not complete. "
            f"Overall status: {report['overall_status']}"
        )

        print(
            f"[-] See report details at: {flow_md}"
        )

        print(
            "[-] Halting workflow. Post-upgrade snapshot "
            "and diff will NOT be run.\n"
        )

        return 2

    skipped = any(
        result.get("id", "").startswith("FLOW-SKIP")
        for result in report.get("results", [])
    )

    if skipped:
        print(
            "\n[i] Upgrade steps were skipped or not needed. "
            "Halting workflow cleanly."
        )
        return 0

    print(
        "\n[+] SOFTWARE UPGRADE COMPLETED SUCCESSFULLY"
        "\n    Installation, volume readiness, standby reboot, and post-boot "
        "validation completed."
    )

    # -------------------------------------------------------------------------
    # STAGE 3: POST-UPGRADE STATE COLLECTION
    # -------------------------------------------------------------------------
    print(
        "\n[STAGE 3/4] Collecting Post-Upgrade State..."
    )

    stabilization_seconds = 180
    print(
        "\nWaiting "
        f"{stabilization_seconds} seconds for VIPs, pools, and nodes "
        "to stabilize before post-upgrade checks..."
    )
    for elapsed in range(1, stabilization_seconds + 1):
        print(
            f"\rStabilization timer: {elapsed}/{stabilization_seconds} seconds",
            end="",
            flush=True,
        )
        time.sleep(1)
    print()
    print("\n[+] Stabilization wait completed.")
    StateCollector(client).display_ltm_health_summary()

    manage_auto_sync(
        client,
        enable=True,
        groups=auto_sync_groups,
        prompt=(
            "Auto-sync was disabled by this upgrade workflow. Re-enable "
            "it for those same device groups before collecting the "
            "post-upgrade state?"
        ),
    )

    post_json = (
        snapshots_dir
        / f"post_state_{safe_host}.json"
    )

    post_xlsx = (
        snapshots_dir
        / f"post_state_{safe_host}.xlsx"
    )

    collector.phase = "post"
    post_state = collector.collect_all_state()
    collector.save_operational_evidence(post_state, output_dir)

    collector.save_snapshot_json(
        post_state,
        post_json,
    )

    collector.save_snapshot_excel(
        post_state,
        post_xlsx,
    )

    print(
        f"[+] Saved Post-State Snapshot: {post_json}"
    )

    # -------------------------------------------------------------------------
    # STAGE 4: REGRESSION DIFFERENCE ENGINE
    # -------------------------------------------------------------------------
    print(
        "\n[STAGE 4/4] Comparing Pre vs Post "
        "Upgrade States..."
    )

    engine = DiffEngine(
        pre_state,
        post_state,
    )

    diff_results = engine.compare()

    diff_json = (
        output_dir
        / f"diff_report_{safe_host}.json"
    )

    diff_md = (
        output_dir
        / f"diff_report_{safe_host}.md"
    )

    with open(
        diff_json,
        "w",
        encoding="utf-8",
    ) as file_handle:
        json.dump(
            diff_results,
            file_handle,
            indent=2,
            ensure_ascii=False,
        )

    markdown_report = engine.generate_markdown_report(
        diff_results,
        settings.host,
    )

    with open(
        diff_md,
        "w",
        encoding="utf-8",
    ) as file_handle:
        file_handle.write(markdown_report)

    print(
        f"[+] Wrote Diff Report: {diff_md}"
    )

    summary = diff_results.get(
        "summary",
        {},
    )

    warnings = summary.get(
        "warnings",
        0,
    )

    counts = diff_results.get("counts", {})
    sync_review_required = any(
        item.get("severity") == "CRITICAL"
        for item in diff_results.get("sync_status", [])
    )
    blocking_critical_regressions = [
        item
        for category in (
            "virtual_servers",
            "pools",
            "nodes",
            "tmm_routes",
            "mgmt_routes",
            "interfaces",
        )
        for item in diff_results.get(category, [])
        if item.get("severity") == "CRITICAL"
    ]

    print(
        "\n======================================================="
    )

    if blocking_critical_regressions:
        print(
            "  UPGRADE VALIDATION RESULT: "
            f"FAIL ({len(blocking_critical_regressions)} "
            "Critical Regressions Detected)"
        )
    else:
        print(
            "  UPGRADE VALIDATION RESULT: "
            "PASS (Post-validation closed)"
        )

    print(
        f"  VIPs: {counts.get('post_vs_count', 0)} "
        f"| Pools: {counts.get('post_pool_count', 0)} "
        f"| Nodes: {counts.get('post_node_count', 0)}"
    )

    if sync_review_required:
        print(
            "\n  CONFIGSYNC REVIEW REQUIRED"
            "\n  ConfigSync is reporting Changes Pending (red)."
            "\n  Review the active/standby configuration and sync "
            "the changes using the authoritative device."
        )

    print(
        f"  Warnings/Changes: {warnings}"
    )

    print(
        "=======================================================\n"
    )

    if blocking_critical_regressions:
        print(
            f"Node upgrade completed, but post-validation found "
            f"blocking regressions for {settings.host}."
        )
    else:
        print(
            f"Upgrade successful and post-validation closed for "
            f"{settings.host}."
        )

    return (
        0
        if not blocking_critical_regressions
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())