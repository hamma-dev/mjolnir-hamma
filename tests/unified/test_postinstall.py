"""
Tests for the HAM-120 install-gap fixes.

These run install.sh in --dry-run mode and assert the manifest contains the
new post-install operations:

  - HAM-84:  gpiozero is installed, pinned <2.0 on Python 3.7
  - HAM-118: .googlechat notification key is fetched from the server
  - HAM-80:  datasync user is provisioned locally

All assertions are against the dry-run manifest, matching the pattern in
test_script_execution.py.
"""

import json
import os
import subprocess
import textwrap

import pytest


def _run_install(unified_install_dir, work_dir, extra_args=None, path_prepend=None,
                 return_result=False, skip_hardware=True):
    """Run install.sh --wifi --dry-run and return the parsed manifest.

    By default brokkr and post-install run (only packages/hardware/extras are
    skipped) so the gpiozero and Phase 7 operations land in the manifest.
    With return_result=True, returns (manifest, CompletedProcess) so callers
    can also assert on stdout.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = work_dir / "manifest.json"
    env = os.environ.copy()
    env["MANIFEST_FILE"] = str(manifest_file)
    env["HOME"] = str(work_dir)
    env["FILES_DIR"] = str(unified_install_dir.parent / "files")
    env["SCRIPTS_DIR"] = str(unified_install_dir.parent / "scripts")
    if path_prepend:
        env["PATH"] = f"{path_prepend}:{env['PATH']}"

    args = ["01", "--wifi", "--dry-run", "--skip-packages", "--skip-extras"]
    if skip_hardware:
        args.append("--skip-hardware")
    if extra_args:
        args += extra_args

    result = subprocess.run(
        ["bash", str(unified_install_dir / "install.sh")] + args,
        capture_output=True, text=True, env=env, cwd=str(unified_install_dir),
    )
    if not manifest_file.exists():
        pytest.fail(f"Manifest not created.\nstdout: {result.stdout}\nstderr: {result.stderr}")
    manifest = json.loads(manifest_file.read_text())
    return (manifest, result) if return_result else manifest


def _pip_packages(manifest):
    return [op.get("package", "") for op in manifest["operations"]
            if op.get("type") == "pip_install"]


class TestGpiozeroPin:
    """HAM-84: gpiozero must be pinned <2.0 on Python 3.7."""

    def test_gpiozero_still_installed(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "gpz")
        assert any("gpiozero" in p for p in _pip_packages(manifest)), \
            "gpiozero pip_install op missing from brokkr install"

    def test_gpiozero_pinned_on_python37(self, unified_install_dir, tmp_path):
        """With a Python 3.7 interpreter, gpiozero must be pinned <2.0."""
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        stub = stub_dir / "python3"
        stub.write_text(textwrap.dedent("""\
            #!/bin/bash
            # Report minor version 7 for the gpiozero detection query;
            # delegate everything else to the real python3.
            if [[ "$*" == *"version_info.minor"* ]]; then echo 7; exit 0; fi
            exec /usr/bin/python3 "$@" 2>/dev/null || exec python3 "$@"
        """))
        stub.chmod(0o755)

        manifest = _run_install(unified_install_dir, tmp_path / "gpz37",
                                path_prepend=str(stub_dir))
        assert any("gpiozero<2.0" in p for p in _pip_packages(manifest)), \
            f"gpiozero not pinned <2.0 on Python 3.7. pip ops: {_pip_packages(manifest)}"

    def test_gpiozero_unpinned_on_newer_python(self, unified_install_dir, tmp_path):
        """On Python >=3.8 the pin must NOT be applied (auto-heals off Buster)."""
        stub_dir = tmp_path / "stub311"
        stub_dir.mkdir()
        stub = stub_dir / "python3"
        stub.write_text(textwrap.dedent("""\
            #!/bin/bash
            if [[ "$*" == *"version_info.minor"* ]]; then echo 11; exit 0; fi
            exec /usr/bin/python3 "$@" 2>/dev/null || exec python3 "$@"
        """))
        stub.chmod(0o755)

        manifest = _run_install(unified_install_dir, tmp_path / "gpz311",
                                path_prepend=str(stub_dir))
        gpz = [p for p in _pip_packages(manifest) if "gpiozero" in p]
        assert gpz, "gpiozero op missing"
        assert not any("<2.0" in p for p in gpz), \
            f"gpiozero should be unpinned on Python 3.11, got: {gpz}"


class TestNotificationKey:
    """HAM-118: .googlechat key fetched from the server."""

    def test_googlechat_fetch_present(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "gchat")
        scp_ops = [op for op in manifest["operations"]
                   if op.get("type") == "command" and ".googlechat" in op.get("cmd", "")]
        assert len(scp_ops) >= 1, "No .googlechat fetch operation found"
        assert "scp" in scp_ops[0]["cmd"], "googlechat fetch should use scp"
        assert scp_ops[0].get("best_effort") == "true", \
            "googlechat fetch must be marked best_effort (non-fatal)"


class TestDatasyncUser:
    """HAM-80: local datasync user provisioning."""

    def test_datasync_user_created(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "ds")
        cmds = [op.get("cmd", "") for op in manifest["operations"]
                if op.get("type") == "command"]
        assert any("useradd" in c and "datasync" in c for c in cmds), \
            "No datasync useradd operation found"
        assert any("usermod" in c and "datasync" in c for c in cmds), \
            "datasync not added to pi group"
        assert any("chmod o+rx /media/pi" in c for c in cmds), \
            "/media/pi permission not set for datasync"


class TestNotifiersEditable:
    """HAM-158: notifiers installed editable so git-pulls deploy."""

    def test_notifiers_editable(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "notif")
        notif = [op for op in manifest["operations"]
                 if op.get("type") == "pip_install" and "notifiers" in op.get("package", "")]
        assert notif, "notifiers pip_install op missing"
        assert notif[0].get("editable") == "true", \
            f"notifiers should be installed editable, got: {notif[0]}"

    def test_brokkr_and_sindri_stay_non_editable(self, unified_install_dir, tmp_path):
        """Guard: only notifiers flips to editable, not brokkr/serviceinstaller."""
        manifest = _run_install(unified_install_dir, tmp_path / "notif2")
        for pkg in ("brokkr", "serviceinstaller"):
            ops = [op for op in manifest["operations"]
                   if op.get("type") == "pip_install"
                   and op.get("package", "").endswith(pkg)]
            assert ops, f"{pkg} pip_install op missing"
            assert ops[0].get("editable") != "true", f"{pkg} should stay non-editable"


class TestLegacyServiceCleanup:
    """sensor-log #43/#9: retired pre-default units removed on install."""

    def test_legacy_units_removed(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "legacy")
        cmds = [op.get("cmd", "") for op in manifest["operations"]
                if op.get("type") == "command"]
        assert any("rm -f /etc/systemd/system/autossh-hamma.service" in c for c in cmds), \
            "legacy autossh-hamma.service not scheduled for removal"
        assert any("rm -f /etc/systemd/system/brokkr-hamma.service" in c for c in cmds), \
            "legacy brokkr-hamma.service not scheduled for removal"


class TestOwnershipNormalize:
    """sensor-log #78/#11/#33: pi paths normalized to pi ownership."""

    def test_ownership_normalized(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "own")
        cmds = [op.get("cmd", "") for op in manifest["operations"]
                if op.get("type") == "command"]
        assert any("chown -R pi:pi /home/pi/dev" in c for c in cmds), \
            "pi ownership normalization of /home/pi/dev missing"


class TestMountpointCleanup:
    """sensor-log #52: stale-mountpoint cleanup oneshot installed + enabled."""

    def test_cleanup_unit_installed_and_enabled(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "mp", skip_hardware=False)
        unit = "hamma-cleanup-stale-mountpoints.service"
        copies = [op for op in manifest["operations"]
                  if op.get("type") == "copy" and unit in op.get("dst", "")]
        assert copies, f"{unit} not copied into /etc/systemd/system"
        enables = [op for op in manifest["operations"]
                   if op.get("type") == "systemctl" and op.get("action") == "enable"
                   and unit in op.get("service", "")]
        assert enables, f"{unit} not enabled"

    def test_cleanup_unit_file_is_valid(self, repo_root):
        """The shipped unit must be well-formed and safe (rmdir-only)."""
        unit = repo_root / "files" / "hamma-cleanup-stale-mountpoints.service"
        text = unit.read_text()
        assert "Type=oneshot" in text
        assert "Before=udisks2.service" in text, "must run before udisks auto-mount"
        assert "rmdir" in text and "rm -rf" not in text, \
            "cleanup must use rmdir (empty-only), never rm -rf"


class TestBestEffortNonFatal:
    """Phase 7 steps must never abort the install under set -e, even when their
    privileged commands fail. Runtime (non-dry-run) coverage of the guards.

    The harness sources software.sh under `set -e` and shadows every mutating
    command with a shell function so nothing touches the real system; some are
    forced to fail to exercise the guards. Reaching the END marker proves the
    function returned instead of aborting.
    """

    def _harness(self, unified_install_dir, func_call):
        lib = unified_install_dir / "lib"
        script = f"""
            set -e
            source '{lib}/common.sh'
            source '{lib}/software.sh'
            # Sandbox: shadow all mutating commands; force the ones the guards
            # must tolerate to FAIL, so an unguarded command would trip set -e.
            id() {{ return 1; }}            # datasync user "absent" -> useradd path
            useradd() {{ return 0; }}
            usermod() {{ return 1; }}       # forced failure (guarded)
            mkdir() {{ return 0; }}
            chmod() {{ return 1; }}         # forced failure (guarded)
            chown() {{ return 1; }}         # forced failure (guarded)
            scp() {{ return 1; }}           # forced failure (guarded)
            systemctl() {{ return 1; }}     # forced failure (guarded)
            rm() {{ return 1; }}            # forced failure (guarded)
            sudo() {{ "$@"; }}              # strip sudo, run the (stubbed) command
            DRY_RUN=false
            {func_call}
            echo "HARNESS_REACHED_END"
        """
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_setup_datasync_local_never_aborts(self, unified_install_dir):
        r = self._harness(unified_install_dir, "setup_datasync_local")
        assert "HARNESS_REACHED_END" in r.stdout, (
            "setup_datasync_local aborted under set -e when a command failed "
            f"(best-effort contract violated).\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.returncode == 0, f"non-zero exit: {r.returncode}"

    def test_fetch_notification_key_never_aborts(self, unified_install_dir, tmp_path):
        # Point HOME at an empty tmp dir so the key_dst existence check is false
        # and the scp path (forced-fail) is exercised.
        r = self._harness(unified_install_dir, "fetch_notification_key")
        assert "HARNESS_REACHED_END" in r.stdout, (
            "fetch_notification_key aborted under set -e on scp failure.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.returncode == 0, f"non-zero exit: {r.returncode}"

    def test_normalize_pi_ownership_never_aborts(self, unified_install_dir):
        r = self._harness(unified_install_dir, "normalize_pi_ownership")
        assert "HARNESS_REACHED_END" in r.stdout, (
            "normalize_pi_ownership aborted under set -e on chown/chmod failure.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.returncode == 0, f"non-zero exit: {r.returncode}"

    def test_cleanup_legacy_services_never_aborts(self, unified_install_dir):
        r = self._harness(unified_install_dir, "cleanup_legacy_services")
        assert "HARNESS_REACHED_END" in r.stdout, (
            "cleanup_legacy_services aborted under set -e.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.returncode == 0, f"non-zero exit: {r.returncode}"


class TestSkipPostinstall:
    """--skip-postinstall must suppress the Phase 7 operations."""

    def test_skip_postinstall_suppresses_ops(self, unified_install_dir, tmp_path):
        manifest, result = _run_install(unified_install_dir, tmp_path / "skip",
                                        extra_args=["--skip-postinstall"],
                                        return_result=True)
        blob = json.dumps(manifest)
        assert ".googlechat" not in blob, "googlechat op present despite --skip-postinstall"
        assert "datasync" not in blob, "datasync op present despite --skip-postinstall"
        # Positively confirm the skip branch actually executed (not just that
        # Phase 7 happens to emit nothing for some unrelated reason).
        assert "Skipping post-install configuration" in result.stdout, \
            f"skip branch did not run.\nstdout: {result.stdout}"

    def test_postinstall_runs_by_default(self, unified_install_dir, tmp_path):
        """Positive control: without the flag, Phase 7 ops ARE present."""
        manifest = _run_install(unified_install_dir, tmp_path / "noskip")
        blob = json.dumps(manifest)
        assert ".googlechat" in blob, "googlechat op missing in a normal run"
        assert "datasync" in blob, "datasync op missing in a normal run"
