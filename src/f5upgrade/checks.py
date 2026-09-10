from __future__ import annotations

import os
import re
import sys
from typing import List

from .bigip_client import BigIPClient
from .report import CheckResult


def check_config_load(client: BigIPClient, interactive: bool = True) -> CheckResult:
    """
    CFG-001:
    Execute 'tmsh load sys config verify partitions all' to validate configuration
    syntax across all administrative partitions.

    Rules:
      - Any fatal ERROR (syntax error, 0107*:3: error, cannot load) -> FAIL immediately.
      - WARNINGS only (deprecations, iRule syntax warnings, 0107*:4: warning) ->
        Display warnings and prompt operator manually for permission to proceed.
        (Can also be accepted automatically if ALLOW_CONFIG_WARNINGS=1).
      - Clean -> PASS.
    """
    cmd = "tmsh load sys config verify partitions all"

    try:
        # Allow up to 300 seconds for large multi-partition config validation
        resp = client.run_bash(cmd, timeout=300)
        out = str(resp.get("commandResult", "")).strip()

        has_error = False
        error_lines = []
        warning_lines = []

        lines = [line.strip() for line in out.splitlines() if line.strip()]

        in_warning_section = False

        for line in lines:
            line_l = line.lower()

            if "there were warnings:" in line_l:
                in_warning_section = True
                continue

            # Fatal error indicators (TMOS severity :3:, syntax errors, or load failures)
            if (
                re.search(r":\d{8}:3:", line)
                or "syntax error:" in line_l
                or "syntax error " in line_l
                or "error:" in line_l
                or "failed to load" in line_l
                or "cannot load" in line_l
                or line_l.startswith("error ")
            ):
                has_error = True
                error_lines.append(line)

            # Warning indicators (TMOS severity :4: or :5:, deprecation, syntax warnings)
            elif (
                in_warning_section
                or re.search(r":\d{8}:[45]:", line)
                or "warning:" in line_l
                or "deprecated" in line_l
                or line_l.startswith("warning ")
            ):
                warning_lines.append(line)

        # Fatal error encountered -> hard FAIL
        if has_error:
            return CheckResult(
                id="CFG-001",
                category="Configuration and Policy Integrity",
                name="Config load verification (all partitions)",
                status="FAIL",
                details={
                    "error": "Configuration load validation failed with fatal ERRORS.",
                    "command": cmd,
                    "error_lines": error_lines,
                    "full_output": out,
                },
            )

        # Warnings encountered -> prompt operator or allow via environment variable
        if warning_lines:
            env_allow = os.getenv("ALLOW_CONFIG_WARNINGS", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            user_decision = "ACCEPTED" if env_allow else None

            if user_decision is None and interactive and sys.stdin.isatty():
                print("\n" + "=" * 75)
                print("⚠️  [WARNING] Config Load Verification Generated Warnings:")
                print("=" * 75)
                for w in warning_lines:
                    print(f"  - {w}")
                print("-" * 75)

                try:
                    ans = input(
                        "Do you want to accept these warnings and proceed with the upgrade? (y/N): "
                    ).strip().lower()
                    if ans in ("y", "yes"):
                        user_decision = "ACCEPTED"
                    else:
                        user_decision = "REJECTED"
                except (EOFError, KeyboardInterrupt):
                    user_decision = "REJECTED"

            if user_decision == "ACCEPTED":
                return CheckResult(
                    id="CFG-001",
                    category="Configuration and Policy Integrity",
                    name="Config load verification (all partitions)",
                    status="RISK_ACCEPTED",
                    details={
                        "note": "Warnings detected during config load and accepted by operator.",
                        "command": cmd,
                        "warning_lines": warning_lines,
                        "full_output": out,
                    },
                )

            return CheckResult(
                id="CFG-001",
                category="Configuration and Policy Integrity",
                name="Config load verification (all partitions)",
                status="FAIL",
                details={
                    "error": "Configuration load has warnings and operator did not accept them (or non-interactive).",
                    "command": cmd,
                    "warning_lines": warning_lines,
                    "full_output": out,
                },
            )

        # Clean PASS (No errors, no warnings)
        return CheckResult(
            id="CFG-001",
            category="Configuration and Policy Integrity",
            name="Config load verification (all partitions)",
            status="PASS",
            details={
                "command": cmd,
                "note": "Configuration validated cleanly across all partitions with no errors or warnings.",
                "output": out or "CLEAN",
            },
        )

    except Exception as e:
        return CheckResult(
            id="CFG-001",
            category="Configuration and Policy Integrity",
            name="Config load verification (all partitions)",
            status="FAIL",
            details={
                "error": f"Failed to execute config verify: {e}",
                "command": cmd,
            },
        )


def run_prechecks(client: BigIPClient, interactive: bool = True) -> List[CheckResult]:
    """
    Run formal prechecks before an upgrade.

    Order:
      1. Platform reachability/version
      2. Configuration syntax validation across all partitions
      3. HA failover status
      4. ConfigSync status
      5. Rollback software volume visibility
      6. License reachability
    """
    results: List[CheckResult] = []

    # ---------- 1. Platform / reachability ----------
    try:
        ver = client.system_version()
        results.append(
            CheckResult(
                id="PLAT-001",
                category="Platform and Version State",
                name="System version reachable",
                status="PASS",
                details={"version_keys": list(ver.keys())},
            )
        )
    except Exception as e:
        results.append(
            CheckResult(
                id="PLAT-001",
                category="Platform and Version State",
                name="System version reachable",
                status="FAIL",
                details={"error": str(e)},
            )
        )
        # Fail fast: if version cannot be read, nothing else is trustworthy.
        return results

    # ---------- 2. Config load verification across all partitions ----------
    cfg_check = check_config_load(client, interactive=interactive)
    results.append(cfg_check)
    if cfg_check.status == "FAIL":
        # Fail fast if configuration validation fails.
        return results

    # ---------- 3. HA / failover status ----------
    try:
        fo = client.failover_state()
        results.append(
            CheckResult(
                id="HA-001",
                category="High Availability Health",
                name="Failover status captured",
                status="PASS",
                details={"failover_status": fo},
            )
        )
    except Exception as e:
        results.append(
            CheckResult(
                id="HA-001",
                category="High Availability Health",
                name="Failover status captured",
                status="FAIL",
                details={"error": str(e)},
            )
        )

    # ---------- 4. ConfigSync status ----------
    try:
        sync = client.sync_status()
        results.append(
            CheckResult(
                id="HA-002",
                category="High Availability Health",
                name="ConfigSync status captured",
                status="PASS",
                details={"sync_status": sync},
            )
        )
    except Exception as e:
        results.append(
            CheckResult(
                id="HA-002",
                category="High Availability Health",
                name="ConfigSync status captured",
                status="FAIL",
                details={"error": str(e)},
            )
        )

    # ---------- 5. Rollback readiness: software volumes visible ----------
    try:
        vols = client.software_volumes()
        results.append(
            CheckResult(
                id="RB-001",
                category="Rollback Readiness",
                name="Software volumes visible",
                status="PASS",
                details={"software_volumes": vols},
            )
        )
    except Exception as e:
        results.append(
            CheckResult(
                id="RB-001",
                category="Rollback Readiness",
                name="Software volumes visible",
                status="FAIL",
                details={"error": str(e)},
            )
        )

    # ---------- 6. License reachable ----------
    try:
        lic = client.license_info()
        results.append(
            CheckResult(
                id="PLAT-002",
                category="Platform and Version State",
                name="License information reachable",
                status="PASS",
                details={"license_keys": list(lic.keys())},
            )
        )
    except Exception as e:
        results.append(
            CheckResult(
                id="PLAT-002",
                category="Platform and Version State",
                name="License information reachable",
                status="FAIL",
                details={"error": str(e)},
            )
        )

    return results
