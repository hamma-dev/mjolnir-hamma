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
LIB_DIR = UNIFIED_DIR / "lib"


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
            (LIB_DIR / "log_bounds.sh").read_text(),
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
        # The path is defined once, in the shared lib.
        lib = (LIB_DIR / "log_bounds.sh").read_text()
        assert "/var/backups/logrotate-rsyslog.mjolnir-orig" in lib


class TestSingleImplementation:
    """The apply logic must exist in exactly ONE place (lib/log_bounds.sh).

    Before this refactor, configure_log_bounds() in hardware.sh and
    scripts/apply_log_bounds.sh were parallel implementations of the same three
    operations -- same paths, same sed, same backup logic, duplicated comments.
    These tests fail if the duplication is reintroduced.
    """

    def _lib(self):
        return (LIB_DIR / "log_bounds.sh").read_text()

    def _callers(self):
        return {
            "hardware.sh": (UNIFIED_DIR / "lib" / "hardware.sh").read_text(),
            "apply_log_bounds.sh": (REPO_ROOT / "scripts" / "apply_log_bounds.sh").read_text(),
        }

    def test_shared_lib_exists_and_defines_the_operations(self):
        lib = self._lib()
        for fn in ("log_bounds_apply_journald",
                   "log_bounds_apply_rsyslog",
                   "log_bounds_apply_cron"):
            assert f"{fn}()" in lib, f"{fn} must be defined in lib/log_bounds.sh"

    def test_both_callers_source_the_shared_lib(self):
        for name, text in self._callers().items():
            assert "log_bounds.sh" in text, f"{name} must source lib/log_bounds.sh"

    def test_callers_do_not_reimplement_the_sed(self):
        """The maxsize injection is the sharpest duplication marker."""
        for name, text in self._callers().items():
            assert "sed -i" not in text or "maxsize" not in text, (
                f"{name} appears to reimplement the maxsize sed; "
                "it must call log_bounds_apply_rsyslog instead")

    def test_callers_do_not_hardcode_the_managed_paths(self):
        """Paths are defined once in the lib and referenced by variable."""
        managed = [
            "/etc/systemd/journald.conf.d/00-sensor-bounds.conf",
            "/etc/cron.hourly/mjolnir-logrotate",
        ]
        for name, text in self._callers().items():
            for path in managed:
                assert path not in text, (
                    f"{name} hardcodes {path}; use the LOG_BOUNDS_* variable")

    def test_status_helpers_available_for_verify_deployment(self):
        """verify_deployment.sh needs a read-only check it can call."""
        lib = self._lib()
        assert "log_bounds_all_present()" in lib
        for fn in ("log_bounds_status_journald",
                   "log_bounds_status_rsyslog",
                   "log_bounds_status_cron"):
            assert f"{fn}()" in lib


class TestJournaldPersistence:
    """A cap on a RAM-only journal bounds nothing and loses history (HAM-216).

    mj03 had the drop-in but no /var/log/journal, so the stock Storage=auto ran
    journald in RAM: SystemMaxUse was inert and every reboot erased the logs,
    while `apply_log_bounds.sh --check` reported "All bounds present".
    """

    def test_dropin_pins_persistent_storage(self):
        """Parse the directive: the comment above it also says Storage=persistent."""
        section, storage = None, []
        for line in (FILES_DIR / "journald-sensor-bounds.conf").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
            elif section == "[Journal]" and line.split("=", 1)[0].strip() == "Storage":
                storage.append(line.split("=", 1)[1].strip())
        assert storage == ["persistent"], (
            "Storage=auto silently falls back to RAM when /var/log/journal "
            "is missing; pin persistent under [Journal]")

    def test_default_journal_locations(self):
        script = (f'source "{LIB_DIR / "log_bounds.sh"}"\n'
                  'echo "$LOG_BOUNDS_JOURNAL_DIR $LOG_BOUNDS_RUNTIME_JOURNAL_DIR"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, timeout=30).stdout.split()
        assert out == ["/var/log/journal", "/run/log/journal"]

    def _status(self, tmp_path, dropin=True, journal_dir=True,
                runtime_dir=False, fn="log_bounds_status_journald"):
        dst = tmp_path / "00-sensor-bounds.conf"
        jdir = tmp_path / "journal"
        rdir = tmp_path / "run-journal"
        if runtime_dir:
            rdir.mkdir()
        lr = tmp_path / "logrotate-rsyslog"
        cron = tmp_path / "mjolnir-logrotate"
        if dropin:
            dst.write_text("[Journal]\n")
        if journal_dir:
            jdir.mkdir()
        lr.write_text("maxsize 100M\n")
        cron.write_text("#!/bin/sh\n")
        script = (
            f'source "{LIB_DIR / "log_bounds.sh"}"\n'
            f'LOG_BOUNDS_JOURNALD_DST="{dst}"\n'
            f'LOG_BOUNDS_JOURNAL_DIR="{jdir}"\n'
            f'LOG_BOUNDS_RUNTIME_JOURNAL_DIR="{rdir}"\n'
            f'LOG_BOUNDS_RSYSLOG_LR="{lr}"\n'
            f'LOG_BOUNDS_CRON_DST="{cron}"\n'
            f'{fn} && echo "RC=0" || echo "RC=1"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, timeout=30)
        return result.stdout

    def test_status_present_when_dropin_and_journal_dir_exist(self, tmp_path):
        assert self._status(tmp_path).startswith("PRESENT")

    def test_status_missing_without_dropin(self, tmp_path):
        assert self._status(tmp_path, dropin=False).startswith("MISSING")

    def test_status_ram_only_without_journal_dir(self, tmp_path):
        """The mj03 state must not read as PRESENT."""
        assert self._status(tmp_path, journal_dir=False).startswith("RAM-ONLY")

    def test_all_present_fails_for_ram_only_journal(self, tmp_path):
        out = self._status(tmp_path, journal_dir=False,
                           fn="log_bounds_all_present")
        assert "RC=1" in out

    def test_all_present_passes_when_everything_is_in_place(self, tmp_path):
        out = self._status(tmp_path, fn="log_bounds_all_present")
        assert "RC=0" in out

    def test_status_missing_with_neither_dropin_nor_journal_dir(self, tmp_path):
        """A fresh unit: RAM-ONLY here would claim the drop-in is installed."""
        out = self._status(tmp_path, dropin=False, journal_dir=False)
        assert out.startswith("MISSING")

    def test_status_ram_only_while_a_runtime_journal_exists(self, tmp_path):
        """/var/log/journal existing is not proof: journald may still be writing
        to /run (dir made but never flushed, or Storage=volatile elsewhere)."""
        out = self._status(tmp_path, runtime_dir=True)
        assert out.startswith("RAM-ONLY")


class TestApplyJournaldMakesItPersistent:
    """The apply path must reproduce the mj03 hand fix (HAM-216): journal dir
    with the systemd-journal group/ACLs, restart, then flush RAM to disk.
    A restart alone leaves the current boot's logs in /run (lost at reboot)."""

    def _apply(self, tmp_path, failing=""):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "sudo.log"
        # Fake sudo: record the command, let the two journal tools chatter on
        # stdout, and fail for the command named in $FAILING.
        (bindir / "sudo").write_text(
            '#!/bin/bash\n'
            f'echo "$*" >> "{log}"\n'
            '[[ "$1" == journalctl || "$1" == systemd-tmpfiles ]] && echo "noise from $1"\n'
            '[[ -n "$FAILING" && "$1" == "$FAILING" ]] && exit 1\n'
            'exit 0\n')
        (bindir / "sudo").chmod(0o755)
        script = (f'source "{LIB_DIR / "log_bounds.sh"}"\n'
                  'log_bounds_apply_journald; echo "RC=$?"\n')
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   FAILING=failing)
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, timeout=30, env=env).stdout
        return out, log.read_text().splitlines()

    @staticmethod
    def _index(cmds, needle):
        hits = [i for i, c in enumerate(cmds) if needle in c]
        assert hits, f"{needle!r} never run; sudo saw {cmds}"
        return hits[0]

    def test_result_token_is_clean(self, tmp_path):
        """apply_log_bounds.sh matches the captured stdout against APPLIED."""
        out, _ = self._apply(tmp_path)
        assert out.splitlines() == ["APPLIED", "RC=0"]

    def test_journal_dir_prepared_before_restart(self, tmp_path):
        _, cmds = self._apply(tmp_path)
        mk = self._index(cmds, "mkdir -p /var/log/journal")
        tmpf = self._index(cmds, "systemd-tmpfiles --create --prefix /var/log/journal")
        restart = self._index(cmds, "systemctl restart systemd-journald")
        assert mk < tmpf < restart

    def test_flushes_runtime_journal_after_restart(self, tmp_path):
        _, cmds = self._apply(tmp_path)
        restart = self._index(cmds, "systemctl restart systemd-journald")
        flush = self._index(cmds, "journalctl --flush")
        assert restart < flush

    def test_failed_flush_does_not_abort(self, tmp_path):
        """The cap is installed regardless; a journal left in RAM is reported
        as RAM-ONLY by the status check, not hidden by a crash here."""
        out, _ = self._apply(tmp_path, failing="journalctl")
        assert out.splitlines() == ["APPLIED", "RC=0"]
