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


def _run_install(unified_install_dir, work_dir, extra_args=None, path_prepend=None):
    """Run install.sh --wifi --dry-run and return the parsed manifest.

    By default brokkr and post-install run (only packages/hardware/extras are
    skipped) so the gpiozero and Phase 7 operations land in the manifest.
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

    args = ["01", "--wifi", "--dry-run",
            "--skip-packages", "--skip-hardware", "--skip-extras"]
    if extra_args:
        args += extra_args

    result = subprocess.run(
        ["bash", str(unified_install_dir / "install.sh")] + args,
        capture_output=True, text=True, env=env, cwd=str(unified_install_dir),
    )
    if not manifest_file.exists():
        pytest.fail(f"Manifest not created.\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(manifest_file.read_text())


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


class TestSkipPostinstall:
    """--skip-postinstall must suppress the Phase 7 operations."""

    def test_skip_postinstall_suppresses_ops(self, unified_install_dir, tmp_path):
        manifest = _run_install(unified_install_dir, tmp_path / "skip",
                                extra_args=["--skip-postinstall"])
        blob = json.dumps(manifest)
        assert ".googlechat" not in blob, "googlechat op present despite --skip-postinstall"
        assert "datasync" not in blob, "datasync op present despite --skip-postinstall"
