"""Tests for hamma_noise module."""

import importlib.util
import json
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "hamma_noise.py"


def load_hamma_noise():
    """Load hamma_noise module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "hamma_noise", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hamma_noise():
    """Provide the hamma_noise module."""
    return load_hamma_noise()


class TestBuildParser:
    """Test argument parser construction."""

    def test_defaults(self, hamma_noise):
        parser = hamma_noise._build_parser()
        args = parser.parse_args([])
        assert args.mj_path == "/media/pi"
        assert args.count == 10
        assert args.warn_pct == 80
        assert args.output == "/tmp/noise_check.json"
        assert args.no_save is False

    def test_custom_args(self, hamma_noise):
        parser = hamma_noise._build_parser()
        args = parser.parse_args([
            "--mj-path", "/tmp/test",
            "--count", "5",
            "--warn-pct", "90",
            "--output", "/tmp/out.json",
            "--no-save",
        ])
        assert args.mj_path == "/tmp/test"
        assert args.count == 5
        assert args.warn_pct == 90
        assert args.output == "/tmp/out.json"
        assert args.no_save is True


class TestDiscoverFiles:
    """Test file discovery across DATA drives."""

    def test_finds_bin_files(self, hamma_noise, tmp_path):
        """Finds .bin files in DATA drive subdirectories."""
        drive = tmp_path / "DATA37"
        hour_dir = drive / "2026-04-23T14"
        hour_dir.mkdir(parents=True)
        f1 = hour_dir / "mj05_2026-04-23_14-00-00-000.bin"
        f2 = hour_dir / "mj05_2026-04-23_14-01-00-000.bin"
        f1.write_bytes(b"\x00" * 100)
        f2.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 2

    def test_no_drives_returns_empty(self, hamma_noise, tmp_path):
        """Returns empty list when no DATA drives exist."""
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert result == []

    def test_count_limits_files(self, hamma_noise, tmp_path):
        """Respects count limit, returning most recent first."""
        drive = tmp_path / "DATA37"
        hour_dir = drive / "2026-04-23T14"
        hour_dir.mkdir(parents=True)
        for i in range(5):
            f = hour_dir / "mj05_2026-04-23_14-0{}-00-000.bin".format(i)
            f.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=3)
        assert len(result) == 3

    def test_multiple_drives(self, hamma_noise, tmp_path):
        """Finds files across multiple DATA drives."""
        for drv in ["DATA37", "DATA38"]:
            d = tmp_path / drv / "2026-04-23T14"
            d.mkdir(parents=True)
            (d / "mj05_file.bin").write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 2

    def test_ignores_non_bin(self, hamma_noise, tmp_path):
        """Ignores .hmc and other non-.bin files."""
        drive = tmp_path / "DATA37" / "2026-04-23T14"
        drive.mkdir(parents=True)
        (drive / "mj05_file.bin").write_bytes(b"\x00" * 100)
        (drive / "mj05_file.hmc").write_bytes(b"\x00" * 100)
        (drive / "mj05_file.txt").write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 1

    def test_sorted_newest_first(self, hamma_noise, tmp_path):
        """Files are returned newest (by mtime) first."""
        import time
        drive = tmp_path / "DATA37" / "2026-04-23T14"
        drive.mkdir(parents=True)
        f_old = drive / "mj05_old.bin"
        f_old.write_bytes(b"\x00" * 100)
        time.sleep(0.05)
        f_new = drive / "mj05_new.bin"
        f_new.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert os.path.basename(result[0]) == "mj05_new.bin"


def _make_mock_header(volt, volt_fast=None, threshold=0.05):
    """Create a mock hamma.Header that returns given waveform data."""
    mock_hdr = MagicMock()
    mock_hdr.count = 1
    mock_data = MagicMock()
    mock_data.threshold = [threshold]
    mock_hdr.data = mock_data
    data_ns = SimpleNamespace(volt=volt, voltFast=volt_fast)
    mock_hdr.get_data = MagicMock(return_value=data_ns)
    return mock_hdr


class TestMeasureNoise:
    """Test noise measurement from a single trigger."""

    def test_quiet_signal(self, hamma_noise):
        """Noise of a flat signal should be near zero."""
        volt = np.zeros(30000)
        volt_fast = np.zeros(300000)
        mock_hdr = _make_mock_header(volt, volt_fast, threshold=0.05)
        with patch.object(hamma_noise, '_load_header', return_value=mock_hdr):
            result = hamma_noise.measure_noise("dummy.bin")
        assert result is not None
        assert result["threshold"] == 0.05
        assert result["slow_noise"] == pytest.approx(0.0, abs=1e-10)
        assert result["fast_noise"] == pytest.approx(0.0, abs=1e-10)
        assert result["slow_offset"] == pytest.approx(0.0, abs=1e-10)

    def test_noisy_signal(self, hamma_noise):
        """Noise of a random signal should be nonzero."""
        rng = np.random.RandomState(42)
        volt = rng.normal(0.5, 0.01, 30000)
        volt_fast = rng.normal(-0.1, 0.02, 300000)
        mock_hdr = _make_mock_header(volt, volt_fast, threshold=0.1)
        with patch.object(hamma_noise, '_load_header', return_value=mock_hdr):
            result = hamma_noise.measure_noise("dummy.bin")
        assert result["slow_noise"] > 0
        assert result["fast_noise"] > 0
        assert result["slow_offset"] == pytest.approx(0.5, abs=0.01)
        assert result["fast_offset"] == pytest.approx(-0.1, abs=0.01)

    def test_no_fast_channel(self, hamma_noise):
        """Handles triggers with no fast channel data."""
        volt = np.zeros(30000)
        mock_hdr = _make_mock_header(volt, volt_fast=None, threshold=0.05)
        with patch.object(hamma_noise, '_load_header', return_value=mock_hdr):
            result = hamma_noise.measure_noise("dummy.bin")
        assert result["slow_noise"] == pytest.approx(0.0, abs=1e-10)
        assert np.isnan(result["fast_noise"])
        assert np.isnan(result["fast_offset"])

    def test_bad_file_returns_none(self, hamma_noise):
        """Returns None when Header fails to read the file."""
        with patch.object(hamma_noise, '_load_header', side_effect=Exception("bad file")):
            result = hamma_noise.measure_noise("bad.bin")
        assert result is None


class TestExtractSensorId:
    """Test sensor ID extraction from filenames."""

    def test_standard_filename(self, hamma_noise):
        assert hamma_noise.extract_sensor_id("mj05_2026-04-23_14-00.bin") == "mj05"

    def test_path_with_directory(self, hamma_noise):
        assert hamma_noise.extract_sensor_id(
            "/media/pi/DATA37/2026-04-23T14/mj05_file.bin"
        ) == "mj05"

    def test_unknown_format(self, hamma_noise):
        assert hamma_noise.extract_sensor_id("weird.bin") == "unknown"
