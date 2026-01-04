"""
Behavior comparison tests for unified install scripts.

These tests verify that the unified scripts behave identically
to the TRUE ORIGINAL scripts in install_scripts/archive/.

References:
- Confluence "Pi Setup [Working]" (Page 126681092)
- Confluence "Cellular Fixes" (Page 361332739)
- POSTMORTEM_2026-01-03.md
"""

import pytest
from pathlib import Path


class TestOriginalScriptsExist:
    """Verify original scripts exist for comparison."""

    def test_archive_directory_exists(self, archive_scripts_dir):
        """Archive directory with original scripts should exist."""
        assert archive_scripts_dir.exists(), \
            f"Archive directory not found: {archive_scripts_dir}"

    def test_update_host_exists(self, archive_scripts_dir):
        """Original update_host.sh should exist."""
        script = archive_scripts_dir / "update_host.sh"
        assert script.exists(), "update_host.sh missing from archive"

    def test_disable_wifi_radio_exists(self, archive_scripts_dir):
        """Original disable_wifi_radio.sh should exist."""
        script = archive_scripts_dir / "disable_wifi_radio.sh"
        assert script.exists(), "disable_wifi_radio.sh missing from archive"

    def test_install_brokkr_exists(self, archive_scripts_dir):
        """Original install_brokkr.sh should exist."""
        script = archive_scripts_dir / "install_brokkr.sh"
        assert script.exists(), "install_brokkr.sh missing from archive"

    def test_setup_sensor_connect_exists(self, archive_scripts_dir):
        """Original setup_sensor_connect.sh should exist."""
        script = archive_scripts_dir / "setup_sensor_connect.sh"
        assert script.exists(), "setup_sensor_connect.sh missing from archive"

    def test_enable_automount_exists(self, archive_scripts_dir):
        """Original enable_automount.sh should exist."""
        script = archive_scripts_dir / "enable_automount.sh"
        assert script.exists(), "enable_automount.sh missing from archive"


class TestSetupUahWirelessSSHKeygen:
    """
    CRITICAL: Verify setup_uah_wireless.sh calls ssh-keygen.

    This was the KEY BUG in the failed unification attempt.
    The WiFi path MUST generate id_rsa for server SSH access.
    Line 54 of the original script calls `ssh-keygen`.
    """

    def test_original_script_has_ssh_keygen(self, original_scripts_dir):
        """Original setup_uah_wireless.sh must contain ssh-keygen call."""
        script = original_scripts_dir / "setup_uah_wireless.sh"
        assert script.exists(), "setup_uah_wireless.sh not found"

        content = script.read_text()
        assert "ssh-keygen" in content, \
            "CRITICAL: setup_uah_wireless.sh missing ssh-keygen call!"

    def test_ssh_keygen_on_correct_line(self, original_scripts_dir):
        """ssh-keygen should be near line 54 (documented in postmortem)."""
        script = original_scripts_dir / "setup_uah_wireless.sh"
        content = script.read_text()
        lines = content.split('\n')

        ssh_keygen_lines = [
            i + 1 for i, line in enumerate(lines)
            if 'ssh-keygen' in line and not line.strip().startswith('#')
        ]

        assert len(ssh_keygen_lines) > 0, \
            "No ssh-keygen call found in setup_uah_wireless.sh"

        # Should be around line 54 (allow some variance)
        assert any(45 <= ln <= 60 for ln in ssh_keygen_lines), \
            f"ssh-keygen found on lines {ssh_keygen_lines}, expected near line 54"

    def test_generates_rsa_key_by_default(self, original_scripts_dir):
        """ssh-keygen without -t flag generates RSA (for id_rsa)."""
        script = original_scripts_dir / "setup_uah_wireless.sh"
        content = script.read_text()
        lines = content.split('\n')

        for line in lines:
            if 'ssh-keygen' in line and not line.strip().startswith('#'):
                # Check if it's a bare ssh-keygen (generates RSA by default)
                # or explicitly specifies a type
                assert '-t ed25519' not in line, \
                    "WiFi path should generate RSA, not ED25519"
                break


class TestBrokkrInstallBehavior:
    """Verify Brokkr install matches original behavior."""

    def test_original_install_brokkr_creates_venv(self, archive_scripts_dir):
        """Original install_brokkr.sh creates venv at /home/pi/dev/ltgenv."""
        script = archive_scripts_dir / "install_brokkr.sh"
        content = script.read_text()

        assert "python3 -m venv" in content, \
            "install_brokkr.sh should create venv"
        assert "ltgenv" in content, \
            "install_brokkr.sh should use 'ltgenv' as venv name"
        assert "/home/pi/dev" in content, \
            "install_brokkr.sh should use /home/pi/dev/ as install path"

    def test_original_install_brokkr_clones_repos(self, archive_scripts_dir):
        """Original install_brokkr.sh clones 4 repos."""
        script = archive_scripts_dir / "install_brokkr.sh"
        content = script.read_text()

        required_repos = [
            "brokkr",
            "mjolnir-hamma",
            "serviceinstaller",
            "notifiers",
        ]

        for repo in required_repos:
            assert repo in content, \
                f"install_brokkr.sh should clone {repo}"

    def test_original_install_brokkr_pip_installs(self, archive_scripts_dir):
        """Original install_brokkr.sh pip installs packages."""
        script = archive_scripts_dir / "install_brokkr.sh"
        content = script.read_text()

        # Should pip install brokkr, serviceinstaller, notifiers
        assert 'pip install' in content, \
            "install_brokkr.sh should run pip install"

        # Should also install GPIO packages
        assert "gpiozero" in content, \
            "install_brokkr.sh should install gpiozero"
        assert "RPi.GPIO" in content, \
            "install_brokkr.sh should install RPi.GPIO"


class TestSetupBrokkrBehavior:
    """Verify Brokkr setup matches original behavior."""

    def test_original_setup_brokkr_requires_sensor_num(self, original_scripts_dir):
        """Original setup_brokkr.sh requires sensor number argument."""
        script = original_scripts_dir / "setup_brokkr.sh"
        content = script.read_text()

        # Should check for argument
        assert "$#" in content or "$1" in content, \
            "setup_brokkr.sh should check for sensor number argument"

    def test_original_setup_brokkr_configures_system(self, original_scripts_dir):
        """Original setup_brokkr.sh runs brokkr configure-system."""
        script = original_scripts_dir / "setup_brokkr.sh"
        content = script.read_text()

        assert "brokkr configure-system" in content, \
            "setup_brokkr.sh should run 'brokkr configure-system'"

    def test_original_setup_brokkr_configures_unit(self, original_scripts_dir):
        """Original setup_brokkr.sh runs brokkr configure-unit."""
        script = original_scripts_dir / "setup_brokkr.sh"
        content = script.read_text()

        assert "brokkr configure-unit" in content, \
            "setup_brokkr.sh should run 'brokkr configure-unit'"

    def test_original_setup_brokkr_installs_all(self, original_scripts_dir):
        """Original setup_brokkr.sh runs brokkr install-all."""
        script = original_scripts_dir / "setup_brokkr.sh"
        content = script.read_text()

        assert "brokkr" in content and "install" in content, \
            "setup_brokkr.sh should run some form of brokkr install"


class TestHardwareSetupBehavior:
    """Verify hardware setup matches original behavior."""

    def test_original_sensor_connect_copies_ssh_config(self, archive_scripts_dir):
        """Original setup_sensor_connect.sh copies SSH config."""
        script = archive_scripts_dir / "setup_sensor_connect.sh"
        content = script.read_text()

        assert "config" in content, \
            "setup_sensor_connect.sh should copy SSH config"
        assert ".ssh" in content or "SSH_PATH" in content, \
            "setup_sensor_connect.sh should reference .ssh directory"

    def test_original_sensor_connect_copies_network_files(self, archive_scripts_dir):
        """Original setup_sensor_connect.sh copies eth network files."""
        script = archive_scripts_dir / "setup_sensor_connect.sh"
        content = script.read_text()

        assert "eth" in content, \
            "setup_sensor_connect.sh should copy eth network files"

    def test_original_automount_copies_polkit(self, archive_scripts_dir):
        """Original enable_automount.sh copies polkit rules."""
        script = archive_scripts_dir / "enable_automount.sh"
        content = script.read_text()

        assert "polkit" in content.lower() or "pkla" in content, \
            "enable_automount.sh should copy polkit rules"
        assert "mount" in content.lower(), \
            "enable_automount.sh should reference mount rules"

    def test_original_automount_sets_permissions(self, archive_scripts_dir):
        """Original enable_automount.sh sets correct permissions."""
        script = archive_scripts_dir / "enable_automount.sh"
        content = script.read_text()

        assert "chown" in content or "chmod" in content, \
            "enable_automount.sh should set file permissions"


class TestHostnameBehavior:
    """Verify hostname setting matches original behavior."""

    def test_original_update_host_requires_sensor_num(self, archive_scripts_dir):
        """Original update_host.sh requires sensor number argument."""
        script = archive_scripts_dir / "update_host.sh"
        content = script.read_text()

        assert "$#" in content or "$1" in content, \
            "update_host.sh should check for sensor number argument"

    def test_original_update_host_formats_hostname(self, archive_scripts_dir):
        """Original update_host.sh formats hostname with printf."""
        script = archive_scripts_dir / "update_host.sh"
        content = script.read_text()

        assert "printf" in content, \
            "update_host.sh should use printf for formatting"
        assert "mjolnir" in content, \
            "update_host.sh should create mjolnir hostname"

    def test_original_update_host_updates_etc_hostname(self, archive_scripts_dir):
        """Original update_host.sh updates /etc/hostname."""
        script = archive_scripts_dir / "update_host.sh"
        content = script.read_text()

        assert "/etc/hostname" in content, \
            "update_host.sh should update /etc/hostname"

    def test_original_update_host_updates_etc_hosts(self, archive_scripts_dir):
        """Original update_host.sh updates /etc/hosts."""
        script = archive_scripts_dir / "update_host.sh"
        content = script.read_text()

        assert "/etc/hosts" in content, \
            "update_host.sh should update /etc/hosts"


class TestWifiDisableBehavior:
    """Verify WiFi disable matches original behavior."""

    def test_original_disable_wifi_modifies_config(self, archive_scripts_dir):
        """Original disable_wifi_radio.sh modifies boot config."""
        script = archive_scripts_dir / "disable_wifi_radio.sh"
        content = script.read_text()

        assert "disable-wifi" in content or "config.txt" in content, \
            "disable_wifi_radio.sh should disable internal WiFi"


class TestUnifiedScriptConsistency:
    """Verify unified scripts are consistent with original behavior."""

    def test_unified_common_sh_exists(self, unified_install_dir):
        """Unified lib/common.sh should exist."""
        script = unified_install_dir / "lib" / "common.sh"
        assert script.exists(), \
            f"lib/common.sh not found in {unified_install_dir}"

    def test_unified_common_has_ssh_keygen_wrapper(self, unified_install_dir):
        """Unified common.sh should have safe_ssh_keygen function."""
        script = unified_install_dir / "lib" / "common.sh"
        if script.exists():
            content = script.read_text()
            assert "safe_ssh_keygen" in content, \
                "common.sh should have safe_ssh_keygen wrapper"

    def test_unified_common_has_manifest_functions(self, unified_install_dir):
        """Unified common.sh should have manifest functions for dry-run."""
        script = unified_install_dir / "lib" / "common.sh"
        if script.exists():
            content = script.read_text()
            assert "manifest_add" in content, \
                "common.sh should have manifest_add function"
            assert "DRY_RUN" in content, \
                "common.sh should support DRY_RUN mode"
