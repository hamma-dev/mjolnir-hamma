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
import difflib
import glob as glob_module
import os
import re
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

# Brokkr modes are a single string key, NOT composable on the command line
# (--mode nosensor,nochargecontroller raises KeyError). But the key encodes two
# independent axes, and only the sensor axis belongs to --on/--off:
#
#     nosensor            <- what --off/--on toggles
#     nochargecontroller  <- a property of the unit's hardware; STICKY
#
# mode.conf is the shared filename for every mode override, and the field uses
# two equally valid forms (HAM-184):
#
#     Environment=BROKKR_MODE=<mode>              <- what this script writes
#     ExecStart=\nExecStart=... --mode <mode> ... <- documented manual method
#
# Treating the file as a boolean (present => nosensor) misreports the mode and
# destroys the nochargecontroller half on any unit using it.
#
# The two forms are NOT equal in force. brokkr resolves its mode as
#
#     CLI --mode  >  BROKKR_MODE in the environment  >  mode.toml
#
# (server/fleet_probe.py's mode probe; docs/sensors-usage.md "four
# placements"). So on a drop-in holding both, the ExecStart override wins and
# the Environment= line is dead text. Reading the Environment= form first --
# which is what this script did -- reports the LOSER, and then every decision
# downstream is made about a mode that is not in force.
#
# There are FOUR placements and this file can see only two of them: the
# drop-in's Environment= and its ExecStart=. The unit's own ExecStart= and
# ~/.config/brokkr/hamma/mode.toml are invisible here. That is why a write
# only ever EDITS what is in the drop-in and never reconstructs it: an axis
# set elsewhere is not ours to move, and claiming otherwise is how mj05
# (mode.toml) got reported as `default`.
MODE_DEFAULT = "default"
MODE_UNKNOWN = "unknown"
NOSENSOR_TOKEN = "nosensor"
NOCHARGE_TOKEN = "nochargecontroller"

# Every mode this script is entitled to rewrite: exactly the four the two axes
# can compose. config/mode.toml also defines `realtime`, `sindri02x` and `test`,
# and adding another costs nothing -- all of them parse cleanly here, so
# MODE_UNKNOWN never catches them, but decomposing one yields (False, False) and
# an --on would compute `default` and delete the drop-in, silently discarding
# whatever the operator set. Refuse rather than guess, same as MODE_UNKNOWN.
KNOWN_MODES = frozenset([
    MODE_DEFAULT,
    NOSENSOR_TOKEN,
    NOCHARGE_TOKEN,
    "{}_{}".format(NOSENSOR_TOKEN, NOCHARGE_TOKEN),
])

ENVIRONMENT_DROPIN_TEMPLATE = "[Service]\nEnvironment=BROKKR_MODE={}\n"

# systemd comments start with EITHER '#' or ';' -- systemd.syntax(7).
# Honouring only '#' made a ';'-commented old command line look like a mode
# token in an unreadable shape, which then refused every toggle.
COMMENT_PREFIXES = ("#", ";")

# The drop-in is scanned one logical line at a time, so EVERY declaration is
# found rather than the first match of a file-wide regex. Finding only the
# first is what let a second, winning declaration sit untouched behind a
# rewrite that reported success.
RE_ENV_MODE_LINE = re.compile(
    r"^\s*Environment\s*=\s*[\"']?BROKKR_MODE=(\S+?)[\"']?\s*$")
RE_ENV_MODE_VALUE = re.compile(r"(BROKKR_MODE=)(\S+?)([\"']?\s*)$")
RE_BROKKR_MODE = re.compile(r"BROKKR_MODE")
RE_EXECSTART_LINE = re.compile(r"^\s*ExecStart\s*=")
RE_REAL_EXECSTART = re.compile(r"^\s*ExecStart\s*=\s*\S")
# A bare `ExecStart=` with no replacement clears brokkr's command line; the
# line exists precisely to clear the unit's original before the override sets
# a new one, so on its own it stops the unit from starting at all.
RE_BARE_RESET = re.compile(r"^\s*ExecStart\s*=\s*$")
RE_SECTION = re.compile(r"^\s*\[[^\]]*\]\s*$")
# `--mode` as its own word. `--mode=x` matches too, deliberately: it names a
# mode in a shape this script cannot rewrite, so it must be seen and refused
# rather than missed.
RE_MODE_TOKEN = re.compile(r"(?<![\w-])--mode(?![\w-])")
# `--mode <value>`, split so a rewrite keeps the operator's own separator.
RE_MODE_FLAG_SET = re.compile(r"((?<![\w-])--mode)(\s+)(\S+)")
# The same flag plus the whitespace in front of it, for removal.
RE_MODE_FLAG_DEL = re.compile(r"\s*(?<![\w-])--mode\s+\S+")

# Retained for backward compatibility: the plain nosensor drop-in.
DROPIN_CONTENT = ENVIRONMENT_DROPIN_TEMPLATE.format(NOSENSOR_TOKEN)

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


def build_message(sensor_name, site, action, success, rc=None, reason=None):
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
    reason : str, optional
        Short explanation appended on failure. A refusal is a failure the
        operator cannot diagnose from `rc=1` alone, and the chat notice is
        often the only part of a tunnelled run anyone reads.

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
        if reason:
            body += " -- {}".format(reason)
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


def logical_lines(content):
    """Split `content` into systemd logical lines.

    Yields (text, index, span): the joined text, the index of its first
    physical line, and how many physical lines it spans. A trailing
    backslash continues a directive onto the next line, so a scan that works
    physical line by physical line can see half a directive and parse it as
    whole.
    """
    out = []
    lines = (content or "").split("\n")
    index = 0
    while index < len(lines):
        start = index
        parts = [lines[index]]
        while (lines[index].rstrip().endswith("\\")
               and index + 1 < len(lines)):
            index += 1
            parts.append(lines[index])
        if len(parts) == 1:
            text = parts[0]
        else:
            text = " ".join(part.rstrip().rstrip("\\") for part in parts)
        out.append((text, start, index - start + 1))
        index += 1
    return out


def scan_modes(content):
    """Find every mode declaration in `content`.

    Returns (sites, problems).

    `sites` is one dict per declaration this script can both read and
    rewrite, in file order: {"form", "mode", "index", "text"}. `problems` is
    one (lineno, text, why) per mode token in a shape it cannot -- an
    `--mode=x`, a line continuation, a quoting it does not handle, a mode on
    a directive other than ExecStart=.

    Finding ALL of them is the point. The old file-wide first-match regexes
    could not tell one declaration from three, so a file naming the mode
    twice looked exactly like a file naming it once.
    """
    sites = []
    problems = []
    for text, start, span in logical_lines(content):
        stripped = text.strip()
        if not stripped or stripped[0] in COMMENT_PREFIXES:
            continue
        env_tokens = RE_BROKKR_MODE.findall(text)
        flag_tokens = RE_MODE_TOKEN.findall(text)
        if not env_tokens and not flag_tokens:
            continue
        lineno = start + 1
        if span > 1:
            problems.append((lineno, stripped,
                             "the mode is on a continued line"))
        elif env_tokens and flag_tokens:
            problems.append((lineno, stripped,
                             "one line names the mode two ways"))
        elif len(env_tokens) > 1 or len(flag_tokens) > 1:
            problems.append((lineno, stripped,
                             "one line names the mode twice"))
        elif env_tokens:
            match = RE_ENV_MODE_LINE.match(text)
            if match:
                sites.append({"form": "environment", "mode": match.group(1),
                              "index": start, "text": stripped})
            else:
                problems.append((lineno, stripped,
                                 "unrecognised BROKKR_MODE assignment"))
        elif not RE_EXECSTART_LINE.match(text):
            # ExecStartPre=/ExecStartPost=/ExecReload= carrying --mode are
            # not brokkr's command line, and editing them would change a
            # co-resident directive's meaning. Hand them back.
            problems.append((lineno, stripped,
                             "--mode on a directive this script does not "
                             "rewrite"))
        else:
            match = RE_MODE_FLAG_SET.search(text)
            if match:
                sites.append({"form": "execstart", "mode": match.group(3),
                              "index": start, "text": stripped})
            else:
                problems.append((lineno, stripped,
                                 "--mode without a separate value"))
    return sites, problems


def winning_site(sites):
    """The declaration brokkr will actually obey, per its precedence order.

    CLI `--mode` beats `BROKKR_MODE` in the environment, and for a repeated
    assignment of either, systemd applies the LAST one.
    """
    execstarts = [site for site in sites if site["form"] == "execstart"]
    return (execstarts or sites)[-1] if sites else None


def inspect_content(content):
    """Everything this script knows about a mode.conf, from one read.

    Returns a dict:
        mode     -- the mode in force, by brokkr's precedence order;
                    MODE_DEFAULT for a provably mode-free file,
                    MODE_UNKNOWN when a mode is named unreadably
        form     -- "environment", "execstart" or None
        site     -- the winning declaration, or None
        sites    -- every readable declaration, in file order
        problems -- mode tokens in shapes this script cannot read
    """
    sites, problems = scan_modes(content)
    if problems:
        # Something here names a mode and we cannot say which one, so we
        # cannot say what is in force either -- the unreadable one may be
        # the winner.
        return {"mode": MODE_UNKNOWN, "form": None, "site": None,
                "sites": sites, "problems": problems}
    site = winning_site(sites)
    if site is None:
        # Provably mode-free: no mode token on any live line. brokkr runs in
        # default mode. Calling this "unknown" conflated "sets no mode" with
        # "sets one we cannot read", and bricked the toggle after a collapse
        # -- stripping --mode leaves a legitimate, mode-free file.
        return {"mode": MODE_DEFAULT, "form": None, "site": None,
                "sites": [], "problems": []}
    return {"mode": site["mode"], "form": site["form"], "site": site,
            "sites": sites, "problems": []}


def parse_mode(content):
    """Parse the brokkr mode out of drop-in content.

    Reports the mode in FORCE, by brokkr's precedence order, so --status
    tells the truth even about a file that must not be rewritten. Never
    guesses: a mode named in a shape it cannot read reports MODE_UNKNOWN.

    Parameters
    ----------
    content : str
        Raw contents of mode.conf.

    Returns
    -------
    tuple of (str, str or None)
        (mode, form) where form is "environment", "execstart" or None.
    """
    detail = inspect_content(content)
    return detail["mode"], detail["form"]


def read_mode_detail(path=None):
    """Read mode.conf once and return everything decided from those bytes.

    The dict is inspect_content()'s, plus:
        content  -- the raw bytes, or None if there is no drop-in
        readable -- False when the file is there but could not be read

    One read. Reading the file a second time to get the content meant the
    mode and the rewrite could come from two different versions of it, and an
    error on that second read looked exactly like "no drop-in".
    """
    path = DROPIN_PATH if path is None else path
    if not os.path.isfile(path):
        detail = inspect_content(None)
        detail.update({"content": None, "readable": True})
        return detail
    try:
        with open(path) as file:
            content = file.read()
    except OSError:
        # Absent and unreadable are different facts. Reporting a live
        # override as "default" would have apply_mode treat it as absent.
        return {"mode": MODE_UNKNOWN, "form": None, "site": None,
                "sites": [], "problems": [], "content": None,
                "readable": False}
    detail = inspect_content(content)
    detail.update({"content": content, "readable": True})
    return detail


def read_mode(path=None):
    """Return the (mode, form, content) currently configured on this unit.

    No drop-in means default mode -- that much the original code got right.
    """
    detail = read_mode_detail(path)
    return detail["mode"], detail["form"], detail["content"]


def is_directive(text):
    """True if this line configures something, rather than being scaffolding.

    Blank lines, comments (BOTH '#' and ';') and section headers are
    scaffolding: a file holding nothing else sets nothing, so removing its
    mode IS removing the file.
    """
    stripped = text.strip()
    if not stripped or stripped[0] in COMMENT_PREFIXES:
        return False
    return not RE_SECTION.match(stripped)


def mode_free_lines(content):
    """`content`'s directives, in order, with every mode reference removed.

    Blanks and comments drop out, so a rewrite that only moves whitespace
    around compares equal -- while a changed interpreter path, a reordering
    or a lost duplicate does not. Order and multiplicity are kept because
    both are semantic in systemd: for a repeated assignment the LAST one
    wins.
    """
    out = []
    for text, _, _ in logical_lines(content):
        stripped = text.strip()
        if not stripped or stripped[0] in COMMENT_PREFIXES:
            continue
        if RE_BROKKR_MODE.search(stripped):
            continue
        out.append(RE_MODE_FLAG_DEL.sub("", stripped).strip())
    return [line for line in out if line]


def directive_delta(before, after):
    """(lost, gained) between two files, ignoring the mode itself.

    An ordered diff, not a set difference. Plain list membership -- which is
    what the previous attempt used -- cannot see a reordering or a lost
    duplicate at all, which is why its check never fired.
    """
    old = mode_free_lines(before)
    new = mode_free_lines(after)
    lost = []
    gained = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            lost.extend(old[i1:i2])
        if tag in ("insert", "replace"):
            gained.extend(new[j1:j2])
    return lost, gained


def drop_orphan_resets(lines):
    """Remove a bare `ExecStart=` that has no real ExecStart to reset.

    The empty line is required while an ExecStart override is present -- it
    clears the unit's original before the override sets a new one. Left on
    its own it clears the command line and nothing replaces it, so brokkr
    never starts. Keeping such a file because some other line survived is
    worse than not keeping it.
    """
    if any(RE_REAL_EXECSTART.match(line) for line in lines):
        return list(lines)
    return [line for line in lines if not RE_BARE_RESET.match(line)]


def collapse_content(content, sites):
    """`content` with every mode declaration removed, or None.

    None means the file held nothing but the mode, so removing the mode IS
    removing the file -- which is more honest than leaving an empty
    `[Service]` stanza behind.

    Every site goes, not just the one that happened to parse first: removing
    one of two leaves the other's mode in force while the tool reports
    default. mode.conf is the shared filename for every override and is
    hand-edited in the field, so anything an operator put alongside the mode
    has to survive -- the hazard the power-state-reconciliation review named
    as "could delete a custom mode.conf".
    """
    if not content:
        return None
    lines = content.split("\n")
    dropped = set()
    for site in sites:
        index = site["index"]
        if site["form"] == "environment":
            dropped.add(index)
        else:
            lines[index] = RE_MODE_FLAG_DEL.sub("", lines[index], count=1)
    kept = drop_orphan_resets(
        [line for index, line in enumerate(lines) if index not in dropped])
    if not any(is_directive(line) for line in kept):
        return None
    return "\n".join(kept)


def strip_mode_directive(existing, form=None):
    """Back-compatible wrapper: the collapsed content, or None.

    `form` is ignored -- the content is authoritative, and trusting a
    separately-derived form is what made a mixed-form file strip only half
    of itself. A None return no longer licenses a delete on its own: see
    plan_mode(), which proves the delete is safe before taking it.
    """
    detail = inspect_content(existing)
    if detail["problems"]:
        return None
    return collapse_content(existing, detail["sites"])


def decompose_mode(mode):
    """Split a mode key into its (nosensor, nochargecontroller) axes."""
    if mode == MODE_DEFAULT:
        return False, False
    parts = mode.split("_")
    return NOSENSOR_TOKEN in parts, NOCHARGE_TOKEN in parts


def compose_mode(nosensor, nocharge):
    """Build the mode key for a pair of axis flags."""
    tokens = []
    if nosensor:
        tokens.append(NOSENSOR_TOKEN)
    if nocharge:
        tokens.append(NOCHARGE_TOKEN)
    return "_".join(tokens) if tokens else MODE_DEFAULT


def target_mode(current_mode, sensor_on):
    """Apply an on/off transition to the sensor axis only.

    nochargecontroller is a property of the unit's hardware, not of whether
    the sensor is powered, so it is preserved across both transitions.
    """
    _, nocharge = decompose_mode(current_mode)
    return compose_mode(nosensor=not sensor_on, nocharge=nocharge)


def set_mode_at(content, site, mode):
    """`content` with the mode at `site` changed to `mode`, nothing else.

    Only that one line is touched, found by index rather than by a regex
    `count=1` -- which rewrote the FIRST match and left a later, winning
    duplicate in place.
    """
    lines = content.split("\n")
    index = site["index"]
    if site["form"] == "environment":
        # Keep the operator's quoting and indentation; swap only the value.
        lines[index] = RE_ENV_MODE_VALUE.sub(
            lambda m: m.group(1) + mode + m.group(3), lines[index], count=1)
    else:
        # Keep the operator's separator too: the parser accepts any
        # whitespace run, so a single-space str.replace silently matched
        # nothing on `--mode\tnosensor` and reported a transition that never
        # happened.
        lines[index] = RE_MODE_FLAG_SET.sub(
            lambda m: m.group(1) + m.group(2) + mode, lines[index], count=1)
    return "\n".join(lines)


def add_mode_line(content, mode):
    """Add a mode to a file that sets none, keeping what is already there."""
    directive = "Environment=BROKKR_MODE={}".format(mode)
    lines = content.split("\n")
    for index, line in enumerate(lines):
        if line.strip().lower() == "[service]":
            lines.insert(index + 1, directive)
            return "\n".join(lines)
    # No [Service] section to extend. Append one rather than restructure
    # what is there; verify_write() allows exactly this one added header.
    separator = "" if content.endswith("\n") else "\n"
    return "{}{}[Service]\n{}\n".format(content, separator, directive)


def render_dropin(mode, form=None, existing=None):
    """Render drop-in content for `mode`, preserving everything else.

    The existing declaration is edited in place so the unit's own
    interpreter path, quoting and co-resident directives survive; nothing is
    reconstructed from a template that might not match this unit.

    `form` is accepted but ignored: the content is authoritative. Taking the
    form from somewhere other than the bytes being edited is how a
    mixed-form file got its losing half rewritten.
    """
    detail = inspect_content(existing)
    if detail["site"] is not None:
        return set_mode_at(existing, detail["site"], mode)
    if existing:
        return add_mode_line(existing, mode)
    # Nothing to preserve. There used to be an ExecStart template here with a
    # hardcoded interpreter path, which is a guess about the unit. The
    # Environment= form states the mode without inventing a command line.
    return ENVIRONMENT_DROPIN_TEMPLATE.format(mode)


def _untouched(what):
    """The tail every post-condition refusal ends with."""
    return ("          {}\n"
            "          The drop-in was NOT changed. Inspect it by hand: {}"
            .format(what, DROPIN_PATH))


def verify_write(content, target, existing):
    """Why these bytes must not be written, or None if they are correct.

    Checking our own output is what turns "the rewrite silently did not
    take" from a confident [OK] over a wrong file into a refusal that leaves
    the file alone. It has to re-scan with the real scanner and compare
    ordered directives, or it passes everything: the previous attempt
    re-parsed with a first-match parser and compared with list membership,
    and so never fired on any input.
    """
    detail = inspect_content(content)
    if detail["problems"]:
        return ("  [ERROR] Refusing to write: the rewrite produced a mode "
                "it cannot read back\n"
                + _untouched("({}).".format(detail["problems"][0][2])))
    if len(detail["sites"]) != 1:
        return ("  [ERROR] Refusing to write: the rewrite would leave {} "
                "mode declarations,\n".format(len(detail["sites"]))
                + _untouched("and exactly one is correct."))
    if detail["mode"] != target:
        return ("  [ERROR] Refusing to write: the rewrite produced mode "
                "'{}', not '{}'.\n".format(detail["mode"], target)
                + _untouched("The mode change did not take."))
    lost, gained = directive_delta(existing, content)
    gained = [line for line in gained if line.lower() != "[service]"]
    if lost or gained:
        return ("  [ERROR] Refusing to write: the rewrite would change more "
                "than the mode\n"
                + _untouched("(dropped {!r}, added {!r}).".format(
                    lost[:2], gained[:2])))
    return None


def verify_collapse(remainder, existing):
    """Why this collapsed file must not be written, or None if it is correct.

    A collapse has to leave a file that is both mode-free AND startable. A
    mode token we could not strip must never be answered by deleting the
    file: that turns a parse failure into data loss.
    """
    detail = inspect_content(remainder)
    if detail["problems"] or detail["sites"]:
        return ("  [ERROR] Refusing to write: a mode declaration survived "
                "the collapse,\n"
                + _untouched("so 'default' would be a false report."))
    if any(RE_BARE_RESET.match(line) for line in remainder.split("\n")) \
            and not any(RE_REAL_EXECSTART.match(line)
                        for line in remainder.split("\n")):
        return ("  [ERROR] Refusing to write: that would leave a bare "
                "'ExecStart=' reset\n"
                + _untouched("with nothing to replace it, and brokkr would "
                             "not start."))
    lost, gained = directive_delta(existing, remainder)
    lost = [line for line in lost if not RE_BARE_RESET.match(line)]
    if lost or gained:
        return ("  [ERROR] Refusing to write: the collapse would change more "
                "than the mode\n"
                + _untouched("(dropped {!r}, added {!r}).".format(
                    lost[:2], gained[:2])))
    return None


def refuse_reason(mode, detail=None):
    """Why this mode must not be rewritten, or None if it is safe to.

    One rule: never guess at a mode we do not fully understand. Every way of
    not understanding it is reported here, so apply_mode and --dry-run cannot
    drift apart on which cases refuse.
    """
    problems = (detail or {}).get("problems") or []
    if problems:
        lines = ["  [ERROR] {} names a mode in a shape this script cannot "
                 "read.".format(DROPIN_PATH),
                 "          Refusing to rewrite it. Fix or remove these "
                 "lines by hand:"]
        for lineno, text, why in problems:
            lines.append("            line {}: {} ({})".format(
                lineno, text, why))
        return "\n".join(lines)
    sites = (detail or {}).get("sites") or []
    if len(sites) > 1:
        winner = winning_site(sites)
        lines = ["  [ERROR] {} names a mode in more than one place.".format(
            DROPIN_PATH),
            "          Refusing to rewrite it -- editing one and leaving "
            "the others would",
            "          silently change the mode in force. Keep exactly one "
            "of these:"]
        for site in sites:
            lines.append("            line {}: {}{}".format(
                site["index"] + 1, site["text"],
                "   <- the one brokkr obeys" if site is winner else ""))
        return "\n".join(lines)
    if mode == MODE_UNKNOWN:
        return ("  [ERROR] {} exists but its mode could not be parsed.\n"
                "          Refusing to overwrite it. Inspect it by hand."
                .format(DROPIN_PATH))
    if mode not in KNOWN_MODES:
        return ("  [ERROR] {} sets mode '{}', which is outside the "
                "sensor/chargecontroller model.\n"
                "          Refusing to rewrite it -- on/off cannot tell what "
                "that mode means, and\n"
                "          collapsing it would silently discard it. Change it "
                "by hand.".format(DROPIN_PATH, mode))
    return None


def plan_mode(sensor_on, path=None):
    """Decide AND verify the whole mode change without touching anything.

    Everything that can refuse happens in here, off the live hardware, so a
    refusal costs nothing. apply_mode ran at step 4 of the off sequence --
    after brokkr and sindri were stopped and the relay de-energized -- so a
    refusal there left the unit dark with nothing restarted. This is the
    emergency power tool, used at low battery: it has to fail empty-handed
    or not at all.

    Returns a dict:
        kind    -- "noop", "write", "delete" or "refuse"
        current -- the mode in force now
        mode    -- the target mode
        content -- the bytes to write, for "write"
        reason  -- the operator-facing text, for "refuse"
        summary -- one short line of it, for the notification
    """
    detail = read_mode_detail(path)
    current = detail["mode"]
    reason = refuse_reason(current, detail)
    if reason is not None:
        return {"kind": "refuse", "current": current, "mode": None,
                "reason": reason,
                "summary": "mode drop-in cannot be rewritten"}

    existing = detail["content"]
    target = target_mode(current, sensor_on=sensor_on)
    if target == current:
        return {"kind": "noop", "current": current, "mode": current}

    if target == MODE_DEFAULT:
        remainder = collapse_content(existing, detail["sites"])
        if remainder is None:
            # collapse_content has already established that the mode was all
            # this file set, so deleting it IS removing the mode and nothing
            # else. Note which way the arrows point: NO post-condition
            # failure routes here. Every one of them returns "refuse" and
            # leaves the file alone, because answering a check we could not
            # satisfy with `rm -f` turns a parse failure into data loss.
            return {"kind": "delete", "current": current, "mode": target}
        reason = verify_collapse(remainder, existing)
        if reason is not None:
            return {"kind": "refuse", "current": current, "mode": target,
                    "reason": reason,
                    "summary": "mode drop-in cannot be collapsed"}
        return {"kind": "write", "current": current, "mode": target,
                "content": remainder}

    content = render_dropin(target, detail["form"], existing)
    reason = verify_write(content, target, existing)
    if reason is not None:
        return {"kind": "refuse", "current": current, "mode": target,
                "reason": reason,
                "summary": "mode drop-in rewrite could not be verified"}
    return {"kind": "write", "current": current, "mode": target,
            "content": content}


def apply_mode(sensor_on, plan=None):
    """Execute a mode plan. Decides one first if the caller did not.

    Returns
    -------
    int
        0 on success, nonzero on failure or refusal.
    """
    if plan is None:
        plan = plan_mode(sensor_on=sensor_on)

    if plan["kind"] == "refuse":
        print(plan["reason"])
        return 1
    if plan["kind"] == "noop":
        print("  [OK] Mode already {} (unchanged)".format(plan["mode"]))
        return 0
    if plan["kind"] == "delete":
        return remove_dropin()

    # Unconditional: an equal-and-no-change and a delete have both already
    # returned, so reaching here IS a transition. The old guard reduced to
    # `current != MODE_DEFAULT`, which announced the sticky transitions but
    # stayed quiet on default -> nosensor, the most common one of all.
    print("  [INFO] Mode {} -> {} (preserving {})".format(
        plan["current"], plan["mode"], NOCHARGE_TOKEN)
        if NOCHARGE_TOKEN in plan["mode"]
        else "  [INFO] Mode {} -> {}".format(plan["current"], plan["mode"]))
    return write_dropin(plan["content"], plan["mode"])


def write_dropin(content=None, mode=None):
    """Create the systemd drop-in with the given content."""
    if content is None:
        content = DROPIN_CONTENT
        mode = NOSENSOR_TOKEN
    rc = run_command(
        ["sudo", "mkdir", "-p", DROPIN_DIR],
        "Created drop-in directory")
    if rc != 0:
        return rc
    return run_command(
        ["sudo", "tee", DROPIN_PATH],
        "Wrote mode drop-in ({})".format(mode or NOSENSOR_TOKEN),
        stdin_data=content)


def remove_dropin():
    """Delete the drop-in, restoring default mode.

    Only ever called once plan_mode() has PROVEN the mode was all the file
    set -- see verify_delete(). It used to decide that for itself from
    strip_mode_directive() returning None, which also meant "a mode token
    survived that I could not strip": answering a parse failure with `rm -f`
    turned it into data loss. Deleting is never a way to satisfy a
    post-condition.
    """
    return run_command(
        ["sudo", "rm", "-f", DROPIN_PATH],
        "Removed mode drop-in (default)")


def sensor_off(pin, active_high, plan=None):
    """Execute the sensor off sequence.

    1. Decide and verify the mode change (refuses here, before anything moves)
    2. Stop brokkr and sindri
    3. Toggle relay to power off sensor
    4. Archive today's telemetry CSV
    5. Apply the mode plan
    6. Reload systemd
    7. Start brokkr in nosensor mode, then sindri

    `plan` is the pre-flight from run(); it is computed here if absent so the
    "refuse before anything moves" guarantee holds for every caller, not
    only the one that remembered to pre-flight.

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor OFF ---")

    if plan is None:
        plan = plan_mode(sensor_on=False)
    if plan["kind"] == "refuse":
        print(plan["reason"])
        return 1

    rc = stop_brokkr()
    if rc != 0:
        return rc

    stop_sindri()

    relay_on = compute_relay_flag(sensor_on=False, active_high=active_high)
    rc = toggle_relay(relay_on=relay_on, pin=pin)
    if rc != 0:
        return rc

    archive_telemetry_csv()

    rc = apply_mode(sensor_on=False, plan=plan)
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


def sensor_on(pin, active_high, plan=None):
    """Execute the sensor on sequence.

    1. Decide and verify the mode change (refuses here, before anything moves)
    2. Stop brokkr and sindri
    3. Archive today's telemetry CSV
    4. Apply the mode plan
    5. Reload systemd
    6. Toggle relay to power on sensor
    7. Start brokkr in default mode, then sindri

    Returns
    -------
    int
        0 on success, nonzero on failure.
    """
    print("--- Turning sensor ON ---")

    if plan is None:
        plan = plan_mode(sensor_on=True)
    if plan["kind"] == "refuse":
        print(plan["reason"])
        return 1

    rc = stop_brokkr()
    if rc != 0:
        return rc

    stop_sindri()

    archive_telemetry_csv()

    rc = apply_mode(sensor_on=True, plan=plan)
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

    # Drop-in. Report the mode the file actually sets, not its mere existence
    # -- mode.conf is the shared filename for every override (HAM-184).
    # One read for the mode, the form AND the contents -- a separate isfile()
    # plus a bare open() could contradict what was just parsed, and the bare
    # open() would raise straight out of a read-only status command if the file
    # went away in between.
    detail = read_mode_detail()
    mode, form, content = detail["mode"], detail["form"], detail["content"]
    if content is not None:
        lines.append("Drop-in: yes ({} mode{})".format(
            mode, ", {} form".format(form) if form else ""))
        lines.append("  Contents: {}".format(content.strip()))
        # Say so when on/off will refuse. "--status lies" was the original
        # HAM-184 complaint; staying quiet about a file this script cannot
        # rewrite is the same failure one step further on.
        if detail["problems"]:
            lines.append("  WARNING: names a mode in a shape sensors.py "
                         "cannot read; --on/--off will refuse")
        elif len(detail["sites"]) > 1:
            lines.append("  WARNING: names a mode in {} places; --on/--off "
                         "will refuse until one remains"
                         .format(len(detail["sites"])))
    elif mode == MODE_UNKNOWN:
        # Present but unreadable is not the same as absent, and reporting it as
        # "default mode" would be a confident lie about a live unit.
        lines.append("Drop-in: present but could not be read ({})"
                     .format(DROPIN_PATH))
    else:
        lines.append("Drop-in: no (default mode)")

    # Brokkr service
    result = subprocess.run(
        ["systemctl", "is-active", BROKKR_SERVICE],
        capture_output=True, text=True)
    state = result.stdout.strip() if result.stdout else "unknown"
    lines.append("Brokkr service: {}".format(state))

    # Brokkr mode
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
        # The SAME plan the real run would execute, so the two cannot drift
        # apart on which cases refuse or on what gets written. Stop where the
        # real run would stop: printing the rest of the sequence after a
        # REFUSE showed an operator a run that completes, for a case that
        # hard-fails.
        plan = plan_mode(sensor_on=args.sensor_on)
        if plan["kind"] == "refuse":
            print("  REFUSE: {}".format(plan["summary"]))
            print(plan["reason"])
            return 1
        print("  Mode: {} -> {}".format(plan["current"], plan["mode"]))
        if plan["kind"] == "noop":
            print("  (no drop-in change)")
        elif plan["kind"] == "delete":
            print("  sudo rm -f {}".format(DROPIN_PATH))
        elif plan["mode"] == MODE_DEFAULT:
            print("  Rewrite {} without the mode directive "
                  "(other directives present)".format(DROPIN_PATH))
        else:
            print("  Write {} ({} mode)".format(DROPIN_PATH, plan["mode"]))
        print("  sudo systemctl daemon-reload")
        if args.sensor_on:
            print("  {} --pin {} {}".format(
                RELAY_SCRIPT, config["pin"], relay_flag))
        print("  sudo systemctl start {}".format(BROKKR_SERVICE))
        return 0

    # Prepare the notifier up front; failures here are non-fatal and just
    # disable notifications for this run. This has to come BEFORE the
    # pre-flight: a refusal that returns before build_sender() prints to a
    # tunnelled stdout nobody is watching and sends no chat notice, which
    # from the operator's side is a silent no-op.
    sender = build_sender(load_notifier_config(main_toml_path))
    sensor_name, site = get_unit_identifier(config_path)
    action = "on" if args.sensor_on else "off"

    # Pre-flight. Every refusal -- including a failed post-condition on the
    # bytes we were about to write -- happens here, before any service is
    # stopped and before the relay moves.
    plan = plan_mode(sensor_on=args.sensor_on)
    if plan["kind"] == "refuse":
        print(plan["reason"])
        send_notification(sender, build_message(
            sensor_name, site, action, success=False, rc=1,
            reason="REFUSED: {}".format(plan["summary"])))
        return 1

    # Execute
    if args.sensor_on:
        rc = sensor_on(pin=config["pin"], active_high=config["active_high"],
                       plan=plan)
    else:
        rc = sensor_off(pin=config["pin"], active_high=config["active_high"],
                        plan=plan)

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
