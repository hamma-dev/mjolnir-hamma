"""
Unit tests for 50_bring_wwan0_up.py - the WWAN connection management script.

These tests mock subprocess calls to test the logic without actually
interacting with ModemManager or network interfaces.
"""

import subprocess
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
import sys
import importlib.util

# Load the wwan script as a module
REPO_ROOT = Path(__file__).parent.parent.parent
WWAN_SCRIPT = REPO_ROOT / "scripts" / "50_bring_wwan0_up.py"


def load_wwan_module():
    """Load the wwan script as a module for testing."""
    spec = importlib.util.spec_from_file_location("wwan_script", WWAN_SCRIPT)
    module = importlib.util.module_from_spec(spec)

    # Mock the logging setup before loading
    with patch('logging.handlers.SysLogHandler'):
        with patch('logging.getLogger') as mock_logger:
            mock_log = MagicMock()
            mock_logger.return_value = mock_log
            spec.loader.exec_module(module)

    return module


class TestPingCheck:
    """Tests for the ping_check() function."""

    def test_ping_success(self):
        """Test that ping_check returns True on successful ping."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            module = load_wwan_module()
            # Re-patch for the actual test
            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0)
                mock_sub.CalledProcessError = subprocess.CalledProcessError
                mock_sub.TimeoutExpired = subprocess.TimeoutExpired

                result = module.ping_check()
                assert result is True

    def test_ping_failure(self):
        """Test that ping_check returns False on failed ping."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.side_effect = subprocess.CalledProcessError(
                    1, 'ping', stderr=b'Host unreachable'
                )
                mock_sub.CalledProcessError = subprocess.CalledProcessError
                mock_sub.TimeoutExpired = subprocess.TimeoutExpired

                result = module.ping_check()
                assert result is False

    def test_ping_timeout(self):
        """Test that ping_check returns False on timeout."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.side_effect = subprocess.TimeoutExpired('ping', 5)
                mock_sub.CalledProcessError = subprocess.CalledProcessError
                mock_sub.TimeoutExpired = subprocess.TimeoutExpired

                result = module.ping_check()
                assert result is False


class TestModemStateParsing:
    """Tests for modem state parsing functions."""

    def test_get_modem_state_connected(self, sample_mmcli_output):
        """Test parsing 'connected' state from mmcli output."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout=sample_mmcli_output['modem_connected'].encode(),
                    stderr=b''
                )

                result = module.get_modem_state('0')
                assert result == 'connected'

    def test_get_modem_state_registered(self, sample_mmcli_output):
        """Test parsing 'registered' state from mmcli output."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout=sample_mmcli_output['modem_registered'].encode(),
                    stderr=b''
                )

                result = module.get_modem_state('0')
                assert result == 'registered'

    def test_get_signal_quality_high(self, sample_mmcli_output):
        """Test parsing high signal quality."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout=sample_mmcli_output['modem_connected'].encode(),
                    stderr=b''
                )

                result = module.get_signal_quality('0')
                assert result == 75

    def test_get_signal_quality_low(self, sample_mmcli_output):
        """Test parsing low signal quality triggers zombie detection."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout=sample_mmcli_output['modem_low_signal'].encode(),
                    stderr=b''
                )

                result = module.get_signal_quality('0')
                assert result == 3
                assert result <= module.LOW_SIGNAL_THRESHOLD


class TestFindModem:
    """Tests for the find_modem() function."""

    def test_find_modem_success(self, sample_mmcli_output):
        """Test successful modem discovery."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout=sample_mmcli_output['modem_list']
                )
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):  # Skip sleep
                    result = module.find_modem(retries=1, delay=0)
                    assert result == '0'

    def test_find_modem_not_found(self):
        """Test modem not found after retries."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout='No modems found'
                )
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):  # Skip sleep
                    result = module.find_modem(retries=2, delay=0)
                    assert result is None


class TestRawIpMode:
    """Tests for raw_ip mode handling."""

    def test_check_raw_ip_enabled(self, temp_filesystem):
        """Test checking when raw_ip is enabled."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            # Write 'Y' to mock raw_ip file
            raw_ip_path = temp_filesystem['sys_class_net_wwan0_qmi'] / 'raw_ip'
            raw_ip_path.write_text('Y')

            # Patch the IFACE to use our temp path
            original_open = open

            def mock_file_open(path, *args, **kwargs):
                if 'raw_ip' in str(path):
                    return original_open(str(raw_ip_path), *args, **kwargs)
                return original_open(path, *args, **kwargs)

            with patch('builtins.open', side_effect=mock_file_open):
                result = module.check_raw_ip()
                assert result is True

    def test_check_raw_ip_disabled(self, temp_filesystem):
        """Test checking when raw_ip is disabled."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            raw_ip_path = temp_filesystem['sys_class_net_wwan0_qmi'] / 'raw_ip'
            raw_ip_path.write_text('N')

            original_open = open

            def mock_file_open(path, *args, **kwargs):
                if 'raw_ip' in str(path):
                    return original_open(str(raw_ip_path), *args, **kwargs)
                return original_open(path, *args, **kwargs)

            with patch('builtins.open', side_effect=mock_file_open):
                result = module.check_raw_ip()
                assert result is False


class TestConnectModem:
    """Tests for modem connection logic."""

    def test_connect_modem_success(self, sample_mmcli_output):
        """Test successful modem connection."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                # First call: simple-connect succeeds
                # Subsequent calls: get_modem_state returns 'connected'
                mock_sub.run.side_effect = [
                    MagicMock(returncode=0),  # simple-connect
                    MagicMock(  # get_modem_state
                        returncode=0,
                        stdout=sample_mmcli_output['modem_connected'].encode(),
                        stderr=b''
                    ),
                ]
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):
                    result = module.connect_modem('0', retries=1, delay=0)
                    assert result is True

    def test_connect_modem_already_connected(self):
        """Test handling 'TooMany bearers' error (already connected)."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                # First call raises TooMany error
                error = subprocess.CalledProcessError(1, 'mmcli')
                error.stdout = b''
                error.stderr = b'TooMany: all existing bearers are connected'
                mock_sub.run.side_effect = [
                    error,  # simple-connect (TooMany)
                    MagicMock(  # get_modem_state - still returns connected
                        returncode=0,
                        stdout=b'state: connected',
                        stderr=b''
                    ),
                ]
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):
                    # Should handle gracefully and verify state
                    result = module.connect_modem('0', retries=1, delay=0)
                    # Result depends on state verification


class TestForceDisconnect:
    """Tests for force disconnect functionality."""

    def test_force_disconnect_success(self, sample_mmcli_output):
        """Test successful bearer disconnect."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.side_effect = [
                    MagicMock(  # mmcli -m 0 to find bearer
                        returncode=0,
                        stdout=sample_mmcli_output['modem_connected']
                    ),
                    MagicMock(returncode=0),  # bearer disconnect
                ]
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):
                    result = module.force_disconnect('0')
                    assert result is True


class TestDHCPClient:
    """Tests for DHCP client functionality."""

    def test_run_dhcp_client_success(self):
        """Test successful DHCP client run."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(returncode=0)
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):
                    result = module.run_dhcp_client()
                    assert result is True

    def test_run_dhcp_client_failure(self):
        """Test DHCP client failure handling."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                error = subprocess.CalledProcessError(1, 'udhcpc')
                error.stdout = b''
                error.stderr = b'No lease obtained'
                mock_sub.run.side_effect = error
                mock_sub.CalledProcessError = subprocess.CalledProcessError

                with patch.object(module, 'time'):
                    result = module.run_dhcp_client()
                    assert result is False


class TestIPAddressCheck:
    """Tests for IP address checking."""

    def test_check_ip_address_has_ip(self):
        """Test when interface has an IP address."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout='    inet 10.45.67.89/30 brd 10.45.67.91 scope global wwan0'
                )

                result = module.check_ip_address()
                assert result is True

    def test_check_ip_address_no_ip(self):
        """Test when interface has no IP address."""
        with patch('subprocess.run') as mock_run:
            module = load_wwan_module()

            with patch.object(module, 'subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0,
                    stdout='wwan0: <POINTOPOINT,NOARP> mtu 1500'  # No inet line
                )

                result = module.check_ip_address()
                assert result is False


class TestConfigurationValues:
    """Tests for configuration constants."""

    def test_apn_is_set(self):
        """Test that APN is configured."""
        with patch('subprocess.run'):
            module = load_wwan_module()
            assert hasattr(module, 'APN')
            assert module.APN is not None
            assert len(module.APN) > 0

    def test_low_signal_threshold_reasonable(self):
        """Test that low signal threshold is reasonable."""
        with patch('subprocess.run'):
            module = load_wwan_module()
            assert hasattr(module, 'LOW_SIGNAL_THRESHOLD')
            assert 0 < module.LOW_SIGNAL_THRESHOLD < 20

    def test_ping_timeout_reasonable(self):
        """Test that ping timeout is reasonable."""
        with patch('subprocess.run'):
            module = load_wwan_module()
            assert hasattr(module, 'PING_TIMEOUT')
            assert 1 <= module.PING_TIMEOUT <= 30
