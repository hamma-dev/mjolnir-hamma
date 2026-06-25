"""
Pytest configuration and shared fixtures for mjolnir-hamma tests.
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the files directory to path so we can import the Python scripts
REPO_ROOT = Path(__file__).parent.parent
FILES_DIR = REPO_ROOT / "files"
INSTALL_SCRIPTS_DIR = REPO_ROOT / "install_scripts"

sys.path.insert(0, str(FILES_DIR))


@pytest.fixture
def repo_root():
    """Return the repository root path."""
    return REPO_ROOT


@pytest.fixture
def files_dir():
    """Return the files directory path."""
    return FILES_DIR


@pytest.fixture
def install_scripts_dir():
    """Return the install_scripts directory path."""
    return INSTALL_SCRIPTS_DIR


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for testing without actual system calls."""
    with patch('subprocess.run') as mock_run:
        # Default successful return
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'',
            stderr=b''
        )
        yield mock_run


@pytest.fixture
def mock_logger():
    """Mock logger for testing logging output."""
    with patch('logging.getLogger') as mock_get_logger:
        mock_log = MagicMock()
        mock_get_logger.return_value = mock_log
        yield mock_log


@pytest.fixture
def temp_filesystem(tmp_path):
    """
    Create a temporary filesystem structure mimicking a Pi.
    Returns a dict with paths to various mock system directories.
    """
    # Create mock system directories
    dirs = {
        'etc_systemd_network': tmp_path / 'etc' / 'systemd' / 'network',
        'etc_systemd_system': tmp_path / 'etc' / 'systemd' / 'system',
        'usr_local_bin': tmp_path / 'usr' / 'local' / 'bin',
        'home_pi': tmp_path / 'home' / 'pi',
        'home_pi_dev': tmp_path / 'home' / 'pi' / 'dev',
        'home_pi_ssh': tmp_path / 'home' / 'pi' / '.ssh',
        'sys_class_net_wwan0_qmi': tmp_path / 'sys' / 'class' / 'net' / 'wwan0' / 'qmi',
        'etc_networkd_dispatcher_carrier': tmp_path / 'etc' / 'networkd-dispatcher' / 'carrier.d',
        'etc_networkd_dispatcher_degraded': tmp_path / 'etc' / 'networkd-dispatcher' / 'degraded.d',
        'boot': tmp_path / 'boot',
    }

    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    # Create mock raw_ip file
    raw_ip_file = dirs['sys_class_net_wwan0_qmi'] / 'raw_ip'
    raw_ip_file.write_text('N')

    # Create mock config.txt
    config_txt = dirs['boot'] / 'config.txt'
    config_txt.write_text('# Raspberry Pi config\n')

    dirs['root'] = tmp_path
    return dirs


@pytest.fixture
def sample_mmcli_output():
    """Sample mmcli output for testing modem state parsing."""
    return {
        'modem_list': '/org/freedesktop/ModemManager1/Modem/0 [Quectel] EG25-G\n',
        'modem_connected': '''
  -------------------------
  General  |      dbus path: /org/freedesktop/ModemManager1/Modem/0
           |         device id: abcd1234
  -------------------------
  Status   |         state: connected
           |   power state: on
           |   access tech: lte
           | signal quality: 75% (recent)
  -------------------------
  Modes    |       supported: allowed: 4g; preferred: none
  -------------------------
  Bands    |       supported: utran-1
  -------------------------
  IP       |       supported: ipv4, ipv6, ipv4v6
  -------------------------
  Bearer   | dbus path: /org/freedesktop/ModemManager1/Bearer/1
''',
        'modem_registered': '''
  -------------------------
  Status   |         state: registered
           |   power state: on
           | signal quality: 54% (recent)
''',
        'modem_low_signal': '''
  -------------------------
  Status   |         state: connected
           |   power state: on
           | signal quality: 3% (recent)
''',
        'modem_disconnected': '''
  -------------------------
  Status   |         state: disabled
           |   power state: on
           | signal quality: 0% (cached)
''',
    }
