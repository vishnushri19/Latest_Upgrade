from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests
import urllib3
from requests.auth import HTTPBasicAuth


class BigIPClient:
    """
    Minimal BIG-IP iControl REST client.

    This client intentionally exposes only a small set of helpers that the
    upgrade flows use. For anything else, prefer adding a thin wrapper here
    instead of calling requests directly in other modules.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        self.host = host
        self.base_url = f"https://{host}"
        self.auth = HTTPBasicAuth(username, password)
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.max_retries = max_retries
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """
        Execute an HTTP request with retry logic for transient timeouts and connection errors.
        """
        eff_timeout = timeout if timeout is not None else self.timeout
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    auth=self.auth,
                    verify=self.verify_tls,
                    timeout=eff_timeout,
                    **kwargs,
                )
                resp.raise_for_status()
                return resp
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    backoff = attempt * 3
                    print(
                        f"\n[i] BIG-IP REST API busy/slow ({type(exc).__name__}). "
                        f"Retrying in {backoff}s (attempt {attempt}/{self.max_retries})..."
                    )
                    time.sleep(backoff)
                else:
                    raise exc
            except requests.exceptions.HTTPError as exc:
                # Retry 502/503/504 Service Unavailable (often seen when restjavad restarts or GC runs)
                if exc.response is not None and exc.response.status_code in (502, 503, 504):
                    last_exc = exc
                    if attempt < self.max_retries:
                        backoff = attempt * 4
                        print(
                            f"\n[i] BIG-IP REST API returned HTTP {exc.response.status_code}. "
                            f"Retrying in {backoff}s (attempt {attempt}/{self.max_retries})..."
                        )
                        time.sleep(backoff)
                        continue
                raise exc

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Request failed after {self.max_retries} attempts.")

    def get(self, path: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Perform a GET against an iControl REST path (starting with '/mgmt/...').
        """
        url = self.base_url + path
        resp = self._request_with_retry("GET", url, timeout=timeout)
        return resp.json()

    def post(self, path: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Perform a POST against an iControl REST path (starting with '/mgmt/...').
        """
        url = self.base_url + path
        resp = self._request_with_retry("POST", url, timeout=timeout, json=payload)
        return resp.json()

    def patch(self, path: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Partially update an iControl REST resource.
        """
        url = self.base_url + path
        resp = self._request_with_retry("PATCH", url, timeout=timeout, json=payload)
        return resp.json()

    def run_bash(self, command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute a shell command via iControl.

        Note:
          - This is powerful; only use for controlled commands (tmsh/ls/show).
          - Endpoint: /mgmt/tm/util/bash
        """
        payload = {
            "command": "run",
            "utilCmdArgs": f"-c '{command}'",
        }
        return self.post("/mgmt/tm/util/bash", payload, timeout=timeout)

    # ---------------- Discovery ----------------

    def devices(self) -> Dict[str, Any]:
        """
        List devices in the trust domain.
        """
        return self.get("/mgmt/tm/cm/device")

    # ---------------- Platform / HA ----------------

    def system_version(self) -> Dict[str, Any]:
        """
        Get BIG-IP software version information.
        """
        return self.get("/mgmt/tm/sys/version")

    def failover_state(self) -> Dict[str, Any]:
        """
        Get failover-status (ACTIVE/STANDBY, etc).
        """
        return self.get("/mgmt/tm/cm/failover-status")

    def sync_status(self) -> Dict[str, Any]:
        """
        Get config-sync status.
        """
        return self.get("/mgmt/tm/cm/sync-status")

    # ---------------- Software / Rollback ----------------

    def software_images(self) -> Dict[str, Any]:
        """
        List software images known to the system.
        """
        return self.get("/mgmt/tm/sys/software/image")

    def software_volumes(self) -> Dict[str, Any]:
        """
        List software volumes.
        """
        return self.get("/mgmt/tm/sys/software/volume")

    # ---------------- Licensing ----------------

    def license_info(self) -> Dict[str, Any]:
        """
        Get license information.
        """
        return self.get("/mgmt/tm/sys/license")
