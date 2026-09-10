from __future__ import annotations

import os
import sys
from subprocess import run


def _sh(cmd: str) -> int:
    print(f"\n==> {cmd}")
    cp = run(cmd, shell=True)
    return cp.returncode


def main() -> int:
    """
    Orchestrate:
      1) Upgrade BIGIP_HOST (standby-first flow)
      2) Upgrade PEER_BIGIP_HOST
      3) Run HA recovery checks on original BIGIP_HOST
    """
    # Required env:
    #   BIGIP_HOST (current target), BIGIP_USER, BIGIP_PASS
    #   PEER_BIGIP_HOST (other node)
    peer = os.getenv("PEER_BIGIP_HOST", "").strip()
    if not peer:
        print("ERROR: set PEER_BIGIP_HOST to the other BIG-IP management IP.")
        return 2

    cur = os.environ.get("BIGIP_HOST", "").strip()
    if not cur:
        print("ERROR: BIGIP_HOST not set.")
        return 2

    # 1) Upgrade current host
    print(f"Upgrading current host: {cur}")
    rc = _sh("make flow")
    if rc != 0:
        return rc

    # 2) Upgrade peer host (swap BIGIP_HOST)
    print(f"\nSwitching BIGIP_HOST to peer: {peer}")
    os.environ["BIGIP_HOST"] = peer
    rc = _sh("make flow")
    if rc != 0:
        return rc

    # 3) HA recovery checks on original source host (switch back)
    print(f"\nSwitching BIGIP_HOST back to original: {cur}")
    os.environ["BIGIP_HOST"] = cur
    rc = _sh("make ha")
    if rc != 0:
        return rc

    print("\nDONE: flow (host) + flow (peer) + ha completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
