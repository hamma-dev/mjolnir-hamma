"""Tests for sensors.py — sensor power control script."""

import datetime
import importlib.util
import os
import pathlib
import textwrap

import pytest
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

    def test_stop_sindri(self, sensors):
        """stop_sindri calls systemctl stop on sindri service."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.stop_sindri()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "systemctl", "stop", sensors.SINDRI_SERVICE]

    def test_start_sindri(self, sensors):
        """start_sindri calls systemctl start on sindri service."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.start_sindri()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "systemctl", "start", sensors.SINDRI_SERVICE]


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
        """Off sequence: stop -> stop_sindri -> relay -> archive -> dropin -> reload -> start -> start_sindri."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "stop_sindri", side_effect=track("stop_sindri")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "archive_telemetry_csv",
                          side_effect=track("archive")), \
             patch.object(sensors, "write_dropin", side_effect=track("dropin")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("start_sindri")):
            sensors.sensor_off(pin=17, active_high=False)

        assert calls == ["stop", "stop_sindri", "relay", "archive",
                         "dropin", "reload", "start", "start_sindri"]

    def test_off_polarity_active_high_false(self, sensors):
        """Off + active_high=false -> relay energized (--on)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "stop_sindri", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0) as mock_relay, \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "write_dropin", return_value=0), \
             patch.object(sensors, "daemon_reload", return_value=0), \
             patch.object(sensors, "start_brokkr", return_value=0), \
             patch.object(sensors, "start_sindri", return_value=0):
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
        """On sequence: stop -> stop_sindri -> archive -> remove dropin -> reload -> relay -> start -> start_sindri."""
        calls = []
        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn

        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "stop_sindri", side_effect=track("stop_sindri")), \
             patch.object(sensors, "archive_telemetry_csv",
                          side_effect=track("archive")), \
             patch.object(sensors, "remove_dropin",
                          side_effect=track("remove")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("start_sindri")):
            sensors.sensor_on(pin=17, active_high=False)

        assert calls == ["stop", "stop_sindri", "archive", "remove",
                         "reload", "relay", "start", "start_sindri"]

    def test_on_polarity_active_high_false(self, sensors):
        """On + active_high=false -> relay de-energized (--off)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "stop_sindri", return_value=0), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "remove_dropin", return_value=0), \
             patch.object(sensors, "daemon_reload", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0) as mock_relay, \
             patch.object(sensors, "start_brokkr", return_value=0), \
             patch.object(sensors, "start_sindri", return_value=0):
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


class TestStatus:
    """Tests for sensor_status()."""

    def test_status_checks_dropin(self, sensors, tmp_path):
        """Status reports drop-in presence."""
        dropin = tmp_path / "mode.conf"
        dropin.write_text(sensors.DROPIN_CONTENT)
        with patch.object(sensors, "DROPIN_PATH", str(dropin)), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Drop-in: yes" in output

    def test_status_no_dropin(self, sensors, tmp_path):
        """Status reports no drop-in."""
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Drop-in: no" in output

    def test_status_shows_relay_config(self, sensors, tmp_path):
        """Status shows pin and active_high from config."""
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 4, "active_high": True})
        assert "pin=4" in output
        assert "active_high=True" in output

    def test_status_shows_brokkr_service(self, sensors, tmp_path):
        """Status shows brokkr service state."""
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Brokkr service: active" in output

    def test_status_shows_mode_nosensor(self, sensors, tmp_path):
        """Status shows nosensor mode when drop-in present."""
        dropin = tmp_path / "mode.conf"
        dropin.write_text(sensors.DROPIN_CONTENT)
        with patch.object(sensors, "DROPIN_PATH", str(dropin)), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Brokkr mode: nosensor" in output

    def test_status_shows_mode_default(self, sensors, tmp_path):
        """Status shows default mode when no drop-in."""
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Brokkr mode: default" in output

    def test_status_shows_sensor_reachable(self, sensors, tmp_path):
        """Status shows sensor reachability."""
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Sensor reachable:" in output

    def test_status_shows_last_telemetry(self, sensors, tmp_path):
        """Status shows last telemetry when CSV exists."""
        csv_file = tmp_path / "telemetry_hamma_0005_2026-01-01.csv"
        csv_file.write_text("data\n")
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "nonexistent")), \
             patch.object(sensors, "TELEMETRY_DIR", str(tmp_path)), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="active")):
            output = sensors.sensor_status(config={"pin": 17, "active_high": False})
        assert "Last telemetry:" in output
        assert "telemetry_hamma_0005_2026-01-01.csv" in output


class TestCLI:
    """Tests for argument parsing."""

    def test_on_flag(self, sensors):
        """--on sets sensor_on=True."""
        args = sensors.parse_args(["--on"])
        assert args.sensor_on is True

    def test_off_flag(self, sensors):
        """--off sets sensor_on=False."""
        args = sensors.parse_args(["--off"])
        assert args.sensor_on is False

    def test_on_off_mutually_exclusive(self, sensors):
        """--on and --off cannot be used together."""
        with pytest.raises(SystemExit):
            sensors.parse_args(["--on", "--off"])

    def test_status_flag(self, sensors):
        """--status sets status=True."""
        args = sensors.parse_args(["--status"])
        assert args.status is True

    def test_dry_run_with_off(self, sensors):
        """--dry-run can be combined with --off."""
        args = sensors.parse_args(["--off", "--dry-run"])
        assert args.sensor_on is False
        assert args.dry_run is True

    def test_no_args_exits(self, sensors):
        """No arguments prints help and exits."""
        with pytest.raises(SystemExit):
            sensors.parse_args([])


class TestDryRun:
    """Tests for dry-run mode."""

    def test_dry_run_off_no_side_effects(self, sensors, tmp_path):
        """Dry-run --off prints commands but does not execute."""
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\npin = 17\nactive_high = false\n")
        with patch.object(sensors, "stop_brokkr") as mock_stop, \
             patch.object(sensors, "toggle_relay") as mock_relay, \
             patch.object(sensors, "sensor_off") as mock_off:
            sensors.run(["--off", "--dry-run"],
                        config_path=str(config_file))
        mock_off.assert_not_called()
        mock_stop.assert_not_called()
        mock_relay.assert_not_called()


# --- Notifications ---

class TestLoadNotifierConfig:
    """Tests for load_notifier_config() reading [steps.state_monitor]."""

    def test_reads_state_monitor_block(self, sensors, tmp_path):
        """Returns method/channel/key_file from main.toml."""
        main_toml = tmp_path / "main.toml"
        main_toml.write_text(textwrap.dedent("""\
            [steps]
                [steps.state_monitor]
                method = "gchat"
                channel = "status"
                key_file = "/home/pi/.googlechat"
        """))
        cfg = sensors.load_notifier_config(str(main_toml))
        assert cfg == {
            "method": "gchat",
            "channel": "status",
            "key_file": "/home/pi/.googlechat",
        }

    def test_missing_file_returns_none(self, sensors, tmp_path):
        """Non-existent main.toml returns None (notifications disabled)."""
        assert sensors.load_notifier_config(str(tmp_path / "nope.toml")) is None

    def test_missing_state_monitor_returns_none(self, sensors, tmp_path):
        """No [steps.state_monitor] block returns None."""
        main_toml = tmp_path / "main.toml"
        main_toml.write_text("[steps]\n[steps.other_step]\nfoo = \"bar\"\n")
        assert sensors.load_notifier_config(str(main_toml)) is None

    def test_invalid_toml_returns_none(self, sensors, tmp_path):
        """Malformed TOML returns None instead of raising."""
        main_toml = tmp_path / "main.toml"
        main_toml.write_text("[steps\nbroken")
        assert sensors.load_notifier_config(str(main_toml)) is None

    def test_partial_block_keeps_none_values(self, sensors, tmp_path):
        """Missing keys come back as None rather than KeyError."""
        main_toml = tmp_path / "main.toml"
        main_toml.write_text(textwrap.dedent("""\
            [steps]
                [steps.state_monitor]
                method = "gchat"
        """))
        cfg = sensors.load_notifier_config(str(main_toml))
        assert cfg["method"] == "gchat"
        assert cfg["channel"] is None
        assert cfg["key_file"] is None


class TestBuildSender:
    """Tests for build_sender() instantiation."""

    def test_none_config_returns_none(self, sensors):
        """No config -> no sender."""
        assert sensors.build_sender(None) is None

    def test_missing_method_returns_none(self, sensors):
        """Missing method -> no sender."""
        assert sensors.build_sender(
            {"method": None, "key_file": "/x", "channel": "y"}) is None

    def test_missing_key_file_returns_none(self, sensors):
        """Missing key_file -> no sender."""
        assert sensors.build_sender(
            {"method": "gchat", "key_file": None, "channel": "y"}) is None

    def test_unknown_method_returns_none(self, sensors):
        """Unknown notifier method -> warn + None (no crash)."""
        cfg = {"method": "carrier_pigeon", "key_file": "/x", "channel": "y"}
        assert sensors.build_sender(cfg) is None

    def test_gchat_instantiation(self, sensors):
        """Resolves gchat -> GoogleChatSender and instantiates with key_file/channel."""
        cfg = {"method": "gchat", "key_file": "/k", "channel": "status"}
        fake_sender = MagicMock()
        fake_cls = MagicMock(return_value=fake_sender)
        # Patch the notifiers.google_chat module's GoogleChatSender symbol.
        with patch.dict("sys.modules", {
                "notifiers": MagicMock(),
                "notifiers.google_chat": MagicMock(GoogleChatSender=fake_cls)}):
            result = sensors.build_sender(cfg)
        assert result is fake_sender
        fake_cls.assert_called_once_with("/k", channel="status")

    def test_slack_instantiation(self, sensors):
        """Resolves slack -> SlackSender."""
        cfg = {"method": "slack", "key_file": "/k", "channel": "status"}
        fake_sender = MagicMock()
        fake_cls = MagicMock(return_value=fake_sender)
        with patch.dict("sys.modules", {
                "notifiers": MagicMock(),
                "notifiers.slack": MagicMock(SlackSender=fake_cls)}):
            result = sensors.build_sender(cfg)
        assert result is fake_sender
        fake_cls.assert_called_once_with("/k", channel="status")

    def test_key_file_not_found_returns_none(self, sensors):
        """Missing key file at instantiation -> warn + None."""
        cfg = {"method": "gchat", "key_file": "/k", "channel": "status"}
        fake_cls = MagicMock(side_effect=FileNotFoundError())
        with patch.dict("sys.modules", {
                "notifiers": MagicMock(),
                "notifiers.google_chat": MagicMock(GoogleChatSender=fake_cls)}):
            assert sensors.build_sender(cfg) is None

    def test_unexpected_exception_returns_none(self, sensors):
        """Any other exception at instantiation -> warn + None."""
        cfg = {"method": "gchat", "key_file": "/k", "channel": "status"}
        fake_cls = MagicMock(side_effect=RuntimeError("boom"))
        with patch.dict("sys.modules", {
                "notifiers": MagicMock(),
                "notifiers.google_chat": MagicMock(GoogleChatSender=fake_cls)}):
            assert sensors.build_sender(cfg) is None


class TestGetUnitIdentifier:
    """Tests for get_unit_identifier() name + site lookup."""

    def test_reads_number_and_site(self, sensors, tmp_path):
        """Number formatted as 'MjolnirNN', site_description passed through."""
        unit = tmp_path / "unit.toml"
        unit.write_text(textwrap.dedent("""\
            number = 3
            site_description = "SWI Berm"
            [relay]
            pin = 4
            active_high = true
        """))
        name, site = sensors.get_unit_identifier(str(unit))
        assert name == "Mjolnir03"
        assert site == "SWI Berm"

    def test_no_site_returns_none(self, sensors, tmp_path):
        """site_description absent -> None."""
        unit = tmp_path / "unit.toml"
        unit.write_text("number = 7\n[relay]\npin = 4\nactive_high = true\n")
        name, site = sensors.get_unit_identifier(str(unit))
        assert name == "Mjolnir07"
        assert site is None

    def test_empty_site_returns_none(self, sensors, tmp_path):
        """Empty site_description -> None (not the empty string)."""
        unit = tmp_path / "unit.toml"
        unit.write_text(
            'number = 2\nsite_description = ""\n'
            '[relay]\npin = 4\nactive_high = true\n')
        _, site = sensors.get_unit_identifier(str(unit))
        assert site is None

    def test_missing_file_falls_back_to_hostname(self, sensors, tmp_path):
        """Missing unit.toml -> hostname, None."""
        with patch("socket.gethostname", return_value="mjolnir99"):
            name, site = sensors.get_unit_identifier(str(tmp_path / "nope.toml"))
        assert name == "mjolnir99"
        assert site is None

    def test_missing_number_falls_back_to_hostname(self, sensors, tmp_path):
        """unit.toml present but no 'number' -> hostname."""
        unit = tmp_path / "unit.toml"
        unit.write_text("site_description = \"Lab\"\n")
        with patch("socket.gethostname", return_value="mjolnir99"):
            name, site = sensors.get_unit_identifier(str(unit))
        assert name == "mjolnir99"
        # Site is NOT returned when we fall back — the (name, site) pair
        # must be consistent (both from unit.toml, or both from hostname).
        assert site is None


class TestBuildMessage:
    """Tests for build_message() text format."""

    def test_success_with_site(self, sensors):
        msg = sensors.build_message(
            "Mjolnir02", "SWI Berm", "on", success=True)
        assert msg == "Mjolnir02 (SWI Berm): sensor turned ON"

    def test_success_without_site(self, sensors):
        msg = sensors.build_message(
            "Mjolnir02", None, "off", success=True)
        assert msg == "Mjolnir02: sensor turned OFF"

    def test_failure_with_rc(self, sensors):
        msg = sensors.build_message(
            "Mjolnir02", None, "off", success=False, rc=1)
        assert msg == "Mjolnir02: sensor turn-OFF FAILED (rc=1)"

    def test_failure_without_rc(self, sensors):
        msg = sensors.build_message(
            "Mjolnir02", None, "on", success=False)
        assert msg == "Mjolnir02: sensor turn-ON FAILED (rc=?)"

    def test_action_case_normalized(self, sensors):
        """action is uppercased in message regardless of input case."""
        msg = sensors.build_message(
            "Mjolnir02", None, "ON", success=True)
        assert "ON" in msg


class TestSendNotification:
    """Tests for send_notification() error-swallowing wrapper."""

    def test_none_sender_no_op(self, sensors):
        """None sender -> no exception, no call."""
        sensors.send_notification(None, "hello")  # must not raise

    def test_calls_sender_send(self, sensors):
        """Sender.send is called with the message."""
        sender = MagicMock()
        sensors.send_notification(sender, "hello")
        sender.send.assert_called_once_with("hello")

    def test_swallows_exception(self, sensors):
        """Exception from sender.send is caught (must not propagate)."""
        sender = MagicMock()
        sender.send.side_effect = RuntimeError("network down")
        sensors.send_notification(sender, "hello")  # must not raise


class TestRunNotifications:
    """Tests that run() wires notifications correctly into the on/off flow."""

    @pytest.fixture
    def configs(self, tmp_path):
        unit = tmp_path / "unit.toml"
        unit.write_text(textwrap.dedent("""\
            number = 2
            site_description = "Lab"
            [relay]
            pin = 4
            active_high = true
        """))
        main = tmp_path / "main.toml"
        main.write_text(textwrap.dedent("""\
            [steps]
                [steps.state_monitor]
                method = "gchat"
                channel = "status"
                key_file = "/home/pi/.googlechat"
        """))
        return str(unit), str(main)

    def test_off_success_sends_success_message(self, sensors, configs):
        """Successful --off triggers a 'sensor turned OFF' notification."""
        unit, main = configs
        fake_sender = MagicMock()
        with patch.object(sensors, "build_sender", return_value=fake_sender), \
             patch.object(sensors, "sensor_off", return_value=0):
            rc = sensors.run(
                ["--off"], config_path=unit, main_toml_path=main)
        assert rc == 0
        fake_sender.send.assert_called_once()
        msg = fake_sender.send.call_args[0][0]
        assert "Mjolnir02" in msg
        assert "Lab" in msg
        assert "turned OFF" in msg

    def test_on_success_sends_success_message(self, sensors, configs):
        """Successful --on triggers a 'sensor turned ON' notification."""
        unit, main = configs
        fake_sender = MagicMock()
        with patch.object(sensors, "build_sender", return_value=fake_sender), \
             patch.object(sensors, "sensor_on", return_value=0):
            rc = sensors.run(
                ["--on"], config_path=unit, main_toml_path=main)
        assert rc == 0
        msg = fake_sender.send.call_args[0][0]
        assert "turned ON" in msg

    def test_failure_sends_failure_message(self, sensors, configs):
        """Failed --off triggers a FAILED notification with the rc."""
        unit, main = configs
        fake_sender = MagicMock()
        with patch.object(sensors, "build_sender", return_value=fake_sender), \
             patch.object(sensors, "sensor_off", return_value=2):
            rc = sensors.run(
                ["--off"], config_path=unit, main_toml_path=main)
        assert rc == 2
        msg = fake_sender.send.call_args[0][0]
        assert "FAILED" in msg
        assert "rc=2" in msg

    def test_dry_run_no_notification(self, sensors, configs):
        """--dry-run must not send a notification."""
        unit, main = configs
        fake_sender = MagicMock()
        with patch.object(sensors, "build_sender", return_value=fake_sender):
            sensors.run(
                ["--off", "--dry-run"],
                config_path=unit, main_toml_path=main)
        fake_sender.send.assert_not_called()

    def test_status_no_notification(self, sensors, configs):
        """--status must not send a notification."""
        unit, main = configs
        fake_sender = MagicMock()
        with patch.object(sensors, "build_sender", return_value=fake_sender), \
             patch.object(sensors, "sensor_status", return_value="ok"):
            sensors.run(
                ["--status"], config_path=unit, main_toml_path=main)
        fake_sender.send.assert_not_called()

    def test_no_sender_still_completes(self, sensors, configs):
        """If build_sender returns None, run() still succeeds (no crash)."""
        unit, main = configs
        with patch.object(sensors, "build_sender", return_value=None), \
             patch.object(sensors, "sensor_off", return_value=0):
            rc = sensors.run(
                ["--off"], config_path=unit, main_toml_path=main)
        assert rc == 0

    def test_send_failure_does_not_change_rc(self, sensors, configs):
        """A failing sender.send() must not affect the return code."""
        unit, main = configs
        fake_sender = MagicMock()
        fake_sender.send.side_effect = RuntimeError("network down")
        with patch.object(sensors, "build_sender", return_value=fake_sender), \
             patch.object(sensors, "sensor_off", return_value=0):
            rc = sensors.run(
                ["--off"], config_path=unit, main_toml_path=main)
        assert rc == 0
