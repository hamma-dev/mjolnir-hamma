# Server-side Threshold & Gain Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator set a HAMMA2 sensor's trigger threshold (input-referred mV) and FCM gain (level 0–3) from the VPS, for one/many/all sensors, with an optional `--persist` flag that survives a DAS reset.

**Architecture:** Add clearly-named functions + CLI subcommands to the Pi-side `scripts/ags.py` (which owns the AGS command socket `10.10.10.1:8082`); add dispatch actions to the VPS-side `server/mjol_array.py` that invoke `ags.py` over the existing autossh tunnel, reusing its `-a/-p` fan-out — a near-clone of the existing `--trigger` action. `--persist` rewrites the matching line of the sensor's `/ags/scripts/startup` script via `ssh hamma`.

**Tech Stack:** Python 3.6+ (stdlib only: `argparse`, `socket`, `subprocess`), pytest with `importlib.util.spec_from_file_location` module loading and `unittest.mock`.

## Global Constraints

- **Python 3.6+ compatible.** No walrus, no `subprocess.run(capture_output=...)` (3.7+) — use `stdout=subprocess.PIPE, stderr=subprocess.PIPE`.
- **stdlib only** in `ags.py` and `mjol_array.py` action paths (no pandas/numpy at import scope).
- **Threshold conversion:** `ags = millivolts / 1000 * 6.024`; inverse `mv = ags / 6.024 * 1000`. `GAIN_FACTOR = 6.024`.
- **No hard upper cap** on threshold mV (firmware enforces); reject only `mv < 0`. High mV is **sent silently** (no client warning).
- **Gain registers (bare hex):** FAST-E = `"8"` (0x08), SLOW-E = `"10"` (0x10). Gain level ∈ {0,1,2,3}.
- **Threshold channels:** {1, 2} only (reject 3–8).
- **Pi script path on sensors:** `/home/pi/dev/mjolnir-hamma/scripts/ags.py`.
- **AGS host ssh alias (from the Pi):** `hamma`; startup script path `/ags/scripts/startup`.
- **Tests live in** `tests/python/`; load modules via `importlib.util.spec_from_file_location`.
- **Commit** after every passing task. Branch: `feature/server-threshold-gain-control`.

---

## File Structure

- **Modify** `scripts/ags.py` — add `GAIN_FACTOR`, gain register map, conversion helpers (`mv_to_ags`, `ags_to_mv`), `set_threshold`, `set_gain`, startup-file helpers (`rewrite_startup`, `parse_startup_state`, `persist_startup`), and CLI subcommand branching in `main()`. Keep the existing generic passthrough.
- **Modify** `server/mjol_array.py` — add `_run_ags_command` helper, refactor `trigger()` onto it, add `set_threshold`/`set_gain` static methods + `set_threshold_array`/`set_gain_array` wrappers, add `--set-threshold`/`--set-gain`/`--persist` CLI options and `main()` dispatch.
- **Create** `tests/python/test_ags.py` — unit tests for all new `ags.py` functions + CLI.
- **Modify** `tests/python/test_mjol_array.py` — tests for the new actions, wrappers, helper, and CLI dispatch.

---

## Task 1: ags.py — threshold conversion helpers

**Files:**
- Modify: `scripts/ags.py` (add constants + two functions near the top, after the existing `SOCKET_BUFFER` constant)
- Test: `tests/python/test_ags.py` (create)

**Interfaces:**
- Produces: `GAIN_FACTOR = 6.024`; `mv_to_ags(millivolts) -> float` (raises `ValueError` if `millivolts < 0`); `ags_to_mv(ags) -> float`.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_ags.py`:

```python
"""Tests for ags.py — AGS command wrapper (threshold/gain control)."""

import importlib.util
import pathlib

import pytest
from unittest.mock import patch, MagicMock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "ags.py"


def load_ags():
    spec = importlib.util.spec_from_file_location("ags", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ags():
    return load_ags()


class TestConversion:
    def test_mv_to_ags_known_points(self, ags):
        assert ags.mv_to_ags(830) == pytest.approx(5.0, abs=1e-3)
        assert ags.mv_to_ags(83) == pytest.approx(0.5, abs=1e-3)
        assert ags.mv_to_ags(0) == 0.0

    def test_ags_to_mv_is_inverse(self, ags):
        assert ags.ags_to_mv(5.0) == pytest.approx(830, abs=1.0)
        assert ags.ags_to_mv(0.5) == pytest.approx(83, abs=1.0)

    def test_negative_mv_rejected(self, ags):
        with pytest.raises(ValueError):
            ags.mv_to_ags(-1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo> && python -m pytest tests/python/test_ags.py -v`
Expected: FAIL — `AttributeError: module 'ags' has no attribute 'mv_to_ags'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/ags.py`, after the existing constants (`SOCKET_BUFFER = 4096`), add:

```python
GAIN_FACTOR = 6.024  # fixed analog factor; input-referred volts = ags_value / GAIN_FACTOR


def mv_to_ags(millivolts):
    """Convert an input-referred threshold in mV to the AGS das value."""
    millivolts = float(millivolts)
    if millivolts < 0:
        raise ValueError("threshold mV must be non-negative")
    return millivolts / 1000.0 * GAIN_FACTOR


def ags_to_mv(ags):
    """Convert an AGS das value back to the input-referred threshold in mV."""
    return float(ags) / GAIN_FACTOR * 1000.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): threshold mV<->ags conversion helpers"
```

---

## Task 2: ags.py — set_threshold (live)

**Files:**
- Modify: `scripts/ags.py`
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Consumes: `mv_to_ags`, `send_ags_command`, `SENSOR_IP`, `AGS_COMMAND_PORT`.
- Produces: `set_threshold(channel, millivolts, persist=False, host=SENSOR_IP, port=AGS_COMMAND_PORT) -> str`. Sends `das_set_threshold <channel> <ags>`. Validates `channel ∈ {1,2}`. `persist` is accepted but unused until Task 6. Helper `_format_ags(ags) -> str` returns `f"{ags:g}"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`:

```python
class TestSetThreshold:
    def test_sends_das_set_threshold(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            reply = ags.set_threshold(1, 830)
        sent = mock_send.call_args[0][0]
        toks = sent.split()
        assert toks[0] == "das_set_threshold"
        assert toks[1] == "1"
        assert float(toks[2]) == pytest.approx(5.0, abs=1e-3)
        assert reply == "OK"

    def test_channel_2(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_threshold(2, 83)
        toks = mock_send.call_args[0][0].split()
        assert toks[1] == "2"
        assert float(toks[2]) == pytest.approx(0.5, abs=1e-3)

    def test_rejects_bad_channel(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_threshold(3, 83)
            mock_send.assert_not_called()

    def test_high_mv_sent_without_cap(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_threshold(1, 5000)  # far above nominal full-scale
        toks = mock_send.call_args[0][0].split()
        assert float(toks[2]) == pytest.approx(ags.mv_to_ags(5000), abs=1e-3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestSetThreshold -v`
Expected: FAIL — `module 'ags' has no attribute 'set_threshold'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/ags.py`, after `send_ags_command`, add:

```python
THRESHOLD_CHANNELS = (1, 2)


def _format_ags(ags_value):
    return "{:g}".format(ags_value)


def set_threshold(channel, millivolts, persist=False,
                  host=SENSOR_IP, port=AGS_COMMAND_PORT):
    """Set a DAC trigger threshold (input-referred mV) on a HAMMA2 sensor."""
    channel = int(channel)
    if channel not in THRESHOLD_CHANNELS:
        raise ValueError("threshold channel must be 1 or 2")
    ags_value = mv_to_ags(millivolts)
    command = "das_set_threshold {} {}".format(channel, _format_ags(ags_value))
    return send_ags_command(command, host=host, port=port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py::TestSetThreshold -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): set_threshold sends das_set_threshold (mV input)"
```

---

## Task 3: ags.py — set_gain (live)

**Files:**
- Modify: `scripts/ags.py`
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Consumes: `send_ags_command`, `SENSOR_IP`, `AGS_COMMAND_PORT`.
- Produces: `GAIN_REGISTERS = {"fast-e": "8", "slow-e": "10"}`; `GAIN_LEVELS = (0,1,2,3)`; `set_gain(channel, level, persist=False, host=SENSOR_IP, port=AGS_COMMAND_PORT) -> str`. Sends `das_send_command <reg> <level>`. Validates channel name and level.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`:

```python
class TestSetGain:
    def test_fast_e_register(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_gain("fast-e", 2)
        assert mock_send.call_args[0][0] == "das_send_command 8 2"

    def test_slow_e_register(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_gain("slow-e", 0)
        assert mock_send.call_args[0][0] == "das_send_command 10 0"

    def test_rejects_bad_channel(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_gain("middle-e", 1)
            mock_send.assert_not_called()

    def test_rejects_bad_level(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_gain("fast-e", 4)
            mock_send.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestSetGain -v`
Expected: FAIL — `module 'ags' has no attribute 'set_gain'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/ags.py`, after `set_threshold`, add:

```python
GAIN_REGISTERS = {"fast-e": "8", "slow-e": "10"}
GAIN_LEVELS = (0, 1, 2, 3)


def set_gain(channel, level, persist=False,
             host=SENSOR_IP, port=AGS_COMMAND_PORT):
    """Set an FCM gain level (0-3) on a HAMMA2 sensor."""
    if channel not in GAIN_REGISTERS:
        raise ValueError("gain channel must be one of: "
                         + ", ".join(sorted(GAIN_REGISTERS)))
    level = int(level)
    if level not in GAIN_LEVELS:
        raise ValueError("gain level must be 0, 1, 2, or 3")
    register = GAIN_REGISTERS[channel]
    command = "das_send_command {} {}".format(register, level)
    return send_ags_command(command, host=host, port=port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py::TestSetGain -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): set_gain sends das_send_command to FCM gain registers"
```

---

## Task 4: ags.py — rewrite_startup (pure line-rewrite)

**Files:**
- Modify: `scripts/ags.py`
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Produces: `rewrite_startup(text, match_tokens, new_line) -> str`. Replaces the line whose leading whitespace-split tokens equal `match_tokens` with `new_line`, preserving all other lines and the trailing newline. If no match, inserts `new_line` immediately before the first `das_reset` line (or appends if none). Token-based match avoids `"8"` matching `"80"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`:

```python
STARTUP_SAMPLE = (
    "ds_enable\n"
    "das_enable\n"
    "das_set_threshold 1 0.5\n"
    "das_set_threshold 2 0\n"
    "das_send_command 8 1\n"
    "das_send_command 10 1\n"
    "das_set_mask 3\n"
    "das_reset\n"
)


class TestRewriteStartup:
    def test_replaces_matching_threshold_line(self, ags):
        out = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        assert "das_set_threshold 1 5\n" in out
        assert "das_set_threshold 1 0.5" not in out
        # other lines untouched
        assert "das_set_threshold 2 0\n" in out
        assert "das_send_command 8 1\n" in out

    def test_replaces_only_exact_register(self, ags):
        out = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_send_command", "8"], "das_send_command 8 3")
        assert "das_send_command 8 3\n" in out
        assert "das_send_command 10 1\n" in out  # 10 not touched by an "8" match

    def test_inserts_before_das_reset_when_absent(self, ags):
        text = "ds_enable\ndas_enable\ndas_reset\n"
        out = ags.rewrite_startup(
            text, ["das_set_threshold", "1"], "das_set_threshold 1 0.5")
        lines = out.splitlines()
        assert lines.index("das_set_threshold 1 0.5") < lines.index("das_reset")

    def test_idempotent(self, ags):
        once = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        twice = ags.rewrite_startup(
            once, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        assert once == twice

    def test_preserves_trailing_newline_absence(self, ags):
        text = "ds_enable\ndas_reset"  # no trailing newline
        out = ags.rewrite_startup(text, ["ds_enable"], "ds_enable")
        assert not out.endswith("\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestRewriteStartup -v`
Expected: FAIL — `module 'ags' has no attribute 'rewrite_startup'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/ags.py`, add:

```python
def rewrite_startup(text, match_tokens, new_line):
    """Return startup-file text with the line matching match_tokens replaced.

    Match is on leading whitespace-split tokens (so "8" never matches "80").
    If no line matches, new_line is inserted before the first das_reset line,
    or appended if there is no das_reset.
    """
    match_tokens = list(match_tokens)
    n = len(match_tokens)
    lines = text.splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.split()[:n] == match_tokens:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        insert_at = None
        for i, line in enumerate(out):
            if line.split()[:1] == ["das_reset"]:
                insert_at = i
                break
        if insert_at is None:
            out.append(new_line)
        else:
            out.insert(insert_at, new_line)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py::TestRewriteStartup -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): rewrite_startup pure line-replace for persistence"
```

---

## Task 5: ags.py — parse_startup_state

**Files:**
- Modify: `scripts/ags.py`
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Consumes: `ags_to_mv`.
- Produces: `parse_startup_state(text) -> dict` with keys `threshold_1_mv`, `threshold_2_mv` (rounded to 1 decimal), `gain_fast`, `gain_slow` (ints). Missing entries are simply absent from the dict.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`:

```python
class TestParseStartupState:
    def test_parses_thresholds_and_gains(self, ags):
        state = ags.parse_startup_state(STARTUP_SAMPLE)
        assert state["threshold_1_mv"] == pytest.approx(83, abs=1.0)
        assert state["threshold_2_mv"] == 0.0
        assert state["gain_fast"] == 1
        assert state["gain_slow"] == 1

    def test_missing_lines_absent(self, ags):
        state = ags.parse_startup_state("ds_enable\ndas_reset\n")
        assert "threshold_1_mv" not in state
        assert "gain_fast" not in state
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestParseStartupState -v`
Expected: FAIL — `module 'ags' has no attribute 'parse_startup_state'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/ags.py`, add:

```python
def parse_startup_state(text):
    """Parse persisted threshold (mV) and gain levels from startup-file text."""
    state = {}
    for line in text.splitlines():
        toks = line.split()
        if len(toks) >= 3 and toks[0] == "das_set_threshold":
            channel = toks[1]
            if channel in ("1", "2"):
                state["threshold_{}_mv".format(channel)] = round(
                    ags_to_mv(float(toks[2])), 1)
        elif len(toks) >= 3 and toks[0] == "das_send_command":
            if toks[1] == "8":
                state["gain_fast"] = int(toks[2])
            elif toks[1] == "10":
                state["gain_slow"] = int(toks[2])
    return state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py::TestParseStartupState -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): parse_startup_state reads persisted threshold/gain"
```

---

## Task 6: ags.py — persist_startup + wire persist into setters

**Files:**
- Modify: `scripts/ags.py`
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Consumes: `rewrite_startup`, `subprocess`, `set_threshold`, `set_gain`.
- Produces: `AGS_SSH_HOST = "hamma"`; `STARTUP_PATH = "/ags/scripts/startup"`; `persist_startup(match_tokens, new_line, host=AGS_SSH_HOST) -> None` (reads the file via `ssh host cat`, rewrites, writes back atomically via temp+mv). `set_threshold`/`set_gain` now honor `persist=True` by calling `persist_startup` after the live command.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`. Add `import subprocess` to the test file's imports (top of file).

```python
class TestPersist:
    def test_persist_startup_reads_then_writes(self, ags):
        read_result = MagicMock(returncode=0, stdout=STARTUP_SAMPLE.encode())
        write_result = MagicMock(returncode=0)
        with patch.object(ags, "subprocess") as mock_sub:
            mock_sub.run.side_effect = [read_result, write_result]
            ags.persist_startup(["das_set_threshold", "1"],
                                "das_set_threshold 1 5")
        # second call is the write; its input carries the rewritten file
        write_call = mock_sub.run.call_args_list[1]
        written = write_call.kwargs["input"].decode()
        assert "das_set_threshold 1 5\n" in written
        assert "das_set_threshold 2 0\n" in written

    def test_persist_raises_on_read_failure(self, ags):
        with patch.object(ags, "subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=1, stdout=b"",
                                                  stderr=b"no route")
            with pytest.raises(RuntimeError):
                ags.persist_startup(["ds_enable"], "ds_enable")

    def test_set_threshold_persist_calls_persist_startup(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_threshold(1, 830, persist=True)
        match_tokens, new_line = mock_persist.call_args[0][0], mock_persist.call_args[0][1]
        assert match_tokens == ["das_set_threshold", "1"]
        assert new_line.split()[:2] == ["das_set_threshold", "1"]

    def test_set_gain_persist_calls_persist_startup(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_gain("fast-e", 3, persist=True)
        assert mock_persist.call_args[0][0] == ["das_send_command", "8"]
        assert mock_persist.call_args[0][1] == "das_send_command 8 3"

    def test_set_threshold_no_persist_skips(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_threshold(1, 830, persist=False)
            mock_persist.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestPersist -v`
Expected: FAIL — `module 'ags' has no attribute 'persist_startup'`

- [ ] **Step 3: Write minimal implementation**

Add `import subprocess` to the top of `scripts/ags.py` (with the other stdlib imports). Then add:

```python
AGS_SSH_HOST = "hamma"
STARTUP_PATH = "/ags/scripts/startup"


def persist_startup(match_tokens, new_line, host=AGS_SSH_HOST):
    """Rewrite one line of the sensor's startup script via ssh, atomically."""
    read = subprocess.run(
        ["ssh", host, "cat {}".format(STARTUP_PATH)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if read.returncode != 0:
        raise RuntimeError("could not read {}: {}".format(
            STARTUP_PATH, read.stderr.decode(errors="replace")))
    new_text = rewrite_startup(read.stdout.decode(), match_tokens, new_line)
    write_cmd = "cat > {0}.tmp && mv {0}.tmp {0}".format(STARTUP_PATH)
    write = subprocess.run(
        ["ssh", host, write_cmd], input=new_text.encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if write.returncode != 0:
        raise RuntimeError("could not write {}: {}".format(
            STARTUP_PATH, write.stderr.decode(errors="replace")))
```

Then update `set_threshold` — replace its `return send_ags_command(...)` line so it captures the reply, optionally persists, and returns:

```python
    reply = send_ags_command(command, host=host, port=port)
    if persist:
        persist_startup(["das_set_threshold", str(channel)],
                        "das_set_threshold {} {}".format(channel, _format_ags(ags_value)))
    return reply
```

And update `set_gain` similarly:

```python
    reply = send_ags_command(command, host=host, port=port)
    if persist:
        persist_startup(["das_send_command", register],
                        "das_send_command {} {}".format(register, level))
    return reply
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py -v`
Expected: PASS (all ags tests, incl. earlier ones still green)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): --persist rewrites /ags/scripts/startup via ssh hamma"
```

---

## Task 7: ags.py — CLI subcommands

**Files:**
- Modify: `scripts/ags.py` (rewrite `main()`)
- Test: `tests/python/test_ags.py`

**Interfaces:**
- Consumes: `set_threshold`, `set_gain`, `parse_startup_state`, `send_ags_command`, `subprocess`.
- Produces: `main(argv=None)` that branches on the first arg: `set-threshold <ch> <mv> [--persist]`, `set-gain <fast-e|slow-e> <level> [--persist]`, `get-state`, else the existing generic passthrough (`ags.py <command> [--host H] [--port P]`).

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_ags.py`:

```python
class TestCli:
    def test_set_threshold_subcommand(self, ags):
        with patch.object(ags, "set_threshold", return_value="OK") as mock_set:
            ags.main(["set-threshold", "1", "830"])
        assert mock_set.call_args.kwargs.get("persist", False) is False
        args = mock_set.call_args[0]
        assert int(args[0]) == 1 and float(args[1]) == 830

    def test_set_threshold_persist_flag(self, ags):
        with patch.object(ags, "set_threshold", return_value="OK") as mock_set:
            ags.main(["set-threshold", "1", "830", "--persist"])
        assert mock_set.call_args.kwargs["persist"] is True

    def test_set_gain_subcommand(self, ags):
        with patch.object(ags, "set_gain", return_value="OK") as mock_set:
            ags.main(["set-gain", "fast-e", "2"])
        args = mock_set.call_args[0]
        assert args[0] == "fast-e" and int(args[1]) == 2

    def test_get_state_subcommand(self, ags):
        result = MagicMock(returncode=0, stdout=STARTUP_SAMPLE.encode())
        with patch.object(ags, "subprocess") as mock_sub:
            mock_sub.run.return_value = result
            ags.main(["get-state"])  # should not raise

    def test_passthrough_preserved(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.main(["das_reset"])
        assert mock_send.call_args[0][0] == "das_reset"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_ags.py::TestCli -v`
Expected: FAIL — `main()` takes no argv / subcommands not handled (TypeError or send_ags_command called with "set-threshold")

- [ ] **Step 3: Write minimal implementation**

Add `import sys` to the imports if not present. Replace the existing `main()` in `scripts/ags.py` with:

```python
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] == "set-threshold":
        parser = argparse.ArgumentParser(prog="ags.py set-threshold")
        parser.add_argument("channel", type=int, help="threshold channel (1 or 2)")
        parser.add_argument("millivolts", type=float,
                            help="input-referred threshold in mV")
        parser.add_argument("--persist", action="store_true",
                            help="also write /ags/scripts/startup")
        ns = parser.parse_args(argv[1:])
        print(set_threshold(ns.channel, ns.millivolts, persist=ns.persist))
        return

    if argv and argv[0] == "set-gain":
        parser = argparse.ArgumentParser(prog="ags.py set-gain")
        parser.add_argument("channel", choices=sorted(GAIN_REGISTERS),
                            help="gain channel")
        parser.add_argument("level", type=int, help="gain level (0-3)")
        parser.add_argument("--persist", action="store_true",
                            help="also write /ags/scripts/startup")
        ns = parser.parse_args(argv[1:])
        print(set_gain(ns.channel, ns.level, persist=ns.persist))
        return

    if argv and argv[0] == "get-state":
        read = subprocess.run(
            ["ssh", AGS_SSH_HOST, "cat {}".format(STARTUP_PATH)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if read.returncode != 0:
            print("[FAIL] could not read startup: "
                  + read.stderr.decode(errors="replace"))
            return
        state = parse_startup_state(read.stdout.decode())
        for key in ("threshold_1_mv", "threshold_2_mv", "gain_fast", "gain_slow"):
            if key in state:
                print("{}: {}".format(key, state[key]))
        return

    # Generic passthrough (original behaviour)
    parser_main = argparse.ArgumentParser(
        description=(
            "Send an AGS command to a HAMMA2 sensor. "
            "Subcommands: set-threshold, set-gain, get-state. "
            "Otherwise send a raw command; send 'help' for the sensor's list."),
        argument_default=argparse.SUPPRESS)
    parser_main.add_argument("command", nargs="?", default="help",
                             help="The AGS command to send.")
    parser_main.add_argument("--host", help="Sensor host/IP (default 10.10.10.1)")
    parser_main.add_argument("--port", help="AGS command port (default 8082)")
    parsed_args = parser_main.parse_args(argv)
    print(send_ags_command(**vars(parsed_args)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_ags.py -v`
Expected: PASS (all ags tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/ags.py tests/python/test_ags.py
git commit -m "feat(ags): set-threshold/set-gain/get-state CLI subcommands"
```

---

## Task 8: mjol_array.py — shared AGS exec helper + refactor trigger

**Files:**
- Modify: `server/mjol_array.py`
- Test: `tests/python/test_mjol_array.py`

**Interfaces:**
- Produces: `MjolnirArray._run_ags_command(port, ags_args, action_label, quiet=False, timeout=30)` — checks the tunnel (`status`), runs `ags.py` with `ags_args` over `_pi_ssh_cmd(port)`, surfaces stdout/stderr, flags `[SKIP]`/`[FAIL]`. `trigger()` is refactored to call it (behaviour unchanged: same `ags.py <command>` invocation, 30s timeout, `[SKIP]` when down).

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_mjol_array.py`:

```python
class TestRunAgsCommand:
    def test_skips_when_tunnel_down(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "status", return_value=False):
            with patch.object(mjol, "subprocess") as mock_sub:
                mjol.MjolnirArray._run_ags_command(10002, ["das_reset"], "x")
                mock_sub.run.assert_not_called()
        assert "[SKIP]" in capsys.readouterr().out

    def test_runs_ags_with_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"OK", stderr=b"")
                mock_sub.TimeoutExpired = Exception
                mjol.MjolnirArray._run_ags_command(
                    10002, ["set-threshold", "1", "830"], "set thr")
        cmd = mock_sub.run.call_args[0][0]
        assert "/home/pi/dev/mjolnir-hamma/scripts/ags.py" in cmd
        assert cmd[-3:] == ["set-threshold", "1", "830"]

    def test_trigger_still_invokes_ags_command(self, mjol):
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"OK", stderr=b"")
                mock_sub.TimeoutExpired = Exception
                mjol.MjolnirArray.trigger(10002)
        cmd = mock_sub.run.call_args[0][0]
        assert "/home/pi/dev/mjolnir-hamma/scripts/ags.py" in cmd
        assert "das_manual_trigger" in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_mjol_array.py::TestRunAgsCommand -v`
Expected: FAIL — `_run_ags_command` does not exist

- [ ] **Step 3: Write minimal implementation**

In `server/mjol_array.py`, add this static method to `MjolnirArray` (next to `trigger`):

```python
    @staticmethod
    def _run_ags_command(port, ags_args, action_label, quiet=False, timeout=30):
        # Run ags.py on the Pi over its autossh tunnel with the given args.
        # port is fully qualified (10000 + unit number).
        sensor_num = port - 10000

        if not MjolnirArray.status(port):
            if not quiet:
                print(f"[SKIP] mj{sensor_num:02} (port {port}): tunnel down, "
                      f"{action_label} not sent.")
            return

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/mjolnir-hamma/scripts/ags.py'] + list(ags_args)

        if not quiet:
            print(f"--- mj{sensor_num:02}: {action_label} ---")

        try:
            out = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: ags.py did not complete "
                      f"in {timeout}s.")
            return
        except Exception as e:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: error running ags.py: {e}")
            return

        if quiet:
            return

        stdout = out.stdout.decode(errors="replace") if out.stdout else ""
        stderr = out.stderr.decode(errors="replace") if out.stderr else ""
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n")
        if out.returncode != 0:
            print(f"[FAIL] mj{sensor_num:02}: ags.py exited "
                  f"with code {out.returncode}.")
```

Then replace the body of `trigger()` with a delegating call (keep the signature):

```python
    @staticmethod
    def trigger(port, command="das_manual_trigger", quiet=False):
        # Send an AGS command (default: a manual trigger) to a sensor.
        MjolnirArray._run_ags_command(
            port, [command], f"sending AGS '{command}'", quiet=quiet)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_mjol_array.py -v`
Expected: PASS (new tests + existing trigger tests still green)

- [ ] **Step 5: Commit**

```bash
git add server/mjol_array.py tests/python/test_mjol_array.py
git commit -m "refactor(mjol_array): shared _run_ags_command, trigger delegates to it"
```

---

## Task 9: mjol_array.py — set_threshold/set_gain methods + array wrappers

**Files:**
- Modify: `server/mjol_array.py`
- Test: `tests/python/test_mjol_array.py`

**Interfaces:**
- Consumes: `_run_ags_command`.
- Produces:
  - `MjolnirArray.set_threshold(port, channel, millivolts, persist=False, quiet=False)`
  - `MjolnirArray.set_gain(port, channel, level, persist=False, quiet=False)`
  - `set_threshold_array(self, ports=None, channel=None, millivolts=None, persist=False)`
  - `set_gain_array(self, ports=None, channel=None, level=None, persist=False)`
  - Each builds the right `ags.py` args (appending `--persist` when set) and fans out like `trigger_array`.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_mjol_array.py`:

```python
class TestSetThresholdGain:
    def test_set_threshold_builds_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_threshold(10002, 1, 830)
        port, ags_args = mock_run.call_args[0][0], mock_run.call_args[0][1]
        assert port == 10002
        assert ags_args == ["set-threshold", "1", "830"]

    def test_set_threshold_persist_appends_flag(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_threshold(10002, 1, 830, persist=True)
        assert "--persist" in mock_run.call_args[0][1]

    def test_set_gain_builds_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_gain(10002, "fast-e", 2)
        assert mock_run.call_args[0][1] == ["set-gain", "fast-e", "2"]

    def test_set_threshold_array_fans_out(self, mjol):
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, "set_threshold") as mock_set:
            arr.set_threshold_array(channel=1, millivolts=830)
        called_ports = [c[0][0] for c in mock_set.call_args_list]
        assert called_ports == [10002, 10003]

    def test_set_gain_array_explicit_ports(self, mjol):
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, "set_gain") as mock_set:
            arr.set_gain_array(ports=["2"], channel="slow-e", level=0)
        assert mock_set.call_args_list[0][0][0] == 10002
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_mjol_array.py::TestSetThresholdGain -v`
Expected: FAIL — methods do not exist

- [ ] **Step 3: Write minimal implementation**

In `server/mjol_array.py`, add to `MjolnirArray` (after `trigger`):

```python
    @staticmethod
    def set_threshold(port, channel, millivolts, persist=False, quiet=False):
        ags_args = ["set-threshold", str(channel), str(millivolts)]
        if persist:
            ags_args.append("--persist")
        MjolnirArray._run_ags_command(
            port, ags_args,
            f"set threshold ch{channel} = {millivolts} mV"
            + (" (persist)" if persist else ""),
            quiet=quiet)

    @staticmethod
    def set_gain(port, channel, level, persist=False, quiet=False):
        ags_args = ["set-gain", str(channel), str(level)]
        if persist:
            ags_args.append("--persist")
        MjolnirArray._run_ags_command(
            port, ags_args,
            f"set gain {channel} = {level}"
            + (" (persist)" if persist else ""),
            quiet=quiet)
```

And add the array wrappers (after `trigger_array`):

```python
    def set_threshold_array(self, ports=None, channel=None, millivolts=None,
                            persist=False):
        if ports is None:
            ports = [10000 + i for i in self.sensors]
        else:
            ports = [10000 + int(p) for p in ports]
        for p in ports:
            MjolnirArray.set_threshold(p, channel, millivolts, persist=persist)

    def set_gain_array(self, ports=None, channel=None, level=None,
                       persist=False):
        if ports is None:
            ports = [10000 + i for i in self.sensors]
        else:
            ports = [10000 + int(p) for p in ports]
        for p in ports:
            MjolnirArray.set_gain(p, channel, level, persist=persist)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_mjol_array.py::TestSetThresholdGain -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add server/mjol_array.py tests/python/test_mjol_array.py
git commit -m "feat(mjol_array): set_threshold/set_gain methods + array fan-out"
```

---

## Task 10: mjol_array.py — CLI options + dispatch

**Files:**
- Modify: `server/mjol_array.py` (the `main()` argparse + dispatch)
- Test: `tests/python/test_mjol_array.py`

**Interfaces:**
- Produces: CLI `--set-threshold CHANNEL MV` (nargs=2), `--set-gain CHANNEL LEVEL` (nargs=2), `--persist` (store_true); dispatched in `main()` to the array wrappers.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_mjol_array.py`:

```python
class TestCliDispatch:
    def test_set_threshold_cli(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "1", "830"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["channel"] == "1" and kwargs["millivolts"] == "830"
        assert kwargs["persist"] is False

    def test_set_gain_cli_with_persist(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_gain_array") as mock_arr:
            mjol.main(["-a", "hamma", "--set-gain", "fast-e", "2", "--persist"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["channel"] == "fast-e" and kwargs["level"] == "2"
        assert kwargs["persist"] is True
```

Note: this requires `main()` to accept `argv` (Step 3 makes `main(argv=None)` and passes it to `parse_args(argv)`), so the test drives args directly rather than patching `sys.argv` (argparse reads its own `sys` reference, so patching `mjol.sys.argv` would not work).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/python/test_mjol_array.py::TestCliDispatch -v`
Expected: FAIL — unrecognized arguments `--set-threshold`

- [ ] **Step 3: Write minimal implementation**

Change the `main()` signature to `def main(argv=None):` and its parse call to
`parsed_args = arg_parser.parse_args(argv)` (argparse treats `argv=None` as "read
`sys.argv`", so the `__main__` call `main()` is unchanged). In `main()`, add these
arguments alongside `--trigger`:

```python
    arg_parser.add_argument("--set-threshold", nargs=2,
                            metavar=("CHANNEL", "MV"), default=None,
                            help="Set threshold: channel (1|2) and mV")
    arg_parser.add_argument("--set-gain", nargs=2,
                            metavar=("CHANNEL", "LEVEL"), default=None,
                            help="Set gain: channel (fast-e|slow-e) and level (0-3)")
    arg_parser.add_argument("--persist", action="store_true", default=False,
                            help="Also persist to /ags/scripts/startup")
```

Extend the dispatch chain in `main()` (insert before the `--up/--down` branch):

```python
    if parsed_args.do_status:
        _ = mj_array.status_array(ports=parsed_args.ports)
    elif parsed_args.do_trigger:
        mj_array.trigger_array(ports=parsed_args.ports)
    elif parsed_args.set_threshold is not None:
        channel, millivolts = parsed_args.set_threshold
        mj_array.set_threshold_array(
            ports=parsed_args.ports, channel=channel, millivolts=millivolts,
            persist=parsed_args.persist)
    elif parsed_args.set_gain is not None:
        channel, level = parsed_args.set_gain
        mj_array.set_gain_array(
            ports=parsed_args.ports, channel=channel, level=level,
            persist=parsed_args.persist)
    elif parsed_args.bring_up | parsed_args.bring_down:
        mj_array.updown_array(parsed_args.bring_up, ports=parsed_args.ports)
    else:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/python/test_mjol_array.py -v`
Expected: PASS (all mjol_array tests)

- [ ] **Step 5: Commit**

```bash
git add server/mjol_array.py tests/python/test_mjol_array.py
git commit -m "feat(mjol_array): --set-threshold/--set-gain/--persist CLI"
```

---

## Task 11: Full suite + E2E on mj02 (gated)

**Files:** none (verification + live test)

**Interfaces:** none.

- [ ] **Step 1: Run the whole Python suite**

Run: `python -m pytest tests/python/test_ags.py tests/python/test_mjol_array.py -v`
Expected: PASS (all). Also run `python -m pytest tests/python -q` to confirm no regressions elsewhere.

- [ ] **Step 2: Lint check (match repo tooling)**

Run: `python -m pyflakes scripts/ags.py server/mjol_array.py` (or the repo's configured linter). Expected: no errors.

- [ ] **Step 3: STOP — get explicit operator go-ahead for the live E2E on mj02**

E2E changes a live sensor's threshold/gain. Do NOT run until the user explicitly approves at this point. Record mj02's baseline first so it can be restored:
- `das_set_threshold 1 0.5`, `das_set_threshold 2 0`, `das_send_command 8 1`, `das_send_command 10 1`.

- [ ] **Step 4: E2E — live set + readback (after approval)**

From the VPS (or via the ssh-fleet MCP to mjolnir02), exercise the real path:
1. `ssh pi@localhost -p 10002 /home/pi/dev/mjolnir-hamma/scripts/ags.py set-threshold 1 100`
2. Trigger a fresh header (`ags.py das_manual_trigger`) and read it back — `mjol_array.py -p 2 --status` shows the Threshold column; confirm it reflects ~100 mV (within rounding).
3. `ags.py set-gain fast-e 2`; trigger; confirm header `threashold_3` (gain_fast) reads 2.
4. `ags.py get-state` reflects live persisted values.

- [ ] **Step 5: E2E — persist + inspect (after approval)**

1. `ags.py set-threshold 1 100 --persist`
2. `ssh hamma cat /ags/scripts/startup` — confirm ONLY the `das_set_threshold 1` line changed; all other lines byte-identical to baseline.
3. Confirm `ags.py get-state` shows the persisted value.

- [ ] **Step 6: E2E — server fan-out smoke (after approval)**

`mjol_array.py -p 2 --set-threshold 1 100` end-to-end over the tunnel; confirm `[OK]`-style output and no `[FAIL]`/`[SKIP]`.

- [ ] **Step 7: RESTORE mj02 baseline**

Re-apply baseline live AND persisted:
- `ags.py set-threshold 1 83 --persist` (0.5 ags), `ags.py set-threshold 2 0 --persist`
- `ags.py set-gain fast-e 1 --persist`, `ags.py set-gain slow-e 1 --persist`
- Verify `ags.py get-state` and `ssh hamma cat /ags/scripts/startup` match the recorded baseline.

- [ ] **Step 8: File a sensor-log issue**

`gh issue create -R hamma-dev/sensor-log` documenting the mj02 E2E touch (what changed, that baseline was restored). See the hamma-expert `sensor-log.md` protocol.

- [ ] **Step 9: Final commit / push branch**

```bash
git push -u origin feature/server-threshold-gain-control
```

---

## Deployment notes (post-merge, not part of TDD tasks)

- `scripts/ags.py` is consumed on **sensors** (mjolnir-hamma 0.4.x; `git pull` on each Pi).
- `server/mjol_array.py` is consumed on the **VPS** (`~/dev/mjolnir-hamma`). Confirm the VPS checkout's branch carries this change so both deployment lines pick it up.
- Confirm `ssh hamma` from each target Pi logs in with write access to `/ags/scripts/startup` (root-owned) before relying on `--persist` there.

---

## Self-Review

- **Spec coverage:** threshold-in-mV + conversion (Tasks 1–2), gain level 0–3 (Task 3), no hard cap / silent high-mV (Task 2 `test_high_mv_sent_without_cap`), `--persist` startup rewrite (Tasks 4,6), get-state readback (Tasks 5,7), server fan-out one/many/all (Tasks 9–10), reuse of trigger pattern + liveness gate (Task 8), verification + E2E + restore + sensor-log (Task 11), dual-deployment note (Deployment notes). ✔ all spec sections mapped.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code. ✔
- **Type consistency:** `set_threshold`/`set_gain` signatures match between `ags.py` (Tasks 2,3,6) and the `mjol_array` invocation strings (Task 9); `_run_ags_command(port, ags_args, action_label, ...)` arg order consistent across Tasks 8–9; `rewrite_startup`/`parse_startup_state`/`persist_startup` names consistent across Tasks 4–7. ✔
