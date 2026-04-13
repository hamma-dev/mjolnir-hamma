# Design: NullInput Plugin for nochargecontroller Mode

**Date:** 2026-04-12
**JIRA:** HAM-86 (Sindri crash), HAM-77 (state_monitor KeyErrors)
**Status:** Approved

## Problem

Sensors running in `nochargecontroller` mode produce telemetry CSVs without charge controller columns. This causes:

1. **HAM-86:** Sindri client crashes on `KeyError: 'power_out'` and `TypeError` when processing these CSVs, because `calculate_columns()` and downstream website code assume charge controller columns exist.
2. **HAM-77:** Brokkr's `state_monitor` plugin raises `KeyError` on `adc_vl_f` and `v_lvd` every 60 seconds (caught but noisy).

The root cause is that `nochargecontroller` mode removes the sunsaver input from `monitor_input_steps`, changing the CSV schema. Every downstream consumer that references charge controller columns breaks.

## Approach

Instead of removing the sunsaver input (which changes the schema), replace it with a `NullInput` plugin that produces NA values for all sunsaver columns. The CSV schema stays identical across all modes.

### Why this over fixing sindri

Fixing sindri requires changes in `process.py`, `config/website.py`, and `website/generate.py` — at least 3 downstream crash sites. Fixing the schema at the source is a single change that fixes all consumers at once.

### Why this over letting modbus fail

Leaving the sunsaver input in the pipeline without hardware connected produces ERROR log spam every 60 seconds from failed modbus reads. NullInput avoids the hardware access entirely.

## Design

### 1. NullInput Plugin

**File:** `plugins/null_input.py`

A `ValueInputStep` subclass that always returns `None` from `read_raw_data()`. The existing brokkr decoder machinery calls `output_na_values()` when it receives `None`, producing NA-filled `DataValue` objects for every data_type defined in the step's preset.

```python
"""Input step that produces NA values for all defined data types."""
import brokkr.pipeline.baseinput

class NullInput(brokkr.pipeline.baseinput.ValueInputStep):
    def read_raw_data(self, input_data=None):
        return None
```

No dependencies beyond brokkr. No hardware access. No log spam.

### 2. Named Step in main.toml

Add a named step wrapping the sunsaver preset:

```toml
[steps.sunsaver_input]
    _preset = "sunsaver_mppt_15l.inputs.ram"
```

Replace the direct preset reference in the telemetry pipeline's `monitor_input_steps`:

```
"sunsaver_mppt_15l.inputs.ram"  →  "sunsaver_input"
```

This is a no-op in default mode — the named step resolves to the same preset. It provides a handle for mode overlays to override.

### 3. Mode Overlay Changes

**`nochargecontroller` mode:** Replace the current `monitor_input_steps` override with a step class override:

```toml
[nochargecontroller]
    [nochargecontroller.main.steps.sunsaver_input]
        _module_path = "null_input"
        _class_name = "NullInput"
        _is_plugin = true
        _preset = "sunsaver_mppt_15l.inputs.ram"
```

The mode overlay sets `_module_path`, `_class_name`, and `_is_plugin` on the named step. The `_preset` reference is preserved, so data_types are inherited from the sunsaver preset. Brokkr's config merge order (preset as base, step definition on top) ensures the class override wins while data_types survive.

**`nosensor_nochargecontroller` mode:** Same step override, plus the existing science pipeline disables:

```toml
[nosensor_nochargecontroller]
    [nosensor_nochargecontroller.main.steps.sunsaver_input]
        _module_path = "null_input"
        _class_name = "NullInput"
        _is_plugin = true
        _preset = "sunsaver_mppt_15l.inputs.ram"

    [nosensor_nochargecontroller.main.steps.state_monitor]
        enable_drive_checks = false

    [nosensor_nochargecontroller.main.pipelines.science_ingest]
        _enabled = false

    [nosensor_nochargecontroller.main.pipelines.science_disk_write]
        _enabled = false

    [nosensor_nochargecontroller.main.pipelines.science_header_decode]
        _enabled = false

    [nosensor_nochargecontroller.main.pipelines.realtime]
        _enabled = false

    [nosensor_nochargecontroller.main.pipelines.rsync_hamma_realtime]
        _enabled = false
```

### Config Merge Verification

The code reviewer verified the merge path in `brokkr/pipeline/builder.py`:

1. `_setup_preset()` resolves `_preset` into the sunsaver input's full config (including data_types)
2. `update_dict_recursive(preset, subobject)` merges the step definition (with mode overlay) on top
3. `_module_path` and `_class_name` from the overlay overwrite the sunsaver preset's values
4. `data_types` from the preset survives because the step definition doesn't define its own

## What Changes

| Item | Change |
|------|--------|
| `plugins/null_input.py` | New file (5 lines) |
| `config/main.toml` | Add `[steps.sunsaver_input]`, update `monitor_input_steps` reference |
| `config/mode.toml` | Rewrite `nochargecontroller` and `nosensor_nochargecontroller` sections |

## What Doesn't Change

- Brokkr core — no changes
- Sindri — no changes
- Sunsaver preset — untouched
- Default mode behavior — identical
- `nosensor` mode — untouched (has charge controller, no sensor)

## Side Effects

**HAM-77 resolved:** With charge controller columns present as NA values, `state_monitor.check_power()` and `check_battery_voltage()` will read `NaN` instead of raising `KeyError`. NaN comparisons (`NaN < threshold`) return `False`, so no spurious alerts are generated.

**`na_on_start` compatibility:** The telemetry pipeline has `na_on_start = true`. On the first cycle, brokkr sends `NASentinel` to input steps, which triggers the same `decode_data(None)` → `output_na_values()` path as NullInput's normal operation. NullInput produces identical output on both first and subsequent cycles — a desirable property.

## Testing

1. **Unit test:** Verify NullInput produces NA values for sunsaver data_types
2. **Config test:** Verify `nochargecontroller` mode renders with the step override (not a removed input)
3. **Integration:** Deploy to mj05, confirm:
   - Brokkr starts without errors
   - Telemetry CSV has all columns (charge controller fields filled with NA)
   - Sindri-client processes without crashing
   - state_monitor stops logging KeyErrors
