from __future__ import annotations

from pathlib import Path

from f5upgrade.bigip_client import BigIPClient
from f5upgrade.checks import run_prechecks
from f5upgrade.config import load_settings
from f5upgrade.report import to_report, write_json, write_markdown


def main() -> int:
    cfg = load_settings()

    client = BigIPClient(
        host=cfg.host,
        username=cfg.username,
        password=cfg.password,
        verify_tls=cfg.verify_tls,
        timeout=cfg.timeout,
    )

    results = run_prechecks(client)
    report = to_report(results, meta={"bigip_host": cfg.host, "mode": "prechecks"})

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    safe_host = cfg.host.replace(":", "_").replace("/", "_")
    out_path = out_dir / f"precheck_report_{safe_host}.json"
    md_path = out_dir / f"precheck_report_{safe_host}.md"

    write_json(str(out_path), report)
    write_markdown(str(md_path), report)

    print(f"Wrote report: {out_path}")
    print(f"Wrote markdown: {md_path}")
    print(f"Overall status: {report['overall_status']}")
    print(f"Summary: {report['summary']}")

    return 0 if report["overall_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

