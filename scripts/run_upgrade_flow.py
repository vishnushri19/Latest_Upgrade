from __future__ import annotations

from pathlib import Path

from f5upgrade.bigipclient import BigIPClient
from f5upgrade.config import load_settings
from f5upgrade.flow import UpgradeFlow, FlowOptions
from f5upgrade.report import to_report, write_json, write_markdown


def main() -> int:
    settings = load_settings()

    client = BigIPClient(
        host=settings.host,
        username=settings.username,
        password=settings.password,
        verify_tls=settings.verify_tls,
        timeout=settings.timeout,
    )

    flow = UpgradeFlow(
        client=client,
        options=FlowOptions(
            allow_risk_accepted=True,
            fail_fast=True,
            require_standby=True,
        ),
        settings=settings,
    )

    results = flow.run()

    report = to_report(
        results,
        meta={
            "bigip_host": settings.host,
            "mode": "flow",
        },
    )

    outdir = Path("outputs")
    outdir.mkdir(exist_ok=True)
    safe_host = settings.host.replace(":", "_").replace("/", "_")

    json_path = outdir / f"upgrade_flow_report_{safe_host}.json"
    md_path = outdir / f"upgrade_flow_report_{safe_host}.md"

    write_json(str(json_path), report)
    write_markdown(str(md_path), report)

    print(f"Wrote report: {json_path}")
    print(f"Wrote markdown: {md_path}")
    print(f"Overall status: {report['overall_status']}")
    print(f"Summary: {report['summary']}")

    return 0 if report["overall_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
