# hamma_noise.py Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On-demand sensor noise check script that runs on mj Pi, analyzes recent triggers, and reports noise levels relative to the AGS threshold.

**Architecture:** Single standalone script at `scripts/hamma_noise.py`. Uses `hamma.Header` to read `.bin` files one at a time, computes noise/offset stats per trigger, aggregates with median/max/IQR, compares max noise to threshold. Prints human-readable report, optionally saves JSON.

**Tech Stack:** Python 3.6+, `hamma` package (Header class), numpy, argparse, json, glob

**Spec:** `docs/superpowers/specs/2026-04-23-hamma-noise-check-design.md`

---

## Files

- **Create:** `scripts/hamma_noise.py` — the noise check script
- **Create:** `tests/python/test_hamma_noise.py` — unit tests

## Chunk 1: Core noise measurement and file discovery

### Task 1: Script skeleton with arg parsing and file discovery

**Files:**
- Create: `scripts/hamma_noise.py`
- Create: `tests/python/test_hamma_noise.py`

- [ ] **Step 1: Write tests for arg parsing and file discovery**

Create `tests/python/test_hamma_noise.py`. Use `importlib.util` to load the script module (same pattern as `test_hamma_scrub.py`).

```python
"""Tests for hamma_noise module."""

import importlib.util
import json
import os
import pathlib

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/bitzer/Documents/insync/uah_gdrive/programming/python/hamma_sensor_repos/mjolnir-hamma && conda run -n sci pytest tests/python/test_hamma_noise.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the script skeleton with parser and file discovery**

Create `scripts/hamma_noise.py`:

```python
#!/usr/bin/env python3
"""On-demand sensor noise level check.

Analyzes recent trigger files on a HAMMA mj Pi and reports noise levels
relative to the AGS trigger threshold.

Usage:
    python hamma_noise.py [--mj-path PATH] [--count N] [--warn-pct N]
"""

import argparse
import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_MJ_PATH = "/media/pi"
DRIVE_PATTERN = "DATA??"
DEFAULT_COUNT = 10
DEFAULT_WARN_PCT = 80
DEFAULT_OUTPUT = "/tmp/noise_check.json"

# Noise measurement constants (from hamma.header.core._diagnostic_data)
MEDSIZE = 20000  # samples for slow channel offset/noise window
NOISE_PERCENTILES = [0.01, 99.9]

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1


def discover_files(mj_path, count):
    """Find the most recent .bin trigger files across DATA drives.

    Parameters
    ----------
    mj_path : str
        Base path containing DATA?? drives (e.g., /media/pi).
    count : int
        Maximum number of files to return.

    Returns
    -------
    list of str
        File paths sorted by modification time (newest first).
    """
    pattern = os.path.join(mj_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        logger.warning("No DATA drives found at %s", mj_path)
        return []

    bin_files = []
    for drive in drives:
        found = glob.glob(os.path.join(drive, "*", "*.bin"))
        bin_files.extend(found)

    # Sort by modification time, newest first
    bin_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return bin_files[:count]


def _build_parser():
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Check sensor noise levels from recent trigger files.",
    )
    parser.add_argument(
        "--mj-path", default=DEFAULT_MJ_PATH,
        help="Base path for DATA drive discovery (default: %(default)s)",
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help="Number of most recent files to analyze (default: %(default)s)",
    )
    parser.add_argument(
        "--warn-pct", type=int, default=DEFAULT_WARN_PCT,
        help="Noise/threshold %% that triggers a warning (default: %(default)s)",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="Path for JSON results file (default: %(default)s)",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="Skip saving JSON, print only",
    )
    return parser


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if os.environ.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    rc = run(
        mj_path=args.mj_path,
        count=args.count,
        warn_pct=args.warn_pct,
        output=args.output,
        no_save=args.no_save,
    )
    sys.exit(rc)


def run(mj_path, count, warn_pct, output, no_save):
    """Main logic. Returns exit code."""
    # Placeholder — implemented in Task 2
    return EXIT_OK


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/bitzer/Documents/insync/uah_gdrive/programming/python/hamma_sensor_repos/mjolnir-hamma && conda run -n sci pytest tests/python/test_hamma_noise.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_noise.py tests/python/test_hamma_noise.py
git commit -m "feat(noise): add script skeleton with arg parsing and file discovery"
```

---

### Task 2: Noise measurement and threshold extraction

**Files:**
- Modify: `scripts/hamma_noise.py`
- Modify: `tests/python/test_hamma_noise.py`

- [ ] **Step 1: Write tests for noise measurement**

The `measure_noise` function takes a single `.bin` file path, reads it with `hamma.Header`, extracts waveform data and threshold, and returns noise/offset stats. Since we can't create real `.bin` files easily in tests, mock `hamma.Header`.

Add to `tests/python/test_hamma_noise.py`:

```python
from unittest.mock import patch, MagicMock
from types import SimpleNamespace


def _make_mock_header(volt, volt_fast=None, threshold=0.05):
    """Create a mock hamma.Header that returns given waveform data.

    Parameters
    ----------
    volt : np.ndarray
        Slow channel voltage data.
    volt_fast : np.ndarray or None
        Fast channel voltage data.
    threshold : float
        AGS threshold in volts.
    """
    mock_hdr = MagicMock()
    mock_hdr.count = 1

    # Header.data is a DataFrame-like with threshold column
    mock_data = MagicMock()
    mock_data.threshold = [threshold]
    mock_hdr.data = mock_data

    # Header.get_data returns SimpleNamespace with volt, voltFast
    data_ns = SimpleNamespace(volt=volt, voltFast=volt_fast)
    mock_hdr.get_data = MagicMock(return_value=data_ns)

    return mock_hdr


class TestMeasureNoise:
    """Test noise measurement from a single trigger."""

    def test_quiet_signal(self, hamma_noise):
        """Noise of a flat signal should be near zero."""
        volt = np.zeros(30000)  # > MEDSIZE
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestMeasureNoise -v`
Expected: FAIL — `measure_noise` and `_load_header` not defined

- [ ] **Step 3: Implement noise measurement functions**

Add to `scripts/hamma_noise.py`, before `_build_parser()`:

```python
def _load_header(filepath):
    """Load a single .bin file via hamma.Header.

    Parameters
    ----------
    filepath : str
        Path to .bin trigger file.

    Returns
    -------
    hamma.Header
        Header object with one trigger loaded.
    """
    import hamma
    return hamma.Header(filepath)


def extract_sensor_id(filepath):
    """Extract sensor ID from a .bin filename.

    Parameters
    ----------
    filepath : str
        Path or filename like 'mj05_2026-04-23_14-00-00-000.bin'.

    Returns
    -------
    str
        Sensor ID (e.g., 'mj05') or 'unknown'.
    """
    basename = os.path.basename(filepath)
    parts = basename.split("_")
    if len(parts) >= 2:
        return parts[0]
    return "unknown"


def measure_noise(filepath):
    """Measure noise and offset from a single trigger file.

    Parameters
    ----------
    filepath : str
        Path to .bin trigger file.

    Returns
    -------
    dict or None
        Dict with keys: threshold, slow_noise, slow_offset,
        fast_noise, fast_offset. Returns None on read failure.
    """
    try:
        hdr = _load_header(filepath)
    except Exception as e:
        logger.warning("Failed to read %s: %s", filepath, e)
        return None

    data = hdr.get_data(0, noTimes=True)
    threshold = float(hdr.data.threshold[0])

    # Slow channel
    perc = np.percentile(data.volt[0:MEDSIZE], NOISE_PERCENTILES)
    slow_noise = perc[1] - perc[0]
    slow_offset = np.median(data.volt[0:MEDSIZE])

    # Fast channel
    if data.voltFast is not None:
        fast_med_size = MEDSIZE * 10
        perc_fast = np.percentile(data.voltFast[0:fast_med_size], NOISE_PERCENTILES)
        fast_noise = perc_fast[1] - perc_fast[0]
        fast_offset = np.median(data.voltFast[0:fast_med_size])
    else:
        fast_noise = np.nan
        fast_offset = np.nan

    return {
        "threshold": threshold,
        "slow_noise": float(slow_noise),
        "slow_offset": float(slow_offset),
        "fast_noise": float(fast_noise),
        "fast_offset": float(fast_offset),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestMeasureNoise tests/python/test_hamma_noise.py::TestExtractSensorId -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_noise.py tests/python/test_hamma_noise.py
git commit -m "feat(noise): add noise measurement and sensor ID extraction"
```

---

### Task 3: Aggregation and threshold comparison

**Files:**
- Modify: `scripts/hamma_noise.py`
- Modify: `tests/python/test_hamma_noise.py`

- [ ] **Step 1: Write tests for aggregation**

Add to `tests/python/test_hamma_noise.py`:

```python
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
        # Median of [0.002, 0.003, 0.004, 0.006] = 0.0035
        assert agg["slow"]["noise_vpp_median"] == pytest.approx(0.0035)
        assert agg["slow"]["noise_vpp_max"] == pytest.approx(0.006)
        # IQR: Q75=0.0045, Q25=0.00275 -> 0.00175
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestAggregateResults tests/python/test_hamma_noise.py::TestCheckWarnings -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement aggregation and warning functions**

Add to `scripts/hamma_noise.py`, before `_build_parser()`:

```python
def aggregate_results(results):
    """Aggregate per-trigger noise measurements.

    Parameters
    ----------
    results : list of dict
        Each dict from measure_noise() with keys: threshold,
        slow_noise, slow_offset, fast_noise, fast_offset.

    Returns
    -------
    dict
        Aggregated stats with threshold_V, slow{}, fast{} sub-dicts.
    """
    thresholds = np.array([r["threshold"] for r in results])
    threshold = float(np.median(thresholds))

    def _channel_stats(key_noise, key_offset):
        noise_vals = np.array([r[key_noise] for r in results])
        offset_vals = np.array([r[key_offset] for r in results])

        # Check for all-NaN (no data for this channel)
        if np.all(np.isnan(noise_vals)):
            return {
                "noise_vpp_median": None,
                "noise_vpp_max": None,
                "noise_vpp_iqr": None,
                "offset_median": None,
                "offset_max": None,
                "offset_iqr": None,
                "noise_thresh_pct": None,
            }

        q25, q75 = np.percentile(noise_vals, [25, 75])
        oq25, oq75 = np.percentile(offset_vals, [25, 75])
        noise_max = float(np.max(noise_vals))

        if threshold > 0:
            noise_thresh_pct = round(100.0 * noise_max / threshold, 1)
        else:
            noise_thresh_pct = None

        return {
            "noise_vpp_median": float(np.median(noise_vals)),
            "noise_vpp_max": noise_max,
            "noise_vpp_iqr": float(q75 - q25),
            "offset_median": float(np.median(offset_vals)),
            "offset_max": float(np.max(offset_vals)),
            "offset_iqr": float(oq75 - oq25),
            "noise_thresh_pct": noise_thresh_pct,
        }

    return {
        "threshold_V": threshold,
        "slow": _channel_stats("slow_noise", "slow_offset"),
        "fast": _channel_stats("fast_noise", "fast_offset"),
    }


def check_warnings(agg, warn_pct):
    """Check if noise levels exceed warning threshold.

    Parameters
    ----------
    agg : dict
        Aggregated results from aggregate_results().
    warn_pct : int
        Warning percentage threshold.

    Returns
    -------
    list of str
        Warning messages (empty if OK).
    """
    warnings = []
    for channel in ["slow", "fast"]:
        pct = agg[channel].get("noise_thresh_pct")
        if pct is not None and pct >= warn_pct:
            warnings.append(
                "{} channel noise at {:.0f}% of threshold".format(channel, pct)
            )
    return warnings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestAggregateResults tests/python/test_hamma_noise.py::TestCheckWarnings -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_noise.py tests/python/test_hamma_noise.py
git commit -m "feat(noise): add aggregation and threshold warning logic"
```

---

## Chunk 2: Output formatting and run() integration

### Task 4: Human-readable output formatting

**Files:**
- Modify: `scripts/hamma_noise.py`
- Modify: `tests/python/test_hamma_noise.py`

- [ ] **Step 1: Write tests for output formatting**

Add to `tests/python/test_hamma_noise.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestFormatReport -v`
Expected: FAIL — `format_report` not defined

- [ ] **Step 3: Implement report formatting**

Add to `scripts/hamma_noise.py`, before `_build_parser()`:

```python
def format_report(sensor_id, files_analyzed, agg, warnings):
    """Format human-readable noise report.

    Parameters
    ----------
    sensor_id : str
        Sensor identifier (e.g., 'mj05').
    files_analyzed : int
        Number of files successfully analyzed.
    agg : dict
        Aggregated results from aggregate_results().
    warnings : list of str
        Warning messages.

    Returns
    -------
    str
        Formatted report string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append("=== Noise Check: {} | {} ===".format(sensor_id, now))
    lines.append("Files analyzed: {}".format(files_analyzed))
    lines.append("Threshold: {:.4f}V".format(agg["threshold_V"]))
    lines.append("")

    # Noise table
    hdr = "{:<10s} {:>12s} {:>10s} {:>10s} {:>18s}".format(
        "Channel", "Median(Vpp)", "Max(Vpp)", "IQR(Vpp)", "Noise/Thresh(max)")
    lines.append(hdr)
    for ch in ["slow", "fast"]:
        s = agg[ch]
        if s["noise_vpp_median"] is None:
            lines.append("{:<10s} {:>12s} {:>10s} {:>10s} {:>18s}".format(
                ch, "N/A", "N/A", "N/A", "N/A"))
        else:
            pct_str = "{:.1f}%".format(s["noise_thresh_pct"]) if s["noise_thresh_pct"] is not None else "N/A"
            lines.append("{:<10s} {:>11.4f}V {:>9.4f}V {:>9.4f}V {:>17s}".format(
                ch, s["noise_vpp_median"], s["noise_vpp_max"],
                s["noise_vpp_iqr"], pct_str))
    lines.append("")

    # Offset table
    hdr2 = "{:<10s} {:>12s} {:>10s} {:>10s}".format(
        "Channel", "Median(Off)", "Max(Off)", "IQR(Off)")
    lines.append(hdr2)
    for ch in ["slow", "fast"]:
        s = agg[ch]
        if s["offset_median"] is None:
            lines.append("{:<10s} {:>12s} {:>10s} {:>10s}".format(
                ch, "N/A", "N/A", "N/A"))
        else:
            lines.append("{:<10s} {:>11.4f}V {:>9.4f}V {:>9.4f}V".format(
                ch, s["offset_median"], s["offset_max"], s["offset_iqr"]))
    lines.append("")

    # Status
    if warnings:
        for w in warnings:
            lines.append("Status: WARNING - {}".format(w))
    else:
        lines.append("Status: OK")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestFormatReport -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_noise.py tests/python/test_hamma_noise.py
git commit -m "feat(noise): add human-readable report formatting"
```

---

### Task 5: Wire up run() and JSON output

**Files:**
- Modify: `scripts/hamma_noise.py`
- Modify: `tests/python/test_hamma_noise.py`

- [ ] **Step 1: Write tests for run() integration**

Add to `tests/python/test_hamma_noise.py`:

```python
class TestRun:
    """Test the run() function end-to-end with mocked Header."""

    def _make_results(self, n=3, threshold=0.05):
        """Helper to create n mock measure_noise results."""
        rng = np.random.RandomState(42)
        results = []
        for _ in range(n):
            results.append({
                "threshold": threshold,
                "slow_noise": 0.002 + rng.uniform(0, 0.002),
                "slow_offset": 0.01 + rng.uniform(0, 0.005),
                "fast_noise": 0.010 + rng.uniform(0, 0.005),
                "fast_offset": rng.uniform(-0.003, 0.003),
            })
        return results

    def test_run_ok(self, hamma_noise, tmp_path):
        """Successful run returns EXIT_OK and saves JSON."""
        results = self._make_results()
        files = ["mj05_a.bin", "mj05_b.bin", "mj05_c.bin"]
        output = str(tmp_path / "noise.json")

        with patch.object(hamma_noise, 'discover_files', return_value=files), \
             patch.object(hamma_noise, 'measure_noise', side_effect=results):
            rc = hamma_noise.run(
                mj_path="/tmp", count=10, warn_pct=80,
                output=output, no_save=False,
            )

        assert rc == hamma_noise.EXIT_OK
        assert os.path.exists(output)
        with open(output) as f:
            data = json.load(f)
        assert data["sensor"] == "mj05"
        assert data["files_analyzed"] == 3
        assert "slow" in data
        assert "fast" in data

    def test_run_no_files(self, hamma_noise, tmp_path, capsys):
        """Returns EXIT_ERROR when no files found."""
        with patch.object(hamma_noise, 'discover_files', return_value=[]):
            rc = hamma_noise.run(
                mj_path="/tmp", count=10, warn_pct=80,
                output=str(tmp_path / "noise.json"), no_save=False,
            )
        assert rc == hamma_noise.EXIT_ERROR

    def test_run_all_files_fail(self, hamma_noise, tmp_path):
        """Returns EXIT_ERROR when all files fail to read."""
        files = ["mj05_a.bin", "mj05_b.bin"]
        with patch.object(hamma_noise, 'discover_files', return_value=files), \
             patch.object(hamma_noise, 'measure_noise', return_value=None):
            rc = hamma_noise.run(
                mj_path="/tmp", count=10, warn_pct=80,
                output=str(tmp_path / "noise.json"), no_save=False,
            )
        assert rc == hamma_noise.EXIT_ERROR

    def test_run_no_save(self, hamma_noise, tmp_path):
        """With --no-save, JSON file is not created."""
        results = self._make_results(n=1)
        output = str(tmp_path / "noise.json")
        with patch.object(hamma_noise, 'discover_files', return_value=["mj05_a.bin"]), \
             patch.object(hamma_noise, 'measure_noise', side_effect=results):
            rc = hamma_noise.run(
                mj_path="/tmp", count=10, warn_pct=80,
                output=output, no_save=True,
            )
        assert rc == hamma_noise.EXIT_OK
        assert not os.path.exists(output)

    def test_run_some_files_fail(self, hamma_noise, tmp_path):
        """Partial failures still produce a report from remaining files."""
        results = [None, self._make_results(n=1)[0]]
        files = ["mj05_bad.bin", "mj05_good.bin"]
        output = str(tmp_path / "noise.json")
        with patch.object(hamma_noise, 'discover_files', return_value=files), \
             patch.object(hamma_noise, 'measure_noise', side_effect=results):
            rc = hamma_noise.run(
                mj_path="/tmp", count=10, warn_pct=80,
                output=output, no_save=False,
            )
        assert rc == hamma_noise.EXIT_OK
        with open(output) as f:
            data = json.load(f)
        assert data["files_analyzed"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py::TestRun -v`
Expected: FAIL — run() is still a placeholder

- [ ] **Step 3: Implement run() function**

Replace the placeholder `run()` in `scripts/hamma_noise.py`:

```python
def run(mj_path, count, warn_pct, output, no_save):
    """Run noise check and produce report.

    Parameters
    ----------
    mj_path : str
        Base path for DATA drive discovery.
    count : int
        Number of most recent files to analyze.
    warn_pct : int
        Warning percentage threshold.
    output : str
        Path for JSON output file.
    no_save : bool
        If True, skip saving JSON.

    Returns
    -------
    int
        Exit code (EXIT_OK or EXIT_ERROR).
    """
    # Discover files
    files = discover_files(mj_path, count)
    if not files:
        print("ERROR: No .bin files found under {}".format(mj_path))
        return EXIT_ERROR

    # Measure each file
    results = []
    failures = 0
    for filepath in files:
        result = measure_noise(filepath)
        if result is None:
            failures += 1
        else:
            results.append(result)

    if not results:
        print("ERROR: All {} files failed to read".format(len(files)))
        return EXIT_ERROR

    if failures > 0:
        logger.warning("%d of %d files failed to read", failures, len(files))

    # Extract sensor ID from first successful file
    sensor_id = extract_sensor_id(files[0])

    # Aggregate and check warnings
    agg = aggregate_results(results)
    warnings = check_warnings(agg, warn_pct)

    # Print report
    report = format_report(sensor_id, len(results), agg, warnings)
    print(report)

    # Save JSON
    if not no_save:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        json_data = {
            "sensor": sensor_id,
            "timestamp": now,
            "files_analyzed": len(results),
            "threshold_V": agg["threshold_V"],
            "slow": agg["slow"],
            "fast": agg["fast"],
            "status": "WARNING" if warnings else "OK",
            "warnings": warnings,
        }
        try:
            with open(output, "w") as f:
                json.dump(json_data, f, indent=2)
            logger.info("Results saved to %s", output)
        except IOError as e:
            logger.warning("Could not save JSON: %s", e)

    return EXIT_OK
```

- [ ] **Step 4: Run all tests**

Run: `conda run -n sci pytest tests/python/test_hamma_noise.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_noise.py tests/python/test_hamma_noise.py
git commit -m "feat(noise): wire up run() with JSON output — script complete"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run the full test suite for the repo**

Run: `cd /Users/bitzer/Documents/insync/uah_gdrive/programming/python/hamma_sensor_repos/mjolnir-hamma && conda run -n sci pytest tests/python/ -v`
Expected: All tests pass (existing + new)

- [ ] **Step 2: Verify the script runs with --help**

Run: `conda run -n sci python scripts/hamma_noise.py --help`
Expected: Usage message with all documented options

- [ ] **Step 3: Commit spec and plan (if not already committed)**

```bash
git add docs/superpowers/specs/2026-04-23-hamma-noise-check-design.md docs/superpowers/plans/2026-04-23-hamma-noise-check.md
git commit -m "docs: add noise check spec and implementation plan"
```
