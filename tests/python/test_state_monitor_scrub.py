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
                 stuck_scrub_cycles=10):
    """Create a StateMonitor instance with test defaults."""
    mon = StateMonitor.__new__(StateMonitor)
    mon.low_space = low_space
    mon.scrub_command = scrub_command
    mon.logger = MagicMock()
    mon._previous_data = make_input_data(space_previous)
    mon.stuck_scrub_cycles = stuck_scrub_cycles
    mon._scrub_lock_held_cycles = 0
    mon._stuck_scrub_alerted = False
    return mon


# --- Tests ---

class TestScrubSpawning:
    """Test that check_sensor_drive spawns scrub on threshold crossing."""

    @pytest.fixture(autouse=True)
    def _lock_free(self):
        """These tests assume no prior scrub is running (lock free)."""
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=False):
            yield

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

    def test_no_scrub_when_already_below(self):
        """No scrub if space was already below threshold (not a crossing)."""
        mon = make_monitor(
            scrub_command="python3 /path/to/scrub.py --recover --purge --since auto",
            space_previous=80,
        )

        with patch("subprocess.Popen") as mock_popen:
            msg = mon.check_sensor_drive(make_input_data(70))

        assert msg is None
        mock_popen.assert_not_called()

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


class TestScrubLockHonesty:
    """_spawn_scrub must not spawn -- and must not silently claim success --
    when a prior scrub still holds the lock (the mj05 flock -n no-op)."""

    def test_no_spawn_when_lock_held(self):
        mon = make_monitor(scrub_command="python3 scrub.py", space_previous=150)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))
        mock_popen.assert_not_called()
        mon.logger.warning.assert_called()  # honest: it says it did NOT spawn

    def test_spawns_when_lock_free(self):
        mon = make_monitor(scrub_command="python3 scrub.py", space_previous=150)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=False), \
             patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))
        mock_popen.assert_called_once()


class TestCheckScrubHealth:
    """Per-cycle stuck-lock detection: the lock held across many cycles means
    a prior scrub is hung (the failure mode a timer cannot fix)."""

    def _mon(self, cycles=3):
        return make_monitor(scrub_command="python3 scrub.py",
                            stuck_scrub_cycles=cycles)

    def test_alerts_after_threshold_consecutive_held_cycles(self):
        mon = self._mon(cycles=3)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            assert mon.check_scrub_health(make_input_data(50)) is None   # 1
            assert mon.check_scrub_health(make_input_data(50)) is None   # 2
            msg = mon.check_scrub_health(make_input_data(50))            # 3
        assert msg is not None and "hung" in msg.lower()

    def test_alert_is_one_shot(self):
        mon = self._mon(cycles=1)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            assert mon.check_scrub_health(make_input_data(50)) is not None
            assert mon.check_scrub_health(make_input_data(50)) is None

    def test_resets_when_lock_frees(self):
        mon = self._mon(cycles=2)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            mon.check_scrub_health(make_input_data(50))  # 1 held
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=False):
            assert mon.check_scrub_health(make_input_data(50)) is None
        assert mon._scrub_lock_held_cycles == 0
        assert mon._stuck_scrub_alerted is False

    def test_re_arms_after_recovery(self):
        mon = self._mon(cycles=1)
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            assert mon.check_scrub_health(make_input_data(50)) is not None
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=False):
            mon.check_scrub_health(make_input_data(50))
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            assert mon.check_scrub_health(make_input_data(50)) is not None

    def test_no_op_when_no_scrub_command(self):
        mon = self._mon(cycles=1)
        mon.scrub_command = ""
        with patch.object(StateMonitor, "_scrub_lock_is_held",
                          return_value=True):
            assert mon.check_scrub_health(make_input_data(50)) is None


class TestScrubLockProbe:
    """The real flock(2) probe interoperates with `flock -n`."""

    def test_free_when_unlocked(self, tmp_path):
        mon = make_monitor()
        lock = str(tmp_path / "scrub.lock")
        with patch.object(MODULE, "SCRUB_LOCK_FILE", lock):
            assert mon._scrub_lock_is_held() is False

    def test_held_when_locked_by_another_fd(self, tmp_path):
        import fcntl
        import os
        mon = make_monitor()
        lock = str(tmp_path / "scrub.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch.object(MODULE, "SCRUB_LOCK_FILE", lock):
                assert mon._scrub_lock_is_held() is True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
