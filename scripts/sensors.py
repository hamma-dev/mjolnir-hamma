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
import argparse
import datetime
import glob as glob_module
import os
import socket
import subprocess
import sys

# Third party imports
import tomli


# --- Constants ---

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
SYSTEM_UNIT_TOML = os.path.join(REPO_ROOT, "config", "unit.toml")
SYSTEM_MAIN_TOML = os.path.join(REPO_ROOT, "config", "main.toml")
LOCAL_UNIT_TOML = os.path.expanduser(
    "~/.config/brokkr/hamma/unit.toml")

RELAY_SCRIPT = os.path.join(SCRIPT_DIR, "relay.py")

BROKKR_SERVICE = "brokkr-hamma-default.service"
SINDRI_SERVICE = "sindri-hamma-client.service"
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
            with open(system_path, "rb") as f:
                config = tomli.load(f)
        except tomli.TOMLDecodeError as exc:
            print("[FAIL] Invalid TOML in {}: {}".format(system_path, exc))
            sys.exit(1)

    # Load and merge local config (local wins)
    if os.path.isfile(local_path):
        try:
            with open(local_path, "rb") as f:
                local_config = tomli.load(f)
        except tomli.TOMLDecodeError as exc:
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


# --- Notifications (uses same channel as state_monitor) ---

def load_notifier_config(main_toml_path=None):
    """Load notifier config from main.toml's [steps.state_monitor] block.

    Reuses the same method/channel/key_file that the state_monitor plugin
    uses, so on/off notifications land in the same chat channel.

    Parameters
    ----------
    main_toml_path : str, optional
        Path to main.toml. Defaults to repo config/main.toml.

    Returns
    -------
    dict or None
        Dict with 'method', 'channel', 'key_file' (any may be None), or
        None if main.toml or the state_monitor block is missing/malformed.
    """
    if main_toml_path is None:
        main_toml_path = SYSTEM_MAIN_TOML
    if not os.path.isfile(main_toml_path):
        return None
    try:
        with open(main_toml_path, "rb") as f:
            data = tomli.load(f)
    except (tomli.TOMLDecodeError, OSError):
        return None
    try:
        sm = data["steps"]["state_monitor"]
    except (KeyError, TypeError):
        return None
    return {
        "method": sm.get("method"),
        "channel": sm.get("channel"),
        "key_file": sm.get("key_file"),
    }


def build_sender(notifier_config):
    """Instantiate a notifier sender from config.

    Returns None on any failure (missing config, unknown method, import
    error, missing key file). All failures print a [WARN] to stderr and
    are swallowed so notifications never block the main on/off operation.

    Parameters
    ----------
    notifier_config : dict or None
        Output of load_notifier_config().

    Returns
    -------
    object or None
        Sender with a .send(msg) method, or None.
    """
    if not notifier_config:
        return None
    method = notifier_config.get("method")
    key_file = notifier_config.get("key_file")
    channel = notifier_config.get("channel")
    if not method or not key_file:
        return None

    try:
        if method == "gchat":
            from notifiers.google_chat import GoogleChatSender
            cls = GoogleChatSender
        elif method == "slack":
            from notifiers.slack import SlackSender
            cls = SlackSender
        else:
            print("[WARN] Unknown notifier method '{}'; "
                  "skipping notification.".format(method), file=sys.stderr)
            return None
    except ImportError as exc:
        print("[WARN] Could not import notifier ({}); "
              "skipping notification.".format(exc), file=sys.stderr)
        return None

    try:
        return cls(key_file, channel=channel)
    except FileNotFoundError:
        print("[WARN] Notifier key file not found: {}; "
              "skipping notification.".format(key_file), file=sys.stderr)
        return None
    except Exception as exc:
        print("[WARN] Could not initialize notifier ({}: {}); "
              "skipping notification.".format(type(exc).__name__, exc),
              file=sys.stderr)
        return None


def get_unit_identifier(local_path=None):
    """Return (sensor_name, site_description) for notification messages.

    sensor_name is 'MjolnirNN' from unit.toml's 'number'. Falls back to
    socket.gethostname() if the number is missing or the file can't be
    read. site_description is None if not set in unit.toml.

    Parameters
    ----------
    local_path : str, optional
        Path to local unit.toml. Defaults to LOCAL_UNIT_TOML.

    Returns
    -------
    tuple of (str, str or None)
    """
    if local_path is None:
        local_path = LOCAL_UNIT_TOML
    try:
        if os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                config = tomli.load(f)
            number = config.get("number")
            site = config.get("site_description") or None
            if isinstance(number, int):
                return "Mjolnir{:02d}".format(number), site
    except (tomli.TOMLDecodeError, OSError):
        pass
    return socket.gethostname(), None


def build_message(sensor_name, site, action, success, rc=None):
    """Construct the notification message for an on/off event.

    Parameters
    ----------
    sensor_name : str
        e.g. 'Mjolnir02'.
    site : str or None
        e.g. 'SWI Berm'. Included in parentheses if non-empty.
    action : str
        'on' or 'off' (case-insensitive).
    success : bool
        True for success, False for failure.
    rc : int, optional
        Return code on failure. Included in the message if provided.

    Returns
    -------
    str
    """
    header = "{} ({}): ".format(sensor_name, site) if site else "{}: ".format(sensor_name)
    if success:
        body = "sensor turned {}".format(action.upper())
    else:
        rc_str = "rc={}".format(rc) if rc is not None else "rc=?"
        body = "sensor turn-{} FAILED ({})".format(action.upper(), rc_str)
    return header + body


def send_notification(sender, msg):
    """Send a notification, swallowing any errors.

    Notifications are best-effort: a send failure must never fail the
    on/off operation that has already happened on the hardware.

    Parameters
    ----------
    sender : object or None
        Sender with a .send(msg) method. If None, this is a no-op.
    msg : str
    """
    if sender is None:
        return
    try:
        sender.send(msg)
        print("[OK] Notification sent")
    except Exception as exc:
        print("[WARN] Notification send failed ({}: {})".format(
            type(exc).__name__, exc), file=sys.stderr)


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


def stop_sindri():
    """Stop the sindri service."""
    return run_command(
        ["sudo", "systemctl", "stop", SINDRI_SERVICE],
        "Stopped sindri service")


def start_sindri():
    """Start the sindri service."""
    return run_command(
        ["sudo", "systemctl", "start", SINDRI_SERVICE],
        "Started sindri service")


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

    1. Stop brokkr and sindri
    2. Toggle relay to power off sensor
    3. Archive today's telemetry CSV
    4. Write nosensor mode drop-in
    5. Reload systemd
    6. Start brokkr in nosensor mode, then sindri

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor OFF ---")

    rc = stop_brokkr()
    if rc != 0:
        return rc

    stop_sindri()

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
    if rc != 0:
        return rc

    start_sindri()
    return 0


def sensor_on(pin, active_high):
    """Execute the sensor on sequence.

    1. Stop brokkr and sindri
    2. Archive today's telemetry CSV
    3. Remove nosensor mode drop-in
    4. Reload systemd
    5. Toggle relay to power on sensor
    6. Start brokkr in default mode, then sindri

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor ON ---")

    rc = stop_brokkr()
    if rc != 0:
        return rc

    stop_sindri()

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
    if rc != 0:
        return rc

    start_sindri()
    return 0


def sensor_status(config):
    """Report current sensor state.

    Parameters
    ----------
    config : dict
        Relay config with 'pin' and 'active_high'.

    Returns
    -------
    str
        Multi-line status report.
    """
    lines = []

    # Drop-in
    if os.path.isfile(DROPIN_PATH):
        lines.append("Drop-in: yes (nosensor mode)")
        with open(DROPIN_PATH) as f:
            lines.append("  Contents: {}".format(f.read().strip()))
    else:
        lines.append("Drop-in: no (default mode)")

    # Brokkr service
    result = subprocess.run(
        ["systemctl", "is-active", BROKKR_SERVICE],
        capture_output=True, text=True)
    state = result.stdout.strip() if result.stdout else "unknown"
    lines.append("Brokkr service: {}".format(state))

    # Brokkr mode
    mode = "nosensor" if os.path.isfile(DROPIN_PATH) else "default"
    lines.append("Brokkr mode: {}".format(mode))

    # Sensor reachable
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", SENSOR_IP],
        capture_output=True, text=True)
    reachable = "yes" if result.returncode == 0 else "no"
    lines.append("Sensor reachable: {} ({})".format(reachable, SENSOR_IP))

    # Last telemetry
    if os.path.isdir(TELEMETRY_DIR):
        csvs = sorted(glob_module.glob(
            os.path.join(TELEMETRY_DIR, "telemetry_*.csv")))
        if csvs:
            latest = csvs[-1]
            mtime = datetime.datetime.fromtimestamp(
                os.path.getmtime(latest)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append("Last telemetry: {} ({})".format(
                os.path.basename(latest), mtime))
        else:
            lines.append("Last telemetry: none")
    else:
        lines.append("Last telemetry: directory not found")

    # Relay config
    lines.append("Relay config: pin={}, active_high={}".format(
        config["pin"], config["active_high"]))

    output = "\n".join(lines)
    return output


def parse_args(argv=None):
    """Parse command-line arguments.

    Parameters
    ----------
    argv : list, optional
        Argument list. Defaults to sys.argv[1:].

    Returns
    -------
    argparse.Namespace
    """
    parser = argparse.ArgumentParser(
        description="Turn a HAMMA sensor on or off.")

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--on", action="store_true", dest="sensor_on", default=None,
        help="Turn sensor on (power on, brokkr default mode)")
    action_group.add_argument(
        "--off", action="store_false", dest="sensor_on",
        help="Turn sensor off (power off, brokkr nosensor mode)")
    action_group.add_argument(
        "--status", action="store_true", default=False,
        help="Report current sensor state")

    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print what would happen without executing")

    args = parser.parse_args(argv)

    # Require at least one action
    if args.sensor_on is None and not args.status:
        parser.print_help()
        sys.exit(1)

    return args


def run(argv=None, config_path=None, main_toml_path=None):
    """Main entry point.

    Parameters
    ----------
    argv : list, optional
        CLI arguments. Defaults to sys.argv[1:].
    config_path : str, optional
        Override unit.toml path (for testing).
    main_toml_path : str, optional
        Override main.toml path (for testing). Used to locate the
        state_monitor notifier config.
    """
    args = parse_args(argv)

    # Load config
    config = load_relay_config(
        local_path=config_path) if config_path else load_relay_config()

    # Status
    if args.status:
        print(sensor_status(config))
        return 0

    # Dry run
    if args.dry_run:
        relay_on = compute_relay_flag(
            args.sensor_on, config["active_high"])
        action = "ON" if args.sensor_on else "OFF"
        relay_flag = "--on" if relay_on else "--off"
        print("--- DRY RUN: Turn sensor {} ---".format(action))
        print("Config: pin={}, active_high={}".format(
            config["pin"], config["active_high"]))
        print("Would run:")
        print("  sudo systemctl stop {}".format(BROKKR_SERVICE))
        if not args.sensor_on:
            print("  {} --pin {} {}".format(
                RELAY_SCRIPT, config["pin"], relay_flag))
        print("  Archive telemetry CSV")
        if args.sensor_on:
            print("  sudo rm -f {}".format(DROPIN_PATH))
        else:
            print("  Write {} (nosensor mode)".format(DROPIN_PATH))
        print("  sudo systemctl daemon-reload")
        if args.sensor_on:
            print("  {} --pin {} {}".format(
                RELAY_SCRIPT, config["pin"], relay_flag))
        print("  sudo systemctl start {}".format(BROKKR_SERVICE))
        return 0

    # Prepare the notifier up front; failures here are non-fatal and just
    # disable notifications for this run.
    sender = build_sender(load_notifier_config(main_toml_path))
    sensor_name, site = get_unit_identifier(config_path)
    action = "on" if args.sensor_on else "off"

    # Execute
    if args.sensor_on:
        rc = sensor_on(pin=config["pin"], active_high=config["active_high"])
    else:
        rc = sensor_off(pin=config["pin"], active_high=config["active_high"])

    msg = build_message(
        sensor_name, site, action,
        success=(rc == 0),
        rc=rc if rc != 0 else None)
    send_notification(sender, msg)
    return rc


def main():
    """CLI entry point."""
    sys.exit(run())


if __name__ == "__main__":
    main()
