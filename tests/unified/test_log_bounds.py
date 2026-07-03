"""
Tests for the HAM-113 log-size bounds feature.

Covers the shipped config artifacts, the install-time wiring
(configure_log_bounds in hardware.sh, called from install.sh Phase 4), the
dry-run manifest operations, and the fleet-remediation script's --check mode.
"""

import os
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
FILES_DIR = REPO_ROOT / "files"
UNIFIED_DIR = REPO_ROOT / "unified_install"


class TestLogBoundsArtifacts:
    """The shipped config files exist and contain the expected caps."""

    def test_journald_dropin_present_and_bounded(self):
        conf = (FILES_DIR / "journald-sensor-bounds.conf").read_text()
        assert "[Journal]" in conf
        assert "SystemMaxUse=" in conf, "journald drop-in must cap SystemMaxUse"

    def test_hourly_logrotate_script_runs_logrotate(self):
        script = (FILES_DIR / "logrotate-hourly.sh").read_text()
        assert script.startswith("#!"), "cron script needs a shebang"
        assert "logrotate" in script, "hourly job must invoke logrotate"


class TestLogBoundsWiring:
    """configure_log_bounds is defined and invoked in the install flow."""

    def test_function_defined_in_hardware_lib(self):
        hardware = (UNIFIED_DIR / "lib" / "hardware.sh").read_text()
        assert "configure_log_bounds()" in hardware

    def test_function_called_in_phase4(self):
        install = (UNIFIED_DIR / "install.sh").read_text()
        assert "configure_log_bounds" in install, \
            "configure_log_bounds must be called from install.sh"


class TestLogBoundsManifest:
    """Running install.sh --dry-run through Phase 4 emits the log-bounds ops."""

    def _run_install_with_hardware(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        env = os.environ.copy()
        env["MANIFEST_FILE"] = str(manifest_file)
        env["HOME"] = str(tmp_path)
        env["USB_PATH"] = str(tmp_path / "usb")
        env["FILES_DIR"] = str(FILES_DIR)
        (tmp_path / "usb").mkdir(exist_ok=True)

        # Run the hardware phase (do NOT --skip-hardware) so configure_log_bounds runs.
        result = subprocess.run(
            ["bash", str(UNIFIED_DIR / "install.sh"),
             "05", "--wifi", "--dry-run",
             "--skip-packages", "--skip-brokkr", "--skip-extras"],
            capture_output=True, text=True, env=env, cwd=str(UNIFIED_DIR),
        )
        if not manifest_file.exists():
            pytest.fail(
                f"Manifest not created.\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return json.loads(manifest_file.read_text())["operations"]

    def test_manifest_caps_journald(self, tmp_path):
        ops = self._run_install_with_hardware(tmp_path)
        journald = [op for op in ops
                    if op.get("type") == "copy"
                    and op.get("dst", "").endswith("00-sensor-bounds.conf")]
        assert journald, f"No journald cap copy op. Ops: {[o.get('type') for o in ops]}"

    def test_manifest_adds_rsyslog_maxsize(self, tmp_path):
        ops = self._run_install_with_hardware(tmp_path)
        modify = [op for op in ops
                  if op.get("type") == "modify"
                  and op.get("path") == "/etc/logrotate.d/rsyslog"]
        assert modify, f"No rsyslog maxsize modify op. Ops: {[o.get('type') for o in ops]}"

    def test_manifest_installs_hourly_logrotate(self, tmp_path):
        ops = self._run_install_with_hardware(tmp_path)
        cron = [op for op in ops
                if op.get("type") == "copy"
                and op.get("dst", "").endswith("cron.hourly/mjolnir-logrotate")]
        assert cron, f"No hourly logrotate cron op. Ops: {[o.get('type') for o in ops]}"


class TestApplyLogBoundsCheck:
    """The fleet-remediation script's --check mode is read-only and exits 0."""

    def test_check_mode_runs_clean(self):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "apply_log_bounds.sh"), "--check"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"--check failed: {result.stderr}"
        assert "apply-log-bounds" in result.stdout

    def test_rejects_unknown_arg(self):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "apply_log_bounds.sh"), "--bogus"],
            capture_output=True, text=True,
        )
        assert result.returncode == 2, "unknown arg should exit 2"


class TestRsyslogBackupLocation:
    """Regression, found during the mj05 E2E: the rsyslog backup must NOT be
    written inside /etc/logrotate.d/ -- logrotate reads every file there, so a
    backup copy triggers 'duplicate log entry' errors for every log path."""

    def _sources(self):
        return [
            (UNIFIED_DIR / "lib" / "hardware.sh").read_text(),
            (REPO_ROOT / "scripts" / "apply_log_bounds.sh").read_text(),
        ]

    def test_backup_not_written_into_logrotate_dir(self):
        for text in self._sources():
            # the old buggy forms appended .mjolnir-orig to the in-dir path
            assert "${rsyslog_lr}.mjolnir-orig" not in text
            assert "${RSYSLOG_LR}.mjolnir-orig" not in text
            assert "/etc/logrotate.d/rsyslog.mjolnir-orig" not in text

    def test_backup_uses_var_backups(self):
        for text in self._sources():
            assert "/var/backups/logrotate-rsyslog.mjolnir-orig" in text
