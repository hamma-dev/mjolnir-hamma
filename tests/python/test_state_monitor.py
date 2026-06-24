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
        low_space=100,
        power_delim=15,
        enable_drive_checks=True,
    )
    kwargs.update(overrides)
    monitor = StateMonitor(**kwargs)
    return monitor


class TestCheckSensorDriveBoundary:
    """check_sensor_drive must fire when pre lands exactly on low_space.

    Real-world miss (mj07, 2026-05-28 15:25:03 UTC):
    bytes_remaining went from 100.0 -> 99.96 GB. Strict `>` against
    low_space=100 missed the edge. After that, bytes_remaining only
    decreased, so no future edge could ever fire.
    """

    def test_pre_exactly_at_threshold_fires(self):
        """pre = low_space exactly; now below -> fires."""
        m = _make_monitor(low_space=100)
        m._previous_data = {"bytes_remaining": _dv(100.0)}
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(99.96)})
        spawn.assert_called_once()
        assert msg is not None
        assert "99.9" in msg or "100.0" in msg or "99.96" in msg

    def test_pre_above_now_below_fires(self):
        """Standard down-cross: pre clearly above, now clearly below -> fires."""
        m = _make_monitor(low_space=100)
        m._previous_data = {"bytes_remaining": _dv(150.0)}
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(80.0)})
        spawn.assert_called_once()
        assert msg is not None

    def test_both_below_threshold_no_fire(self):
        """No edge if already below: pre=50, now=45 -> no fire."""
        m = _make_monitor(low_space=100)
        m._previous_data = {"bytes_remaining": _dv(50.0)}
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(45.0)})
        spawn.assert_not_called()
        assert msg is None

    def test_both_above_threshold_no_fire(self):
        """No edge if still above: pre=200, now=150 -> no fire."""
        m = _make_monitor(low_space=100)
        m._previous_data = {"bytes_remaining": _dv(200.0)}
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(150.0)})
        spawn.assert_not_called()
        assert msg is None

    def test_now_exactly_at_threshold_no_fire(self):
        """now exactly on threshold is NOT below; spec is strict `now <`."""
        m = _make_monitor(low_space=100)
        m._previous_data = {"bytes_remaining": _dv(150.0)}
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(100.0)})
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
