"""
Tests for the HAM-84 gpiozero pin.

gpiozero 2.0 imports importlib.metadata, which does not exist on Python 3.7,
so `import gpiozero` raises ModuleNotFoundError on Buster. That breaks
relay.py and therefore sensor power control (mjol_array --up/--down).

install_brokkr must pin gpiozero<2.0 when the interpreter is Python <=3.7,
and must NOT pin it on newer Python so the constraint auto-heals once the
fleet moves off Buster.
"""

import os
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
FILES_DIR = REPO_ROOT / "files"
UNIFIED_DIR = REPO_ROOT / "unified_install"


def _run_install(tmp_path, path_prepend=None):
    """Run install.sh --dry-run through the brokkr phase, return pip ops.

    Brokkr is deliberately NOT skipped -- install_brokkr is what emits the
    gpiozero pip_install operation into the manifest.
    """
    manifest_file = tmp_path / "manifest.json"
    env = os.environ.copy()
    env["MANIFEST_FILE"] = str(manifest_file)
    env["HOME"] = str(tmp_path)
    env["FILES_DIR"] = str(FILES_DIR)
    if path_prepend:
        env["PATH"] = f"{path_prepend}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(UNIFIED_DIR / "install.sh"),
         "01", "--wifi", "--dry-run",
         "--skip-packages", "--skip-hardware", "--skip-extras"],
        capture_output=True, text=True, env=env, cwd=str(UNIFIED_DIR),
    )
    if not manifest_file.exists():
        pytest.fail(
            f"Manifest not created.\nstdout: {result.stdout}\nstderr: {result.stderr}")
    manifest = json.loads(manifest_file.read_text())
    return [op.get("package", "") for op in manifest["operations"]
            if op.get("type") == "pip_install"]


def _python3_stub(tmp_path, minor):
    """Build a python3 stub reporting the given minor version.

    Only the version_info.minor probe is faked; everything else delegates to
    the real interpreter so the rest of the install is unaffected.
    """
    stub_dir = tmp_path / f"stub{minor}"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        if [[ "$*" == *"version_info.minor"* ]]; then echo {minor}; exit 0; fi
        exec /usr/bin/python3 "$@" 2>/dev/null || exec python3 "$@"
    """))
    stub.chmod(0o755)
    return str(stub_dir)


class TestGpiozeroPinWiring:
    """The pin logic is present in the brokkr install lib."""

    def test_pin_logic_defined(self):
        brokkr = (UNIFIED_DIR / "lib" / "brokkr.sh").read_text()
        assert "gpiozero_spec" in brokkr, \
            "install_brokkr must compute a gpiozero spec, not hardcode 'gpiozero'"
        assert "gpiozero<2.0" in brokkr, "the <2.0 pin must appear in brokkr.sh"
        assert "HAM-84" in brokkr, "reference the ticket so the pin isn't removed blindly"

    def test_pin_is_version_conditional(self):
        brokkr = (UNIFIED_DIR / "lib" / "brokkr.sh").read_text()
        assert "version_info.minor" in brokkr, \
            "the pin must be conditional on the interpreter version, not unconditional"


class TestGpiozeroPinManifest:
    """The dry-run manifest carries the right gpiozero spec per Python version."""

    def test_gpiozero_still_installed(self, tmp_path):
        packages = _run_install(tmp_path)
        assert any("gpiozero" in p for p in packages), \
            f"gpiozero pip_install op missing from brokkr install. pip ops: {packages}"

    def test_gpiozero_pinned_on_python37(self, tmp_path):
        packages = _run_install(
            tmp_path, path_prepend=_python3_stub(tmp_path, 7))
        assert any("gpiozero<2.0" in p for p in packages), \
            f"gpiozero not pinned <2.0 on Python 3.7. pip ops: {packages}"

    def test_gpiozero_unpinned_on_newer_python(self, tmp_path):
        """On Python >=3.8 the pin must NOT apply, so it auto-heals off Buster."""
        packages = _run_install(
            tmp_path, path_prepend=_python3_stub(tmp_path, 11))
        gpz = [p for p in packages if "gpiozero" in p]
        assert gpz, f"gpiozero op missing entirely. pip ops: {packages}"
        assert not any("<2.0" in p for p in gpz), \
            f"gpiozero should be unpinned on Python 3.11, got: {gpz}"


class TestVerifyDeploymentCheck:
    """verify_deployment.sh catches a broken gpiozero at bring-up."""

    def test_gpiozero_import_check_present(self):
        verify = (REPO_ROOT / "scripts" / "verify_deployment.sh").read_text()
        assert "import gpiozero" in verify, \
            "verify_deployment.sh must check that gpiozero imports in ltgenv"
        assert "HAM-84" in verify, "the failure message should point at the ticket"
