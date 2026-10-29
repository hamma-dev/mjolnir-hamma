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
    assert step["update_time"] == 1800
    assert step["partial_dir"] == ".rsync-partial"
    assert step["timeout"] > 10


def test_noise_diag_rsync_has_its_own_pipeline(main_config):
    """A slow noise push must not share a pipeline with the plot push."""
    pipelines = main_config["pipelines"]
    noise = pipelines["rsync_noise_diag"]
    assert noise["_enabled"] is True
    assert noise["output_steps"] == ["noise_diag_rsync"]
    assert pipelines["rsync_hamma_realtime"]["output_steps"] == ["hamma_plot_rsync"]


def test_plot_rsync_keeps_default_timeout(main_config):
    """The existing plot push is not changed by this feature."""
    step = main_config["steps"]["hamma_plot_rsync"]
    assert "timeout" not in step
    assert "partial_dir" not in step
