from __future__ import annotations

# Compatibility shim so `f5upgrade.bigipclient` works while the implementation
# lives in `bigip_client.py` for readability.

from .bigip_client import BigIPClient  # re-export

__all__ = ["BigIPClient"]

