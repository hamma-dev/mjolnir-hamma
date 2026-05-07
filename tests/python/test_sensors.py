"""Tests for sensors.py — sensor power control script."""

import datetime
import importlib.util
import os
import pathlib
import textwrap

import pytest
import toml
from unittest.mock import patch, MagicMock, call

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "sensors.py"


def load_sensors():
    """Load sensors module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "sensors", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sensors():
    """Provide the sensors module."""
    return load_sensors()


# --- Config reading ---

class TestLoadConfig:
    """Tests for load_relay_config()."""

    def test_reads_relay_section(self, sensors, tmp_path):
        """Config with valid [relay] section returns pin and active_high."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text(textwrap.dedent("""\
            [relay]
            pin = 17
            active_high = false
        """))
        config = sensors.load_relay_config(str(config_file))
        assert config["pin"] == 17
        assert config["active_high"] is False

    def test_missing_relay_section(self, sensors, tmp_path):
        """Config without [relay] section raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("network_interface = \"wlan0\"\n")
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_missing_pin_key(self, sensors, tmp_path):
        """Config with [relay] but no pin raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\nactive_high = false\n")
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_missing_active_high_key(self, sensors, tmp_path):
        """Config with [relay] but no active_high raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\npin = 17\n")
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_file_not_found(self, sensors, tmp_path):
        """Non-existent config file raises SystemExit."""
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(tmp_path / "nope.toml"))

    def test_invalid_toml(self, sensors, tmp_path):
        """Invalid TOML syntax raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay\npin = 17\n")
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_pin_not_int(self, sensors, tmp_path):
        """Non-integer pin raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text('[relay]\npin = "seventeen"\nactive_high = false\n')
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_active_high_not_bool(self, sensors, tmp_path):
        """Non-boolean active_high raises SystemExit."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text('[relay]\npin = 17\nactive_high = "yes"\n')
        with pytest.raises(SystemExit):
            sensors.load_relay_config(str(config_file))

    def test_merges_local_over_system(self, sensors, tmp_path):
        """Local config overrides system config."""
        system_file = tmp_path / "system" / "unit.toml"
        system_file.parent.mkdir()
        system_file.write_text(textwrap.dedent("""\
            network_interface = "wlan0"
        """))
        local_file = tmp_path / "local" / "unit.toml"
        local_file.parent.mkdir()
        local_file.write_text(textwrap.dedent("""\
            [relay]
            pin = 4
            active_high = true
        """))
        config = sensors.load_relay_config(
            str(local_file), system_path=str(system_file))
        assert config["pin"] == 4
        assert config["active_high"] is True


# --- Relay polarity ---

class TestRelayPolarity:
    """Tests for compute_relay_flag()."""

    @pytest.mark.parametrize("sensor_on, active_high, expected_relay_on", [
        (True, True, True),    # on + active_high=true -> relay on
        (True, False, False),  # on + active_high=false -> relay off
        (False, True, False),  # off + active_high=true -> relay off
        (False, False, True),  # off + active_high=false -> relay on
    ])
    def test_polarity_truth_table(
            self, sensors, sensor_on, active_high, expected_relay_on):
        """Verify relay_on = (sensor_on == active_high)."""
        result = sensors.compute_relay_flag(sensor_on, active_high)
        assert result == expected_relay_on


class TestArchiveTelemetry:
    """Tests for archive_telemetry_csv()."""

    def test_archives_todays_csv(self, sensors, tmp_path):
        """Renames today's telemetry CSV to .bak."""
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        csv_file = tmp_path / "telemetry_hamma_0005_{}.csv".format(today)
        csv_file.write_text("header\ndata\n")
        sensors.archive_telemetry_csv(str(tmp_path))
        assert not csv_file.exists()
        assert (tmp_path / (csv_file.name + ".bak")).exists()

    def test_no_csv_for_today(self, sensors, tmp_path):
        """No matching CSV — returns without error."""
        sensors.archive_telemetry_csv(str(tmp_path))
        # No exception = pass

    def test_bak_collision_uses_timestamp(self, sensors, tmp_path):
        """When .bak exists, uses timestamp suffix."""
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        csv_file = tmp_path / "telemetry_hamma_0005_{}.csv".format(today)
        csv_file.write_text("new data\n")
        bak_file = tmp_path / (csv_file.name + ".bak")
        bak_file.write_text("old data\n")
        sensors.archive_telemetry_csv(str(tmp_path))
        assert not csv_file.exists()
        assert bak_file.exists()  # original .bak untouched
        # A timestamped .bak should exist
        bak_files = list(tmp_path.glob("*.bak.*"))
        assert len(bak_files) == 1

    def test_telemetry_dir_missing(self, sensors, tmp_path):
        """Non-existent telemetry directory — returns without error."""
        sensors.archive_telemetry_csv(str(tmp_path / "nonexistent"))
        # No exception = pass

    def test_only_archives_todays_file(self, sensors, tmp_path):
        """Does not archive CSVs from other days."""
        old_csv = tmp_path / "telemetry_hamma_0005_2020-01-01.csv"
        old_csv.write_text("old\n")
        sensors.archive_telemetry_csv(str(tmp_path))
        assert old_csv.exists()  # untouched


class TestRunCommand:
    """Tests for run_command() helper."""

    def test_success_returns_zero(self, sensors):
        """Successful command returns 0."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            rc = sensors.run_command(["echo", "hello"], "Test")
        assert rc == 0

    def test_failure_returns_nonzero(self, sensors):
        """Failed command returns nonzero and prints FAIL."""
        mock_result = MagicMock(returncode=1, stderr="error msg")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            rc = sensors.run_command(["false"], "Test step")
        assert rc != 0


class TestServiceCommands:
    """Tests for brokkr service management functions."""

    def test_stop_brokkr(self, sensors):
        """stop_brokkr calls systemctl stop."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.stop_brokkr()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "systemctl", "stop", sensors.BROKKR_SERVICE]

    def test_start_brokkr(self, sensors):
        """start_brokkr calls systemctl start."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.start_brokkr()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "systemctl", "start", sensors.BROKKR_SERVICE]

    def test_daemon_reload(self, sensors):
        """daemon_reload calls systemctl daemon-reload."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.daemon_reload()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "systemctl", "daemon-reload"]


class TestRelayToggle:
    """Tests for toggle_relay() subprocess call."""

    def test_relay_on_command(self, sensors):
        """toggle_relay(True, 17) calls relay.py --pin 17 --on."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.toggle_relay(relay_on=True, pin=17)
        cmd = mock_run.call_args[0][0]
        assert cmd == [sensors.RELAY_SCRIPT, "--pin", "17", "--on"]

    def test_relay_off_command(self, sensors):
        """toggle_relay(False, 4) calls relay.py --pin 4 --off."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.toggle_relay(relay_on=False, pin=4)
        cmd = mock_run.call_args[0][0]
        assert cmd == [sensors.RELAY_SCRIPT, "--pin", "4", "--off"]

    def test_pin_forwarded_from_config(self, sensors, tmp_path):
        """Pin value from config is passed through to relay.py."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\npin = 4\nactive_high = true\n")
        config = sensors.load_relay_config(str(config_file))
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            relay_on = sensors.compute_relay_flag(True, config["active_high"])
            sensors.toggle_relay(relay_on=relay_on, pin=config["pin"])
        cmd = mock_run.call_args[0][0]
        assert "--pin" in cmd
        assert "4" in cmd


class TestDropin:
    """Tests for drop-in file management."""

    def test_write_dropin_content(self, sensors):
        """write_dropin writes correct content via sudo tee."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.write_dropin()
        # Check mkdir call
        mkdir_cmd = mock_run.call_args_list[0][0][0]
        assert mkdir_cmd == ["sudo", "mkdir", "-p", sensors.DROPIN_DIR]
        # Check tee call
        tee_call = mock_run.call_args_list[1]
        tee_cmd = tee_call[0][0]
        assert tee_cmd == ["sudo", "tee", sensors.DROPIN_PATH]
        assert tee_call[1]["input"] == sensors.DROPIN_CONTENT

    def test_remove_dropin(self, sensors):
        """remove_dropin calls sudo rm -f."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.remove_dropin()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "rm", "-f", sensors.DROPIN_PATH]


class TestSensorOff:
    """Tests for sensor_off() sequence."""

    def test_off_sequence_order(self, sensors):
        """Off sequence: stop -> relay -> archive -> dropin -> reload -> start."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "archive_telemetry_csv",
                          side_effect=track("archive")), \
             patch.object(sensors, "write_dropin", side_effect=track("dropin")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")):
            sensors.sensor_off(pin=17, active_high=False)

        assert calls == ["stop", "relay", "archive", "dropin", "reload", "start"]

    def test_off_polarity_active_high_false(self, sensors):
        """Off + active_high=false -> relay energized (--on)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0) as mock_relay, \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "write_dropin", return_value=0), \
             patch.object(sensors, "daemon_reload", return_value=0), \
             patch.object(sensors, "start_brokkr", return_value=0):
            sensors.sensor_off(pin=17, active_high=False)
        mock_relay.assert_called_once_with(relay_on=True, pin=17)

    def test_off_stops_on_failure(self, sensors):
        """If stop_brokkr fails, subsequent steps do not run."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", return_value=1), \
             patch.object(sensors, "toggle_relay",
                          side_effect=track("relay")), \
             patch.object(sensors, "write_dropin",
                          side_effect=track("dropin")):
            rc = sensors.sensor_off(pin=17, active_high=False)

        assert rc != 0
        assert "relay" not in calls


class TestSensorOn:
    """Tests for sensor_on() sequence."""

    def test_on_sequence_order(self, sensors):
        """On sequence: stop -> archive -> remove dropin -> reload -> relay -> start."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "archive_telemetry_csv",
                          side_effect=track("archive")), \
             patch.object(sensors, "remove_dropin",
                          side_effect=track("remove")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")):
            sensors.sensor_on(pin=17, active_high=False)

        assert calls == ["stop", "archive", "remove", "reload", "relay", "start"]

    def test_on_polarity_active_high_false(self, sensors):
        """On + active_high=false -> relay de-energized (--off)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "remove_dropin", return_value=0), \
             patch.object(sensors, "daemon_reload", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0) as mock_relay, \
             patch.object(sensors, "start_brokkr", return_value=0):
            sensors.sensor_on(pin=17, active_high=False)
        mock_relay.assert_called_once_with(relay_on=False, pin=17)

    def test_on_stops_on_failure(self, sensors):
        """If stop_brokkr fails, subsequent steps do not run."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", return_value=1), \
             patch.object(sensors, "toggle_relay",
                          side_effect=track("relay")), \
             patch.object(sensors, "remove_dropin",
                          side_effect=track("remove")):
            rc = sensors.sensor_on(pin=17, active_high=False)

        assert rc != 0
        assert "relay" not in calls
