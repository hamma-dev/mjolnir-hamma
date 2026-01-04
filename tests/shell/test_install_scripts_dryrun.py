"""
Dry-run simulation tests for install scripts.

These tests verify the logic and file operations of install scripts
without actually modifying the system. They use a mock filesystem
and trace what operations would be performed.
"""

import subprocess
import pytest
from pathlib import Path
import tempfile
import shutil
import os


REPO_ROOT = Path(__file__).parent.parent.parent
INSTALL_SCRIPTS_DIR = REPO_ROOT / "install_scripts"
FILES_DIR = REPO_ROOT / "files"


class TestBootstrapScript:
    """Tests for bootstrap.sh script (runs from USB once)."""

    def test_requires_sensor_number(self):
        """Test that script fails without sensor number argument."""
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPTS_DIR / "bootstrap.sh")],
            capture_output=True,
            text=True
        )
        assert result.returncode != 0
        assert "Usage" in result.stdout or "Error" in result.stdout

    def test_requires_wifi_or_no_wifi(self):
        """Test that script requires --wifi or --no-wifi."""
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPTS_DIR / "bootstrap.sh"), "-n", "42"],
            capture_output=True,
            text=True
        )
        assert result.returncode != 0
        assert "--wifi" in result.stdout or "no-wifi" in result.stdout

    def test_wifi_requires_ssid(self):
        """Test that --wifi requires an SSID argument."""
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPTS_DIR / "bootstrap.sh"), "-n", "42", "--wifi"],
            capture_output=True,
            text=True
        )
        assert result.returncode != 0

    def test_supports_n_flag(self):
        """Test that -n flag is supported for sensor number."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        assert "-n|--sensor-num" in content or '"-n"' in content

    def test_supports_positional_sensor_number(self):
        """Test that positional sensor number is still supported."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        # Should handle positional argument for backwards compat
        assert "Positional" in content or "positional" in content or "backwards" in content.lower()

    def test_script_structure(self):
        """Test that bootstrap.sh has the expected structure."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()

        # Should set hostname
        assert "hostname" in content.lower()
        assert "mjolnir" in content

        # Should disable wifi
        assert "disable-wifi" in content

        # Should copy/clone repo
        assert "mjolnir-hamma" in content

        # Should set timezone to UTC
        assert "UTC" in content

        # Should setup temp WiFi
        assert "wpa_supplicant" in content
        assert "WIFI_SSID" in content or "wifi" in content.lower()

    def test_formats_hostname_correctly(self):
        """Test that hostname formatting uses printf for zero-padding."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()

        # Should use printf for zero-padding (%.2d or %02d)
        assert 'printf "%.2d"' in content or 'printf "%02d"' in content

    def test_wifi_config_appends_network(self):
        """Test that WiFi setup appends to wpa_supplicant config."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        # Should append (tee -a) not overwrite
        assert "tee -a" in content
        assert "priority=10" in content

    def test_fixes_system_clock_if_wrong(self):
        """Test that bootstrap fixes system clock if year is before 2024."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        # Should check year and fix if wrong
        assert "CURRENT_YEAR" in content or "date" in content
        assert "2024" in content  # Check for obviously wrong year
        # Should use file timestamp as approximate time
        assert "stat" in content

    def test_enables_systemd_networkd(self):
        """Test that bootstrap enables systemd-networkd for persistence."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        assert "systemctl enable systemd-networkd" in content

    def test_enables_systemd_resolved(self):
        """Test that bootstrap enables systemd-resolved for DNS."""
        content = (INSTALL_SCRIPTS_DIR / "bootstrap.sh").read_text()
        assert "systemctl enable systemd-resolved" in content


class TestInstallDriverScript:
    """Tests for install.sh master driver script."""

    def test_requires_root(self):
        """Test that script checks for root privileges."""
        content = (INSTALL_SCRIPTS_DIR / "install.sh").read_text()
        assert "EUID" in content or "root" in content.lower()

    def test_has_help_option(self):
        """Test that --help option is available."""
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPTS_DIR / "install.sh"), "--help"],
            capture_output=True,
            text=True
        )
        # Should show usage without error (or at least mention usage)
        assert "Usage" in result.stdout or "--help" in result.stdout

    def test_supports_sensor_num_option(self):
        """Test that -n/--sensor-num option is supported."""
        content = (INSTALL_SCRIPTS_DIR / "install.sh").read_text()
        assert "--sensor-num" in content or "-n)" in content

    def test_supports_skip_options(self):
        """Test that --skip-* options are supported."""
        content = (INSTALL_SCRIPTS_DIR / "install.sh").read_text()
        assert "--skip-packages" in content
        assert "--skip-network" in content
        assert "--skip-brokkr" in content
        assert "--skip-hardware" in content

    def test_supports_only_option(self):
        """Test that --only option is supported."""
        content = (INSTALL_SCRIPTS_DIR / "install.sh").read_text()
        assert "--only" in content
        assert "packages" in content
        assert "network" in content
        assert "brokkr" in content
        assert "hardware" in content

    def test_calls_worker_scripts(self):
        """Test that install.sh calls the worker scripts."""
        content = (INSTALL_SCRIPTS_DIR / "install.sh").read_text()

        worker_scripts = [
            "install_packages.sh",
            "setup_wwan.sh",
            "setup_brokkr.sh",
            "setup_hardware.sh"
        ]

        for script in worker_scripts:
            assert script in content, f"install.sh should call {script}"


class TestSetupHardwareScript:
    """Tests for setup_hardware.sh (combined sensor connect + automount)."""

    def test_requires_root(self):
        """Test that script checks for root privileges."""
        content = (INSTALL_SCRIPTS_DIR / "setup_hardware.sh").read_text()
        assert "EUID" in content or "root" in content.lower()

    def test_copies_ssh_config(self):
        """Test that SSH config is copied for sensor connection."""
        content = (INSTALL_SCRIPTS_DIR / "setup_hardware.sh").read_text()
        assert "config" in content
        assert ".ssh" in content or "SSH_PATH" in content

    def test_copies_network_files(self):
        """Test that network config files are copied."""
        content = (INSTALL_SCRIPTS_DIR / "setup_hardware.sh").read_text()
        assert "eth0" in content or "40-eth0" in content
        assert "eth1" in content or "30-eth1" in content

    def test_sets_up_automount(self):
        """Test that polkit rules for automount are set up."""
        content = (INSTALL_SCRIPTS_DIR / "setup_hardware.sh").read_text()
        assert "polkit" in content.lower() or "pkla" in content

    def test_copies_required_files(self, tmp_path):
        """Test that required files are copied to correct locations."""
        # Create source structure
        source_files = tmp_path / "source" / "files"
        source_files.mkdir(parents=True)

        # Create source files
        (source_files / "config").write_text("Host hamma\n  User pi\n")
        (source_files / "30-eth1.network").write_text("[Match]\nName=eth1\n")
        (source_files / "40-eth0.network").write_text("[Match]\nName=eth0\n")
        (source_files / "mount-udisks.pkla").write_text("[Allow]\nAction=org.freedesktop.udisks2.*\n")

        # Create destination directories
        dest_ssh = tmp_path / "home" / "pi" / ".ssh"
        dest_network = tmp_path / "etc" / "systemd" / "network"
        dest_polkit = tmp_path / "etc" / "polkit-1" / "localauthority" / "50-local.d"
        dest_ssh.mkdir(parents=True)
        dest_network.mkdir(parents=True)
        dest_polkit.mkdir(parents=True)

        # Modify script for test
        script_content = (INSTALL_SCRIPTS_DIR / "setup_hardware.sh").read_text()
        script_content = script_content.replace(
            'FILES_PATH="/home/pi/dev/mjolnir-hamma/files"',
            f'FILES_PATH="{source_files}"'
        )
        script_content = script_content.replace(
            'NETWORK_PATH="/etc/systemd/network"',
            f'NETWORK_PATH="{dest_network}"'
        )
        script_content = script_content.replace(
            'POLKIT_PATH="/etc/polkit-1/localauthority/50-local.d"',
            f'POLKIT_PATH="{dest_polkit}"'
        )
        script_content = script_content.replace(
            'SSH_PATH="/home/pi/.ssh"',
            f'SSH_PATH="{dest_ssh}"'
        )
        # Remove sudo and root check for testing
        script_content = script_content.replace("sudo ", "")
        script_content = script_content.replace(
            'if [[ $EUID -ne 0 ]]; then\n    echo "This script must be run as root (use sudo)"\n    exit 1\nfi',
            '# Root check disabled for testing'
        )
        # Don't restart networkd in test
        script_content = script_content.replace(
            'systemctl restart systemd-networkd',
            'echo "Would restart systemd-networkd"'
        )
        # Replace chown commands (pi user doesn't exist on test system)
        import re
        script_content = re.sub(r'chown [^\n]+', 'echo "Would chown"', script_content)

        test_script = tmp_path / "test_setup_hardware.sh"
        test_script.write_text(script_content)
        test_script.chmod(0o755)

        result = subprocess.run(
            ["bash", str(test_script)],
            capture_output=True,
            text=True
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert (dest_ssh / "config").exists()
        assert (dest_network / "30-eth1.network").exists()
        assert (dest_network / "40-eth0.network").exists()


class TestSetupWwanScript:
    """Tests for setup_wwan.sh script (timer-based approach)."""

    def test_uses_timer_service(self):
        """Test that setup_wwan.sh uses timer-based service (not degraded.d)."""
        content = (INSTALL_SCRIPTS_DIR / "setup_wwan.sh").read_text()
        assert "wwan-check.timer" in content, \
            "setup_wwan.sh should use wwan-check.timer"

    def test_disables_dhcpcd(self):
        """Test that dhcpcd is disabled (conflicts with systemd-networkd)."""
        content = (INSTALL_SCRIPTS_DIR / "setup_wwan.sh").read_text()
        assert "dhcpcd" in content
        assert "disable" in content or "stop" in content

    def test_removes_old_networkd_dispatcher(self):
        """Test that old networkd-dispatcher scripts are removed."""
        content = (INSTALL_SCRIPTS_DIR / "setup_wwan.sh").read_text()
        assert "networkd-dispatcher" in content
        # Should remove old carrier.d or degraded.d scripts
        assert "rm" in content

    def test_supports_apn_argument(self):
        """Test that --apn argument is supported."""
        content = (INSTALL_SCRIPTS_DIR / "setup_wwan.sh").read_text()
        assert "--apn" in content or "APN" in content

    def test_copies_wwan_scripts(self):
        """Test that WWAN scripts are copied to correct location."""
        content = (INSTALL_SCRIPTS_DIR / "setup_wwan.sh").read_text()
        assert "/usr/local/bin" in content
        assert "wwan-check.sh" in content or "bring_wwan0_up" in content


class TestSetupBrokkrScript:
    """Tests for setup_brokkr.sh script (combined install + setup)."""

    def test_requires_sensor_number(self):
        """Test that script requires sensor number argument."""
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPTS_DIR / "setup_brokkr.sh")],
            capture_output=True,
            text=True
        )
        assert result.returncode != 0
        assert "Usage" in result.stdout or "sensor_number" in result.stdout

    def test_creates_venv(self):
        """Test that virtual environment is created."""
        content = (INSTALL_SCRIPTS_DIR / "setup_brokkr.sh").read_text()

        assert "python3 -m venv" in content
        assert "pip install --upgrade pip" in content

    def test_clones_required_repos(self):
        """Test that all required repos are cloned."""
        content = (INSTALL_SCRIPTS_DIR / "setup_brokkr.sh").read_text()

        required_repos = [
            "brokkr",
            "serviceinstaller",
            "notifiers"
        ]

        for repo in required_repos:
            assert repo in content, f"Missing clone for {repo}"

    def test_configures_brokkr(self):
        """Test that brokkr configure commands are called."""
        content = (INSTALL_SCRIPTS_DIR / "setup_brokkr.sh").read_text()

        assert "brokkr configure-system" in content
        assert "brokkr configure-unit" in content
        assert "brokkr install-dependencies" in content

    def test_installs_services(self):
        """Test that Brokkr services are installed."""
        content = (INSTALL_SCRIPTS_DIR / "setup_brokkr.sh").read_text()
        assert "brokkr" in content and "install" in content


class TestInstallPackagesScript:
    """Tests for install_packages.sh script."""

    def test_installs_core_packages(self):
        """Test that core packages are installed."""
        content = (INSTALL_SCRIPTS_DIR / "install_packages.sh").read_text()

        core_packages = [
            "python3-venv",
            "git",
            "imagemagick",
            "udisks2"
        ]

        for pkg in core_packages:
            assert pkg in content, f"Missing package: {pkg}"

    def test_installs_wwan_packages(self):
        """Test that WWAN-related packages are installed."""
        content = (INSTALL_SCRIPTS_DIR / "install_packages.sh").read_text()

        wwan_packages = [
            "modemmanager",
            "udhcpc",
            "libqmi-utils"
        ]

        for pkg in wwan_packages:
            assert pkg in content, f"Missing WWAN package: {pkg}"

    def test_uses_noninteractive_flags(self):
        """Test that apt-get uses -y for non-interactive install."""
        content = (INSTALL_SCRIPTS_DIR / "install_packages.sh").read_text()
        # All apt-get install commands should have -y
        lines = [l for l in content.split('\n') if 'apt-get install' in l]
        for line in lines:
            assert '-y' in line, f"Missing -y flag in: {line}"


class TestNetworkConfigFiles:
    """Tests for network configuration files in files/."""

    def test_20_wwan0_network_format(self):
        """Test that 20-wwan0.network has correct format."""
        config_file = FILES_DIR / "20-wwan0.network"
        content = config_file.read_text()

        assert "[Match]" in content
        assert "Name=wwan0" in content
        assert "[Network]" in content
        # Should have DHCP=no and Unmanaged=yes per Cellular Fixes
        assert "DHCP=no" in content
        assert "Unmanaged=yes" in content

    def test_30_eth1_network_format(self):
        """Test that 30-eth1.network has correct format."""
        config_file = FILES_DIR / "30-eth1.network"
        content = config_file.read_text()

        assert "[Match]" in content
        assert "Name=eth1" in content
        assert "192.168.1.2" in content  # Static IP for sensor connection

    def test_40_eth0_network_format(self):
        """Test that 40-eth0.network has correct format."""
        config_file = FILES_DIR / "40-eth0.network"
        content = config_file.read_text()

        assert "[Match]" in content
        assert "Name=eth0" in content

    def test_wwan_check_sh_uses_flock(self):
        """Test that wwan-check.sh uses flock for concurrency control."""
        script_file = FILES_DIR / "wwan-check.sh"
        content = script_file.read_text()

        assert "flock" in content, "wwan-check.sh should use flock"
        assert "/tmp/wwan_connect.lock" in content or "lock" in content.lower()

    def test_wwan_check_timer_exists(self):
        """Test that wwan-check.timer exists."""
        timer_file = FILES_DIR / "wwan-check.timer"
        assert timer_file.exists(), "wwan-check.timer should exist"

        content = timer_file.read_text()
        assert "[Timer]" in content
        assert "OnBootSec" in content
        assert "OnUnitActiveSec" in content

    def test_wwan_check_service_exists(self):
        """Test that wwan-check.service exists."""
        service_file = FILES_DIR / "wwan-check.service"
        assert service_file.exists(), "wwan-check.service should exist"

        content = service_file.read_text()
        assert "[Service]" in content
        assert "wwan-check.sh" in content


class TestFileConsistency:
    """Tests for consistency between scripts and config files."""

    def test_wwan_script_apn_is_configurable(self):
        """Test that APN is defined as a constant that can be changed."""
        script_file = REPO_ROOT / "scripts" / "50_bring_wwan0_up.py"
        content = script_file.read_text()

        assert "APN" in content
        # Should be near the top as a configuration constant
        lines = content.split('\n')
        apn_line = None
        for i, line in enumerate(lines):
            if 'APN' in line and '=' in line and not line.strip().startswith('#'):
                apn_line = i
                break

        assert apn_line is not None and apn_line < 30, \
            "APN should be defined as a constant near the top of the script"

    def test_scripts_reference_consistent_paths(self):
        """Test that scripts use consistent paths."""
        expected_paths = {
            # Scripts that must reference mjolnir-hamma path (directly or via variable)
            "/home/pi/dev/": ["setup_hardware.sh", "setup_brokkr.sh"],
            "/etc/systemd/network/": ["setup_hardware.sh", "setup_wwan.sh"],
        }

        for path, scripts in expected_paths.items():
            for script_name in scripts:
                script_path = INSTALL_SCRIPTS_DIR / script_name
                if script_path.exists():
                    content = script_path.read_text()
                    # Path should appear (possibly with slight variations or as variable)
                    base_path = path.rstrip('/')
                    # Also check for variable assignments that contain the path
                    path_found = (
                        base_path in content or
                        path in content or
                        # Handle cases like INSTALL_PATH="/home/pi/dev/"
                        f'"{base_path}"' in content or
                        f'"{path}"' in content
                    )
                    assert path_found, \
                        f"{script_name} should reference {path}"


class TestArchivedScriptsNotReferenced:
    """Ensure archived scripts are not referenced by active scripts."""

    def test_no_references_to_archived_scripts(self):
        """Test that active scripts don't reference archived scripts."""
        archived_scripts = [
            "install_brokkr.sh",
            "setup_sensor_connect.sh",
            "enable_automount.sh",
            "update_host.sh",
            "disable_wifi_radio.sh",
            "setup_wwan_BU.sh",
            "fix_wwan_finally.sh"
        ]

        active_scripts = [
            "bootstrap.sh",
            "install.sh",
            "setup_brokkr.sh",
            "setup_hardware.sh",
            "setup_wwan.sh",
            "install_packages.sh"
        ]

        for active in active_scripts:
            script_path = INSTALL_SCRIPTS_DIR / active
            if script_path.exists():
                content = script_path.read_text()
                for archived in archived_scripts:
                    # Only check if the archived script is called (not just mentioned in comments)
                    # Look for patterns like "./archived.sh" or "source archived.sh"
                    if f"./{archived}" in content or f"source {archived}" in content:
                        assert False, f"{active} references archived script {archived}"
