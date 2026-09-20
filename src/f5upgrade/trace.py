"""Small, operator-facing call trace for the upgrade workflow."""

from __future__ import annotations

from .report import CheckResult


def trace_call(caller: str, callee: str, action: str) -> None:
    """Identify the Python call that starts an operator-visible action."""
    print(f"\n[CALL] {caller}\n       -> {callee}\n       Action: {action}")


def trace_check(callee: str, result: CheckResult) -> None:
    """Report check outcome; detailed errors remain in the CRQ report."""
    print(f"[RESULT] {callee}: {result.id} {result.status} — {result.name}")
    if result.status == "FAIL":
        print(f"[ISSUE] {result.id}: See the upgrade flow report for details.")
