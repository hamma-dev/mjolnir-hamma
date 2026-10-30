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
    # Pin "now" just after the sample's last row so it is not stale.
    monkeypatch.setitem(ns, "_noise_now",
                        lambda: pd.Timestamp("2026-06-26 14:25:00"))
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
# Fix A — bug_002: tolerate one bad timestamp in ingest_noise_data
# ---------------------------------------------------------------------------

def test_ingest_tolerates_one_bad_timestamp(ns, tmp_path):
    csv = (
        "time,trigger_time,fast_offset,fast_noise,fast_vpp,fast_snr,threshold,noise_thresh_ratio\n"
        "2026-06-26 14:23:01.123456+00:00,2026-06-26 14:23:01.000000+00:00,0.012,0.0045,0.1,5.0,0.05,0.09\n"
        "BADSTAMP,2026-06-26 14:24:01.000000+00:00,0.013,0.0046,0.1,5.0,0.05,0.09\n"
        "2026-06-26 14:25:01.123456+00:00,2026-06-26 14:25:01.000000+00:00,0.014,0.0047,0.1,5.0,0.05,0.09\n"
    )
    (tmp_path / "noise_hamma02_2026-06-26.csv").write_text(csv)
    df = ns["ingest_noise_data"](data_dir=tmp_path)
    assert len(df) == 2                       # bad row dropped, 2 valid remain, no raise
    assert isinstance(df.index, pd.DatetimeIndex)


# ---------------------------------------------------------------------------
# Fix B — bug_004: DC-offset gauge delta colors bipolar convention
# ---------------------------------------------------------------------------

def test_dcoffset_gauge_delta_colors_bipolar(ns):
    params = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]["plot_params"]
    assert params["decreasing_color"] == "blue"
    assert params["increasing_color"] == "orange"


# ---------------------------------------------------------------------------
# Fix C — bug_005: dead STANDARD_LAYOUTS / LAYOUT_MAP noise entries removed
# ---------------------------------------------------------------------------

def test_standard_layouts_has_no_dead_noise_entries(ns):
    assert "noise_floor_mv" not in ns["STANDARD_LAYOUTS"]
    assert "dc_offset_mv" not in ns["STANDARD_LAYOUTS"]
    # wiring still provides the fast_* layout keys
    assert "fast_noise" in ns["LAYOUT_MAP"]
    assert "fast_offset" in ns["LAYOUT_MAP"]


# ---------------------------------------------------------------------------
# Fix D — bug_006: validate override ranges + clamp threshold marker on-axis
# ---------------------------------------------------------------------------

def test_resolve_config_rejects_nonfinite_or_inverted_override_range(ns):
    for bad in ([float("nan"), 100], [0, float("inf")], [100, 50]):
        cfg = ns["_resolve_noise_config"](2, 83.0, overrides={2: {"noise_range": bad}})
        assert cfg["noise_range"] == [0, 1.25 * 83.0]     # fell through to derived

def test_offset_range_rejects_nonfinite_or_inverted_override(ns):
    for bad in ([float("nan"), 100], [500, -500], [0, float("inf")]):
        rng = ns["get_noise_offset_range_mv"](unit_n=2, overrides={2: {"offset_range": bad}})
        assert rng == [-300.0, 300.0]                     # rejected -> default (no data)

def test_clamp_threshold_into_range(ns):
    clamp = ns["_clamp"]
    assert clamp(80, 0, 50) == 50       # above top -> top edge (visible)
    assert clamp(80, 100, 200) == 100   # below bottom -> bottom edge
    assert clamp(40, 0, 100) == 40      # within -> unchanged


# ---------------------------------------------------------------------------
# Slow-channel noise tracking
# ---------------------------------------------------------------------------

SLOW_CSV_CONTENT = textwrap.dedent("""\
    time,trigger_time,fast_offset,fast_noise,fast_vpp,fast_snr,threshold,noise_thresh_ratio,slow_offset,slow_noise,slow_vpp,slow_snr
    2026-06-27 14:23:01.123456+00:00,2026-06-27 14:23:01.000000+00:00,0.012,0.0045,0.1,5.0,0.05,0.09,,,,
    2026-06-27 14:24:01.123456+00:00,2026-06-27 14:24:01.000000+00:00,0.013,0.0045,0.1,5.0,0.05,0.09,-0.025,0.0040,0.1,10.0
    2026-06-27 14:25:01.123456+00:00,2026-06-27 14:25:01.000000+00:00,0.013,0.0045,0.1,5.0,0.05,0.09,0.020,0.0030,0.1,10.0
""")


def _use_noise_dir(ns, monkeypatch, data_dir):
    orig = ns["ingest_noise_data"]
    monkeypatch.setitem(
        ns, "ingest_noise_data",
        lambda n_days=None, data_dir=data_dir, glob_pattern=ns["NOISE_GLOB_PATTERN"]:
            orig(n_days=n_days, data_dir=data_dir, glob_pattern=glob_pattern))


def test_slow_gauges_registered(ns):
    plots = ns["STATUS_DASHBOARD_PLOTS"]
    assert plots["slownoisefloor"]["plot_metadata"]["plot_title"] == "Slow Noise Floor"
    assert plots["slowdcoffset"]["plot_metadata"]["plot_title"] == "Slow DC Offset"


def test_slow_subplots_and_names(ns):
    assert "slow_noise" in ns["NOISE_PLOT_SUBPLOTS"]
    assert "slow_offset" in ns["NOISE_PLOT_SUBPLOTS"]
    assert ns["VARIABLE_NAME_MAP"]["slow_noise"] == "Slow Noise Floor"
    assert ns["VARIABLE_NAME_MAP"]["slow_offset"] == "Slow DC Offset"
    assert "slow_noise_thresh_ratio" not in ns["NOISE_CSV_COLUMNS"]


def test_slow_axes_fall_back_to_fast_without_slow_data(ns):
    """No slow data yet: the slow axes borrow the fast ranges (independent
    copies), so the plots are not empty or degenerate."""
    lm = ns["LAYOUT_MAP"]
    assert lm["slow_noise"]["range"] == lm["fast_noise"]["range"]
    assert lm["slow_offset"]["range"] == lm["fast_offset"]["range"]
    assert lm["slow_noise"] is not lm["fast_noise"]
    plots = ns["STATUS_DASHBOARD_PLOTS"]
    assert (plots["slownoisefloor"]["plot_params"]["range"]
            == lm["slow_noise"]["range"])


def test_slow_offset_gauge_has_no_fast_zones(ns):
    """The fast +-200 mV green/red zones mean nothing known for the slow
    channel (PB, 2026-10-30): no zones until there is data to set them."""
    plots = ns["STATUS_DASHBOARD_PLOTS"]
    assert plots["slowdcoffset"]["plot_params"]["steps"] is None
    assert plots["slowdcoffset"]["plot_params"]["range"] == ns["OFFSET_GAUGE_RANGE"]
    assert plots["dcoffset"]["plot_params"]["steps"] is not None


def test_slow_noise_has_no_threshold_band(ns):
    assert "slow_noise" not in ns["NOISE_COLOR_TABLE_MAP"]
    params = ns["STATUS_DASHBOARD_PLOTS"]["slownoisefloor"]["plot_params"]
    assert params["steps"] is None
    assert params["threshold_value"] == 0


def test_ingest_old_csv_adds_float_slow_columns(ns, tmp_noise_dir):
    """CSVs written before slow tracking load with NaN float slow columns."""
    df = ns["ingest_noise_data"](data_dir=tmp_noise_dir)
    for col in ("slow_noise", "slow_offset", "slow_vpp", "slow_snr"):
        assert col in df.columns
        assert pd.api.types.is_float_dtype(df[col])
        assert df[col].isna().all()


def test_ingest_mixed_old_and_new_csvs(ns, tmp_path):
    (tmp_path / "noise_hamma02_2026-06-26.csv").write_text(SAMPLE_CSV_CONTENT)
    (tmp_path / "noise_hamma02_2026-06-27.csv").write_text(SLOW_CSV_CONTENT)
    df = ns["ingest_noise_data"](data_dir=tmp_path)
    assert len(df) == 5
    assert df["slow_noise"].isna().sum() == 3
    assert abs(df["slow_noise"].iloc[-1] - 0.003) < 1e-9


def test_preprocess_slow_mv_conversion(ns, tmp_path, monkeypatch):
    (tmp_path / "noise_hamma02_2026-06-27.csv").write_text(SLOW_CSV_CONTENT)
    _use_noise_dir(ns, monkeypatch, tmp_path)
    result = ns["_noise_plot_preprocess"](pd.DataFrame())
    assert abs(result["slow_noise"].iloc[-1] - 3.0) < 0.01
    assert abs(result["slow_offset"].iloc[-2] - (-25.0)) < 0.01


def test_slow_gauge_series_drops_leading_blank(ns, tmp_path, monkeypatch):
    """A blank first row must not poison sindri's max() (max([nan, ...]) is nan)."""
    (tmp_path / "noise_hamma02_2026-06-27.csv").write_text(SLOW_CSV_CONTENT)
    _use_noise_dir(ns, monkeypatch, tmp_path)
    # Pin "now" just after the fixture's last row, or the gauges' 24 h
    # staleness cutoff (#116) turns this June data into NA.
    monkeypatch.setitem(ns, "_noise_now",
                        lambda: pd.Timestamp("2026-06-27 14:26:00"))
    series = ns["STATUS_DASHBOARD_PLOTS"]["slownoisefloor"]["plot_data"]["variable"](None)
    assert not series.isna().any()
    assert abs(max(series) - 4.0) < 0.01
    assert abs(series.iloc[-1] - 3.0) < 0.01


def test_wiring_slow_axes_are_sized_from_slow_data(tmp_path, monkeypatch):
    """E2E: each channel's axes come from its own data (PB, 2026-10-30)."""
    noise_dir = tmp_path / "brokkr" / "hamma" / "noise_diag"
    noise_dir.mkdir(parents=True)
    (noise_dir / "noise_hamma02_2026-06-27.csv").write_text(SLOW_CSV_CONTENT)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    _install_stubs()
    ns2 = {}
    exec(compile(MAIN_PY.read_text(), str(MAIN_PY), "exec"), ns2)  # noqa: S102

    lm = ns2["LAYOUT_MAP"]
    # fast: threshold 50 mV -> [0, 1.25*50]; offsets 12-13 mV -> +-14.3
    assert lm["fast_noise"]["range"] == [0, 62.5]
    assert abs(lm["fast_offset"]["range"][1] - 14.3) < 0.01
    # slow: noise peak 4.0 mV -> [0, 4.4]; offsets -25/+20 mV -> +-27.5
    assert abs(lm["slow_noise"]["range"][1] - 4.4) < 0.01
    assert lm["slow_noise"]["range"][0] == 0
    assert abs(lm["slow_offset"]["range"][1] - 27.5) < 0.01
    params = ns2["STATUS_DASHBOARD_PLOTS"]["slownoisefloor"]["plot_params"]
    assert params["range"] == lm["slow_noise"]["range"]


def test_wiring_large_slow_noise_does_not_widen_fast(tmp_path, monkeypatch):
    """A slow channel running far above the fast one stays on its own plot
    and leaves the fast (alarm) gauge and axes alone (PB, 2026-10-30)."""
    noise_dir = tmp_path / "brokkr" / "hamma" / "noise_diag"
    noise_dir.mkdir(parents=True)
    # fast offset max 13 mV, slow offset -25 mV; fast noise 4.5 mV, slow 90 mV;
    # threshold 50 mV -> 1.25*50 = 62.5 would clip the 90 mV slow noise.
    csv = SLOW_CSV_CONTENT.replace(",0.0040,0.1,10.0", ",0.090,0.1,10.0")
    (noise_dir / "noise_hamma02_2026-06-27.csv").write_text(csv)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    _install_stubs()
    ns2 = {}
    exec(compile(MAIN_PY.read_text(), str(MAIN_PY), "exec"), ns2)  # noqa: S102

    lm = ns2["LAYOUT_MAP"]
    plots = ns2["STATUS_DASHBOARD_PLOTS"]
    assert lm["slow_noise"]["range"] == [0, 99.0]      # 90 mV * 1.1
    assert plots["slownoisefloor"]["plot_params"]["range"] == [0, 99.0]
    assert abs(lm["slow_offset"]["range"][1] - 27.5) < 0.01   # |-25| * 1.1
    # The fast channel is sized from fast data alone.
    assert lm["fast_noise"]["range"] == [0, 62.5]
    assert plots["noisefloor"]["plot_params"]["range"] == [0, 62.5]
    assert abs(lm["fast_offset"]["range"][1] - 14.3) < 0.01
    # Threshold band still sits at the fast-channel threshold.
    assert ns2["NOISE_COLOR_TABLE_MAP"]["fast_noise"][0] == [50.0]


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------

def test_main_py_syntax():
    import py_compile
    py_compile.compile(str(MAIN_PY), doraise=True)


# ---------------------------------------------------------------------------
# HAM-187: noise gauges report NA -- not a traceback, not a stale value
# ---------------------------------------------------------------------------
# On a nosensor unit noise_diag writes nothing; an EMPTY Series made sindri's
# gauge code raise (caught, but a full traceback per gauge per ~60 s build). A
# unit switched to nosensor also keeps its old CSVs (mj43), so the gauge showed
# a months-old value as current. Every assertion below goes through sindri's
# REAL get_dashboard_plot_data (vendored verbatim from 436bf61) driven by each
# gauge's own plot_data, so a config change cannot slip past a hand-copy.
import datetime as dt  # noqa: E402
import importlib.util  # noqa: E402

_SINDRI_SPEC = importlib.util.spec_from_file_location(
    "sindri_436bf61_gauge",
    pathlib.Path(__file__).parent / "fixtures" / "sindri_436bf61_gauge.py")
SINDRI_GAUGE = importlib.util.module_from_spec(_SINDRI_SPEC)
_SINDRI_SPEC.loader.exec_module(SINDRI_GAUGE)

GAUGES = ["noisefloor", "dcoffset", "slownoisefloor", "slowdcoffset"]
SAMPLE_LAST_ROW = pd.Timestamp("2026-06-26 14:24:01.123456")


def _gauge_values(ns, gauge):
    """[value, delta_ref, threshold] exactly as the deployed sindri computes them."""
    plot = ns["STATUS_DASHBOARD_PLOTS"][gauge]
    return list(SINDRI_GAUGE.get_dashboard_plot_data(
        pd.DataFrame(), plot_type=plot["plot_type"], plot_data=plot["plot_data"]))


def _utc_now():
    return pd.Timestamp(dt.datetime.now(dt.timezone.utc)).tz_convert(None)


@pytest.fixture()
def noise_source(ns):
    """Point ingest_noise_data at a temp dir or an in-memory frame (volts)."""
    orig = ns["ingest_noise_data"]

    def use_dir(data_dir):
        ns["ingest_noise_data"] = (
            lambda n_days=None, data_dir=data_dir,
            glob_pattern=ns["NOISE_GLOB_PATTERN"]:
            orig(n_days=n_days, data_dir=data_dir, glob_pattern=glob_pattern))

    def use_rows(rows):
        index = pd.DatetimeIndex([t for t, _ in rows], name="time")
        values = [v for _, v in rows]
        frame = pd.DataFrame(
            {"fast_noise": values, "fast_offset": values,
             "slow_noise": values, "slow_offset": values},
            index=index, dtype="float64")
        ns["ingest_noise_data"] = lambda n_days=None, **kwargs: frame

    def use_error(error):
        def boom(*args, **kwargs):
            raise error
        ns["ingest_noise_data"] = boom

    yield types.SimpleNamespace(dir=use_dir, rows=use_rows, error=use_error)
    ns["ingest_noise_data"] = orig


@pytest.fixture()
def clock(ns):
    orig = ns["_noise_now"]

    def set_now(ts):
        ns["_noise_now"] = ts if callable(ts) else (lambda: pd.Timestamp(ts))

    yield set_now
    ns["_noise_now"] = orig


# --- no data / load failure -------------------------------------------------

@pytest.mark.parametrize("gauge", GAUGES)
def test_gauge_reports_na_without_noise_data(ns, empty_noise_dir, noise_source, gauge):
    noise_source.dir(empty_noise_dir)
    series = ns["STATUS_DASHBOARD_PLOTS"][gauge]["plot_data"]["variable"](pd.DataFrame())
    assert isinstance(series.index, pd.DatetimeIndex) and series.dtype == "float64"
    assert _gauge_values(ns, gauge) == [None, None, None]


@pytest.mark.parametrize("gauge", GAUGES)
def test_gauge_reports_na_when_loading_fails(ns, noise_source, gauge):
    noise_source.error(OSError("unreadable noise dir"))
    assert _gauge_values(ns, gauge) == [None, None, None]


# --- the real clock (nothing else exercises it) ------------------------------

def test_real_clock_is_utc_and_tz_naive(ns):
    """A tz-aware or local clock would mis-age every unit (or raise)."""
    now = ns["_noise_now"]()
    assert now.tz is None
    assert abs(now - _utc_now()) < pd.Timedelta("1min")


@pytest.mark.parametrize("gauge", GAUGES)
def test_fresh_data_shows_with_the_real_clock(ns, noise_source, gauge):
    noise_source.rows([(_utc_now() - pd.Timedelta("10min"), 0.0045)])
    assert _gauge_values(ns, gauge)[0] == pytest.approx(4.5)


@pytest.mark.parametrize("gauge", GAUGES)
def test_a_broken_clock_degrades_to_na_not_a_traceback(ns, noise_source, clock, gauge):
    """The staleness comparison must sit inside the guard (red team M18)."""
    noise_source.rows([(_utc_now(), 0.0045)])
    clock(lambda: pd.Timestamp.now(tz="UTC"))     # tz-aware vs naive index
    assert _gauge_values(ns, gauge) == [None, None, None]


# --- staleness: 24 h on the newest VALID row ---------------------------------

def test_max_age_is_24h(ns):
    assert ns["NOISE_GAUGE_MAX_AGE"] == pd.Timedelta("24h")


@pytest.mark.parametrize("gauge", GAUGES)
def test_older_than_24h_reads_na(ns, tmp_noise_dir, noise_source, clock, gauge):
    noise_source.dir(tmp_noise_dir)
    clock(SAMPLE_LAST_ROW + pd.Timedelta("24h") + pd.Timedelta("1s"))
    assert _gauge_values(ns, gauge) == [None, None, None]


def test_within_24h_shows_value(ns, tmp_noise_dir, noise_source, clock):
    noise_source.dir(tmp_noise_dir)
    clock(SAMPLE_LAST_ROW + pd.Timedelta("24h") - pd.Timedelta("1s"))
    assert _gauge_values(ns, "noisefloor")[0] == pytest.approx(4.5)


def test_exactly_24h_old_still_counts_as_current(ns, noise_source, clock):
    t0 = pd.Timestamp("2026-10-01")
    noise_source.rows([(t0, 0.0045)])
    clock(t0 + pd.Timedelta("24h"))
    assert _gauge_values(ns, "noisefloor")[0] == pytest.approx(4.5)


def test_recent_nan_row_does_not_make_stale_data_current(ns, noise_source, clock):
    """Age is taken from the newest VALID row, not any row (red team M03/M12)."""
    t0 = pd.Timestamp("2026-10-01")
    noise_source.rows([(t0, 0.0045), (t0 + pd.Timedelta("23h"), float("nan"))])
    clock(t0 + pd.Timedelta("25h"))
    assert _gauge_values(ns, "noisefloor") == [None, None, None]


def test_fresh_data_with_a_malformed_newest_value_shows_last_valid(ns, noise_source, clock):
    """The gauge is fed the valid rows, so it agrees with its own age check."""
    t0 = pd.Timestamp("2026-10-01")
    noise_source.rows([(t0, 0.0040), (t0 + pd.Timedelta("10min"), 0.0090),
                       (t0 + pd.Timedelta("20min"), float("nan"))])
    clock(t0 + pd.Timedelta("30min"))
    value, _delta, threshold = _gauge_values(ns, "noisefloor")
    assert value == pytest.approx(9.0)
    assert threshold == pytest.approx(9.0)


def test_nan_first_in_window_does_not_null_the_threshold(ns, noise_source, clock):
    """Python's max() with a leading NaN returns NaN; feeding valid rows avoids it."""
    t0 = pd.Timestamp("2026-10-01")
    noise_source.rows([(t0, float("nan")), (t0 + pd.Timedelta("1min"), 0.0090),
                       (t0 + pd.Timedelta("2min"), 0.0045)])
    clock(t0 + pd.Timedelta("3min"))
    assert _gauge_values(ns, "noisefloor")[2] == pytest.approx(9.0)


@pytest.mark.parametrize("gauge", GAUGES)
def test_unit_with_data_is_unchanged(ns, tmp_noise_dir, noise_source, clock, gauge):
    noise_source.dir(tmp_noise_dir)
    clock(SAMPLE_LAST_ROW + pd.Timedelta("1min"))
    value = _gauge_values(ns, gauge)[0]
    # The sample day file predates the slow columns: slow gauges read NA,
    # the fast ones are unchanged.
    expected = {"noisefloor": 4.5, "dcoffset": 13.0,
                "slownoisefloor": None, "slowdcoffset": None}[gauge]
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


def test_na_series_is_stamped_with_the_clock(ns, clock):
    clock("2026-10-29 12:00:00")
    assert ns["_na_gauge_series"]().index[0] == pd.Timestamp("2026-10-29 12:00:00")


def test_offset_override_applies_to_the_fast_channel_only(ns):
    """offset_range is a fast-channel calibration; the slow axis ignores it."""
    ov = {7: {"offset_range": [-50, 50]}}
    fn = ns["get_noise_offset_range_mv"]
    assert fn(unit_n=7, overrides=ov, column="fast_offset") == [-50, 50]
    assert fn(unit_n=7, overrides=ov, column="slow_offset",
              default=(-1.0, 1.0)) != [-50, 50]


# --- slow gauges: their own column, and a dead slow channel is not current ---

def _frame(ns, rows):
    """rows: (time, fast_noise, fast_offset, slow_noise, slow_offset), volts."""
    index = pd.DatetimeIndex([r[0] for r in rows], name="time")
    frame = pd.DataFrame(
        [r[1:] for r in rows], index=index, dtype="float64",
        columns=["fast_noise", "fast_offset", "slow_noise", "slow_offset"])
    ns["ingest_noise_data"] = lambda n_days=None, **kwargs: frame


@pytest.fixture()
def restore_ingest(ns):
    orig = ns["ingest_noise_data"]
    yield
    ns["ingest_noise_data"] = orig


def test_slow_gauges_read_their_own_columns(ns, clock, restore_ingest):
    t0 = pd.Timestamp("2026-10-01")
    _frame(ns, [(t0, 0.0045, 0.013, 0.0040, -0.025)])
    clock(t0 + pd.Timedelta("1min"))
    assert _gauge_values(ns, "slownoisefloor")[0] == pytest.approx(4.0)
    assert _gauge_values(ns, "slowdcoffset")[0] == pytest.approx(-25.0)
    assert _gauge_values(ns, "noisefloor")[0] == pytest.approx(4.5)
    assert _gauge_values(ns, "dcoffset")[0] == pytest.approx(13.0)


@pytest.mark.parametrize("gauge", ["slownoisefloor", "slowdcoffset"])
def test_dead_slow_channel_reads_na_while_fast_is_fresh(
        ns, clock, restore_ingest, gauge):
    """Slow failed 2 h ago, fast still reporting: the last good slow value
    must not read as current for the rest of the 24 h window."""
    t0 = pd.Timestamp("2026-10-01")
    nan = float("nan")
    _frame(ns, [(t0, 0.0045, 0.013, 0.0090, 0.009),
                (t0 + pd.Timedelta("2h"), 0.0045, 0.013, nan, nan)])
    clock(t0 + pd.Timedelta("2h1min"))
    assert _gauge_values(ns, gauge) == [None, None, None]
    assert _gauge_values(ns, "noisefloor")[0] == pytest.approx(4.5)


@pytest.mark.parametrize("gauge", ["slownoisefloor", "slowdcoffset"])
def test_slow_one_blank_row_behind_fast_still_shows(
        ns, clock, restore_ingest, gauge):
    """A single blank slow reading within the lag allowance is not a dead
    channel."""
    t0 = pd.Timestamp("2026-10-01")
    nan = float("nan")
    _frame(ns, [(t0, 0.0045, 0.013, 0.0090, 0.009),
                (t0 + pd.Timedelta("30min"), 0.0045, 0.013, nan, nan)])
    clock(t0 + pd.Timedelta("31min"))
    assert _gauge_values(ns, gauge)[0] == pytest.approx(9.0)


def test_slow_lag_allowance_is_one_hour(ns):
    assert ns["SLOW_GAUGE_MAX_LAG"] == pd.Timedelta("1h")


def test_slow_lag_is_measured_against_the_matching_fast_column(
        ns, clock, restore_ingest):
    """slow_offset lags fast_offset, not fast_noise: a blank fast_noise on the
    newest row must not hide a dead slow offset channel."""
    t0 = pd.Timestamp("2026-10-01")
    nan = float("nan")
    _frame(ns, [(t0, 0.0045, 0.013, 0.0090, 0.009),
                (t0 + pd.Timedelta("2h"), nan, 0.013, nan, nan)])
    clock(t0 + pd.Timedelta("2h1min"))
    assert _gauge_values(ns, "slowdcoffset") == [None, None, None]


def test_an_all_blank_newest_row_is_not_a_dead_slow_channel(
        ns, clock, restore_ingest):
    """The lag is taken from the newest VALID fast reading."""
    t0 = pd.Timestamp("2026-10-01")
    nan = float("nan")
    _frame(ns, [(t0, 0.0045, 0.013, 0.0090, 0.009),
                (t0 + pd.Timedelta("2h"), nan, nan, nan, nan)])
    clock(t0 + pd.Timedelta("2h1min"))
    assert _gauge_values(ns, "slownoisefloor")[0] == pytest.approx(9.0)
