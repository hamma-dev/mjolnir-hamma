#!/home/pi/dev/ltgenv/bin/python
"""Turn a HAMMA sensor on or off.

Controls sensor power via relay toggle and brokkr mode switching.
Reads relay configuration (pin, polarity) from the local unit config.

Usage:
    sensors.py --on
    sensors.py --off
    sensors.py --status
    sensors.py --off --dry-run
"""

# Standard library imports
import datetime
import glob as glob_module
import os
import subprocess
import sys

# Third party imports
import toml


# --- Constants ---

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
SYSTEM_UNIT_TOML = os.path.join(REPO_ROOT, "config", "unit.toml")
LOCAL_UNIT_TOML = os.path.expanduser(
    "~/.config/brokkr/hamma/unit.toml")

RELAY_SCRIPT = os.path.join(SCRIPT_DIR, "relay.py")

BROKKR_SERVICE = "brokkr-hamma-default.service"
DROPIN_DIR = "/etc/systemd/system/{}.d".format(BROKKR_SERVICE)
DROPIN_PATH = os.path.join(DROPIN_DIR, "mode.conf")
DROPIN_CONTENT = "[Service]\nEnvironment=BROKKR_MODE=nosensor\n"

TELEMETRY_DIR = os.path.expanduser("~/brokkr/hamma/telemetry")
SENSOR_IP = "10.10.10.1"


# --- Config ---

def load_relay_config(local_path=None, system_path=None):
    """Load relay config from unit.toml (local overrides system).

    Parameters
    ----------
    local_path : str, optional
        Path to local unit.toml. Defaults to ~/.config/brokkr/hamma/unit.toml.
    system_path : str, optional
        Path to system unit.toml. Defaults to repo config/unit.toml.

    Returns
    -------
    dict
        Dict with 'pin' (int) and 'active_high' (bool).
    """
    if local_path is None:
        local_path = LOCAL_UNIT_TOML
    if system_path is None:
        system_path = SYSTEM_UNIT_TOML

    config = {}

    # Load system config (optional — may not have [relay])
    if os.path.isfile(system_path):
        try:
            config = toml.load(system_path)
        except toml.TomlDecodeError as exc:
            print("[FAIL] Invalid TOML in {}: {}".format(system_path, exc))
            sys.exit(1)

    # Load and merge local config (local wins)
    if os.path.isfile(local_path):
        try:
            local_config = toml.load(local_path)
        except toml.TomlDecodeError as exc:
            print("[FAIL] Invalid TOML in {}: {}".format(local_path, exc))
            sys.exit(1)
        config.update(local_config)
    elif local_path == LOCAL_UNIT_TOML:
        # Default local path not found is OK — fall through to validation
        pass
    else:
        # Explicit path was given but not found
        print("[FAIL] Config file not found: {}".format(local_path))
        sys.exit(1)

    # Validate
    if "relay" not in config:
        print("[FAIL] No [relay] section in config. "
              "Add [relay] with pin and active_high to {}".format(local_path))
        sys.exit(1)

    relay = config["relay"]
    for key in ("pin", "active_high"):
        if key not in relay:
            print("[FAIL] Missing '{}' in [relay] section".format(key))
            sys.exit(1)

    if not isinstance(relay["pin"], int):
        print("[FAIL] 'pin' must be an integer, got: {}".format(
            type(relay["pin"]).__name__))
        sys.exit(1)
    if not isinstance(relay["active_high"], bool):
        print("[FAIL] 'active_high' must be a boolean, got: {}".format(
            type(relay["active_high"]).__name__))
        sys.exit(1)

    return relay


# --- Relay polarity ---

def compute_relay_flag(sensor_on, active_high):
    """Compute whether to energize the relay.

    Parameters
    ----------
    sensor_on : bool
        True if intent is to turn sensor on.
    active_high : bool
        True if energizing the relay powers the sensor on.

    Returns
    -------
    bool
        True to energize relay (relay.py --on), False to de-energize (--off).
    """
    return sensor_on == active_high


def archive_telemetry_csv(telemetry_dir=None):
    """Archive today's telemetry CSV by renaming to .bak.

    If a .bak already exists, uses a timestamp suffix (.bak.HHMMSS).
    If no CSV matches today or directory doesn't exist, does nothing.

    Parameters
    ----------
    telemetry_dir : str, optional
        Path to telemetry directory. Defaults to ~/brokkr/hamma/telemetry.
    """
    if telemetry_dir is None:
        telemetry_dir = TELEMETRY_DIR

    if not os.path.isdir(telemetry_dir):
        return

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    pattern = os.path.join(telemetry_dir, "telemetry_*_{}.csv".format(today))
    matches = glob_module.glob(pattern)

    for csv_path in matches:
        bak_path = csv_path + ".bak"
        if os.path.exists(bak_path):
            timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
            bak_path = csv_path + ".bak.{}".format(timestamp)
        os.rename(csv_path, bak_path)
        print("[OK] Archived {} -> {}".format(
            os.path.basename(csv_path), os.path.basename(bak_path)))


def run_command(cmd, description, stdin_data=None):
    """Run a subprocess command with status output.

    Parameters
    ----------
    cmd : list
        Command and arguments.
    description : str
        Human-readable description of the step.
    stdin_data : str, optional
        Data to pass to stdin.

    Returns
    -------
    int
        Return code (0 = success).
    """
    kwargs = {"capture_output": True, "text": True}
    if stdin_data is not None:
        kwargs["input"] = stdin_data
        kwargs.pop("capture_output")
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.PIPE

    result = subprocess.run(cmd, **kwargs)
    if result.returncode == 0:
        print("[OK] {}".format(description))
    else:
        stderr = result.stderr.strip() if result.stderr else ""
        print("[FAIL] {}: {}".format(description, stderr))
    return result.returncode


def stop_brokkr():
    """Stop the brokkr service."""
    return run_command(
        ["sudo", "systemctl", "stop", BROKKR_SERVICE],
        "Stopped brokkr service")


def start_brokkr():
    """Start the brokkr service."""
    return run_command(
        ["sudo", "systemctl", "start", BROKKR_SERVICE],
        "Started brokkr service")


def daemon_reload():
    """Reload systemd daemon configuration."""
    return run_command(
        ["sudo", "systemctl", "daemon-reload"],
        "Reloaded systemd daemon")


def toggle_relay(relay_on, pin):
    """Toggle the sensor relay via relay.py.

    Parameters
    ----------
    relay_on : bool
        True to energize relay (--on), False to de-energize (--off).
    pin : int
        BCM GPIO pin number.
    """
    flag = "--on" if relay_on else "--off"
    description = "Relay {} (pin {})".format(
        "on (energized)" if relay_on else "off (de-energized)", pin)
    return run_command(
        [RELAY_SCRIPT, "--pin", str(pin), flag], description)


def write_dropin():
    """Create the systemd drop-in for nosensor mode."""
    rc = run_command(
        ["sudo", "mkdir", "-p", DROPIN_DIR],
        "Created drop-in directory")
    if rc != 0:
        return rc
    return run_command(
        ["sudo", "tee", DROPIN_PATH],
        "Wrote mode drop-in (nosensor)",
        stdin_data=DROPIN_CONTENT)


def remove_dropin():
    """Remove the systemd drop-in to restore default mode."""
    return run_command(
        ["sudo", "rm", "-f", DROPIN_PATH],
        "Removed mode drop-in (default)")


def sensor_off(pin, active_high):
    """Execute the sensor off sequence.

    1. Stop brokkr
    2. Toggle relay to power off sensor
    3. Archive today's telemetry CSV
    4. Write nosensor mode drop-in
    5. Reload systemd
    6. Start brokkr in nosensor mode

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor OFF ---")

    rc = stop_brokkr()
    if rc != 0:
        return rc

    relay_on = compute_relay_flag(sensor_on=False, active_high=active_high)
    rc = toggle_relay(relay_on=relay_on, pin=pin)
    if rc != 0:
        return rc

    archive_telemetry_csv()

    rc = write_dropin()
    if rc != 0:
        return rc

    rc = daemon_reload()
    if rc != 0:
        return rc

    rc = start_brokkr()
    return rc


def sensor_on(pin, active_high):
    """Execute the sensor on sequence.

    1. Stop brokkr
    2. Archive today's telemetry CSV
    3. Remove nosensor mode drop-in
    4. Reload systemd
    5. Toggle relay to power on sensor
    6. Start brokkr in default mode

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor ON ---")

    rc = stop_brokkr()
    if rc != 0:
        return rc

    archive_telemetry_csv()

    rc = remove_dropin()
    if rc != 0:
        return rc

    rc = daemon_reload()
    if rc != 0:
        return rc

    relay_on = compute_relay_flag(sensor_on=True, active_high=active_high)
    rc = toggle_relay(relay_on=relay_on, pin=pin)
    if rc != 0:
        return rc

    rc = start_brokkr()
    return rc
