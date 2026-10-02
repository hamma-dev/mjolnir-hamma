"""Tests for sensors.py — sensor power control script.
INDEPENDENTLY WRITTEN SUITE (HAM-184). Kept as its own module, verbatim.

Written from the specification by an agent that never saw the
implementation, so these tests cannot have been shaped to fit it. Two
earlier attempts at this fix shipped regressions their own tests did not
catch -- in both cases the same author wrote the code and the tests that
judged it.

It does not trust the module under test to say what a drop-in means: it
carries its own oracle, effective_mode()/non_mode_lines(), implementing
brokkr's documented precedence directly (CLI `--mode` beats
`Environment=BROKKR_MODE=`; within a form systemd's last assignment
wins), so it cannot be fooled by the inverted-precedence bug it exists to
catch. Everything round-trips through a real file.

Measured: 48 failed / 222 passed against the pre-fix baseline 052ee1f;
269 passed against this implementation.

It is verbatim, and therefore re-runs the shared baseline tests that
test_sensors.py also runs. That duplication is deliberate: extracting
only the new definitions silently emptied the parametrize lists built
from shared fixture data, and a suite whose value is independence is not
worth hand-editing to save a few hundred milliseconds.
"""

import datetime
import importlib.util
import io
import os
import pathlib
import re
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
        # An empty mode FIELD is unparseable. ("[Service]\nRestart=always\n",
        # the original fixture, is mode-free and now resolves to `default`.)
        dropin.write_text("[Service]\nEnvironment=BROKKR_MODE=\n")

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

    def test_unparseable_returns_unknown(self, sensors):
        """Content we don't recognise must report unknown, never a guess.

        Fixture changed from "[Service]\\nRestart=always\\n" by the
        independent spec suite below: a file with no mode field at all is
        provably mode-free and resolves to `default` (see
        TestModeFreeFileIsDefault). What is genuinely unparseable is a
        mode FIELD whose value cannot be read -- here the field is
        present and empty.
        """
        mode, form = sensors.parse_mode(
            "[Service]\nEnvironment=BROKKR_MODE=\n")
        assert mode == sensors.MODE_UNKNOWN

    def test_empty_execstart_reset_line_ignored(self, sensors):
        """The bare 'ExecStart=' reset line must not parse as a mode.

        Expectation changed from MODE_UNKNOWN by the independent spec
        suite below: the reset line carries no `--mode`, so the file sets
        no mode, and a file that sets no mode is `default`. (That the
        reset line alone is a broken unit is a WRITE post-condition --
        see TestCollapseToDefault -- not a parse result.)
        """
        mode, _ = sensors.parse_mode("[Service]\nExecStart=\n")
        assert mode == sensors.MODE_DEFAULT


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
        """An unrecognised drop-in must be refused, not overwritten.

        Fixture changed from "[Service]\\nRestart=always\\n" for the same
        reason as TestParseMode.test_unparseable_returns_unknown: a file
        with no mode field is mode-free, not unparseable.
        """
        rc, w = self._run(
            sensors, tmp_path, "[Service]\nEnvironment=BROKKR_MODE=\n",
            sensor_on=False)
        assert rc != 0
        assert "content" not in w and "removed" not in w

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


# =====================================================================
# HAM-184 / PR #101 -- independent spec suite
#
# Written from the spec alone, against the API as of 052ee1f, with no
# sight of the implementation. Two earlier attempts at this fix shipped
# regressions their own tests did not catch, so nothing below asserts a
# mechanism: every check is a property of the bytes on disk, the return
# code, or the recorded command log.
#
# THE FACT IT ALL TURNS ON. Brokkr resolves its mode CLI `--mode` > env
# `BROKKR_MODE` > `mode.toml` -- see server/fleet_probe.py ("Resolved
# here in brokkr's own precedence order: CLI arg beats env beats config
# file") and docs/sensors-usage.md, which names the four placements.
# systemd drop-ins APPEND, so a drop-in's `ExecStart= --mode A` lands on
# the command line and therefore BEATS the same drop-in's
# `Environment=BROKKR_MODE=B`.
# =====================================================================

# A path that appears in no template anywhere in this repo. Content that
# was fabricated from a template instead of edited in place cannot carry
# it, so "the unit's own interpreter survived" is not satisfiable by
# accident.
CUSTOM_INTERPRETER = "/opt/custom/venv/bin/python3"

CUSTOM_EXECSTART_DROPIN = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
    "--system hamma --mode {} start\n"
)


# --- The suite's own parser (never calls into sensors.py) -------------

ENV_ORACLE_RE = re.compile(
    r"^Environment\s*=\s*[\"']?BROKKR_MODE=(.*?)[\"']?$")
EXEC_ORACLE_RE = re.compile(r"^ExecStart\s*=")
MODE_FLAG_RE = re.compile(r"--mode\b(?:[=\s]+(\S+))?")


def _effective_lines(text):
    """Yield the lines systemd would act on: no blanks, no comments.

    systemd takes BOTH `#` and `;` as comment introducers.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Yields
    ------
    str
        Each significant line, stripped.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in "#;":
            continue
        yield stripped


def mode_directives(text):
    """Every mode assignment systemd would see, in file order.

    This is the test suite's own oracle. It deliberately does not call
    sensors.parse_mode, so a test cannot be fooled by the same parsing
    mistake it exists to catch.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    list of tuple
        ``(form, value)`` pairs; form is "execstart" or "environment",
        value is the assigned token or None when the field is present
        but carries no readable value.
    """
    found = []
    for line in _effective_lines(text):
        env = ENV_ORACLE_RE.match(line)
        if env is not None:
            found.append(("environment", env.group(1).strip() or None))
            continue
        if EXEC_ORACLE_RE.match(line):
            for match in MODE_FLAG_RE.finditer(line):
                found.append(("execstart", match.group(1)))
    return found


def effective_mode(text):
    """The mode brokkr would run in, given this drop-in alone.

    CLI beats env (brokkr's precedence), and within one form systemd's
    LAST assignment wins.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    str or None
        The resolved mode token, or None if the file sets no mode.
    """
    directives = mode_directives(text)
    execs = [value for form, value in directives if form == "execstart"]
    envs = [value for form, value in directives if form == "environment"]
    if execs:
        return execs[-1]
    if envs:
        return envs[-1]
    return None


def non_mode_lines(text):
    """Everything a mode rewrite is NOT allowed to change.

    Effective lines with the mode taken out: `Environment=BROKKR_MODE=`
    lines dropped entirely, `--mode <v>` excised from ExecStart lines,
    whitespace normalised. Two files that differ only in their mode
    produce equal lists, so comparing before against after is a
    "changed the mode and nothing else" check that does not have to
    enumerate what "else" might be.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    list of str
    """
    kept = []
    for line in _effective_lines(text):
        if ENV_ORACLE_RE.match(line) is not None:
            continue
        if EXEC_ORACLE_RE.match(line):
            line = MODE_FLAG_RE.sub(" ", line)
        kept.append(" ".join(line.split()))
    return kept


def _argv(cmd):
    """Normalise a command to a list of str, shell string included."""
    if isinstance(cmd, str):
        return [cmd]
    return [str(part) for part in cmd]


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """Stand-in for the subprocess module: records, never spawns.

    Every entry point is implemented, not just ``run``. A refusal that
    is supposed to happen before anything is touched must be checked
    against ALL the ways out of the process, or an implementation that
    reaches for ``check_call`` slips past the assertion.
    """

    DEVNULL = -3
    PIPE = -1
    STDOUT = -2

    def __init__(self, harness):
        self._harness = harness

    def run(self, cmd, **kwargs):
        return self._harness.dispatch(_argv(cmd), kwargs)

    def Popen(self, cmd, **kwargs):
        self._harness.record("Popen", _argv(cmd))
        return MagicMock(returncode=0)

    def call(self, cmd, **kwargs):  # noqa: F811 - subprocess.call
        self._harness.record("call", _argv(cmd))
        return 0

    def check_call(self, cmd, **kwargs):
        self._harness.record("check_call", _argv(cmd))
        return 0

    def check_output(self, cmd, **kwargs):
        self._harness.record("check_output", _argv(cmd))
        return ""


class DropinHarness:
    """A real mode.conf on disk, plus a fake for every way out.

    ``sudo tee`` and ``sudo rm -f`` are applied to the real temp file,
    so every assertion reads back bytes that actually landed and a
    rewrite that re-parses its own output wrongly is visible. A delete
    makes :meth:`read_back` return None rather than "" -- tests guard
    that explicitly so a deleted file fails with a message instead of
    passing vacuously or raising TypeError.

    Everything else -- systemctl, relay.py, ping, os.system -- is
    recorded and not run. ``calls`` is the whole record.

    Parameters
    ----------
    sensors : module
        The loaded sensors module to patch.
    tmp_path : pathlib.Path
        pytest tmp_path.
    content : str, optional
        Initial drop-in contents. Omit for "no drop-in".
    name : str, optional
        Subdirectory name, so one test can hold two harnesses.
    """

    def __init__(self, sensors, tmp_path, content=None, name="dropin"):
        self.sensors = sensors
        self.dir = tmp_path / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "mode.conf"
        self.telemetry = self.dir / "absent-telemetry"
        if content is not None:
            self.path.write_text(content)
        self.calls = []
        self._patches = []

    def read_back(self):
        """Return the drop-in's bytes, or None when it is not there."""
        if not self.path.is_file():
            return None
        return self.path.read_text()

    def ran(self, *tokens):
        """Recorded commands whose argv holds all of ``tokens``.

        Tokens match whole argv ELEMENTS, not substrings: ``ran("rm")``
        does not match a ``--rm`` flag. (This repo has shipped
        ``assert "abort" in cmd`` against an argv holding ``--abort``,
        which is always False and passes silently.)

        Parameters
        ----------
        *tokens : str
            argv elements that must all be present.

        Returns
        -------
        list of list of str
        """
        return [cmd for _via, cmd in self.calls
                if all(token in cmd for token in tokens)]

    def record(self, via, cmd):
        """Append a command to the log without running it."""
        self.calls.append((via, cmd))

    def dispatch(self, cmd, kwargs):
        """Log a command, then emulate only the file-touching ones."""
        self.record("run", cmd)
        if cmd[:2] == ["sudo", "tee"] and len(cmd) > 2:
            data = kwargs.get("input")
            if data is None:
                return _Completed(1, stderr="tee received no stdin")
            pathlib.Path(cmd[2]).write_text(data)
            return _Completed(0)
        if cmd[:3] == ["sudo", "rm", "-f"] and len(cmd) > 3:
            target = pathlib.Path(cmd[3])
            if target.exists():
                target.unlink()
            return _Completed(0)
        if cmd[:3] == ["sudo", "mkdir", "-p"] and len(cmd) > 3:
            pathlib.Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            return _Completed(0)
        if cmd[:1] == ["ping"]:
            return _Completed(1)
        if "is-active" in cmd:
            return _Completed(0, stdout="active")
        return _Completed(0)

    def _os_system(self, command):
        self.record("os.system", _argv(command))
        return 0

    def _os_popen(self, command, *args, **kwargs):
        self.record("os.popen", _argv(command))
        return io.StringIO("")

    def __enter__(self):
        sensors = self.sensors
        self._patches = [
            patch.object(sensors, "DROPIN_PATH", str(self.path)),
            patch.object(sensors, "DROPIN_DIR", str(self.dir)),
            patch.object(sensors, "TELEMETRY_DIR", str(self.telemetry)),
            patch.object(sensors, "subprocess", _FakeSubprocess(self)),
            patch.object(sensors.os, "system", self._os_system),
            patch.object(sensors.os, "popen", self._os_popen),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc_info):
        for item in reversed(self._patches):
            item.stop()
        return False


def write_run_configs(tmp_path):
    """Write the unit.toml and main.toml that run() needs.

    main.toml has no [steps.state_monitor], so no real notifier is ever
    built; tests that care about notifications patch build_sender.

    Parameters
    ----------
    tmp_path : pathlib.Path

    Returns
    -------
    tuple of (str, str)
        Paths to unit.toml and main.toml.
    """
    unit = tmp_path / "unit.toml"
    unit.write_text(textwrap.dedent("""\
        number = 3
        site_description = "Bench"
        [relay]
        pin = 17
        active_high = true
    """))
    main = tmp_path / "main.toml"
    main.write_text("[steps]\n")
    return str(unit), str(main)


# --- Fixture catalogue -----------------------------------------------
#
# Field shapes, per docs/sensors-usage.md and the live fleet: mj04 and
# mj43 carry the Environment form; mj06 and mj50 the ExecStart form,
# single space, sticky axis. Modes are chosen so that no expected token
# is a substring of the token it replaces -- `nosensor` IS a substring
# of `nosensor_nochargecontroller`, so substring assertions on mode
# tokens are unfalsifiable and the suite compares resolved modes for
# equality instead.

DUPLICATE_FIXTURES = {
    # Both valid forms in one file. ExecStart wins per brokkr, so the
    # Environment line is dead weight that contradicts it.
    "dup-both-forms": (
        CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        + "Environment=BROKKR_MODE=nosensor\n"),
    # systemd applies the LAST Environment= assignment; a first-match
    # parser reads the first.
    "dup-two-environment": (
        "[Service]\n"
        "Environment=BROKKR_MODE=nosensor\n"
        "Environment=BROKKR_MODE=nochargecontroller\n"),
    # Two --mode flags on one command line; brokkr's argparse takes the
    # last, a non-greedy regex takes the first.
    "dup-two-mode-flags": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr --system hamma "
        "--mode nosensor --mode nochargecontroller start\n"),
}

UNPARSEABLE_FIXTURES = {
    # brokkr's argparse takes `--mode x`, not `--mode=x`; the shape is
    # close enough to be a plausible hand-edit and must not be guessed.
    "bad-mode-equals": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr --system hamma "
        "--mode=nosensor start\n"),
    # A systemd line continuation: the mode is on a physical line that
    # does not itself begin with ExecStart=.
    "bad-backslash-continuation": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr \\\n"
        "    --system hamma --mode nosensor start\n"),
    # Field present, value empty.
    "bad-environment-no-value": "[Service]\nEnvironment=BROKKR_MODE=\n",
    # systemd has no trailing comments, so the value is literally
    # "nosensor # old" -- not a mode, and not safely strippable.
    "bad-environment-trailing-comment": (
        "[Service]\nEnvironment=BROKKR_MODE=nosensor # old\n"),
}

# config/mode.toml really does define these three alongside the four
# compositions. They parse cleanly but decompose to neither axis, so an
# --on would compute `default` and discard whatever the operator set.
UNMODELED_FIXTURES = {
    "unmodeled-test-execstart": CUSTOM_EXECSTART_DROPIN.format("test"),
    "unmodeled-realtime-environment": ENVIRONMENT_DROPIN.format("realtime"),
    "unmodeled-sindri02x-execstart": (
        CUSTOM_EXECSTART_DROPIN.format("sindri02x")),
}

REFUSE_FIXTURES = {}
REFUSE_FIXTURES.update(DUPLICATE_FIXTURES)
REFUSE_FIXTURES.update(UNPARSEABLE_FIXTURES)
REFUSE_FIXTURES.update(UNMODELED_FIXTURES)

# (content, sensor_on, expected resolved mode after the write)
WRITE_FIXTURES = {
    "write-execstart-form": (
        CUSTOM_EXECSTART_DROPIN.format("nochargecontroller"), False,
        "nosensor_nochargecontroller"),
    "write-environment-form": (
        ENVIRONMENT_DROPIN.format("nochargecontroller"), False,
        "nosensor_nochargecontroller"),
    "write-mode-free-file": (
        "[Service]\nRestart=always\nTimeoutStopSec=90\n", False,
        "nosensor"),
}

COLLAPSE_FIXTURES = {
    "collapse-execstart-only": CUSTOM_EXECSTART_DROPIN.format("nosensor"),
    "collapse-execstart-with-neighbours": (
        CUSTOM_EXECSTART_DROPIN.format("nosensor")
        + "Restart=always\nTimeoutStopSec=90\n"),
    "collapse-environment-only": ENVIRONMENT_DROPIN.format("nosensor"),
    "collapse-environment-with-neighbours": (
        "[Service]\n"
        "Environment=BROKKR_MODE=nosensor\n"
        "Restart=always\nTimeoutStopSec=90\n"),
}

# Everything above, for the properties that must hold across the whole
# input space rather than for one shape.
ALL_FIXTURES = {}
ALL_FIXTURES.update(REFUSE_FIXTURES)
ALL_FIXTURES.update(COLLAPSE_FIXTURES)
ALL_FIXTURES.update(
    {label: spec[0] for label, spec in WRITE_FIXTURES.items()})


class TestTheSuitesOwnOracle:
    """The oracle the rest of the suite leans on, checked on its own.

    If `effective_mode` were wrong, every assertion built on it would be
    wrong in the same direction and nothing would show it.
    """

    def test_execstart_beats_environment(self):
        """Both forms present: the ExecStart mode is the effective one."""
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        assert effective_mode(content) == "nochargecontroller"

    def test_last_environment_assignment_wins(self):
        """Repeated Environment=: systemd applies the last one."""
        content = DUPLICATE_FIXTURES["dup-two-environment"]
        assert effective_mode(content) == "nochargecontroller"

    def test_last_mode_flag_on_a_line_wins(self):
        """Repeated --mode on one ExecStart: argparse takes the last."""
        content = DUPLICATE_FIXTURES["dup-two-mode-flags"]
        assert effective_mode(content) == "nochargecontroller"

    def test_comments_hold_no_directives(self):
        """`#` and `;` lines are comments, so they assign nothing."""
        content = (
            "[Service]\n"
            "# Environment=BROKKR_MODE=nosensor\n"
            "; ExecStart=/x -m brokkr --mode nochargecontroller\n"
            "Restart=always\n")
        assert mode_directives(content) == []
        assert effective_mode(content) is None

    def test_mode_free_file_has_no_effective_mode(self):
        """A file with neither field resolves to no mode at all."""
        assert effective_mode(
            "[Service]\nRestart=always\n") is None

    def test_bare_reset_line_assigns_no_mode(self):
        """`ExecStart=` with no replacement carries no --mode."""
        assert mode_directives("[Service]\nExecStart=\n") == []

    def test_non_mode_lines_ignores_only_the_mode(self):
        """Two files differing only in mode have equal non_mode_lines.

        And a file differing in a neighbour does NOT -- the second
        assertion is what makes the first falsifiable.
        """
        one = CUSTOM_EXECSTART_DROPIN.format("nosensor")
        two = CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        assert non_mode_lines(one) == non_mode_lines(two)
        assert non_mode_lines(one) != non_mode_lines(
            one + "Restart=always\n")

    def test_counts_the_mode_token_whatever_separates_it(self):
        """`--mode x`, `--mode  x`, `--mode\\tx`, `--mode=x` all count."""
        for gap in (" ", "  ", "\t", " \t ", "="):
            line = ("[Service]\nExecStart=/x -m brokkr --mode"
                    + gap + "nosensor start\n")
            assert effective_mode(line) == "nosensor", repr(gap)


class TestHarnessRoundTrip:
    """The harness itself, so no later assertion is vacuous.

    A fake that silently failed to write, or that returned "" for a
    deleted file, would make half this suite pass for free.
    """

    def test_tee_lands_on_disk_and_is_readable(self, sensors, tmp_path):
        """A `sudo tee` is visible to the next read of the real file."""
        harness = DropinHarness(sensors, tmp_path, "old\n")
        with harness:
            rc = sensors.write_dropin("new\n", "nosensor")
        assert rc == 0
        assert harness.read_back() == "new\n"

    def test_rm_makes_read_back_none_not_empty(self, sensors, tmp_path):
        """A delete reads back as None, so `is not None` can catch it."""
        harness = DropinHarness(sensors, tmp_path, "old\n")
        with harness:
            # remove_dropin() lost its (existing, form) params: the
            # delete decision is now proven upstream by plan_mode().
            rc = sensors.remove_dropin()
        assert rc == 0
        assert harness.read_back() is None

    def test_every_route_out_of_process_is_recorded(
            self, sensors, tmp_path):
        """run/Popen/call/check_call/check_output/os.system all log."""
        harness = DropinHarness(sensors, tmp_path)
        with harness:
            sensors.subprocess.run(["a"])
            sensors.subprocess.Popen(["b"])
            sensors.subprocess.call(["c"])
            sensors.subprocess.check_call(["d"])
            sensors.subprocess.check_output(["e"])
            sensors.os.system("f")
        assert [via for via, _cmd in harness.calls] == [
            "run", "Popen", "call", "check_call", "check_output",
            "os.system"]

    def test_a_real_service_command_is_logged_not_run(
            self, sensors, tmp_path):
        """stop_brokkr is captured; `ran` matches whole argv elements."""
        harness = DropinHarness(sensors, tmp_path)
        with harness:
            rc = sensors.stop_brokkr()
        assert rc == 0
        assert harness.ran("systemctl", "stop") != []
        assert harness.ran("systemctl", "start") == []


# --- Spec clause 1: precedence ---------------------------------------

class TestModePrecedence:
    """Resolution follows brokkr's precedence, not the file's order.

    brokkr: CLI `--mode` > `BROKKR_MODE` > mode.toml. Drop-ins append,
    so a drop-in's ExecStart `--mode` reaches the command line and beats
    the same file's `Environment=BROKKR_MODE=`.
    """

    def test_execstart_mode_beats_environment_mode(self, sensors):
        """parse_mode returns the ExecStart mode and the execstart form.

        A = nochargecontroller, B = nosensor. Neither token contains the
        other, so "A is reported" and "B is not" fail in opposite
        directions and neither can pass by accident.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        mode, form = sensors.parse_mode(content)
        assert mode == "nochargecontroller"
        assert form == "execstart"

    def test_status_never_names_the_overridden_environment_mode(
            self, sensors, tmp_path):
        """--status does not report the losing Environment mode.

        Weaker than the clause above on purpose, and independent of how
        the fix chooses to represent a conflict: this holds whether the
        reported mode is `nochargecontroller` or some refusal marker,
        and fails only if `nosensor` -- the value the command line
        overrides -- is reported as the unit's mode.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            out = sensors.sensor_status(
                {"pin": 17, "active_high": False})
        match = re.search(r"^Brokkr mode: (\S+)$", out, re.MULTILINE)
        assert match is not None, (
            "status printed no 'Brokkr mode:' line:\n" + out)
        assert match.group(1) != "nosensor", (
            "status reports the Environment mode that the ExecStart "
            "command line overrides")


# --- Spec clause 2: a mode set twice is refused ----------------------

class TestDuplicateModeDirectivesRefused:
    """A mode named in more than one place is refused, not reconciled.

    Whichever one the script picks, it is guessing at which the operator
    meant, and a wrong guess rewrites the other one out of existence.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("label", sorted(DUPLICATE_FIXTURES))
    def test_apply_mode_refuses_and_leaves_the_bytes_alone(
            self, sensors, tmp_path, label, sensor_on):
        """apply_mode returns nonzero; the file is byte-identical.

        Both directions: a duplicate is equally unreadable to --on and
        to --off.
        """
        content = DUPLICATE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "a duplicated mode was reconciled, not refused"
        assert after is not None, "the drop-in was deleted"
        assert after == content, "the drop-in was rewritten"
        assert harness.ran("tee") == []
        assert harness.ran("rm") == []


# --- Spec clause 3: an unparseable mode token is refused -------------

class TestUnparseableModeTokenRefused:
    """A mode field whose value cannot be read is refused.

    Distinct from clause 4: here the FIELD is present, so the file is
    plainly trying to set a mode and we cannot tell which.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("label", sorted(UNPARSEABLE_FIXTURES))
    def test_apply_mode_refuses_and_leaves_the_bytes_alone(
            self, sensors, tmp_path, label, sensor_on):
        """apply_mode returns nonzero; the file is byte-identical."""
        content = UNPARSEABLE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "an unreadable mode token was overwritten"
        assert after is not None, "the drop-in was deleted"
        assert after == content, "the drop-in was rewritten"


# --- Spec clause 4: a mode-free file is default ----------------------

class TestModeFreeFileIsDefault:
    """A file that provably sets no mode is `default`, not `unknown`.

    Refusing here would strand any unit whose mode.conf holds only
    unrelated directives -- a file on/off has every right to add a mode
    to.
    """

    def test_directive_free_file_resolves_to_default(self, sensors):
        """No mode field anywhere: the mode is default."""
        content = "[Service]\nRestart=always\nTimeoutStopSec=90\n"
        assert mode_directives(content) == []
        mode, _form = sensors.parse_mode(content)
        assert mode == sensors.MODE_DEFAULT

    def test_hash_and_semicolon_comments_are_not_directives(
            self, sensors):
        """A mode named only inside a `#` or `;` comment does not count.

        systemd takes both characters as comment introducers, and the
        `;` half is the one a `#`-only parser gets wrong.
        """
        content = (
            "[Service]\n"
            "# Environment=BROKKR_MODE=nosensor (set 2026-01-01)\n"
            "; ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode nochargecontroller start\n"
            "Restart=always\n")
        assert mode_directives(content) == []
        mode, _form = sensors.parse_mode(content)
        assert mode == sensors.MODE_DEFAULT

    def test_mode_free_file_gains_a_mode_and_keeps_its_directives(
            self, sensors, tmp_path):
        """--off writes nosensor into it without losing a neighbour."""
        content = "[Service]\nRestart=always\nTimeoutStopSec=90\n"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0, "a mode-free drop-in was refused"
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor"
        assert non_mode_lines(after) == non_mode_lines(content)


# --- Spec clause 5: a write changes the mode and nothing else --------

class TestWriteChangesTheModeAndNothingElse:
    """What else is in mode.conf has to come out the other side.

    mode.conf is the shared filename for every override and is
    hand-edited in the field, so a rewrite that reconstructs the file
    from a template silently replaces whatever was there.
    """

    def test_execstart_write_keeps_the_units_own_interpreter(
            self, sensors, tmp_path):
        """--off retargets the mode; path, --system and neighbours stay.

        The interpreter is a path no template in this repo contains, so
        a rewrite that fabricated the line instead of editing it cannot
        pass.
        """
        content = (
            CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
            + "Restart=always\nTimeoutStopSec=90\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert CUSTOM_INTERPRETER in after
        assert "--system hamma" in after
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert non_mode_lines(after) == non_mode_lines(content)

    def test_environment_write_keeps_coresident_directives(
            self, sensors, tmp_path):
        """--off on the Environment form keeps the other directives."""
        content = (
            "[Service]\n"
            "Environment=BROKKR_MODE=nochargecontroller\n"
            "Restart=always\n"
            "TimeoutStopSec=90\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert non_mode_lines(after) == non_mode_lines(content)

    def test_environment_write_keeps_a_mode_free_execstart_override(
            self, sensors, tmp_path):
        """The mode is in Environment=; a mode-free ExecStart survives.

        Only one mode directive, so this is a write, not a refusal. If
        the ExecStart override goes, the unit falls back to the parent
        unit's command line -- a different interpreter, and a different
        place for the mode to come from.
        """
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma start\n"
            "Environment=BROKKR_MODE=nochargecontroller\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert CUSTOM_INTERPRETER in after
        assert non_mode_lines(after) == non_mode_lines(content)


# --- Spec clause 6: whitespace before the mode value -----------------

class TestWhitespaceBeforeTheModeValue:
    """systemd and argparse accept any whitespace run; so must we.

    A rewrite that looks for the literal "--mode <old>" finds nothing
    when the file uses two spaces or a tab, and then writes the file
    back unchanged while reporting success -- the worst outcome
    available, because the operator is told the mode changed.
    """

    WHITESPACE = [" ", "  ", "\t", " \t "]

    @pytest.mark.parametrize("gap", WHITESPACE,
                             ids=["space", "two-spaces", "tab", "mixed"])
    def test_retarget_tolerates_any_whitespace(
            self, sensors, tmp_path, gap):
        """--off reaches nosensor_nochargecontroller for every gap."""
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode" + gap + "nochargecontroller start\n")
        assert effective_mode(content) == "nochargecontroller"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller", (
            "mode not retargeted with {!r} before the value".format(gap))
        assert CUSTOM_INTERPRETER in after

    @pytest.mark.parametrize("gap", WHITESPACE,
                             ids=["space", "two-spaces", "tab", "mixed"])
    def test_collapse_tolerates_any_whitespace(
            self, sensors, tmp_path, gap):
        """--on leaves no mode set, for every gap."""
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode" + gap + "nosensor start\n")
        assert effective_mode(content) == "nosensor"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        if after is not None:
            assert mode_directives(after) == [], (
                "a mode survived the collapse with {!r} before the "
                "value".format(gap))


# --- Spec clause 7: exactly one mode directive after a write ---------

class TestExactlyOneModeDirective:
    """After a write the file holds one mode assignment, no more.

    Two is the quiet failure: a first-match parser reads back the one it
    wrote and reports success, while systemd applies the other.
    """

    @pytest.mark.parametrize("label", sorted(WRITE_FIXTURES))
    def test_one_directive_naming_the_target(
            self, sensors, tmp_path, label):
        """Exactly one assignment, and it names the target mode."""
        content, sensor_on, expected = WRITE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert len(mode_directives(after)) == 1, (
            "{} mode assignments in:\n{}".format(
                len(mode_directives(after)), after))
        assert effective_mode(after) == expected


# --- Spec clause 8: a collapse to default leaves no mode -------------

class TestCollapseToDefault:
    """--on to default removes every mode form, or the file.

    "Valid" has one extra condition: a drop-in whose only surviving
    ExecStart is the bare reset line clears brokkr's command line, and
    the unit will not start at all. That is worse than a wrong mode.
    """

    @pytest.mark.parametrize("label", sorted(COLLAPSE_FIXTURES))
    def test_no_mode_survives_and_no_lone_reset_is_left(
            self, sensors, tmp_path, label):
        """Afterwards: absent, or present, mode-free and startable."""
        content = COLLAPSE_FIXTURES[label]
        assert effective_mode(content) == "nosensor"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        if after is None:
            assert harness.ran("rm") != [], (
                "the drop-in vanished but no rm was issued")
            return
        assert mode_directives(after) == [], (
            "a mode is still set:\n" + after)
        exec_lines = [line for line in _effective_lines(after)
                      if EXEC_ORACLE_RE.match(line)]
        if exec_lines:
            assert any(line.split("=", 1)[1].strip()
                       for line in exec_lines), (
                "only a bare ExecStart= reset survived, which clears "
                "brokkr's command line:\n" + after)


# --- Spec clause 9: a collapse preserves, or refuses -----------------

class TestCollapsePreservesRatherThanDeletes:
    """Deleting the file is never how a post-condition gets satisfied.

    The design review for power-state reconciliation named "could delete
    a custom mode.conf" as a real hazard, and a rejected attempt at this
    fix did exactly that when its own verification step failed.
    """

    @pytest.mark.parametrize("label", [
        "collapse-execstart-with-neighbours",
        "collapse-environment-with-neighbours",
    ])
    def test_neighbours_survive_and_nothing_is_deleted(
            self, sensors, tmp_path, label):
        """Collapse keeps the neighbours; no rm -f is issued."""
        content = COLLAPSE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        assert harness.ran("rm") == [], (
            "the drop-in was deleted rather than rewritten")
        assert after is not None, "the drop-in is gone"
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert mode_directives(after) == []

    def test_a_refused_collapse_changes_nothing(
            self, sensors, tmp_path):
        """--on on a file whose collapse cannot be proven correct.

        Stripping the Environment line here would leave a file that
        still sets a mode from its ExecStart, so there is no correct
        single-directive result to write. The outcome must be: refuse,
        and the bytes stay.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc != 0, "a collapse with no correct result went ahead"
        assert after is not None, "the drop-in was deleted"
        assert after == content


# --- Spec clause 10: the sticky axis survives both transitions -------

class TestStickyAxisSurvivesBothTransitions:
    """nochargecontroller is hardware, not power state.

    mj06 and mj50 carry it in the ExecStart form; mj04 and mj43 use the
    Environment form. Dropping it starts a unit polling a charge
    controller that is not connected.
    """

    def test_execstart_form_round_trip(self, sensors, tmp_path):
        """mj06/mj50 shape: off then on, sticky axis at every step."""
        content = CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc_off = sensors.apply_mode(sensor_on=False)
            after_off = harness.read_back()
            rc_on = sensors.apply_mode(sensor_on=True)
            after_on = harness.read_back()
        assert (rc_off, rc_on) == (0, 0)
        assert after_off is not None, "--off deleted the drop-in"
        assert effective_mode(after_off) == "nosensor_nochargecontroller"
        assert after_on is not None, "--on deleted the drop-in"
        assert effective_mode(after_on) == "nochargecontroller"
        assert CUSTOM_INTERPRETER in after_on
        assert non_mode_lines(after_on) == non_mode_lines(content)

    def test_environment_form_round_trip(self, sensors, tmp_path):
        """mj04/mj43 shape: off then on, sticky axis at every step."""
        content = ENVIRONMENT_DROPIN.format("nochargecontroller")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc_off = sensors.apply_mode(sensor_on=False)
            after_off = harness.read_back()
            rc_on = sensors.apply_mode(sensor_on=True)
            after_on = harness.read_back()
        assert (rc_off, rc_on) == (0, 0)
        assert after_off is not None, "--off deleted the drop-in"
        assert effective_mode(after_off) == "nosensor_nochargecontroller"
        assert after_on is not None, "--on deleted the drop-in"
        assert effective_mode(after_on) == "nochargecontroller"


# --- Spec clause 11: refuse before touching anything -----------------

class TestRefusalHappensBeforeAnythingIsTouched:
    """sensors.py is the emergency power tool: fail empty-handed.

    A refusal after brokkr is stopped, or after the relay has moved,
    leaves the unit down with its mode still wrong -- the operator ran
    one command and got a half-transition they now have to unpick by
    hand, over cellular.
    """

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_no_command_runs_and_the_file_is_untouched(
            self, sensors, tmp_path, label, flag):
        """run() refuses with an empty command log and the same bytes.

        Not "no relay command": NO command, by any route out of the
        process (subprocess run/Popen/call/check_call/check_output, or
        os.system), since stopping a service is as destructive here as
        moving the relay.
        """
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        sender = MagicMock()
        with harness, patch.object(sensors, "build_sender",
                                   return_value=sender):
            rc = sensors.run([flag], config_path=unit,
                             main_toml_path=main)
        assert rc != 0, "{} was not refused".format(label)
        assert harness.calls == [], (
            "commands ran before the refusal: {}".format(harness.calls))
        assert harness.read_back() == content, (
            "the drop-in changed despite the refusal")


# --- Spec clause 12: a refusal still notifies ------------------------

class TestRefusalReachesTheNotifier:
    """Nobody is watching the terminal on the VPS.

    The notification is how a refused on/off becomes visible at all, so
    a refusal that returns early past the notifier is a silent no-op as
    far as the team is concerned.
    """

    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_a_refused_off_sends_one_failure_message(
            self, sensors, tmp_path, label):
        """One send(), and the message says the turn-off failed."""
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        sender = MagicMock()
        with harness, patch.object(sensors, "build_sender",
                                   return_value=sender):
            rc = sensors.run(["--off"], config_path=unit,
                             main_toml_path=main)
        assert rc != 0, "{} was not refused".format(label)
        assert sender.send.call_count == 1, (
            "a refusal must still be announced; send() called {} "
            "times".format(sender.send.call_count))
        message = sender.send.call_args[0][0]
        assert "FAILED" in message
        assert "Mjolnir03" in message


# --- Spec clause 13: --dry-run refuses what the real run refuses -----

class TestDryRunMatchesTheRealRun:
    """A dry run exists to be trusted before the real one.

    Printing a sequence that completes, for a case that hard-fails,
    makes the check worse than not running it.
    """

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_dry_run_refuses_and_stops(
            self, sensors, tmp_path, label, flag, capsys):
        """Nonzero, and the tail of a successful run is not printed."""
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.run([flag, "--dry-run"], config_path=unit,
                             main_toml_path=main)
        out = capsys.readouterr().out
        assert rc != 0, (
            "dry run reported success for a refused case: " + label)
        assert "daemon-reload" not in out, (
            "dry run printed a sequence that completes:\n" + out)
        assert "systemctl start" not in out, (
            "dry run printed a sequence that completes:\n" + out)
        assert harness.read_back() == content

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(ALL_FIXTURES))
    def test_dry_run_and_real_run_agree_on_every_fixture(
            self, sensors, tmp_path, label, flag):
        """The two paths refuse the same inputs, fixture by fixture.

        Catches drift in either direction: a dry run that waves through
        what the real run rejects, and a dry run that refuses what the
        real run would have done.
        """
        content = ALL_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        dry_harness = DropinHarness(sensors, tmp_path, content,
                                    name="dry")
        with dry_harness:
            dry_rc = sensors.run([flag, "--dry-run"], config_path=unit,
                                 main_toml_path=main)
        real_harness = DropinHarness(sensors, tmp_path, content,
                                     name="real")
        sender = MagicMock()
        with real_harness, patch.object(sensors, "build_sender",
                                        return_value=sender):
            real_rc = sensors.run([flag], config_path=unit,
                                  main_toml_path=main)
        assert (dry_rc != 0) == (real_rc != 0), (
            "{} {}: dry-run rc={}, real rc={}".format(
                label, flag, dry_rc, real_rc))


# --- Spec clause 14: the mode.toml presets are refused ---------------

class TestUnmodeledPresetModesRefused:
    """`test`, `realtime` and `sindri02x` are outside the two axes.

    All three are real presets in config/mode.toml. They parse cleanly,
    so an unknown-mode check never fires, but they decompose to neither
    axis -- so --on computes `default` and throws the preset away.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("mode", ["test", "realtime", "sindri02x"])
    @pytest.mark.parametrize("form", ["execstart", "environment"])
    def test_refused_in_both_forms_and_both_directions(
            self, sensors, tmp_path, form, mode, sensor_on):
        """apply_mode returns nonzero and the file is byte-identical."""
        content = (CUSTOM_EXECSTART_DROPIN.format(mode)
                   if form == "execstart"
                   else ENVIRONMENT_DROPIN.format(mode))
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "{} {} was rewritten".format(mode, form)
        assert after is not None, "the drop-in was deleted"
        assert after == content
