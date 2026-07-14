"""Tests for state_monitor scrub-on-low-space integration."""

import importlib.util
import signal
import subprocess
import time
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
                 scrub_hang_timeout_s=900, scrub_status_file="/tmp/nonexistent",
                 scrub_auto_recover=True, scrub_log=""):
    """Create a StateMonitor instance with test defaults."""
    mon = StateMonitor.__new__(StateMonitor)
    mon.low_space = low_space
    mon.scrub_command = scrub_command
    mon.logger = MagicMock()
    mon._previous_data = make_input_data(space_previous)
    mon.scrub_hang_timeout_s = scrub_hang_timeout_s
    mon.scrub_status_file = scrub_status_file
    mon.scrub_auto_recover = scrub_auto_recover
    mon.scrub_log = scrub_log
    mon._scrub_first_held = None
    mon._scrub_last_progress = None
    mon._scrub_progress_token = None
    mon._stuck_scrub_alerted = False
    return mon


# --- Tests ---

class TestScrubSpawning:
    """Test that check_sensor_drive spawns scrub on threshold crossing."""

    @pytest.fixture(autouse=True)
    def _lock_free(self):
        """These tests assume no prior scrub is running (lock free)."""
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"):
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
    when a prior scrub holds the lock (the mj05 flock -n no-op), and must not
    spawn into an unreadable lock (disk full)."""

    def test_no_spawn_when_lock_held(self):
        mon = make_monitor(scrub_command="python3 scrub.py", space_previous=150)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))
        mock_popen.assert_not_called()
        mon.logger.warning.assert_called()  # honest: it says it did NOT spawn

    def test_no_spawn_when_lock_unknown(self):
        mon = make_monitor(scrub_command="python3 scrub.py", space_previous=150)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="unknown"), \
             patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))
        mock_popen.assert_not_called()
        mon.logger.warning.assert_called()

    def test_spawns_when_lock_free(self):
        mon = make_monitor(scrub_command="python3 scrub.py", space_previous=150)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"), \
             patch("subprocess.Popen") as mock_popen:
            mon.check_sensor_drive(make_input_data(90))
        mock_popen.assert_called_once()


class TestCheckScrubHealth:
    """Hung-scrub detection is gated on heartbeat *advancement* measured in the
    monitor's own monotonic clock -- immune to sensor clock skew and to a stale
    heartbeat from a previous run; a long legit recovery (advancing heartbeat)
    is never flagged. On a genuine hang: kill + re-spawn + alert once."""

    def _mon(self, **kw):
        kw.setdefault("scrub_command", "python3 scrub.py")
        kw.setdefault("scrub_hang_timeout_s", 900)
        return make_monitor(**kw)

    def _idle_streak(self, mon, token, idle_s=1000):
        """Put mon into an established held-streak idle for idle_s seconds."""
        mon._scrub_first_held = time.monotonic() - idle_s - 1
        mon._scrub_last_progress = time.monotonic() - idle_s
        mon._scrub_progress_token = token

    def test_fresh_streak_gives_grace_ignoring_stale_file(self):
        """First cycle the lock is seen held -> grace, even if a stale status
        file from a PREVIOUS run is present (guards the stale-file false-kill)."""
        mon = self._mon()
        old = {"pid": 999, "timestamp": time.time() - 99999}  # ancient prev run
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=old), \
             patch.object(StateMonitor, "_recover_stuck_scrub") as mock_recover:
            assert mon.check_scrub_health(make_input_data(50)) is None
        mock_recover.assert_not_called()
        assert mon._scrub_first_held is not None

    def test_advancing_heartbeat_never_hangs(self):
        """Idle-looking streak, but the heartbeat token ADVANCES -> progress."""
        mon = self._mon()
        s1 = {"pid": 5, "timestamp": 1, "phase": "recover", "recovered": 2}
        self._idle_streak(mon, StateMonitor._progress_token(s1))
        s2 = dict(s1, recovered=3)  # advanced (more recovered) -> new token
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s2), \
             patch.object(StateMonitor, "_recover_stuck_scrub") as mock_recover:
            assert mon.check_scrub_health(make_input_data(50)) is None
        mock_recover.assert_not_called()

    def test_stale_heartbeat_hangs_recovers_and_respawns(self):
        """Idle streak with NO token advance -> hung: kill + re-spawn + alert."""
        mon = self._mon(scrub_auto_recover=True)
        s = {"pid": 22, "timestamp": 1, "phase": "recover", "recovered": 2}
        self._idle_streak(mon, StateMonitor._progress_token(s))
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s), \
             patch.object(StateMonitor, "_recover_stuck_scrub",
                          return_value=True) as mock_recover, \
             patch.object(StateMonitor, "_spawn_scrub") as mock_spawn:
            msg = mon.check_scrub_health(make_input_data(50))
        mock_recover.assert_called_once()
        mock_spawn.assert_called_once()          # freed lock is re-used
        assert msg is not None and "hung" in msg.lower()

    def test_clock_skew_immune_future_timestamp_still_hangs(self):
        """A heartbeat timestamp in the FUTURE (NTP skew) must not disable
        detection: detection is by token-change, not absolute time."""
        mon = self._mon(scrub_auto_recover=False)
        s = {"pid": 7, "timestamp": time.time() + 999999}  # far future
        self._idle_streak(mon, StateMonitor._progress_token(s))
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s):
            assert mon.check_scrub_health(make_input_data(50)) is not None

    def test_no_kill_when_auto_recover_disabled(self):
        mon = self._mon(scrub_auto_recover=False)
        s = {"pid": 3, "timestamp": 1}
        self._idle_streak(mon, StateMonitor._progress_token(s))
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s), \
             patch.object(StateMonitor, "_recover_stuck_scrub") as mock_recover:
            msg = mon.check_scrub_health(make_input_data(50))
        mock_recover.assert_not_called()
        assert msg is not None and "manual" in msg.lower()

    def test_no_heartbeat_uses_held_duration_fallback(self):
        """No status file at all: not hung until the lock has been held (in the
        monitor's monotonic clock) longer than the timeout."""
        mon = self._mon()
        self._idle_streak(mon, None)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status",
                          return_value=None), \
             patch.object(StateMonitor, "_recover_stuck_scrub",
                          return_value=False):
            assert mon.check_scrub_health(make_input_data(50)) is not None

    def test_boundary_not_hung_at_exact_timeout(self):
        """idle == timeout is NOT hung (<= is grace); just over IS. Pin
        monotonic so the boundary is exact (catches <= vs < mutations)."""
        mon = self._mon(scrub_hang_timeout_s=900)
        s = {"pid": 9, "timestamp": 1}
        mon._scrub_first_held = 9000                       # established streak
        mon._scrub_progress_token = StateMonitor._progress_token(s)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s), \
             patch.object(StateMonitor, "_recover_stuck_scrub",
                          return_value=False), \
             patch("time.monotonic", return_value=10000):
            mon._scrub_last_progress = 10000 - 900  # idle == 900 exactly
            assert mon.check_scrub_health(make_input_data(50)) is None
            mon._scrub_last_progress = 10000 - 901  # idle == 901
            assert mon.check_scrub_health(make_input_data(50)) is not None

    def test_alert_is_one_shot_and_re_arms_on_free(self):
        mon = self._mon(scrub_auto_recover=False)
        s = {"pid": 4, "timestamp": 1}
        self._idle_streak(mon, StateMonitor._progress_token(s))
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=s):
            assert mon.check_scrub_health(make_input_data(50)) is not None  # alert
            assert mon.check_scrub_health(make_input_data(50)) is None      # quiet
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"):
            assert mon.check_scrub_health(make_input_data(50)) is None
        assert mon._scrub_first_held is None
        assert mon._stuck_scrub_alerted is False

    def test_unknown_lock_state_starts_streak_not_reset(self):
        """'unknown' (disk full) must not reset -- it counts toward a hang."""
        mon = self._mon()
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="unknown"), \
             patch.object(StateMonitor, "_read_scrub_status", return_value=None):
            mon.check_scrub_health(make_input_data(50))
            assert mon._scrub_first_held is not None  # streak started, not reset

    def test_no_op_when_no_scrub_command(self):
        mon = self._mon()
        mon.scrub_command = ""
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="held"):
            assert mon.check_scrub_health(make_input_data(50)) is None


class TestScrubLockState:
    """The real flock(2) tri-state probe interoperates with `flock -n`."""

    def test_free_when_unlocked(self, tmp_path):
        mon = make_monitor()
        lock = str(tmp_path / "scrub.lock")
        with patch.object(MODULE, "SCRUB_LOCK_FILE", lock):
            assert mon._scrub_lock_state() == "free"

    def test_held_when_locked_by_another_fd(self, tmp_path):
        import fcntl
        import os
        mon = make_monitor()
        lock = str(tmp_path / "scrub.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch.object(MODULE, "SCRUB_LOCK_FILE", lock):
                assert mon._scrub_lock_state() == "held"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_unknown_when_lockfile_unopenable(self):
        """os.open failure (e.g. ENOSPC on a full disk) -> 'unknown', not
        'free' -- the detector must not go blind during a disk-full event."""
        mon = make_monitor()
        with patch("os.open", side_effect=OSError("ENOSPC")):
            assert mon._scrub_lock_state() == "unknown"


class TestRecoverStuckScrub:
    """Self-healing: kill a genuinely-hung scrub, guarded against PID reuse."""

    def test_kills_the_process_GROUP_not_the_pid(self):
        # pid and pgid differ, so a mutation killpg(pid) instead of
        # killpg(getpgid(pid)) is caught.
        mon = make_monitor()
        with patch.object(StateMonitor, "_pid_is_scrub", return_value=True), \
             patch("os.getpgid", return_value=777) as mock_getpgid, \
             patch("os.killpg") as mock_killpg:
            assert mon._recover_stuck_scrub({"pid": 555}) is True
        mock_getpgid.assert_called_once_with(555)
        mock_killpg.assert_called_once_with(777, signal.SIGKILL)

    def test_pid_reuse_guard_prevents_kill(self):
        # getpgid is valid here, so the ONLY thing preventing a kill is the
        # _pid_is_scrub guard -- dropping the guard makes this test fail.
        mon = make_monitor()
        with patch.object(StateMonitor, "_pid_is_scrub",
                          return_value=False) as mock_guard, \
             patch("os.getpgid", return_value=777), \
             patch("os.killpg") as mock_killpg:
            assert mon._recover_stuck_scrub({"pid": 999}) is False
        mock_guard.assert_called_once_with(999)
        mock_killpg.assert_not_called()

    def test_no_pid_cannot_recover(self):
        mon = make_monitor()
        with patch("os.killpg") as mock_killpg:
            assert mon._recover_stuck_scrub(None) is False
            assert mon._recover_stuck_scrub({}) is False
        mock_killpg.assert_not_called()


class TestScrubLogRedirect:
    """The scrub's stdout/stderr go to a durable log, not DEVNULL (the mj05
    incident ran blind because output was DEVNULL'd)."""

    def test_output_redirected_to_scrub_log(self, tmp_path):
        log = str(tmp_path / "sub" / "scrub.log")
        mon = make_monitor(scrub_command="python3 scrub.py", scrub_log=log)
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"), \
             patch("subprocess.Popen") as mock_popen:
            mon._spawn_scrub()
        kwargs = mock_popen.call_args[1]
        assert kwargs["stdout"] is not subprocess.DEVNULL   # a real file
        assert kwargs["stderr"] == subprocess.STDOUT        # merged into it

    def test_devnull_when_no_scrub_log(self):
        mon = make_monitor(scrub_command="python3 scrub.py", scrub_log="")
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"), \
             patch("subprocess.Popen") as mock_popen:
            mon._spawn_scrub()
        kwargs = mock_popen.call_args[1]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL

    def test_falls_back_to_devnull_when_log_unwritable(self):
        mon = make_monitor(scrub_command="python3 scrub.py",
                           scrub_log="/proc/nope/scrub.log")
        with patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"), \
             patch("subprocess.Popen") as mock_popen:
            mon._spawn_scrub()
        kwargs = mock_popen.call_args[1]
        assert kwargs["stdout"] == subprocess.DEVNULL

    def test_rotates_when_oversized(self, tmp_path):
        log = tmp_path / "scrub.log"
        log.write_bytes(b"x" * 10)
        mon = make_monitor(scrub_command="python3 scrub.py", scrub_log=str(log))
        with patch.object(MODULE, "SCRUB_LOG_MAX_BYTES", 5), \
             patch.object(StateMonitor, "_scrub_lock_state",
                          return_value="free"), \
             patch("subprocess.Popen"):
            mon._spawn_scrub()
        assert (tmp_path / "scrub.log.1").exists()  # old log rotated aside
