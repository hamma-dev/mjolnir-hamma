"""Tests for state_monitor sensor_prefix and send_message."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
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
        "notifiers.notify": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location(
            "state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module


MODULE = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor


# --- Tests ---

class TestSensorPrefix:
    """Test the module-level sensor_prefix() function."""

    def test_compact_format(self):
        """With a site_description, format is 'mj05 (UAH): '."""
        mock_unit_config = {"number": 5, "site_description": "UAH"}
        mock_metadata = {"name": "mj"}

        with patch.dict("sys.modules", {
            "brokkr.config.unit": MagicMock(UNIT_CONFIG=mock_unit_config),
            "brokkr.config.metadata": MagicMock(METADATA=mock_metadata),
        }):
            result = MODULE.sensor_prefix()

        assert result == "mj05 (UAH): "

    def test_compact_format_empty_site(self):
        """With empty site_description, format is 'mj05: '."""
        mock_unit_config = {"number": 5, "site_description": ""}
        mock_metadata = {"name": "mj"}

        with patch.dict("sys.modules", {
            "brokkr.config.unit": MagicMock(UNIT_CONFIG=mock_unit_config),
            "brokkr.config.metadata": MagicMock(METADATA=mock_metadata),
        }):
            result = MODULE.sensor_prefix()

        assert result == "mj05: "


# --- Edge-trigger boundary tests ---

def _dv(value):
    """Wrap a scalar as a DataValue-like object (just exposes .value)."""
    return SimpleNamespace(value=value)


def _make_monitor(**overrides):
    """Build a StateMonitor with mocked dependencies and reasonable defaults.

    Uses method='gchat' so the sender_class lookup succeeds; the actual
    GoogleChatSender is a MagicMock so the key_file is never touched.
    """
    kwargs = dict(
        method="gchat",
        channel="status",
        key_file="/dev/null",
        purge_space=200,
        alert_space=75,
        scrub_cooldown_s=1800,
        power_delim=15,
        enable_drive_checks=True,
    )
    kwargs.update(overrides)
    monitor = StateMonitor(**kwargs)
    return monitor


class TestTwoThresholdDrain:
    """check_sensor_drive is LEVEL-triggered across two thresholds (§3.4):
    drain while free < purge_space; alert (once) when free < alert_space
    despite the draining. The old edge trigger fired once at the crossing and
    never retried -- the mj05 failure mode this replaces.
    """

    def _prev(self, m, v=250.0):
        m._previous_data = {"bytes_remaining": _dv(v)}

    def test_above_purge_threshold_no_spawn_no_alert(self):
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(220.0)})
        spawn.assert_not_called()
        assert msg is None

    def test_below_purge_above_alert_drains_no_alert(self):
        """150 GB: below purge(200) but above alert(75) -> drain, no alert."""
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(150.0)})
        spawn.assert_called_once()   # level-triggered drain
        assert msg is None           # no alert in the purge band

    def test_below_alert_drains_and_alerts_once(self):
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg1 = m.check_sensor_drive({"bytes_remaining": _dv(50.0)})
            msg2 = m.check_sensor_drive({"bytes_remaining": _dv(45.0)})
        assert msg1 is not None and "50.0" in msg1
        assert msg2 is None          # one-shot alert
        spawn.assert_called_once()   # cooldown holds the 2nd spawn

    def test_level_trigger_fires_even_when_already_below(self):
        """Unlike the old edge trigger: pre already below purge still drains."""
        m = _make_monitor()
        self._prev(m, v=150.0)       # previous ALSO below purge
        with patch.object(m, "_spawn_scrub") as spawn:
            m.check_sensor_drive({"bytes_remaining": _dv(140.0)})
        spawn.assert_called_once()

    def test_cooldown_gates_repeat_spawns(self):
        m = _make_monitor(scrub_cooldown_s=1800)
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            m.check_sensor_drive({"bytes_remaining": _dv(150.0)})  # spawns
            m.check_sensor_drive({"bytes_remaining": _dv(148.0)})  # within cooldown
        spawn.assert_called_once()
        # After the cooldown elapses, a fresh spawn is allowed.
        m._last_scrub_spawn = None
        with patch.object(m, "_spawn_scrub") as spawn2:
            m.check_sensor_drive({"bytes_remaining": _dv(146.0)})
        spawn2.assert_called_once()

    def test_alert_rearms_after_recovery_above_purge(self):
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub"):
            assert m.check_sensor_drive({"bytes_remaining": _dv(50.0)}) is not None
            # recover above purge -> re-arm
            assert m.check_sensor_drive({"bytes_remaining": _dv(210.0)}) is None
            assert m.check_sensor_drive({"bytes_remaining": _dv(50.0)}) is not None

    def test_na_reading_no_spawn_no_alert(self):
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv("NA")})
        spawn.assert_not_called()
        assert msg is None


class TestCheckPowerBoundary:
    """check_power must fire when prior power lands exactly on power_delim."""

    def _input(self, load_v, current_a):
        # power = load * current
        return {"adc_vl_f": _dv(load_v), "adc_il_f": _dv(current_a)}

    def test_pre_exactly_at_threshold_fires(self):
        """pre power == power_delim, now below -> fires."""
        m = _make_monitor(power_delim=15)
        # pre: 15 V * 1.0 A = 15 W exactly
        m._previous_data = self._input(15.0, 1.0)
        # now: 14 V * 1.0 A = 14 W
        msg = m.check_power(self._input(14.0, 1.0))
        assert msg is not None
        assert "15.00" in msg
        assert "14.00" in msg

    def test_pre_above_now_below_fires(self):
        """Standard down-cross."""
        m = _make_monitor(power_delim=15)
        m._previous_data = self._input(25.0, 1.0)  # 25 W
        msg = m.check_power(self._input(10.0, 1.0))  # 10 W
        assert msg is not None

    def test_both_below_no_fire(self):
        m = _make_monitor(power_delim=15)
        m._previous_data = self._input(10.0, 1.0)  # 10 W
        msg = m.check_power(self._input(8.0, 1.0))  # 8 W
        assert msg is None

    def test_both_above_no_fire(self):
        m = _make_monitor(power_delim=15)
        m._previous_data = self._input(25.0, 1.0)
        msg = m.check_power(self._input(20.0, 1.0))
        assert msg is None


class TestCheckBatteryVoltageBoundary:
    """check_battery_voltage must fire when pre lands on CRITICAL_VOLTAGE.

    CRITICAL_VOLTAGE = v_lvd + 0.5, taken from input_data each iteration.
    """

    def _input(self, batt, v_lvd):
        return {"adc_vb_f": _dv(batt), "v_lvd": _dv(v_lvd)}

    def test_pre_exactly_at_critical_fires(self):
        """pre == CRITICAL; now <= CRITICAL -> fires."""
        m = _make_monitor()
        # v_lvd=11.0 -> CRITICAL = 11.5; pre and now both 11.5
        # With the fix (>=), pre at threshold counts -> fires.
        m._previous_data = self._input(batt=11.5, v_lvd=11.0)
        msg = m.check_battery_voltage(self._input(batt=11.5, v_lvd=11.0))
        assert msg is not None
        assert "11.500" in msg

    def test_pre_above_now_at_critical_fires(self):
        """pre above CRITICAL, now equals CRITICAL -> fires (now uses <=)."""
        m = _make_monitor()
        m._previous_data = self._input(batt=12.0, v_lvd=11.0)
        msg = m.check_battery_voltage(self._input(batt=11.5, v_lvd=11.0))
        assert msg is not None

    def test_pre_above_now_above_no_fire(self):
        m = _make_monitor()
        m._previous_data = self._input(batt=13.0, v_lvd=11.0)
        msg = m.check_battery_voltage(self._input(batt=12.0, v_lvd=11.0))
        assert msg is None

    def test_both_below_no_fire(self):
        m = _make_monitor()
        m._previous_data = self._input(batt=11.0, v_lvd=11.0)  # below 11.5
        msg = m.check_battery_voltage(self._input(batt=10.8, v_lvd=11.0))
        assert msg is None
def test_send_message_prefixes_and_delegates():
    sm = StateMonitor.__new__(StateMonitor)
    sm.notifier = MagicMock()
    with patch.object(MODULE, "sensor_prefix", return_value="mj02: "):
        sm.send_message("hi")
    sm.notifier.send.assert_called_once_with("mj02: hi")
