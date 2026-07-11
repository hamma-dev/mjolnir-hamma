"""Tests for state_monitor scrub-on-low-space integration."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --- Module loading with mocked dependencies ---

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"


class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep."""

    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_state_monitor_module():
    """Load the state_monitor plugin with mocked dependencies."""
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep

    mock_pipeline = MagicMock()
    mock_pipeline.base = mock_base

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.pipeline.decode": MagicMock(),
        "brokkr.utils": MagicMock(),
        "brokkr.utils.output": MagicMock(),
        "notifiers": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location(
            "state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module


MODULE = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor


# --- Helpers ---

class FakeDataValue:
    """Minimal stand-in for brokkr DataValue."""
    def __init__(self, value):
        self.value = value


def make_input_data(bytes_remaining):
    """Create minimal input_data dict with bytes_remaining."""
    return {"bytes_remaining": FakeDataValue(bytes_remaining)}


def make_monitor(scrub_command="", low_space=100, space_previous=150,
                 scrub_cooldown_s=1800, last_scrub_time=None,
                 low_space_active=False, scrub_log=""):
    """Create a StateMonitor instance with test defaults."""
    mon = StateMonitor.__new__(StateMonitor)
    mon.low_space = low_space
    mon.scrub_command = scrub_command
    mon.scrub_cooldown_s = scrub_cooldown_s
    mon.scrub_log = scrub_log
    mon._last_scrub_time = last_scrub_time
    mon._low_space_active = low_space_active
    mon.logger = MagicMock()
    mon._previous_data = make_input_data(space_previous)
    return mon


# --- Tests ---

class TestScrubSpawning:
    """Test that check_sensor_drive spawns scrub on threshold crossing."""

    def test_scrub_spawned_on_low_space(self):
        """When space drops below threshold, scrub process is spawned."""
        mon = make_monitor(
            scrub_command="python3 /home/pi/dev/mjolnir-hamma/scripts/hamma_scrub.py --recover --purge --since auto",
            space_previous=150,
        )

        with patch("subprocess.Popen") as mock_popen:
            msg = mon.check_sensor_drive(make_input_data(90))

        assert msg is not None
        mock_popen.assert_called_once()
        popen_cmd = mock_popen.call_args[0][0]
        assert "flock" in popen_cmd[0]

    def test_scrub_spawns_when_already_below_level_triggered(self):
        """NEW CONTRACT (was test_no_scrub_when_already_below): a below-threshold
        sample spawns even without a fresh high->low crossing. The old edge-only
        behavior is the mj05 bug (one-shot, no retry); level-triggering fixes it."""
        mon = make_monitor(
            scrub_command="python3 /path/to/scrub.py --recover --purge --since auto",
            space_previous=80,  # already below last cycle -> old code would NOT fire
        )

        with patch("subprocess.Popen") as mock_popen:
            msg = mon.check_sensor_drive(make_input_data(70))

        assert msg is not None
        mock_popen.assert_called_once()

    def test_no_scrub_when_command_empty(self):
        """No scrub if scrub_command is empty."""
        mon = make_monitor(scrub_command="", space_previous=150)

        with patch("subprocess.Popen") as mock_popen:
            msg = mon.check_sensor_drive(make_input_data(90))

        assert msg is not None  # alert still fires
        mock_popen.assert_not_called()

    def test_scrub_failure_logged_not_raised(self):
        """If Popen fails, error is logged but check_sensor_drive still returns."""
        mon = make_monitor(
            scrub_command="python3 /path/to/scrub.py",
            space_previous=150,
        )

        with patch("subprocess.Popen", side_effect=OSError("flock not found")):
            msg = mon.check_sensor_drive(make_input_data(90))

        assert msg is not None
        mon.logger.error.assert_called()

    def test_flock_uses_lock_file(self):
        """Popen command uses flock with a specific lock file."""
        mon = make_monitor(
            scrub_command="python3 /path/to/scrub.py --recover --purge --since auto",
            space_previous=150,
        )

        with patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))

        popen_cmd = mock_popen.call_args[0][0]
        # Should be: flock -n /tmp/hamma_scrub.lock <scrub_command>
        assert popen_cmd[0] == "flock"
        assert popen_cmd[1] == "-n"
        assert "hamma_scrub.lock" in popen_cmd[2]
        popen_kwargs = mock_popen.call_args[1]
        assert popen_kwargs["start_new_session"] is True
        assert popen_kwargs["stdout"] == subprocess.DEVNULL
        assert popen_kwargs["stderr"] == subprocess.DEVNULL

    def test_no_scrub_when_command_whitespace_only(self):
        """No scrub if scrub_command is whitespace-only."""
        mon = make_monitor(scrub_command="   ", space_previous=150)

        with patch("subprocess.Popen") as mock_popen:
            msg = mon.check_sensor_drive(make_input_data(90))

        assert msg is not None  # alert still fires
        mock_popen.assert_not_called()


class TestScrubConfig:
    """Test scrub_command config wiring."""

    def test_init_accepts_scrub_command(self):
        """StateMonitor accepts scrub_command parameter."""
        mon = make_monitor(scrub_command="python3 /path/to/scrub.py")
        assert mon.scrub_command == "python3 /path/to/scrub.py"

    def test_init_default_scrub_command_empty(self):
        """Default scrub_command is empty string (disabled)."""
        mon = make_monitor(scrub_command="")
        assert mon.scrub_command == ""


SCRUB_CMD = "python3 /path/to/scrub.py --recover --purge --since auto"


class TestLevelTriggerRobustness:
    """New contract: level-triggered, numeric-robust, cooldown-gated retry,
    observable scrub output. Regression guard for the mj05 fill incident."""

    def test_nan_sample_makes_no_decision(self):
        """An 'NA' (-> nan) H&S sample: cannot assess -> no spawn, no alert, no error."""
        mon = make_monitor(scrub_command=SCRUB_CMD)
        with patch("subprocess.Popen") as popen:
            msg = mon.check_sensor_drive(make_input_data("NA"))
        assert msg is None
        popen.assert_not_called()
        mon.logger.error.assert_not_called()

    def test_none_value_makes_no_decision_no_typeerror(self):
        """A None/non-numeric value must not raise TypeError; treated as unassessable."""
        mon = make_monitor(scrub_command=SCRUB_CMD)
        with patch("subprocess.Popen") as popen:
            msg = mon.check_sensor_drive(make_input_data(None))
        assert msg is None
        popen.assert_not_called()
        mon.logger.error.assert_not_called()

    def test_full_disk_zero_still_fires(self):
        """bytes_remaining == 0 (a legitimately full disk) must still spawn."""
        mon = make_monitor(scrub_command=SCRUB_CMD)
        with patch("subprocess.Popen") as popen, patch("time.monotonic", return_value=1.0):
            msg = mon.check_sensor_drive(make_input_data(0))
        assert msg is not None
        popen.assert_called_once()

    def test_cooldown_suppresses_second_spawn_and_alert(self):
        """Two below samples within cooldown -> one spawn, one alert."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_cooldown_s=1800)
        with patch("subprocess.Popen") as popen:
            with patch("time.monotonic", return_value=1000.0):
                msg1 = mon.check_sensor_drive(make_input_data(70))
            with patch("time.monotonic", return_value=1600.0):  # +600s, within cooldown
                msg2 = mon.check_sensor_drive(make_input_data(65))
        popen.assert_called_once()
        assert msg1 is not None
        assert msg2 is None

    def test_cooldown_elapsed_respawns_and_realerts(self):
        """After cooldown elapses while still low -> respawn AND re-alert (escalation)."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_cooldown_s=1800)
        with patch("subprocess.Popen") as popen:
            with patch("time.monotonic", return_value=1000.0):
                mon.check_sensor_drive(make_input_data(70))
            with patch("time.monotonic", return_value=3000.0):  # +2000s, past cooldown
                msg2 = mon.check_sensor_drive(make_input_data(65))
        assert popen.call_count == 2
        assert msg2 is not None

    def test_cooldown_not_stamped_when_launch_fails(self):
        """If the scrub launch fails, cooldown is NOT armed -> next low sample retries."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_cooldown_s=1800)
        with patch("subprocess.Popen", side_effect=OSError("flock missing")), \
                patch("time.monotonic", return_value=1000.0):
            mon.check_sensor_drive(make_input_data(70))
        assert mon._last_scrub_time is None
        mon.logger.error.assert_called()

    def test_realarm_above_then_below_realerts(self):
        """below -> above (re-arm, no alert) -> below again -> alert again."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_cooldown_s=1800)
        with patch("subprocess.Popen"):
            with patch("time.monotonic", return_value=1000.0):
                m1 = mon.check_sensor_drive(make_input_data(70))
                m2 = mon.check_sensor_drive(make_input_data(150))
            with patch("time.monotonic", return_value=5000.0):  # past cooldown
                m3 = mon.check_sensor_drive(make_input_data(70))
        assert m1 is not None
        assert m2 is None
        assert m3 is not None

    def test_no_duplicate_alert_while_staying_low(self):
        """Staying low across cycles within cooldown -> only the first alert."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_cooldown_s=1800)
        with patch("subprocess.Popen"):
            with patch("time.monotonic", return_value=1000.0):
                m1 = mon.check_sensor_drive(make_input_data(70))
            with patch("time.monotonic", return_value=1100.0):
                m2 = mon.check_sensor_drive(make_input_data(69))
        assert m1 is not None
        assert m2 is None

    def test_scrub_output_redirected_to_log_when_configured(self):
        """Observability: when scrub_log is set, scrub output goes to that file
        (append), not /dev/null -- so a futile/failed scrub is diagnosable."""
        mon = make_monitor(scrub_command=SCRUB_CMD, scrub_log="/tmp/hamma_scrub_auto.log")
        fake_fh = MagicMock()
        with patch("subprocess.Popen") as popen, \
                patch("builtins.open", return_value=fake_fh) as mopen, \
                patch("time.monotonic", return_value=1.0):
            mon.check_sensor_drive(make_input_data(70))
        mopen.assert_called_once_with("/tmp/hamma_scrub_auto.log", "ab")
        kwargs = popen.call_args[1]
        assert kwargs["stdout"] is fake_fh
        assert kwargs["stderr"] == subprocess.STDOUT


class TestInitWiresNewState:
    """The three new state attributes must be initialized by the real __init__."""

    def test_init_wires_cooldown_and_flags(self):
        with patch.object(MODULE, "Notifier", MagicMock(), create=True):
            mon = StateMonitor(method="slack")
        assert mon._last_scrub_time is None
        assert mon._low_space_active is False
        assert mon.scrub_cooldown_s == 1800
