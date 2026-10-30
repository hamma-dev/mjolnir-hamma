"""
Unit tests for plugins/rsync_server.py - the plugin that rsyncs files to the server.

These tests mock brokkr to test the plugin logic without requiring it to be
installed. subprocess.run is patched so no rsync is ever executed.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --- Module loading with mocked dependencies ---

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "rsync_server.py"


class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep."""

    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_rsync_module():
    """Load the rsync_server plugin with mocked dependencies."""
    mock_brokkr_base = MagicMock()
    mock_brokkr_base.OutputStep = MockOutputStep

    mock_brokkr_output = MagicMock()
    # The plugin renders local_path through brokkr; return it unchanged.
    mock_brokkr_output.render_output_filename.side_effect = (
        lambda output_path, filename_template: Path(output_path))

    mock_brokkr_pipeline = MagicMock()
    mock_brokkr_pipeline.base = mock_brokkr_base
    mock_brokkr_utils = MagicMock()
    mock_brokkr_utils.output = mock_brokkr_output

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_brokkr_pipeline
    mock_brokkr.utils = mock_brokkr_utils

    with patch.dict('sys.modules', {
        'brokkr': mock_brokkr,
        'brokkr.pipeline': mock_brokkr_pipeline,
        'brokkr.pipeline.base': mock_brokkr_base,
        'brokkr.utils': mock_brokkr_utils,
        'brokkr.utils.output': mock_brokkr_output,
    }):
        spec = importlib.util.spec_from_file_location(
            "rsync_server", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module


MODULE = load_rsync_module()
RsyncServer = MODULE.RsyncServer


def make_step(**kwargs):
    """Create an RsyncServer instance with sensible test defaults."""
    defaults = dict(
        server="hamma.dev",
        local_path="/home/pi/brokkr/hamma/noise_diag",
        server_path="/home/pi/noise_diag",
        update_time=1800,
    )
    defaults.update(kwargs)
    return RsyncServer(**defaults)


@pytest.fixture
def mock_run():
    """Patch subprocess.run as seen by the plugin."""
    with patch.object(MODULE.subprocess, 'run') as run:
        run.return_value = MagicMock(returncode=0, stdout="")
        yield run


def run_rsync(step, mock_run):
    """Call rsync() and return (command list, subprocess.run kwargs)."""
    step.rsync()
    args, kwargs = mock_run.call_args
    return args[0], kwargs


# --- Timeout ---

def test_timeout_defaults_to_ten_seconds(mock_run):
    """Existing steps that do not set a timeout keep the 10 s behaviour."""
    _, kwargs = run_rsync(make_step(), mock_run)
    assert kwargs["timeout"] == 10


def test_timeout_is_configurable(mock_run):
    _, kwargs = run_rsync(make_step(timeout=120), mock_run)
    assert kwargs["timeout"] == 120


# --- Partial dir ---

def test_no_partial_dir_by_default(mock_run):
    """The plot push command line is unchanged unless partial_dir is set."""
    cmd, _ = run_rsync(make_step(), mock_run)
    assert not any(arg.startswith("--partial-dir") for arg in cmd)


def test_partial_dir_is_passed_to_rsync(mock_run):
    cmd, _ = run_rsync(make_step(partial_dir=".rsync-partial"), mock_run)
    assert "--partial-dir=.rsync-partial" in cmd


def test_partial_dir_precedes_paths(mock_run):
    """rsync options must come before the source and destination arguments."""
    cmd, _ = run_rsync(make_step(partial_dir=".rsync-partial"), mock_run)
    assert cmd.index("--partial-dir=.rsync-partial") < len(cmd) - 2


# --- rsync I/O timeout and ssh options ---
# Python 3.7.3 (Buster) subprocess.run calls communicate() without a timeout
# after killing rsync on TimeoutExpired, so it can block until rsync's ssh
# child exits. rsync and ssh must give up on their own, before `timeout`.

def test_no_io_timeout_by_default(mock_run):
    cmd, _ = run_rsync(make_step(), mock_run)
    assert not any(arg.startswith("--timeout") for arg in cmd)


def test_io_timeout_is_passed_to_rsync(mock_run):
    cmd, _ = run_rsync(make_step(io_timeout=60), mock_run)
    assert "--timeout=60" in cmd
    assert cmd.index("--timeout=60") < len(cmd) - 2


def test_ssh_command_unchanged_by_default(mock_run):
    cmd, _ = run_rsync(make_step(), mock_run)
    ssh = cmd[cmd.index("-e") + 1]
    assert ssh == ("ssh -o StrictHostKeyChecking=no "
                   "-o UserKnownHostsFile=/dev/null")


def test_ssh_options_come_first_so_they_win(mock_run):
    """ssh takes the FIRST value of each option, so the configured options
    must precede the built-in ones or a per-unit override is silently lost."""
    cmd, _ = run_rsync(
        make_step(ssh_options="-o ConnectTimeout=20 -o BatchMode=yes"),
        mock_run)
    ssh = cmd[cmd.index("-e") + 1]
    assert ssh == ("ssh -o ConnectTimeout=20 -o BatchMode=yes "
                   "-o StrictHostKeyChecking=no "
                   "-o UserKnownHostsFile=/dev/null")


# --- Unchanged command shape ---

def test_source_and_destination(mock_run):
    cmd, _ = run_rsync(make_step(include="noise_hamma*.csv"), mock_run)
    assert cmd[-2] == "/home/pi/brokkr/hamma/noise_diag/"
    assert cmd[-1] == "pi@hamma.dev:/home/pi/noise_diag"
    assert "--include=noise_hamma*.csv" in cmd
    assert "--exclude=*" in cmd


# --- Deployed config (config/main.toml) ---

@pytest.fixture(scope="module")
def main_config():
    tomli = pytest.importorskip("tomli")
    with open(REPO_ROOT / "config" / "main.toml", "rb") as f:
        return tomli.load(f)


def test_noise_diag_rsync_step_config(main_config):
    """The noise CSVs are pushed flat into pi's home on the VPS."""
    step = main_config["steps"]["noise_diag_rsync"]
    assert step["_preset"] == "rsync_server.outputs.plugin"
    assert step["server"] == "hamma.dev"
    # Must match the noise_diag step's output_path and filename_template
    assert step["local_path"] == main_config["steps"]["noise_diag"]["output_path"]
    assert step["include"] == "noise_hamma*.csv"
    assert step["server_path"] == "/home/pi/noise_diag"
    assert step["update_time"] == 1790  # just under 3 x period_s (600)
    assert step["partial_dir"] == ".rsync-partial"


def test_noise_diag_rsync_timeouts_are_nested(main_config):
    """ssh gives up before rsync, and rsync before the Python timeout."""
    step = main_config["steps"]["noise_diag_rsync"]
    opts = dict(o.split("=", 1) for o in step["ssh_options"].split()
                if "=" in o)
    connect = int(opts["ConnectTimeout"])
    keepalive = (int(opts["ServerAliveInterval"])
                 * int(opts["ServerAliveCountMax"]))
    assert opts["BatchMode"] == "yes"
    assert max(connect, keepalive) < step["io_timeout"] < step["timeout"]


def test_noise_diag_rsync_has_its_own_pipeline(main_config):
    """A slow noise push must not share a pipeline with the plot push."""
    pipelines = main_config["pipelines"]
    noise = pipelines["rsync_noise_diag"]
    assert noise["_enabled"] is True
    assert noise["output_steps"] == ["noise_diag_rsync"]
    assert pipelines["rsync_hamma_realtime"]["output_steps"] == ["hamma_plot_rsync"]


def test_noise_diag_rsync_pipeline_period_bounds_retries(main_config):
    """A failed push retries every period_s; keep that well apart."""
    pipeline = main_config["pipelines"]["rsync_noise_diag"]
    step = main_config["steps"]["noise_diag_rsync"]
    assert pipeline["period_s"] > step["timeout"]
    assert pipeline["period_s"] < step["update_time"]


@pytest.fixture(scope="module")
def mode_config():
    tomli = pytest.importorskip("tomli")
    with open(REPO_ROOT / "config" / "mode.toml", "rb") as f:
        return tomli.load(f)


def test_noise_diag_rsync_disabled_in_test_mode(mode_config):
    """Test mode writes under ~/brokkr/test, so there is nothing to push."""
    pipelines = mode_config["test"]["main"]["pipelines"]
    assert pipelines["rsync_noise_diag"]["_enabled"] is False


@pytest.mark.parametrize("mode", ["nosensor", "nosensor_nochargecontroller"])
def test_noise_diag_rsync_stays_on_with_sensor_off(mode_config, mode):
    """A unit with its sensor off still pushes the backlog it already has."""
    pipelines = mode_config[mode]["main"].get("pipelines", {})
    assert pipelines.get("rsync_noise_diag", {}).get("_enabled", True) is True


def test_plot_rsync_keeps_default_timeout(main_config):
    """The existing plot push is not changed by this feature."""
    step = main_config["steps"]["hamma_plot_rsync"]
    for key in ("timeout", "partial_dir", "io_timeout", "ssh_options"):
        assert key not in step


# --- execute(): cadence, retry and skip behaviour (red team, PR #113) -------
import datetime  # noqa: E402
import fnmatch  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _t(seconds):
    return {"time": SimpleNamespace(
        value=datetime.datetime(2026, 1, 1) + datetime.timedelta(seconds=seconds))}


def _drive(step, times, returncodes=None, raise_at=()):
    """Run execute() at the given times; record when rsync() is called."""
    calls = []
    codes = list(returncodes or [])

    def fake_rsync():
        now = current[0]
        calls.append(now)
        if now in raise_at:
            raise subprocess.TimeoutExpired("rsync", step.timeout)
        step._last_returncode = codes.pop(0) if codes else 0
        return ""

    current = [None]
    step.rsync = fake_rsync
    for seconds in times:
        current[0] = seconds
        step.execute(_t(seconds))
    return calls


GRID = [i * 600 for i in range(10)]     # rsync_noise_diag period_s = 600


def test_default_step_waits_update_time_after_a_failure():
    """The plot push (defaults) is unchanged: a non-zero exit resets the clock."""
    step = make_step(update_time=1790)
    calls = _drive(step, GRID, returncodes=[255, 0, 0])
    assert calls == [1800, 3600, 5400]


def test_retry_on_failure_retries_on_the_next_period():
    """A non-zero rsync exit (ssh 255, --timeout 30) must not reset the clock."""
    step = make_step(update_time=1790, retry_on_failure=True)
    calls = _drive(step, GRID, returncodes=[255, 30, 0, 0])
    assert calls == [1800, 2400, 3000, 4800]


def test_timeout_expired_retries_on_the_next_period():
    step = make_step(update_time=1790)
    calls = _drive(step, GRID, raise_at={1800})
    assert calls == [1800, 2400, 4200]


def test_configured_cadence_is_every_30_min_on_the_600s_grid(main_config):
    """update_time sits just under a multiple of period_s, so jitter in the
    tick timing cannot flip the cadence between 30 and 40 minutes."""
    st = main_config["steps"]["noise_diag_rsync"]
    period = main_config["pipelines"]["rsync_noise_diag"]["period_s"]
    step = make_step(update_time=st["update_time"])
    for jitter_ms in (-5, 0, 5):
        step = make_step(update_time=st["update_time"])
        times = [i * period + jitter_ms / 1000 * (i % 2) for i in range(10)]
        calls = _drive(step, times)
        gaps = {round(b - a) for a, b in zip(calls, calls[1:])}
        assert gaps == {1800}, (jitter_ms, calls)


def test_random_phase_and_delay(monkeypatch):
    """jitter_s staggers units so a fleet-wide restart cannot synchronise them."""
    draws = iter([700.0, 12.5])
    monkeypatch.setattr(MODULE.random, "uniform", lambda a, b: next(draws))
    sleeps = []
    monkeypatch.setattr(MODULE.time, "sleep", sleeps.append)
    step = make_step(update_time=1790, jitter_s=60)
    calls = _drive(step, GRID)
    # first tick at t=0 starts the clock 700 s in the past -> due at 1200
    assert calls[0] == 1200
    assert sleeps == [12.5]


def test_no_jitter_by_default(monkeypatch):
    sleeps = []
    monkeypatch.setattr(MODULE.time, "sleep", sleeps.append)
    step = make_step(update_time=1790)
    _drive(step, GRID)
    assert sleeps == []


def test_skip_when_local_dir_is_missing(tmp_path):
    step = make_step(update_time=1790, skip_if_unchanged=True,
                     local_path=str(tmp_path / "absent"),
                     include="noise_hamma*.csv")
    assert _drive(step, GRID) == []


def test_skip_when_nothing_changed_since_last_push(tmp_path):
    f = tmp_path / "noise_hamma06_2026-10-01.csv"
    f.write_text("x\n")
    os.utime(f, (1000, 1000))
    step = make_step(update_time=1790, skip_if_unchanged=True,
                     local_path=str(tmp_path), include="noise_hamma*.csv")
    assert _drive(step, GRID) == [1800]          # pushes once, then quiet


def test_push_again_when_a_file_changes(tmp_path):
    f = tmp_path / "noise_hamma02_2026-10-01.csv"
    f.write_text("x\n")
    os.utime(f, (1000, 1000))
    step = make_step(update_time=1790, skip_if_unchanged=True,
                     local_path=str(tmp_path), include="noise_hamma*.csv")
    assert _drive(step, GRID[:4]) == [1800]
    os.utime(f, (2000, 2000))                     # appended to
    assert _drive(step, [3600]) == [3600]


def test_skip_ignores_files_outside_the_include_pattern(tmp_path):
    f = tmp_path / "unrelated.txt"
    f.write_text("x\n")
    step = make_step(update_time=1790, skip_if_unchanged=True,
                     local_path=str(tmp_path), include="noise_hamma*.csv")
    assert _drive(step, GRID) == []


def test_failed_push_is_not_recorded_as_pushed(tmp_path):
    f = tmp_path / "noise_hamma02_2026-10-01.csv"
    f.write_text("x\n")
    step = make_step(update_time=1790, skip_if_unchanged=True,
                     retry_on_failure=True, local_path=str(tmp_path),
                     include="noise_hamma*.csv")
    assert _drive(step, GRID[:5], returncodes=[255, 0]) == [1800, 2400]


# --- kill_process_group: a real bound on Python 3.7.3 ----------------------
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX only")
def test_kill_process_group_bounds_a_child_holding_the_pipe(tmp_path):
    """rsync's ssh child keeps the output pipe open. Killing only rsync left
    Python's communicate() waiting for ssh; killing the group does not."""
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n")
    step = make_step(timeout=1, kill_process_group=True)
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        step._run_command([sys.executable, "-c", script])
    assert time.monotonic() - start < 5
    child = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_kill_process_group_returns_output_and_code(tmp_path):
    step = make_step(timeout=10, kill_process_group=True)
    out = step._run_command([sys.executable, "-c", "print('hi'); raise SystemExit(3)"])
    assert out.returncode == 3
    assert out.stdout.strip() == "hi"


# --- config: the deployed noise step uses all of it ------------------------
def test_noise_step_enables_the_hardening(main_config):
    step = main_config["steps"]["noise_diag_rsync"]
    assert step["retry_on_failure"] is True
    assert step["kill_process_group"] is True
    assert step["skip_if_unchanged"] is True
    assert 0 < step["jitter_s"] < (main_config["pipelines"]["rsync_noise_diag"]["period_s"]
                                   - step["timeout"])


def test_plot_step_keeps_all_defaults(main_config):
    step = main_config["steps"]["hamma_plot_rsync"]
    for key in ("retry_on_failure", "kill_process_group", "skip_if_unchanged",
                "jitter_s"):
        assert key not in step


def test_include_matches_the_filename_noise_diag_writes(main_config):
    tomli = pytest.importorskip("tomli")
    with open(REPO_ROOT / "metadata.toml", "rb") as f:
        name = tomli.load(f)["name"]
    nd = main_config["steps"]["noise_diag"]
    filename = nd["filename_template"].format(
        system_name=name, system_prefix=name, unit_number=3,
        utc_date=datetime.date(2026, 1, 1))
    assert fnmatch.fnmatch(filename, main_config["steps"]["noise_diag_rsync"]["include"])


def test_push_logs_in_as_pi(main_config):
    assert main_config["steps"]["noise_diag_rsync"].get("username", "pi") == "pi"


def test_local_path_has_no_placeholders(main_config):
    """RsyncServer renders local_path once at init; NoiseDiag renders per write."""
    assert "{" not in main_config["steps"]["noise_diag_rsync"]["local_path"]


@pytest.mark.parametrize("mode", ["test", "nosensor", "nosensor_nochargecontroller",
                                  "nochargecontroller", "realtime", "sindri02x"])
def test_mode_overrides_keep_push_and_writer_aligned(main_config, mode_config, mode):
    steps = mode_config.get(mode, {}).get("main", {}).get("steps", {})
    nd = {**main_config["steps"]["noise_diag"], **steps.get("noise_diag", {})}
    rs = {**main_config["steps"]["noise_diag_rsync"],
          **steps.get("noise_diag_rsync", {})}
    assert rs["local_path"] == nd["output_path"]


def _ssh_first_wins(opts):
    """ssh keeps the FIRST value per -o option; accept -oX=Y and 'X Y' forms."""
    toks, out, i = opts.split(), {}, 0
    while i < len(toks):
        tok = toks[i]
        if tok == "-o":
            i += 1
            tok = toks[i]
        elif tok.startswith("-o"):
            tok = tok[2:]
        else:
            i += 1
            continue
        key, _, value = tok.partition("=")
        if not value:
            i += 1
            value = toks[i]
        out.setdefault(key.lower(), value)
        i += 1
    return out


def test_timeouts_nest_with_ssh_semantics(main_config):
    step = main_config["steps"]["noise_diag_rsync"]
    o = _ssh_first_wins(step["ssh_options"])
    connect = int(o["connecttimeout"]) * int(o.get("connectionattempts", 1))
    alive = int(o["serveraliveinterval"]) * int(o["serveralivecountmax"])
    assert o["batchmode"] == "yes"
    assert max(connect, alive) < step["io_timeout"] < step["timeout"]


def test_push_is_due_strictly_after_update_time():
    """Pin the strict '>' (the plot push's 120 s on a 60 s grid depends on it)."""
    step = make_step(update_time=120)
    assert _drive(step, [0, 60, 120, 180]) == [180]
