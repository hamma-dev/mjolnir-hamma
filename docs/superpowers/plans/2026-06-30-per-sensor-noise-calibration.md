# Per-sensor noise calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the website noise-floor panel/gauge calibrate per sensor from the per-Pi threshold (with a manual override dict), and bound the DC-offset gauge/panel sensibly, replacing the mj02-tuned fleet-wide constants.

**Architecture:** All changes live in `website/main.py` on the `feature/website-noise-plot` branch (ships with PR #68). Pure helper functions compute the per-sensor layout from the already-computed `NOISE_THRESHOLD_MV`; a module-level wiring block (placed *after* `NOISE_THRESHOLD_MV` is known, ~line 1032) reassigns `LAYOUT_MAP["fast_noise"]`/`["fast_offset"]` and mutates the `STATUS_DASHBOARD_PLOTS["noisefloor"]`/`["dcoffset"]` gauge params. No sindri changes.

**Tech Stack:** Python 3.6+-compatible (pandas 1.3.5 on the Pi; tests run under conda `sci`/py3.12), pytest. Tests exec the real `main.py` with stubbed brokkr/sindri modules.

## Global Constraints

- **No sindri code changes.** Threshold marking uses sindri's existing `color_map` → shaded-band mechanism; the time-series `range` is mandatory (a `None` range raises in `generate_plot_block`), so "autoscale" is a data-derived range, never an omitted one.
- **Never raise on the website build path.** Every new function returns a documented fallback on bad/missing/empty input — a raise crash-loops the sindri service.
- **Empty noise frame stays float-dtype + `DatetimeIndex`** (existing `_noise_plot_preprocess` contract) so sindri's `np.isfinite` / `.strftime` paths don't raise.
- **Gauge `steps` format** is `[color_domain, color_range]` with `len(color_domain) == len(color_range) - 1` (consumed by `generate_step_strings`). Inline `steps` (not `None`) takes precedence over `color_map`.
- **Python 3.6 compatibility:** no f-string `=`, no walrus in shipped code, limited type hints. `import math` at top of `main.py` if not already present (verify).
- Run tests with: `source /opt/miniconda3/etc/profile.d/conda.sh && conda activate sci && python -m pytest tests/test_noise_website.py -v`

---

### Task 1: Per-sensor config resolver + nice-dtick helper + override dict

**Files:**
- Modify: `website/main.py` — add near the noise block, *before* `NOISE_THRESHOLD_MV` (so functions exist when wiring runs). Add `import math` to the stdlib imports if absent.
- Test: `tests/test_noise_website.py`

**Interfaces:**
- Produces:
  - `NOISE_OVERRIDES: dict` — `{unit_n: {"threshold_mv": float, "noise_range": [lo, hi], "noise_dtick": float, "offset_range": [lo, hi]}}`. Ships empty (`{}`).
  - `_nice_dtick(span, divisions=5) -> float` — a "nice" tick step (1/2/2.5/5 × 10ⁿ) ≈ `span/divisions`; returns `1` for non-positive/non-finite span.
  - `_resolve_noise_config(unit_n, threshold_mv, overrides=None) -> dict` with keys `threshold_mv` (float), `noise_range` (`[0, hi]`), `noise_dtick` (float). Order: override → threshold-derived (`hi = 1.25 * threshold`) → default (`threshold 80.0` → `[0, 100]`). Never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_noise_website.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /opt/miniconda3/etc/profile.d/conda.sh && conda activate sci && python -m pytest tests/test_noise_website.py -k "nice_dtick or resolve_noise_config or overrides_ships" -v`
Expected: FAIL — `KeyError: '_nice_dtick'` / `'_resolve_noise_config'` / `'NOISE_OVERRIDES'`.

- [ ] **Step 3: Write minimal implementation**

In `website/main.py`, ensure `import math` is present in the stdlib import group. Then, in the noise block immediately **before** the `NOISE_THRESHOLD_MV = ...` assignment (currently ~line 1031), add:

```python
# --- Per-sensor noise calibration -------------------------------------------
# Optional manual overrides, keyed by unit number. Empty = pure data-driven.
#   {unit_n: {"threshold_mv": float, "noise_range": [lo, hi],
#             "noise_dtick": float, "offset_range": [lo, hi]}}
NOISE_OVERRIDES = {}

OFFSET_GREEN_RED_MV = 200      # DC-offset green/red demarcation (fleet constant)
OFFSET_GAUGE_RANGE = [-300, 300]


def _nice_dtick(span, divisions=5):
    """A 'nice' tick step (1/2/2.5/5 x 10**n) close to span/divisions."""
    try:
        raw = float(span) / divisions
        if not math.isfinite(raw) or raw <= 0:
            return 1
        mag = 10 ** math.floor(math.log10(raw))
        for mult in (1, 2, 2.5, 5, 10):
            if raw <= mult * mag:
                return mult * mag
        return 10 * mag
    except Exception:
        return 1


def _coerce_positive_float(value):
    """Return float(value) if finite and > 0, else None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _resolve_noise_config(unit_n, threshold_mv, overrides=None):
    """Effective noise-floor layout for a unit. Override > derived > default.

    Never raises. Returns {"threshold_mv", "noise_range", "noise_dtick"}.
    """
    if overrides is None:
        overrides = NOISE_OVERRIDES
    unit_override = {}
    try:
        candidate = overrides.get(unit_n, {})
        if isinstance(candidate, dict):
            unit_override = candidate
    except Exception:
        unit_override = {}

    # Threshold: override -> data-derived -> 80 mV default.
    threshold = (_coerce_positive_float(unit_override.get("threshold_mv"))
                 or _coerce_positive_float(threshold_mv)
                 or 80.0)

    # Axis range: override -> [0, 1.25 * threshold].
    noise_range = [0, 1.25 * threshold]
    ov_range = unit_override.get("noise_range")
    if (isinstance(ov_range, (list, tuple)) and len(ov_range) == 2):
        try:
            noise_range = [float(ov_range[0]), float(ov_range[1])]
        except (TypeError, ValueError):
            pass

    # dtick: override -> derived from the range span.
    noise_dtick = (_coerce_positive_float(unit_override.get("noise_dtick"))
                   or _nice_dtick(noise_range[1] - noise_range[0]))

    return {"threshold_mv": threshold,
            "noise_range": noise_range,
            "noise_dtick": noise_dtick}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_noise_website.py -k "nice_dtick or resolve_noise_config or overrides_ships" -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add website/main.py tests/test_noise_website.py
git commit -m "Add per-sensor noise config resolver + override dict"
```

---

### Task 2: Data-derived DC-offset range helper

**Files:**
- Modify: `website/main.py` — add after `_resolve_noise_config`, still before `NOISE_THRESHOLD_MV`.
- Test: `tests/test_noise_website.py`

**Interfaces:**
- Consumes: `ingest_noise_data` (existing), `NOISE_OVERRIDES`, `NOISE_PLOT_DAYS` (existing).
- Produces:
  - `get_noise_offset_range_mv(n_days=NOISE_PLOT_DAYS, default=(-300.0, 300.0), unit_n=None, overrides=None) -> list` — symmetric padded `[-m, m]` (mV) from the offset data (`m = max(|min|, |max|) * 1.1`); `offset_range` override wins; empty/NaN/error → `list(default)`. Never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_noise_website.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_noise_website.py -k "offset_range" -v`
Expected: FAIL — `KeyError: 'get_noise_offset_range_mv'`.

- [ ] **Step 3: Write minimal implementation**

In `website/main.py`, after `_resolve_noise_config`:

```python
def get_noise_offset_range_mv(n_days=None, default=(-300.0, 300.0),
                              unit_n=None, overrides=None):
    """Symmetric padded DC-offset axis range (mV) from the offset data.

    offset_range override wins; empty/NaN/error -> list(default). Never raises.
    """
    if n_days is None:
        n_days = NOISE_PLOT_DAYS
    if overrides is None:
        overrides = NOISE_OVERRIDES
    if unit_n is None:
        unit_n = UNIT_N

    try:
        unit_override = overrides.get(unit_n, {})
        ov_range = unit_override.get("offset_range")
        if isinstance(ov_range, (list, tuple)) and len(ov_range) == 2:
            return [float(ov_range[0]), float(ov_range[1])]
    except Exception:
        pass

    try:
        offset_mv = ingest_noise_data(n_days=n_days)["fast_offset"].dropna() * 1000
        if offset_mv.empty:
            return list(default)
        magnitude = max(abs(float(offset_mv.min())),
                        abs(float(offset_mv.max()))) * 1.1
        if not math.isfinite(magnitude) or magnitude <= 0:
            return list(default)
        return [-magnitude, magnitude]
    except Exception:
        return list(default)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_noise_website.py -k "offset_range" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add website/main.py tests/test_noise_website.py
git commit -m "Add data-derived DC-offset range helper"
```

---

### Task 3: Wire noise-floor — LAYOUT_MAP, gauge, red threshold line

**Files:**
- Modify: `website/main.py` — wiring block right after `NOISE_THRESHOLD_MV = ...` and the `NOISE_COLOR_TABLE_MAP` definition (~lines 1031-1039); also bump `NOISE_PLOT_CONTENT_ARGS["shape_opacity"]`.
- Test: `tests/test_noise_website.py`

**Interfaces:**
- Consumes: `_resolve_noise_config`, `NOISE_THRESHOLD_MV`, `UNIT_N`, `STATUS_DASHBOARD_PLOTS`, `LAYOUT_MAP`.
- Produces (post-exec module state, with no noise data present → threshold 80):
  - `LAYOUT_MAP["fast_noise"] == {"dtick": 20, "range": [0, 100], "suffix": " mV"}`
  - `STATUS_DASHBOARD_PLOTS["noisefloor"]["plot_params"]`: `range [0,100]`, `dtick 20`, `threshold_value 80.0`, `steps [[80.0], ["green", "red"]]`.
  - `NOISE_COLOR_TABLE_MAP["fast_noise"]` = a thin red stripe centered at the threshold.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_noise_website.py`:

```python
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

def test_noise_color_table_is_threshold_stripe(ns):
    # fast_noise band is a thin stripe straddling the threshold (a red line):
    # domain [lo, hi] with 3 colors, middle one red, outers transparent.
    domain, colors = ns["NOISE_COLOR_TABLE_MAP"]["fast_noise"]
    assert len(domain) == 2 and len(colors) == 3
    assert colors[1] == "red"
    lo, hi = domain
    assert lo < 80.0 < hi      # straddles the threshold
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_noise_website.py -k "fast_noise_derived or noisefloor_gauge_wired or threshold_stripe" -v`
Expected: FAIL — current `LAYOUT_MAP["fast_noise"]` is the shared `[0,100]` dict but `dtick 20` already matches by coincidence; the gauge `threshold_value` is `0` and `steps` is `None`, and `NOISE_COLOR_TABLE_MAP` has a single-breakpoint 2-color band → assertions fail.

- [ ] **Step 3: Write minimal implementation**

In `website/main.py`, **replace** the existing `NOISE_COLOR_TABLE_MAP` block (currently lines ~1034-1039) with a wiring block placed after `NOISE_THRESHOLD_MV`:

```python
# --- Apply per-sensor noise-floor calibration -------------------------------
_NOISE_CFG = _resolve_noise_config(UNIT_N, NOISE_THRESHOLD_MV)
_NOISE_THR = _NOISE_CFG["threshold_mv"]

LAYOUT_MAP["fast_noise"] = {
    "dtick": _NOISE_CFG["noise_dtick"],
    "range": _NOISE_CFG["noise_range"],
    "suffix": " mV",
    }

STATUS_DASHBOARD_PLOTS["noisefloor"]["plot_params"].update({
    "range": _NOISE_CFG["noise_range"],
    "dtick": _NOISE_CFG["noise_dtick"],
    "threshold_value": _NOISE_THR,
    "steps": [[_NOISE_THR], ["green", "red"]],
    })

# Thin red stripe straddling the threshold = a red reference "line" on the
# fast_noise time-series. Half-width: 1% of the axis span, min 0.5 mV.
_NOISE_LINE_HALF = max(0.5, 0.01 * _NOISE_CFG["noise_range"][1])
NOISE_COLOR_TABLE_MAP = {
    "fast_noise": [[_NOISE_THR - _NOISE_LINE_HALF, _NOISE_THR + _NOISE_LINE_HALF],
                   ["rgba(0, 0, 0, 0)", "red", "rgba(0, 0, 0, 0)"]],
    }
```

Then, where `NOISE_PLOT_CONTENT_ARGS` is built (currently line 1064), make the stripe render solid (the inherited `shape_opacity` is `0.2`):

```python
NOISE_PLOT_CONTENT_ARGS = dict(HISTORY_PLOT_CONTENT_ARGS)
NOISE_PLOT_CONTENT_ARGS["plot_height"] = 512
NOISE_PLOT_CONTENT_ARGS["shape_opacity"] = 1.0
```

Update the `NOISE_PLOT_METADATA` `section_description` wording from "The shaded band marks the AGS trigger threshold." to "The red line marks the AGS trigger threshold."

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_noise_website.py -k "fast_noise_derived or noisefloor_gauge_wired or threshold_stripe" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add website/main.py tests/test_noise_website.py
git commit -m "Wire per-sensor noise-floor axis, gauge zones, threshold line"
```

---

### Task 4: Wire DC-offset — gauge range/zones + data-derived series range

**Files:**
- Modify: `website/main.py` — extend the wiring block from Task 3.
- Test: `tests/test_noise_website.py` — add new asserts AND update the two existing offset tests.

**Interfaces:**
- Consumes: `get_noise_offset_range_mv`, `OFFSET_GREEN_RED_MV`, `OFFSET_GAUGE_RANGE`, `_nice_dtick`.
- Produces (no data in test env → fallback `[-300, 300]`):
  - `LAYOUT_MAP["fast_offset"]["range"] == [-300.0, 300.0]`
  - `STATUS_DASHBOARD_PLOTS["dcoffset"]["plot_params"]`: `range [-300, 300]`, `steps [[-200, 200], ["red", "green", "red"]]`.

- [ ] **Step 1: Update the two existing offset tests and add new ones**

In `tests/test_noise_website.py`, **change** the two existing assertions:

```python
def test_layout_map_fast_offset_range(ns):
    # Data-derived; no noise data in test env -> symmetric fallback.
    assert ns["LAYOUT_MAP"]["fast_offset"]["range"] == [-300.0, 300.0]

def test_dcoffset_gauge_range(ns):
    gauge = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]
    assert gauge["plot_params"]["range"] == [-300, 300]
```

**Add**:

```python
def test_dcoffset_gauge_steps(ns):
    params = ns["STATUS_DASHBOARD_PLOTS"]["dcoffset"]["plot_params"]
    assert params["steps"] == [[-200, 200], ["red", "green", "red"]]

def test_offset_green_red_constant(ns):
    assert ns["OFFSET_GREEN_RED_MV"] == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_noise_website.py -k "fast_offset_range or dcoffset" -v`
Expected: FAIL — gauge `range` is still `[0,1000]`, `steps` is `None`; `LAYOUT_MAP["fast_offset"]` is still `[0,1000]`.

- [ ] **Step 3: Write minimal implementation**

Append to the wiring block in `website/main.py` (after the noise-floor wiring from Task 3):

```python
# --- Apply DC-offset calibration --------------------------------------------
_OFFSET_RANGE = get_noise_offset_range_mv()
LAYOUT_MAP["fast_offset"] = {
    "dtick": _nice_dtick(_OFFSET_RANGE[1] - _OFFSET_RANGE[0]),
    "range": _OFFSET_RANGE,
    "suffix": " mV",
    }

STATUS_DASHBOARD_PLOTS["dcoffset"]["plot_params"].update({
    "range": OFFSET_GAUGE_RANGE,
    "dtick": 100,
    "steps": [[-OFFSET_GREEN_RED_MV, OFFSET_GREEN_RED_MV],
              ["red", "green", "red"]],
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_noise_website.py -k "fast_offset_range or dcoffset or green_red" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add website/main.py tests/test_noise_website.py
git commit -m "Wire DC-offset gauge zones and data-derived series range"
```

---

### Task 5: Full-suite regression + module-exec safety

**Files:**
- Test: `tests/test_noise_website.py` (one integration-style test) — no `main.py` change expected; if a regression surfaces, fix in `main.py`.

**Interfaces:**
- Consumes: full module exec (the `ns` fixture).

- [ ] **Step 1: Write the test**

```python
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
```

- [ ] **Step 2: Run the FULL suite**

Run: `python -m pytest tests/test_noise_website.py -v`
Expected: PASS — all prior 10 tests (two updated) + the ~18 new tests, 0 failures.

- [ ] **Step 3: Run the broader project test gate**

Run: `python -m pytest tests/shell tests/unified -q` (if present in this repo) and any website tests.
Expected: no new failures introduced by this change. Report any pre-existing failures rather than fixing unrelated ones.

- [ ] **Step 4: Commit**

```bash
git add tests/test_noise_website.py
git commit -m "Add module-exec consistency regression for noise calibration"
```

---

## Self-Review

**Spec coverage:**
- Override + data-driven mechanism → Task 1 (`NOISE_OVERRIDES`, `_resolve_noise_config`).
- Noise-floor axis `[0, 1.25×threshold]` + derived dtick → Task 1 (resolver) + Task 3 (wiring).
- Noise-floor gauge red marker + green/red zones → Task 3.
- Noise-floor time-series red threshold line → Task 3 (`NOISE_COLOR_TABLE_MAP` stripe + `shape_opacity=1.0`).
- DC-offset series autoscale (data-derived range, mandatory-range workaround) → Task 2 + Task 4.
- DC-offset gauge `[-300,300]` + green/red at ±200 (constant) → Task 4.
- Crash-guard / never-raise → Tasks 1, 2 (guards) + Task 5 (module-exec safety).
- Tests enumerated in spec → Tasks 1-5.

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `_resolve_noise_config` returns `{"threshold_mv","noise_range","noise_dtick"}` — consumed identically in Task 3. `get_noise_offset_range_mv` returns a 2-list — consumed in Task 4. `steps` format `[domain, colors]` consistent with `generate_step_strings`. `OFFSET_GREEN_RED_MV`/`OFFSET_GAUGE_RANGE` defined in Task 1, used in Task 4.

**Open execution note (flag to user before/at execution):** The spec table originally said the noise-floor *series* would be "green below / red above"; this plan instead renders a **thin red threshold line** (Task 3), matching the user's literal "red line at the threshold" wording. If a green-below/red-above fill is preferred instead, swap the `NOISE_COLOR_TABLE_MAP` stripe for `[[_NOISE_THR], ["rgba(0,160,0,0.15)", "rgba(200,60,60,0.25)"]]` and keep `shape_opacity` at `0.2`.

**Deferred to deploy E2E (not unit-testable here):** actual rendered appearance of the red line and gauge zones on `hamma.dev/hamma2` — verify visually after deploy, per PR #68's existing verification step.
