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
# Test 2: offset layout range and gauge wiring (data-derived; fallback used in
# test env because no noise CSVs are present)
# ---------------------------------------------------------------------------

def test_layout_map_fast_offset_range(ns):
    # Data-derived; no noise data in test env -> symmetric fallback.
    assert ns["LAYOUT_MAP"]["fast_offset"]["range"] == [-300.0, 300.0]


def test_dcoffset_gauge_range(ns):
    gauge = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]
    assert gauge["plot_params"]["range"] == [-300, 300]


def test_dcoffset_gauge_steps(ns):
    params = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]["plot_params"]
    assert params["steps"] == [[-200, 200], ["red", "green", "red"]]


def test_offset_green_red_constant(ns):
    assert ns["OFFSET_GREEN_RED_MV"] == 200


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
# Per-sensor noise config resolver
# ---------------------------------------------------------------------------

def test_nice_dtick_basic(ns):
    nd = ns["_nice_dtick"]
    assert nd(100) == 20      # 100/5 = 20
    assert nd(0) == 1         # non-positive guard
    assert nd(-5) == 1

def test_resolve_noise_config_derived(ns):
    cfg = ns["_resolve_noise_config"](2, 83.0, overrides={})
    assert cfg["threshold_mv"] == 83.0
    assert cfg["noise_range"] == [0, 1.25 * 83.0]      # [0, 103.75]
    assert cfg["noise_dtick"] > 0

def test_resolve_noise_config_fallback(ns):
    # Missing/NaN/non-positive threshold -> 80 mV default -> [0, 100]
    for bad in (None, float("nan"), 0, -5):
        cfg = ns["_resolve_noise_config"](2, bad, overrides={})
        assert cfg["threshold_mv"] == 80.0
        assert cfg["noise_range"] == [0, 100]

def test_resolve_noise_config_override_wins(ns):
    overrides = {2: {"noise_range": [0, 250], "noise_dtick": 50,
                     "threshold_mv": 120.0}}
    cfg = ns["_resolve_noise_config"](2, 83.0, overrides=overrides)
    assert cfg["noise_range"] == [0, 250]
    assert cfg["noise_dtick"] == 50
    assert cfg["threshold_mv"] == 120.0

def test_resolve_noise_config_malformed_override_ignored(ns):
    overrides = {2: {"noise_range": "not-a-range", "noise_dtick": None}}
    cfg = ns["_resolve_noise_config"](2, 83.0, overrides=overrides)
    # falls back to threshold-derived, no raise
    assert cfg["noise_range"] == [0, 1.25 * 83.0]
    assert cfg["noise_dtick"] > 0

def test_noise_overrides_ships_empty(ns):
    assert ns["NOISE_OVERRIDES"] == {}


# ---------------------------------------------------------------------------
# DC-offset data-derived range
# ---------------------------------------------------------------------------

def test_offset_range_from_sample(ns, tmp_noise_dir):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=tmp_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=tmp_noise_dir, glob_pattern=glob_pattern)
    try:
        rng = ns["get_noise_offset_range_mv"](overrides={})
        # sample offsets 0.012/0.013 V -> 12/13 mV; m = 13 * 1.1 = 14.3
        assert rng[0] == -rng[1]
        assert abs(rng[1] - 14.3) < 0.1
    finally:
        ns["ingest_noise_data"] = orig

def test_offset_range_empty_fallback(ns, empty_noise_dir):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=empty_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=empty_noise_dir, glob_pattern=glob_pattern)
    try:
        rng = ns["get_noise_offset_range_mv"](overrides={})
        assert rng == [-300.0, 300.0]
    finally:
        ns["ingest_noise_data"] = orig

def test_offset_range_override_wins(ns):
    rng = ns["get_noise_offset_range_mv"](
        unit_n=2, overrides={2: {"offset_range": [-500, 500]}})
    assert rng == [-500.0, 500.0]

def test_offset_range_never_raises(ns):
    orig = ns["ingest_noise_data"]
    def boom(*a, **k):
        raise RuntimeError("ingest failure")
    ns["ingest_noise_data"] = boom
    try:
        assert ns["get_noise_offset_range_mv"](overrides={}) == [-300.0, 300.0]
    finally:
        ns["ingest_noise_data"] = orig

def test_offset_range_malformed_override_fallback(ns, empty_noise_dir):
    """Malformed offset_range override must not raise; falls back to no-data default.

    Fix 3: covers the branch where float() on a non-numeric element raises inside
    the try/except, causing fallback to data-derivation and then the [-300, 300]
    default (no data in empty_noise_dir).
    """
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = (
        lambda n_days=None, data_dir=empty_noise_dir,
               glob_pattern=ns["NOISE_GLOB_PATTERN"]:
        orig(n_days=n_days, data_dir=empty_noise_dir, glob_pattern=glob_pattern)
    )
    try:
        rng = ns["get_noise_offset_range_mv"](
            unit_n=2, overrides={2: {"offset_range": [-500, "x"]}})
        assert rng == [-300.0, 300.0]
    finally:
        ns["ingest_noise_data"] = orig


# ---------------------------------------------------------------------------
# Noise-floor wiring (no data in test env -> threshold 80 -> [0,100])
# ---------------------------------------------------------------------------

def test_layout_map_fast_noise_derived(ns):
    assert ns["LAYOUT_MAP"]["fast_noise"]["range"] == [0, 100]
    assert ns["LAYOUT_MAP"]["fast_noise"]["dtick"] == 20
    assert ns["LAYOUT_MAP"]["fast_noise"]["suffix"] == " mV"

def test_noisefloor_gauge_wired(ns):
    params = ns["STATUS_DASHBOARD_PLOTS"]["noisefloor"]["plot_params"]
    assert params["range"] == [0, 100]
    assert params["threshold_value"] == 80.0
    assert params["steps"] == [[80.0], ["green", "red"]]

def test_noise_color_table_is_threshold_fill(ns):
    # fast_noise band: single split at the threshold, green below / red above.
    domain, colors = ns["NOISE_COLOR_TABLE_MAP"]["fast_noise"]
    assert domain == [80.0]
    assert len(colors) == 2
    # below-threshold colour is green-ish, above is red-ish
    assert "0, 160, 0" in colors[0] or "green" in colors[0]
    assert "200, 60, 60" in colors[1] or "red" in colors[1]


# ---------------------------------------------------------------------------
# Module-exec consistency: no-data state is safe and self-consistent
# ---------------------------------------------------------------------------

def test_module_exec_no_data_is_safe_and_consistent(ns):
    # With no noise data, all derived values resolve to documented fallbacks
    # and the gauge/layout ranges agree with each other.
    noise_layout = ns["LAYOUT_MAP"]["fast_noise"]
    noise_gauge = ns["STATUS_DASHBOARD_PLOTS"]["noisefloor"]["plot_params"]
    assert noise_layout["range"] == noise_gauge["range"] == [0, 100]
    assert noise_gauge["threshold_value"] == 80.0

    offset_gauge = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]["plot_params"]
    assert offset_gauge["range"] == [-300, 300]
    assert ns["LAYOUT_MAP"]["fast_offset"]["range"] == [-300.0, 300.0]


# ---------------------------------------------------------------------------
# Fix 1: E2E wiring test with a NON-default threshold (data present)
# ---------------------------------------------------------------------------

def test_wiring_with_real_threshold(tmp_path, monkeypatch):
    """E2E wiring: execing main.py with noise data present picks up threshold.

    SAMPLE_CSV_CONTENT has threshold=0.05 V -> 50.0 mV and offsets 0.012/0.013 V
    -> max 13 mV.  All derived ranges must differ from the 80 mV no-data defaults,
    so a regression that reverts to hardcoded [0,100] would fail this test.
    """
    # 1. Build a temp HOME dir containing the noise CSV at the expected path.
    noise_dir = tmp_path / "brokkr" / "hamma" / "noise_diag"
    noise_dir.mkdir(parents=True)
    (noise_dir / "noise_hamma02_2026-06-26.csv").write_text(SAMPLE_CSV_CONTENT)

    # 2. Monkeypatch Path.home BEFORE exec so NOISE_DATA_DIR and the
    #    ingest_noise_data default both resolve to our temp tree.
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    # 3. Fresh namespace exec (do NOT reuse the module-scoped ns fixture).
    _install_stubs()
    ns2 = {}
    exec(compile(MAIN_PY.read_text(), str(MAIN_PY), "exec"), ns2)  # noqa: S102

    # 4. Assert derived wiring (threshold 50 mV -> range [0, 62.5]).
    assert ns2["LAYOUT_MAP"]["fast_noise"]["range"] == [0, 62.5], (
        "fast_noise range must be [0, 1.25*50] = [0, 62.5]")
    params = ns2["STATUS_DASHBOARD_PLOTS"]["noisefloor"]["plot_params"]
    assert params["threshold_value"] == 50.0
    assert params["range"] == [0, 62.5]
    # Color-table band split must be at the threshold.
    assert ns2["NOISE_COLOR_TABLE_MAP"]["fast_noise"][0] == [50.0]
    # Offset range: max(12, 13)*1.1 = 14.3 mV, symmetric.
    offset_range = ns2["LAYOUT_MAP"]["fast_offset"]["range"]
    assert abs(offset_range[1] - 14.3) < 0.1, (
        f"fast_offset upper range expected ~14.3, got {offset_range[1]}")
    assert offset_range[0] == -offset_range[1]


# ---------------------------------------------------------------------------
# Alarm-axis clipping fix: axis top = max(1.25*threshold, observed_max*1.1)
# ---------------------------------------------------------------------------

def test_noise_floor_max_from_sample(ns, tmp_noise_dir):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=tmp_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=tmp_noise_dir, glob_pattern=glob_pattern)
    try:
        # sample fast_noise 0.0045 V -> 4.5 mV
        assert abs(ns["get_noise_floor_max_mv"]() - 4.5) < 0.01
    finally:
        ns["ingest_noise_data"] = orig

def test_noise_floor_max_empty_default(ns, empty_noise_dir):
    orig = ns["ingest_noise_data"]
    ns["ingest_noise_data"] = lambda n_days=None, data_dir=empty_noise_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]: orig(n_days=n_days, data_dir=empty_noise_dir, glob_pattern=glob_pattern)
    try:
        assert ns["get_noise_floor_max_mv"]() == 0.0
    finally:
        ns["ingest_noise_data"] = orig

def test_noise_floor_max_never_raises(ns):
    orig = ns["ingest_noise_data"]
    def boom(*a, **k):
        raise RuntimeError("ingest failure")
    ns["ingest_noise_data"] = boom
    try:
        assert ns["get_noise_floor_max_mv"]() == 0.0
    finally:
        ns["ingest_noise_data"] = orig

def test_resolve_config_threshold_dominates_when_noise_low(ns):
    # observed max low -> 1.25*threshold wins
    cfg = ns["_resolve_noise_config"](2, 50.0, observed_max_mv=20.0, overrides={})
    assert cfg["noise_range"] == [0, 62.5]          # max(62.5, 22.0)

def test_resolve_config_axis_extends_for_alarm(ns):
    # observed max ABOVE threshold -> axis extends to keep it visible
    cfg = ns["_resolve_noise_config"](2, 50.0, observed_max_mv=90.0, overrides={})
    assert cfg["noise_range"] == [0, 99.0]          # max(62.5, 99.0)
    assert cfg["noise_dtick"] > 0

def test_resolve_config_default_observed_max_unchanged(ns):
    # default observed_max_mv=0.0 keeps the pure 1.25*threshold behavior
    cfg = ns["_resolve_noise_config"](2, 83.0, overrides={})
    assert cfg["noise_range"] == [0, 1.25 * 83.0]

def test_resolve_config_override_beats_observed_max(ns):
    cfg = ns["_resolve_noise_config"](
        2, 50.0, observed_max_mv=90.0, overrides={2: {"noise_range": [0, 250]}})
    assert cfg["noise_range"] == [0, 250]

def test_wiring_extends_axis_for_high_noise(tmp_path, monkeypatch):
    """Module E2E: a sensor whose noise floor exceeds 1.25*threshold gets an
    axis that fits it (alarm case stays visible), not a clipped [0,62.5]."""
    noise_dir = tmp_path / "brokkr" / "hamma" / "noise_diag"
    noise_dir.mkdir(parents=True)
    # threshold 0.05 V -> 50 mV; fast_noise 0.090 V -> 90 mV (above threshold)
    csv = (
        "time,trigger_time,fast_offset,fast_noise,fast_vpp,fast_snr,threshold,noise_thresh_ratio\n"
        "2026-06-26 14:23:01.123456+00:00,2026-06-26 14:23:01.000000+00:00,0.012,0.090,0.2,2.0,0.05,1.8\n"
    )
    (noise_dir / "noise_hamma02_2026-06-26.csv").write_text(csv)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    _install_stubs()
    ns2 = {}
    exec(compile(MAIN_PY.read_text(), str(MAIN_PY), "exec"), ns2)  # noqa: S102
    # 90 * 1.1 = 99.0 > 1.25*50 = 62.5  -> axis top = 99.0
    assert ns2["LAYOUT_MAP"]["fast_noise"]["range"] == [0, 99.0]
    params = ns2["STATUS_DASHBOARD_PLOTS"]["noisefloor"]["plot_params"]
    assert params["range"] == [0, 99.0]
    # threshold marker / band split stay at the threshold, not the axis top
    assert params["threshold_value"] == 50.0
    assert ns2["NOISE_COLOR_TABLE_MAP"]["fast_noise"][0] == [50.0]


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------

def test_main_py_syntax():
    import py_compile
    py_compile.compile(str(MAIN_PY), doraise=True)
