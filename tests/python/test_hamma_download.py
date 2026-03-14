"""Tests for hamma_download module."""

import importlib.util
import pathlib
import subprocess
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "hamma_download.py"


def load_hamma_download():
    """Load hamma_download module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "hamma_download", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hamma_download():
    """Provide the hamma_download module."""
    return load_hamma_download()


class TestSSHRun:
    """Tests for the _ssh_run SSH command helper."""

    def test_ssh_run_returns_stdout(self, hamma_download):
        """Successful SSH returns stripped stdout."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "DATA37\ncompressed\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = hamma_download._ssh_run(41, "ls /media/pi/")

        assert output == "DATA37\ncompressed"
        cmd = mock_run.call_args[0][0]
        assert "-p" in cmd
        assert "10041" in cmd
        assert "StrictHostKeyChecking=no" in " ".join(cmd)
        assert "BatchMode=yes" in " ".join(cmd)

    def test_ssh_run_raises_on_failure(self, hamma_download):
        """Non-zero SSH exit raises RuntimeError with stderr."""
        mock_result = MagicMock()
        mock_result.returncode = 255
        mock_result.stdout = ""
        mock_result.stderr = "Connection refused"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="Connection refused"):
                hamma_download._ssh_run(41, "ls /media/pi/")

    def test_ssh_run_timeout(self, hamma_download):
        """SSH timeout raises RuntimeError."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            with pytest.raises(RuntimeError, match="timed out"):
                hamma_download._ssh_run(41, "ls /media/pi/")


class TestDiscoverDrives:
    """Tests for DATA drive discovery."""

    def test_single_drive(self, hamma_download):
        """Single DATA drive returned as list."""
        with patch.object(
            hamma_download, "_ssh_run", return_value="DATA37\nlog\nlost+found"
        ):
            drives = hamma_download._discover_drives(41)
        assert drives == ["DATA37"]

    def test_multiple_drives(self, hamma_download):
        """Multiple DATA drives all returned."""
        with patch.object(
            hamma_download, "_ssh_run", return_value="DATA37\nDATA38\nlog"
        ):
            drives = hamma_download._discover_drives(41)
        assert drives == ["DATA37", "DATA38"]

    def test_no_drives_raises(self, hamma_download):
        """No DATA drive raises RuntimeError."""
        with patch.object(
            hamma_download, "_ssh_run", return_value="log\nlost+found"
        ):
            with pytest.raises(RuntimeError, match="No DATA drive found"):
                hamma_download._discover_drives(41)
