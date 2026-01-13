"""
Layer 3 Integration Tests - Verify actual file creation and ownership.

These tests should be run INSIDE the Docker systemd container after
running install.sh. They verify that files were actually created
(not just logged in the manifest).

Usage:
    # Start Docker container
    ./test-with-systemd.sh --run-install --cellular

    # Then run these tests inside the container
    docker exec mjolnir-systemd-test bash -c \
        "cd /home/pi/dev/mjolnir-hamma && python3 -m pytest tests/integration/test_integration.py -v"

Or run the convenience script:
    ./test-with-systemd.sh --verify
"""

import os
import pwd
import grp
import subprocess
import pytest
from pathlib import Path


# --- Helper functions ---

def get_owner(path: Path) -> str:
    """Get the username of the file owner."""
    if not path.exists():
        return None
    return pwd.getpwuid(path.stat().st_uid).pw_name


def get_group(path: Path) -> str:
    """Get the group name of the file."""
    if not path.exists():
        return None
    return grp.getgrgid(path.stat().st_gid).gr_name


def is_executable(path: Path) -> bool:
    """Check if file is executable."""
    if not path.exists():
        return False
    return os.access(path, os.X_OK)


def service_exists(service_name: str) -> bool:
    """Check if a systemd service unit file exists."""
    paths = [
        Path(f"/etc/systemd/system/{service_name}"),
        Path(f"/lib/systemd/system/{service_name}"),
        Path(f"/usr/lib/systemd/system/{service_name}"),
    ]
    return any(p.exists() for p in paths)


def service_enabled(service_name: str) -> bool:
    """Check if a systemd service is enabled."""
    result = subprocess.run(
        ["systemctl", "is-enabled", service_name],
        capture_output=True,
        text=True
    )
    return result.stdout.strip() == "enabled"


def timer_exists(timer_name: str) -> bool:
    """Check if a systemd timer exists."""
    return service_exists(timer_name)


# --- Skip if not in container ---

def in_docker():
    """Check if we're running inside Docker."""
    return os.path.exists("/.dockerenv") or os.environ.get("MJOLNIR_TESTING") == "1"


skip_if_not_docker = pytest.mark.skipif(
    not in_docker(),
    reason="Integration tests must run inside Docker container"
)


# --- Test Classes ---

@skip_if_not_docker
class TestFileOwnership:
    """Verify that files created by install.sh have correct ownership."""

    def test_ssh_dir_owned_by_pi(self):
        """SSH directory should be owned by pi."""
        ssh_dir = Path("/home/pi/.ssh")
        if ssh_dir.exists():
            assert get_owner(ssh_dir) == "pi", f".ssh owned by {get_owner(ssh_dir)}, not pi"

    def test_ssh_key_owned_by_pi(self):
        """SSH keys should be owned by pi, not root."""
        id_rsa = Path("/home/pi/.ssh/id_rsa")
        if id_rsa.exists():
            owner = get_owner(id_rsa)
            assert owner == "pi", f"id_rsa owned by {owner}, expected pi"

    def test_ssh_key_pub_owned_by_pi(self):
        """SSH public key should be owned by pi."""
        id_rsa_pub = Path("/home/pi/.ssh/id_rsa.pub")
        if id_rsa_pub.exists():
            owner = get_owner(id_rsa_pub)
            assert owner == "pi", f"id_rsa.pub owned by {owner}, expected pi"

    def test_brokkr_venv_owned_by_pi(self):
        """Brokkr virtual environment should be owned by pi."""
        venv = Path("/home/pi/ltgenv")
        if venv.exists():
            owner = get_owner(venv)
            assert owner == "pi", f"ltgenv owned by {owner}, expected pi"

    def test_brokkr_config_owned_by_pi(self):
        """Brokkr config directory should be owned by pi."""
        config = Path("/home/pi/.config/brokkr")
        if config.exists():
            owner = get_owner(config)
            assert owner == "pi", f".config/brokkr owned by {owner}, expected pi"

    def test_no_root_config_brokkr(self):
        """There should be no brokkr config in /root (common bug)."""
        root_config = Path("/root/.config/brokkr")
        assert not root_config.exists(), \
            "Found /root/.config/brokkr - this causes permission issues!"

    def test_dev_dir_owned_by_pi(self):
        """Dev directory should be owned by pi."""
        dev_dir = Path("/home/pi/dev")
        if dev_dir.exists():
            owner = get_owner(dev_dir)
            assert owner == "pi", f"dev/ owned by {owner}, expected pi"


@skip_if_not_docker
class TestSSHKeyGeneration:
    """Verify SSH keys were generated correctly."""

    def test_id_rsa_exists(self):
        """Private key should exist."""
        id_rsa = Path("/home/pi/.ssh/id_rsa")
        assert id_rsa.exists(), "id_rsa not found - SSH key not generated"

    def test_id_rsa_pub_exists(self):
        """Public key should exist."""
        id_rsa_pub = Path("/home/pi/.ssh/id_rsa.pub")
        assert id_rsa_pub.exists(), "id_rsa.pub not found"

    def test_id_rsa_permissions(self):
        """Private key should have 600 permissions."""
        id_rsa = Path("/home/pi/.ssh/id_rsa")
        if id_rsa.exists():
            mode = id_rsa.stat().st_mode & 0o777
            assert mode == 0o600, f"id_rsa has permissions {oct(mode)}, expected 0600"

    def test_ssh_dir_permissions(self):
        """SSH directory should have 700 permissions."""
        ssh_dir = Path("/home/pi/.ssh")
        if ssh_dir.exists():
            mode = ssh_dir.stat().st_mode & 0o777
            assert mode == 0o700, f".ssh has permissions {oct(mode)}, expected 0700"

    def test_key_has_pi_identity(self):
        """SSH key comment should reference pi user, not root."""
        id_rsa_pub = Path("/home/pi/.ssh/id_rsa.pub")
        if id_rsa_pub.exists():
            content = id_rsa_pub.read_text()
            assert "root@" not in content, "SSH key has root@ identity (was generated as root)"


@skip_if_not_docker
class TestCellularSetup:
    """Verify cellular (WWAN) setup files exist."""

    def test_wwan_check_timer_exists(self):
        """Timer unit file should exist."""
        timer = Path("/etc/systemd/system/wwan-check.timer")
        assert timer.exists(), "wwan-check.timer not installed"

    def test_wwan_check_service_exists(self):
        """Service unit file should exist."""
        service = Path("/etc/systemd/system/wwan-check.service")
        assert service.exists(), "wwan-check.service not installed"

    def test_wwan_check_sh_exists(self):
        """Shell wrapper script should exist."""
        script = Path("/usr/local/bin/wwan-check.sh")
        assert script.exists(), "wwan-check.sh not installed"

    def test_wwan_check_sh_executable(self):
        """Shell wrapper should be executable."""
        script = Path("/usr/local/bin/wwan-check.sh")
        if script.exists():
            assert is_executable(script), "wwan-check.sh is not executable"

    def test_python_script_exists(self):
        """Python connection script should exist."""
        script = Path("/usr/local/bin/50_bring_wwan0_up.py")
        assert script.exists(), "50_bring_wwan0_up.py not installed"

    def test_python_script_executable(self):
        """Python script should be executable."""
        script = Path("/usr/local/bin/50_bring_wwan0_up.py")
        if script.exists():
            assert is_executable(script), "50_bring_wwan0_up.py is not executable"

    def test_wwan_network_config_exists(self):
        """WWAN network config should exist."""
        config = Path("/etc/systemd/network/20-wwan0.network")
        assert config.exists(), "20-wwan0.network not installed"

    def test_timer_enabled(self):
        """Timer should be enabled."""
        assert service_enabled("wwan-check.timer"), "wwan-check.timer not enabled"

    def test_dhcpcd_disabled(self):
        """dhcpcd should be disabled (conflicts with networkd)."""
        result = subprocess.run(
            ["systemctl", "is-enabled", "dhcpcd"],
            capture_output=True,
            text=True
        )
        # Can be "disabled" or "masked" or service might not exist
        assert result.stdout.strip() in ["disabled", "masked"] or result.returncode != 0, \
            "dhcpcd should be disabled"


@skip_if_not_docker
class TestWiFiSetup:
    """Verify WiFi setup files exist (run only if WiFi mode was used)."""

    @pytest.fixture(autouse=True)
    def check_wifi_mode(self):
        """Skip these tests if WiFi wasn't configured."""
        wpa_conf = Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
        if not wpa_conf.exists():
            pytest.skip("WiFi mode not configured (no wpa_supplicant config)")

    def test_wpa_supplicant_config_exists(self):
        """WPA supplicant config should exist."""
        config = Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
        assert config.exists()

    def test_wlan_network_config_exists(self):
        """WLAN network config should exist."""
        config = Path("/etc/systemd/network/10-wlan0.network")
        assert config.exists(), "10-wlan0.network not installed"

    def test_resolv_conf_is_symlink(self):
        """resolv.conf should be a symlink to systemd-resolved."""
        resolv = Path("/etc/resolv.conf")
        assert resolv.is_symlink(), "resolv.conf should be a symlink"
        target = os.readlink(resolv)
        assert "systemd/resolve" in target, f"resolv.conf points to {target}, expected systemd-resolved"


@skip_if_not_docker
class TestBrokkrSetup:
    """Verify Brokkr installation."""

    def test_venv_exists(self):
        """Virtual environment should exist."""
        venv = Path("/home/pi/ltgenv")
        assert venv.exists(), "ltgenv virtual environment not found"

    def test_venv_has_activate(self):
        """Venv should have activate script."""
        activate = Path("/home/pi/ltgenv/bin/activate")
        assert activate.exists(), "venv activate script not found"

    def test_brokkr_installed(self):
        """Brokkr should be importable in the venv."""
        result = subprocess.run(
            ["bash", "-c", "source /home/pi/ltgenv/bin/activate && python -c 'import brokkr'"],
            capture_output=True,
            text=True,
            cwd="/home/pi"
        )
        assert result.returncode == 0, f"Cannot import brokkr: {result.stderr}"

    def test_brokkr_cli_works(self):
        """Brokkr CLI should be available."""
        result = subprocess.run(
            ["bash", "-c", "source /home/pi/ltgenv/bin/activate && brokkr --version"],
            capture_output=True,
            text=True,
            cwd="/home/pi"
        )
        assert result.returncode == 0, f"brokkr --version failed: {result.stderr}"

    def test_brokkr_config_exists(self):
        """Brokkr config directory should exist."""
        config = Path("/home/pi/.config/brokkr")
        assert config.exists(), "Brokkr config directory not found"

    def test_brokkr_systempath_exists(self):
        """Brokkr systempath.toml should exist."""
        systempath = Path("/home/pi/.config/brokkr/systempath.toml")
        assert systempath.exists(), "systempath.toml not found"


@skip_if_not_docker
class TestHardwareSetup:
    """Verify hardware configuration files."""

    def test_sensor_ssh_config_exists(self):
        """Sensor SSH config should exist."""
        config = Path("/home/pi/.ssh/config")
        assert config.exists(), "SSH config not found"

    def test_sensor_ssh_config_has_hamma(self):
        """SSH config should have 'hamma' host entry."""
        config = Path("/home/pi/.ssh/config")
        if config.exists():
            content = config.read_text()
            assert "Host hamma" in content, "SSH config missing 'Host hamma' entry"

    def test_eth1_network_config_exists(self):
        """eth1 network config should exist."""
        config = Path("/etc/systemd/network/30-eth1.network")
        assert config.exists(), "30-eth1.network not installed"

    def test_automount_polkit_exists(self):
        """Automount polkit rules should exist."""
        polkit = Path("/etc/polkit-1/localauthority/50-local.d/10-udisks2-mount.pkla")
        # Might also be at different path depending on distro
        alt_polkit = Path("/etc/polkit-1/localauthority/50-local.d/mount-udisks.pkla")
        assert polkit.exists() or alt_polkit.exists(), "Automount polkit rules not found"


@skip_if_not_docker
class TestSystemdServices:
    """Verify systemd services are properly installed."""

    def test_networkd_enabled(self):
        """systemd-networkd should be enabled."""
        assert service_enabled("systemd-networkd"), "systemd-networkd not enabled"

    def test_resolved_enabled(self):
        """systemd-resolved should be enabled."""
        assert service_enabled("systemd-resolved"), "systemd-resolved not enabled"


# --- Summary Test ---

@skip_if_not_docker
class TestInstallSummary:
    """High-level summary tests - these are the minimum for a working install."""

    def test_essential_files_exist(self):
        """Check all essential files exist."""
        essential = [
            "/home/pi/.ssh/id_rsa",
            "/home/pi/.ssh/id_rsa.pub",
            "/home/pi/ltgenv/bin/activate",
            "/home/pi/.config/brokkr/systempath.toml",
        ]
        missing = [f for f in essential if not Path(f).exists()]
        assert not missing, f"Missing essential files: {missing}"

    def test_essential_ownership(self):
        """Check essential files are owned by pi."""
        paths = [
            "/home/pi/.ssh",
            "/home/pi/ltgenv",
            "/home/pi/.config/brokkr",
        ]
        wrong_owner = []
        for p in paths:
            path = Path(p)
            if path.exists():
                owner = get_owner(path)
                if owner != "pi":
                    wrong_owner.append(f"{p} owned by {owner}")
        assert not wrong_owner, f"Wrong ownership: {wrong_owner}"

    def test_no_root_artifacts(self):
        """Check for common root-owned artifacts that indicate bugs."""
        bad_paths = [
            "/root/.config/brokkr",  # Should not exist
        ]
        found = [p for p in bad_paths if Path(p).exists()]
        assert not found, f"Found root artifacts (indicates bug): {found}"
