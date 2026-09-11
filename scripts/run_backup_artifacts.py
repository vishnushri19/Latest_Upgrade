from __future__ import annotations

import os
import posixpath
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from f5upgrade.config import load_settings


SSH_TIMEOUT = 30
BACKUP_TIMEOUT = 1800
COPY_TIMEOUT = 1800


def _safe_host(host: str) -> str:
    """Sanitize a host string for use in local filenames."""
    return (
        host.replace(":", "_")
        .replace("/", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as a human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f}s"

    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f"{minutes}m {remaining:.1f}s"


def _extract_files(output: str) -> List[str]:
    """
    Extract absolute paths after the __FILES__ marker.

    Only regular-looking absolute paths are collected. Duplicate paths are
    removed while preserving order.
    """
    files: List[str] = []
    capture = False

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if line == "__FILES__":
            capture = True
            continue

        if capture and line.startswith("/"):
            files.append(line)

    return list(dict.fromkeys(files))


def _remote_shell_quote(value: str) -> str:
    """
    Quote a value for the BIG-IP remote shell.

    The backup name is locally generated, but remote paths and passphrases
    should still be quoted before being placed into shell commands.
    """
    return shlex.quote(value)


def _is_protected_export_path(remote_file: str) -> bool:
    """
    Return True for BIG-IP paths subject to the unencrypted-export check.
    """
    return (
        remote_file.startswith("/var/local/ucs/")
        or remote_file.startswith("/var/local/scf/")
    )


class SSHSession:
    """
    Shared SSH connection using OpenSSH ControlMaster.

    SCP uses uppercase -O to force the legacy SCP protocol. This is required
    for compatibility with newer macOS OpenSSH clients and BIG-IP.
    """

    def __init__(
        self,
        user: str,
        host: str,
        persist_minutes: int = 30,
    ) -> None:
        self.user = user
        self.host = host
        self.target = f"{user}@{host}"

        self.control_path = (
            f"/tmp/f5upgrade-{os.getpid()}-{_safe_host(host)}.sock"
        )

        self.persist = f"{persist_minutes}m"
        self.is_open = False

    def _base_opts(self) -> List[str]:
        """Options shared by SSH and SCP."""
        return [
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            "ControlMaster=auto",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]

    def open(self) -> None:
        """Open the shared SSH master connection."""
        print(
            "\nOpening shared SSH connection. "
            "You should be prompted for the password once."
        )

        command = [
            "ssh",
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPersist={self.persist}",
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            self.target,
            "true",
        ]

        try:
            completed = subprocess.run(command)

            if completed.returncode == 0:
                self.is_open = True
            else:
                print(
                    "Warning: Shared SSH connection failed. "
                    "Individual operations may prompt for the password."
                )

        except Exception as exc:
            print(f"Warning: Could not open shared SSH connection: {exc}")

    def run(
        self,
        command: str,
        timeout_sec: int,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command over SSH."""
        ssh_command = [
            "ssh",
            *self._base_opts(),
            self.target,
            command,
        ]

        return subprocess.run(
            ssh_command,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )

    def scp_from(
        self,
        remote_file: str,
        local_dir: Path,
        timeout_sec: int,
    ) -> subprocess.CompletedProcess[str]:
        """
        Copy a remote file to the local system.

        -O is uppercase letter O and forces legacy SCP instead of SFTP.
        """
        local_dir.mkdir(parents=True, exist_ok=True)

        remote_spec = f"{self.target}:{remote_file}"

        scp_command = [
            "scp",
            "-O",
            *self._base_opts(),
            remote_spec,
            str(local_dir),
        ]

        return subprocess.run(
            scp_command,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )

    def close(self) -> None:
        """Close the shared SSH master connection."""
        if not self.is_open:
            return

        command = [
            "ssh",
            *self._base_opts(),
            "-O",
            "exit",
            self.target,
        ]

        try:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=SSH_TIMEOUT,
            )
        finally:
            self.is_open = False


def _run_backup_task(
    session: SSHSession,
    name: str,
    command: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Run one remote backup task and collect its result."""
    print(f"\nStarting {name}...")

    start = time.monotonic()
    status = "FAIL"
    output = ""
    error = ""

    try:
        completed = session.run(
            command,
            timeout_sec=timeout_sec,
        )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        output = stdout

        if stderr:
            output += f"\n{stderr}"

        output = output.strip()

        if completed.returncode == 0:
            status = "PASS"
        else:
            error = (
                f"Command exited with return code "
                f"{completed.returncode}. Output:\n{output}"
            )

    except subprocess.TimeoutExpired:
        error = f"TimeoutExpired after {timeout_sec} seconds"

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - start
    remote_files = _extract_files(output)

    print(
        f"Completed {name}: {status} "
        f"in {_fmt_elapsed(elapsed)}"
    )

    if error:
        print(f"Error: {error}")

    return {
        "name": name,
        "status": status,
        "elapsed_human": _fmt_elapsed(elapsed),
        "remote_files": remote_files,
        "output": output,
        "error": error,
    }


def _stage_file_for_export(
    session: SSHSession,
    remote_file: str,
    backup_name: str,
) -> Tuple[Optional[str], str]:
    """
    Copy a protected UCS/SCF file to /var/tmp.

    BIG-IP's FIPS/Common Criteria export check applies to the original
    /var/local/ucs and /var/local/scf directories. The staged copy is placed
    directly under /var/tmp so it can be downloaded without encryption.
    """
    basename = posixpath.basename(remote_file)

    if not basename:
        return None, "Unable to determine the remote filename."

    stage_name = (
        f"f5upgrade-{backup_name}-{basename}"
    )

    staged_file = f"/var/tmp/{stage_name}"

    source_quoted = _remote_shell_quote(remote_file)
    staged_quoted = _remote_shell_quote(staged_file)

    command = f"cp {source_quoted} {staged_quoted}"

    print(f"Staging protected artifact to {staged_file}...")

    try:
        completed = session.run(
            command,
            timeout_sec=COPY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"Staging timed out after {COPY_TIMEOUT} seconds."
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()

        details = stderr or stdout or "No error text returned."
        return None, (
            f"Remote staging failed with exit code "
            f"{completed.returncode}: {details}"
        )

    return staged_file, ""


def _remove_remote_file(
    session: SSHSession,
    remote_file: str,
) -> None:
    """Remove a temporary staged file from /var/tmp."""
    command = f"rm -f {_remote_shell_quote(remote_file)}"

    try:
        completed = session.run(
            command,
            timeout_sec=SSH_TIMEOUT,
        )

        if completed.returncode != 0:
            details = (
                (completed.stderr or "").strip()
                or (completed.stdout or "").strip()
            )
            print(
                f"Warning: Could not remove temporary file "
                f"{remote_file}: {details}"
            )

    except Exception as exc:
        print(
            f"Warning: Exception removing temporary file "
            f"{remote_file}: {exc}"
        )


def _copy_artifact(
    session: SSHSession,
    remote_file: str,
    local_dir: Path,
    backup_name: str,
) -> bool:
    """
    Copy one artifact to the local system.

    Protected UCS/SCF files are staged in /var/tmp first. QKView files already
    in /var/tmp are copied directly.
    """
    staged_file: Optional[str] = None
    source_for_scp = remote_file

    try:
        if _is_protected_export_path(remote_file):
            staged_file, stage_error = _stage_file_for_export(
                session=session,
                remote_file=remote_file,
                backup_name=backup_name,
            )

            if not staged_file:
                print(
                    f"Copy FAIL: {remote_file}\n"
                    f"  {stage_error}"
                )
                return False

            source_for_scp = staged_file

        print(f"Copying: {remote_file}")

        start = time.monotonic()

        try:
            completed = session.scp_from(
                remote_file=source_for_scp,
                local_dir=local_dir,
                timeout_sec=COPY_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(
                f"Copy FAIL: {remote_file}\n"
                f"  SCP timed out after {COPY_TIMEOUT} seconds."
            )
            return False
        except Exception as exc:
            print(
                f"Copy FAIL: {remote_file}\n"
                f"  {type(exc).__name__}: {exc}"
            )
            return False

        elapsed = time.monotonic() - start

        if completed.returncode == 0:
            print(
                f"Copy PASS in {_fmt_elapsed(elapsed)}: "
                f"{remote_file}"
            )
            return True

        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()

        print(
            f"Copy FAIL in {_fmt_elapsed(elapsed)}: "
            f"{remote_file}"
        )
        print(
            f"  SCP exited with code {completed.returncode}."
        )

        if stderr:
            print(f"  Stderr: {stderr}")

        if stdout:
            print(f"  Stdout: {stdout}")

        return False

    finally:
        if staged_file:
            print(f"Removing temporary staged file: {staged_file}")
            _remove_remote_file(session, staged_file)


def main() -> int:
    cfg = load_settings()

    safe_host = _safe_host(cfg.host)
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    output_dir = (
        Path("outputs") / cfg.crq_number
        / "evidence"
        / f"backup_{safe_host}_{run_timestamp}"
    )

    artifact_copy_dir = output_dir / "copied_artifacts"

    session = SSHSession(
        user=cfg.scp_user,
        host=cfg.host,
    )

    print("=" * 80)
    print("BIG-IP Backup Artifact Creation")
    print("=" * 80)
    print(f"Host: {cfg.host}")
    print(f"SSH User: {cfg.scp_user}")
    print(f"Output Directory: {output_dir}")
    print("=" * 80)

    try:
        session.open()

        hostname_result = session.run(
            "echo $HOSTNAME",
            timeout_sec=SSH_TIMEOUT,
        )

        hostname = (
            hostname_result.stdout or ""
        ).strip()

        if not hostname:
            hostname = safe_host

        short_host = hostname.split(".")[0].strip()

        if not short_host:
            short_host = safe_host

        short_host = _safe_host(short_host)
        backup_name = f"{short_host}-{run_timestamp}"

        print(f"BIG-IP hostname: {hostname}")
        print(f"Backup base name: {backup_name}")

        commands = [
            (
                "UCS Backup",
                (
                    f"tmsh save sys ucs {backup_name}; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"ls -1 "
                    f"/var/local/ucs/{backup_name}.ucs"
                ),
            ),
            (
                "SCF Backup",
                (
                    f"tmsh save sys config file {backup_name} "
                    f"no-passphrase; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/local/scf "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-name {_remote_shell_quote(backup_name + '*')} "
                    f"-print"
                ),
            ),
            (
                "QKView",
                (
                    f"marker=/var/tmp/{backup_name}.qk.marker; "
                    f"touch \"$marker\"; "
                    f"qkview -s0; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/tmp "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-iname '*qkview*' "
                    f"-newer \"$marker\" "
                    f"-print; "
                    f"rm -f \"$marker\""
                ),
            ),
            (
                "ASMQKView",
                (
                    f"marker=/var/tmp/{backup_name}.asm.marker; "
                    f"touch \"$marker\"; "
                    f"asmqkview; "
                    f"echo; "
                    f"echo __FILES__; "
                    f"find /var/tmp "
                    f"-maxdepth 1 "
                    f"-type f "
                    f"-iname '*.qkview' "
                    f"-newer \"$marker\" "
                    f"-print; "
                    f"rm -f \"$marker\""
                ),
            ),
        ]

        backup_results: List[Dict[str, Any]] = []
        all_remote_files: List[str] = []
        has_backup_failures = False

        for name, command in commands:
            result = _run_backup_task(
                session=session,
                name=name,
                command=command,
                timeout_sec=BACKUP_TIMEOUT,
            )

            backup_results.append(result)

            if result["status"] == "FAIL":
                has_backup_failures = True

            all_remote_files.extend(
                result.get("remote_files", [])
            )

        all_remote_files = list(
            dict.fromkeys(all_remote_files)
        )

        print("\n" + "=" * 80)
        print("Backup Creation Summary")
        print("=" * 80)

        for result in backup_results:
            print(
                f"{result['name']}: "
                f"{result['status']} "
                f"({result['elapsed_human']})"
            )

        if not all_remote_files:
            print(
                "\nNo remote artifacts were detected. "
                "Nothing to copy."
            )
            return 2 if has_backup_failures else 0

        print("\nRemote artifact files detected:")

        for remote_file in all_remote_files:
            print(f" - {remote_file}")

        try:
            answer = input(
                "\nDo you want to copy these files to the "
                "local system now? (y/N): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer not in ("y", "yes"):
            print("\nCopy skipped by user.")
            return 2 if has_backup_failures else 0

        print("\nStarting file copy via legacy SCP protocol...")
        print(
            "Protected UCS/SCF files will be staged temporarily "
            "under /var/tmp."
        )

        has_copy_failures = False

        for remote_file in all_remote_files:
            copied = _copy_artifact(
                session=session,
                remote_file=remote_file,
                local_dir=artifact_copy_dir,
                backup_name=backup_name,
            )

            if not copied:
                has_copy_failures = True

        print(
            f"\nCopied artifacts directory: "
            f"{artifact_copy_dir.resolve()}"
        )

        if has_copy_failures:
            return 3

        if has_backup_failures:
            return 2

        return 0

    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
