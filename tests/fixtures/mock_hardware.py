"""
Mock hardware fixtures for testing HAMMA Pi install scripts.

Provides simulation of:
- Cellular modem (mmcli responses)
- Systemctl service management
- Network interfaces
- USB disk operations
"""

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch
import json


@dataclass
class CommandRecord:
    """Record of a command that was executed."""
    command: List[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class MockModem:
    """Simulate cellular modem with ModemManager responses.

    Supports simulating:
    - Connected state
    - Registered state (not connected)
    - Zombie state (connected but no actual connection)
    - Modem not found
    - Low signal conditions
    """

    MODEM_LIST_TEMPLATE = "/org/freedesktop/ModemManager1/Modem/{index} [Quectel] EG25-G\n"

    MODEM_STATES = {
        "connected": {
            "state": "connected",
            "power_state": "on",
            "access_tech": "lte",
            "signal_quality": 75,
        },
        "registered": {
            "state": "registered",
            "power_state": "on",
            "access_tech": "lte",
            "signal_quality": 54,
        },
        "zombie": {
            "state": "connected",  # Shows connected but...
            "power_state": "on",
            "access_tech": "lte",
            "signal_quality": 3,  # Very low signal indicates zombie
        },
        "disconnected": {
            "state": "disabled",
            "power_state": "on",
            "access_tech": "unknown",
            "signal_quality": 0,
        },
        "not_found": None,  # Special case - no modem
    }

    def __init__(self, initial_state: str = "connected", modem_index: int = 0):
        self.state = initial_state
        self.modem_index = modem_index
        self.command_history: List[CommandRecord] = []
        self.connection_attempts = 0
        self.disconnect_attempts = 0

    def set_state(self, state: str):
        """Change modem state."""
        if state not in self.MODEM_STATES:
            raise ValueError(f"Unknown state: {state}. Valid: {list(self.MODEM_STATES.keys())}")
        self.state = state

    def get_mmcli_list_response(self) -> str:
        """Response for 'mmcli -L'."""
        if self.state == "not_found":
            return ""
        return self.MODEM_LIST_TEMPLATE.format(index=self.modem_index)

    def get_mmcli_modem_response(self) -> str:
        """Response for 'mmcli -m X'."""
        if self.state == "not_found":
            return "error: couldn't find modem"

        state_data = self.MODEM_STATES[self.state]
        return f"""
  -------------------------
  General  |      dbus path: /org/freedesktop/ModemManager1/Modem/{self.modem_index}
           |         device id: abcd1234
  -------------------------
  Status   |         state: {state_data['state']}
           |   power state: {state_data['power_state']}
           |   access tech: {state_data['access_tech']}
           | signal quality: {state_data['signal_quality']}% (recent)
  -------------------------
  IP       |       supported: ipv4, ipv6, ipv4v6
  -------------------------
  Bearer   | dbus path: /org/freedesktop/ModemManager1/Bearer/1
"""

    def handle_command(self, cmd: List[str]) -> CommandRecord:
        """Handle an mmcli command and return simulated response."""
        cmd_str = " ".join(cmd)

        if "mmcli" not in cmd_str:
            return CommandRecord(cmd, returncode=1, stderr="Not an mmcli command")

        record = CommandRecord(cmd)

        if "-L" in cmd or "--list-modems" in cmd:
            record.stdout = self.get_mmcli_list_response()
            if self.state == "not_found":
                record.returncode = 1

        elif "-m" in cmd:
            record.stdout = self.get_mmcli_modem_response()
            if self.state == "not_found":
                record.returncode = 1
                record.stderr = "error: couldn't find modem"

            # Handle simple-connect
            if "--simple-connect" in cmd_str:
                self.connection_attempts += 1
                if self.state in ["registered", "disconnected"]:
                    self.state = "connected"

        # Handle bearer disconnect (can be with -b or --bearer)
        elif "-b" in cmd or "--bearer" in cmd:
            if "--disconnect" in cmd_str:
                self.disconnect_attempts += 1
                record.stdout = "successfully disconnected bearer"

        self.command_history.append(record)
        return record


class MockSystemctl:
    """Track systemctl service operations.

    Records all enable/disable/start/stop/restart calls
    for verification in tests.
    """

    def __init__(self):
        self.enabled_services: set = set()
        self.disabled_services: set = set()
        self.started_services: set = set()
        self.stopped_services: set = set()
        self.command_history: List[CommandRecord] = []

    def handle_command(self, cmd: List[str]) -> CommandRecord:
        """Handle a systemctl command."""
        if len(cmd) < 2:
            return CommandRecord(cmd, returncode=1, stderr="Usage: systemctl ACTION SERVICE")

        action = cmd[1] if cmd[0] == "systemctl" else cmd[0]
        service = cmd[2] if len(cmd) > 2 and cmd[0] == "systemctl" else (cmd[1] if len(cmd) > 1 else "")

        record = CommandRecord(cmd)

        if action == "enable":
            self.enabled_services.add(service)
            self.disabled_services.discard(service)
        elif action == "disable":
            self.disabled_services.add(service)
            self.enabled_services.discard(service)
        elif action == "start":
            self.started_services.add(service)
            self.stopped_services.discard(service)
        elif action == "stop":
            self.stopped_services.add(service)
            self.started_services.discard(service)
        elif action == "restart":
            self.started_services.add(service)
            self.stopped_services.discard(service)
        elif action == "daemon-reload":
            pass  # Just record it
        else:
            record.returncode = 1
            record.stderr = f"Unknown action: {action}"

        self.command_history.append(record)
        return record

    def is_enabled(self, service: str) -> bool:
        return service in self.enabled_services

    def is_started(self, service: str) -> bool:
        return service in self.started_services

    def get_service_history(self, service: str) -> List[str]:
        """Get list of actions performed on a service."""
        actions = []
        for record in self.command_history:
            if service in " ".join(record.command):
                # Extract action from command
                if "enable" in record.command:
                    actions.append("enable")
                elif "disable" in record.command:
                    actions.append("disable")
                elif "start" in record.command:
                    actions.append("start")
                elif "stop" in record.command:
                    actions.append("stop")
                elif "restart" in record.command:
                    actions.append("restart")
        return actions


class MockNetwork:
    """Simulate network interfaces and operations."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.interfaces: Dict[str, dict] = {}
        self.sys_class_net = tmp_path / "sys" / "class" / "net"
        self.sys_class_net.mkdir(parents=True, exist_ok=True)

    def create_interface(self, name: str, state: str = "up", ip: str = None):
        """Create a mock network interface."""
        self.interfaces[name] = {
            "state": state,
            "ip": ip,
        }

        # Create /sys/class/net entry
        iface_dir = self.sys_class_net / name
        iface_dir.mkdir(exist_ok=True)

        # For wwan0, create qmi directory with raw_ip
        if name == "wwan0":
            qmi_dir = iface_dir / "qmi"
            qmi_dir.mkdir(exist_ok=True)
            (qmi_dir / "raw_ip").write_text("N\n")

    def set_interface_state(self, name: str, state: str):
        """Change interface state (up/down)."""
        if name in self.interfaces:
            self.interfaces[name]["state"] = state

    def set_interface_ip(self, name: str, ip: str):
        """Set interface IP address."""
        if name in self.interfaces:
            self.interfaces[name]["ip"] = ip

    def has_ip(self, name: str) -> bool:
        """Check if interface has an IP."""
        return name in self.interfaces and self.interfaces[name].get("ip") is not None


class MockDisk:
    """Simulate USB disk operations."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.mounted_disks: Dict[str, Path] = {}
        self.disk_labels: Dict[str, str] = {}

    def create_usb_disk(self, device: str = "/dev/sda1", label: str = "USB"):
        """Simulate a USB disk being inserted."""
        mount_point = self.tmp_path / "media" / "pi" / label
        mount_point.mkdir(parents=True, exist_ok=True)
        self.mounted_disks[device] = mount_point
        self.disk_labels[device] = label
        return mount_point

    def mount_disk(self, device: str, mount_point: Path = None) -> Path:
        """Simulate mounting a disk."""
        label = self.disk_labels.get(device, "USB")
        if mount_point is None:
            mount_point = self.tmp_path / "mnt" / "usb"
        mount_point.mkdir(parents=True, exist_ok=True)
        self.mounted_disks[device] = mount_point
        return mount_point

    def unmount_disk(self, device: str):
        """Simulate unmounting a disk."""
        if device in self.mounted_disks:
            del self.mounted_disks[device]

    def is_mounted(self, device: str) -> bool:
        return device in self.mounted_disks

    def get_mount_point(self, device: str) -> Optional[Path]:
        return self.mounted_disks.get(device)


class MockSSHKeygen:
    """Track ssh-keygen calls for verification.

    CRITICAL: The WiFi path MUST call ssh-keygen to generate id_rsa.
    This mock verifies that ssh-keygen is called correctly.
    """

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.ssh_dir = tmp_path / "home" / "pi" / ".ssh"
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        self.keygen_calls: List[dict] = []

    def handle_ssh_keygen(self, cmd: List[str]) -> CommandRecord:
        """Handle an ssh-keygen command."""
        record = CommandRecord(cmd)

        # Parse command arguments
        keytype = "rsa"  # default
        keyfile = None
        passphrase = ""

        i = 0
        while i < len(cmd):
            if cmd[i] == "-t" and i + 1 < len(cmd):
                keytype = cmd[i + 1]
                i += 2
            elif cmd[i] == "-f" and i + 1 < len(cmd):
                keyfile = cmd[i + 1]
                i += 2
            elif cmd[i] == "-N" and i + 1 < len(cmd):
                passphrase = cmd[i + 1]
                i += 2
            else:
                i += 1

        # Record the call
        call_record = {
            "keytype": keytype,
            "keyfile": keyfile,
            "passphrase": passphrase,
        }
        self.keygen_calls.append(call_record)

        # Create mock key files
        if keyfile is None:
            if keytype == "rsa":
                keyfile = str(self.ssh_dir / "id_rsa")
            elif keytype == "ed25519":
                keyfile = str(self.ssh_dir / "id_ed25519")
            else:
                keyfile = str(self.ssh_dir / f"id_{keytype}")

        keyfile_path = Path(keyfile)
        keyfile_path.parent.mkdir(parents=True, exist_ok=True)
        keyfile_path.write_text(f"-----BEGIN {keytype.upper()} PRIVATE KEY-----\nMOCK_KEY\n-----END {keytype.upper()} PRIVATE KEY-----\n")
        keyfile_path.with_suffix(".pub").write_text(f"ssh-{keytype} MOCKPUBLICKEY user@host\n")

        return record

    def was_called(self) -> bool:
        """Check if ssh-keygen was called at all."""
        return len(self.keygen_calls) > 0

    def was_called_with_type(self, keytype: str) -> bool:
        """Check if ssh-keygen was called with specific key type."""
        return any(call["keytype"] == keytype for call in self.keygen_calls)

    def get_generated_keys(self) -> List[str]:
        """Get list of generated key files."""
        keys = []
        for call in self.keygen_calls:
            if call["keyfile"]:
                keys.append(call["keyfile"])
            else:
                if call["keytype"] == "rsa":
                    keys.append(str(self.ssh_dir / "id_rsa"))
                elif call["keytype"] == "ed25519":
                    keys.append(str(self.ssh_dir / "id_ed25519"))
        return keys


@dataclass
class MockEnvironment:
    """Complete mock environment for testing install scripts."""

    tmp_path: Path
    modem: MockModem = field(default_factory=MockModem)
    systemctl: MockSystemctl = field(default_factory=MockSystemctl)
    network: MockNetwork = None
    disk: MockDisk = None
    ssh_keygen: MockSSHKeygen = None

    def __post_init__(self):
        if self.network is None:
            self.network = MockNetwork(self.tmp_path)
        if self.disk is None:
            self.disk = MockDisk(self.tmp_path)
        if self.ssh_keygen is None:
            self.ssh_keygen = MockSSHKeygen(self.tmp_path)

    def setup_standard_interfaces(self):
        """Create standard Pi network interfaces."""
        self.network.create_interface("eth0", state="up")
        self.network.create_interface("eth1", state="down")
        self.network.create_interface("wlan0", state="up")
        self.network.create_interface("wwan0", state="down")

    def setup_usb_with_repo(self) -> Path:
        """Create USB with mjolnir-hamma repo structure."""
        usb_mount = self.disk.create_usb_disk()
        repo_dir = usb_mount / "mjolnir-hamma"
        (repo_dir / "install_scripts").mkdir(parents=True)
        (repo_dir / "files").mkdir(parents=True)
        (repo_dir / "scripts").mkdir(parents=True)
        return usb_mount


def create_mock_subprocess_runner(env: MockEnvironment) -> Callable:
    """Create a subprocess.run replacement that uses mock environment."""

    def mock_run(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

        # Route to appropriate mock
        if "mmcli" in cmd_str:
            result = env.modem.handle_command(cmd if isinstance(cmd, list) else cmd.split())
        elif "systemctl" in cmd_str:
            result = env.systemctl.handle_command(cmd if isinstance(cmd, list) else cmd.split())
        elif "ssh-keygen" in cmd_str:
            result = env.ssh_keygen.handle_ssh_keygen(cmd if isinstance(cmd, list) else cmd.split())
        else:
            # Default: just record the command
            result = CommandRecord(cmd if isinstance(cmd, list) else cmd.split())

        # Convert to CompletedProcess-like object
        mock_result = MagicMock()
        mock_result.returncode = result.returncode
        mock_result.stdout = result.stdout.encode() if isinstance(result.stdout, str) else result.stdout
        mock_result.stderr = result.stderr.encode() if isinstance(result.stderr, str) else result.stderr

        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)

        return mock_result

    return mock_run
