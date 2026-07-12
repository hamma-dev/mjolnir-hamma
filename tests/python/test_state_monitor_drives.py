"""Tests for the v2 disk-safety monitors:
- check_recovery_drives (Layer 1: /media/pi/DATA?? free space)
- check_hs_staleness   (Layer 2: H&S bytes_remaining staleness)
- futile-scrub-loop alert in check_sensor_drive (Layer 3)
"""

import importlib.util
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"


class MockOutputStep:
    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_state_monitor_module():
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
        spec = importlib.util.spec_from_file_location("state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


MODULE = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor

DU = namedtuple("DU", ["total", "used", "free"])
GB = 2 ** 30


def du(free_gb):
    return DU(2000 * GB, 0, int(free_gb * GB))


class FakeDataValue:
    def __init__(self, value):
        self.value = value


def make_input_data(bytes_remaining):
    return {"bytes_remaining": FakeDataValue(bytes_remaining)}


def make_drive_monitor(low_space=100, scrub_command="", recovery_low_gb=25,
                       hs_stale_s=900, futile_scrub_alert_after=3,
                       scrub_cooldown_s=1800, space_previous=150):
    """Build a StateMonitor with ALL scrub-era + v2 attrs set (bypasses __init__)."""
    mon = StateMonitor.__new__(StateMonitor)
    mon.logger = MagicMock()
    mon._previous_data = make_input_data(space_previous)
    # scrub-era (PR#78)
    mon.low_space = low_space
    mon.scrub_command = scrub_command
    mon.scrub_cooldown_s = scrub_cooldown_s
    mon.scrub_log = ""
    mon._last_scrub_time = None
    mon._low_space_active = False
    # v2 config
    mon.recovery_low_gb = recovery_low_gb
    mon.hs_stale_s = hs_stale_s
    mon.futile_scrub_alert_after = futile_scrub_alert_after
    # v2 state
    mon._recovery_low_active = False
    mon._hs_stale_active = False
    mon._last_numeric_hs_time = None
    mon._scrub_respawn_count = 0
    mon._futile_scrub_alerted = False
    return mon


# --- Layer 1: check_recovery_drives ---

class TestRecoveryDrives:
    def test_alert_when_all_drives_low(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with patch("glob.glob", return_value=["/media/pi/DATA55", "/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=[du(2), du(10)]):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is not None
        assert "GB" in msg

    def test_no_alert_when_one_drive_has_room(self):
        """DATA55 at 0 (normal rotation), DATA56 has room -> no alert."""
        mon = make_drive_monitor(recovery_low_gb=25)
        with patch("glob.glob", return_value=["/media/pi/DATA55", "/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=[du(0), du(800)]):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None

    def test_empty_glob_returns_none(self):
        mon = make_drive_monitor()
        with patch("glob.glob", return_value=[]), patch("shutil.disk_usage") as usage:
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None
        usage.assert_not_called()

    def test_all_paths_oserror_returns_none(self):
        """Non-empty glob but every path raises -> None, NOT ValueError from max([])."""
        mon = make_drive_monitor()
        with patch("glob.glob", return_value=["/media/pi/DATA55", "/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=OSError("unmounted")):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None
        mon.logger.error.assert_not_called()

    def test_per_path_oserror_skipped_healthy_evaluated(self):
        """One path errors, the other is low -> still alerts on the good one."""
        mon = make_drive_monitor(recovery_low_gb=25)

        def side(p):
            if "DATA55" in p:
                raise OSError("unmounted")
            return du(3)
        with patch("glob.glob", return_value=["/media/pi/DATA55", "/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=side):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is not None

    def test_debounce_and_rearm(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with patch("glob.glob", return_value=["/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(2)]):
                m1 = mon.check_recovery_drives(make_input_data(100))
            with patch("shutil.disk_usage", side_effect=[du(1)]):
                m2 = mon.check_recovery_drives(make_input_data(100))  # still low
            with patch("shutil.disk_usage", side_effect=[du(500)]):
                m3 = mon.check_recovery_drives(make_input_data(100))  # recovered
            with patch("shutil.disk_usage", side_effect=[du(1)]):
                m4 = mon.check_recovery_drives(make_input_data(100))  # low again
        assert m1 is not None
        assert m2 is None       # debounced
        assert m3 is None       # recovery is silent
        assert m4 is not None   # re-armed -> re-alert

    def test_boundary_equal_threshold_no_alert(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with patch("glob.glob", return_value=["/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=[du(25)]):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None  # free == threshold is NOT below

    def test_alert_only_never_spawns(self):
        mon = make_drive_monitor(recovery_low_gb=25,
                                 scrub_command="python3 scrub.py")
        with patch("glob.glob", return_value=["/media/pi/DATA56"]), \
                patch("shutil.disk_usage", side_effect=[du(1)]), \
                patch("subprocess.Popen") as popen:
            mon.check_recovery_drives(make_input_data(100))
        popen.assert_not_called()


# --- Layer 2: check_hs_staleness ---

class TestHsStaleness:
    def test_na_past_threshold_alerts(self):
        mon = make_drive_monitor(hs_stale_s=900)
        with patch("time.monotonic", return_value=1000.0):
            mon.check_hs_staleness(make_input_data(150))  # numeric -> sets clock
        with patch("time.monotonic", return_value=1000.0 + 901):
            msg = mon.check_hs_staleness(make_input_data("NA"))
        assert msg is not None

    def test_numeric_resets_clock(self):
        mon = make_drive_monitor(hs_stale_s=900)
        with patch("time.monotonic", return_value=1000.0):
            mon.check_hs_staleness(make_input_data(150))
        with patch("time.monotonic", return_value=1000.0 + 800):
            mon.check_hs_staleness(make_input_data(120))  # numeric, resets
        with patch("time.monotonic", return_value=1000.0 + 900 + 1):  # <900 since reset
            msg = mon.check_hs_staleness(make_input_data("NA"))
        assert msg is None

    def test_lazy_init_from_first_call(self):
        """AGS dark from the very first sample -> alerts hs_stale_s after first call."""
        mon = make_drive_monitor(hs_stale_s=900)
        with patch("time.monotonic", return_value=500.0):
            m0 = mon.check_hs_staleness(make_input_data("NA"))  # lazy-init to 500
        with patch("time.monotonic", return_value=500.0 + 901):
            m1 = mon.check_hs_staleness(make_input_data("NA"))
        assert m0 is None
        assert m1 is not None

    def test_none_value_is_not_numeric(self):
        """A None value must be treated as non-numeric (not reset the clock)."""
        mon = make_drive_monitor(hs_stale_s=900)
        with patch("time.monotonic", return_value=1000.0):
            mon.check_hs_staleness(make_input_data(150))
        with patch("time.monotonic", return_value=1000.0 + 901):
            msg = mon.check_hs_staleness(make_input_data(None))
        assert msg is not None

    def test_fires_with_empty_scrub_command(self):
        mon = make_drive_monitor(hs_stale_s=900, scrub_command="")
        with patch("time.monotonic", return_value=1000.0):
            mon.check_hs_staleness(make_input_data(150))
        with patch("time.monotonic", return_value=1000.0 + 901):
            msg = mon.check_hs_staleness(make_input_data("NA"))
        assert msg is not None

    def test_debounce_one_alert_while_persisting(self):
        mon = make_drive_monitor(hs_stale_s=900)
        with patch("time.monotonic", return_value=1000.0):
            mon.check_hs_staleness(make_input_data(150))
        with patch("time.monotonic", return_value=1000.0 + 901):
            m1 = mon.check_hs_staleness(make_input_data("NA"))
        with patch("time.monotonic", return_value=1000.0 + 1000):
            m2 = mon.check_hs_staleness(make_input_data("NA"))
        assert m1 is not None
        assert m2 is None


# --- Layer 3: futile-scrub-loop alert in check_sensor_drive ---

class TestFutileScrubLoop:
    def _below(self, mon, val, t):
        with patch("subprocess.Popen"), patch("time.monotonic", return_value=t):
            return mon.check_sensor_drive(make_input_data(val))

    def test_futile_alert_after_n_respawns(self):
        mon = make_drive_monitor(low_space=100, scrub_command="python3 scrub.py",
                                 futile_scrub_alert_after=3, scrub_cooldown_s=1800)
        msgs = []
        # 3 respawns, each past the cooldown, drive never recovers
        for i in range(3):
            msgs.append(self._below(mon, 70, 1000.0 + i * 2000))
        assert mon._scrub_respawn_count == 3
        assert any("re-fired" in (m or "") or "manual intervention" in (m or "")
                   for m in msgs)

    def test_no_futile_alert_if_recovers(self):
        mon = make_drive_monitor(low_space=100, scrub_command="python3 scrub.py",
                                 futile_scrub_alert_after=3, scrub_cooldown_s=1800)
        self._below(mon, 70, 1000.0)          # respawn 1
        self._below(mon, 70, 3000.0)          # respawn 2
        self._below(mon, 150, 5000.0)         # recovered -> counter resets
        assert mon._scrub_respawn_count == 0
        m = self._below(mon, 70, 7000.0)      # respawn 1 again
        assert "manual intervention" not in (m or "")

    def test_futile_alert_only_once(self):
        mon = make_drive_monitor(low_space=100, scrub_command="python3 scrub.py",
                                 futile_scrub_alert_after=2, scrub_cooldown_s=1800)
        m1 = self._below(mon, 70, 1000.0)     # respawn 1
        m2 = self._below(mon, 70, 3000.0)     # respawn 2 -> futile alert
        m3 = self._below(mon, 70, 5000.0)     # respawn 3 -> already alerted, no repeat
        assert "manual intervention" in (m2 or "")
        assert "manual intervention" not in (m3 or "")


# --- Init wiring ---

class TestInitWiresV2:
    def test_init_sets_new_attrs_and_defaults(self):
        with patch.object(MODULE, "Notifier", MagicMock(), create=True):
            mon = StateMonitor(method="slack")
        assert mon.recovery_low_gb == 25
        assert mon.hs_stale_s == 900
        assert mon.futile_scrub_alert_after == 3
        assert mon._recovery_low_active is False
        assert mon._hs_stale_active is False
        assert mon._last_numeric_hs_time is None
        assert mon._scrub_respawn_count == 0
        assert mon._futile_scrub_alerted is False
