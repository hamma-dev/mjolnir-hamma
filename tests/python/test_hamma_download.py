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
        assert "-J" in cmd
        assert "monitor@hamma.dev" in cmd

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

    def test_missing_directory_returns_empty(self, hamma_download):
        """Missing remote directory returns empty list instead of raising."""
        with patch.object(
            hamma_download, "_ssh_run",
            side_effect=RuntimeError("SSH to sensor 5 failed (exit 2): No such file")
        ):
            dirs = hamma_download._list_remote_dirs(5, "DATA38", compressed=True)

        assert dirs == []


class TestDownload:
    """Tests for the main download() function."""

    def test_single_hour_compressed(self, hamma_download):
        """Download single hour of compressed data."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=["2025-11-05T05"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            rc = hamma_download.download(
                sensor=41,
                dest="/rgroup/hammadev/ignis/mj41",
                start="2025-11-05T05",
            )

        assert rc == 0
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "rsync" in cmd_str
        assert "-avz" in cmd_str
        assert "10041" in cmd_str
        assert "/media/pi/DATA37/compressed/2025-11-05T05" in cmd_str
        assert "/rgroup/hammadev/ignis/mj41" in cmd_str

    def test_dry_run_flag(self, hamma_download):
        """dry_run=True passes --dry-run to rsync."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=["2025-11-05T05"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.download(
                sensor=41, dest="/tmp/test", start="2025-11-05T05", dry_run=True,
            )

        cmd = mock_run.call_args[0][0]
        assert "--dry-run" in cmd

    def test_raw_mode(self, hamma_download):
        """compressed=False uses drive root path."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=["2025-11-05T05"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.download(
                sensor=41, dest="/tmp/test", start="2025-11-05T05", compressed=False,
            )

        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "/media/pi/DATA37/2025-11-05T05" in cmd_str
        assert "compressed" not in cmd_str

    def test_multiple_drives(self, hamma_download):
        """Multiple DATA drives each get their own rsync call."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37", "DATA38"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=["2025-11-05T05"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.download(
                sensor=41, dest="/tmp/test", start="2025-11-05T05",
            )

        assert mock_run.call_count == 2
        calls = [" ".join(c[0][0]) for c in mock_run.call_args_list]
        assert any("DATA37" in c for c in calls)
        assert any("DATA38" in c for c in calls)

    def test_no_matching_dirs_returns_zero(self, hamma_download):
        """No matching directories logs warning and returns 0."""
        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=[]):
            rc = hamma_download.download(
                sensor=41, dest="/tmp/test", start="2099-01-01",
            )

        assert rc == 0

    def test_rsync_failure_returns_code(self, hamma_download):
        """Rsync failure returns the non-zero exit code."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 23
        mock_rsync.stderr = "some rsync error"

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=["2025-11-05T05"]), \
             patch("subprocess.run", return_value=mock_rsync):
            rc = hamma_download.download(
                sensor=41, dest="/tmp/test", start="2025-11-05T05",
            )

        assert rc == 23

    def test_multiple_dirs_batched(self, hamma_download):
        """Multiple matching dirs batched into single rsync call."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives", return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs", return_value=[
                 "2025-11-05T00", "2025-11-05T05", "2025-11-05T12",
             ]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.download(
                sensor=41, dest="/tmp/test", start="2025-11-05",
            )

        # Single rsync call with all three dirs
        assert mock_run.call_count == 1
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "2025-11-05T00" in cmd_str
        assert "2025-11-05T05" in cmd_str
        assert "2025-11-05T12" in cmd_str


class TestCLI:
    """Tests for the argparse CLI."""

    def test_required_args(self, hamma_download):
        """Missing required args causes SystemExit."""
        with pytest.raises(SystemExit):
            hamma_download._build_parser().parse_args([])

    def test_all_args_parsed(self, hamma_download):
        """All arguments parsed correctly."""
        args = hamma_download._build_parser().parse_args([
            "-s", "41",
            "-d", "/rgroup/hammadev/ignis/mj41",
            "--start", "2025-11-05",
            "--end", "2025-11-07",
            "--raw",
            "-n",
            "-v",
        ])
        assert args.sensor == 41
        assert args.dest == "/rgroup/hammadev/ignis/mj41"
        assert args.start == "2025-11-05"
        assert args.end == "2025-11-07"
        assert args.raw is True
        assert args.dry_run is True
        assert args.verbose is True

    def test_defaults(self, hamma_download):
        """Default values for optional args."""
        args = hamma_download._build_parser().parse_args([
            "-s", "1", "-d", "/tmp/test", "--start", "2025-01-01",
        ])
        assert args.end is None
        assert args.raw is False
        assert args.dry_run is False
        assert args.verbose is False

    def test_main_calls_download(self, hamma_download):
        """main() parses args and calls download()."""
        with patch.object(hamma_download, "download", return_value=0) as mock_dl, \
             patch("sys.argv", [
                 "hamma_download.py", "-s", "41",
                 "-d", "/tmp/test", "--start", "2025-11-05",
             ]):
            with pytest.raises(SystemExit) as exc_info:
                hamma_download.main()
            assert exc_info.value.code == 0

        mock_dl.assert_called_once_with(
            sensor=41,
            dest="/tmp/test",
            start="2025-11-05",
            end=None,
            compressed=True,
            dry_run=False,
        )

    def test_main_raw_flag_inverts_compressed(self, hamma_download):
        """--raw flag sets compressed=False in download() call."""
        with patch.object(hamma_download, "download", return_value=0) as mock_dl, \
             patch("sys.argv", [
                 "hamma_download.py", "-s", "41",
                 "-d", "/tmp/test", "--start", "2025-11-05", "--raw",
             ]):
            with pytest.raises(SystemExit):
                hamma_download.main()

        assert mock_dl.call_args[1]["compressed"] is False


class TestSync:
    """Tests for the sync download mode."""

    def test_sync_downloads_all_compressed(self, hamma_download):
        """Sync mode downloads everything in compressed/ without date filter."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=["2026-03-15T08", "2026-03-15T12"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            rc = hamma_download.sync(sensor=41, dest="/tmp/test")

        assert rc == 0
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "rsync" in cmd_str
        assert "/media/pi/DATA37/compressed/2026-03-15T08" in cmd_str
        assert "/media/pi/DATA37/compressed/2026-03-15T12" in cmd_str

    def test_sync_uses_remove_source_files(self, hamma_download):
        """Sync mode passes --remove-source-files to rsync."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=["2026-03-15T08"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.sync(sensor=41, dest="/tmp/test", cleanup=True)

        cmd = mock_run.call_args[0][0]
        assert "--remove-source-files" in cmd

    def test_sync_no_cleanup_by_default(self, hamma_download):
        """Without cleanup flag, no --remove-source-files."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=["2026-03-15T08"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.sync(sensor=41, dest="/tmp/test", cleanup=False)

        cmd = mock_run.call_args[0][0]
        assert "--remove-source-files" not in cmd

    def test_sync_skips_recent_dirs(self, hamma_download):
        """Sync skips directories from the current hour to avoid partial files."""
        from datetime import datetime as real_datetime
        fake_now = real_datetime(2026, 3, 17, 14, 30, 0)

        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=[
                              "2026-03-16T08", "2026-03-16T12",
                              "2026-03-17T14",  # current hour
                          ]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run, \
             patch.object(hamma_download, "datetime") as mock_dt:
            mock_dt.utcnow.return_value = fake_now
            hamma_download.sync(sensor=41, dest="/tmp/test")

        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "2026-03-16T08" in cmd_str
        assert "2026-03-16T12" in cmd_str
        assert "2026-03-17T14" not in cmd_str

    def test_sync_multiple_drives(self, hamma_download):
        """Sync handles multiple DATA drives."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37", "DATA38"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=["2026-03-16T08"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.sync(sensor=41, dest="/tmp/test")

        assert mock_run.call_count == 2

    def test_sync_dry_run(self, hamma_download):
        """Sync dry_run passes --dry-run and omits --remove-source-files."""
        mock_rsync = MagicMock()
        mock_rsync.returncode = 0
        mock_rsync.stderr = ""

        with patch.object(hamma_download, "_discover_drives",
                          return_value=["DATA37"]), \
             patch.object(hamma_download, "_list_remote_dirs",
                          return_value=["2026-03-15T08"]), \
             patch("subprocess.run", return_value=mock_rsync) as mock_run:
            hamma_download.sync(
                sensor=41, dest="/tmp/test", cleanup=True, dry_run=True)

        cmd = mock_run.call_args[0][0]
        assert "--dry-run" in cmd
        # cleanup should be suppressed during dry run
        assert "--remove-source-files" not in cmd


class TestSyncCLI:
    """Tests for sync mode CLI integration."""

    def test_sync_flag_no_start_required(self, hamma_download):
        """--sync mode doesn't require --start."""
        args = hamma_download._build_parser().parse_args([
            "-s", "41", "-d", "/tmp/test", "--sync",
        ])
        assert args.sync is True
        assert args.start is None

    def test_sync_with_cleanup(self, hamma_download):
        """--sync --cleanup parsed correctly."""
        args = hamma_download._build_parser().parse_args([
            "-s", "41", "-d", "/tmp/test", "--sync", "--cleanup",
        ])
        assert args.sync is True
        assert args.cleanup is True

    def test_main_sync_calls_sync_function(self, hamma_download):
        """main() with --sync calls sync() instead of download()."""
        with patch.object(hamma_download, "sync", return_value=0) as mock_sync, \
             patch("sys.argv", [
                 "hamma_download.py", "-s", "41",
                 "-d", "/tmp/test", "--sync", "--cleanup",
             ]):
            with pytest.raises(SystemExit) as exc_info:
                hamma_download.main()
            assert exc_info.value.code == 0

        mock_sync.assert_called_once_with(
            sensor=41, dest="/tmp/test",
            cleanup=True, dry_run=False,
        )

    def test_start_required_without_sync(self, hamma_download):
        """Without --sync, missing --start causes error exit."""
        with patch("sys.argv", [
                 "hamma_download.py", "-s", "41", "-d", "/tmp/test",
             ]):
            with pytest.raises(SystemExit) as exc_info:
                hamma_download.main()
            assert exc_info.value.code != 0
