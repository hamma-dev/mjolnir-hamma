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
        with patch.object(mjol.MjolnirArray, "set_threshold") as mock_set:
            arr.set_threshold_array(channel=1, millivolts=830)
        called_ports = [c[0][0] for c in mock_set.call_args_list]
        assert called_ports == [10002, 10003]

    def test_set_gain_array_explicit_ports(self, mjol):
        arr = mjol.MjolnirArray(sensors=[2, 3])
        with patch.object(mjol.MjolnirArray, "set_gain") as mock_set:
            arr.set_gain_array(ports=["2"], channel="slow-e", level=0)
        assert mock_set.call_args_list[0][0][0] == 10002
