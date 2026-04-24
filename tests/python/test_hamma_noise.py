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


class TestAggregateResults:
    """Test aggregation of per-trigger noise measurements."""

    def test_basic_aggregation(self, hamma_noise):
        """Computes median, max, IQR for noise and offset."""
        results = [
            {"threshold": 0.05, "slow_noise": 0.002, "slow_offset": 0.01,
             "fast_noise": 0.010, "fast_offset": -0.001},
            {"threshold": 0.05, "slow_noise": 0.004, "slow_offset": 0.02,
             "fast_noise": 0.020, "fast_offset": -0.003},
            {"threshold": 0.05, "slow_noise": 0.003, "slow_offset": 0.015,
             "fast_noise": 0.015, "fast_offset": -0.002},
            {"threshold": 0.05, "slow_noise": 0.006, "slow_offset": 0.018,
             "fast_noise": 0.030, "fast_offset": 0.001},
        ]
        agg = hamma_noise.aggregate_results(results)

        assert agg["threshold_V"] == pytest.approx(0.05)
        assert agg["slow"]["noise_vpp_median"] == pytest.approx(0.0035)
        assert agg["slow"]["noise_vpp_max"] == pytest.approx(0.006)
        assert agg["slow"]["noise_vpp_iqr"] == pytest.approx(0.00175)

    def test_threshold_varies_uses_median(self, hamma_noise):
        """When thresholds differ, uses median."""
        results = [
            {"threshold": 0.04, "slow_noise": 0.002, "slow_offset": 0.01,
             "fast_noise": 0.01, "fast_offset": 0.0},
            {"threshold": 0.06, "slow_noise": 0.002, "slow_offset": 0.01,
             "fast_noise": 0.01, "fast_offset": 0.0},
        ]
        agg = hamma_noise.aggregate_results(results)
        assert agg["threshold_V"] == pytest.approx(0.05)

    def test_zero_threshold(self, hamma_noise):
        """Zero threshold produces None for noise_thresh_pct."""
        results = [
            {"threshold": 0.0, "slow_noise": 0.002, "slow_offset": 0.01,
             "fast_noise": 0.01, "fast_offset": 0.0},
        ]
        agg = hamma_noise.aggregate_results(results)
        assert agg["slow"]["noise_thresh_pct"] is None
        assert agg["fast"]["noise_thresh_pct"] is None

    def test_nan_fast_channel(self, hamma_noise):
        """Handles NaN fast channel values (no fast data)."""
        results = [
            {"threshold": 0.05, "slow_noise": 0.002, "slow_offset": 0.01,
             "fast_noise": float("nan"), "fast_offset": float("nan")},
            {"threshold": 0.05, "slow_noise": 0.003, "slow_offset": 0.02,
             "fast_noise": float("nan"), "fast_offset": float("nan")},
        ]
        agg = hamma_noise.aggregate_results(results)
        assert agg["fast"]["noise_vpp_median"] is None
        assert agg["fast"]["noise_thresh_pct"] is None


class TestCheckWarnings:
    """Test warning generation."""

    def test_ok_status(self, hamma_noise):
        """No warnings when noise is well below threshold."""
        agg = {
            "threshold_V": 0.05,
            "slow": {"noise_vpp_max": 0.005, "noise_thresh_pct": 10.0},
            "fast": {"noise_vpp_max": 0.020, "noise_thresh_pct": 40.0},
        }
        warnings = hamma_noise.check_warnings(agg, warn_pct=80)
        assert warnings == []

    def test_warning_triggered(self, hamma_noise):
        """Warning when noise exceeds warn_pct of threshold."""
        agg = {
            "threshold_V": 0.05,
            "slow": {"noise_vpp_max": 0.005, "noise_thresh_pct": 10.0},
            "fast": {"noise_vpp_max": 0.045, "noise_thresh_pct": 90.0},
        }
        warnings = hamma_noise.check_warnings(agg, warn_pct=80)
        assert len(warnings) == 1
        assert "fast" in warnings[0].lower()

    def test_no_warning_when_pct_is_none(self, hamma_noise):
        """No warning when threshold was zero (pct is None)."""
        agg = {
            "threshold_V": 0.0,
            "slow": {"noise_vpp_max": 0.005, "noise_thresh_pct": None},
            "fast": {"noise_vpp_max": 0.020, "noise_thresh_pct": None},
        }
        warnings = hamma_noise.check_warnings(agg, warn_pct=80)
        assert warnings == []


class TestFormatReport:
    """Test human-readable report formatting."""

    def test_basic_report(self, hamma_noise):
        """Report contains expected sections."""
        agg = {
            "threshold_V": 0.042,
            "slow": {
                "noise_vpp_median": 0.003, "noise_vpp_max": 0.005,
                "noise_vpp_iqr": 0.001, "offset_median": 0.015,
                "offset_max": 0.018, "offset_iqr": 0.002,
                "noise_thresh_pct": 11.9,
            },
            "fast": {
                "noise_vpp_median": 0.012, "noise_vpp_max": 0.028,
                "noise_vpp_iqr": 0.006, "offset_median": -0.002,
                "offset_max": 0.004, "offset_iqr": 0.003,
                "noise_thresh_pct": 66.7,
            },
        }
        report = hamma_noise.format_report(
            sensor_id="mj05", files_analyzed=8,
            agg=agg, warnings=[],
        )
        assert "mj05" in report
        assert "Threshold:" in report
        assert "0.042" in report
        assert "Status: OK" in report
        assert "Files analyzed: 8" in report

    def test_report_with_warning(self, hamma_noise):
        """Report shows WARNING status."""
        agg = {
            "threshold_V": 0.05,
            "slow": {
                "noise_vpp_median": 0.003, "noise_vpp_max": 0.005,
                "noise_vpp_iqr": 0.001, "offset_median": 0.015,
                "offset_max": 0.018, "offset_iqr": 0.002,
                "noise_thresh_pct": 10.0,
            },
            "fast": {
                "noise_vpp_median": 0.040, "noise_vpp_max": 0.046,
                "noise_vpp_iqr": 0.003, "offset_median": 0.0,
                "offset_max": 0.002, "offset_iqr": 0.001,
                "noise_thresh_pct": 92.0,
            },
        }
        warnings = ["fast channel noise at 92% of threshold"]
        report = hamma_noise.format_report(
            sensor_id="mj05", files_analyzed=10,
            agg=agg, warnings=warnings,
        )
        assert "WARNING" in report
        assert "92%" in report

    def test_report_none_pct(self, hamma_noise):
        """Report handles None noise_thresh_pct (zero threshold)."""
        agg = {
            "threshold_V": 0.0,
            "slow": {
                "noise_vpp_median": 0.003, "noise_vpp_max": 0.005,
                "noise_vpp_iqr": 0.001, "offset_median": 0.015,
                "offset_max": 0.018, "offset_iqr": 0.002,
                "noise_thresh_pct": None,
            },
            "fast": {
                "noise_vpp_median": 0.012, "noise_vpp_max": 0.028,
                "noise_vpp_iqr": 0.006, "offset_median": -0.002,
                "offset_max": 0.004, "offset_iqr": 0.003,
                "noise_thresh_pct": None,
            },
        }
        report = hamma_noise.format_report(
            sensor_id="mj05", files_analyzed=5,
            agg=agg, warnings=[],
        )
        assert "N/A" in report
