"""Tests for sensors.py — sensor power control script."""

import datetime
import importlib.util
import os
import pathlib
import textwrap

import pytest
from unittest.mock import patch, MagicMock, call

try:
    from . import test_sensors_independent as oracle
except ImportError:  # run outside the package
    import test_sensors_independent as oracle

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


def isolate_from_host(module, host_dir):
    """Point every probe of the live unit away from this machine.

    plan_mode() reads the unit file, other drop-ins and mode.toml, and asks
    systemd about the loaded unit. On a Pi, a systemd CI box or a dev box
    with brokkr installed those answers would leak into every test, so they
    are pinned here; tests that exercise a probe use the saved _real_*.
    """
    module._real_systemd_preflight = module.systemd_preflight
    module._real_systemctl_show_mode = module.systemctl_show_mode
    dropin_dir = host_dir / "brokkr-hamma-default.service.d"
    module.DROPIN_DIR = str(dropin_dir)
    module.DROPIN_PATH = str(dropin_dir / "mode.conf")
    module.DROPIN_TMP_PATH = str(dropin_dir / module.DROPIN_TMP_NAME)
    module.UNIT_FILE_PATH = str(host_dir / "absent.service")
    module.EXTRA_DROPIN_DIRS = [str(host_dir / "absent.d")]
    module.LOCAL_MODE_TOML = str(host_dir / "absent-mode.toml")
    module.systemd_preflight = lambda current: None
    module.systemctl_show_mode = lambda: None
    return module


@pytest.fixture
def sensors(tmp_path_factory):
    """Provide the sensors module, isolated from this machine's systemd."""
    return isolate_from_host(load_sensors(), tmp_path_factory.mktemp("host"))


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
        # Check tee call: a temp file, renamed over mode.conf
        tee_call = mock_run.call_args_list[1]
        tee_cmd = tee_call[0][0]
        assert tee_cmd == ["sudo", "tee", sensors.DROPIN_TMP_PATH]
        assert tee_call[1]["input"] == sensors.DROPIN_CONTENT
        assert mock_run.call_args_list[2][0][0] == [
            "sudo", "mv", "-f", sensors.DROPIN_TMP_PATH, sensors.DROPIN_PATH]

    def test_remove_dropin(self, sensors):
        """remove_dropin calls sudo rm -f."""
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sensors.remove_dropin()
        cmd = mock_run.call_args_list[0][0][0]
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
             patch.object(sensors, "apply_mode", side_effect=track("mode")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("start_sindri")):
            sensors.sensor_off(pin=17, active_high=False)

        assert calls == ["stop", "stop_sindri", "relay", "archive",
                         "mode", "reload", "start", "start_sindri"]

    def test_off_polarity_active_high_false(self, sensors):
        """Off + active_high=false -> relay energized (--on)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "stop_sindri", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0) as mock_relay, \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", return_value=0), \
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
             patch.object(sensors, "apply_mode",
                          side_effect=track("mode")):
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
             patch.object(sensors, "apply_mode",
                          side_effect=track("mode")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("start_sindri")):
            sensors.sensor_on(pin=17, active_high=False)

        assert calls == ["stop", "stop_sindri", "archive", "mode",
                         "reload", "relay", "start", "start_sindri"]

    def test_on_polarity_active_high_false(self, sensors):
        """On + active_high=false -> relay de-energized (--off)."""
        with patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "stop_sindri", return_value=0), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", return_value=0), \
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
             patch.object(sensors, "apply_mode",
                          side_effect=track("mode")):
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

    def test_dry_run_refusal_matches_the_real_run(self, sensors, tmp_path,
                                                  capsys):
        """A dry run that says REFUSE must refuse, not print a full sequence.

        The point of --dry-run is to sanity-check before doing it for real. On
        an unparseable drop-in the real path returns 1 and stops; the dry run
        printed REFUSE and then kept going, listing daemon-reload, the relay
        toggle and the start as if the run would complete, and returned 0.
        """
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\npin = 17\nactive_high = false\n")
        dropin = tmp_path / "mode.conf"
        # A mode named in a shape parse_mode cannot read -> UNKNOWN -> refuse.
        dropin.write_text(
            "[Service]\nExecStart=/x -m brokkr --mode=nosensor start\n")

        with patch.object(sensors, "DROPIN_PATH", str(dropin)):
            rc = sensors.run(["--off", "--dry-run"],
                             config_path=str(config_file))
        out = capsys.readouterr().out

        assert rc != 0, "dry run reported success for a case that hard-fails"
        assert "REFUSE" in out
        assert "daemon-reload" not in out
        assert "systemctl start" not in out


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


# --- Mode parsing and composition (HAM-184) ---

EXECSTART_DROPIN = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=/home/pi/dev/ltgenv/bin/python3 -m brokkr "
    "--system hamma --mode {} start\n"
)

ENVIRONMENT_DROPIN = "[Service]\nEnvironment=BROKKR_MODE={}\n"


class TestParseMode:
    """parse_mode() must read BOTH valid drop-in forms, not guess."""

    def test_environment_form(self, sensors):
        mode, form = sensors.parse_mode(ENVIRONMENT_DROPIN.format("nosensor"))
        assert mode == "nosensor"
        assert form == "environment"

    def test_execstart_form(self, sensors):
        mode, form = sensors.parse_mode(
            EXECSTART_DROPIN.format("nochargecontroller"))
        assert mode == "nochargecontroller"
        assert form == "execstart"

    def test_execstart_combined_mode(self, sensors):
        mode, form = sensors.parse_mode(
            EXECSTART_DROPIN.format("nosensor_nochargecontroller"))
        assert mode == "nosensor_nochargecontroller"
        assert form == "execstart"

    @pytest.mark.parametrize("content", [
        "[Service]\nExecStart=/x -m brokkr --mode=nosensor start\n",
        "[Service]\nEnvironment=BROKKR_MODE=\n",
        '[Service]\nEnvironment="BROKKR_MODE=nosensor extra"\n',
        "[Service]\nExecStart=/x -m brokkr \\\n  --mode nosensor start\n",
        "[Service]\nExecStartPre=/x --mode nosensor\n",
    ])
    def test_a_mode_named_in_a_shape_we_cannot_read_is_unknown(
            self, sensors, content):
        """A mode we cannot parse must report unknown, never a guess.

        This is the half of the old test_unparseable_returns_unknown that
        still holds: content that NAMES a mode in a shape this script cannot
        read must refuse. The other half -- that a file naming no mode at all
        is also "unknown" -- was wrong, and is now pinned the other way by
        test_a_mode_free_file_is_default_not_unknown below.
        """
        mode, _ = sensors.parse_mode(content)
        assert mode == sensors.MODE_UNKNOWN

    @pytest.mark.parametrize("content", [
        "[Service]\nRestart=always\n",
        "[Service]\nExecStart=\n",
        "[Service]\n# was: ExecStart=/x --mode nosensor start\n",
        "[Service]\n; was: ExecStart=/x --mode nosensor start\n",
    ])
    def test_a_mode_free_file_is_default_not_unknown(self, sensors, content):
        """A file that names no mode means brokkr runs in default mode.

        The bare `ExecStart=` reset line carries no mode -- the property the
        old test_empty_execstart_reset_line_ignored protected, now stated as
        "not a mode" rather than "unknown". A commented-out old command line
        is the operator's own record, and systemd comments start with EITHER
        '#' or ';' (systemd.syntax(7)).
        """
        mode, form = sensors.parse_mode(content)
        assert mode == sensors.MODE_DEFAULT
        assert form is None


class TestModeComposition:
    """Mode is two independent axes; on/off touches only the sensor axis."""

    def test_decompose(self, sensors):
        assert sensors.decompose_mode("default") == (False, False)
        assert sensors.decompose_mode("nosensor") == (True, False)
        assert sensors.decompose_mode("nochargecontroller") == (False, True)
        assert sensors.decompose_mode(
            "nosensor_nochargecontroller") == (True, True)

    def test_compose(self, sensors):
        assert sensors.compose_mode(False, False) == "default"
        assert sensors.compose_mode(True, False) == "nosensor"
        assert sensors.compose_mode(False, True) == "nochargecontroller"
        assert sensors.compose_mode(True, True) == "nosensor_nochargecontroller"

    def test_roundtrip(self, sensors):
        for mode in ("default", "nosensor", "nochargecontroller",
                     "nosensor_nochargecontroller"):
            assert sensors.compose_mode(*sensors.decompose_mode(mode)) == mode

    @pytest.mark.parametrize("current,expected", [
        ("default", "nosensor"),
        ("nochargecontroller", "nosensor_nochargecontroller"),
        ("nosensor", "nosensor"),
        ("nosensor_nochargecontroller", "nosensor_nochargecontroller"),
    ])
    def test_target_mode_off_is_sticky(self, sensors, current, expected):
        """--off sets nosensor and PRESERVES nochargecontroller."""
        assert sensors.target_mode(current, sensor_on=False) == expected

    @pytest.mark.parametrize("current,expected", [
        ("nosensor", "default"),
        ("nosensor_nochargecontroller", "nochargecontroller"),
        ("default", "default"),
        ("nochargecontroller", "nochargecontroller"),
    ])
    def test_target_mode_on_is_sticky(self, sensors, current, expected):
        """--on clears nosensor and PRESERVES nochargecontroller."""
        assert sensors.target_mode(current, sensor_on=True) == expected


class TestReadMode:
    """read_mode() reports the real mode from disk, with the bytes it parsed.

    Returning the content alongside the parse is what lets every caller work
    from one read instead of opening the file again and hoping it still says
    the same thing.
    """

    def test_no_dropin_is_default(self, sensors, tmp_path):
        with patch.object(sensors, "DROPIN_PATH", str(tmp_path / "none.conf")):
            assert sensors.read_mode() == ("default", None, None)

    def test_execstart_dropin(self, sensors, tmp_path):
        p = tmp_path / "mode.conf"
        content = EXECSTART_DROPIN.format("nochargecontroller")
        p.write_text(content)
        with patch.object(sensors, "DROPIN_PATH", str(p)):
            assert sensors.read_mode() == (
                "nochargecontroller", "execstart", content)

    def test_unreadable_dropin_is_unknown_not_default(self, sensors, tmp_path):
        """A file that exists but cannot be read must never read as 'default'.

        Absent and unreadable are different facts; conflating them would have
        apply_mode treat a live override as if it were not there.
        """
        p = tmp_path / "mode.conf"
        p.write_text(ENVIRONMENT_DROPIN.format("nosensor"))
        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch("builtins.open", side_effect=OSError("EIO")):
            assert sensors.read_mode() == ("unknown", None, None)


class TestSensorStatusReadsOnce:
    """--status must report one consistent view, and must not be able to crash.

    read_mode() already read and parsed the file; sensor_status then did its own
    isfile() plus a third, unguarded open(). Those can disagree, and the bare
    open() raises FileNotFoundError straight out of a read-only status command.
    """

    def _status(self, sensors, dropin_path):
        with patch.object(sensors, "DROPIN_PATH", str(dropin_path)), \
             patch.object(sensors, "subprocess") as mock_sub, \
             patch.object(sensors, "TELEMETRY_DIR", "/nonexistent"):
            mock_sub.run.return_value = MagicMock(stdout="active", returncode=1)
            return sensors.sensor_status({"pin": 17, "active_high": False})

    def test_reads_the_dropin_exactly_once(self, sensors, tmp_path):
        p = tmp_path / "mode.conf"
        p.write_text(EXECSTART_DROPIN.format("nochargecontroller"))
        reads = []
        real_open = open

        def counting_open(path, *args, **kwargs):
            if str(path) == str(p):
                reads.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", counting_open):
            out = self._status(sensors, p)
        assert "nochargecontroller" in out
        assert len(reads) == 1, "read {} times, want 1".format(len(reads))

    def test_missing_dropin_reports_default_without_raising(
            self, sensors, tmp_path):
        out = self._status(sensors, tmp_path / "absent.conf")
        assert "Drop-in: no (default mode)" in out


class TestRenderDropin:
    """Writing back must PRESERVE the form already on the unit."""

    def test_execstart_form_preserved_and_interpreter_kept(self, sensors):
        existing = EXECSTART_DROPIN.format("nochargecontroller")
        out = sensors.render_dropin(
            "nosensor_nochargecontroller", "execstart", existing)
        assert "--mode nosensor_nochargecontroller start" in out
        # the unit's own interpreter path must survive
        assert "/home/pi/dev/ltgenv/bin/python3" in out
        assert "nochargecontroller start" in out
        assert "--mode nochargecontroller " not in out

    def test_environment_form_preserved(self, sensors):
        existing = ENVIRONMENT_DROPIN.format("nosensor")
        out = sensors.render_dropin("nosensor", "environment", existing)
        assert "Environment=BROKKR_MODE=nosensor" in out

    def test_no_existing_dropin_uses_environment_form(self, sensors):
        out = sensors.render_dropin("nosensor", None, None)
        assert out == sensors.ENVIRONMENT_DROPIN_TEMPLATE.format("nosensor")

    def test_no_content_never_invents_an_interpreter_path(self, sensors):
        """With nothing to edit, say the mode -- do not guess a command line.

        This used to emit a hardcoded /home/pi/dev/ltgenv/bin/python3 ExecStart,
        which is a guess about the unit. Unreachable now that form and content
        come from one read, but the template is gone so it cannot come back.
        """
        out = sensors.render_dropin("nosensor", "execstart", None)
        assert "ExecStart" not in out
        assert "python3" not in out
        assert "BROKKR_MODE=nosensor" in out


class TestStickyRegression:
    """The exact HAM-184 field cases, end to end through the drop-in writer."""

    def _run(self, sensors, tmp_path, existing, sensor_on):
        p = tmp_path / "mode.conf"
        if existing is not None:
            p.write_text(existing)
        written = {}

        def fake_run_command(cmd, description, stdin_data=None):
            if cmd[:2] == ["sudo", "tee"]:
                written["content"] = stdin_data
            elif cmd[:3] == ["sudo", "rm", "-f"]:
                written["removed"] = True
            return 0

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", fake_run_command):
            rc = sensors.apply_mode(sensor_on=sensor_on)
        return rc, written

    def test_mj54_off_keeps_nochargecontroller(self, sensors, tmp_path):
        """mj54 is nochargecontroller; --off must NOT drop that half."""
        rc, w = self._run(
            sensors, tmp_path,
            EXECSTART_DROPIN.format("nochargecontroller"), sensor_on=False)
        assert rc == 0
        assert "--mode nosensor_nochargecontroller start" in w["content"]

    def test_mj06_on_keeps_nochargecontroller(self, sensors, tmp_path):
        """mj06 is nosensor_nochargecontroller; --on must leave nochargecontroller."""
        rc, w = self._run(
            sensors, tmp_path,
            EXECSTART_DROPIN.format("nosensor_nochargecontroller"),
            sensor_on=True)
        assert rc == 0
        assert "--mode nochargecontroller start" in w["content"]
        assert "removed" not in w, "must not delete a nochargecontroller drop-in"

    def test_plain_nosensor_on_still_removes_dropin(self, sensors, tmp_path):
        """The ordinary case must keep working: nosensor + --on -> no drop-in."""
        rc, w = self._run(
            sensors, tmp_path,
            ENVIRONMENT_DROPIN.format("nosensor"), sensor_on=True)
        assert rc == 0
        assert w.get("removed") is True

    def test_default_off_writes_plain_nosensor(self, sensors, tmp_path):
        """No drop-in + --off -> plain nosensor, as today."""
        rc, w = self._run(sensors, tmp_path, None, sensor_on=False)
        assert rc == 0
        assert "BROKKR_MODE=nosensor" in w["content"]

    def test_unknown_mode_refuses(self, sensors, tmp_path):
        """An unrecognised drop-in must be refused, not overwritten."""
        rc, w = self._run(
            sensors, tmp_path,
            "[Service]\nExecStart=/x -m brokkr --mode=nosensor start\n",
            sensor_on=False)
        assert rc != 0
        assert "content" not in w and "removed" not in w

    def test_mode_free_file_gets_a_mode_added_not_substituted(
            self, sensors, tmp_path):
        """A file that names no mode transitions; its directives survive.

        `[Service]\\nRestart=always\\n` used to be the fixture for "refuse",
        which conflated "sets no mode" with "sets one we cannot read". It now
        transitions -- so the rewrite has to carry the operator's directives
        across rather than emitting the bare template over the top of them.
        """
        rc, w = self._run(
            sensors, tmp_path, "[Service]\nRestart=always\n", sensor_on=False)
        assert rc == 0
        assert "Environment=BROKKR_MODE=nosensor" in w["content"]
        assert "Restart=always" in w["content"]

    def test_mode_outside_the_two_axis_model_refuses(self, sensors, tmp_path):
        """A parseable mode the two-axis model doesn't know must not be rewritten.

        `test`, `realtime` and `sindri02x` are real presets in config/mode.toml,
        and a future one costs nothing to add. All of them parse cleanly, so
        MODE_UNKNOWN never fires -- but decomposing them yields (False, False),
        so --on would compute `default` and delete the drop-in, silently
        discarding whatever the operator set. Refuse instead.
        """
        for mode in ("test", "realtime", "nosensor_futuremode"):
            rc, w = self._run(
                sensors, tmp_path, EXECSTART_DROPIN.format(mode),
                sensor_on=True)
            assert rc != 0, "{} must be refused".format(mode)
            assert "removed" not in w, "{} drop-in was deleted".format(mode)
            assert "content" not in w, "{} drop-in was rewritten".format(mode)

    def test_dropin_is_read_exactly_once(self, sensors, tmp_path):
        """One read decides both the mode and the bytes written back.

        read_mode() parses the file to get (mode, form); a second, independent
        read supplied the content to edit. Anything changing the file between
        them meant the mode came from one version and the rewrite from another
        -- and an OSError on that second read fell through to the hardcoded
        DEFAULT_EXECSTART template, silently replacing a unit's real interpreter
        path with a generic guess while still reporting success.
        """
        p = tmp_path / "mode.conf"
        p.write_text(EXECSTART_DROPIN.format("nochargecontroller"))
        reads = []
        real_open = open

        def counting_open(path, *args, **kwargs):
            if str(path) == str(p):
                reads.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", lambda *a, **k: 0), \
             patch("builtins.open", counting_open):
            rc = sensors.apply_mode(sensor_on=False)
        assert rc == 0
        assert len(reads) == 1, "read {} times, want 1".format(len(reads))


class TestRemoveDropinPreservesOtherDirectives:
    """Collapsing to default must remove the mode override, not the file.

    mode.conf is the shared filename for every override and is hand-edited in
    the field; the power-state-reconciliation design review already called out
    "could delete a custom mode.conf" as a real hazard. Anything an operator
    added alongside the mode directive has to survive.
    """

    def _collapse(self, sensors, tmp_path, existing):
        p = tmp_path / "mode.conf"
        p.write_text(existing)
        written = {}

        def fake_run_command(cmd, description, stdin_data=None):
            if cmd[:2] == ["sudo", "tee"]:
                written["content"] = stdin_data
            elif cmd[:3] == ["sudo", "rm", "-f"]:
                written["removed"] = True
            return 0

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", fake_run_command):
            rc = sensors.apply_mode(sensor_on=True)
        return rc, written

    def test_environment_form_keeps_unrelated_directives(
            self, sensors, tmp_path):
        rc, w = self._collapse(
            sensors, tmp_path,
            "[Service]\nEnvironment=BROKKR_MODE=nosensor\nRestart=always\n")
        assert rc == 0
        assert "removed" not in w, "the whole file was deleted"
        assert "Restart=always" in w["content"]
        assert "BROKKR_MODE" not in w["content"]

    def test_execstart_form_keeps_unrelated_directives(
            self, sensors, tmp_path):
        rc, w = self._collapse(
            sensors, tmp_path,
            EXECSTART_DROPIN.format("nosensor") + "TimeoutStopSec=90\n")
        assert rc == 0
        assert "removed" not in w, "the whole file was deleted"
        assert "TimeoutStopSec=90" in w["content"]
        assert "--mode" not in w["content"]
        assert "/home/pi/dev/ltgenv/bin/python3" in w["content"]

    def test_mode_only_dropin_is_still_deleted(self, sensors, tmp_path):
        """Nothing left to keep -- removing the file is the honest result."""
        rc, w = self._collapse(
            sensors, tmp_path, ENVIRONMENT_DROPIN.format("nosensor"))
        assert rc == 0
        assert w.get("removed") is True


# --- HAM-184: brokkr's own mode precedence, and refusing ambiguity ---

MIXED_DROPIN = (
    "[Service]\n"
    "Environment=BROKKR_MODE=nosensor\n"
    "ExecStart=\n"
    "ExecStart=/home/pi/dev/ltgenv/bin/python3 -m brokkr "
    "--system hamma --mode nosensor_nochargecontroller start\n"
)


def _orphan_reset(text):
    """True if `text` has a bare `ExecStart=` and no real one to reset."""
    lines = [line.strip() for line in (text or "").splitlines()]
    return ("ExecStart=" in lines
            and not any(line.startswith("ExecStart=") and line != "ExecStart="
                        for line in lines))


def _apply(sensors, tmp_path, existing, sensor_on):
    """Run apply_mode against `existing`, capturing what it tried to do."""
    p = tmp_path / "mode.conf"
    if existing is not None:
        p.write_text(existing)
    written = {}

    def fake_run_command(cmd, description, stdin_data=None):
        if cmd[:2] == ["sudo", "tee"]:
            written["content"] = stdin_data
        elif cmd[:3] == ["sudo", "rm", "-f"]:
            written["removed"] = True
        return 0

    with patch.object(sensors, "DROPIN_PATH", str(p)), \
         patch.object(sensors, "run_command", fake_run_command):
        rc = sensors.apply_mode(sensor_on=sensor_on)
    after = p.read_text() if p.exists() else None
    return rc, written, after


class TestModePrecedence:
    """brokkr resolves CLI --mode > BROKKR_MODE env > mode.toml.

    Authoritative: server/fleet_probe.py's mode probe, and
    docs/sensors-usage.md's "four placements" note. The parser checked the
    Environment= form FIRST, so on a drop-in holding both forms it reported
    the LOSER -- and every decision downstream was made about a mode that
    was not in force.
    """

    def test_execstart_beats_environment(self, sensors):
        mode, form = sensors.parse_mode(MIXED_DROPIN)
        assert mode == "nosensor_nochargecontroller", (
            "reported the Environment= form, which brokkr ignores when a "
            "--mode is on the command line")
        assert form == "execstart"

    def test_last_environment_line_wins(self, sensors):
        """systemd applies the LAST assignment of a repeated variable."""
        content = ("[Service]\n"
                   "Environment=BROKKR_MODE=nosensor\n"
                   "Environment=BROKKR_MODE=nochargecontroller\n")
        mode, form = sensors.parse_mode(content)
        assert mode == "nochargecontroller"
        assert form == "environment"

    def test_status_reports_the_mode_in_force(self, sensors, tmp_path):
        p = tmp_path / "mode.conf"
        p.write_text(MIXED_DROPIN)
        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "subprocess") as mock_sub, \
             patch.object(sensors, "TELEMETRY_DIR", "/nonexistent"):
            mock_sub.run.return_value = MagicMock(stdout="active",
                                                  returncode=1)
            out = sensors.sensor_status({"pin": 17, "active_high": False})
        assert "Brokkr mode: nosensor_nochargecontroller" in out


class TestRefusesAmbiguousDropins:
    """A mode named in more than one place is refused, never reconciled.

    Rewriting one site and leaving another is how the sticky axis gets
    destroyed while the tool prints `(preserving nochargecontroller)` and
    `[OK]`: with `nosensor` in Environment= and
    `nosensor_nochargecontroller` on the ExecStart override, an --off that
    edits only the Environment= line reports a transition to
    `nosensor_nochargecontroller` and leaves the unit on whatever the
    ExecStart line says.
    """

    AMBIGUOUS = {
        "two forms": MIXED_DROPIN,
        "two forms, same mode": (
            "[Service]\n"
            "Environment=BROKKR_MODE=nosensor\n"
            "ExecStart=\n"
            "ExecStart=/x -m brokkr --mode nosensor start\n"),
        "two environment lines": (
            "[Service]\n"
            "Environment=BROKKR_MODE=nosensor\n"
            "Environment=BROKKR_MODE=nochargecontroller\n"),
        "two mode flags on one line": (
            "[Service]\nExecStart=\n"
            "ExecStart=/x -m brokkr --mode nosensor --mode default start\n"),
        "two execstart overrides": (
            "[Service]\nExecStart=\n"
            "ExecStart=/x -m brokkr --mode nosensor start\n"
            "ExecStart=/y -m brokkr --mode nochargecontroller start\n"),
    }

    @pytest.mark.parametrize("name", sorted(AMBIGUOUS))
    @pytest.mark.parametrize("sensor_on", [True, False])
    def test_refuses_and_leaves_the_file_alone(self, sensors, tmp_path,
                                               name, sensor_on):
        existing = self.AMBIGUOUS[name]
        rc, w, after = _apply(sensors, tmp_path, existing,
                              sensor_on=sensor_on)
        assert rc != 0, "{}: was rewritten".format(name)
        assert "content" not in w, "{}: wrote a new file".format(name)
        assert "removed" not in w, "{}: deleted the file".format(name)
        assert after == existing, "{}: file changed on disk".format(name)

    # Two `--mode` flags on ONE line is excluded: that is argparse's
    # precedence, not systemd's, and brokkr is not in this repo (the local
    # clone is five years stale and must not be used to conclude production
    # behaviour). With no authority for which flag wins, "unknown" IS the
    # truthful answer -- it is a shape this script cannot read, not an
    # ambiguity across two places whose winner systemd's rules decide.
    RESOLVABLE = [name for name in sorted(AMBIGUOUS)
                  if name != "two mode flags on one line"]

    @pytest.mark.parametrize("name", RESOLVABLE)
    def test_resolution_stays_truthful_while_writing_refuses(
            self, sensors, tmp_path, name):
        """Ambiguity across places is a WRITE problem, never a mode value.

        Encoding it as a sentinel would have --status -- a read-only command
        -- tell an operator it cannot say what a live unit is running, when
        systemd's own rules say exactly what it is running. Resolution
        answers; only the rewrite refuses, because which directive to edit
        is the part that is genuinely unknowable.
        """
        existing = self.AMBIGUOUS[name]
        mode, _ = sensors.parse_mode(existing)
        assert mode != sensors.MODE_UNKNOWN, (
            "{}: resolution returned a refusal sentinel".format(name))
        assert mode in sensors.KNOWN_MODES or mode == "default", name
        # ... and it is the one systemd/brokkr will obey.
        sites, problems = sensors.scan_modes(existing)
        assert not problems and len(sites) > 1, name
        assert mode == sensors.winning_site(sites)["mode"], name
        # The write still refuses.
        rc, w, after = _apply(sensors, tmp_path, existing, sensor_on=False)
        assert rc != 0 and after == existing, name

    def test_refusal_names_what_to_inspect(self, sensors, tmp_path, capsys):
        _apply(sensors, tmp_path, MIXED_DROPIN, sensor_on=False)
        out = capsys.readouterr().out
        assert "mode.conf" in out
        assert "more than one" in out.lower()
        # Both offending lines, so the operator knows which to delete.
        assert "Environment=BROKKR_MODE=nosensor" in out
        assert "--mode nosensor_nochargecontroller" in out


class TestRefusesUnreadableModeShapes:
    """A mode token in a shape we cannot parse refuses -- and never deletes.

    `strip_mode_directive` returning None meant "nothing left to keep", and
    the collapse path answered that with `rm -f`. Reaching it with a mode
    token still in the file turned a parse failure into data loss. Refusal
    is the only safe answer; the file must be left exactly as it was.
    """

    UNREADABLE = {
        "mode with an equals sign": (
            "[Service]\nExecStart=\n"
            "ExecStart=/x -m brokkr --mode=nosensor start\n"),
        "mode on a continued line": (
            "[Service]\nExecStart=\n"
            "ExecStart=/x -m brokkr \\\n  --mode nosensor start\n"),
        "mode on ExecStartPre": (
            "[Service]\nEnvironment=BROKKR_MODE=nosensor\n"
            "ExecStartPre=/x/precheck --mode nosensor\n"),
        "mode on ExecReload": (
            "[Service]\nEnvironment=BROKKR_MODE=nosensor\n"
            "ExecReload=/x/reload --mode nosensor\n"),
        "empty environment value": "[Service]\nEnvironment=BROKKR_MODE=\n",
        "quoted multi-word environment": (
            '[Service]\nEnvironment="BROKKR_MODE=nosensor extra"\n'),
    }

    @pytest.mark.parametrize("name", sorted(UNREADABLE))
    @pytest.mark.parametrize("sensor_on", [True, False])
    def test_refuses_without_touching_the_file(self, sensors, tmp_path,
                                               name, sensor_on):
        existing = self.UNREADABLE[name]
        rc, w, after = _apply(sensors, tmp_path, existing,
                              sensor_on=sensor_on)
        assert rc != 0, "{}: was rewritten".format(name)
        assert "removed" not in w, (
            "{}: DELETED the drop-in instead of refusing".format(name))
        assert "content" not in w, "{}: wrote a new file".format(name)
        assert after == existing, "{}: file changed on disk".format(name)

    def test_a_semicolon_comment_is_a_comment_not_an_unreadable_mode(
            self, sensors, tmp_path):
        """A ';'-commented old command line must not block the toggle."""
        existing = ("[Service]\n"
                    "; was: ExecStart=/x -m brokkr --mode nosensor start\n"
                    "Environment=BROKKR_MODE=nosensor_nochargecontroller\n")
        rc, w, _ = _apply(sensors, tmp_path, existing, sensor_on=True)
        assert rc == 0
        assert "BROKKR_MODE=nochargecontroller" in w["content"]


class TestCollapseNeverBricksTheUnit:
    """A collapse leaves a file that works, or no file -- never a half one.

    A bare `ExecStart=` reset line with no replacement clears brokkr's
    command line, and the unit then will not start at all (the empty line
    exists precisely to clear the original -- hamma-expert
    services-and-pipelines.md). Keeping the file because some other line
    survived, while the real ExecStart went with the mode, is worse than
    deleting it.
    """

    def test_orphaned_reset_line_is_never_left_behind(self, sensors,
                                                      tmp_path):
        existing = ("[Service]\n"
                    "ExecStart=\n"
                    "Environment=BROKKR_MODE=nosensor\n"
                    "; operator note: front end removed 2026-09-01\n")
        rc, w, _ = _apply(sensors, tmp_path, existing, sensor_on=True)
        assert rc == 0
        content = w.get("content")
        if content is None:
            assert w.get("removed") is True
            return
        lines = [ln.strip() for ln in content.splitlines()]
        assert "ExecStart=" not in lines, (
            "left a bare ExecStart= reset with nothing to replace it; "
            "brokkr will not start: {!r}".format(content))

    def test_reset_line_survives_when_the_real_execstart_does(
            self, sensors, tmp_path):
        """The reset is required while a real ExecStart override remains."""
        existing = EXECSTART_DROPIN.format("nosensor") + "Restart=always\n"
        rc, w, _ = _apply(sensors, tmp_path, existing, sensor_on=True)
        assert rc == 0
        lines = [ln.strip() for ln in w["content"].splitlines()]
        assert "ExecStart=" in lines, "dropped the required reset line"
        assert any(ln.startswith("ExecStart=/") for ln in lines)
        assert "--mode" not in w["content"]

    def test_collapse_leaves_a_file_the_next_toggle_can_read(self, sensors,
                                                             tmp_path):
        """Stripping --mode must leave a mode-free file, not an unknown one."""
        p = tmp_path / "mode.conf"
        p.write_text(EXECSTART_DROPIN.format("nosensor"))

        def fake_run_command(cmd, description, stdin_data=None):
            if cmd[:2] == ["sudo", "tee"]:
                p.write_text(stdin_data)
            elif cmd[:3] == ["sudo", "rm", "-f"] and p.exists():
                p.unlink()
            return 0

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", fake_run_command):
            assert sensors.apply_mode(sensor_on=True) == 0
            mode_after = sensors.read_mode()[0]
        assert mode_after == "default", (
            "the next --on/--off would refuse forever; file is {!r}".format(
                p.read_text() if p.exists() else None))


class TestCollapseStripsEveryForm:
    """The second layer, below the refusal: strip ALL of them, not the winner.

    apply_mode never reaches this with two sites -- refuse_reason stops an
    ambiguous file first -- so this is defence in depth, pinned directly.
    Stripping only the form that parsed is what left `--mode nosensor` on an
    ExecStart override after the Environment= line went: relay energized,
    brokkr ingesting nothing, reported as default.
    """

    def test_collapse_removes_both_forms(self, sensors):
        sites, problems = sensors.scan_modes(MIXED_DROPIN)
        assert len(sites) == 2 and not problems
        remainder = sensors.collapse_content(MIXED_DROPIN, sites)
        assert remainder is not None
        assert "BROKKR_MODE" not in remainder
        assert "--mode" not in remainder
        assert sensors.parse_mode(remainder)[0] == sensors.MODE_DEFAULT

    def test_strip_mode_directive_never_licenses_a_delete(self, sensors):
        """Its None means "nothing left to keep" -- and nothing acts on it.

        plan_mode decides the delete itself, from content it has proved
        mode-free. A None here used to double as "a mode token survived that
        I could not strip", and remove_dropin answered that with `rm -f`.
        """
        unreadable = "[Service]\nExecStart=/x --mode=nosensor start\n"
        assert sensors.strip_mode_directive(unreadable) is None
        with patch.object(sensors, "run_command") as run:
            plan = sensors.plan_mode(sensor_on=True, path="/nonexistent/x")
            assert plan["kind"] == "noop"
        run.assert_not_called()


class TestEveryRefusalHappensBeforeAnythingMoves:
    """Refuse empty-handed: no service stopped, no mode changed. The one
    exception is a refused --off, which still cuts sensor power and nothing
    else (PB, 2026-10-30).

    apply_mode ran at step 4 of the off sequence, after brokkr and sindri
    were stopped and the relay de-energized. A refusal there leaves the unit
    dark with nothing restarted. This is the emergency power tool, used at
    low battery -- it has to fail before it touches anything, or not at all.
    """

    UNREWRITABLE = "[Service]\nExecStart=/x -m brokkr --mode=nosensor start\n"

    # EVERY reason refuse_reason can give, not just the unparseable one. The
    # `test`/`realtime`/`sindri02x` refusals already worked at the baseline
    # -- and already left the unit with brokkr stopped, sindri stopped and
    # the front end POWERED DOWN, because apply_mode was the fifth step of
    # the off sequence. That is a live half-completion on exactly the units
    # the refusal exists to protect.
    REASONS = {
        "unreadable shape": UNREWRITABLE,
        "ambiguous: two forms": MIXED_DROPIN,
        "ambiguous: two env lines": (
            "[Service]\nEnvironment=BROKKR_MODE=nosensor\n"
            "Environment=BROKKR_MODE=nochargecontroller\n"),
        "outside the two-axis model": "[Service]\n"
                                      "Environment=BROKKR_MODE=test\n",
        "outside the model (realtime)": "[Service]\n"
                                        "Environment=BROKKR_MODE=realtime\n",
        "outside the model (sindri02x)": (
            "[Service]\nEnvironment=BROKKR_MODE=sindri02x\n"),
    }

    @pytest.mark.parametrize("reason", sorted(REASONS))
    @pytest.mark.parametrize("flag", ["--on", "--off"])
    def test_run_refuses_before_any_side_effect(
            self, sensors, tmp_path, flag, reason):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = false\n")
        dropin = tmp_path / "mode.conf"
        existing = self.REASONS[reason]
        dropin.write_text(existing)

        with patch.object(sensors, "DROPIN_PATH", str(dropin)), \
             patch.object(sensors, "stop_brokkr") as stop_b, \
             patch.object(sensors, "stop_sindri") as stop_s, \
             patch.object(sensors, "toggle_relay") as relay, \
             patch.object(sensors, "archive_telemetry_csv") as archive, \
             patch.object(sensors, "daemon_reload") as reload_, \
             patch.object(sensors, "start_brokkr") as start_b, \
             patch.object(sensors, "build_sender", return_value=None):
            rc = sensors.run([flag], config_path=str(unit))

        assert rc != 0
        for name, mock in [("stop_brokkr", stop_b), ("stop_sindri", stop_s),
                           ("archive_telemetry_csv", archive),
                           ("daemon_reload", reload_),
                           ("start_brokkr", start_b)]:
            assert not mock.called, "{} ran before the refusal".format(name)
        if flag == "--off":
            relay.assert_called_once_with(
                relay_on=sensors.compute_relay_flag(False, False), pin=17,
                timeout=sensors.RELAY_TIMEOUT)
        else:
            relay.assert_not_called()
        assert dropin.read_text() == existing

    @pytest.mark.parametrize("entry", ["sensor_on", "sensor_off"])
    def test_the_sequence_functions_refuse_before_stopping_brokkr(
            self, sensors, tmp_path, entry):
        """The guarantee must not depend on run() having pre-flighted."""
        dropin = tmp_path / "mode.conf"
        dropin.write_text(self.UNREWRITABLE)
        with patch.object(sensors, "DROPIN_PATH", str(dropin)), \
             patch.object(sensors, "stop_brokkr") as stop_b, \
             patch.object(sensors, "toggle_relay") as relay:
            rc = getattr(sensors, entry)(pin=17, active_high=False)
        assert rc != 0
        stop_b.assert_not_called()
        if entry == "sensor_off":
            relay.assert_called_once_with(relay_on=True, pin=17,
                                          timeout=sensors.RELAY_TIMEOUT)
        else:
            relay.assert_not_called()

    def test_a_refusal_still_reaches_the_notification_path(self, sensors,
                                                           tmp_path):
        """Hoisting the pre-flight must not hoist it past build_sender.

        A refusal that prints to a tunnelled stdout nobody is watching and
        sends no chat notice is a silent no-op from the operator's side.
        """
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = false\n")
        dropin = tmp_path / "mode.conf"
        dropin.write_text(self.UNREWRITABLE)
        fake_sender = MagicMock()

        with patch.object(sensors, "DROPIN_PATH", str(dropin)), \
             patch.object(sensors, "build_sender",
                          return_value=fake_sender) as build, \
             patch.object(sensors, "stop_brokkr"), \
             patch.object(sensors, "toggle_relay"):
            rc = sensors.run(["--off"], config_path=str(unit))

        assert rc != 0
        build.assert_called_once()
        fake_sender.send.assert_called_once()
        msg = fake_sender.send.call_args[0][0]
        assert "OFF" in msg
        assert "REFUS" in msg.upper() or "FAIL" in msg.upper()


class TestInvariantsOverEveryDropinShape:
    """Sweep the shapes a hand-edited mode.conf can take, and assert the
    four things that must hold for all of them.

    Case-by-case tests show the cases someone thought of. The defects here
    were all reached by a shape nobody thought of -- a ';' comment, an
    ExecStartPre=, a duplicate Environment= line -- so the properties are
    asserted over the product instead.
    """

    PIECES = [
        ("", "[Service]\n"),
        ("env", "Environment=BROKKR_MODE=nosensor\n"),
        ("env2", "Environment=BROKKR_MODE=nochargecontroller\n"),
        ("reset", "ExecStart=\n"),
        ("exec", "ExecStart=/opt/v/bin/python3 -m brokkr --system hamma "
                 "--mode nochargecontroller start\n"),
        ("execbad", "ExecStart=/opt/v/bin/python3 -m brokkr --mode=nosensor "
                    "start\n"),
        ("pre", "ExecStartPre=/x/check --mode nosensor\n"),
        ("hash", "# was: ExecStart=/x --mode nosensor start\n"),
        ("semi", "; was: ExecStart=/x --mode nosensor start\n"),
        ("other", "TimeoutStopSec=90\n"),
        ("other2", "Restart=always\n"),
        ("unit", "[Unit]\n"),
        ("install", "[Install]\n"),
    ]

    def _shapes(self):
        import itertools
        seen = set()
        for size in (1, 2, 3):
            for combo in itertools.permutations(self.PIECES, size):
                text = "".join(piece for _, piece in combo)
                if text not in seen:
                    seen.add(text)
                    yield text

    def test_every_shape_upholds_the_four_invariants(self, sensors, tmp_path):
        p = tmp_path / "mode.conf"
        checked = 0
        for existing in self._shapes():
            for sensor_on in (True, False):
                p.write_text(existing)
                with patch.object(sensors, "DROPIN_PATH", str(p)):
                    plan = sensors.plan_mode(sensor_on=sensor_on)
                where = "{!r} --{}".format(
                    existing, "on" if sensor_on else "off")
                checked += 1

                # 1. plan_mode decides without touching the file.
                assert p.read_text() == existing, where

                if plan["kind"] in ("refuse", "noop"):
                    continue

                # 3b. --on/--off never moves the sticky axis.
                before = oracle.effective_mode(existing) or "default"
                assert (sensors.NOCHARGE_TOKEN in before.split("_")) == (
                    sensors.NOCHARGE_TOKEN in plan["mode"].split("_")), where

                if plan["kind"] == "delete":
                    # 2. Delete only when the mode was all the file set.
                    for line in existing.splitlines():
                        stripped = line.strip()
                        if not stripped or stripped[0] in ("#", ";"):
                            continue
                        if stripped.startswith("[") and \
                                stripped.endswith("]"):
                            continue
                        if stripped == "ExecStart=":
                            continue
                        assert ("BROKKR_MODE" in stripped
                                or "--mode" in stripped), (
                            "deleted a file still holding {!r}: {}".format(
                                stripped, where))
                    continue

                content = plan["content"]
                # 3. What gets written resolves to the target mode -- judged
                #    by the independent suite's own oracle, never by this
                #    module's parser, which cannot catch its own misparse.
                assert (oracle.effective_mode(content)
                        or sensors.MODE_DEFAULT) == plan["mode"], where

                # 4. A write never INTRODUCES an orphaned `ExecStart=`
                #    reset, which clears brokkr's command line with nothing
                #    to replace it. An orphan already in the file survives
                #    on purpose: a file like that has brokkr down already,
                #    and refusing would block an emergency power-down for a
                #    breakage this tool did not cause and cannot judge.
                if _orphan_reset(content):
                    assert _orphan_reset(existing), (
                        "introduced a bare ExecStart= reset: " + where)
        assert checked > 2500, "swept only {} shapes".format(checked)


class TestWritesPreserveEverythingButTheMode:
    """A write changes the mode and nothing else, verified by re-reading."""

    def _roundtrip(self, sensors, tmp_path, existing, sensor_on):
        p = tmp_path / "mode.conf"
        if existing is not None:
            p.write_text(existing)

        def fake_run_command(cmd, description, stdin_data=None):
            if cmd[:2] == ["sudo", "tee"]:
                p.write_text(stdin_data)
            elif cmd[:3] == ["sudo", "rm", "-f"] and p.exists():
                p.unlink()
            return 0

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", fake_run_command):
            rc = sensors.apply_mode(sensor_on=sensor_on)
            mode_after = sensors.read_mode()[0]
        return rc, mode_after, (p.read_text() if p.exists() else None)

    def test_keeps_the_units_own_interpreter_path(self, sensors, tmp_path):
        existing = EXECSTART_DROPIN.format("nochargecontroller").replace(
            "/home/pi/dev/ltgenv/bin/python3", "/opt/custom/venv/bin/python3")
        rc, mode_after, text = self._roundtrip(
            sensors, tmp_path, existing, sensor_on=False)
        assert rc == 0
        assert mode_after == "nosensor_nochargecontroller"
        assert "/opt/custom/venv/bin/python3" in text
        assert "/home/pi/dev/ltgenv" not in text

    def test_tolerates_whitespace_around_the_mode_flag(self, sensors,
                                                       tmp_path):
        """The parser accepts --mode<WS>value, so the rewriter must too."""
        spaced = EXECSTART_DROPIN.format("nochargecontroller").replace(
            "--mode nochargecontroller", "--mode\tnochargecontroller")
        rc, mode_after, text = self._roundtrip(
            sensors, tmp_path, spaced, sensor_on=False)
        assert rc == 0
        assert mode_after == "nosensor_nochargecontroller", (
            "silent no-op: file is {!r}".format(text))

    def test_environment_rewrite_keeps_other_directives(self, sensors,
                                                        tmp_path):
        existing = (ENVIRONMENT_DROPIN.format("nochargecontroller")
                    + "Restart=always\nTimeoutStopSec=90\n")
        rc, mode_after, text = self._roundtrip(
            sensors, tmp_path, existing, sensor_on=False)
        assert rc == 0
        assert mode_after == "nosensor_nochargecontroller"
        assert "Restart=always" in text
        assert "TimeoutStopSec=90" in text

    def test_quoted_environment_form_keeps_its_quoting(self, sensors,
                                                       tmp_path):
        existing = '[Service]\nEnvironment="BROKKR_MODE=nochargecontroller"\n'
        rc, mode_after, text = self._roundtrip(
            sensors, tmp_path, existing, sensor_on=False)
        assert rc == 0
        assert mode_after == "nosensor_nochargecontroller"
        assert '"BROKKR_MODE=nosensor_nochargecontroller"' in text


# --- Red team 2026-10-30: read what systemd and brokkr actually read ---

EXEC_LINE = ("ExecStart=/home/pi/dev/ltgenv/bin/python3 -m brokkr "
             "--system hamma --mode {} start")


class TestParserMatchesSystemd:
    """systemd 241's conf-parser decides what a line is; mirror it."""

    def test_comment_ending_in_backslash_does_not_continue(self, sensors):
        """systemd drops a comment before any continuation handling, so the
        next line is a live directive -- not swallowed into the comment."""
        content = ("[Service]\n# was: --mode nosensor \\\n"
                   "Environment=BROKKR_MODE=nosensor_nochargecontroller\n")
        assert sensors.parse_mode(content)[0] == \
            "nosensor_nochargecontroller"

    def test_comment_inside_a_continuation_is_skipped(self, sensors):
        """systemd drops a comment line even mid-continuation, so a mode
        that is only commented out there names nothing."""
        content = ("[Service]\nExecStartPre=/bin/true \\\n"
                   "# Environment=BROKKR_MODE=nosensor\n"
                   "  --quiet\n")
        detail = sensors.inspect_content(content)
        assert detail["problems"] == []
        assert detail["mode"] == sensors.MODE_DEFAULT

    def test_backslash_then_space_is_not_a_continuation(self, sensors):
        """systemd scans the raw line: a space after the `\\` clears it."""
        content = ("[Service]\nRestartSec=30 \\ \n[Unit]\n"
                   "Environment=BROKKR_MODE=nosensor\n")
        detail = sensors.inspect_content(content)
        assert detail["mode"] == sensors.MODE_UNKNOWN  # outside [Service]

    def test_blank_line_inside_a_continuation_is_skipped(self, sensors):
        """systemd skips blank lines like comments, even mid-continuation,
        so the next line is folded into the continued directive."""
        content = ("[Service]\nExecStartPre=/bin/echo \\\n\n"
                   "Environment=BROKKR_MODE=nosensor\n")
        assert sensors.parse_mode(content)[0] == sensors.MODE_UNKNOWN

    @pytest.mark.parametrize("header", ["[service]", "[SERVICE]"])
    def test_section_names_are_case_sensitive(self, sensors, header):
        """systemd ignores an unknown section, and `[service]` is one."""
        content = header + "\nEnvironment=BROKKR_MODE=nosensor\n"
        assert sensors.parse_mode(content)[0] == sensors.MODE_UNKNOWN

    @pytest.mark.parametrize("content", [
        "[Unit]\nDescription=x\n[Service]\nRestartSec=30\n[Install]\n"
        "WantedBy=multi-user.target\n",
        "[Unit]\nDescription=x\n[Install]\nWantedBy=multi-user.target\n",
    ])
    def test_mode_is_added_inside_service(self, sensors, content):
        out = sensors.add_mode_line(content, "nosensor")
        assert oracle.effective_mode(out) == "nosensor", out
        assert sensors.parse_mode(out)[0] == "nosensor"

    def test_mode_is_never_added_under_a_miscased_header(self, sensors):
        out = sensors.add_mode_line("[service]\nRestartSec=30\n", "nosensor")
        assert "[Service]\nEnvironment=BROKKR_MODE=nosensor" in out

    def test_escaped_backslash_is_not_a_continuation(self, sensors):
        """A line ending in an escaped backslash (`\\\\`) ends there."""
        content = ("[Service]\nTimeoutStopSec=90 \\\\\n"
                   "Environment=BROKKR_MODE=nosensor\n")
        detail = sensors.inspect_content(content)
        assert detail["problems"] == []
        assert detail["mode"] == "nosensor"

    def test_odd_backslash_run_still_continues(self, sensors):
        content = ("[Service]\nEnvironment=BROKKR_MODE=nosensor \\\\\\\n"
                   "  OTHER=1\n")
        assert sensors.parse_mode(content)[0] == sensors.MODE_UNKNOWN

    @pytest.mark.parametrize("content", [
        "[Unit]\nEnvironment=BROKKR_MODE=nosensor\n",
        "[Install]\nEnvironment=BROKKR_MODE=nochargecontroller\n",
        "Environment=BROKKR_MODE=nosensor\n",
        "[Service]\n[Unit]\n" + EXEC_LINE.format("nosensor") + "\n",
    ])
    def test_mode_outside_service_section_is_refused(self, sensors, content):
        """systemd ignores it there, so reading it as the mode is wrong in
        both directions -- refuse instead."""
        detail = sensors.inspect_content(content)
        assert detail["mode"] == sensors.MODE_UNKNOWN
        assert any("[Service]" in why for _, _, why in detail["problems"])

    @pytest.mark.parametrize("content", [
        "[Service]\nEnvironment=BROKKR_MODE=nochargecontroller\nEnvironment=\n",
        "[Service]\nEnvironment=\n",
        "[Service]\nEnvironmentFile=/etc/default/brokkr\n",
        "[Service]\nEnvironmentFile=-/etc/default/brokkr\n"
        "Environment=BROKKR_MODE=nosensor\n",
    ])
    def test_environment_reset_or_file_is_refused(self, sensors, content):
        """An empty Environment= wipes the list (the mode above it is dead,
        and a line we add above it would be too); EnvironmentFile= can set
        BROKKR_MODE where this script cannot see."""
        assert sensors.inspect_content(content)["mode"] == \
            sensors.MODE_UNKNOWN

    def test_undecodable_dropin_refuses_instead_of_crashing(
            self, sensors, tmp_path):
        p = tmp_path / "mode.conf"
        p.write_bytes(b"[Service]\n# caf\xe9\nEnvironment=BROKKR_MODE=nosensor\n")
        with patch.object(sensors, "DROPIN_PATH", str(p)):
            plan = sensors.plan_mode(sensor_on=True)
        assert plan["kind"] == "refuse"


class _Placements:
    """Point every mode source outside mode.conf at tmp paths."""

    def __init__(self, sensors, tmp_path, unit=None, other_dropin=None,
                 local_toml=None, system_toml=None, reload_pending=False,
                 loaded=None, sibling=None):
        self.sensors = sensors
        self.loaded = loaded
        unit_path = tmp_path / "brokkr-hamma-default.service"
        unit_path.write_text(unit if unit is not None else (
            "[Service]\nExecStart = /home/pi/dev/ltgenv/bin/python3 -m brokkr"
            " --system hamma start\n"))
        extra = tmp_path / "run.d"
        extra.mkdir()
        if other_dropin is not None:
            (extra / "override.conf").write_text(other_dropin)
        local = tmp_path / "local-mode.toml"
        if local_toml is not None:
            local.write_text(local_toml)
        system = tmp_path / "system-mode.toml"
        system.write_text(system_toml if system_toml is not None
                          else "[nosensor]\n")
        self.dropin = tmp_path / "dropin.d" / "mode.conf"
        self.dropin.parent.mkdir()
        if sibling is not None:
            (self.dropin.parent / "override.conf").write_text(sibling)
        placements = self

        def loaded_mode(show=None):
            if placements.loaded is not None:
                return placements.loaded
            # systemd agrees with whatever mode.conf says, unless told not to
            return sensors.read_mode_detail(str(placements.dropin))["mode"]
        self.patches = [
            patch.object(sensors, "UNIT_FILE_PATH", str(unit_path)),
            patch.object(sensors, "EXTRA_DROPIN_DIRS", [str(extra)]),
            patch.object(sensors, "LOCAL_MODE_TOML", str(local)),
            patch.object(sensors, "SYSTEM_MODE_TOML", str(system)),
            patch.object(sensors, "DROPIN_PATH", str(self.dropin)),
            patch.object(sensors, "need_daemon_reload",
                         return_value=reload_pending),
            patch.object(sensors, "manager_environment",
                         return_value="LANG=C\nPATH=/usr/bin\n"),
            patch.object(sensors, "systemd_preflight",
                         sensors._real_systemd_preflight),
            patch.object(sensors, "effective_unit_mode",
                         side_effect=loaded_mode),
        ]

    def plan(self, sensor_on, dropin=None):
        if dropin is not None:
            self.dropin.write_text(dropin)
        for p in self.patches:
            p.start()
        try:
            return self.sensors.plan_mode(sensor_on=sensor_on)
        finally:
            for p in reversed(self.patches):
                p.stop()


class TestModeSourcesOutsideTheDropin:
    """brokkr v0.4.0 merges system mode.toml < local mode.toml <
    BROKKR_MODE < CLI --mode (brokkr/config/modehandler.py). A write that
    ignores a source it cannot override reports [OK] over the wrong mode."""

    def test_clean_unit_still_plans_a_write(self, sensors, tmp_path):
        plan = _Placements(sensors, tmp_path).plan(sensor_on=False)
        assert plan["kind"] == "write"
        assert plan["mode"] == "nosensor"

    def test_unit_file_execstart_mode_refuses(self, sensors, tmp_path):
        """mj06's unit file itself carries --mode: an Environment= write
        loses to it, and a collapse would bring it back into force."""
        unit = "[Service]\n" + EXEC_LINE.format("nosensor_nochargecontroller")
        plan = _Placements(sensors, tmp_path, unit=unit).plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "brokkr-hamma-default.service" in plan["reason"]

    def test_unit_file_environment_mode_refuses(self, sensors, tmp_path):
        unit = "[Service]\nEnvironment=BROKKR_MODE=nochargecontroller\n"
        plan = _Placements(sensors, tmp_path, unit=unit).plan(sensor_on=False)
        assert plan["kind"] == "refuse"

    def test_another_dropin_naming_a_mode_refuses(self, sensors, tmp_path):
        plan = _Placements(
            sensors, tmp_path,
            other_dropin="[Service]\nEnvironment=BROKKR_MODE=nosensor\n",
        ).plan(sensor_on=True)
        assert plan["kind"] == "refuse"
        assert "override.conf" in plan["reason"]

    def test_another_dropin_without_a_mode_is_fine(self, sensors, tmp_path):
        plan = _Placements(
            sensors, tmp_path, other_dropin="[Service]\nRestartSec=30\n",
        ).plan(sensor_on=False)
        assert plan["kind"] == "write"

    def test_local_mode_toml_mode_refuses(self, sensors, tmp_path):
        """mj05: nochargecontroller in mode.toml, no drop-in. --off would
        write BROKKR_MODE=nosensor, which outranks it -- sticky axis gone."""
        plan = _Placements(sensors, tmp_path,
                           local_toml='mode = "nochargecontroller"\n'
                           ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "mode.toml" in plan["reason"]

    def test_system_mode_toml_mode_refuses(self, sensors, tmp_path):
        plan = _Placements(sensors, tmp_path,
                           system_toml='mode = "nochargecontroller"\n'
                           ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"

    def test_mode_toml_presets_alone_are_fine(self, sensors, tmp_path):
        """The [nosensor] etc. tables define modes; only a top-level `mode`
        selects one."""
        plan = _Placements(sensors, tmp_path,
                           local_toml="[nosensor.main]\nx = 1\n"
                           ).plan(sensor_on=False)
        assert plan["kind"] == "write"

    def test_unparseable_mode_toml_refuses(self, sensors, tmp_path):
        plan = _Placements(sensors, tmp_path, local_toml="mode = \n"
                           ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"

    def test_pending_daemon_reload_refuses(self, sensors, tmp_path):
        """Our own daemon-reload would silently activate somebody else's
        edit (mj54 had one pending on 2026-10-29)."""
        plan = _Placements(sensors, tmp_path, reload_pending=True
                           ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "daemon-reload" in plan["reason"]

    def test_real_system_mode_toml_selects_no_mode(self, sensors):
        """The preset shipped in this repo must not trip the check."""
        assert sensors.SYSTEM_MODE_TOML == str(REPO_ROOT / "config" /
                                               "mode.toml")
        assert sensors.mode_toml_selection(sensors.SYSTEM_MODE_TOML) is None

    def test_a_second_conf_beside_mode_conf_refuses(self, sensors, tmp_path):
        """`systemctl edit` puts override.conf right next to mode.conf: the
        most likely outside source of all."""
        plan = _Placements(
            sensors, tmp_path,
            sibling="[Service]\nEnvironment=BROKKR_MODE=nosensor\n",
        ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "override.conf" in plan["reason"]

    @pytest.mark.parametrize("unit", [
        "[Service]\nEnvironmentFile=/etc/default/brokkr\n",
        "[Service]\nExecStart=/x -m brokkr --mode=nosensor start\n",
    ])
    def test_unreadable_shapes_in_the_unit_file_refuse(
            self, sensors, tmp_path, unit):
        plan = _Placements(sensors, tmp_path, unit=unit).plan(sensor_on=False)
        assert plan["kind"] == "refuse"

    def test_undecodable_outside_file_refuses(self, sensors, tmp_path):
        placements = _Placements(sensors, tmp_path)
        (tmp_path / "run.d" / "bad.conf").write_bytes(b"[Service]\n\xff\n")
        assert placements.plan(sensor_on=False)["kind"] == "refuse"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
    def test_unreadable_outside_file_refuses(self, sensors, tmp_path):
        placements = _Placements(sensors, tmp_path)
        bad = tmp_path / "run.d" / "locked.conf"
        bad.write_text("[Service]\nRestartSec=1\n")
        bad.chmod(0)
        try:
            assert placements.plan(sensor_on=False)["kind"] == "refuse"
        finally:
            bad.chmod(0o644)

    def test_unit_file_mode_overridden_by_an_execstart_dropin_is_dead(
            self, sensors, tmp_path):
        """mj06: the unit file names a mode, but mode.conf resets ExecStart=
        and sets its own, so the unit file's line never runs -- and a write
        keeps that override in place."""
        mode = "nosensor_nochargecontroller"
        unit = "[Service]\n" + EXEC_LINE.format(mode) + "\n"
        dropin = "[Service]\nExecStart=\n" + EXEC_LINE.format(mode) + "\n"
        placements = _Placements(sensors, tmp_path, unit=unit)
        on = placements.plan(sensor_on=True, dropin=dropin)
        off = placements.plan(sensor_on=False, dropin=dropin)
        assert on["kind"] == "write" and on["mode"] == "nochargecontroller"
        assert off["kind"] == "noop"

    @pytest.mark.parametrize("content,expected", [
        ("[Service]\nExecStart=\nExecStart=/x\n", True),
        ("[Unit]\nExecStart=\nExecStart=/x\n", False),
        ("[Service]\nExecStart=\n", False),
        ("[Service]\nExecStart=/x\nExecStart=\n", False),
    ])
    def test_overrides_execstart(self, sensors, content, expected):
        assert sensors.overrides_execstart(content) is expected

    def test_execstart_skip_is_for_the_unit_file_only(
            self, sensors, tmp_path):
        """mode.conf's override makes the UNIT FILE's --mode dead -- not a
        --mode in another drop-in, which may sort after it."""
        mode = "nosensor_nochargecontroller"
        dropin = "[Service]\nExecStart=\n" + EXEC_LINE.format(mode) + "\n"
        plan = _Placements(
            sensors, tmp_path,
            other_dropin="[Service]\nExecStart=\n" + EXEC_LINE.format(mode)
            + "\n").plan(sensor_on=True, dropin=dropin)
        assert plan["kind"] == "refuse"

    def test_unit_file_mode_without_an_execstart_override_still_refuses(
            self, sensors, tmp_path):
        unit = "[Service]\n" + EXEC_LINE.format("nochargecontroller") + "\n"
        plan = _Placements(sensors, tmp_path, unit=unit).plan(
            sensor_on=False, dropin="[Service]\nEnvironment=BROKKR_MODE=x\n"
            .replace("x", "nochargecontroller"))
        assert plan["kind"] == "refuse"

    def test_pending_reload_refuses_even_a_noop(self, sensors, tmp_path):
        plan = _Placements(sensors, tmp_path, reload_pending=True).plan(
            sensor_on=True)
        assert plan["kind"] == "refuse"

    @pytest.mark.parametrize("pending", [None])
    def test_unknown_reload_state_refuses(self, sensors, tmp_path, pending):
        plan = _Placements(sensors, tmp_path, reload_pending=pending).plan(
            sensor_on=False)
        assert plan["kind"] == "refuse"

    def test_systemd_disagreeing_with_mode_conf_refuses(
            self, sensors, tmp_path):
        """Whatever the source -- a drop-in dir not listed here, the manager
        environment -- if systemd has loaded a different mode than mode.conf
        reads as, the model is wrong: refuse before anything moves."""
        plan = _Placements(sensors, tmp_path, loaded="nochargecontroller"
                           ).plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "nochargecontroller" in plan["reason"]

    def test_unreadable_loaded_mode_refuses(self, sensors, tmp_path):
        placements = _Placements(sensors, tmp_path)
        placements.loaded = None
        placements.patches[-1] = patch.object(
            sensors, "effective_unit_mode", return_value=None)
        plan = placements.plan(sensor_on=False)
        assert plan["kind"] == "refuse"
        assert "Could not read the mode systemd has loaded" in plan["reason"]

    def test_unit_file_environment_mode_survives_an_execstart_override(
            self, sensors, tmp_path):
        """Resetting ExecStart= does nothing to the unit's Environment=."""
        unit = "[Service]\nEnvironment=BROKKR_MODE=nochargecontroller\n"
        dropin = ("[Service]\nExecStart=\n" + EXEC_LINE.format("nosensor")
                  + "\n")
        plan = _Placements(sensors, tmp_path, unit=unit).plan(
            sensor_on=True, dropin=dropin)
        assert plan["kind"] == "refuse"

    def test_execstart_without_a_reset_does_not_override_the_unit(
            self, sensors, tmp_path):
        unit = "[Service]\n" + EXEC_LINE.format("nochargecontroller") + "\n"
        dropin = "[Service]\n" + EXEC_LINE.format("nochargecontroller") + "\n"
        plan = _Placements(sensors, tmp_path, unit=unit).plan(
            sensor_on=False, dropin=dropin)
        assert plan["kind"] == "refuse"

    def test_repo_paths_resolve_through_a_symlink(self, tmp_path):
        """Run as a symlink (e.g. from /usr/local/bin), config/mode.toml
        must still be the repo's, not one beside the link."""
        link = tmp_path / "bin" / "sensors.py"
        link.parent.mkdir()
        link.symlink_to(SCRIPT_PATH)
        spec = importlib.util.spec_from_file_location("sensors_link",
                                                      str(link))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.SYSTEM_MODE_TOML == str(REPO_ROOT / "config" /
                                              "mode.toml")

    def test_prefix_and_control_dropin_dirs_are_checked(self, sensors):
        dirs = load_sensors().EXTRA_DROPIN_DIRS
        assert any(d.endswith("/brokkr-.service.d") for d in dirs)
        assert any(d.endswith("/brokkr-hamma-.service.d") for d in dirs)
        assert any("/system.control/" in d for d in dirs)
        assert any(d.startswith("/usr/local/lib/systemd/system/")
                   for d in dirs)


SVC = "brokkr-hamma-default.service"
REAL_SHOW = (
    "ExecStart={{ path=/home/pi/dev/ltgenv/bin/python3 ; argv[]=/home/pi/dev/"
    "ltgenv/bin/python3 -m brokkr --system hamma {flag}start ; ignore_errors="
    "no ; start_time=[Thu 2026-10-29 10:00:01 CDT] ; stop_time=[n/a] ; "
    "pid=812 ; code=(null) ; status=0/0 }}\nEnvironment={env}\n")


class TestPlanCarriesTheOriginal:
    """A post-reload mismatch restores plan["original"]; a wrong or missing
    one would `rm` a file that should have been put back."""

    @pytest.mark.parametrize("existing,sensor_on,kind", [
        ("[Service]\nEnvironment=BROKKR_MODE=nosensor\n", False, "noop"),
        ("[Service]\nEnvironment=BROKKR_MODE=nosensor\n", True, "delete"),
        ("[Service]\nEnvironment=BROKKR_MODE=nosensor\nRestartSec=30\n",
         True, "write"),
        ("[Service]\nRestartSec=30\n", False, "write"),
        (None, False, "write"),
    ])
    def test_original_is_the_files_content(self, sensors, tmp_path,
                                           existing, sensor_on, kind):
        p = tmp_path / "mode.conf"
        if existing is not None:
            p.write_text(existing)
        with patch.object(sensors, "DROPIN_PATH", str(p)):
            plan = sensors.plan_mode(sensor_on=sensor_on)
        assert plan["kind"] == kind
        assert plan["original"] == existing

    def test_restore_needs_the_original_key(self, sensors):
        """No silent default: a plan without "original" must not turn into
        a delete."""
        with patch.object(sensors, "remove_dropin", return_value=0) as rm, \
             pytest.raises(KeyError):
            sensors.restore_mode_conf({"current": "default"})
        rm.assert_not_called()


class TestRealSystemdChain:
    """plan_mode -> systemd_preflight -> the systemctl queries, with ONLY
    subprocess faked -- and the exact argv each query must use."""

    def _plan(self, tmp_path, dropin, show, pending="no", unit=None,
              manager="LANG=en_GB.UTF-8\nPATH=/usr/bin\n", sensor_on=False):
        module = load_sensors()
        d = tmp_path / "d"
        d.mkdir()
        if dropin is not None:
            (d / "mode.conf").write_text(dropin)
        u = tmp_path / "unit.service"
        u.write_text(unit or "[Service]\nExecStart=/home/pi/dev/ltgenv/bin/"
                     "python3 -m brokkr --system hamma start\n")
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            if cmd == ["systemctl", "show", "-p", "NeedDaemonReload",
                       "--value", SVC]:
                return MagicMock(returncode=0, stdout=pending + "\n")
            if cmd == ["systemctl", "show", "-p", "ExecStart", "-p",
                       "Environment", SVC]:
                return MagicMock(returncode=0, stdout=show)
            if cmd == ["systemctl", "show-environment"]:
                if manager is None:
                    return MagicMock(returncode=1, stdout="")
                return MagicMock(returncode=0, stdout=manager)
            raise AssertionError("unexpected command {!r}".format(cmd))
        with patch.object(module, "DROPIN_PATH", str(d / "mode.conf")), \
             patch.object(module, "UNIT_FILE_PATH", str(u)), \
             patch.object(module, "EXTRA_DROPIN_DIRS", [str(tmp_path / "x.d")]), \
             patch.object(module, "LOCAL_MODE_TOML", str(tmp_path / "x.toml")), \
             patch("subprocess.run", side_effect=run):
            plan = module.plan_mode(sensor_on=sensor_on)
        return plan, seen

    def test_environment_form_writes(self, tmp_path):
        plan, seen = self._plan(
            tmp_path, "[Service]\nEnvironment=BROKKR_MODE=nochargecontroller\n",
            REAL_SHOW.format(flag="", env="BROKKR_MODE=nochargecontroller"))
        assert plan["kind"] == "write", plan
        assert plan["mode"] == "nosensor_nochargecontroller"
        assert len(seen) == 3

    def test_mj06_execstart_override_writes(self, tmp_path):
        mode = "nochargecontroller"
        plan, _ = self._plan(
            tmp_path, "[Service]\nExecStart=\n" + EXEC_LINE.format(mode),
            REAL_SHOW.format(flag="--mode {} ".format(mode), env=""),
            unit="[Service]\n" + EXEC_LINE.format(mode) + "\n")
        assert plan["kind"] == "write", plan
        assert plan["mode"] == "nosensor_nochargecontroller"

    @pytest.mark.parametrize("env", ["LANG=C", ""])
    def test_no_dropin_writes(self, tmp_path, env):
        plan, _ = self._plan(tmp_path, None, REAL_SHOW.format(flag="", env=env))
        assert plan["kind"] == "write" and plan["mode"] == "nosensor", plan

    def test_loaded_mode_mismatch_refuses(self, tmp_path):
        plan, _ = self._plan(tmp_path, None, REAL_SHOW.format(
            flag="", env="BROKKR_MODE=nosensor"))
        assert plan["kind"] == "refuse" and "differs" in plan["summary"]

    def test_pending_reload_refuses(self, tmp_path):
        plan, _ = self._plan(tmp_path, None, REAL_SHOW.format(flag="", env=""),
                             pending="yes")
        assert plan["kind"] == "refuse" and "pending" in plan["summary"]

    def test_unit_not_loaded_refuses(self, tmp_path):
        plan, _ = self._plan(tmp_path, None, "Environment=\n")
        assert plan["kind"] == "refuse"

    def test_manager_environment_mode_refuses(self, tmp_path):
        """systemctl set-environment / DefaultEnvironment= reach the service
        but are not in `show -p Environment`: invisible until mode.conf is
        removed, and then they win."""
        plan, _ = self._plan(
            tmp_path, "[Service]\nEnvironment=BROKKR_MODE=nosensor\n",
            REAL_SHOW.format(flag="", env="BROKKR_MODE=nosensor"),
            manager="LANG=C\nBROKKR_MODE=nochargecontroller\n", sensor_on=True)
        assert plan["kind"] == "refuse"
        assert "manager environment" in plan["reason"]

    def test_unreadable_manager_environment_refuses_the_plan(self, tmp_path):
        plan, _ = self._plan(tmp_path, None, REAL_SHOW.format(flag="", env=""),
                             manager=None)
        assert plan["kind"] == "refuse"
        assert plan["summary"] == "systemd could not be queried"

    def test_unreadable_manager_environment_is_none(self, tmp_path):
        module = load_sensors()
        with patch("subprocess.run", return_value=MagicMock(
                returncode=1, stdout="")):
            assert module.manager_environment() is None


class TestSystemdQueries:
    """The real need_daemon_reload / systemctl_show_mode, faked subprocess."""

    @pytest.mark.parametrize("rc,out,expected", [
        (0, "yes\n", True), (0, "no\n", False), (1, "", None),
        (0, "", None), (1, "no\n", None), (0, "NeedDaemonReload=no\n", None),
    ])
    def test_need_daemon_reload(self, rc, out, expected):
        module = load_sensors()
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=rc, stdout=out)):
            assert module.need_daemon_reload() is expected

    @pytest.mark.parametrize("error", [
        OSError("no systemctl"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        __import__("subprocess").TimeoutExpired("systemctl", 30),
    ])
    def test_query_failures_are_unknown_not_a_crash(self, error):
        module = load_sensors()
        with patch("subprocess.run", side_effect=error):
            assert module.need_daemon_reload() is None
            assert module.systemctl_show_mode() is None

    def test_queries_have_a_timeout(self):
        module = load_sensors()
        with patch("subprocess.run", return_value=MagicMock(
                returncode=0, stdout="no\n")) as run:
            module.need_daemon_reload()
            module.systemctl_show_mode()
        assert all(c[1].get("timeout") for c in run.call_args_list)


SHOW_TEMPLATE = (
    "ExecStart={{ path=/home/pi/dev/ltgenv/bin/python3 ; argv[]=/home/pi/dev/"
    "ltgenv/bin/python3 -m brokkr --system hamma {flag}start ; "
    "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
    "code=(null) ; status=0/0 }}\nEnvironment={env}\n")


class TestEffectiveModeAfterReload:
    """After daemon-reload, ask systemd what it will run before starting."""

    @pytest.mark.parametrize("flag,env,expected", [
        ("", "", "default"),
        ("", "BROKKR_MODE=nosensor", "nosensor"),
        ("", "FOO=1 BROKKR_MODE=nosensor_nochargecontroller",
         "nosensor_nochargecontroller"),
        ("--mode nochargecontroller ", "BROKKR_MODE=nosensor",
         "nochargecontroller"),
        ("--mode\tnosensor ", "", "nosensor"),
    ])
    def test_reads_systemds_view(self, sensors, flag, env, expected):
        show = SHOW_TEMPLATE.format(flag=flag, env=env)
        assert sensors.effective_unit_mode(show) == expected

    @pytest.mark.parametrize("show", ["", "garbage\n", None,
                                      "Environment=BROKKR_MODE=x\n",
                                      "ExecStart=\nEnvironment=\n"])
    def test_unreadable_view_is_none(self, sensors, show):
        module = load_sensors()  # the real systemctl_show_mode, for None
        with patch("subprocess.run", side_effect=OSError("no systemctl")):
            assert module.effective_unit_mode(show) is None

    @pytest.mark.parametrize("flag,env,expected", [
        ("", "BROKKR_MODE=nosensor BROKKR_MODE=nochargecontroller",
         "nochargecontroller"),
        ("--mode=nosensor ", "", "nosensor"),
        ("", "BROKKR_MODE=", None),
    ])
    def test_last_assignment_eq_form_and_empty_value(
            self, sensors, flag, env, expected):
        show = SHOW_TEMPLATE.format(flag=flag, env=env)
        assert sensors.effective_unit_mode(show) == expected

    def test_only_the_exact_variable_counts(self, sensors):
        show = SHOW_TEMPLATE.format(flag="", env="NOT_BROKKR_MODE=nosensor")
        assert sensors.effective_unit_mode(show) == "default"

    def test_environment_line_before_execstart(self, sensors):
        show = SHOW_TEMPLATE.format(flag="", env="BROKKR_MODE=nosensor")
        lines = show.strip().split("\n")
        assert sensors.effective_unit_mode(
            "\n".join(reversed(lines)) + "\n") == "nosensor"

    def _off(self, sensors, show):
        calls = []
        plan = {"kind": "write", "current": "default", "mode": "nosensor",
                "content": "[Service]\nEnvironment=BROKKR_MODE=nosensor\n",
                "original": None}

        def track(name, rc=0):
            def fn(*args, **kwargs):
                calls.append(name)
                return rc
            return fn
        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "stop_sindri", side_effect=track("ss")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", side_effect=track("mode")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "systemctl_show_mode", return_value=show), \
             patch.object(sensors, "remove_dropin", return_value=0), \
             patch.object(sensors, "write_dropin", return_value=0), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("si")):
            rc = sensors.sensor_off(pin=17, active_high=True, plan=plan)
        return rc, calls

    def test_matching_mode_succeeds(self, sensors):
        rc, calls = self._off(sensors, SHOW_TEMPLATE.format(
            flag="", env="BROKKR_MODE=nosensor"))
        assert rc == 0
        assert calls[-2:] == ["start", "si"]

    def test_mismatch_fails_but_still_starts_brokkr(self, sensors, capsys):
        """A stopped brokkr sends no telemetry and no alerts, so it is
        started either way -- and the run reports the failure."""
        rc, calls = self._off(sensors, SHOW_TEMPLATE.format(
            flag="--mode default ", env="BROKKR_MODE=nosensor"))
        assert rc != 0
        assert "start" in calls
        assert "'default'" in capsys.readouterr().out

    def test_unverifiable_view_warns_and_succeeds(self, sensors, capsys):
        rc, _ = self._off(sensors, "")
        assert rc == 0
        assert "could not verify" in capsys.readouterr().out.lower()

    def _on(self, sensors, show):
        calls = []
        plan = {"kind": "write", "current": "nosensor_nochargecontroller",
                "mode": "nochargecontroller", "content": "x",
                "original": "orig"}

        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                return 0
            return fn
        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "stop_sindri", side_effect=track("ss")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", side_effect=track("mode")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "systemctl_show_mode", return_value=show), \
             patch.object(sensors, "remove_dropin", return_value=0), \
             patch.object(sensors, "write_dropin", return_value=0), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("si")):
            rc = sensors.sensor_on(pin=17, active_high=True, plan=plan)
        return rc, calls, plan

    def test_on_checks_the_target_mode_not_the_current_one(self, sensors):
        rc, calls, _ = self._on(sensors, SHOW_TEMPLATE.format(
            flag="--mode nochargecontroller ", env=""))
        assert rc == 0
        assert "relay" in calls

    def test_on_mismatch_leaves_sensor_power_alone_but_starts_brokkr(
            self, sensors, capsys):
        """--on is not the emergency direction: on a mode the script got
        wrong, not powering the sensor is the safe failure."""
        rc, calls, plan = self._on(sensors, SHOW_TEMPLATE.format(
            flag="--mode nosensor_nochargecontroller ", env=""))
        assert rc == sensors.RC_MODE_MISMATCH
        assert "relay" not in calls
        assert calls[-2:] == ["start", "si"]
        assert plan["mismatch"] == ("nosensor_nochargecontroller",
                                    "nochargecontroller")
        out = capsys.readouterr().out
        assert "sensor power was not changed" in out.lower()
        assert "relay off" not in out.lower()

    def test_off_mismatch_is_recorded_on_the_plan(self, sensors):
        plan = {"kind": "write", "current": "default", "mode": "nosensor",
                "content": "x", "original": None}
        with patch.object(sensors, "remove_dropin", return_value=0), \
             patch.object(sensors, "write_dropin", return_value=0), \
             patch.object(sensors, "stop_brokkr", return_value=0), \
             patch.object(sensors, "stop_sindri", return_value=0), \
             patch.object(sensors, "toggle_relay", return_value=0), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", return_value=0), \
             patch.object(sensors, "daemon_reload", return_value=0), \
             patch.object(sensors, "systemctl_show_mode",
                          return_value=SHOW_TEMPLATE.format(flag="", env="")), \
             patch.object(sensors, "start_brokkr", return_value=0), \
             patch.object(sensors, "start_sindri", return_value=0):
            rc = sensors.sensor_off(pin=17, active_high=True, plan=plan)
        assert rc == sensors.RC_MODE_MISMATCH
        assert plan["mismatch"] == ("default", "nosensor")

    def _mismatch(self, sensors, sensor_on, original, show_after_restore):
        """Run a sequence whose post-reload check fails, recording calls."""
        calls = []
        shows = [SHOW_TEMPLATE.format(flag="--mode default ", env=""),
                 show_after_restore]
        plan = {"kind": "write", "current": "nochargecontroller",
                "mode": "nosensor_nochargecontroller" if not sensor_on
                else "nochargecontroller",
                "content": "new", "original": original}
        if sensor_on:
            plan["current"] = "nosensor_nochargecontroller"

        def track(name, rc=0):
            def fn(*args, **kwargs):
                calls.append((name,) + args)
                return rc
            return fn
        with patch.object(sensors, "stop_brokkr", side_effect=track("stop")), \
             patch.object(sensors, "stop_sindri", side_effect=track("ss")), \
             patch.object(sensors, "toggle_relay", side_effect=track("relay")), \
             patch.object(sensors, "archive_telemetry_csv"), \
             patch.object(sensors, "apply_mode", side_effect=track("mode")), \
             patch.object(sensors, "write_dropin", side_effect=track("restore")), \
             patch.object(sensors, "remove_dropin", side_effect=track("unlink")), \
             patch.object(sensors, "daemon_reload", side_effect=track("reload")), \
             patch.object(sensors, "systemctl_show_mode",
                          side_effect=lambda: shows.pop(0)), \
             patch.object(sensors, "start_brokkr", side_effect=track("start")), \
             patch.object(sensors, "start_sindri", side_effect=track("si")):
            fn = sensors.sensor_on if sensor_on else sensors.sensor_off
            rc = fn(pin=17, active_high=True, plan=plan)
        return rc, [c[0] for c in calls], calls, plan

    @pytest.mark.parametrize("sensor_on", [True, False])
    def test_mismatch_restores_the_original_mode_conf(
            self, sensors, sensor_on):
        """Otherwise mode.conf now disagrees with what systemd runs, and
        every later toggle -- the emergency --off included -- refuses."""
        current = ("nosensor_nochargecontroller" if sensor_on
                   else "nochargecontroller")
        rc, names, calls, plan = self._mismatch(
            sensors, sensor_on, "ORIGINAL BYTES",
            SHOW_TEMPLATE.format(flag="--mode {} ".format(current), env=""))
        assert rc == sensors.RC_MODE_MISMATCH
        i = names.index("restore")
        assert calls[i][1] == "ORIGINAL BYTES"
        assert names[i - 1] == "reload" and names[i + 1] == "reload"
        assert names[-2:] == ["start", "si"]
        assert plan["restored"] is True

    def test_mismatch_with_no_original_file_removes_ours(self, sensors):
        rc, names, _, plan = self._mismatch(
            sensors, False, None,
            SHOW_TEMPLATE.format(flag="--mode nochargecontroller ", env=""))
        assert "unlink" in names and "restore" not in names
        assert plan["restored"] is True

    def test_unconfirmed_restore_is_not_called_a_failure(self, sensors,
                                                         capsys):
        """The file went back and systemd reloaded; only the confirmation is
        missing -- say that, not "could not restore"."""
        rc, names, _, plan = self._mismatch(sensors, False, "ORIGINAL BYTES",
                                            "")
        assert plan["restored"] is True
        out = capsys.readouterr().out.lower()
        assert "could not confirm" in out and "could not restore" not in out

    def test_a_failed_restore_is_reported(self, sensors, capsys):
        rc, names, _, plan = self._mismatch(
            sensors, False, "ORIGINAL BYTES",
            SHOW_TEMPLATE.format(flag="--mode default ", env=""))
        assert rc == sensors.RC_MODE_MISMATCH
        assert plan["restored"] is False
        assert "could not restore" in capsys.readouterr().out.lower()
        assert names[-2:] == ["start", "si"]

    def test_mismatch_notification_wording(self, sensors, tmp_path):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        sent = []
        for sensor_on in (True, False):
            sender = MagicMock()

            def fake(pin, active_high, plan):
                plan["mismatch"] = ("default", "nosensor")
                plan["restored"] = True
                return sensors.RC_MODE_MISMATCH
            with patch.object(sensors, "build_sender", return_value=sender), \
                 patch.object(sensors, "sensor_on", side_effect=fake), \
                 patch.object(sensors, "sensor_off", side_effect=fake):
                sensors.run(["--on" if sensor_on else "--off"],
                            config_path=str(unit),
                            main_toml_path=str(tmp_path / "main.toml"))
            sent.append(sender.send.call_args[0][0])
        on, off = sent
        assert "mode 'default', not 'nosensor'" in on
        assert "sensor power not changed" in on
        assert "sensor power OFF" in off
        assert "mode.conf restored" in on and "mode.conf restored" in off

    def test_mismatch_notification_names_both_modes(self, sensors, tmp_path):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        sender = MagicMock()

        def fake_off(pin, active_high, plan):
            plan["mismatch"] = ("default", "nosensor")
            return sensors.RC_MODE_MISMATCH
        with patch.object(sensors, "DROPIN_PATH", str(tmp_path / "none")), \
             patch.object(sensors, "build_sender", return_value=sender), \
             patch.object(sensors, "sensor_off", side_effect=fake_off):
            rc = sensors.run(["--off"], config_path=str(unit),
                             main_toml_path=str(tmp_path / "main.toml"))
        assert rc == sensors.RC_MODE_MISMATCH
        message = sender.send.call_args[0][0]
        assert "'default'" in message and "'nosensor'" in message


class TestRefusedOffStillCutsPower:
    """A refused --off still de-energizes the sensor (PB, 2026-10-30): the
    mode is left alone, but at low battery the power has to go."""

    def _run(self, sensors, tmp_path, argv, active_high=True):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = {}\n".format(
            "true" if active_high else "false"))
        sender = MagicMock()
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "  [ERROR] nope", "summary": "the summary"}
        with patch.object(sensors, "plan_mode", return_value=refuse), \
             patch.object(sensors, "build_sender", return_value=sender), \
             patch.object(sensors, "toggle_relay", return_value=0) as relay, \
             patch.object(sensors, "stop_brokkr", return_value=0) as stop, \
             patch.object(sensors, "apply_mode", return_value=0) as mode:
            rc = sensors.run(argv, config_path=str(unit),
                             main_toml_path=str(tmp_path / "main.toml"))
        return rc, relay, stop, mode, sender

    @pytest.mark.parametrize("active_high", [True, False])
    def test_refused_off_cuts_power_only(self, sensors, tmp_path,
                                         active_high):
        rc, relay, stop, mode, sender = self._run(
            sensors, tmp_path, ["--off"], active_high)
        assert rc == 1
        relay.assert_called_once_with(
            relay_on=sensors.compute_relay_flag(False, active_high), pin=17,
            timeout=sensors.RELAY_TIMEOUT)
        stop.assert_not_called()
        mode.assert_not_called()
        message = sender.send.call_args[0][0]
        assert "REFUSED: the summary" in message
        assert "sensor power CUT" in message

    def test_refused_on_touches_nothing(self, sensors, tmp_path):
        rc, relay, stop, mode, sender = self._run(sensors, tmp_path, ["--on"])
        assert rc == 1
        relay.assert_not_called()
        stop.assert_not_called()
        assert "REFUSED: the summary" in sender.send.call_args[0][0]

    def test_failed_power_cut_is_reported(self, sensors, tmp_path):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        sender = MagicMock()
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "x", "summary": "s"}
        with patch.object(sensors, "plan_mode", return_value=refuse), \
             patch.object(sensors, "build_sender", return_value=sender), \
             patch.object(sensors, "toggle_relay", return_value=7):
            rc = sensors.run(["--off"], config_path=str(unit),
                             main_toml_path=str(tmp_path / "main.toml"))
        assert rc == sensors.RC_CUT_FAILED
        assert "power cut FAILED" in sender.send.call_args[0][0]

    @pytest.mark.parametrize("error", [
        OSError("relay.py missing"),
        __import__("subprocess").TimeoutExpired("relay.py", 60)])
    def test_a_crashing_relay_still_sends_the_refusal(
            self, sensors, tmp_path, error):
        """The cut runs before the notice: a missing or hung relay.py must
        not swallow it."""
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        sender = MagicMock()
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "x", "summary": "s"}
        with patch.object(sensors, "plan_mode", return_value=refuse), \
             patch.object(sensors, "build_sender", return_value=sender), \
             patch("subprocess.run", side_effect=error):
            rc = sensors.run(["--off"], config_path=str(unit),
                             main_toml_path=str(tmp_path / "main.toml"))
        assert rc == sensors.RC_CUT_FAILED
        assert "power cut FAILED" in sender.send.call_args[0][0]

    def test_the_cut_is_bounded(self, sensors):
        with patch("subprocess.run", return_value=MagicMock(
                returncode=0, stderr="")) as run:
            sensors.cut_sensor_power(17, True)
        assert run.call_args[1].get("timeout")

    def test_sensor_off_called_directly_also_cuts_power(self, sensors):
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "x", "summary": "s"}
        with patch.object(sensors, "toggle_relay", return_value=0) as relay, \
             patch.object(sensors, "stop_brokkr", return_value=0) as stop:
            rc = sensors.sensor_off(pin=17, active_high=True, plan=refuse)
        assert rc == 1
        relay.assert_called_once_with(relay_on=False, pin=17,
                                      timeout=sensors.RELAY_TIMEOUT)
        stop.assert_not_called()

    def test_dry_run_refusal_shows_only_the_power_cut(
            self, sensors, tmp_path, capsys):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "  [ERROR] nope", "summary": "s"}
        with patch.object(sensors, "plan_mode", return_value=refuse):
            rc = sensors.run(["--off", "--dry-run"], config_path=str(unit))
        out = capsys.readouterr().out
        assert rc == 1
        assert "systemctl stop" not in out
        assert "would cut sensor power" in out.lower()
        assert "relay.py --pin 17 --off" in out

    def test_dry_run_on_refusal_lists_no_steps(self, sensors, tmp_path,
                                               capsys):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        refuse = {"kind": "refuse", "current": "default", "mode": None,
                  "reason": "  [ERROR] nope", "summary": "s"}
        with patch.object(sensors, "plan_mode", return_value=refuse):
            sensors.run(["--on", "--dry-run"], config_path=str(unit))
        out = capsys.readouterr().out
        assert "systemctl stop" not in out and "relay.py" not in out


class TestDryRunShowsTheWholeSequence:

    def test_dry_run_lists_temp_write_rename_and_mode_check(
            self, sensors, tmp_path, capsys):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        with patch.object(sensors, "DROPIN_PATH",
                          str(tmp_path / "mode.conf")):
            rc = sensors.run(["--off", "--dry-run"], config_path=str(unit))
        out = capsys.readouterr().out
        assert rc == 0
        assert "tee" in out and "mv -f" in out
        assert "timeout 30 sync -f" in out
        assert "systemctl show" in out


class TestDryRunDetails:

    def _dry(self, sensors, tmp_path, flag, dropin=None):
        unit = tmp_path / "unit.toml"
        unit.write_text("[relay]\npin = 17\nactive_high = true\n")
        path = tmp_path / "mode.conf"
        if dropin is not None:
            path.write_text(dropin)
        with patch.object(sensors, "DROPIN_PATH", str(path)):
            return sensors.run([flag, "--dry-run"], config_path=str(unit))

    def test_on_says_the_relay_waits_for_the_mode_check(
            self, sensors, tmp_path, capsys):
        self._dry(sensors, tmp_path, "--on",
                  "[Service]\nEnvironment=BROKKR_MODE=nosensor\n")
        out = capsys.readouterr().out
        assert "(if not: relay not commanded, mode.conf restored)" in out

    def test_delete_lists_no_temp_write(self, sensors, tmp_path, capsys):
        self._dry(sensors, tmp_path, "--on",
                  "[Service]\nEnvironment=BROKKR_MODE=nosensor\n")
        out = capsys.readouterr().out
        assert "sudo rm -f" in out and "sudo tee" not in out

    def test_off_cuts_power_before_the_archive(self, sensors, tmp_path,
                                               capsys):
        self._dry(sensors, tmp_path, "--off")
        out = capsys.readouterr().out
        relay = out.index("relay.py --pin 17 --off")
        assert out.index("systemctl stop") < relay < out.index(
            "Archive telemetry CSV")


class TestAtomicDropinWrite:
    """mode.conf is now operator-authored, multi-line content: write it to
    a temp file and rename, never truncate the live file in place."""

    def test_writes_temp_then_renames(self, sensors):
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            rc = sensors.write_dropin("[Service]\nX=1\n", "nosensor")
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert rc == 0
        assert cmds == [
            ["sudo", "mkdir", "-p", sensors.DROPIN_DIR],
            ["sudo", "tee", sensors.DROPIN_TMP_PATH],
            ["sudo", "mv", "-f", sensors.DROPIN_TMP_PATH, sensors.DROPIN_PATH],
            ["timeout", "30", "sync", "-f", sensors.DROPIN_DIR],
        ]
        assert mock_run.call_args_list[1][1]["input"] == "[Service]\nX=1\n"

    def test_temp_name_is_not_loaded_by_systemd(self, sensors):
        """systemd only reads *.conf from the drop-in dir."""
        assert not sensors.DROPIN_TMP_PATH.endswith(".conf")
        assert os.path.dirname(sensors.DROPIN_TMP_PATH) == sensors.DROPIN_DIR

    def test_failed_rename_is_a_failure(self, sensors):
        def fake(cmd, **kwargs):
            return MagicMock(returncode=1 if cmd[1] == "mv" else 0, stderr="x")
        with patch("subprocess.run", side_effect=fake):
            assert sensors.write_dropin("[Service]\nX=1\n", "nosensor") != 0

    def test_flushes_after_the_rename(self, sensors):
        """A first-time mode.conf can come back empty after a power cut
        (likely, at low battery) unless it is synced."""
        with patch("subprocess.run", return_value=MagicMock(
                returncode=0, stderr="")) as mock_run:
            sensors.write_dropin("[Service]\nX=1\n", "nosensor")
        cmds = [c[0][0] for c in mock_run.call_args_list]
        # Only the drop-in's filesystem, with a bound: a plain `sync`
        # flushes the USB data sticks too, while brokkr is stopped.
        assert cmds[-1] == ["timeout", "30", "sync", "-f", sensors.DROPIN_DIR]

    def test_delete_is_flushed_too(self, sensors):
        with patch("subprocess.run", return_value=MagicMock(
                returncode=0, stderr="")) as mock_run:
            assert sensors.remove_dropin() == 0
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert cmds == [["sudo", "rm", "-f", sensors.DROPIN_PATH],
                        ["timeout", "30", "sync", "-f", sensors.DROPIN_DIR]]

    def test_failed_mkdir_writes_nothing(self, sensors):
        with patch("subprocess.run", return_value=MagicMock(
                returncode=1, stderr="x")) as mock_run:
            assert sensors.write_dropin("[Service]\nX=1\n", "nosensor") != 0
        assert len(mock_run.call_args_list) == 1

    def test_failed_temp_write_leaves_live_file_alone(self, sensors):
        def fake(cmd, **kwargs):
            return MagicMock(returncode=1 if cmd[1] == "tee" else 0,
                             stderr="No space left on device")
        with patch("subprocess.run", side_effect=fake) as mock_run:
            rc = sensors.write_dropin("[Service]\nX=1\n", "nosensor")
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert rc != 0
        assert not any(c[1] == "mv" for c in cmds)
        assert ["sudo", "rm", "-f", sensors.DROPIN_TMP_PATH] in cmds
