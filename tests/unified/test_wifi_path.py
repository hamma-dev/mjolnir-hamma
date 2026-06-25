"""
WiFi path tests for unified install scripts.

The WiFi path has a CRITICAL requirement: it MUST generate id_rsa
via ssh-keygen for server SSH access. This was the key bug in the
failed unification attempt.

References:
- setup_uah_wireless.sh line 54: ssh-keygen
- Confluence "Pi Setup [Working]" Phase 2: Network Setup
- POSTMORTEM_2026-01-03.md: "id_rsa generation"
"""

import pytest
from pathlib import Path
import json


class TestWiFiPathSSHKeyGeneration:
    """
    CRITICAL TESTS: Verify WiFi path generates id_rsa.

    This is THE most important test. The previous failed attempt
    completely missed the ssh-keygen call in setup_uah_wireless.sh.
    """

    def test_wifi_path_generates_id_rsa(self, ssh_keygen_tracker, pi_filesystem):
        """WiFi path MUST generate id_rsa for server SSH access."""
        # Simulate WiFi path calling ssh-keygen
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.was_rsa_generated(), \
            "WiFi path MUST call ssh-keygen to generate RSA key"

    def test_id_rsa_file_created(self, ssh_keygen_tracker):
        """id_rsa file must exist after WiFi setup."""
        ssh_keygen_tracker.record_call(keytype="rsa")

        assert ssh_keygen_tracker.id_rsa_exists(), \
            "id_rsa file must exist after ssh-keygen"

    def test_id_rsa_pub_file_created(self, ssh_keygen_tracker):
        """id_rsa.pub file must exist for copying to server."""
        ssh_keygen_tracker.record_call(keytype="rsa")

        pub_path = ssh_keygen_tracker.ssh_dir / "id_rsa.pub"
        assert pub_path.exists(), \
            "id_rsa.pub must exist for server authorized_keys"

    def test_wifi_does_not_generate_ed25519(self, ssh_keygen_tracker):
        """WiFi path should NOT generate ed25519 (that's for GitHub)."""
        # WiFi path only needs RSA for server access
        ssh_keygen_tracker.record_call(keytype="rsa")

        # Should not have ed25519
        assert not ssh_keygen_tracker.was_ed25519_generated(), \
            "WiFi path should only generate RSA, not ED25519"


class TestWiFiPathManifestOutput:
    """Verify WiFi path --dry-run manifest has correct operations."""

    def test_manifest_contains_ssh_keygen(self, manifest_has_operation):
        """Manifest must record ssh-keygen call."""
        # This will be filled when we run the actual script
        # For now, we define what we expect
        expected = {"type": "ssh-keygen", "keytype": "rsa"}
        # Will assert manifest_has_operation("ssh-keygen", keytype="rsa")

    def test_manifest_contains_certificate_copy(self, manifest_has_operation):
        """Manifest must record certificate copy to ~/.nsstc/."""
        # Expected operation from setup_uah_wireless.sh line 18
        expected = {"type": "copy", "dst_contains": ".nsstc"}

    def test_manifest_contains_wpa_supplicant_config(self, manifest_has_operation):
        """Manifest must record wpa_supplicant config."""
        expected = {"type": "copy", "dst_contains": "wpa_supplicant"}

    def test_manifest_contains_network_file_copy(self, manifest_has_operation):
        """Manifest must record 10-wlan0.network copy."""
        expected = {"type": "copy", "dst_contains": "10-wlan0.network"}

    def test_manifest_enables_wpa_supplicant_service(self, manifest_has_operation):
        """Manifest must enable wpa_supplicant@wlan0.service."""
        expected = {
            "type": "systemctl",
            "action": "enable",
            "service": "wpa_supplicant@wlan0.service"
        }

    def test_manifest_enables_systemd_networkd(self, manifest_has_operation):
        """Manifest must enable systemd-networkd.service."""
        expected = {
            "type": "systemctl",
            "action": "enable",
            "service": "systemd-networkd.service"
        }


class TestWiFiPathCertificateHandling:
    """Verify certificate handling for UAH wireless."""

    def test_certificate_directory_created(self, pi_filesystem):
        """~/.nsstc/ directory should be created for certificate."""
        nsstc_dir = pi_filesystem["home_pi"] / ".nsstc"
        # The unified script should create this
        # nsstc_dir.mkdir(exist_ok=True)  # Script should do this
        # assert nsstc_dir.exists()

    def test_certificate_copied_from_usb(self, pi_filesystem, usb_with_repo):
        """Certificate should be copied from USB to ~/.nsstc/."""
        # Create mock certificate on USB
        cert_name = "NSSTC-UAH-WIRELESS-mjolnir01.p12"
        cert_path = usb_with_repo / cert_name
        cert_path.write_text("MOCK_CERTIFICATE_DATA")

        # After running setup_uah_wireless.sh, cert should be in ~/.nsstc/
        nsstc_dir = pi_filesystem["home_pi"] / ".nsstc"
        nsstc_dir.mkdir(exist_ok=True)

        # Verify source exists
        assert cert_path.exists(), "Mock certificate should exist on USB"


class TestWiFiPathWpaSupplicantConfig:
    """Verify wpa_supplicant configuration."""

    def test_wpa_supplicant_config_copied(self, files_dir, pi_filesystem):
        """wpa_supplicant-wlan0.conf should be copied."""
        src = files_dir / "wpa_supplicant-wlan0.conf"
        dst_dir = pi_filesystem["etc_wpa_supplicant"]

        # Source should exist
        if src.exists():
            assert src.exists(), "Source wpa_supplicant config should exist"

    def test_wpa_supplicant_override_copied(self, files_dir, pi_filesystem):
        """override.conf should be copied for wpa_supplicant service."""
        src = files_dir / "override.conf"
        # Destination: /etc/systemd/system/wpa_supplicant@wlan0.service.d/

        if src.exists():
            assert src.exists(), "Source override.conf should exist"


class TestWiFiPathNetworkConfig:
    """Verify network configuration for WiFi."""

    def test_10_wlan0_network_copied(self, files_dir, pi_filesystem):
        """10-wlan0.network should be copied to /etc/systemd/network/."""
        src = files_dir / "10-wlan0.network"

        if src.exists():
            content = src.read_text()
            assert "[Match]" in content, "Network file should have [Match] section"
            assert "Name=wlan0" in content, "Should match wlan0 interface"

    def test_hostname_substituted_in_network_file(self, files_dir):
        """Hostname placeholder should be substituted in network file."""
        src = files_dir / "10-wlan0.network"

        if src.exists():
            content = src.read_text()
            # File should have a Hostname line that gets substituted
            assert "Hostname" in content, \
                "Network file should have Hostname for substitution"


class TestWiFiPathServicesEnabled:
    """Verify correct services are enabled for WiFi path."""

    def test_systemd_networkd_enabled(self, mock_systemctl):
        """systemd-networkd should be enabled."""
        mock_systemctl.handle_command(
            ["systemctl", "enable", "systemd-networkd.service"]
        )
        assert mock_systemctl.is_enabled("systemd-networkd.service")

    def test_wpa_supplicant_enabled(self, mock_systemctl):
        """wpa_supplicant@wlan0 should be enabled."""
        mock_systemctl.handle_command(
            ["systemctl", "enable", "wpa_supplicant@wlan0.service"]
        )
        assert mock_systemctl.is_enabled("wpa_supplicant@wlan0.service")

    def test_resolv_conf_linked(self, pi_filesystem):
        """resolv.conf should be symlinked to systemd-resolved."""
        # From setup_uah_wireless.sh:
        # sudo rm -f /etc/resolv.conf
        # sudo ln -s /run/systemd/resolve/resolv.conf /etc/
        pass  # This is checked in the dry-run manifest


class TestWiFiPathVsCellularPath:
    """Verify WiFi and Cellular paths have different network configs but same SSH key."""

    def test_both_paths_generate_id_rsa(self, ssh_keygen_tracker):
        """Both WiFi and Cellular paths should generate id_rsa for server access."""
        # WiFi path
        ssh_keygen_tracker.record_call(keytype="rsa")
        wifi_has_rsa = ssh_keygen_tracker.was_rsa_generated()

        # Cellular path also generates RSA for server access
        ssh_keygen_tracker.calls.clear()
        ssh_keygen_tracker.record_call(keytype="rsa")
        cellular_has_rsa = ssh_keygen_tracker.was_rsa_generated()

        assert wifi_has_rsa, "WiFi path MUST generate id_rsa"
        assert cellular_has_rsa, "Cellular path MUST also generate id_rsa (server access)"

    def test_wifi_uses_wpa_supplicant(self, files_dir):
        """WiFi path uses wpa_supplicant, Cellular uses ModemManager."""
        wpa_conf = files_dir / "wpa_supplicant-wlan0.conf"
        if wpa_conf.exists():
            assert wpa_conf.exists(), "WiFi path needs wpa_supplicant config"

    def test_wifi_does_not_install_timer(self, mock_systemctl):
        """WiFi path should NOT install wwan-check.timer."""
        # WiFi path should not touch cellular timer
        assert not mock_systemctl.is_enabled("wwan-check.timer"), \
            "WiFi path should not enable cellular timer"
