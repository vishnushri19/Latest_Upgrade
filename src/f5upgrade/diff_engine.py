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
        status = diff_result.get("overall_status", "UNKNOWN")

        badge = "🔴 **FAIL**" if status == "FAIL" else "🟢 **PASS**"

        lines = [
            f"# Pre vs Post Upgrade Difference Report",
            f"",
            f"- **Host:** `{host}`",
            f"- **Generated:** `{diff_result.get('generated_at')}`",
            f"- **Overall Result:** {badge}",
            f"- **Critical Regressions:** {crit}",
            f"- **Warnings / Changes:** {warn}",
            f"",
            f"## Inventory Counts",
            f"| Object Type | Pre-Upgrade Count | Post-Upgrade Count |",
            f"| :--- | :--- | :--- |",
            f"| **Virtual Servers** | {counts.get('pre_vs_count', 0)} | {counts.get('post_vs_count', 0)} |",
            f"| **Pools** | {counts.get('pre_pool_count', 0)} | {counts.get('post_pool_count', 0)} |",
            f"| **Nodes** | {counts.get('pre_node_count', 0)} | {counts.get('post_node_count', 0)} |",
            f"",
        ]

        # Critical Section
        all_diffs = (
            diff_result.get("virtual_servers", [])
            + diff_result.get("pools", [])
            + diff_result.get("nodes", [])
            + diff_result.get("sync_status", [])
            + diff_result.get("tmm_routes", [])
            + diff_result.get("mgmt_routes", [])
            + diff_result.get("interfaces", [])
        )

        crit_diffs = [d for d in all_diffs if d.get("severity") == "CRITICAL"]
        warn_diffs = [d for d in all_diffs if d.get("severity") == "WARNING"]

        if crit_diffs:
            lines.append("## 🔴 Critical Regressions Detected")
            lines.append("| Category | Object Name | Pre State | Post State | Details |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for d in crit_diffs:
                lines.append(f"| **{d.get('category', '').upper()}** | `{d.get('name')}` | `{d.get('pre_state')}` | `{d.get('post_state')}` | {d.get('details')} |")
            lines.append("")
        else:
            lines.append("## 🟢 No Critical Regressions Detected")
            lines.append("- All pre-existing virtual servers, active pool members, and nodes maintained healthy state.")
            lines.append("")

        if warn_diffs:
            lines.append("## 🟡 Warnings & Status Variations")
            lines.append("| Category | Object Name | Pre State | Post State | Details |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for d in warn_diffs:
                lines.append(f"| **{d.get('category', '').upper()}** | `{d.get('name')}` | `{d.get('pre_state')}` | `{d.get('post_state')}` | {d.get('details')} |")
            lines.append("")

        lines.append("## Notes")
        lines.append("- This report is generated automatically by comparing pre-upgrade and post-upgrade iControl REST snapshots.")
        lines.append("- Recheck any offline virtual servers or missing routes before releasing traffic.")
        lines.append("")

        return "\n".join(lines)
