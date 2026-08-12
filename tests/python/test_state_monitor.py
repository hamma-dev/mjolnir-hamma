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
        scrub_cooldown_s=300,
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

    def test_at_purge_threshold_is_healthy(self):
        """free == purge_space (200) is healthy: no spawn, no alert."""
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(200.0)})
        spawn.assert_not_called()
        assert msg is None

    def test_at_alert_threshold_drains_no_alert(self):
        """free == alert_space (75) is in the drain band: spawn, no alert."""
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub") as spawn:
            msg = m.check_sensor_drive({"bytes_remaining": _dv(75.0)})
        spawn.assert_called_once()
        assert msg is None

    def test_alert_rearms_in_drain_band(self):
        """Re-arm on recovery into the drain band, not only full recovery --
        an oscillation below alert_space pages each time (hysteresis)."""
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub"):
            assert m.check_sensor_drive({"bytes_remaining": _dv(50.0)}) is not None
            # recover into [alert_space, purge_space) -> re-arm
            assert m.check_sensor_drive({"bytes_remaining": _dv(150.0)}) is None
            assert m.check_sensor_drive({"bytes_remaining": _dv(50.0)}) is not None

    def test_noop_spawn_does_not_consume_cooldown(self):
        """A no-op spawn (lock held -> _spawn_scrub returns False) must NOT arm
        the cooldown, so the next cycle re-attempts (M3)."""
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub", return_value=False) as spawn:
            m.check_sensor_drive({"bytes_remaining": _dv(150.0)})
            m.check_sensor_drive({"bytes_remaining": _dv(148.0)})
        assert spawn.call_count == 2
        assert m._last_scrub_spawn is None

    def test_successful_spawn_arms_cooldown(self):
        m = _make_monitor()
        self._prev(m)
        with patch.object(m, "_spawn_scrub", return_value=True) as spawn:
            m.check_sensor_drive({"bytes_remaining": _dv(150.0)})
            m.check_sensor_drive({"bytes_remaining": _dv(148.0)})  # gated
        spawn.assert_called_once()

    def test_misconfig_alert_ge_purge_warns(self):
        m = _make_monitor(purge_space=75, alert_space=200)
        assert any("alert_space" in str(c) for c in
                   m.logger.warning.call_args_list)

    def test_accepts_and_ignores_legacy_low_space(self):
        """A stale per-unit `low_space` override must NOT crash construction
        (brokkr's Executable has no **kwargs) -- accept + warn + ignore."""
        m = _make_monitor(low_space=10)  # must not raise
        assert not hasattr(m, "low_space")
        assert any("low_space" in str(c) for c in
                   m.logger.warning.call_args_list)

    def test_scrub_auto_recover_defaults_off(self):
        """The SIGKILL self-heal ships OFF (bench-validation gate)."""
        m = _make_monitor()  # no explicit scrub_auto_recover
        assert m.scrub_auto_recover is False

    def test_hs_staleness_alerts_when_ags_dark(self):
        """Persistent NA bytes_remaining (AGS dark) -> alert once, reset on
        a numeric reading -- so a fill isn't missed silently."""
        m = _make_monitor(hs_stale_cycles=3)
        self._prev(m)
        with patch.object(m, "_spawn_scrub"):
            assert m.check_sensor_drive({"bytes_remaining": _dv("NA")}) is None
            assert m.check_sensor_drive({"bytes_remaining": _dv("NA")}) is None
            msg = m.check_sensor_drive({"bytes_remaining": _dv("NA")})   # 3rd
            assert msg is not None and "NA" in msg
            assert m.check_sensor_drive({"bytes_remaining": _dv("NA")}) is None  # one-shot
            # numeric reading clears the watchdog and re-arms it
            assert m.check_sensor_drive({"bytes_remaining": _dv(220.0)}) is None
            assert m._hs_stale_count == 0 and m._hs_stale_alerted is False


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


# --- check_power_state_divergence (HAM-182) ---------------------------------

class TestCheckPowerStateDivergence:
    """Relay-vs-mode comparison. Alert-only: it must never mutate anything.

    Two things carry the weight. Polarity: relay.py always energises the COIL by
    driving the pad LOW, and whether that means the FRONT END is on depends on the
    unit's `active_high` -- mj42 is wired inverted, so it is covered explicitly.
    And `declared_on`, which must come from whether the science-ingest pipeline is
    actually enabled, NOT from the mode's name.
    """

    AH_TRUE = {"pin": 4, "active_high": True}
    AH_FALSE = {"pin": 4, "active_high": False}

    def _monitor(self, relay, science_enabled, level, func="OUTPUT", mode="default"):
        m = _make_monitor()
        m._next_power_check = None
        m._read_gpio = lambda pin: (level, func)
        unit_config = {"number": 3, "site_description": "Test"}
        if relay is not None:
            unit_config["relay"] = relay
        science = {} if science_enabled is None else {"_enabled": science_enabled}
        mods = {
            "brokkr.config": MagicMock(),
            "brokkr.config.unit": SimpleNamespace(UNIT_CONFIG=unit_config),
            "brokkr.config.main": SimpleNamespace(
                CONFIG={"pipelines": {"science_ingest": science}}),
            "brokkr.config.mode": SimpleNamespace(MODE_CONFIG={"mode": mode}),
        }
        return m, mods

    def _run(self, relay, science_enabled, level, func="OUTPUT", mode="default"):
        m, mods = self._monitor(relay, science_enabled, level, func, mode)
        with patch.dict("sys.modules", mods):
            return m.check_power_state_divergence({})

    # --- agreeing states must be silent ---

    def test_powered_and_ingesting_is_silent(self):
        """mj04/mj08: pad LOW on active_high=true is ON, ingest enabled."""
        assert self._run(self.AH_TRUE, None, 0) is None

    def test_off_and_not_ingesting_is_silent(self):
        """mj03 as left by `sensors.py --off`."""
        assert self._run(self.AH_TRUE, False, 1) is None

    def test_no_relay_section_is_silent(self):
        """mj05/mj06/mj50/mj54 have no [relay] -- nothing to compare."""
        assert self._run(None, None, 1) is None

    def test_mj42_inverted_powered_and_ingesting_is_silent(self):
        """mj42 is active_high=false: pad HIGH is ON."""
        assert self._run(self.AH_FALSE, None, 1) is None

    # --- the divergence this exists for ---

    def test_cold_loss_powered_but_not_ingesting_alerts(self):
        msg = self._run(self.AH_TRUE, False, 0, func="INPUT", mode="nosensor")
        assert msg is not None
        assert "POWERED" in msg
        assert "ACTION:" in msg

    def test_mj42_inverted_powered_but_not_ingesting_alerts(self):
        msg = self._run(self.AH_FALSE, False, 1, mode="nosensor")
        assert msg is not None
        assert "POWERED" in msg

    def test_not_powered_but_ingesting_alerts(self):
        msg = self._run(self.AH_TRUE, None, 1)
        assert msg is not None
        assert "NOT powered" in msg
        assert "ACTION:" in msg

    # --- declared_on must be structural, not name-based ---

    def test_absent_enabled_key_means_ingesting(self):
        """`_enabled` is simply absent unless a mode overrides it."""
        assert self._run(self.AH_TRUE, None, 0) is None

    def test_decision_ignores_the_mode_name(self):
        """A mode named 'nosensor' whose pipeline is enabled must count as ON.

        This is the regression guard: if anyone reverts to matching on the mode
        string, this fails. The name is display-only.
        """
        assert self._run(self.AH_TRUE, True, 0, mode="nosensor") is None
        msg = self._run(self.AH_TRUE, True, 1, mode="nosensor")
        assert msg is not None
        assert "NOT powered" in msg

    def test_mode_name_appears_in_the_message_only(self):
        msg = self._run(self.AH_TRUE, False, 0, mode="nosensor_nochargecontroller")
        assert "nosensor_nochargecontroller" in msg

    def test_mode_toml_nosensor_presets_still_disable_science_ingest(self):
        """Guard the config side of the contract.

        The check reads `science_ingest._enabled`. If a future edit stopped the
        nosensor presets from setting it, the check would go quietly blind on
        exactly the units it matters for.
        """
        mode_toml = (REPO_ROOT / "config" / "mode.toml").read_text()
        for preset in ("nosensor", "nosensor_nochargecontroller"):
            section = mode_toml.split("[" + preset + "]", 1)[1]
            assert "science_ingest" in section, (
                "[{}] no longer disables science_ingest".format(preset))

    # --- robustness ---

    def test_unreadable_gpio_is_silent_and_retries_sooner(self):
        m, mods = self._monitor(self.AH_TRUE, False, 0)
        m._read_gpio = lambda pin: (None, None)
        with patch.dict("sys.modules", mods):
            assert m.check_power_state_divergence({}) is None
        assert m.logger.warning.called
        # must not have consumed the full interval
        remaining = m._next_power_check - MODULE.time.monotonic()
        assert remaining <= m.power_check_retry_s + 1
        assert remaining < m.power_check_interval_s

    def test_non_dict_relay_is_reported_not_raised(self):
        """`relay = true` instead of a [relay] table passes a falsy-only guard."""
        msg = self._run(True, None, 1)
        assert msg is not None
        assert "not a" in msg and "table" in msg

    def test_string_active_high_is_reported_not_silently_inverted(self):
        """`active_high = "false"` is valid TOML and bool("false") is True."""
        msg = self._run({"pin": 4, "active_high": "false"}, None, 1)
        assert msg is not None
        assert "malformed" in msg

    def test_missing_relay_key_is_reported(self):
        msg = self._run({"pin": 4}, None, 1)
        assert msg is not None
        assert "malformed" in msg

    # --- rate limiting ---

    def test_rate_limited_then_refires_level_triggered(self):
        m, mods = self._monitor(self.AH_TRUE, False, 0)
        with patch.dict("sys.modules", mods):
            assert m.check_power_state_divergence({}) is not None
            assert m.check_power_state_divergence({}) is None
            m._next_power_check -= m.power_check_interval_s + 1
            assert m.check_power_state_divergence({}) is not None

    def test_interval_is_configurable(self):
        m = _make_monitor(power_check_interval_s=7, power_check_retry_s=2)
        assert m.power_check_interval_s == 7
        assert m.power_check_retry_s == 2
