# Per-sensor noise panel/gauge calibration

**Date:** 2026-06-30
**Branch:** `feature/website-noise-plot` (extends PR hamma-dev/mjolnir-hamma#68)
**Scope:** `website/main.py` + `tests/test_noise_website.py` only. No sindri code changes.

## Problem

PR #68 surfaces the `noise_diag` plugin's fast-channel **noise floor** and **DC offset**
on the per-sensor website. The axis ranges were tuned to mj02 and are hardcoded
fleet-wide:

- `STANDARD_LAYOUTS["noise_floor_mv"]` → `range [0, 100]`
- `STANDARD_LAYOUTS["dc_offset_mv"]` → `range [0, 1000]`

Other sensors have different noise floors and DC offsets (different hardware and gain),
so a fleet-wide rollout would clip or mis-scale their plots. We want the noise panel and
gauges to calibrate **per sensor**, driven by the sensor's own threshold, with a manual
override escape hatch.

## Anchor value

`NOISE_THRESHOLD_MV = get_latest_noise_threshold_mv(default=DEFAULT_NOISE_THRESHOLD_MV)`
already exists in `main.py`. It reads the latest `threshold` column from the local
`noise_diag` CSVs (so it is **already per-Pi**) and falls back to `80.0` mV when no data
is present. Every derived value below keys off this anchor.

## Mechanism: data-driven + override dict

Resolution order for every knob: **override → threshold-derived → fleet default**.

```
NOISE_OVERRIDES = {
    # UNIT_N: {"threshold_mv": float,            # force the anchor
    #          "noise_range": [lo, hi],          # force noise-floor axis
    #          "noise_dtick": float,             # force noise-floor dtick
    #          "offset_range": [lo, hi]},        # force DC-offset *series* axis
    # e.g. 2: {"threshold_mv": 83.0},
}
```

`_resolve_noise_config(unit_n, threshold_mv)` returns the effective dict. It **never
raises**: any missing/NaN/non-positive value falls through to the next source, and
malformed override entries are ignored. This preserves PR #68's hard invariant that the
website build path cannot crash-loop the sindri service.

Derivation when no override is present:

| Knob | Derived value |
|---|---|
| noise threshold (red marker / band split) | `threshold_mv` |
| noise-floor axis `range` | `[0, 1.25 * threshold_mv]` |
| noise-floor axis `dtick` | rounded "nice" step ≈ `range_hi / 5` |
| DC-offset *series* axis `range` | data-derived (see below) |
| DC-offset green/red demarcation | `±OFFSET_GREEN_RED_MV` (constant `200`) |

Fallback when `threshold_mv` is missing/NaN: `threshold_mv = 80.0` → noise-floor
`range [0, 100]` (current behavior preserved).

## sindri constraints (verified against `fix/plot-template-multiblock`)

Two assumptions were checked in `sindri/website/generate.py` and corrected:

1. **Time-series `range` is mandatory.** `generate_plot_block` computes
   `layout_args["range"][0]` / `[1]` unconditionally; a `None` range raises. So
   "DC-offset autoscale" cannot be done by omitting `range` — it is implemented as a
   **data-derived range** computed from the offset data, with a fixed fallback when the
   frame is empty. This stays within `main.py` (no sindri change).
2. **No hline primitive.** Threshold marking is drawn via `color_map` →
   `generate_step_strings` → `SHAPE_RANGE_TEMPLATE`, i.e. shaded horizontal **bands**
   split at the `color_map` breakpoints. The existing `NOISE_COLOR_TABLE_MAP`
   (`fast_noise: [[NOISE_THRESHOLD_MV], [...]]`) already splits the noise-floor band at
   the threshold. The "red line at the threshold" is realized as the green→red boundary
   of that band; colors are set so the boundary reads as a threshold line.

Gauge coloring is config-only: gauges already pull `steps` from `color_map` via
`generate_steps`, and each gauge has a `threshold_value` (red marker) and `range`.

## Element-by-element design

### Noise floor — gauge (`"noisefloor"`) and time-series subplot (`fast_noise`)

- **Axis** (both): `range = [0, 1.25 * threshold_mv]`, `dtick =` derived nice step.
- **Gauge red marker:** `threshold_value = threshold_mv` (currently `0`).
- **Gauge zones:** `color_map` entry for the noise gauge variable → green `[0, threshold]`,
  red `[threshold, range_hi]`.
- **Series band:** keep `NOISE_COLOR_TABLE_MAP["fast_noise"]` split at `threshold_mv`;
  green below, red above.

### DC offset — gauge (`"dcoffset"`) and time-series subplot (`fast_offset`)

- **Gauge arc:** fixed `range = [-300, 300]` mV (symmetric, shows sign).
- **Gauge zones:** `color_map` → red `[-300, -200]`, green `[-200, 200]`, red `[200, 300]`.
  Demarcation constant `OFFSET_GREEN_RED_MV = 200`, fleet-wide.
- **Gauge red marker:** `threshold_value` left at `0` (center reference).
- **Series axis:** data-derived range from the offset data (symmetric, padded), e.g.
  `m = max(|min|, |max|) * 1.1; range = [-m, m]`. Fallback `[-1000, 1000]` (or the
  override `offset_range`) when the frame is empty. `dtick` derived.
- **Series colors:** no band (the user asked for autoscale only; coloring lives on the
  gauge).

## Error handling / crash-guard

- `_resolve_noise_config` and the offset-range computation are wrapped so they **never
  raise**; empty/NaN/corrupt input yields the documented fallbacks.
- Empty noise frame still returns float-dtype columns + `DatetimeIndex` (the existing
  `_noise_plot_preprocess` contract) so sindri's `np.isfinite` / `.strftime` paths do not
  raise.
- Override values that are non-numeric, non-positive (where positivity is required), or
  malformed `[lo, hi]` pairs are ignored in favor of the derived/default value.

## Testing

Extend `tests/test_noise_website.py` (execs the real `main.py` with stubbed
brokkr/sindri imports). New cases:

1. **Override applied** — `NOISE_OVERRIDES[UNIT_N]["noise_range"]` wins over derived.
2. **Threshold-derived range** — given threshold `T`, noise-floor `range == [0, 1.25*T]`.
3. **Fallback range** — missing/NaN threshold → `range == [0, 100]`, no raise.
4. **Offset gauge steps** — `[-300,300]` arc with green/red split at `±200`.
5. **Offset series data-derived range** — symmetric padded range from sample data;
   empty frame → fixed fallback, no raise.
6. **Empty-data safety** — full resolution path on an empty noise frame never raises and
   yields valid float-dtype + DatetimeIndex output.

## Out of scope

- Per-sensor colors on the DC-offset *time-series* (gauge only).
- Any sindri-side plot API changes (hline primitive, true autoscale).
- Changing the per-Pi threshold source (still the CSV `threshold` column).

## Rollout note

This lands on `feature/website-noise-plot` and ships with PR #68 (which also requires
sindri #2). Per project memory, mj02 and any sensor parked on these feature branches must
be returned to `0.3.x`/`0.4.x` after #68 + #2 merge.
