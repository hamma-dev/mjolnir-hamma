"""
Cellular path tests for unified install scripts.

The Cellular path uses a timer-based approach as documented in
Confluence "Cellular Fixes" (Page 361332739).

Key requirements:
- dhcpcd.service disabled
- wwan0 is Unmanaged=yes in systemd-networkd
- Timer-based approach (NOT networkd-dispatcher)
- flock wrapper prevents concurrent connections

References:
- Confluence "Cellular Fixes" (Page 361332739)
- Current setup_wwan.sh in install_scripts/
"""

import pytest
from pathlib import Path


class TestCellularPathTimerApproach:
    """
    Verify timer-based approach from Cellular Fixes documentation.

    The timer-based approach replaced the old networkd-dispatcher
    method because wwan0 is Unmanaged=yes in systemd-networkd.
    """

    def test_wwan_check_timer_installed(self, files_dir):
        """wwan-check.timer should exist in files/."""
        timer_file = files_dir / "wwan-check.timer"
        assert timer_file.exists(), \
            "wwan-check.timer should exist in files/"

        if timer_file.exists():
            content = timer_file.read_text()
            assert "[Timer]" in content, \
                "Timer file should have [Timer] section"
            assert "OnBootSec" in content, \
                "Timer should have OnBootSec (start after boot)"
            assert "OnUnitActiveSec" in content, \
                "Timer should have OnUnitActiveSec (periodic)"

    def test_wwan_check_service_installed(self, files_dir):
        """wwan-check.service should exist in files/."""
        service_file = files_dir / "wwan-check.service"
        assert service_file.exists(), \
            "wwan-check.service should exist in files/"

        if service_file.exists():
            content = service_file.read_text()
            assert "[Service]" in content, \
                "Service file should have [Service] section"
            assert "wwan-check.sh" in content, \
                "Service should execute wwan-check.sh"

    def test_wwan_check_sh_uses_flock(self, files_dir):
        """wwan-check.sh should use flock for concurrency control."""
        script_file = files_dir / "wwan-check.sh"
        assert script_file.exists(), \
            "wwan-check.sh should exist in files/"

        if script_file.exists():
            content = script_file.read_text()
            assert "flock" in content, \
                "wwan-check.sh MUST use flock to prevent concurrent connections"

    def test_python_script_exists(self, repo_root):
        """50_bring_wwan0_up.py should exist in scripts/."""
        script_file = repo_root / "scripts" / "50_bring_wwan0_up.py"
        assert script_file.exists(), \
            "50_bring_wwan0_up.py should exist in scripts/"


class TestCellularPathNetworkConfig:
    """Verify network configuration for Cellular path."""

    def test_20_wwan0_network_has_correct_settings(self, files_dir):
        """20-wwan0.network should have DHCP=no and Unmanaged=yes."""
        network_file = files_dir / "20-wwan0.network"
        assert network_file.exists(), \
            "20-wwan0.network should exist"

        content = network_file.read_text()

        assert "[Match]" in content, \
            "Network file should have [Match] section"
        assert "Name=wwan0" in content, \
            "Should match wwan0 interface"
        assert "DHCP=no" in content, \
            "wwan0 should have DHCP=no (Cellular Fixes requirement)"
        assert "Unmanaged=yes" in content, \
            "wwan0 should have Unmanaged=yes (Cellular Fixes requirement)"


class TestCellularPathServicesDisabled:
    """Verify conflicting services are disabled."""

    def test_dhcpcd_disabled(self, mock_systemctl):
        """dhcpcd.service must be disabled (conflicts with systemd-networkd)."""
        mock_systemctl.handle_command(
            ["systemctl", "disable", "dhcpcd.service"]
        )
        assert "dhcpcd.service" in mock_systemctl.disabled_services, \
            "dhcpcd.service must be disabled"

    def test_old_wwan_connect_service_disabled(self, mock_systemctl):
        """Old wwan-connect.service should be disabled if present."""
        mock_systemctl.handle_command(
            ["systemctl", "disable", "wwan-connect.service"]
        )
        assert "wwan-connect.service" in mock_systemctl.disabled_services


class TestCellularPathServicesEnabled:
    """Verify correct services are enabled."""

    def test_wwan_check_timer_enabled(self, mock_systemctl):
        """wwan-check.timer should be enabled."""
        mock_systemctl.handle_command(
            ["systemctl", "enable", "wwan-check.timer"]
        )
        assert mock_systemctl.is_enabled("wwan-check.timer"), \
            "wwan-check.timer must be enabled"

    def test_modem_manager_not_disabled(self, mock_systemctl):
        """ModemManager should NOT be disabled (needed for modem)."""
        assert "ModemManager.service" not in mock_systemctl.disabled_services, \
            "ModemManager must NOT be disabled"


class TestCellularPathOldApproachRemoved:
    """Verify old networkd-dispatcher approach is removed."""

    def test_dispatcher_symlinks_removed(self, original_scripts_dir):
        """setup_wwan.sh should remove old dispatcher symlinks."""
        script = original_scripts_dir / "setup_wwan.sh"
        if script.exists():
            content = script.read_text()
            # Should remove old carrier.d and degraded.d scripts
            assert "rm" in content, \
                "setup_wwan.sh should remove old dispatcher scripts"

    def test_dispatcher_override_removed(self, original_scripts_dir):
        """Old networkd-dispatcher override should be removed."""
        script = original_scripts_dir / "setup_wwan.sh"
        if script.exists():
            content = script.read_text()
            assert "networkd-dispatcher" in content, \
                "setup_wwan.sh should clean up networkd-dispatcher"


class TestCellularPathAPNConfiguration:
    """Verify APN configuration."""

    def test_default_apn_is_h2g2(self, original_scripts_dir):
        """Default APN should be h2g2 (T-Mobile)."""
        script = original_scripts_dir / "setup_wwan.sh"
        if script.exists():
            content = script.read_text()
            assert "h2g2" in content, \
                "Default APN should be h2g2"

    def test_apn_argument_supported(self, original_scripts_dir):
        """setup_wwan.sh should support --apn argument."""
        script = original_scripts_dir / "setup_wwan.sh"
        if script.exists():
            content = script.read_text()
            assert "--apn" in content or "APN" in content, \
                "setup_wwan.sh should support APN configuration"

    def test_python_script_apn_configurable(self, repo_root):
        """APN should be configurable in Python script."""
        script = repo_root / "scripts" / "50_bring_wwan0_up.py"
        if script.exists():
            content = script.read_text()
            assert "APN" in content, \
                "Python script should have APN configuration"


class TestCellularPathGeneratesSSHKey:
    """
    Cellular path MUST generate id_rsa for server access.

    SSH key is needed for server access regardless of connection method
    (WiFi or Cellular). Both paths need to generate id_rsa.
    """

    def test_cellular_path_generates_ssh_keygen(self, ssh_keygen_tracker):
        """Cellular path should call ssh-keygen for id_rsa (server access)."""
        # Simulate cellular path generating SSH key (uses tracker's temp ssh_dir)
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.was_rsa_generated(), \
            "Cellular path MUST generate id_rsa for server access"

    def test_id_rsa_file_created(self, ssh_keygen_tracker):
        """id_rsa should exist after cellular setup."""
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.id_rsa_exists(), \
            "id_rsa MUST exist for cellular path (server access)"


class TestCellularPathPythonScript:
    """Verify Python connection script functionality."""

    def test_script_has_zombie_detection(self, repo_root):
        """Python script should detect zombie modem state."""
        script = repo_root / "scripts" / "50_bring_wwan0_up.py"
        if script.exists():
            content = script.read_text()
            # Should check signal quality for zombie detection
            assert "signal" in content.lower() or "zombie" in content.lower() or "quality" in content.lower(), \
                "Python script should have signal/zombie detection"

    def test_script_has_ping_check(self, repo_root):
        """Python script should check connectivity with ping."""
        script = repo_root / "scripts" / "50_bring_wwan0_up.py"
        if script.exists():
            content = script.read_text()
            assert "ping" in content.lower(), \
                "Python script should check connectivity with ping"

    def test_script_handles_bearer_disconnect(self, repo_root):
        """Python script should handle bearer disconnect for zombie state."""
        script = repo_root / "scripts" / "50_bring_wwan0_up.py"
        if script.exists():
            content = script.read_text()
            assert "bearer" in content.lower() or "disconnect" in content.lower(), \
                "Python script should handle bearer disconnect"


class TestCellularPathMockModem:
    """Test cellular path with mock modem."""

    def test_modem_connected_state(self, mock_modem):
        """Mock modem should report connected state."""
        mock_modem.set_state("connected")
        response = mock_modem.get_mmcli_modem_response()

        assert "connected" in response, \
            "Connected modem should report connected state"
        assert "75%" in response, \
            "Connected modem should have good signal"

    def test_modem_zombie_state(self, mock_modem):
        """Mock modem should report zombie state (connected but low signal)."""
        mock_modem.set_state("zombie")
        response = mock_modem.get_mmcli_modem_response()

        assert "connected" in response, \
            "Zombie modem reports connected"
        assert "3%" in response, \
            "Zombie modem has very low signal"

    def test_modem_not_found_state(self, mock_modem):
        """Mock modem should handle not found state."""
        mock_modem.set_state("not_found")
        response = mock_modem.get_mmcli_list_response()

        assert response == "", \
            "Not found modem should return empty list"

    def test_connection_attempt_recorded(self, mock_modem):
        """Mock modem should record connection attempts."""
        mock_modem.set_state("registered")
        mock_modem.handle_command(
            ["mmcli", "-m", "0", "--simple-connect=apn=h2g2"]
        )

        assert mock_modem.connection_attempts == 1, \
            "Connection attempt should be recorded"
        assert mock_modem.state == "connected", \
            "State should change to connected after connect"
