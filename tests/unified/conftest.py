"""
Pytest fixtures for unified install script tests.

These fixtures provide a complete mock environment for testing
the unified install scripts without modifying the real system.
"""

import os
import sys
import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from typing import Generator

# Add fixtures directory to path
TESTS_DIR = Path(__file__).parent.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
sys.path.insert(0, str(FIXTURES_DIR))

from mock_hardware import (
    MockModem,
    MockSystemctl,
    MockNetwork,
    MockDisk,
    MockSSHKeygen,
    MockEnvironment,
    create_mock_subprocess_runner,
)
from mock_ssh import SSHKeygenTracker


# --- Path fixtures ---

@pytest.fixture
def repo_root() -> Path:
    """Return the repository root path."""
    return TESTS_DIR.parent


@pytest.fixture
def unified_install_dir(repo_root) -> Path:
    """Return the unified_install directory path."""
    return repo_root / "unified_install"


@pytest.fixture
def original_scripts_dir(repo_root) -> Path:
    """Return the original install_scripts directory path."""
    return repo_root / "install_scripts"


@pytest.fixture
def archive_scripts_dir(original_scripts_dir) -> Path:
    """Return the archive directory with TRUE ORIGINAL scripts."""
    return original_scripts_dir / "archive"


@pytest.fixture
def files_dir(repo_root) -> Path:
    """Return the files directory with config templates."""
    return repo_root / "files"


# --- Mock environment fixtures ---

@pytest.fixture
def mock_env(tmp_path) -> MockEnvironment:
    """Create a complete mock environment."""
    env = MockEnvironment(tmp_path)
    env.setup_standard_interfaces()
    return env


@pytest.fixture
def mock_modem() -> MockModem:
    """Create a standalone mock modem."""
    return MockModem()


@pytest.fixture
def mock_systemctl() -> MockSystemctl:
    """Create a standalone mock systemctl."""
    return MockSystemctl()


@pytest.fixture
def mock_network(tmp_path) -> MockNetwork:
    """Create a standalone mock network."""
    return MockNetwork(tmp_path)


@pytest.fixture
def mock_disk(tmp_path) -> MockDisk:
    """Create a standalone mock disk."""
    return MockDisk(tmp_path)


@pytest.fixture
def ssh_keygen_tracker(tmp_path) -> SSHKeygenTracker:
    """Create SSH keygen tracker for verifying key generation."""
    ssh_dir = tmp_path / "home" / "pi" / ".ssh"
    return SSHKeygenTracker(ssh_dir)


# --- Filesystem fixtures ---

@pytest.fixture
def pi_filesystem(tmp_path) -> dict:
    """Create a mock Pi filesystem structure.

    Returns a dict with paths to key directories.
    """
    dirs = {
        "root": tmp_path,
        "home_pi": tmp_path / "home" / "pi",
        "home_pi_dev": tmp_path / "home" / "pi" / "dev",
        "home_pi_ssh": tmp_path / "home" / "pi" / ".ssh",
        "etc_systemd_network": tmp_path / "etc" / "systemd" / "network",
        "etc_systemd_system": tmp_path / "etc" / "systemd" / "system",
        "etc_wpa_supplicant": tmp_path / "etc" / "wpa_supplicant",
        "etc_polkit": tmp_path / "etc" / "polkit-1" / "localauthority" / "50-local.d",
        "usr_local_bin": tmp_path / "usr" / "local" / "bin",
        "mnt_usb": tmp_path / "mnt" / "usb",
        "boot": tmp_path / "boot",
        "sys_class_net": tmp_path / "sys" / "class" / "net",
    }

    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    # Create boot/config.txt
    (dirs["boot"] / "config.txt").write_text("# Raspberry Pi config\n")

    # Create /etc/hostname
    (tmp_path / "etc" / "hostname").write_text("raspberrypi\n")

    # Create /etc/hosts
    (tmp_path / "etc" / "hosts").write_text(
        "127.0.0.1\tlocalhost\n"
        "127.0.1.1\traspberrypi\n"
    )

    return dirs


@pytest.fixture
def usb_with_repo(pi_filesystem, repo_root) -> Path:
    """Create USB mount with mjolnir-hamma repo structure."""
    usb_path = pi_filesystem["mnt_usb"]

    # Create repo structure
    repo_dir = usb_path / "mjolnir-hamma"
    (repo_dir / "install_scripts").mkdir(parents=True)
    (repo_dir / "files").mkdir(parents=True)
    (repo_dir / "scripts").mkdir(parents=True)
    (repo_dir / "unified_install" / "lib").mkdir(parents=True)

    # Copy actual files from repo
    src_files = repo_root / "files"
    if src_files.exists():
        for f in src_files.iterdir():
            if f.is_file():
                (repo_dir / "files" / f.name).write_text(f.read_text())

    return usb_path


# --- Manifest fixtures ---

@pytest.fixture
def manifest_path(tmp_path) -> Path:
    """Return path for install manifest."""
    return tmp_path / "install_manifest.json"


@pytest.fixture
def load_manifest(manifest_path):
    """Return function to load and parse manifest."""
    def _load():
        if not manifest_path.exists():
            return {"operations": []}
        return json.loads(manifest_path.read_text())
    return _load


@pytest.fixture
def manifest_has_operation(load_manifest):
    """Return function to check if manifest has specific operation."""
    def _check(op_type: str, **kwargs) -> bool:
        manifest = load_manifest()
        for op in manifest.get("operations", []):
            if op.get("type") != op_type:
                continue
            # Check all kwargs match
            if all(op.get(k) == v for k, v in kwargs.items()):
                return True
        return False
    return _check


@pytest.fixture
def manifest_count_operations(load_manifest):
    """Return function to count operations of a type."""
    def _count(op_type: str) -> int:
        manifest = load_manifest()
        return sum(1 for op in manifest.get("operations", []) if op.get("type") == op_type)
    return _count


# --- Script execution fixtures ---

@pytest.fixture
def run_script(pi_filesystem, manifest_path, mock_env):
    """Return function to run a shell script in mock environment."""
    def _run(script_path: str, args: list = None, env_vars: dict = None, dry_run: bool = True):
        if args is None:
            args = []

        # Set up environment
        env = os.environ.copy()
        env["HOME"] = str(pi_filesystem["home_pi"])
        env["MANIFEST_FILE"] = str(manifest_path)

        if dry_run:
            env["DRY_RUN"] = "true"
            args = ["--dry-run"] + args

        if env_vars:
            env.update(env_vars)

        # Run the script
        result = subprocess.run(
            ["bash", script_path] + args,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(pi_filesystem["root"]),
        )

        return result

    return _run


# --- Expected values fixtures ---

@pytest.fixture
def expected_wifi_operations():
    """Expected operations for WiFi path."""
    return [
        {"type": "copy", "dst_contains": "wpa_supplicant"},
        {"type": "copy", "dst_contains": "10-wlan0.network"},
        {"type": "ssh-keygen", "keytype": "rsa"},  # CRITICAL
        {"type": "systemctl", "action": "enable", "service_contains": "wpa_supplicant"},
        {"type": "systemctl", "action": "enable", "service_contains": "systemd-networkd"},
    ]


@pytest.fixture
def expected_cellular_operations():
    """Expected operations for Cellular path."""
    return [
        {"type": "systemctl", "action": "disable", "service_contains": "dhcpcd"},
        {"type": "copy", "dst_contains": "wwan-check.timer"},
        {"type": "copy", "dst_contains": "wwan-check.service"},
        {"type": "copy", "dst_contains": "wwan-check.sh"},
        {"type": "copy", "dst_contains": "50_bring_wwan0_up.py"},
        {"type": "systemctl", "action": "enable", "service_contains": "wwan-check.timer"},
    ]


@pytest.fixture
def expected_brokkr_operations():
    """Expected operations for Brokkr installation."""
    return [
        {"type": "command", "cmd_contains": "python3 -m venv"},
        {"type": "git_clone", "repo_contains": "brokkr"},
        {"type": "git_clone", "repo_contains": "serviceinstaller"},
        {"type": "git_clone", "repo_contains": "notifiers"},
        {"type": "pip_install", "package_contains": "brokkr"},
        {"type": "pip_install", "package_contains": "serviceinstaller"},
        {"type": "pip_install", "package_contains": "notifiers"},
        {"type": "command", "cmd_contains": "brokkr configure-system"},
        {"type": "command", "cmd_contains": "brokkr configure-unit"},
        {"type": "command", "cmd_contains": "brokkr install"},
    ]


@pytest.fixture
def expected_hardware_operations():
    """Expected operations for hardware setup."""
    return [
        {"type": "copy", "dst_contains": ".ssh/config"},
        {"type": "copy", "dst_contains": "30-eth1.network"},
        {"type": "copy", "dst_contains": "40-eth0.network"},
        {"type": "copy", "dst_contains": "mount-udisks.pkla"},
    ]
