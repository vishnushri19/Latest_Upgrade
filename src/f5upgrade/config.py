from __future__ import annotations

import getpass
import os
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # BIG-IP credentials/target
    host: str
    username: str
    password: str
    verify_tls: bool = False
    timeout: int = 20
    crq_number: str = ""

    # Upgrade intent
    target_image_contains: str = "21.0.0.1"
    target_volume: str = "HD1.2"

    # Automation
    auto_upload_iso: bool = False
    iso_local_path: str = ""
    base_iso_local_path: str = ""
    hotfix_iso_local_path: str = ""
    scp_user: str = ""  # default to username if empty


def _req(name: str) -> str:
    """Read a required env var or raise with a clear message."""
    v = os.getenv(name, "").strip()
    if not v:
        raise ValueError(f"Missing required env var: {name}")
    return v


def _get_password() -> str:
    """
    Read password from BIGIP_PASS environment variable.
    If not set, securely prompts the operator using hidden input (getpass).
    """
    p = os.getenv("BIGIP_PASS", "").strip()
    if not p:
        if sys.stdin.isatty():
            try:
                p = getpass.getpass("Enter BIG-IP Password: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise ValueError("Password prompt was cancelled.")
        else:
            raise ValueError(
                "Missing required env var: BIGIP_PASS (required in non-interactive mode)."
            )
    if not p:
        raise ValueError("Password cannot be empty.")
    return p


def load_settings() -> Settings:
    """
    Load upgrade settings from environment variables and secure prompts.

    Required:
      - BIGIP_HOST
      - BIGIP_USER
      - CRQ_NUMBER
      - BIGIP_PASS (if not set in env, will prompt securely via getpass)

    Optional:
      - VERIFY_TLS               (0/1/true/yes)
      - BIGIP_TIMEOUT            (seconds, default 20)
      - TARGET_IMAGE_CONTAINS    (substring of image name/version)
      - TARGET_VOLUME            (e.g. HD1.2)
      - AUTO_UPLOAD_ISO          (0/1/true/yes)
      - ISO_LOCAL_PATH           (path to ISO on local machine)
      - BASE_ISO_LOCAL_PATH      (optional base ISO for combined EHF install)
      - HOTFIX_ISO_LOCAL_PATH    (optional EHF ISO for combined install)
      - SCP_USER                 (defaults to BIGIP_USER if empty)
      - CRQ_NUMBER               (safe folder name under outputs/)
    """
    # Required
    host = _req("BIGIP_HOST")
    username = _req("BIGIP_USER")
    crq_number = _req("CRQ_NUMBER")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", crq_number):
        raise ValueError(
            "CRQ_NUMBER may contain only letters, numbers, hyphens, and underscores."
        )
    password = _get_password()

    # Optional / with defaults
    verify_tls = os.getenv("VERIFY_TLS", "0").strip().lower() in ("1", "true", "yes")

    timeout_raw = os.getenv("BIGIP_TIMEOUT", "20").strip() or "20"
    try:
        timeout = int(timeout_raw)
    except ValueError:
        raise ValueError(f"Invalid BIGIP_TIMEOUT value: {timeout_raw!r}")

    target_image_contains = (
        os.getenv("TARGET_IMAGE_CONTAINS", "21.0.0.1").strip() or "21.0.0.1"
    )
    target_volume = os.getenv("TARGET_VOLUME", "HD1.2").strip() or "HD1.2"

    auto_upload_iso = os.getenv("AUTO_UPLOAD_ISO", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    iso_local_path = os.getenv("ISO_LOCAL_PATH", "").strip()
    base_iso_local_path = os.getenv("BASE_ISO_LOCAL_PATH", "").strip()
    hotfix_iso_local_path = os.getenv("HOTFIX_ISO_LOCAL_PATH", "").strip()

    scp_user = os.getenv("SCP_USER", "").strip() or username

    return Settings(
        host=host,
        username=username,
        password=password,
        verify_tls=verify_tls,
        timeout=timeout,
        crq_number=crq_number,
        target_image_contains=target_image_contains,
        target_volume=target_volume,
        auto_upload_iso=auto_upload_iso,
        iso_local_path=iso_local_path,
        base_iso_local_path=base_iso_local_path,
        hotfix_iso_local_path=hotfix_iso_local_path,
        scp_user=scp_user,
    )
