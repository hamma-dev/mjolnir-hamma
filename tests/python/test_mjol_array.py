"""Tests for server/mjol_array.py — array status and control."""

import importlib.util
import pathlib
import subprocess

import pytest
from unittest.mock import patch, MagicMock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "server" / "mjol_array.py"


@pytest.fixture
def mjol():
    """Provide the mjol_array module.

    pandas and numpy are imported lazily inside collect_data() and
    status_latest_trigger(); mock them globally for the duration of the
    test so those lazy imports resolve to mocks too.
    """
    import sys

    mock_pd = MagicMock()
    mock_np = MagicMock()
    orig_pd = sys.modules.get('pandas')
    orig_np = sys.modules.get('numpy')
    sys.modules['pandas'] = mock_pd
    sys.modules['numpy'] = mock_np
    try:
        spec = importlib.util.spec_from_file_location(
            "mjol_array", str(SCRIPT_PATH),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if orig_pd is not None:
            sys.modules['pandas'] = orig_pd
        else:
            sys.modules.pop('pandas', None)
        if orig_np is not None:
            sys.modules['numpy'] = orig_np
        else:
            sys.modules.pop('numpy', None)


class TestUpdown:
    """Tests for MjolnirArray.updown()."""

    def test_updown_bring_up_calls_sensors_on(self, mjol):
        """bring_up=True should call sensors.py --on (no inversion)."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.updown(10001, bring_up=True)

        cmd = mock_sub.run.call_args[0][0]
        assert '/home/pi/dev/mjolnir-hamma/scripts/sensors.py' in cmd
        assert '--on' in cmd
        assert '--off' not in cmd

    def test_updown_bring_down_calls_sensors_off(self, mjol):
        """bring_up=False should call sensors.py --off."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.updown(10001, bring_up=False)

        cmd = mock_sub.run.call_args[0][0]
        assert '/home/pi/dev/mjolnir-hamma/scripts/sensors.py' in cmd
        assert '--off' in cmd
        assert '--on' not in cmd

    def test_updown_no_hardcoded_pin(self, mjol):
        """Command should NOT contain --pin (sensors.py reads from config)."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.updown(10001, bring_up=True)

        cmd = mock_sub.run.call_args[0][0]
        assert '--pin' not in cmd

    def test_updown_has_timeout(self, mjol):
        """subprocess.run should be called with timeout=120."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.updown(10001, bring_up=True)

        kwargs = mock_sub.run.call_args[1]
        assert kwargs.get('timeout') == 120

    def test_updown_pi_down_skips(self, mjol):
        """If Pi is down, updown should return without calling subprocess."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            with patch.object(mjol.MjolnirArray, 'status', return_value=False):
                mjol.MjolnirArray.updown(10001, bring_up=True, quiet=True)

        mock_sub.run.assert_not_called()

    def test_updown_timeout_catches_exception(self, mjol):
        """On timeout, updown should catch the exception and not raise."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=120)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                # Should not raise
                mjol.MjolnirArray.updown(10001, bring_up=True, quiet=True)


class TestStatusLatestTrigger:
    """Tests for MjolnirArray.status_latest_trigger()."""

    def test_calls_script_directly_on_pi(self, mjol):
        """Should call latest_trigger.py directly, not via stdin piping."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout="{'threshold': 0.5, 'num_sat': 8, 'time': 1234567890}",
            )
            mjol.MjolnirArray.status_latest_trigger(10001)

        cmd = mock_sub.run.call_args[0][0]
        assert '/home/pi/dev/mjolnir-hamma/scripts/latest_trigger.py' in cmd
        kwargs = mock_sub.run.call_args[1]
        assert 'stdin' not in kwargs or kwargs['stdin'] is None

    def test_no_local_file_reference(self, mjol):
        """Should not reference /home/monitor/latest_trigger.py."""
        import inspect
        source = inspect.getsource(mjol.MjolnirArray.status_latest_trigger)
        assert '/home/monitor/' not in source

    def test_returns_dict_on_success(self, mjol):
        """On success, returns dict with threshold, num_sat, time."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout="{'threshold': 0.5, 'num_sat': 8, 'time': 1234567890}",
            )
            result = mjol.MjolnirArray.status_latest_trigger(10001)

        assert 'threshold' in result
        assert 'num_sat' in result
        assert 'time' in result

    def test_returns_nan_dict_on_failure(self, mjol):
        """On failure, returns dict with nan values (keys present)."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=1, stdout="")
            result = mjol.MjolnirArray.status_latest_trigger(10001)

        assert 'threshold' in result
        assert 'num_sat' in result
        assert 'time' in result

    def test_works_without_numpy(self, mjol):
        """Must not require numpy: the operator runs `mjol_array --status`
        under the system python (/usr/bin/python), which has no numpy. Only
        the brokkr plugin runs under the ltgenv python that has numpy.
        """
        import sys
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout="{'threshold': 0.5, 'num_sat': 8, 'time': 1234567890}",
            )
            # Make `import numpy` raise ImportError, exactly as on the VPS
            # system python, for the duration of the call.
            with patch.dict(sys.modules, {'numpy': None}):
                result = mjol.MjolnirArray.status_latest_trigger(10001)

        assert result['threshold'] == 0.5
        assert result['num_sat'] == 8
        assert result['time'] is not None

    def test_failure_path_works_without_numpy(self, mjol):
        """The nan fallback path must also not reference numpy."""
        import sys
        import math
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=1, stdout="")
            with patch.dict(sys.modules, {'numpy': None}):
                result = mjol.MjolnirArray.status_latest_trigger(10001)

        assert math.isnan(result['threshold'])
        assert math.isnan(result['num_sat'])


class TestPiSshCmd:
    """Tests for MjolnirArray._pi_ssh_cmd()."""

    def test_returns_list(self, mjol):
        cmd = mjol.MjolnirArray._pi_ssh_cmd(10001)
        assert isinstance(cmd, list)

    def test_contains_ssh(self, mjol):
        cmd = mjol.MjolnirArray._pi_ssh_cmd(10001)
        assert cmd[0] == 'ssh'

    def test_contains_port(self, mjol):
        cmd = mjol.MjolnirArray._pi_ssh_cmd(10005)
        assert '10005' in cmd

    def test_contains_connect_timeout(self, mjol):
        cmd = mjol.MjolnirArray._pi_ssh_cmd(10001)
        assert 'ConnectTimeout=5' in cmd

    def test_contains_pi_user(self, mjol):
        cmd = mjol.MjolnirArray._pi_ssh_cmd(10001)
        assert 'pi@localhost' in cmd


class TestArgparse:
    """Tests for array constant definitions."""

    def test_hamma_sensors(self, mjol):
        assert mjol.HAMMA_SENSORS == list(range(1, 10))

    def test_pamma_sensors(self, mjol):
        assert mjol.PAMMA_SENSORS == [50, 51, 52, 53, 54, 56]

    def test_aumma_sensors(self, mjol):
        assert mjol.AUMMA_SENSORS == [41, 42, 43]


class TestCliValidation:
    def test_bad_threshold_channel_rejected(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "9", "830"])
        mock_arr.assert_not_called()
        assert "[ERROR]" in capsys.readouterr().out

    def test_injection_attempt_rejected(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "1", "8; rm -rf /"])
        mock_arr.assert_not_called()
        assert "[ERROR]" in capsys.readouterr().out

    def test_bad_gain_level_rejected(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_gain_array") as mock_arr:
            mjol.main(["-p", "2", "--set-gain", "fast-e", "9"])
        mock_arr.assert_not_called()
        assert "[ERROR]" in capsys.readouterr().out

    def test_valid_threshold_still_dispatches(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "1", "830"])
        mock_arr.assert_called_once()


class TestStatusServices:
    """Tests for MjolnirArray.status_services()."""

    def test_returns_two_booleans(self, mjol):
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = mjol.MjolnirArray.status_services(10001)
        assert len(result) == 2
        assert all(isinstance(v, bool) for v in result)

    def test_both_active(self, mjol):
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = mjol.MjolnirArray.status_services(10001)
        assert result == [True, True]

    def test_both_inactive(self, mjol):
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=1)
            result = mjol.MjolnirArray.status_services(10001)
        assert result == [False, False]

    def test_checks_correct_services(self, mjol):
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mjol.MjolnirArray.status_services(10001)
        calls = mock_sub.run.call_args_list
        service_names = [c[0][0][-1] for c in calls]
        assert 'brokkr-hamma-default' in service_names
        assert 'sindri-hamma-client' in service_names


class TestTrigger:
    """Tests for MjolnirArray.trigger()."""

    def test_trigger_calls_ags_manual_trigger(self, mjol):
        """Default trigger should run ags.py with das_manual_trigger."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.trigger(10002)

        cmd = mock_sub.run.call_args[0][0]
        assert '/home/pi/dev/mjolnir-hamma/scripts/ags.py' in cmd
        assert 'das_manual_trigger' in cmd

    def test_trigger_ssh_target_and_port(self, mjol):
        """Trigger should SSH to the unit's tunnel (pi@localhost, port)."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.trigger(10005)

        cmd = mock_sub.run.call_args[0][0]
        assert 'pi@localhost' in cmd
        assert '10005' in cmd

    def test_trigger_custom_command(self, mjol):
        """A custom AGS command should be forwarded verbatim."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.trigger(10002, command="help")

        cmd = mock_sub.run.call_args[0][0]
        assert 'help' in cmd
        assert 'das_manual_trigger' not in cmd

    def test_trigger_has_timeout(self, mjol):
        """subprocess.run should be called with timeout=30."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                mjol.MjolnirArray.trigger(10002)

        kwargs = mock_sub.run.call_args[1]
        assert kwargs.get('timeout') == 30

    def test_trigger_pi_down_skips(self, mjol, capsys):
        """If the tunnel is down, trigger should not call subprocess."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            with patch.object(mjol.MjolnirArray, 'status', return_value=False):
                mjol.MjolnirArray.trigger(10002)

        mock_sub.run.assert_not_called()
        assert "tunnel down, sending AGS 'das_manual_trigger' not sent." in capsys.readouterr().out

    def test_trigger_pi_down_skips_quiet(self, mjol, capsys):
        """With quiet=True, a down tunnel skips silently (no subprocess, no print)."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            with patch.object(mjol.MjolnirArray, 'status', return_value=False):
                mjol.MjolnirArray.trigger(10002, quiet=True)

        mock_sub.run.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_trigger_timeout_catches_exception(self, mjol):
        """On timeout, trigger should catch the exception and not raise."""
        with patch.object(mjol, 'subprocess') as mock_sub:
            mock_sub.run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=30)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            with patch.object(mjol.MjolnirArray, 'status', return_value=True):
                # Should not raise
                mjol.MjolnirArray.trigger(10002, quiet=True)

    def test_trigger_array_iterates_ports(self, mjol):
        """trigger_array should call trigger once per port (10000 + unit)."""
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, 'trigger') as mock_trigger:
            arr.trigger_array(ports=[2, 3])

        called_ports = [c[0][0] for c in mock_trigger.call_args_list]
        assert called_ports == [10002, 10003]


class TestRunAgsCommand:
    def test_skips_when_tunnel_down(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "status", return_value=False):
            with patch.object(mjol, "subprocess") as mock_sub:
                mjol.MjolnirArray._run_ags_command(10002, ["das_reset"], "x")
                mock_sub.run.assert_not_called()
        assert "[SKIP]" in capsys.readouterr().out

    def test_runs_ags_with_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"OK", stderr=b"")
                mock_sub.TimeoutExpired = Exception
                mjol.MjolnirArray._run_ags_command(
                    10002, ["set-threshold", "1", "830"], "set thr")
        cmd = mock_sub.run.call_args[0][0]
        assert "/home/pi/dev/mjolnir-hamma/scripts/ags.py" in cmd
        assert cmd[-3:] == ["set-threshold", "1", "830"]

    def test_trigger_still_invokes_ags_command(self, mjol):
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"OK", stderr=b"")
                mock_sub.TimeoutExpired = Exception
                mjol.MjolnirArray.trigger(10002)
        cmd = mock_sub.run.call_args[0][0]
        assert "/home/pi/dev/mjolnir-hamma/scripts/ags.py" in cmd
        assert "das_manual_trigger" in cmd

    def test_timeout_prints_fail(self, mjol, capsys):
        import subprocess as _sp
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.TimeoutExpired = _sp.TimeoutExpired
                mock_sub.run.side_effect = _sp.TimeoutExpired(cmd="ssh", timeout=30)
                mjol.MjolnirArray._run_ags_command(10002, ["das_reset"], "x")
        assert "[FAIL]" in capsys.readouterr().out

    def test_exception_prints_fail(self, mjol, capsys):
        import subprocess as _sp
        with patch.object(mjol.MjolnirArray, "status", return_value=True):
            with patch.object(mjol, "subprocess") as mock_sub:
                mock_sub.TimeoutExpired = _sp.TimeoutExpired
                mock_sub.run.side_effect = RuntimeError("boom")
                mjol.MjolnirArray._run_ags_command(10002, ["das_reset"], "x")
        assert "[FAIL]" in capsys.readouterr().out


class TestSetThresholdGain:
    def test_set_threshold_builds_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_threshold(10002, 1, 830)
        port, ags_args = mock_run.call_args[0][0], mock_run.call_args[0][1]
        assert port == 10002
        assert ags_args == ["set-threshold", "1", "830"]

    def test_set_threshold_persist_appends_flag(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_threshold(10002, 1, 830, persist=True)
        assert "--persist" in mock_run.call_args[0][1]

    def test_set_gain_builds_args(self, mjol):
        with patch.object(mjol.MjolnirArray, "_run_ags_command") as mock_run:
            mjol.MjolnirArray.set_gain(10002, "fast-e", 2)
        assert mock_run.call_args[0][1] == ["set-gain", "fast-e", "2"]

    def test_set_threshold_array_fans_out(self, mjol):
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, "set_threshold",
                         return_value=(True, "")) as mock_set:
            arr.set_threshold_array(channel=1, millivolts=830)
        called_ports = [c[0][0] for c in mock_set.call_args_list]
        assert called_ports == [10002, 10003]

    def test_set_gain_array_explicit_ports(self, mjol):
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, "set_gain",
                         return_value=(True, "")) as mock_set:
            arr.set_gain_array(ports=["2"], channel="slow-e", level=0)
        assert mock_set.call_args_list[0][0][0] == 10002


class TestCliDispatch:
    def test_set_threshold_cli(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "1", "830"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["channel"] == "1" and kwargs["millivolts"] == "830"
        assert kwargs["persist"] is False

    def test_set_gain_cli_with_persist(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_gain_array") as mock_arr:
            mjol.main(["-a", "hamma", "--set-gain", "fast-e", "2", "--persist"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["channel"] == "fast-e" and kwargs["level"] == "2"
        assert kwargs["persist"] is True


# ==================================================================
# HAM-189 write-hook -- mjol_array records successful changes in the
# fleet-state snapshot fleet_probe.py maintains, so an intentional change
# leaves no diff for the next probe run.
#
# fleet_probe.py's own CSV logic (FIELDS/read_snapshot/render/
# commit_snapshot) is reused rather than duplicated, so these tests load the
# real fleet_probe.py by path (same pattern as test_fleet_probe.py) and
# patch only its commit_snapshot() -- which shells out to git -- so nothing
# here touches git or the network.
#
# Architecture under test (post red-team fixes):
#   - _resolve_front_end_entry()/_resolve_threshold_entry()/
#     _resolve_gain_entry() are pure: given a successful control op's
#     result, they return a (unit, field, value) entry to log, or None if
#     it must not be logged (AGS-reply rejection, missing baseline row's
#     unit is NOT their job -- that is _log_field_changes_batch()'s).
#   - updown_array()/set_threshold_array()/set_gain_array() run every
#     port's control op first, collect entries, then call
#     _log_field_changes_batch() ONCE per sweep.
#   - _log_field_changes_batch() does the actual read+apply+write+commit,
#     for one or many entries.
# ==================================================================
FLEET_PROBE_PATH = REPO_ROOT / "server" / "fleet_probe.py"

# A representative successful AGS reply, matching the shape ags.py's own
# tests use (test_ags.py's TestPersistGating) -- NOT indicating rejection.
OK_REPLY = ('Use "help" command to display a list of commands.\n'
            'Set DAS Threshold 1 to 1.2048.')
# A representative firmware-rejected reply (an "Error -" line), same shape
# as ags.py's own tests.
REJECTED_REPLY = ('Use "help" command to display a list of commands.\n'
                  'Error - Invalid threshold value: 12.048')


@pytest.fixture
def fp():
    """The real fleet_probe module, loaded fresh per test."""
    spec = importlib.util.spec_from_file_location(
        "fleet_probe", str(FLEET_PROBE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_snapshot(repo_path, fp, rows):
    """Write a fleet-state snapshot at <repo_path>/state/fleet-state.csv.

    rows: {unit: {field: value, ...}}. Fields not given default to
    fp.UNKNOWN, matching a real probe run's row shape.
    """
    state_dir = repo_path / "state"
    state_dir.mkdir(exist_ok=True)
    full_rows = {}
    for unit, overrides in rows.items():
        row = {f: fp.UNKNOWN for f in fp.FIELDS}
        row["unit"] = unit
        row.update(overrides)
        full_rows[unit] = row
    (state_dir / "fleet-state.csv").write_text(fp.render(full_rows))
    return state_dir / "fleet-state.csv"


class TestLogFieldChangesBatch:
    """Unit tests for _log_field_changes_batch(), the core write-hook
    primitive -- applies and commits one or more (unit, field, value)
    entries in a single read+write+commit+push."""

    def test_updates_only_the_target_field(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off",
                                                     "brokkr_mode": "default"}})
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "committed and pushed")) as mock_commit:
            ok = mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")])
        assert ok is True
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "on"
        # Untouched fields are carried forward unchanged.
        assert updated["mjolnir02"]["brokkr_mode"] == "default"

    def test_empty_entries_is_a_noop_success(self, mjol, tmp_path):
        """No entries -- e.g. every op in the sweep failed -- must not
        attempt to read/write/commit anything."""
        with patch.object(mjol, "_fleet_probe_module") as mock_fp:
            ok = mjol._log_field_changes_batch(str(tmp_path), [])
        assert ok is True
        mock_fp.assert_not_called()

    def test_no_repo_configured_is_reported_and_skipped(self, mjol, capsys):
        ok = mjol._log_field_changes_batch(
            None, [("mjolnir02", "front_end", "on")])
        assert ok is False
        assert "no --log-repo" in capsys.readouterr().err

    def test_missing_baseline_row_is_not_invented(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {})   # no rows at all yet
        with patch.object(mjol, "_fleet_probe_module", return_value=fp):
            ok = mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")])
        assert ok is True   # nothing applied is not a failure
        # Nothing was fabricated for the unit.
        assert fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv")) == {}

    def test_value_already_current_is_a_noop_success(self, mjol, fp, tmp_path):
        """Nothing to commit is not a failure -- and must not attempt one."""
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "on"}})
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot") as mock_commit:
            ok = mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")])
        assert ok is True
        mock_commit.assert_not_called()

    def test_commit_failure_is_reported_and_returns_false(self, mjol, fp,
                                                          tmp_path, capsys):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(False, "no such remote")):
            ok = mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")])
        assert ok is False
        assert "commit/push failed" in capsys.readouterr().err

    def test_unavailable_fleet_probe_module_is_reported_not_raised(
            self, mjol, capsys):
        with patch.object(mjol, "_fleet_probe_module", return_value=None):
            ok = mjol._log_field_changes_batch(
                "/some/repo", [("mjolnir02", "front_end", "on")])
        assert ok is False
        assert "could not load fleet_probe.py" in capsys.readouterr().err

    def test_snapshot_write_exception_is_swallowed(self, mjol, fp, tmp_path,
                                                    capsys):
        """A write-time error (disk full, permissions, ...) must not raise --
        this is the safety property requirement #5 is built on."""
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "render", side_effect=OSError("disk full")):
            ok = mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")])
        assert ok is False
        assert "failed to record" in capsys.readouterr().err

    def test_reason_lands_in_commit_message_not_the_csv(self, mjol, fp,
                                                        tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            mjol._log_field_changes_batch(
                str(tmp_path), [("mjolnir02", "front_end", "on")],
                reason="bench test, HAM-999")
        message = mock_commit.call_args[0][2]
        assert "bench test, HAM-999" in message
        # The snapshot header must not have grown a reason/free-text column
        # -- that would make every row differ and break write-if-changed.
        header = (tmp_path / "state" / "fleet-state.csv").read_text().splitlines()[0]
        assert header == ",".join(fp.FIELDS)

    def test_multiple_entries_produce_one_commit(self, mjol, fp, tmp_path):
        """HAM-189 finding #2: a multi-unit sweep must produce a single
        commit+push for the whole batch, not one per entry."""
        _seed_snapshot(tmp_path, fp, {
            "mjolnir02": {"front_end": "off"},
            "mjolnir03": {"front_end": "off"},
            "mjolnir04": {"front_end": "off"},
        })
        entries = [("mjolnir02", "front_end", "on"),
                   ("mjolnir03", "front_end", "on"),
                   ("mjolnir04", "front_end", "on")]
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            ok = mjol._log_field_changes_batch(str(tmp_path), entries)
        assert ok is True
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert all(updated[u]["front_end"] == "on"
                   for u in ("mjolnir02", "mjolnir03", "mjolnir04"))

    def test_one_bad_entry_does_not_lose_the_others(self, mjol, fp, tmp_path):
        """A missing baseline row for one unit must not discard entries for
        the others -- everything applicable is still written and committed
        together."""
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        entries = [("mjolnir02", "front_end", "on"),
                   ("mjolnir99", "front_end", "on")]   # no row for mjolnir99
        with patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            ok = mjol._log_field_changes_batch(str(tmp_path), entries)
        assert ok is True
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "on"
        assert "mjolnir99" not in updated


class TestResolveEntries:
    """_resolve_front_end_entry()/_resolve_threshold_entry()/
    _resolve_gain_entry() -- pure functions that decide WHAT (if anything)
    a successful control op should log, before any I/O happens."""

    def test_front_end_entry_on(self, mjol):
        assert mjol._resolve_front_end_entry(10002, True) == (
            "mjolnir02", "front_end", "on")

    def test_front_end_entry_off(self, mjol):
        assert mjol._resolve_front_end_entry(10003, False) == (
            "mjolnir03", "front_end", "off")

    def test_threshold_roundtrip_matches_ags_py_exactly(self, mjol):
        """The value recorded must be exactly what fleet_probe's parser will
        read back out of the persisted startup file -- computed with ags.py's
        own mv_to_ags/_format_ags/ags_to_mv, not re-derived."""
        import ags as real_ags   # sibling ../scripts/ags.py, same as fleet_probe
        expected = round(
            real_ags.ags_to_mv(float(real_ags._format_ags(
                real_ags.mv_to_ags(830)))), 1)

        entry = mjol._resolve_threshold_entry(10002, "1", 830, OK_REPLY)
        assert entry == ("mjolnir02", "threshold_1_mv", str(expected))

    def test_channel_2_maps_to_threshold_2_field(self, mjol):
        entry = mjol._resolve_threshold_entry(10003, "2", 700, OK_REPLY)
        assert entry[0] == "mjolnir03"
        assert entry[1] == "threshold_2_mv"

    def test_missing_ags_module_skips_without_guessing(self, mjol, capsys):
        with patch.object(mjol, "_load_ags_module", return_value=None):
            entry = mjol._resolve_threshold_entry(10002, "1", 830, OK_REPLY)
        assert entry is None
        assert "ags.py not found" in capsys.readouterr().err

    def test_gain_maps_fast_e_and_slow_e(self, mjol):
        entry = mjol._resolve_gain_entry(10002, "fast-e", 2, OK_REPLY)
        assert entry == ("mjolnir02", "gain_fast", "2")
        entry = mjol._resolve_gain_entry(10002, "slow-e", 1, OK_REPLY)
        assert entry == ("mjolnir02", "gain_slow", "1")

    # ---- HAM-189 finding #1: AGS-reply rejection gate ----

    def test_threshold_rejected_reply_is_not_logged(self, mjol, capsys):
        """A firmware-rejected reply, even with a successful ssh round trip,
        must not produce an entry -- the value was never actually written
        to the persisted startup file."""
        entry = mjol._resolve_threshold_entry(
            10002, "1", 2000, REJECTED_REPLY)
        assert entry is None
        assert "REJECTED" in capsys.readouterr().err

    def test_gain_rejected_reply_is_not_logged(self, mjol, capsys):
        entry = mjol._resolve_gain_entry(10002, "fast-e", 3, REJECTED_REPLY)
        assert entry is None
        assert "REJECTED" in capsys.readouterr().err

    def test_threshold_empty_reply_is_not_logged(self, mjol):
        """An empty/absent reply (e.g. a socket timeout with no data) means
        the sensor never confirmed the command -- must not be logged."""
        assert mjol._resolve_threshold_entry(10002, "1", 830, "") is None

    def test_gain_empty_reply_is_not_logged(self, mjol):
        assert mjol._resolve_gain_entry(10002, "fast-e", 2, "") is None


class TestControlOpsRecordChanges:
    """Integration: a successful --up/--down/--set-threshold/--set-gain
    updates the snapshot; a failed one, or one without --persist, does not.
    subprocess.run is mocked throughout -- nothing here contacts the fleet.
    """

    def test_successful_updown_logs_front_end(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.updown_array(True, ports=[2], log_repo=str(tmp_path))
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "on"

    def test_failed_updown_does_not_log(self, mjol, fp, tmp_path):
        """Tunnel down -> updown() returns False -> nothing is recorded."""
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=False), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            arr.updown_array(True, ports=[2], log_repo=str(tmp_path))
        mock_sub.run.assert_not_called()
        mock_log.assert_not_called()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "off"

    def test_nonzero_returncode_does_not_log(self, mjol, tmp_path):
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            mock_sub.run.return_value = MagicMock(returncode=1, stdout=b"",
                                                   stderr=b"boom")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.updown_array(True, ports=[2], log_repo=str(tmp_path))
        mock_log.assert_not_called()

    def test_no_log_flag_suppresses_logging_but_change_still_happens(
            self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"front_end": "off"}})
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot") as mock_commit:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.updown_array(True, ports=[2], log_repo=str(tmp_path),
                             no_log=True)
        # The control action itself still ran (sensors.py was invoked)...
        mock_sub.run.assert_called_once()
        # ...but the snapshot was never touched.
        mock_commit.assert_not_called()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "off"

    def test_threshold_without_persist_is_not_logged(self, mjol, tmp_path):
        """Live-only change: fleet_probe reads the startup file, so a
        non-persisted change is invisible to it and must not be recorded."""
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_threshold_array(ports=[2], channel="1", millivolts="830",
                                    persist=False, log_repo=str(tmp_path))
        mock_log.assert_not_called()

    def test_gain_without_persist_is_not_logged(self, mjol, tmp_path):
        """Same persist-only rule as threshold -- mutation-tested gap: the
        gain path is structurally identical but was not covered."""
        arr = mjol.MjolnirArray(sensors=[3])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_gain_array(ports=[3], channel="fast-e", level=2,
                               persist=False, log_repo=str(tmp_path))
        mock_log.assert_not_called()

    def test_threshold_persist_with_no_log_is_not_logged(self, mjol, tmp_path):
        """Mutation-tested gap: --no-log was covered for updown but not for
        threshold/gain. Today's code already respects it (`not no_log` at
        the persist-gate check) -- this test only guards against a future
        regression, it is not fixing a live bug."""
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_threshold_array(ports=[2], channel="1", millivolts="830",
                                    persist=True, log_repo=str(tmp_path),
                                    no_log=True)
        mock_log.assert_not_called()

    def test_gain_persist_with_no_log_is_not_logged(self, mjol, tmp_path):
        """Same regression guard as above, for the gain path."""
        arr = mjol.MjolnirArray(sensors=[3])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_log_field_changes_batch") as mock_log:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_gain_array(ports=[3], channel="fast-e", level=2,
                               persist=True, log_repo=str(tmp_path),
                               no_log=True)
        mock_log.assert_not_called()

    def test_threshold_with_persist_is_logged(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"threshold_1_mv": "1"}})
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            mock_sub.run.return_value = MagicMock(
                returncode=0, stdout=OK_REPLY.encode(), stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_threshold_array(ports=[2], channel="1", millivolts="830",
                                    persist=True, log_repo=str(tmp_path),
                                    reason="bench")
        mock_commit.assert_called_once()
        message = mock_commit.call_args[0][2]
        assert "bench" in message
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["threshold_1_mv"] != "1"

    def test_gain_with_persist_is_logged(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir03": {"gain_fast": "1"}})
        arr = mjol.MjolnirArray(sensors=[3])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            mock_sub.run.return_value = MagicMock(
                returncode=0, stdout=OK_REPLY.encode(), stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_gain_array(ports=[3], channel="fast-e", level=2,
                              persist=True, log_repo=str(tmp_path))
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir03"]["gain_fast"] == "2"

    # ---- HAM-189 finding #1: AGS-rejected persisted value ----

    def test_threshold_ags_rejection_is_not_logged(self, mjol, fp, tmp_path):
        """ssh/subprocess succeeds (returncode 0) but the AGS reply contains
        an 'Error -' marker -- the firmware rejected the value, so it must
        NOT be logged even though the control op itself "succeeded"."""
        _seed_snapshot(tmp_path, fp, {"mjolnir02": {"threshold_1_mv": "450"}})
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot") as mock_commit:
            mock_sub.run.return_value = MagicMock(
                returncode=0, stdout=REJECTED_REPLY.encode(), stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_threshold_array(ports=[2], channel="1", millivolts="2000",
                                    persist=True, log_repo=str(tmp_path))
        mock_commit.assert_not_called()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["threshold_1_mv"] == "450"   # unchanged

    def test_gain_ags_rejection_is_not_logged(self, mjol, fp, tmp_path):
        _seed_snapshot(tmp_path, fp, {"mjolnir03": {"gain_fast": "1"}})
        arr = mjol.MjolnirArray(sensors=[3])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot") as mock_commit:
            mock_sub.run.return_value = MagicMock(
                returncode=0, stdout=REJECTED_REPLY.encode(), stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.set_gain_array(ports=[3], channel="fast-e", level=3,
                              persist=True, log_repo=str(tmp_path))
        mock_commit.assert_not_called()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir03"]["gain_fast"] == "1"   # unchanged

    # ---- HAM-189 finding #2/#3: multi-unit fan-out with partial failure ----

    def test_fan_out_partial_failure_logs_only_the_succeeding_unit(
            self, mjol, fp, tmp_path):
        """Every existing integration test used a single unit (ports=[2]).
        A real sweep is multiple units, and one unit's tunnel being down
        must not affect another's control op or its logging."""
        _seed_snapshot(tmp_path, fp, {
            "mjolnir02": {"front_end": "off"},
            "mjolnir03": {"front_end": "off"},
        })
        arr = mjol.MjolnirArray(sensors=[2, 3])

        def fake_status(port):
            return port == 10002   # only mjolnir02's tunnel is up

        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status",
                            side_effect=fake_status), \
                patch.object(mjol, "_fleet_probe_module", return_value=fp), \
                patch.object(fp, "commit_snapshot",
                            return_value=(True, "ok")) as mock_commit:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            arr.updown_array(True, ports=[2, 3], log_repo=str(tmp_path))

        # One control op ran (mjolnir03's was skipped -- tunnel down).
        mock_sub.run.assert_called_once()
        # One commit for the whole sweep, covering only the unit that
        # actually succeeded.
        mock_commit.assert_called_once()
        updated = fp.read_snapshot(
            str(tmp_path / "state" / "fleet-state.csv"))
        assert updated["mjolnir02"]["front_end"] == "on"
        assert updated["mjolnir03"]["front_end"] == "off"

    def test_fan_out_snapshot_failure_does_not_affect_other_units_control_op(
            self, mjol, tmp_path):
        """A snapshot/git failure for the batch must not retroactively
        break or hide the fact that every unit's control op already ran."""
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module",
                            side_effect=RuntimeError("no disk")):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            # Must not raise, and both control ops must still run.
            arr.updown_array(True, ports=[2, 3], log_repo=str(tmp_path))
        assert mock_sub.run.call_count == 2


class TestWriteHookSafety:
    """Requirement #5: a snapshot-write failure must not fail the control
    operation, must not raise, and must not change the operation's own
    success reporting."""

    def test_snapshot_failure_does_not_raise_and_control_still_reports_ok(
            self, mjol, tmp_path, capsys):
        arr = mjol.MjolnirArray(sensors=[2])
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module",
                            side_effect=RuntimeError("no disk")):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            # Must not raise.
            arr.updown_array(True, ports=[2], log_repo=str(tmp_path))
        out = capsys.readouterr()
        # The control op's own [FAIL] path (sensors.py exit code) is not
        # triggered -- only the logging side failed.
        assert "[FAIL]" not in out.out

    def test_main_cli_does_not_raise_when_logging_fails(self, mjol, tmp_path):
        with patch.object(mjol, "subprocess") as mock_sub, \
                patch.object(mjol.MjolnirArray, "status", return_value=True), \
                patch.object(mjol, "_fleet_probe_module", return_value=None):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"",
                                                   stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            # Must return normally (no exception -> process would exit 0).
            mjol.main(["-p", "2", "--up", "--log-repo", str(tmp_path)])


class TestWriteHookCliFlags:
    """--log-repo/--no-log/--reason argparse wiring."""

    def test_log_repo_env_fallback(self, mjol, monkeypatch):
        monkeypatch.setenv("FLEET_LOG_REPO", "/env/repo")
        with patch.object(mjol.MjolnirArray, "updown_array") as mock_arr:
            mjol.main(["-p", "2", "--up"])
        assert mock_arr.call_args.kwargs["log_repo"] == "/env/repo"

    def test_log_repo_flag_overrides_env(self, mjol, monkeypatch):
        monkeypatch.setenv("FLEET_LOG_REPO", "/env/repo")
        with patch.object(mjol.MjolnirArray, "updown_array") as mock_arr:
            mjol.main(["-p", "2", "--up", "--log-repo", "/flag/repo"])
        assert mock_arr.call_args.kwargs["log_repo"] == "/flag/repo"

    def test_no_log_and_reason_reach_updown_array(self, mjol):
        with patch.object(mjol.MjolnirArray, "updown_array") as mock_arr:
            mjol.main(["-p", "2", "--up", "--no-log", "--reason", "bench"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["no_log"] is True
        assert kwargs["reason"] == "bench"

    def test_reason_reaches_set_threshold_array(self, mjol):
        with patch.object(mjol.MjolnirArray, "set_threshold_array") as mock_arr:
            mjol.main(["-p", "2", "--set-threshold", "1", "830",
                      "--persist", "--reason", "field adjustment"])
        kwargs = mock_arr.call_args.kwargs
        assert kwargs["reason"] == "field adjustment"
        assert kwargs["log_repo"] is None


class TestPersistOmissionWarning:
    """HAM-189 cheap improvement: omitting --persist with --log-repo set
    silently produces an untracked change. main() should print a one-line
    [LOG] notice, mirroring the existing "no --log-repo" notice, UNLESS the
    operator already said --no-log (self-documenting, no notice needed)."""

    def test_warns_when_persist_omitted_with_log_repo(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_threshold_array"):
            mjol.main(["-p", "2", "--set-threshold", "1", "830",
                      "--log-repo", "/some/repo"])
        assert "--persist not set" in capsys.readouterr().err

    def test_warns_for_gain_too(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_gain_array"):
            mjol.main(["-p", "2", "--set-gain", "fast-e", "2",
                      "--log-repo", "/some/repo"])
        assert "--persist not set" in capsys.readouterr().err

    def test_no_warning_when_persist_given(self, mjol, capsys):
        with patch.object(mjol.MjolnirArray, "set_threshold_array"):
            mjol.main(["-p", "2", "--set-threshold", "1", "830", "--persist",
                      "--log-repo", "/some/repo"])
        assert "--persist not set" not in capsys.readouterr().err

    def test_no_warning_when_no_log_given(self, mjol, capsys):
        """--no-log is already self-documenting -- no need to also warn."""
        with patch.object(mjol.MjolnirArray, "set_threshold_array"):
            mjol.main(["-p", "2", "--set-threshold", "1", "830", "--no-log",
                      "--log-repo", "/some/repo"])
        assert "--persist not set" not in capsys.readouterr().err

    def test_no_warning_without_log_repo(self, mjol, capsys):
        """No --log-repo/$FLEET_LOG_REPO -- the other, existing notice
        already covers this case; do not double-warn."""
        with patch.object(mjol.MjolnirArray, "set_threshold_array"):
            mjol.main(["-p", "2", "--set-threshold", "1", "830"])
        assert "--persist not set" not in capsys.readouterr().err
