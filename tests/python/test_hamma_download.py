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


class TestParseDate:
    """Tests for date string parsing."""

    def test_date_only(self, hamma_download):
        """YYYY-MM-DD returns (date_str, None)."""
        date_str, hour = hamma_download._parse_date("2025-11-05")
        assert date_str == "2025-11-05"
        assert hour is None

    def test_date_with_hour(self, hamma_download):
        """YYYY-MM-DDTHH returns (date_str, hour_str)."""
        date_str, hour = hamma_download._parse_date("2025-11-05T05")
        assert date_str == "2025-11-05"
        assert hour == "05"

    def test_datetime_object(self, hamma_download):
        """datetime object extracts date and hour."""
        from datetime import datetime
        dt = datetime(2025, 11, 5, 14, 30, 0)
        date_str, hour = hamma_download._parse_date(dt)
        assert date_str == "2025-11-05"
        assert hour == "14"

    def test_invalid_format_raises(self, hamma_download):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            hamma_download._parse_date("Nov 5 2025")


class TestFilterDirs:
    """Tests for filtering remote directories by date range."""

    SAMPLE_DIRS = [
        "2025-11-04T23",
        "2025-11-05T00",
        "2025-11-05T05",
        "2025-11-05T12",
        "2025-11-06T00",
        "2025-11-06T23",
        "2025-11-07T00",
        "2025-11-07T12",
    ]

    def test_single_hour(self, hamma_download):
        """Single hour start returns just that directory."""
        result = hamma_download._filter_dirs(
            self.SAMPLE_DIRS, "2025-11-05T05", None
        )
        assert result == ["2025-11-05T05"]

    def test_single_date(self, hamma_download):
        """Date-only start returns all hours for that day."""
        result = hamma_download._filter_dirs(
            self.SAMPLE_DIRS, "2025-11-05", None
        )
        assert result == ["2025-11-05T00", "2025-11-05T05", "2025-11-05T12"]

    def test_date_range(self, hamma_download):
        """Range includes all hours of both start and end dates."""
        result = hamma_download._filter_dirs(
            self.SAMPLE_DIRS, "2025-11-05", "2025-11-06"
        )
        assert result == [
            "2025-11-05T00", "2025-11-05T05", "2025-11-05T12",
            "2025-11-06T00", "2025-11-06T23",
        ]

    def test_range_with_hours(self, hamma_download):
        """Range with hour precision filters exactly."""
        result = hamma_download._filter_dirs(
            self.SAMPLE_DIRS, "2025-11-05T05", "2025-11-06T00"
        )
        assert result == ["2025-11-05T05", "2025-11-05T12", "2025-11-06T00"]

    def test_no_matches(self, hamma_download):
        """No matching dirs returns empty list."""
        result = hamma_download._filter_dirs(
            self.SAMPLE_DIRS, "2025-12-01", None
        )
        assert result == []


class TestListRemoteDirs:
    """Tests for listing directories on the remote sensor."""

    def test_compressed_path(self, hamma_download):
        """Compressed mode lists compressed/ subdir."""
        with patch.object(
            hamma_download, "_ssh_run",
            return_value="2025-11-05T00\n2025-11-05T05\n2025-11-05T12"
        ) as mock_ssh:
            dirs = hamma_download._list_remote_dirs(41, "DATA37", compressed=True)

        assert dirs == ["2025-11-05T00", "2025-11-05T05", "2025-11-05T12"]
        cmd = mock_ssh.call_args[0][1]
        assert "/media/pi/DATA37/compressed" in cmd

    def test_raw_path_filters_non_date_entries(self, hamma_download):
        """Raw mode lists drive root and filters to date dirs only."""
        with patch.object(
            hamma_download, "_ssh_run",
            return_value="2025-11-05T00\n2025-11-05T05\ncompressed\nlog"
        ):
            dirs = hamma_download._list_remote_dirs(41, "DATA37", compressed=False)

        assert dirs == ["2025-11-05T00", "2025-11-05T05"]
