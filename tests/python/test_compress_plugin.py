"""
Unit tests for plugins/compress_data.py - the HAMMA trigger data compression plugin.

These tests mock brokkr and hamma dependencies to test the plugin logic
without requiring those packages to be installed.
"""

import datetime
import importlib.util
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --- Module loading with mocked dependencies ---

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "compress_data.py"


# Create a mock OutputStep base class that CompressData will inherit from
class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep."""

    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_compress_module():
    """Load the compress_data plugin with mocked dependencies."""
    # Build a consistent mock hierarchy so that attribute access
    # through brokkr.pipeline.base.OutputStep resolves correctly.
    mock_brokkr_base = MagicMock()
    mock_brokkr_base.OutputStep = MockOutputStep

    mock_brokkr_pipeline = MagicMock()
    mock_brokkr_pipeline.base = mock_brokkr_base

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_brokkr_pipeline
    mock_brokkr.pipeline.base = mock_brokkr_base

    mock_compress_module = MagicMock()
    mock_hamma = MagicMock()
    mock_hamma.compression = mock_compress_module

    with patch.dict('sys.modules', {
        'brokkr': mock_brokkr,
        'brokkr.pipeline': mock_brokkr_pipeline,
        'brokkr.pipeline.base': mock_brokkr_base,
        'hamma': mock_hamma,
        'hamma.compression': mock_compress_module,
    }):
        spec = importlib.util.spec_from_file_location(
            "compress_data", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module, mock_compress_module


MODULE, MOCK_HAMMA_COMPRESSION = load_compress_module()
CompressData = MODULE.CompressData


def make_step(source_path="/media/pi", **kwargs):
    """Create a CompressData instance with sensible test defaults."""
    return CompressData(source_path=source_path, **kwargs)


# --- Fixtures ---

@pytest.fixture
def mock_compress_file():
    """Patch the module-level compress_file reference."""
    with patch.object(MODULE, 'compress_file') as mock_cf:
        yield mock_cf


@pytest.fixture
def media_tree(tmp_path):
    """
    Create a realistic /media/pi directory structure.

    Layout:
        tmp_path/
            2026-01-15T10/          # old dir (mtime set to 3 days ago)
                trigger01.bin
                trigger02.bin
            2026-02-19T08/          # recent dir (mtime = now)
                trigger03.bin
            compressed/             # output subdir
                2026-01-15T10/
                    trigger01.hmc   # already compressed
    """
    old_dir = tmp_path / "2026-01-15T10"
    old_dir.mkdir()
    (old_dir / "trigger01.bin").write_bytes(b"\x00" * 100)
    (old_dir / "trigger02.bin").write_bytes(b"\x00" * 100)

    recent_dir = tmp_path / "2026-02-19T08"
    recent_dir.mkdir()
    (recent_dir / "trigger03.bin").write_bytes(b"\x00" * 100)

    compressed_dir = tmp_path / "compressed"
    compressed_subdir = compressed_dir / "2026-01-15T10"
    compressed_subdir.mkdir(parents=True)
    (compressed_subdir / "trigger01.hmc").write_bytes(b"\x00" * 50)

    # Set old directory mtime to 3 days ago
    three_days_ago = time.time() - 3 * 86400
    os.utime(old_dir, (three_days_ago, three_days_ago))

    return tmp_path


# --- Tests ---

class TestQuietHours:
    """Tests for the _is_quiet_time method."""

    def test_within_quiet_hours(self):
        step = make_step()  # quiet_start=8, quiet_end=0
        t = datetime.datetime(2026, 2, 19, 12, 0, 0)  # noon
        assert step._is_quiet_time(t) is True

    def test_outside_quiet_hours(self):
        step = make_step()
        t = datetime.datetime(2026, 2, 19, 3, 0, 0)  # 3 AM
        assert step._is_quiet_time(t) is False

    def test_boundary_start(self):
        step = make_step()
        t = datetime.datetime(2026, 2, 19, 8, 0, 0)  # exactly 8 AM
        assert step._is_quiet_time(t) is True

    def test_boundary_end(self):
        step = make_step()
        t = datetime.datetime(2026, 2, 19, 0, 0, 0)  # exactly midnight
        assert step._is_quiet_time(t) is False

    def test_wraparound_night(self):
        step = make_step(quiet_start=22, quiet_end=5)
        t_late = datetime.datetime(2026, 2, 19, 23, 0, 0)
        t_early = datetime.datetime(2026, 2, 19, 3, 0, 0)
        assert step._is_quiet_time(t_late) is True
        assert step._is_quiet_time(t_early) is True

    def test_wraparound_day(self):
        step = make_step(quiet_start=22, quiet_end=5)
        t = datetime.datetime(2026, 2, 19, 10, 0, 0)
        assert step._is_quiet_time(t) is False


class TestFileDiscovery:
    """Tests for compress_old_files file/directory discovery logic."""

    def test_finds_old_bin_files(self, media_tree, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        step = make_step(
            source_path=str(media_tree), delete_originals=False)
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        # trigger01 is already compressed (hmc exists), trigger02 should be new
        assert compressed == 1
        assert skipped == 1
        assert errors == 0

    def test_skips_recent_files(self, media_tree, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        step = make_step(
            source_path=str(media_tree), delete_originals=False)
        step._is_quiet_time = lambda t: True
        step.compress_old_files()

        # trigger03.bin is in recent dir, should not be passed to compress
        compressed_names = [
            call.args[0] for call in mock_compress_file.call_args_list]
        assert not any("trigger03" in str(n) for n in compressed_names)

    def test_skips_already_compressed(self, media_tree, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        step = make_step(
            source_path=str(media_tree), delete_originals=False)
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        # trigger01.hmc already exists -> skipped
        assert skipped == 1
        # Only trigger02 should be compressed
        assert mock_compress_file.call_count == 1
        assert "trigger02" in str(mock_compress_file.call_args[0][0])

    def test_skips_output_subdir(self, media_tree, mock_compress_file):
        step = make_step(
            source_path=str(media_tree), delete_originals=False)
        step._is_quiet_time = lambda t: True
        step.compress_old_files()

        # The compressed/ directory should not be processed as a data dir
        compressed_names = [
            call.args[0] for call in mock_compress_file.call_args_list]
        assert not any("compressed" in str(Path(n).parent.name)
                        for n in compressed_names
                        if str(Path(n).parent.name) == "compressed")

    def test_skips_non_date_directories(self, tmp_path, mock_compress_file):
        """Non-date dirs like lost+found should not be scanned."""
        import time

        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        # Create a valid date dir with a .bin file
        date_dir = tmp_path / "2026-01-15T10"
        date_dir.mkdir()
        (date_dir / "trigger01.bin").write_bytes(b"\x00" * 100)

        # Create non-date dirs that should be ignored
        for name in ["lost+found", ".Trash-1000", "$RECYCLE.BIN",
                      "System Volume Information", "temp"]:
            junk = tmp_path / name
            junk.mkdir()
            (junk / "trigger01.bin").write_bytes(b"\x00" * 100)

        # Backdate all dirs so min_age_days check passes
        old_time = time.time() - 86400 * 2
        for d in tmp_path.iterdir():
            if d.is_dir():
                os.utime(d, (old_time, old_time))

        step = make_step(source_path=str(tmp_path))
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        # Only the valid date dir's file should be compressed
        assert mock_compress_file.call_count == 1
        assert "2026-01-15T10" in str(mock_compress_file.call_args[0][0])

    def test_source_path_missing(self):
        step = make_step(source_path="/nonexistent/path")
        result = step.compress_old_files()
        assert result == (0, 0, 0)


class TestCompression:
    """Tests for _compress_file and related compression logic."""

    def test_compress_file_success(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        step = make_step(source_path=str(tmp_path))
        bin_file = tmp_path / "test.bin"
        bin_file.write_bytes(b"\x00" * 100)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        result = step._compress_file(bin_file, out_dir)
        assert result is True
        mock_compress_file.assert_called_once_with(
            str(bin_file),
            output_dir=str(out_dir),
            method="quantize",
            step=8,
        )

    def test_compress_file_failure(self, tmp_path, mock_compress_file):
        mock_compress_file.side_effect = RuntimeError("bad data")

        step = make_step(source_path=str(tmp_path))
        bin_file = tmp_path / "test.bin"
        bin_file.write_bytes(b"\x00" * 100)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        result = step._compress_file(bin_file, out_dir)
        assert result is False

    def test_compress_file_empty_result(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = []

        step = make_step(source_path=str(tmp_path))
        bin_file = tmp_path / "test.bin"
        bin_file.write_bytes(b"\x00" * 100)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        result = step._compress_file(bin_file, out_dir)
        assert result is False

    def test_delete_originals_true(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        # Create a data dir with one bin file, old enough
        data_dir = tmp_path / "2026-01-15T10"
        data_dir.mkdir()
        bin_file = data_dir / "trigger.bin"
        bin_file.write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(data_dir, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path), delete_originals=True)
        step._is_quiet_time = lambda t: True
        step.compress_old_files()

        assert not bin_file.exists()

    def test_delete_originals_false(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        data_dir = tmp_path / "2026-01-15T10"
        data_dir.mkdir()
        bin_file = data_dir / "trigger.bin"
        bin_file.write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(data_dir, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True
        step.compress_old_files()

        assert bin_file.exists()

    def test_empty_dir_cleanup(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        data_dir = tmp_path / "2026-01-15T10"
        data_dir.mkdir()
        bin_file = data_dir / "trigger.bin"
        bin_file.write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(data_dir, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path), delete_originals=True)
        step._is_quiet_time = lambda t: True
        step.compress_old_files()

        # Dir should be removed because delete_originals=True and it's empty
        assert not data_dir.exists()


class TestExecute:
    """Tests for the execute method."""

    def test_execute_during_quiet_hours(self, tmp_path, mock_compress_file):
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        # Set up a data dir with an old file
        data_dir = tmp_path / "2026-01-15T10"
        data_dir.mkdir()
        (data_dir / "trigger.bin").write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(data_dir, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True

        mock_time_value = MagicMock()
        mock_time_value.value = datetime.datetime(2026, 2, 19, 12, 0, 0)
        input_data = {'time': mock_time_value}

        result = step.execute(input_data)

        assert result is input_data
        mock_compress_file.assert_called_once()

    def test_execute_outside_quiet_hours(self, mock_compress_file):
        step = make_step()

        # 3 AM is outside default quiet hours (8-midnight)
        mock_time_value = MagicMock()
        mock_time_value.value = datetime.datetime(2026, 2, 19, 3, 0, 0)
        input_data = {'time': mock_time_value}

        result = step.execute(input_data)

        assert result is input_data
        mock_compress_file.assert_not_called()

    def test_execute_handles_exceptions(self):
        step = make_step()

        # input_data without 'time' key will raise KeyError
        input_data = {'no_time': 'here'}

        result = step.execute(input_data)

        # Should return input_data without crashing
        assert result is input_data
        step.logger.error.assert_called()


class TestConfiguration:
    """Tests for default and custom configuration values."""

    def test_default_values(self):
        step = make_step()
        assert step.method == "quantize"
        assert step.step == 8
        assert step.quiet_start == 8
        assert step.quiet_end == 0
        assert step.min_age_days == 1
        assert step.delete_originals is False
        assert step.output_subdir == "compressed"
        assert step.drive_glob is None

    def test_custom_values(self):
        step = make_step(
            method="lossless",
            step=4,
            quiet_start=22,
            quiet_end=6,
            min_age_days=3,
            delete_originals=False,
            output_subdir="archive",
            drive_glob="DATA??",
        )
        assert step.method == "lossless"
        assert step.step == 4
        assert step.quiet_start == 22
        assert step.quiet_end == 6
        assert step.min_age_days == 3
        assert step.delete_originals is False
        assert step.output_subdir == "archive"
        assert step.drive_glob == "DATA??"


class TestDriveGlob:
    """Tests for drive_glob / _get_data_roots() functionality."""

    def test_no_drive_glob_returns_source_path(self, tmp_path):
        """Without drive_glob, _get_data_roots returns [source_path]."""
        step = make_step(source_path=str(tmp_path))
        roots = step._get_data_roots()
        assert roots == [tmp_path]

    def test_drive_glob_finds_matching_dirs(self, tmp_path):
        """With drive_glob, finds matching drive directories."""
        (tmp_path / "DATA37").mkdir()
        (tmp_path / "DATA38").mkdir()
        (tmp_path / "other_dir").mkdir()

        step = make_step(source_path=str(tmp_path), drive_glob="DATA??")
        roots = step._get_data_roots()
        assert len(roots) == 2
        assert tmp_path / "DATA37" in roots
        assert tmp_path / "DATA38" in roots

    def test_drive_glob_skips_files(self, tmp_path):
        """drive_glob only matches directories, not files."""
        (tmp_path / "DATA37").mkdir()
        (tmp_path / "DATA38").write_bytes(b"not a dir")

        step = make_step(source_path=str(tmp_path), drive_glob="DATA??")
        roots = step._get_data_roots()
        assert len(roots) == 1
        assert roots[0] == tmp_path / "DATA37"

    def test_drive_glob_no_matches(self, tmp_path):
        """When no drives match, returns empty list."""
        (tmp_path / "something_else").mkdir()

        step = make_step(source_path=str(tmp_path), drive_glob="DATA??")
        roots = step._get_data_roots()
        assert roots == []

    def test_drive_glob_sorted(self, tmp_path):
        """Matching drives are returned in sorted order."""
        (tmp_path / "DATA99").mkdir()
        (tmp_path / "DATA01").mkdir()
        (tmp_path / "DATA50").mkdir()

        step = make_step(source_path=str(tmp_path), drive_glob="DATA??")
        roots = step._get_data_roots()
        names = [r.name for r in roots]
        assert names == ["DATA01", "DATA50", "DATA99"]

    def test_compress_with_drive_glob(self, tmp_path, mock_compress_file):
        """Compression works with drive_glob layout (date dirs inside drives)."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        # Create drive with old data dir
        drive = tmp_path / "DATA37"
        drive.mkdir()
        old_dir = drive / "2026-01-15T10"
        old_dir.mkdir()
        (old_dir / "trigger01.bin").write_bytes(b"\x00" * 100)
        (old_dir / "trigger02.bin").write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(old_dir, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path),
            drive_glob="DATA??",
            delete_originals=False,
        )
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        assert compressed == 2
        assert skipped == 0
        assert errors == 0
        # Output should be in DATA37/compressed/
        assert (drive / "compressed" / "2026-01-15T10").is_dir()

    def test_compress_multiple_drives(self, tmp_path, mock_compress_file):
        """Compression handles data on multiple drives."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        three_days_ago = time.time() - 3 * 86400

        # Drive 37 with data
        drive37 = tmp_path / "DATA37"
        drive37.mkdir()
        old_dir37 = drive37 / "2026-01-15T10"
        old_dir37.mkdir()
        (old_dir37 / "trigger01.bin").write_bytes(b"\x00" * 100)
        os.utime(old_dir37, (three_days_ago, three_days_ago))

        # Drive 38 with data
        drive38 = tmp_path / "DATA38"
        drive38.mkdir()
        old_dir38 = drive38 / "2026-01-16T08"
        old_dir38.mkdir()
        (old_dir38 / "trigger02.bin").write_bytes(b"\x00" * 100)
        os.utime(old_dir38, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path),
            drive_glob="DATA??",
            delete_originals=False,
        )
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        assert compressed == 2
        assert errors == 0
        # Each drive gets its own compressed/ subdir
        assert (drive37 / "compressed" / "2026-01-15T10").is_dir()
        assert (drive38 / "compressed" / "2026-01-16T08").is_dir()

    def test_compress_empty_drive_skipped(self, tmp_path, mock_compress_file):
        """Empty drive directories (no date dirs) produce no errors."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        three_days_ago = time.time() - 3 * 86400

        # Drive 37 with data
        drive37 = tmp_path / "DATA37"
        drive37.mkdir()
        old_dir = drive37 / "2026-01-15T10"
        old_dir.mkdir()
        (old_dir / "trigger01.bin").write_bytes(b"\x00" * 100)
        os.utime(old_dir, (three_days_ago, three_days_ago))

        # Drive 38 is empty (mounted but no data)
        (tmp_path / "DATA38").mkdir()

        step = make_step(
            source_path=str(tmp_path),
            drive_glob="DATA??",
            delete_originals=False,
        )
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        assert compressed == 1
        assert errors == 0

    def test_backward_compat_no_drive_glob(self, tmp_path, mock_compress_file):
        """Without drive_glob, behaves exactly as before (date dirs in source_path)."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        old_dir = tmp_path / "2026-01-15T10"
        old_dir.mkdir()
        (old_dir / "trigger01.bin").write_bytes(b"\x00" * 100)
        three_days_ago = time.time() - 3 * 86400
        os.utime(old_dir, (three_days_ago, three_days_ago))

        # No drive_glob — original behavior
        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True
        compressed, skipped, errors = step.compress_old_files()

        assert compressed == 1
        assert errors == 0
        # Output goes directly to source_path/compressed/
        assert (tmp_path / "compressed" / "2026-01-15T10").is_dir()


class TestForwardOnlyScan:
    """Tests that compression only scans from resume position forward."""

    def test_skips_directories_before_resume_position(
            self, tmp_path, mock_compress_file):
        """Directories older than resume position are not scanned."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        three_days_ago = time.time() - 3 * 86400

        # Old dir — already compressed
        old_dir = tmp_path / "2026-01-10T08"
        old_dir.mkdir()
        (old_dir / "trigger_old.bin").write_bytes(b"\x00" * 100)
        os.utime(old_dir, (three_days_ago, three_days_ago))

        # Newer dir — needs compression
        new_dir = tmp_path / "2026-01-15T10"
        new_dir.mkdir()
        (new_dir / "trigger_new.bin").write_bytes(b"\x00" * 100)
        os.utime(new_dir, (three_days_ago, three_days_ago))

        # Simulate resume position: compressed/ has the old dir done
        compressed = tmp_path / "compressed"
        old_out = compressed / "2026-01-10T08"
        old_out.mkdir(parents=True)
        (old_out / "trigger_old.hmc").write_bytes(b"\x00" * 50)

        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True  # Always quiet for test
        compressed_count, skipped, errors = step.compress_old_files()

        # Should only process the new dir, not re-check old dir's files
        assert compressed_count == 1
        assert skipped == 0  # old dir not scanned at all
        assert errors == 0
        # Verify compress_file was called only for the new file
        assert mock_compress_file.call_count == 1
        assert "trigger_new" in str(mock_compress_file.call_args[0][0])

    def test_rechecks_resume_directory_for_partial(
            self, tmp_path, mock_compress_file):
        """The resume directory itself is re-checked for partial completion."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        three_days_ago = time.time() - 3 * 86400

        # Dir with 2 files, one already compressed
        data_dir = tmp_path / "2026-01-15T10"
        data_dir.mkdir()
        (data_dir / "trigger01.bin").write_bytes(b"\x00" * 100)
        (data_dir / "trigger02.bin").write_bytes(b"\x00" * 100)
        os.utime(data_dir, (three_days_ago, three_days_ago))

        compressed = tmp_path / "compressed"
        out_dir = compressed / "2026-01-15T10"
        out_dir.mkdir(parents=True)
        (out_dir / "trigger01.hmc").write_bytes(b"\x00" * 50)

        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True  # Always quiet for test
        compressed_count, skipped, errors = step.compress_old_files()

        assert compressed_count == 1  # trigger02
        assert skipped == 1  # trigger01 already done
        assert errors == 0

    def test_full_scan_when_no_resume_position(
            self, tmp_path, mock_compress_file):
        """Without any compressed output, scans everything (first run)."""
        mock_compress_file.return_value = [
            {'ratio': 0.07, 'method': 'quantize'}]

        three_days_ago = time.time() - 3 * 86400

        dir1 = tmp_path / "2026-01-10T08"
        dir1.mkdir()
        (dir1 / "trigger01.bin").write_bytes(b"\x00" * 100)
        os.utime(dir1, (three_days_ago, three_days_ago))

        dir2 = tmp_path / "2026-01-15T10"
        dir2.mkdir()
        (dir2 / "trigger02.bin").write_bytes(b"\x00" * 100)
        os.utime(dir2, (three_days_ago, three_days_ago))

        step = make_step(
            source_path=str(tmp_path), delete_originals=False)
        step._is_quiet_time = lambda t: True  # Always quiet for test
        compressed_count, skipped, errors = step.compress_old_files()

        assert compressed_count == 2
        assert errors == 0


class TestResumePosition:
    """Tests for _find_resume_position method."""

    def test_finds_newest_compressed_dir(self, tmp_path):
        """Returns the newest date directory name in compressed/."""
        compressed = tmp_path / "compressed"
        (compressed / "2026-01-15T10").mkdir(parents=True)
        (compressed / "2026-02-20T08").mkdir()
        (compressed / "2026-01-20T00").mkdir()

        step = make_step(source_path=str(tmp_path))
        result = step._find_resume_position(tmp_path)
        assert result == "2026-02-20T08"  # newest by sort

    def test_returns_none_when_no_compressed_dir(self, tmp_path):
        """Returns None when compressed/ doesn't exist."""
        step = make_step(source_path=str(tmp_path))
        result = step._find_resume_position(tmp_path)
        assert result is None

    def test_returns_none_when_compressed_empty(self, tmp_path):
        """Returns None when compressed/ exists but is empty."""
        (tmp_path / "compressed").mkdir()
        step = make_step(source_path=str(tmp_path))
        result = step._find_resume_position(tmp_path)
        assert result is None

    def test_ignores_non_date_dirs(self, tmp_path):
        """Skips directories that don't match YYYY-MM-DDTHH pattern."""
        compressed = tmp_path / "compressed"
        (compressed / "2026-01-15T10").mkdir(parents=True)
        (compressed / "$RECYCLE.BIN").mkdir()
        (compressed / "temp").mkdir()

        step = make_step(source_path=str(tmp_path))
        result = step._find_resume_position(tmp_path)
        assert result == "2026-01-15T10"
