from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List


@dataclass
class CheckResult:
    """
    Standardized result for any precheck/validation/execution step.

    status must be one of: PASS | FAIL | RISK_ACCEPTED
    """
    id: str
    category: str
    name: str
    status: str
    details: Dict[str, Any]


def to_report(results: List[CheckResult], meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a list of CheckResult objects into a single JSON-serializable report.
    """
    summary: Dict[str, int] = {"PASS": 0, "FAIL": 0, "RISK_ACCEPTED": 0}

    for r in results:
        if r.status not in summary:
            summary[r.status] = 0
        summary[r.status] += 1

    overall_status = "FAIL" if summary.get("FAIL", 0) > 0 else "PASS"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "summary": summary,
        "overall_status": overall_status,
        "results": [asdict(r) for r in results],
    }


def write_json(path: str, payload: Dict[str, Any]) -> None:
    """
    Write a report payload to disk as pretty JSON.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def write_markdown(path: str, payload: Dict[str, Any]) -> None:
    """
    Write a simple human-readable Markdown summary of a report.
    """
    meta = payload.get("meta", {})
    summary = payload.get("summary", {})
    overall = payload.get("overall_status", "UNKNOWN")
    results = payload.get("results", [])

    lines: List[str] = []

    lines.append("# Report Summary")
    lines.append("")
    lines.append(f"- **Overall status:** {overall}")
    lines.append(f"- **PASS:** {summary.get('PASS', 0)}")
    lines.append(f"- **FAIL:** {summary.get('FAIL', 0)}")
    lines.append(f"- **RISK_ACCEPTED:** {summary.get('RISK_ACCEPTED', 0)}")
    lines.append("")

    if meta:
        lines.append("## Metadata")
        for k, v in meta.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    lines.append("## Results")
    for r in results:
        rid = r.get("id", "")
        name = r.get("name", "")
        category = r.get("category", "")
        status = r.get("status", "")
        lines.append(f"- **[{status}] {rid}** — {category}: {name}")
    lines.append("")

    lines.append("## Notes")
    lines.append(
        "- This report is intended for lab validation and architectural verification."
    )
    lines.append(
        "- Sanitize any sensitive information before sharing publicly."
    )
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
