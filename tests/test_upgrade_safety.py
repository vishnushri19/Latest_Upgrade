from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from f5upgrade.execution import (  # noqa: E402
    VolumeState,
    _execution_role_allowed,
    check_image_present,
    check_license_dates,
    exec_install_standby,
    exec_upload_iso_standby,
)
from f5upgrade.flow import UpgradeFlow  # noqa: E402


def missing_volume(volume: str = "HD1.2") -> VolumeState:
    return VolumeState(
        found=False,
        volume=volume,
        source="test",
        error="not found",
    )


def installing_volume(volume: str = "HD1.2") -> VolumeState:
    return VolumeState(
        found=True,
        volume=volume,
        version="21.0.0",
        status="installing 1 pct",
        build="0.0.1",
        source="test",
        raw="HD1.2 BIG-IP 21.0.0 0.0.1 no installing 1 pct",
    )


class FakeInstallClient:
    host = "192.0.2.10"

    def __init__(self, rest_result=None, rest_error=None):
        self.rest_result = rest_result or {"commandResult": ""}
        self.rest_error = rest_error
        self.run_bash_once_calls = 0

    def run_bash_once(self, command: str, timeout: int):
        self.run_bash_once_calls += 1
        if self.rest_error is not None:
            raise self.rest_error
        return self.rest_result


class ExecutionRoleTests(unittest.TestCase):
    def test_ha_standby_is_allowed(self):
        self.assertTrue(_execution_role_allowed("standby", False))

    def test_ha_active_is_blocked(self):
        self.assertFalse(_execution_role_allowed("active", False))

    def test_verified_standalone_active_is_allowed(self):
        self.assertTrue(_execution_role_allowed("active", True))

    def test_unknown_role_is_blocked(self):
        self.assertFalse(_execution_role_allowed("", False))
        self.assertFalse(_execution_role_allowed("unknown", True))


class StandaloneTopologyTests(unittest.TestCase):
    def test_zero_device_inventory_does_not_enable_standalone(self):
        client = Mock()
        client.host = "192.0.2.20"
        client.system_version.return_value = {"version": "17.1.2.1"}
        client.devices.return_value = {"items": []}

        settings = SimpleNamespace(
            target_image_contains="21.0.0",
            username="admin",
        )
        flow = UpgradeFlow(client=client, settings=settings)

        output = io.StringIO()
        with (
            patch("f5upgrade.flow.get_failover_role", return_value="active"),
            patch("f5upgrade.flow.run_prechecks", return_value=[]),
            redirect_stdout(output),
        ):
            results = flow.preflight()

        self.assertFalse(flow._is_standalone)
        result_ids = {result.id for result in results}
        self.assertIn("FLOW-STANDALONE-001", result_ids)
        self.assertIn("FLOW-SKIP-002", result_ids)
        text = output.getvalue()
        self.assertIn("src/f5upgrade/bigip_client.py:BigIPClient.devices", text)
        self.assertIn("FLOW-SKIP-002 FAIL", text)
        self.assertIn("[ISSUE] FLOW-SKIP-002", text)


class ScpControlMasterTests(unittest.TestCase):
    def run_upload(self, ssh_control_path=None):
        digest = "a" * 64
        client = Mock()
        client.run_bash.return_value = {
            "commandResult": f"4\n{digest}  /shared/images/test.iso"
        }

        with (
            patch("f5upgrade.execution.get_failover_role", return_value="standby"),
            patch("f5upgrade.execution.os.path.isfile", return_value=True),
            patch("f5upgrade.execution.os.path.getsize", return_value=4),
            patch("f5upgrade.execution._sha256_file", return_value=digest),
            patch(
                "f5upgrade.execution.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as scp_run,
        ):
            result = exec_upload_iso_standby(
                client,
                host="192.0.2.10",
                scp_user="admin",
                iso_local_path="test.iso",
                ssh_control_path=ssh_control_path,
            )

        return result, scp_run.call_args.args[0]

    def test_scp_reuses_controlmaster_without_password_prompt(self):
        result, command = self.run_upload("control.sock")

        self.assertEqual("PASS", result.status)
        self.assertIn("ControlPath=control.sock", command)
        self.assertIn("ControlMaster=auto", command)
        self.assertIn("BatchMode=yes", command)
        self.assertTrue(result.details["reused_ssh_controlmaster"])

    def test_scp_without_control_path_preserves_original_command(self):
        result, command = self.run_upload()

        self.assertEqual("PASS", result.status)
        self.assertEqual(
            [
                "scp",
                "-O",
                "test.iso",
                "admin@192.0.2.10:/shared/images/",
            ],
            command,
        )
        self.assertFalse(result.details["reused_ssh_controlmaster"])


class LicenseValidationOutputTests(unittest.TestCase):
    def test_license_validation_prefers_existing_ssh_controlmaster(self):
        client = SimpleNamespace(host="192.0.2.10")
        responses = [
            {"commandResult": "/version_date"},
            {"commandResult": "20250115"},
            {"commandResult": "Service check date : 20260801"},
        ]
        output = io.StringIO()

        with (
            patch(
                "f5upgrade.execution._run_bash_via_ssh",
                side_effect=responses,
            ) as ssh_mock,
            patch("f5upgrade.execution._run_bash_with_retry") as rest_mock,
            redirect_stdout(output),
        ):
            result = check_license_dates(
                client,
                "BIGIP-17.5.1.9-0.0.12.iso",
                ssh_user="admin",
                ssh_control_path="control.sock",
            )

        self.assertEqual("PASS", result.status)
        self.assertEqual(3, ssh_mock.call_count)
        rest_mock.assert_not_called()
        self.assertIn(
            "Validation transport: authenticated SSH ControlMaster",
            output.getvalue(),
        )

    def test_license_pass_prints_dates_and_result(self):
        client = SimpleNamespace(host="192.0.2.10")
        responses = [
            {"commandResult": "/version_date"},
            {"commandResult": "20250115"},
            {"commandResult": "Service check date : 20260801"},
        ]
        output = io.StringIO()

        with (
            patch(
                "f5upgrade.execution._run_bash_with_retry",
                side_effect=responses,
            ),
            redirect_stdout(output),
        ):
            result = check_license_dates(
                client,
                "BIGIP-17.5.1.9-0.0.12.iso",
                ssh_user="admin",
            )

        text = output.getvalue()
        self.assertEqual("PASS", result.status)
        self.assertIn("[LIC-001] Validating ISO license-check date", text)
        self.assertIn("ISO license-check date: 20250115", text)
        self.assertIn("Device service-check date: 20260801", text)
        self.assertIn("License reactivation required: No", text)
        self.assertIn("[LIC-001] PASS", text)

    def test_license_failure_prints_reactivation_required(self):
        client = SimpleNamespace(host="192.0.2.10")
        responses = [
            {"commandResult": "/version_date"},
            {"commandResult": "20270115"},
            {"commandResult": "Service check date : 20260801"},
        ]
        output = io.StringIO()

        with (
            patch(
                "f5upgrade.execution._run_bash_with_retry",
                side_effect=responses,
            ),
            redirect_stdout(output),
        ):
            result = check_license_dates(
                client,
                "BIGIP-21.0.0-0.0.1.iso",
                ssh_user="admin",
            )

        text = output.getvalue()
        self.assertEqual("FAIL", result.status)
        self.assertIn("License reactivation required: Yes", text)
        self.assertIn("[LIC-001] FAIL", text)


class ImageValidationTransportTests(unittest.TestCase):
    def test_image_check_prefers_existing_ssh_controlmaster(self):
        client = Mock()
        client.host = "192.0.2.10"

        with (
            patch(
                "f5upgrade.execution._run_bash_via_ssh",
                return_value={
                    "commandResult": "BIGIP-17.5.1.9-0.0.12.iso\n"
                },
            ) as ssh_mock,
            patch("f5upgrade.execution._run_bash_with_retry") as rest_mock,
        ):
            result = check_image_present(
                client,
                "17.5.1.9",
                ssh_user="admin",
                ssh_control_path="control.sock",
            )

        self.assertTrue(result.found)
        client.software_images.assert_not_called()
        ssh_mock.assert_called_once()
        rest_mock.assert_not_called()

    def test_image_check_falls_back_when_controlmaster_fails(self):
        client = Mock()
        client.host = "192.0.2.10"

        with (
            patch(
                "f5upgrade.execution._run_bash_via_ssh",
                side_effect=RuntimeError("control socket unavailable"),
            ) as ssh_mock,
            patch(
                "f5upgrade.execution._run_bash_with_retry",
                return_value={
                    "commandResult": "BIGIP-17.5.1.9-0.0.12.iso\n"
                },
            ) as rest_mock,
        ):
            result = check_image_present(
                client,
                "17.5.1.9",
                ssh_user="admin",
                ssh_control_path="control.sock",
            )

        self.assertTrue(result.found)
        client.software_images.assert_not_called()
        ssh_mock.assert_called_once()
        rest_mock.assert_called_once()


class ConfirmationPromptTests(unittest.TestCase):
    def test_standalone_prompt_uses_standalone_wording(self):
        with (
            patch("f5upgrade.flow.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y") as input_mock,
        ):
            confirmed = UpgradeFlow._confirm_install(
                "BIGIP-17.5.1.9-0.0.12.iso",
                "HD1.2",
                allow_standalone=True,
            )

        self.assertTrue(confirmed)
        self.assertIn("standalone reboot", input_mock.call_args.args[0])

    def test_ha_prompt_retains_standby_wording(self):
        with (
            patch("f5upgrade.flow.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y") as input_mock,
        ):
            confirmed = UpgradeFlow._confirm_install(
                "BIGIP-17.5.1.9-0.0.12.iso",
                "HD1.2",
            )

        self.assertTrue(confirmed)
        self.assertIn("standby reboot", input_mock.call_args.args[0])


class InstallSubmissionTests(unittest.TestCase):
    image = "BIGIP-21.0.0-0.0.1.iso"
    volume = "HD1.2"

    def call_install(self, client, **kwargs):
        return exec_install_standby(
            client,
            self.image,
            self.volume,
            ssh_user=kwargs.get("ssh_user"),
            ssh_control_path=kwargs.get("ssh_control_path"),
            allow_standalone=kwargs.get("allow_standalone", False),
        )

    def test_successful_rest_submission_occurs_once(self):
        client = FakeInstallClient()
        with (
            patch("f5upgrade.execution.get_failover_role", return_value="standby"),
            patch("f5upgrade.execution._get_volume_state", return_value=missing_volume()),
        ):
            result = self.call_install(client)

        self.assertEqual("PASS", result.status)
        self.assertEqual(1, client.run_bash_once_calls)
        self.assertEqual("rest-single-attempt", result.details["submission_method"])

    def test_verified_standalone_can_submit_once(self):
        client = FakeInstallClient()
        with (
            patch("f5upgrade.execution.get_failover_role", return_value="active"),
            patch("f5upgrade.execution._get_volume_state", return_value=missing_volume()),
        ):
            result = self.call_install(client, allow_standalone=True)

        self.assertEqual("PASS", result.status)
        self.assertEqual(1, client.run_bash_once_calls)

    def test_ha_active_is_refused_before_submission(self):
        client = FakeInstallClient()
        with patch(
            "f5upgrade.execution.get_failover_role",
            return_value="active",
        ):
            result = self.call_install(client)

        self.assertEqual("FAIL", result.status)
        self.assertEqual(0, client.run_bash_once_calls)

    def test_timeout_with_installing_volume_does_not_submit_over_ssh(self):
        client = FakeInstallClient(
            rest_error=requests.exceptions.ReadTimeout("ambiguous timeout")
        )
        states = [missing_volume(), installing_volume()]

        with (
            patch("f5upgrade.execution.get_failover_role", return_value="standby"),
            patch("f5upgrade.execution._get_volume_state", side_effect=states),
            patch("f5upgrade.execution._run_bash_via_ssh") as ssh_submit,
            patch("f5upgrade.execution.time.sleep"),
        ):
            result = self.call_install(
                client,
                ssh_user="admin",
                ssh_control_path="control.sock",
            )

        self.assertEqual("PASS", result.status)
        self.assertEqual(1, client.run_bash_once_calls)
        ssh_submit.assert_not_called()
        self.assertFalse(result.details["automatic_resubmission"])

    def test_timeout_without_started_install_submits_ssh_once(self):
        client = FakeInstallClient(
            rest_error=requests.exceptions.ReadTimeout("ambiguous timeout")
        )

        with (
            patch("f5upgrade.execution.get_failover_role", return_value="standby"),
            patch("f5upgrade.execution._get_volume_state", return_value=missing_volume()),
            patch(
                "f5upgrade.execution._get_volume_state_via_ssh",
                return_value=missing_volume(),
            ),
            patch(
                "f5upgrade.execution._run_bash_via_ssh",
                return_value={"commandResult": ""},
            ) as ssh_submit,
            patch("f5upgrade.execution.time.sleep"),
        ):
            result = self.call_install(
                client,
                ssh_user="admin",
                ssh_control_path="control.sock",
            )

        self.assertEqual("PASS", result.status)
        self.assertEqual(1, client.run_bash_once_calls)
        self.assertEqual(1, ssh_submit.call_count)
        self.assertEqual(
            "ssh-after-state-verification",
            result.details["submission_method"],
        )

    def test_timeout_without_ssh_fails_without_resubmission(self):
        client = FakeInstallClient(
            rest_error=requests.exceptions.ReadTimeout("ambiguous timeout")
        )

        with (
            patch("f5upgrade.execution.get_failover_role", return_value="standby"),
            patch("f5upgrade.execution._get_volume_state", return_value=missing_volume()),
            patch("f5upgrade.execution._run_bash_via_ssh") as ssh_submit,
            patch("f5upgrade.execution.time.sleep"),
        ):
            result = self.call_install(client)

        self.assertEqual("FAIL", result.status)
        self.assertEqual(1, client.run_bash_once_calls)
        ssh_submit.assert_not_called()
        self.assertFalse(result.details["automatic_resubmission"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
