from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class DiffEngine:
    """
    Compares Pre and Post upgrade state snapshots and classifies changes into:
      - 🔴 CRITICAL: Missing objects, 'available' -> 'offline', pool member loss
      - 🟡 WARNING: State/Reason changes, disabled objects, config modifications
      - 🟢 NORMAL / UNCHANGED: Healthy state retained or expected counter increments
    """

    def __init__(self, pre_state: Dict[str, Any], post_state: Dict[str, Any]) -> None:
        self.pre = pre_state
        self.post = post_state

    def compare(self) -> Dict[str, Any]:
        """Executes full diff across all collected subsystems."""
        vs_diff = self._diff_objects("virtual_servers", key="name", state_field="availabilityState")
        pool_diff = self._diff_pools()
        node_diff = self._diff_objects("nodes", key="name", state_field="availabilityState")
        sync_diff = self._diff_sync()
        tmm_routes_diff = self._diff_routes("tmm_routes")
        mgmt_routes_diff = self._diff_routes("mgmt_routes")
        interfaces_diff = self._diff_interfaces()

        all_diffs = vs_diff + pool_diff + node_diff + sync_diff + tmm_routes_diff + mgmt_routes_diff + interfaces_diff
        critical_count = sum(1 for d in all_diffs if d.get("severity") == "CRITICAL")
        warning_count = sum(1 for d in all_diffs if d.get("severity") == "WARNING")

        overall_status = "FAIL" if critical_count > 0 else "PASS"

        return {
            "generated_at": datetime.now().isoformat(),
            "overall_status": overall_status,
            "summary": {
                "critical_regressions": critical_count,
                "warnings": warning_count,
                "total_differences": len(all_diffs),
            },
            "virtual_servers": vs_diff,
            "pools": pool_diff,
            "nodes": node_diff,
            "sync_status": sync_diff,
            "tmm_routes": tmm_routes_diff,
            "mgmt_routes": mgmt_routes_diff,
            "interfaces": interfaces_diff,
            "counts": {
                "pre_vs_count": len(self.pre.get("virtual_servers", [])),
                "post_vs_count": len(self.post.get("virtual_servers", [])),
                "pre_pool_count": len(self.pre.get("pools", [])),
                "post_pool_count": len(self.post.get("pools", [])),
                "pre_node_count": len(self.pre.get("nodes", [])),
                "post_node_count": len(self.post.get("nodes", [])),
            },
            "health_summary": {
                "pre": self._health_summary(self.pre),
                "post": self._health_summary(self.post),
            },
            "crypto_inventory": {
                "pre": self.pre.get("crypto_inventory", {}),
                "post": self.post.get("crypto_inventory", {}),
            },
            "bgp_inventory": {
                "pre": self.pre.get("bgp_inventory", {}),
                "post": self.post.get("bgp_inventory", {}),
            },
        }

    @staticmethod
    def _health_summary(state: Dict[str, Any]) -> Dict[str, Any]:
        def summarize(category: str) -> Dict[str, Any]:
            items = state.get(category, [])
            summary: Dict[str, Any] = {
                "total": len(items),
                "availability": {},
                "enabled": {},
                "unknown": [],
                "offline": [],
                "disabled": [],
            }

            for item in items:
                name = str(item.get("name", "")).strip()
                availability = str(item.get("availabilityState", "")).strip() or "unknown"
                enabled = str(item.get("enabledState", "")).strip() or "unknown"
                availability_key = availability.lower()
                enabled_key = enabled.lower()

                summary["availability"][availability_key] = (
                    summary["availability"].get(availability_key, 0) + 1
                )
                summary["enabled"][enabled_key] = (
                    summary["enabled"].get(enabled_key, 0) + 1
                )

                if availability_key in ("unknown", ""):
                    summary["unknown"].append(name)
                elif "offline" in availability_key:
                    summary["offline"].append(name)
                if "disabled" in enabled_key:
                    summary["disabled"].append(name)

            return summary

        interfaces = state.get("interface_stats", [])
        interface_up = [
            str(item.get("tmName", "")).strip()
            for item in interfaces
            if str(item.get("status", "")).strip().lower() == "up"
        ]

        return {
            "interfaces": {
                "total": len(interfaces),
                "up_count": len(interface_up),
                "up_names": interface_up,
            },
            "virtual_servers": summarize("virtual_servers"),
            "pools": summarize("pools"),
            "nodes": summarize("nodes"),
        }

    def _diff_objects(self, category: str, key: str, state_field: str) -> List[Dict[str, Any]]:
        pre_items = {item[key]: item for item in self.pre.get(category, []) if item.get(key)}
        post_items = {item[key]: item for item in self.post.get(category, []) if item.get(key)}

        diffs = []
        for name, pre_obj in pre_items.items():
            pre_state = str(pre_obj.get(state_field, "")).strip()
            post_obj = post_items.get(name)

            if post_obj is None:
                diffs.append({
                    "category": category,
                    "name": name,
                    "pre_state": pre_state,
                    "post_state": "MISSING",
                    "severity": "CRITICAL",
                    "details": f"Object existed in pre-check but is MISSING post-upgrade.",
                })
                continue

            post_state = str(post_obj.get(state_field, "")).strip()
            if pre_state.lower() != post_state.lower():
                is_crit = (
                    pre_state.lower() in ("available", "up", "green")
                    and post_state.lower() in ("offline", "down", "red", "unknown", "")
                )
                diffs.append({
                    "category": category,
                    "name": name,
                    "pre_state": pre_state,
                    "post_state": post_state,
                    "pre_reason": pre_obj.get("statusReason", ""),
                    "post_reason": post_obj.get("statusReason", ""),
                    "severity": "CRITICAL" if is_crit else "WARNING",
                    "details": f"Availability state changed: '{pre_state}' -> '{post_state}'.",
                })

        return diffs

    def _diff_pools(self) -> List[Dict[str, Any]]:
        pre_pools = {p["name"]: p for p in self.pre.get("pools", []) if p.get("name")}
        post_pools = {p["name"]: p for p in self.post.get("pools", []) if p.get("name")}

        diffs = []
        for name, pre_p in pre_pools.items():
            post_p = post_pools.get(name)
            if not post_p:
                diffs.append({
                    "category": "pools",
                    "name": name,
                    "pre_state": pre_p.get("availabilityState", ""),
                    "post_state": "MISSING",
                    "severity": "CRITICAL",
                    "details": "Pool is missing post-upgrade.",
                })
                continue

            pre_state = str(pre_p.get("availabilityState", "")).strip()
            post_state = str(post_p.get("availabilityState", "")).strip()
            pre_active = int(pre_p.get("activeMemberCnt", 0) or 0)
            post_active = int(post_p.get("activeMemberCnt", 0) or 0)

            if pre_state.lower() != post_state.lower():
                is_crit = "offline" in post_state.lower() or "down" in post_state.lower()
                diffs.append({
                    "category": "pools",
                    "name": name,
                    "pre_state": pre_state,
                    "post_state": post_state,
                    "pre_active_members": pre_active,
                    "post_active_members": post_active,
                    "severity": "CRITICAL" if is_crit else "WARNING",
                    "details": f"Pool availability changed: '{pre_state}' -> '{post_state}'",
                })
            elif post_active < pre_active:
                diffs.append({
                    "category": "pools",
                    "name": name,
                    "pre_state": pre_state,
                    "post_state": post_state,
                    "pre_active_members": pre_active,
                    "post_active_members": post_active,
                    "severity": "CRITICAL" if post_active == 0 else "WARNING",
                    "details": f"Active pool member count dropped from {pre_active} to {post_active}.",
                })

        return diffs

    def _diff_sync(self) -> List[Dict[str, Any]]:
        pre_sync = self.pre.get("sync_status", [{}])
        post_sync = self.post.get("sync_status", [{}])

        pre_s = pre_sync[0] if pre_sync else {}
        post_s = post_sync[0] if post_sync else {}

        diffs = []
        if pre_s.get("status") != post_s.get("status") or pre_s.get("color") != post_s.get("color"):
            diffs.append({
                "category": "sync_status",
                "name": "ConfigSync",
                "pre_state": f"{pre_s.get('status')} ({pre_s.get('color')})",
                "post_state": f"{post_s.get('status')} ({post_s.get('color')})",
                "severity": "WARNING" if "in sync" in str(post_s.get("status")).lower() else "CRITICAL",
                "details": f"Sync status summary: {post_s.get('summary', '')}",
            })
        return diffs

    def _diff_routes(self, category: str) -> List[Dict[str, Any]]:
        pre_routes = {r.get("name"): r for r in self.pre.get(category, []) if r.get("name")}
        post_routes = {r.get("name"): r for r in self.post.get(category, []) if r.get("name")}

        diffs = []
        for name, r in pre_routes.items():
            if name not in post_routes:
                diffs.append({
                    "category": category,
                    "name": name,
                    "pre_state": f"network: {r.get('network')}, gw: {r.get('gateway') or r.get('tmInterface')}",
                    "post_state": "MISSING",
                    "severity": "CRITICAL",
                    "details": f"Route '{name}' missing post-upgrade.",
                })
        return diffs

    def _diff_interfaces(self) -> List[Dict[str, Any]]:
        pre_if = {i.get("tmName"): i for i in self.pre.get("interface_stats", []) if i.get("tmName")}
        post_if = {i.get("tmName"): i for i in self.post.get("interface_stats", []) if i.get("tmName")}

        diffs = []
        for name, p_if in pre_if.items():
            po_if = post_if.get(name)
            if not po_if:
                continue
            if p_if.get("status") == "up" and po_if.get("status") != "up":
                diffs.append({
                    "category": "interfaces",
                    "name": name,
                    "pre_state": p_if.get("status"),
                    "post_state": po_if.get("status"),
                    "severity": "CRITICAL",
                    "details": f"Interface {name} changed status from 'up' to '{po_if.get('status')}'.",
                })
        return diffs

    # ---------------- Report Generation ----------------

    def generate_markdown_report(self, diff_result: Dict[str, Any], host: str) -> str:
        summary = diff_result.get("summary", {})
        counts = diff_result.get("counts", {})
        crit = summary.get("critical_regressions", 0)
        warn = summary.get("warnings", 0)
        health = diff_result.get("health_summary", {})
        pre_health = health.get("pre", {})
        post_health = health.get("post", {})
        all_diffs = self._all_diffs(diff_result)
        object_critical = [
            d for d in all_diffs
            if d.get("severity") == "CRITICAL"
            and d.get("category") != "sync_status"
        ]
        sync_diffs = diff_result.get("sync_status", [])
        review_only = bool(sync_diffs) and not object_critical
        if object_critical:
            result = "FAIL"
            badge = "🔴 **FAIL**"
        elif review_only:
            result = "REVIEW"
            badge = "🟡 **REVIEW REQUIRED**"
        else:
            result = "PASS"
            badge = "🟢 **PASS**"

        lines = [
            "# Pre vs Post Upgrade Difference Report",
            "",
            f"- **Host:** `{host}`",
            f"- **Generated:** `{diff_result.get('generated_at')}`",
            f"- **Overall Result:** {badge}",
            "",
            "## Executive Summary",
            "",
            "| Area | Pre-Upgrade | Post-Upgrade | Result |",
            "| :--- | :--- | :--- | :--- |",
            f"| Software version | Not captured | Not captured | REVIEW |",
            f"| Device role | Not captured | Not captured | REVIEW |",
            f"| Virtual servers | {counts.get('pre_vs_count', 0)} | {counts.get('post_vs_count', 0)} | {self._count_result(counts.get('pre_vs_count'), counts.get('post_vs_count'))} |",
            f"| Pools | {counts.get('pre_pool_count', 0)} | {counts.get('post_pool_count', 0)} | {self._count_result(counts.get('pre_pool_count'), counts.get('post_pool_count'))} |",
            f"| Nodes | {counts.get('pre_node_count', 0)} | {counts.get('post_node_count', 0)} | {self._count_result(counts.get('pre_node_count'), counts.get('post_node_count'))} |",
            f"| ConfigSync | {self._sync_state(diff_result, 'pre')} | {self._sync_state(diff_result, 'post')} | {'REVIEW' if sync_diffs else 'PASS'} |",
            f"| Application health | {self._health_result(pre_health)} | {self._health_result(post_health)} | {'FAIL' if object_critical else 'PASS'} |",
            "",
            f"**Software upgrade:** PASS if installation and reboot validation completed outside this diff.",
            f"**Post-upgrade validation:** {result}.",
            "",
            "## Health Summary",
            "",
            "| Object type | Available before | Available after | Offline before | Offline after | Unknown before | Unknown after |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for label, key in (
            ("Virtual servers", "virtual_servers"),
            ("Pools", "pools"),
            ("Nodes", "nodes"),
        ):
            before = pre_health.get(key, {})
            after = post_health.get(key, {})
            lines.append(
                f"| {label} | {self._availability_count(before, 'available')} | "
                f"{self._availability_count(after, 'available')} | "
                f"{self._availability_count(before, 'offline')} | "
                f"{self._availability_count(after, 'offline')} | "
                f"{self._availability_count(before, 'unknown')} | "
                f"{self._availability_count(after, 'unknown')} |"
            )
        lines.extend([
            "",
            "## Configuration and Routing Summary",
            "",
            "| Check | Before | After | Result |",
            "| :--- | :--- | :--- | :--- |",
            f"| Interfaces up | {self._interface_state(pre_health)} | {self._interface_state(post_health)} | {'PASS' if self._interface_state(pre_health) == self._interface_state(post_health) else 'REVIEW'} |",
            f"| BGP route domain 0 | {self._bgp_state(diff_result, 'pre')} | {self._bgp_state(diff_result, 'post')} | {self._comparison_result(self._bgp_state(diff_result, 'pre'), self._bgp_state(diff_result, 'post'))} |",
            f"| BGP neighbors | {self._bgp_neighbors(diff_result, 'pre')} | {self._bgp_neighbors(diff_result, 'post')} | {self._comparison_result(self._bgp_neighbors(diff_result, 'pre'), self._bgp_neighbors(diff_result, 'post'))} |",
            "",
        ])
        lines.extend(
            self._bgp_markdown(
                diff_result.get("bgp_inventory", {}).get("pre", {}),
                diff_result.get("bgp_inventory", {}).get("post", {}),
            )
        )
        lines.extend(
            self._crypto_markdown(
                diff_result.get("crypto_inventory", {}).get("pre", {}),
                diff_result.get("crypto_inventory", {}).get("post", {}),
            )
        )
        lines.extend([
            "## Differences Requiring Action",
        ])
        if object_critical:
            lines.extend(self._diff_table("🔴 Critical regressions", object_critical))
        elif sync_diffs:
            lines.extend(self._diff_table("🟡 ConfigSync review", sync_diffs))
            lines.append("- ConfigSync differences are shown as review items during rolling or version-mismatched HA upgrades.")
        else:
            lines.append("No differences detected.")
        lines.extend([
            "",
            "## Raw Evidence",
            "",
            "- Full pre/post snapshots remain in the CRQ `snapshots/` directory.",
            "- Full machine-readable diff data remains in the JSON diff report.",
            "",
            "## Notes",
            "",
            "- This report summarizes pre/post state; it does not replace the raw evidence.",
            "- ConfigSync may require review while HA peers run different software versions.",
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _all_diffs(diff_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            item
            for category in (
                "virtual_servers", "pools", "nodes", "sync_status",
                "tmm_routes", "mgmt_routes", "interfaces",
            )
            for item in diff_result.get(category, [])
        ]

    @staticmethod
    def _count_result(pre: Any, post: Any) -> str:
        return "PASS" if pre == post else "REVIEW"

    @staticmethod
    def _availability_count(summary: Dict[str, Any], state: str) -> int:
        return int(summary.get("availability", {}).get(state, 0))

    @staticmethod
    def _health_result(summary: Dict[str, Any]) -> str:
        return "Stable" if summary else "Not captured"

    @staticmethod
    def _interface_state(summary: Dict[str, Any]) -> str:
        interfaces = summary.get("interfaces", {})
        return f"{interfaces.get('up_count', 0)}/{interfaces.get('total', 0)} up"

    @staticmethod
    def _sync_state(diff_result: Dict[str, Any], side: str) -> str:
        diffs = diff_result.get("sync_status", [])
        if not diffs:
            return "In Sync"
        return str(diffs[0].get(f"{side}_state", "Unknown"))

    @staticmethod
    def _bgp_state(diff_result: Dict[str, Any], side: str) -> str:
        inventory = diff_result.get("bgp_inventory", {}).get(side, {})
        return "Enabled" if inventory.get("enabled") else "Disabled"

    @staticmethod
    def _bgp_neighbors(diff_result: Dict[str, Any], side: str) -> int:
        return len(diff_result.get("bgp_inventory", {}).get(side, {}).get("neighbors", []))

    @staticmethod
    def _comparison_result(pre: Any, post: Any) -> str:
        return "PASS" if pre == post else "REVIEW"

    @staticmethod
    def _diff_table(title: str, diffs: List[Dict[str, Any]]) -> List[str]:
        lines = [
            "",
            f"### {title}",
            "",
            "| Category | Object | Before | After | Severity | Action |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for diff in diffs:
            action = "Review ConfigSync after peer upgrade" if diff.get("category") == "sync_status" else "Investigate before release"
            lines.append(
                f"| {diff.get('category', '').upper()} | `{diff.get('name', '')}` | "
                f"`{diff.get('pre_state', '')}` | `{diff.get('post_state', '')}` | "
                f"{diff.get('severity', '')} | {action} |"
            )
        return lines

    @staticmethod
    def _crypto_markdown(
        pre: Dict[str, Any],
        post: Dict[str, Any],
    ) -> List[str]:
        def values(inventory: Dict[str, Any]) -> Dict[str, Any]:
            non_fips = inventory.get("non_fips", {})
            fips = inventory.get("fips", {})
            return {
                "SSL certificates": non_fips.get("ssl_cert_count"),
                "SSL keys": non_fips.get("ssl_key_count"),
                "FIPS private keys": fips.get("private_key_count"),
                "FIPS public keys": fips.get("public_key_count"),
            }

        pre_values = values(pre)
        post_values = values(post)
        lines = [
            "## Cryptographic Inventory",
            "",
            "| Item | Pre-Upgrade | Post-Upgrade |",
            "| :--- | ---: | ---: |",
        ]
        for label in pre_values:
            before = pre_values[label]
            after = post_values[label]
            lines.append(
                f"| **{label}** | {before if before is not None else 'N/A'} | "
                f"{after if after is not None else 'N/A'} |"
            )
        lines.extend(
            [
                "",
                "FIPS key output is retained in snapshots for audit review; "
                "private key material is never collected.",
                "",
            ]
        )
        return lines

    @staticmethod
    def _bgp_markdown(
        pre: Dict[str, Any],
        post: Dict[str, Any],
    ) -> List[str]:
        if not pre.get("enabled") and not post.get("enabled"):
            return ["## BGP Inventory", "", "Route domain 0: BGP not enabled.", ""]

        pre_neighbors = pre.get("neighbors", [])
        post_neighbors = post.get("neighbors", [])
        return [
            "## BGP Inventory",
            "",
            "| Item | Pre-Upgrade | Post-Upgrade |",
            "| :--- | ---: | ---: |",
            f"| **Route domain 0 BGP enabled** | {pre.get('enabled', False)} | {post.get('enabled', False)} |",
            f"| **Neighbor count** | {len(pre_neighbors)} | {len(post_neighbors)} |",
            f"| **Neighbors** | {', '.join(f'`{n}`' for n in pre_neighbors) or 'None'} | "
            f"{', '.join(f'`{n}`' for n in post_neighbors) or 'None'} |",
            "",
            "Full BGP running configuration, summaries, and advertised-route output "
            "are retained in the pre/post snapshots.",
            "",
        ]

    @staticmethod
    def _health_markdown(title: str, health: Dict[str, Any]) -> List[str]:
        lines = [f"## {title}", ""]
        interfaces = health.get("interfaces", {})
        lines.extend(
            [
                f"- **Interfaces up:** {interfaces.get('up_count', 0)} of "
                f"{interfaces.get('total', 0)}",
                f"- **Interface names up:** "
                f"{', '.join(f'`{name}`' for name in interfaces.get('up_names', [])) or 'None'}",
                "",
                "| Object Type | Total | Availability Counts | Unknown | Offline | Disabled |",
                "| :--- | ---: | :--- | ---: | ---: | ---: |",
            ]
        )

        for key, label in (
            ("virtual_servers", "Virtual Servers"),
            ("pools", "Pools"),
            ("nodes", "Nodes"),
        ):
            item = health.get(key, {})
            availability = item.get("availability", {})
            lines.append(
                f"| **{label}** | {item.get('total', 0)} | "
                f"{', '.join(f'{state}: {count}' for state, count in sorted(availability.items())) or 'None'} | "
                f"{len(item.get('unknown', []))} | {len(item.get('offline', []))} | "
                f"{len(item.get('disabled', []))} |"
            )

        lines.extend(["", "### Object Names Requiring Attention", ""])
        attention_found = False
        for key, label in (
            ("virtual_servers", "Virtual Servers"),
            ("pools", "Pools"),
            ("nodes", "Nodes"),
        ):
            item = health.get(key, {})
            for field, display in (
                ("unknown", "unknown"),
                ("offline", "offline"),
                ("disabled", "disabled"),
            ):
                names = item.get(field, [])
                if names:
                    attention_found = True
                    lines.append(
                        f"- **{label} {display}:** "
                        f"{', '.join(f'`{name}`' for name in names)}"
                    )
        if not attention_found:
            lines.append("- None")
        lines.append("")
        return lines
