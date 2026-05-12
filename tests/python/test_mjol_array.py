"""Tests for server/mjol_array.py — array status and control."""

import importlib.util
import pathlib
import subprocess

import pytest
from unittest.mock import patch, MagicMock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "server" / "mjol_array.py"


def load_mjol_array():
    """Load mjol_array module from server/."""
    spec = importlib.util.spec_from_file_location(
        "mjol_array", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    import sys
    mock_pd = MagicMock()
    mock_np = MagicMock()
    orig_pd = sys.modules.get('pandas')
    orig_np = sys.modules.get('numpy')
    sys.modules['pandas'] = mock_pd
    sys.modules['numpy'] = mock_np
    try:
        spec.loader.exec_module(module)
    finally:
        if orig_pd is not None:
            sys.modules['pandas'] = orig_pd
        else:
            sys.modules.pop('pandas', None)
        if orig_np is not None:
            sys.modules['numpy'] = orig_np
        else:
            sys.modules.pop('numpy', None)
    return module


@pytest.fixture
def mjol():
    """Provide the mjol_array module."""
    return load_mjol_array()


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
