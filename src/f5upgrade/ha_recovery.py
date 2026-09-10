from __future__ import annotations

from typing import Any, Dict, List, Optional

from .bigip_client import BigIPClient
from .ha import get_failover_role
from .report import CheckResult


def _ok(check_id: str, name: str, details: Dict[str, Any]) -> CheckResult:
    return CheckResult(
        id=check_id,
        category="HA Recovery",
        name=name,
        status="PASS",
        details=details,
    )


def _fail(check_id: str, name: str, details: Dict[str, Any]) -> CheckResult:
    return CheckResult(
        id=check_id,
        category="HA Recovery",
        name=name,
        status="FAIL",
        details=details,
    )


def val_trust_domain(
    client: BigIPClient, expect_devices: Optional[List[str]] = None
) -> CheckResult:
    """
    VAL-HA-001:
    Validate trust domain contains both devices and no obvious incompatibility flags.
    Uses iControl: /mgmt/tm/cm/trust-domain
    """
    try:
        td = client.get("/mgmt/tm/cm/trust-domain")

        # Content varies; keep validation pragmatic
        td_str = str(td).lower()
        if "incompatible" in td_str:
            return _fail(
                "VAL-HA-001",
                "Trust domain healthy",
                {
                    "error": "Found 'incompatible' in trust-domain output",
                    "trust_domain": td,
                },
            )

        found_names: List[str] = []

        # Sometimes nested: items -> [ { 'name':..., 'membersReference': ... } ] etc.
        if isinstance(td, dict):
            items = td.get("items", [])
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        n = it.get("name")
                        if n:
                            found_names.append(str(n))

        if expect_devices:
            missing = [
                d
                for d in expect_devices
                if d not in found_names and d.lower() not in td_str
            ]
            if missing:
                return _fail(
                    "VAL-HA-001",
                    "Trust domain healthy",
                    {
                        "error": "Expected devices not observed in trust-domain view",
                        "missing": missing,
                        "observed_names": found_names,
                        "trust_domain": td,
                    },
                )

        return _ok(
            "VAL-HA-001",
            "Trust domain healthy",
            {"observed_names": found_names, "trust_domain_hint": "ok"},
        )
    except Exception as e:
        return _fail(
            "VAL-HA-001",
            "Trust domain healthy",
            {"error": str(e)},
        )


def val_sync_status(client: BigIPClient) -> CheckResult:
    """
    VAL-SYNC-001:
    Validate cm sync-status is In Sync.
    Uses iControl: /mgmt/tm/cm/sync-status
    """
    try:
        s = client.sync_status()
        s_str = str(s).lower()
        if "in sync" in s_str or "insync" in s_str:
            return _ok(
                "VAL-SYNC-001",
                "ConfigSync In Sync",
                {"sync_status": s},
            )
        return _fail(
            "VAL-SYNC-001",
            "ConfigSync In Sync",
            {"error": "Not In Sync", "sync_status": s},
        )
    except Exception as e:
        return _fail(
            "VAL-SYNC-001",
            "ConfigSync In Sync",
            {"error": str(e)},
        )


def val_traffic_group_1(client: BigIPClient) -> CheckResult:
    """
    VAL-TG-001:
    Validate traffic-group-1 has exactly one active and one standby.
    Uses tmsh output via bash:
      tmsh -q show cm traffic-group
    """
    try:
        resp = client.run_bash("tmsh -q show cm traffic-group")
        out = resp.get("commandResult", "")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        tg1_lines = [ln for ln in lines if ln.startswith("traffic-group-1")]

        # Expect two lines (one per device) in typical output
        active = [ln for ln in tg1_lines if " active " in f" {ln} "]
        standby = [ln for ln in tg1_lines if " standby " in f" {ln} "]

        if len(active) == 1 and len(standby) == 1:
            return _ok(
                "VAL-TG-001",
                "Traffic-group-1 ownership sane",
                {"active_line": active[0], "standby_line": standby[0]},
            )

        return _fail(
            "VAL-TG-001",
            "Traffic-group-1 ownership sane",
            {
                "error": "Unexpected TG ownership state",
                "tg1_lines": tg1_lines,
                "active_count": len(active),
                "standby_count": len(standby),
            },
        )
    except Exception as e:
        return _fail(
            "VAL-TG-001",
            "Traffic-group-1 ownership sane",
            {"error": str(e)},
        )


def exec_config_sync_to_group(client: BigIPClient, device_group: str) -> CheckResult:
    """
    EXEC-SYNC-001:
    Force ConfigSync to a specific device-group.
    Safe-ish (config operation), but still changes state.
    """
    try:
        cmd = f"tmsh -q run cm config-sync to-group {device_group}"
        resp = client.run_bash(cmd)
        out = resp.get("commandResult", "")
        return _ok(
            "EXEC-SYNC-001",
            "Forced ConfigSync to group",
            {"device_group": device_group, "tmsh": cmd, "bash_result": out},
        )
    except Exception as e:
        return _fail(
            "EXEC-SYNC-001",
            "Forced ConfigSync to group",
            {"device_group": device_group, "error": str(e)},
        )


def exec_failover_test_standby(client: BigIPClient) -> CheckResult:
    """
    EXEC-FAILOVER-TEST-001 (optional):
    Ask current ACTIVE to go STANDBY (causes failover).
    High impact. Keep disabled by default.
    """
    role = (get_failover_role(client) or "").lower()
    if role != "active":
        return _fail(
            "EXEC-FAILOVER-TEST-001",
            "Controlled failover test",
            {"error": "Refusing: node is not ACTIVE", "role": role},
        )

    try:
        cmd = "tmsh -q run sys failover standby"
        resp = client.run_bash(cmd)
        out = resp.get("commandResult", "")
        return _ok(
            "EXEC-FAILOVER-TEST-001",
            "Controlled failover test",
            {"tmsh": cmd, "bash_result": out},
        )
    except Exception as e:
        return _fail(
            "EXEC-FAILOVER-TEST-001",
            "Controlled failover test",
            {"tmsh": "tmsh run sys failover standby", "error": str(e)},
        )
