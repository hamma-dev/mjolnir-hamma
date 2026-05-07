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
