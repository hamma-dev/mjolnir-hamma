# NullInput Plugin for nochargecontroller Mode — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve telemetry CSV schema across all modes by replacing the sunsaver modbus input with a NullInput plugin in nochargecontroller mode, fixing HAM-86 and HAM-77.

**Architecture:** A mjolnir-hamma plugin (`NullInput`) that always returns `None` from `read_raw_data()`, causing brokkr's decoder to emit NA values for all sunsaver data_types. The telemetry pipeline references the sunsaver via a named step, which the mode overlay swaps to NullInput while inheriting data_types from the sunsaver preset.

**Tech Stack:** Python 3.6+, brokkr pipeline framework, TOML config

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `plugins/null_input.py` | Create | NullInput plugin — ValueInputStep that returns None |
| `config/main.toml` | Modify | Add named step `sunsaver_input`, update `monitor_input_steps` |
| `config/mode.toml` | Modify | Rewrite `nochargecontroller` and `nosensor_nochargecontroller` |
| `tests/python/test_null_input.py` | Create | Unit tests for NullInput plugin |

---

## Chunk 1: NullInput Plugin and Tests

### Task 1: Create NullInput Plugin

**Files:**
- Create: `plugins/null_input.py`
- Test: `tests/python/test_null_input.py`

- [ ] **Step 1: Write the test file**

Create `tests/python/test_null_input.py`. Follow the same mock-loading pattern used by `tests/python/test_compress_plugin.py` — mock `brokkr.pipeline.baseinput.ValueInputStep` so the plugin can be imported without brokkr installed.

```python
"""Unit tests for plugins/null_input.py — NullInput plugin."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "null_input.py"


class MockValueInputStep:
    """Stand-in for brokkr.pipeline.baseinput.ValueInputStep."""

    def __init__(self, data_types=None, **kwargs):
        self.data_types = data_types or []
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_null_input_module():
    """Load the null_input plugin with mocked brokkr dependency."""
    mock_baseinput = MagicMock()
    mock_baseinput.ValueInputStep = MockValueInputStep

    mock_pipeline = MagicMock()
    mock_pipeline.baseinput = mock_baseinput

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.baseinput = mock_baseinput

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.baseinput": mock_baseinput,
    }):
        spec = importlib.util.spec_from_file_location(
            "null_input", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def null_input_module():
    return load_null_input_module()


class TestNullInput:
    def test_class_exists(self, null_input_module):
        """NullInput class should exist and be a subclass of the mock."""
        assert hasattr(null_input_module, "NullInput")

    def test_inherits_value_input_step(self, null_input_module):
        """NullInput should inherit from ValueInputStep."""
        assert issubclass(
            null_input_module.NullInput, MockValueInputStep)

    def test_read_raw_data_returns_none(self, null_input_module):
        """read_raw_data should always return None."""
        instance = null_input_module.NullInput(data_types=[])
        result = instance.read_raw_data()
        assert result is None

    def test_read_raw_data_ignores_input(self, null_input_module):
        """read_raw_data should return None regardless of input_data."""
        instance = null_input_module.NullInput(data_types=[])
        result = instance.read_raw_data(input_data={"some": "data"})
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mjolnir-hamma && conda run -n sci pytest tests/python/test_null_input.py -v`
Expected: FAIL — `plugins/null_input.py` does not exist yet.

- [ ] **Step 3: Write the plugin**

Create `plugins/null_input.py`:

```python
"""Input step that produces NA values for all defined data types."""
import brokkr.pipeline.baseinput


class NullInput(brokkr.pipeline.baseinput.ValueInputStep):
    def read_raw_data(self, input_data=None):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mjolnir-hamma && conda run -n sci pytest tests/python/test_null_input.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/null_input.py tests/python/test_null_input.py
git commit -m "feat: add NullInput plugin for nochargecontroller mode (HAM-86)"
```

---

## Chunk 2: Config Changes

### Task 2: Add Named Step and Update Telemetry Pipeline

**Files:**
- Modify: `config/main.toml:36-38` (steps table) and `config/main.toml:166-173` (monitor_input_steps comment and list)

- [ ] **Step 1: Add named step `sunsaver_input` to `[steps]` table**

In `config/main.toml`, replace lines 36–37:

```toml
# List custom steps or override default preset settings here under the [steps] key
[steps]
```

With:

```toml
# List custom steps or override default preset settings here under the [steps] key
[steps]
    # Named step wrapping sunsaver preset — allows mode overlay to swap class
    [steps.sunsaver_input]
        _preset = "sunsaver_mppt_15l.inputs.ram"
```

- [ ] **Step 2: Update `monitor_input_steps` to use named step**

In `config/main.toml`, change line 170 from:

```toml
            "sunsaver_mppt_15l.inputs.ram",
```

to:

```toml
            "sunsaver_input",
```

- [ ] **Step 3: Remove the now-stale comment on line 166**

Change line 166 from:

```
        # NOTE: If you change this list, also update the nochargecontroller mode in mode.toml
```

to:

```
        # NOTE: sunsaver_input is a named step so nochargecontroller mode can override its class
```

- [ ] **Step 4: Commit**

```bash
git add config/main.toml
git commit -m "config: add named sunsaver_input step for mode overlay support (HAM-86)"
```

### Task 3: Rewrite Mode Overlay Sections

**Files:**
- Modify: `config/mode.toml:68-106`

- [ ] **Step 1: Replace `nochargecontroller` section (lines 68-77)**

Replace:

```toml
# Preset for units without a charge controller connected
# Removes sunsaver input from telemetry
[nochargecontroller]
    [nochargecontroller.main.pipelines.telemetry]
        monitor_input_steps = [
            "builtins.inputs.current_time",
            "builtins.inputs.run_time",
            "hamma2.inputs.ping",
            "hamma2.inputs.hs",
        ]
```

With:

```toml
# Preset for units without a charge controller connected
# Swaps sunsaver modbus input with NullInput to preserve CSV schema with NA values
[nochargecontroller]
    [nochargecontroller.main.steps.sunsaver_input]
        _module_path = "null_input"
        _class_name = "NullInput"
        _is_plugin = true
        _preset = "sunsaver_mppt_15l.inputs.ram"
```

- [ ] **Step 2: Replace `nosensor_nochargecontroller` section (lines 79-106)**

Replace:

```toml
# Preset for units with neither sensor nor charge controller
# Combines nosensor + nochargecontroller overrides
[nosensor_nochargecontroller]
    [nosensor_nochargecontroller.main.steps.state_monitor]
        enable_drive_checks = false

    [nosensor_nochargecontroller.main.pipelines.telemetry]
        monitor_input_steps = [
            "builtins.inputs.current_time",
            "builtins.inputs.run_time",
            "hamma2.inputs.ping",
            "hamma2.inputs.hs",
        ]

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

With:

```toml
# Preset for units with neither sensor nor charge controller
# Combines nosensor + nochargecontroller overrides
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

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `cd mjolnir-hamma && conda run -n sci pytest tests/shell tests/unified tests/python -v`
Expected: All existing tests PASS.

- [ ] **Step 4: Commit**

```bash
git add config/mode.toml
git commit -m "config: swap sunsaver with NullInput in nochargecontroller modes (HAM-86)"
```

---

## Chunk 3: Deployment to mj05

### Task 4: Deploy and Verify on mj05

This task is manual — run commands on the sensor via SSH.

- [ ] **Step 1: Pull updated repo on mj05**

```bash
ssh mjolnir05 "cd /home/pi/dev/mjolnir-hamma && git pull origin 0.3.x"
```

- [ ] **Step 2: Reset mj05 mode.toml to default**

The local `mode.toml` override (`mode = "nochargecontroller"`) is still needed. Verify it's set:

```bash
ssh mjolnir05 "cat /home/pi/.config/brokkr/hamma/mode.toml"
```

Expected:
```
config_version = 1
mode = "nochargecontroller"
```

- [ ] **Step 3: Re-enable and restart sindri-client**

```bash
ssh mjolnir05 "sudo systemctl enable sindri-hamma-client.service && sudo systemctl restart brokkr-hamma-default.service sindri-hamma-client.service"
```

- [ ] **Step 4: Wait ~90 seconds, then verify brokkr telemetry CSV has charge controller columns**

```bash
ssh mjolnir05 "head -2 \$(ls -t /home/pi/brokkr/hamma/telemetry/*.csv | head -1)"
```

Expected: CSV header includes `adc_vb_f`, `adc_vl_f`, `adc_il_f`, `power_out`, etc. Data row has `NA` for those columns.

- [ ] **Step 5: Verify sindri-client is running (not crash-looping)**

```bash
ssh mjolnir05 "systemctl is-active sindri-hamma-client.service"
```

Expected: `active`

- [ ] **Step 6: Verify no state_monitor KeyErrors in recent logs**

```bash
ssh mjolnir05 "journalctl -u brokkr-hamma-default --since '2 min ago' --no-pager | grep -c KeyError"
```

Expected: `0`
