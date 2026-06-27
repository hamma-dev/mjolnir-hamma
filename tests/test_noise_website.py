"""Tests for the noise panel added to website/main.py.

Strategy: exec the real main.py with stubbed brokkr/sindri modules so we
exercise production code without RPi dependencies.
"""

import os
import sys
import types
import importlib
import textwrap
import pathlib
import pytest
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAIN_PY = pathlib.Path(__file__).parent.parent / "website" / "main.py"


def _install_stubs():
    """Install minimal brokkr/sindri stubs into sys.modules."""
    # brokkr.config.unit
    bcu = types.ModuleType("brokkr.config.unit")
    bcu.UNIT_CONFIG = {"number": 2, "name": "hamma2"}
    sys.modules.setdefault("brokkr", types.ModuleType("brokkr"))
    sys.modules.setdefault("brokkr.config", types.ModuleType("brokkr.config"))
    sys.modules["brokkr.config.unit"] = bcu

    # brokkr.config.systempath
    bcsp = types.ModuleType("brokkr.config.systempath")
    sys.modules["brokkr.config.systempath"] = bcsp

    # brokkr.utils.misc
    bum = types.ModuleType("brokkr.utils.misc")
    sys.modules.setdefault("brokkr.utils", types.ModuleType("brokkr.utils"))
    sys.modules["brokkr.utils.misc"] = bum

    # sindri.utils.misc
    sum_ = types.ModuleType("sindri.utils.misc")
    sum_.TRIGGER_SIZE_MB = 2
    sum_.WEBSITE_UPDATE_INTERVAL_S = 10
    sys.modules.setdefault("sindri", types.ModuleType("sindri"))
    sys.modules.setdefault("sindri.utils", types.ModuleType("sindri.utils"))
    sys.modules["sindri.utils.misc"] = sum_

    # sindri.website.preprocess
    swp = types.ModuleType("sindri.website.preprocess")
    # Real preprocess_subplot_params extracts plot_update_code from plot_params;
    # stub it faithfully so OVERVIEW_SUBPLOTS["weblatency"]["plot_update_code"]
    # is populated.
    def _preprocess_subplot_params(plot, subplot_params=None, **kwargs):
        result = {}
        params = plot.get("plot_params", {})
        if "plot_update_code" in params:
            result["plot_update_code"] = params["plot_update_code"]
        if subplot_params:
            result.update(subplot_params)
        return result
    swp.preprocess_subplot_params = _preprocess_subplot_params
    sys.modules.setdefault("sindri.website", types.ModuleType("sindri.website"))
    sys.modules["sindri.website.preprocess"] = swp

    # sindri.website.templates
    swt = types.ModuleType("sindri.website.templates")
    swt.GAUGE_PLOT_UPDATE_CODE = ""
    swt.GAUGE_PLOT_UPDATE_CODE_VALUE = ""
    swt.GAUGE_PLOT_UPDATE_CODE_COLOR = ""
    sys.modules["sindri.website.templates"] = swt


@pytest.fixture(scope="module")
def ns():
    """Exec main.py with stubs; return the module namespace."""
    _install_stubs()
    namespace = {}
    exec(compile(MAIN_PY.read_text(), str(MAIN_PY), "exec"), namespace)
    return namespace


# ---------------------------------------------------------------------------
# Sample CSV fixture
# ---------------------------------------------------------------------------

SAMPLE_CSV_CONTENT = textwrap.dedent("""\
    time,trigger_time,fast_offset,fast_noise,fast_vpp,fast_snr,threshold,noise_thresh_ratio
    2026-06-26 14:23:01.123456+00:00,2026-06-26 14:23:01.000000+00:00,0.012,0.0045,0.1,5.0,0.05,0.09
    2026-06-26 14:24:01.123456+00:00,2026-06-26 14:24:01.000000+00:00,0.013,0.0045,0.1,5.0,0.05,0.09
""")


@pytest.fixture()
def tmp_noise_dir(tmp_path):
    """Temp dir with one valid noise CSV."""
    csv_path = tmp_path / "noise_hamma02_2026-06-26.csv"
    csv_path.write_text(SAMPLE_CSV_CONTENT)
    return tmp_path


@pytest.fixture()
def empty_noise_dir(tmp_path):
    """Temp dir with no CSV files."""
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: block registered + no input_path
# ---------------------------------------------------------------------------

def test_noise_block_registered(ns):
    blocks = ns["SENSOR_PAGE_BLOCKS"]
    assert "noise" in blocks, "SENSOR_PAGE_BLOCKS missing 'noise' key"
    assert blocks["noise"]["type"] == "plot"


def test_noise_plot_data_args_no_input_path(ns):
    data_args = ns["NOISE_PLOT_DATA_ARGS"]
    assert "input_path" not in data_args, \
        "NOISE_PLOT_DATA_ARGS must not have 'input_path' (noise self-loads)"


# ---------------------------------------------------------------------------
# Test 2: offset layout range [0, 1000] on LAYOUT_MAP and gauge
# ---------------------------------------------------------------------------

def test_layout_map_fast_offset_range(ns):
    assert ns["LAYOUT_MAP"]["fast_offset"]["range"] == [0, 1000]


def test_dcoffset_gauge_range(ns):
    gauge = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]
    assert gauge["plot_params"]["range"] == [0, 1000]


# ---------------------------------------------------------------------------
# Test 3: loader sample -> mV conversion
# ---------------------------------------------------------------------------

def test_ingest_noise_data_sample(ns, tmp_noise_dir, monkeypatch):
    monkeypatch.setitem(ns, "NOISE_DATA_DIR", tmp_noise_dir)
    df = ns["ingest_noise_data"](data_dir=tmp_noise_dir)
    # DatetimeIndex
    assert isinstance(df.index, pd.DatetimeIndex)
    # tz-naive
    assert df.index.tz is None
    # float dtype
    assert pd.api.types.is_float_dtype(df["fast_noise"])
    assert pd.api.types.is_float_dtype(df["fast_offset"])


def test_preprocess_mv_conversion(ns, tmp_noise_dir, monkeypatch):
    monkeypatch.setitem(ns, "NOISE_DATA_DIR", tmp_noise_dir)
    # Monkeypatch ingest_noise_data to use tmp_noise_dir
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=tmp_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=tmp_noise_dir, glob_pattern=glob_pattern)
    try:
        result = ns["_noise_plot_preprocess"](pd.DataFrame())
        # fast_noise: 0.0045 V -> 4.5 mV
        assert abs(result["fast_noise"].iloc[-1] - 4.5) < 0.01, \
            f"Expected fast_noise ~4.5 mV, got {result['fast_noise'].iloc[-1]}"
        # fast_offset: 0.012 V -> 12.0 mV
        assert abs(result["fast_offset"].iloc[0] - 12.0) < 0.01, \
            f"Expected fast_offset ~12.0 mV, got {result['fast_offset'].iloc[0]}"
    finally:
        ns["ingest_noise_data"] = orig


def test_gauge_variable_mv_conversion(ns, tmp_noise_dir, monkeypatch):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=tmp_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=tmp_noise_dir, glob_pattern=glob_pattern)
    try:
        gauge_var = ns["STATUS_DASHBOARD_PLOTS"]["noisefloor"]["plot_data"]["variable"]
        series = gauge_var(pd.DataFrame())
        last_val = series.iloc[-1]
        assert abs(last_val - 4.5) < 0.01, \
            f"Expected gauge last value ~4.5 mV, got {last_val}"
    finally:
        ns["ingest_noise_data"] = orig


# ---------------------------------------------------------------------------
# Test 4: empty path -> float dtype + DatetimeIndex (prevents sindri crashes)
# ---------------------------------------------------------------------------

def test_preprocess_empty_dir_float_dtype(ns, empty_noise_dir, monkeypatch):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=empty_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=empty_noise_dir, glob_pattern=glob_pattern)
    try:
        df = ns["_noise_plot_preprocess"](pd.DataFrame())
        assert len(df) == 0, "Expected empty DataFrame"
        assert isinstance(df.index, pd.DatetimeIndex), \
            "Empty frame must have DatetimeIndex (prevents strftime crash)"
        assert pd.api.types.is_float_dtype(df["fast_noise"]), \
            "fast_noise must be float dtype (prevents np.isfinite crash)"
        assert pd.api.types.is_float_dtype(df["fast_offset"]), \
            "fast_offset must be float dtype (prevents np.isfinite crash)"
    finally:
        ns["ingest_noise_data"] = orig


# ---------------------------------------------------------------------------
# Test 5: corrupt path -> guard returns empty frame, no raise
# ---------------------------------------------------------------------------

def test_preprocess_raises_guard(ns):
    orig = ns["ingest_noise_data"]
    def boom(*args, **kwargs):
        raise RuntimeError("simulated ingest failure")
    ns["ingest_noise_data"] = boom
    try:
        # Must not raise
        df = ns["_noise_plot_preprocess"](pd.DataFrame())
        assert isinstance(df.index, pd.DatetimeIndex), \
            "Guard return must have DatetimeIndex"
        assert pd.api.types.is_float_dtype(df["fast_noise"]), \
            "Guard return fast_noise must be float"
    finally:
        ns["ingest_noise_data"] = orig


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------

def test_main_py_syntax():
    import py_compile
    py_compile.compile(str(MAIN_PY), doraise=True)
