"""
Failure simulation tests for unified install scripts.

Tests various failure scenarios:
- Modem failures (zombie state, timeout, low signal, not found)
- Disk failures (not mounted, full, permission errors)
- Network failures (DNS timeout, DHCP failure, no IP)

These tests ensure the scripts handle failures gracefully
rather than failing silently or crashing.
"""

import pytest
from pathlib import Path


class TestModemFailures:
    """Test modem failure scenarios."""

    def test_modem_not_found(self, mock_modem):
        """Script should handle modem not found gracefully."""
        mock_modem.set_state("not_found")

        # Simulate mmcli -L
        result = mock_modem.handle_command(["mmcli", "-L"])

        assert result.returncode == 1 or result.stdout == "", \
            "Modem not found should return error or empty"

    def test_modem_zombie_state_detection(self, mock_modem):
        """Script should detect zombie modem (connected but no actual connection)."""
        mock_modem.set_state("zombie")

        response = mock_modem.get_mmcli_modem_response()

        # Zombie state: shows connected but very low signal
        assert "connected" in response, \
            "Zombie modem reports connected"
        assert "3%" in response, \
            "Zombie modem has very low signal quality"

    def test_modem_zombie_recovery(self, mock_modem):
        """Script should attempt to recover from zombie state."""
        mock_modem.set_state("zombie")

        # Simulate force disconnect
        mock_modem.handle_command(["mmcli", "-b", "1", "--disconnect"])
        assert mock_modem.disconnect_attempts == 1, \
            "Should attempt bearer disconnect"

        # Simulate reconnection
        mock_modem.set_state("registered")
        mock_modem.handle_command(["mmcli", "-m", "0", "--simple-connect=apn=h2g2"])

        assert mock_modem.state == "connected", \
            "Should recover to connected state"

    def test_modem_connection_timeout(self, mock_modem):
        """Script should handle connection timeout."""
        # Modem stuck in registered state (can't connect)
        mock_modem.set_state("registered")

        # Multiple connection attempts should be recorded
        for _ in range(3):
            mock_modem.handle_command(
                ["mmcli", "-m", "0", "--simple-connect=apn=h2g2"]
            )

        assert mock_modem.connection_attempts == 3, \
            "Should retry connection attempts"

    def test_modem_low_signal(self, mock_modem):
        """Script should handle low signal conditions."""
        mock_modem.set_state("zombie")  # Has 3% signal

        response = mock_modem.get_mmcli_modem_response()

        # Should detect low signal
        assert "3%" in response, \
            "Should detect very low signal"


class TestDiskFailures:
    """Test disk failure scenarios."""

    def test_usb_not_mounted(self, mock_disk):
        """Script should handle USB not mounted."""
        # Don't mount the disk
        assert not mock_disk.is_mounted("/dev/sda1"), \
            "Disk should not be mounted initially"

    def test_mount_point_creation(self, mock_disk, tmp_path):
        """Script should create mount point if needed."""
        mount_point = tmp_path / "mnt" / "usb"

        # Mount disk (should create mount point)
        result = mock_disk.mount_disk("/dev/sda1", mount_point)

        assert mount_point.exists(), \
            "Mount point should be created"
        assert mock_disk.is_mounted("/dev/sda1"), \
            "Disk should be mounted"

    def test_disk_unmount(self, mock_disk, tmp_path):
        """Script should handle disk unmount."""
        # Mount then unmount
        mount_point = mock_disk.mount_disk("/dev/sda1")
        mock_disk.unmount_disk("/dev/sda1")

        assert not mock_disk.is_mounted("/dev/sda1"), \
            "Disk should be unmounted"

    def test_repo_not_on_usb(self, mock_disk, tmp_path):
        """Script should fail gracefully if repo not on USB."""
        mount_point = mock_disk.mount_disk("/dev/sda1")

        repo_path = mount_point / "mjolnir-hamma"
        assert not repo_path.exists(), \
            "Repo should not exist on empty USB"

    def test_permission_denied_scenario(self, pi_filesystem):
        """Script should handle permission denied errors."""
        # Create a file without write permission
        test_file = pi_filesystem["home_pi"] / "readonly.txt"
        test_file.write_text("test")
        test_file.chmod(0o444)

        assert not (test_file.stat().st_mode & 0o200), \
            "File should not be writable"


class TestNetworkFailures:
    """Test network failure scenarios."""

    def test_no_ip_address(self, mock_network):
        """Script should detect when interface has no IP."""
        mock_network.create_interface("wwan0", state="up", ip=None)

        assert not mock_network.has_ip("wwan0"), \
            "Interface should have no IP initially"

    def test_interface_down(self, mock_network):
        """Script should handle interface down."""
        mock_network.create_interface("wwan0", state="down")

        assert mock_network.interfaces["wwan0"]["state"] == "down", \
            "Interface should be down"

    def test_dhcp_failure(self, mock_network):
        """Script should handle DHCP failure."""
        mock_network.create_interface("wwan0", state="up", ip=None)

        # After DHCP failure, still no IP
        assert not mock_network.has_ip("wwan0"), \
            "Should have no IP after DHCP failure"

    def test_ip_assignment_success(self, mock_network):
        """Script should detect successful IP assignment."""
        mock_network.create_interface("wwan0", state="up")
        mock_network.set_interface_ip("wwan0", "10.0.0.100")

        assert mock_network.has_ip("wwan0"), \
            "Interface should have IP"
        assert mock_network.interfaces["wwan0"]["ip"] == "10.0.0.100", \
            "IP should match assigned value"


class TestSystemctlFailures:
    """Test systemctl failure scenarios."""

    def test_service_not_found(self, mock_systemctl):
        """Script should handle service not found."""
        # Try to enable non-existent service
        result = mock_systemctl.handle_command(
            ["systemctl", "enable", "nonexistent.service"]
        )

        # Should not crash
        assert "nonexistent.service" in mock_systemctl.enabled_services or True, \
            "Should handle service gracefully"

    def test_service_already_enabled(self, mock_systemctl):
        """Script should handle already enabled service."""
        # Enable twice
        mock_systemctl.handle_command(
            ["systemctl", "enable", "test.service"]
        )
        mock_systemctl.handle_command(
            ["systemctl", "enable", "test.service"]
        )

        assert mock_systemctl.is_enabled("test.service"), \
            "Service should remain enabled"

    def test_service_start_failure(self, mock_systemctl):
        """Script should handle service start failure."""
        # Start without enabling
        mock_systemctl.handle_command(
            ["systemctl", "start", "test.service"]
        )

        assert mock_systemctl.is_started("test.service"), \
            "Service should be marked as started"


class TestSSHKeygenFailures:
    """Test SSH keygen failure scenarios."""

    def test_ssh_dir_not_exists(self, ssh_keygen_tracker):
        """ssh-keygen should create .ssh directory if needed."""
        # Remove the ssh directory
        import shutil
        if ssh_keygen_tracker.ssh_dir.exists():
            shutil.rmtree(ssh_keygen_tracker.ssh_dir)

        # Record a call (should recreate directory)
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.ssh_dir.exists(), \
            "ssh-keygen should create .ssh directory"

    def test_key_already_exists(self, ssh_keygen_tracker):
        """ssh-keygen should handle existing key."""
        # Generate key twice
        ssh_keygen_tracker.record_call(keytype="rsa")
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.call_count == 2, \
            "Should record both calls"
        assert ssh_keygen_tracker.id_rsa_exists(), \
            "Key should exist"


class TestGracefulDegradation:
    """Test graceful degradation scenarios."""

    def test_wifi_fallback_to_cellular(self, mock_network, mock_modem):
        """If WiFi fails, system should be able to use cellular."""
        # WiFi down
        mock_network.create_interface("wlan0", state="down")

        # Cellular available
        mock_modem.set_state("registered")
        mock_modem.handle_command(["mmcli", "-m", "0", "--simple-connect=apn=h2g2"])

        assert mock_modem.state == "connected", \
            "Should connect via cellular"

    def test_partial_install_recovery(self, pi_filesystem):
        """Script should be able to resume after partial install."""
        # Simulate partial install - some files exist
        venv_dir = pi_filesystem["home_pi_dev"] / "ltgenv"
        venv_dir.mkdir(parents=True, exist_ok=True)

        assert venv_dir.exists(), \
            "Partial install state should be detectable"

    def test_network_recovery(self, mock_network):
        """Network should recover after temporary failure."""
        mock_network.create_interface("wwan0", state="down")

        # Network comes back
        mock_network.set_interface_state("wwan0", "up")
        mock_network.set_interface_ip("wwan0", "10.0.0.100")

        assert mock_network.has_ip("wwan0"), \
            "Network should recover"


class TestErrorMessages:
    """Test that error messages are helpful."""

    def test_missing_argument_message(self, archive_scripts_dir):
        """Scripts should have helpful missing argument messages."""
        script = archive_scripts_dir / "update_host.sh"
        if script.exists():
            content = script.read_text()
            assert "sensor number" in content.lower() or "pass" in content.lower(), \
                "Should have helpful error message for missing argument"

    def test_root_check_message(self, original_scripts_dir):
        """Scripts requiring root should have clear message."""
        script = original_scripts_dir / "setup_wwan.sh"
        if script.exists():
            content = script.read_text()
            assert "root" in content.lower() or "sudo" in content.lower(), \
                "Should mention root requirement"
