from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bigip_client import BigIPClient


class StateCollector:
    """
    Automated State & Snapshot Collector for BIG-IP Pre/Post Upgrade Validation.

    Collects structured BIG-IP state through iControl REST and also captures
    raw all-partition LTM evidence through tmsh.

    Important:
      - Structured REST stats are useful for JSON/Excel comparison.
      - Raw tmsh recursive evidence is used to prove all partitions were captured.
      - The all-partition commands intentionally use:
            tmsh -q -c "cd /; ... recursive"
        so objects outside /Common are included.
    """

    def __init__(self, client: BigIPClient) -> None:
        self.client = client

    def get_entries(self, endpoint: str) -> Dict[str, Any]:
        """Query an endpoint and safely return the nested 'entries' dictionary."""
        try:
            resp = self.client.get(endpoint)
            return resp.get("entries", {}) if isinstance(resp, dict) else {}
        except Exception:
            return {}

    def get_raw(self, endpoint: str) -> Dict[str, Any]:
        """Query an endpoint and return the raw dictionary."""
        try:
            resp = self.client.get(endpoint)
            return resp if isinstance(resp, dict) else {}
        except Exception:
            return {}

    def run_tmsh(self, command: str, timeout: int = 120) -> str:
        """
        Run a tmsh/bash command through iControl REST and return commandResult.

        Keep commands double-quoted if using tmsh -c because BigIPClient.run_bash()
        wraps the whole command with single quotes.
        """
        try:
            resp = self.client.run_bash(command, timeout=timeout)
            if isinstance(resp, dict):
                return str(resp.get("commandResult", "")).strip()
            return str(resp).strip()
        except Exception as exc:
            return (
                f"ERROR running command:\n{command}\n\n"
                f"{type(exc).__name__}: {exc}"
            )

    def display_ltm_health_summary(self) -> None:
        """
        Display the operator-facing LTM health summary used during upgrades.

        These are read-only commands. The output is informational; the
        structured pre/post checks remain authoritative for pass/fail status.
        """
        commands = [
            (
                'tmsh -q -c "cd / ; list ltm pool recursive" '
                '| grep "state up" | wc -l'
            ),
            (
                'tmsh -q -c "cd / ; list ltm pool recursive" '
                '| grep "state " | sort | uniq -c'
            ),
            (
                'tmsh -q -c "cd / ; show ltm virtual recursive" '
                '| grep -i "State\\|Availability" | sort | uniq -c'
            ),
        ]

        print("\n" + "-" * 75)
        print("LTM HEALTH SUMMARY (informational)")
        print("-" * 75)
        for command in commands:
            print(f"\n$ {command}")
            output = self.run_tmsh(command)
            print(output or "(no output)")
        print("-" * 75 + "\n")

    def collect_all_state(self) -> Dict[str, Any]:
        """Collects structured state plus raw all-partition LTM evidence."""
        return {
            "metadata": {
                "collected_at": datetime.now().isoformat(),
                "host": self.client.host,
                "collector_note": (
                    "Structured REST stats plus raw all-partition tmsh recursive evidence."
                ),
            },
            "virtual_servers": self._collect_virtual_servers(),
            "pools": self._collect_pools(),
            "nodes": self._collect_nodes(),
            "sync_status": self._collect_sync_status(),
            "device_groups": self._collect_device_groups(),
            "dns": self._collect_dns(),
            "ntp": self._collect_ntp(),
            "syslog": self._collect_syslog(),
            "mgmt_routes": self._collect_mgmt_routes(),
            "tmm_routes": self._collect_tmm_routes(),
            "route_domains": self._collect_route_domains(),
            "interface_stats": self._collect_interfaces(),
            "system_performance": self._collect_performance(),
            "crypto_inventory": self._collect_crypto_inventory(),
            "ltm_raw_all_partitions": self._collect_ltm_raw_all_partitions(),
        }

    def _collect_crypto_inventory(self) -> Dict[str, Any]:
        """Collect non-FIPS certificate/key counts and FIPS key counts only."""
        cert_output = self.run_tmsh(
            'tmsh -q -c "cd /; list sys file ssl-cert" | wc -l'
        )
        key_output = self.run_tmsh(
            'tmsh -q -c "cd /; list sys file ssl-key" | wc -l'
        )
        fips_output = self.run_tmsh("tmsh show sys crypto fips key")

        def parse_count(output: str) -> Optional[int]:
            match = re.search(r"(?m)^\s*(\d+)\s*$", output or "")
            return int(match.group(1)) if match else None

        fips_counts: Dict[str, int] = {}
        for label, count in re.findall(
            r"(?im)^\s*(private keys|public keys)\s*\((\d+)\)",
            fips_output or "",
        ):
            fips_counts[label.lower()] = int(count)

        return {
            "non_fips": {
                "ssl_cert_count": parse_count(cert_output),
                "ssl_key_count": parse_count(key_output),
            },
            "fips": {
                "private_key_count": fips_counts.get("private keys"),
                "public_key_count": fips_counts.get("public keys"),
                "raw_output": fips_output,
            },
        }

    # ---------------- Common Helpers ----------------

    def _split_f5_name(self, value: str) -> Tuple[str, str, str]:
        """
        Split an F5 object name into full_path, partition, object_name.

        Handles values like:
          /Common/vs_app
          Common/vs_app
          ~Common~vs_app
          vs_app
        """
        raw = str(value or "").strip()

        if not raw:
            return "", "", ""

        # REST stats keys often contain ~Partition~Object.
        if "~" in raw:
            parts = [p for p in raw.split("~") if p]
            if len(parts) >= 2:
                partition = parts[-2]
                obj_name = parts[-1].split("/")[-1]
                return f"/{partition}/{obj_name}", partition, obj_name

        # tmName often appears as /Partition/Object.
        cleaned = raw
        if cleaned.startswith("/"):
            cleaned = cleaned[1:]

        pieces = cleaned.split("/")
        if len(pieces) >= 2:
            partition = pieces[0]
            obj_name = "/".join(pieces[1:])
            return f"/{partition}/{obj_name}", partition, obj_name

        return raw, "", raw

    # ---------------- LTM State Collection ----------------

    def _collect_virtual_servers(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/ltm/virtual/stats")
        rows = []

        for entry_key, stat_block in entries.items():
            nstats = stat_block.get("nestedStats", {}).get("entries", {})
            tm_name = nstats.get("tmName", {}).get("description", "")
            full_path, partition, object_name = self._split_f5_name(tm_name or entry_key)

            rows.append(
                {
                    "name": full_path or tm_name,
                    "partition": partition,
                    "objectName": object_name,
                    "rawName": tm_name,
                    "availabilityState": nstats.get(
                        "status.availabilityState", {}
                    ).get("description", ""),
                    "enabledState": nstats.get("status.enabledState", {}).get(
                        "description", ""
                    ),
                    "statusReason": nstats.get("status.statusReason", {}).get(
                        "description", ""
                    ),
                }
            )

        return rows

    def _collect_pools(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/ltm/pool/stats")
        rows = []

        for entry_key, stat_block in entries.items():
            nstats = stat_block.get("nestedStats", {}).get("entries", {})
            tm_name = nstats.get("tmName", {}).get("description", "")
            full_path, partition, object_name = self._split_f5_name(tm_name or entry_key)

            rows.append(
                {
                    "name": full_path or tm_name,
                    "partition": partition,
                    "objectName": object_name,
                    "rawName": tm_name,
                    "availabilityState": nstats.get(
                        "status.availabilityState", {}
                    ).get("description", ""),
                    "enabledState": nstats.get("status.enabledState", {}).get(
                        "description", ""
                    ),
                    "statusReason": nstats.get("status.statusReason", {}).get(
                        "description", ""
                    ),
                    "activeMemberCnt": nstats.get("activeMemberCnt", {}).get("value", 0),
                    "availableMemberCnt": nstats.get("availableMemberCnt", {}).get(
                        "value", 0
                    ),
                    "memberCnt": nstats.get("memberCnt", {}).get("value", 0),
                }
            )

        return rows

    def _collect_nodes(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/ltm/node/stats")
        rows = []

        for entry_key, stat_block in entries.items():
            nstats = stat_block.get("nestedStats", {}).get("entries", {})
            tm_name = nstats.get("tmName", {}).get("description", "")
            full_path, partition, object_name = self._split_f5_name(tm_name or entry_key)

            rows.append(
                {
                    "name": full_path or tm_name,
                    "partition": partition,
                    "objectName": object_name,
                    "rawName": tm_name,
                    "availabilityState": nstats.get(
                        "status.availabilityState", {}
                    ).get("description", ""),
                    "enabledState": nstats.get("status.enabledState", {}).get(
                        "description", ""
                    ),
                    "statusReason": nstats.get("status.statusReason", {}).get(
                        "description", ""
                    ),
                }
            )

        return rows

    def _collect_ltm_raw_all_partitions(self) -> List[Dict[str, Any]]:
        """
        Capture raw all-partition LTM config and runtime evidence.

        These commands are the audit-grade proof that objects from partitions
        outside /Common are included.
        """
        commands = [
            (
                "virtual_list_recursive",
                'tmsh -q -c "cd /; list ltm virtual recursive"',
            ),
            (
                "pool_list_recursive",
                'tmsh -q -c "cd /; list ltm pool recursive"',
            ),
            (
                "node_list_recursive",
                'tmsh -q -c "cd /; list ltm node recursive"',
            ),
            (
                "virtual_show_recursive",
                'tmsh -q -c "cd /; show ltm virtual recursive"',
            ),
            (
                "pool_show_recursive",
                'tmsh -q -c "cd /; show ltm pool recursive"',
            ),
            (
                "node_show_recursive",
                'tmsh -q -c "cd /; show ltm node recursive"',
            ),
        ]

        rows: List[Dict[str, Any]] = []

        for artifact, command in commands:
            output = self.run_tmsh(command)
            rows.append(
                {
                    "artifact": artifact,
                    "command": command,
                    "status": "FAIL" if output.startswith("ERROR running command:") else "PASS",
                    "line_count": len(output.splitlines()) if output else 0,
                    "output": output,
                }
            )

        return rows

    # ---------------- System & HA State Collection ----------------

    def _collect_sync_status(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/cm/sync-status")
        rows = []

        for e in entries.values():
            n = e.get("nestedStats", {}).get("entries", {})
            row = {
                "color": n.get("color", {}).get("description", ""),
                "mode": n.get("mode", {}).get("description", ""),
                "status": n.get("status", {}).get("description", ""),
                "summary": n.get("summary", {}).get("description", ""),
            }

            detail_obj = n.get("https://localhost/mgmt/tm/cm/syncStatus/0/details")
            details = []

            if detail_obj and isinstance(detail_obj, dict):
                d_entries = detail_obj.get("nestedStats", {}).get("entries", {})
                for dv in d_entries.values():
                    d = (
                        dv.get("nestedStats", {})
                        .get("entries", {})
                        .get("details", {})
                        .get("description", "")
                    )
                    if d:
                        details.append(d)

            for i, d in enumerate(details):
                row[f"detail_{i + 1}"] = d

            rows.append(row)

        return rows

    def _collect_device_groups(self) -> List[Dict[str, Any]]:
        data = self.get_raw("/mgmt/tm/cm/device-group")
        return [
            {
                "name": i.get("name", ""),
                "type": i.get("type", ""),
                "networkFailover": i.get("networkFailover", ""),
                "autoSync": i.get("autoSync", ""),
                "asmSync": i.get("asmSync", ""),
            }
            for i in data.get("items", [])
            if isinstance(i, dict)
        ]

    def _collect_dns(self) -> List[Dict[str, Any]]:
        d = self.get_raw("/mgmt/tm/sys/dns")
        return [
            {
                "nameServers": ", ".join(d.get("nameServers", [])),
                "search": ", ".join(d.get("search", [])),
                "numberOfDots": d.get("numberOfDots", ""),
            }
        ]

    def _collect_ntp(self) -> List[Dict[str, Any]]:
        d = self.get_raw("/mgmt/tm/sys/ntp")
        return [
            {
                "servers": ", ".join(d.get("servers", [])),
                "timezone": d.get("timezone", ""),
            }
        ]

    def _collect_syslog(self) -> List[Dict[str, Any]]:
        d = self.get_raw("/mgmt/tm/sys/syslog")
        rows = []

        for r in d.get("remoteServers", []):
            if isinstance(r, dict):
                rows.append(
                    {
                        "name": r.get("name", ""),
                        "host": r.get("host", ""),
                        "localIp": r.get("localIp", ""),
                        "remotePort": r.get("remotePort", ""),
                    }
                )

        if not rows:
            rows.append({"name": "", "host": "", "localIp": "", "remotePort": ""})

        return rows

    def _collect_mgmt_routes(self) -> List[Dict[str, Any]]:
        data = self.get_raw("/mgmt/tm/sys/management-route")
        return [
            {
                "name": i.get("name", ""),
                "network": i.get("network", ""),
                "gateway": i.get("gateway", ""),
                "mtu": i.get("mtu", ""),
            }
            for i in data.get("items", [])
            if isinstance(i, dict)
        ]

    def _collect_tmm_routes(self) -> List[Dict[str, Any]]:
        data = self.get_raw("/mgmt/tm/net/route")
        return [
            {
                "name": i.get("name", ""),
                "network": i.get("network", ""),
                "tmInterface": i.get("tmInterface", "") or i.get("gw", ""),
                "mtu": i.get("mtu", ""),
            }
            for i in data.get("items", [])
            if isinstance(i, dict)
        ]

    def _collect_route_domains(self) -> List[Dict[str, Any]]:
        data = self.get_raw("/mgmt/tm/net/route-domain")
        return [
            {
                "id": i.get("id", ""),
                "name": i.get("name", ""),
                "connectionLimit": i.get("connectionLimit", ""),
                "strict": i.get("strict", ""),
                "throughputCapacity": i.get("throughputCapacity", ""),
                "vlans": ", ".join(i.get("vlans", [])),
            }
            for i in data.get("items", [])
            if isinstance(i, dict)
        ]

    def _collect_interfaces(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/net/interface/stats")
        stats = []

        for v in entries.values():
            n = v.get("nestedStats", {}).get("entries", {})
            stats.append(
                {
                    "tmName": n.get("tmName", {}).get("description", ""),
                    "status": n.get("status", {}).get("description", ""),
                    "mediaActive": n.get("mediaActive", {}).get("description", ""),
                    "bitsIn": n.get("counters.bitsIn", {}).get("value", 0),
                    "bitsOut": n.get("counters.bitsOut", {}).get("value", 0),
                    "pktsIn": n.get("counters.pktsIn", {}).get("value", 0),
                    "pktsOut": n.get("counters.pktsOut", {}).get("value", 0),
                    "dropsAll": n.get("counters.dropsAll", {}).get("value", 0),
                    "errorsAll": n.get("counters.errorsAll", {}).get("value", 0),
                }
            )

        return stats

    def _collect_performance(self) -> List[Dict[str, Any]]:
        entries = self.get_entries("/mgmt/tm/sys/performance/system")
        rows = []

        for e in entries.values():
            n = e.get("nestedStats", {}).get("entries", {})
            stat = (
                n.get("Memory Used", {}).get("description")
                or n.get("System CPU Usage", {}).get("description")
                or "Stat"
            )

            row = {
                "Stat": stat,
                "Current": n.get("Current", {}).get("description", ""),
                "Average": n.get("Average", {}).get("description", ""),
            }

            for k in n:
                if k.startswith("Max"):
                    row["Max"] = n[k].get("description", "")
                    break

            rows.append(row)

        return rows

    # ---------------- Backup Generation (UCS & SCF) ----------------

    def create_backups(self) -> Dict[str, Any]:
        """
        Creates UCS and SCF backups.

        Note:
          This method is retained for compatibility, but the preferred backup
          workflow is scripts/run_backup_artifacts.py because it handles
          qkview/asmqkview and SSH-based copy more reliably.
        """
        results: Dict[str, Any] = {}

        ucs_cmd = (
            "short_host=$(echo $HOSTNAME | cut -d'.' -f1); "
            "backup_name=${short_host}-$(date +%H%M-%m%d%y); "
            "tmsh save sys ucs ${backup_name}; "
            "echo; "
            "echo UCS File Created: /var/local/ucs/${backup_name}.ucs; "
            "ls -lh /var/local/ucs/${backup_name}.ucs 2>/dev/null || true"
        )
        ucs_resp = self.client.run_bash(ucs_cmd, timeout=600)
        results["ucs"] = {
            "command": ucs_cmd,
            "output": ucs_resp.get("commandResult", ""),
        }

        scf_cmd = (
            "short_host=$(echo $HOSTNAME | cut -d'.' -f1); "
            "backup_name=${short_host}-$(date +%H%M-%m%d%y); "
            "tmsh save sys config file ${backup_name} no-passphrase; "
            "echo; "
            "echo SCF File Created: /var/local/scf/${backup_name}.scf; "
            "ls -lh /var/local/scf/${backup_name}.scf 2>/dev/null || true"
        )
        scf_resp = self.client.run_bash(scf_cmd, timeout=300)
        results["scf"] = {
            "command": scf_cmd,
            "output": scf_resp.get("commandResult", ""),
        }

        return results

    # ---------------- Persistence Helpers ----------------

    def save_snapshot_json(self, state: Dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def _excel_safe_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Excel cells cannot safely hold very large command output.

        JSON keeps the full output.
        Excel receives a readable truncated copy only when needed.
        """
        safe_rows: List[Dict[str, Any]] = []

        for row in rows:
            safe_row: Dict[str, Any] = {}
            for key, value in row.items():
                if isinstance(value, str) and len(value) > 32000:
                    safe_row[key] = (
                        value[:32000]
                        + "\n\n[TRUNCATED_FOR_EXCEL_ONLY_FULL_OUTPUT_IN_JSON]"
                    )
                else:
                    safe_row[key] = value
            safe_rows.append(safe_row)

        return safe_rows

    def save_snapshot_excel(self, state: Dict[str, Any], path: Path) -> bool:
        """Saves all table-style sections to a multi-sheet Excel spreadsheet if pandas is available."""
        try:
            import pandas as pd

            path.parent.mkdir(parents=True, exist_ok=True)

            with pd.ExcelWriter(str(path)) as writer:
                for sheet_name, rows in state.items():
                    if sheet_name == "metadata" or not isinstance(rows, list):
                        continue

                    df = pd.DataFrame(self._excel_safe_rows(rows))
                    clean_sheet = sheet_name[:31]  # Excel 31-char sheet name limit
                    df.to_excel(writer, sheet_name=clean_sheet, index=False)

            return True
        except ImportError:
            return False
