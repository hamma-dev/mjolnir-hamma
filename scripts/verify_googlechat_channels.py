#!/home/pi/dev/ltgenv/bin/python
"""HAM-118 helper for verify_deployment.sh's check_googlechat_key.

Read-only, always. An earlier version imported brokkr.config.main to ask
brokkr for the "effective" rendered config, but importing it can WRITE a
missing local config file back to disk (FileConfigLevel writes its
defaults on first read) -- as root, that means new root-owned files
under /home/pi/.config/brokkr, or a brand new /root/.config/brokkr.
verify_deployment.sh must never write anything, so this scans the TOML
files directly with tomli instead of importing brokkr at all.

Deliberately does NOT merge or resolve which config layer is "effective"
for a given step. It scans every steps.* table in:
    config/main.toml                         (system)
    ~/.config/brokkr/hamma/main.toml          (local override, if present)
    config/mode.toml                          (every mode table, system)
    ~/.config/brokkr/hamma/mode.toml          (every mode table, local)
and for every gchat step's channel found anywhere, checks it against the
key file (default /home/pi/.googlechat, or that step's own key_file) --
so a channel reachable only through a mode that is not currently active
is still caught, which an "effective config" view would miss.

The key file is parsed the same way the notifiers package does:
configparser.ConfigParser(interpolation=None), reading options under a
single [channel] table. Never prints a webhook value or an exception's
string form (ParsingError embeds the offending source line; only the
exception's type name is ever printed).

Must always exit 0 and report every problem through its own output --
verify_deployment.sh's caller treats a nonzero exit or empty output as a
helper crash (FAIL), not a value to trust, but this still must not rely
on that: a bug here must never abort the rest of verify_deployment.sh.

Output, one line per item, consumed by check_googlechat_key:
    KEYFILE_PATH <path>           -- a key file was opened (for perms)
    PASS <message>
    FAIL <message>
    SKIP <message>
"""

import configparser
import os
import sys

# Overridable via environment for tests only; unset in production, so
# these are exactly the hardcoded paths the real check always used.
PI_HOME = os.environ.get("GOOGLECHAT_PI_HOME") or "/home/pi"
DEFAULT_KEY_FILE = os.environ.get("GOOGLECHAT_DEFAULT_KEY_FILE") or (
    PI_HOME + "/.googlechat")

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
SYSTEM_MAIN_TOML = os.environ.get("GOOGLECHAT_SYSTEM_MAIN_TOML") or os.path.join(
    REPO_ROOT, "config", "main.toml")
SYSTEM_MODE_TOML = os.environ.get("GOOGLECHAT_SYSTEM_MODE_TOML") or os.path.join(
    REPO_ROOT, "config", "mode.toml")
LOCAL_MAIN_TOML = os.environ.get("GOOGLECHAT_LOCAL_MAIN_TOML") or os.path.join(
    PI_HOME, ".config", "brokkr", "hamma", "main.toml")
LOCAL_MODE_TOML = os.environ.get("GOOGLECHAT_LOCAL_MODE_TOML") or os.path.join(
    PI_HOME, ".config", "brokkr", "hamma", "mode.toml")


def expand_pi_home(path):
    """Expand a leading '~' as pi's home, never the executing user's --
    this may run as root, where os.path.expanduser('~') resolves to
    /root and would silently check the wrong file."""
    if path == "~":
        return PI_HOME
    if path.startswith("~/"):
        return PI_HOME + path[1:]
    return path


def find_step_tables(data):
    """Yield (label, step_dict) for every table under a "steps" key,
    in either shape found in these configs:
      - main.toml:  data["steps"][name]
      - mode.toml:  data[mode_name]["main"]["steps"][name]
    """
    results = []

    steps = data.get("steps")
    if isinstance(steps, dict):
        for name, step in steps.items():
            if isinstance(step, dict):
                results.append(("steps.{}".format(name), step))

    for top_key, top_val in data.items():
        if not isinstance(top_val, dict):
            continue
        main_val = top_val.get("main")
        if not isinstance(main_val, dict):
            continue
        mode_steps = main_val.get("steps")
        if not isinstance(mode_steps, dict):
            continue
        for name, step in mode_steps.items():
            if isinstance(step, dict):
                results.append(
                    ("{}.main.steps.{}".format(top_key, name), step))

    return results


class KeyFileResult(object):
    def __init__(self, ok, parser, reason):
        self.ok = ok
        self.parser = parser
        self.reason = reason


def load_key_file(path, cache):
    if path in cache:
        return cache[path]
    result = _load_key_file_uncached(path)
    cache[path] = result
    return result


def _load_key_file_uncached(path):
    if not os.path.exists(path):
        return KeyFileResult(False, None, "is missing")
    try:
        if os.path.getsize(path) == 0:
            return KeyFileResult(False, None, "is empty")
    except OSError as exc:
        return KeyFileResult(
            False, None, "could not be read ({})".format(type(exc).__name__))

    parser = configparser.ConfigParser(interpolation=None)
    try:
        # Explicit encoding: deterministic across hosts/locales, and lets
        # a non-UTF-8 file raise here (configparser.read() silently
        # swallows OSError per-file, but not a decoding error).
        read_ok = parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        # Covers DuplicateSectionError, DuplicateOptionError,
        # MissingSectionHeaderError, ParsingError, etc. Never str(exc):
        # ParsingError in particular embeds the offending source line.
        return KeyFileResult(
            False, None, "failed to parse ({})".format(type(exc).__name__))
    except ValueError as exc:
        # Includes UnicodeDecodeError (a ValueError subclass).
        return KeyFileResult(
            False, None, "could not be decoded ({})".format(type(exc).__name__))
    except OSError as exc:
        return KeyFileResult(
            False, None, "could not be read ({})".format(type(exc).__name__))

    if not read_ok:
        return KeyFileResult(False, None, "could not be opened (permissions?)")
    if "channel" not in parser:
        return KeyFileResult(False, None, "has no [channel] section")
    return KeyFileResult(True, parser, None)


def main():
    cache = {}
    reported = set()

    def report_keyfile(path):
        if path in reported:
            return
        reported.add(path)
        print("KEYFILE_PATH {}".format(path))
        result = load_key_file(path, cache)
        if result.ok:
            print("PASS key file {} is readable and has a [channel] section"
                  .format(path))
        else:
            print("FAIL key file {} {}".format(path, result.reason))

    # Always check the default key file, independent of anything found
    # by scanning the TOML config below.
    report_keyfile(DEFAULT_KEY_FILE)

    try:
        import tomli
    except ImportError:
        try:
            # Python 3.11+ ships tomllib in the stdlib; ltgenv is 3.7 and
            # needs the separate tomli package, but accept either.
            import tomllib as tomli
        except ImportError:
            print("SKIP tomli not available -- cannot scan TOML config for channels")
            return 0

    sources = (
        ("config/main.toml (system)", SYSTEM_MAIN_TOML),
        ("main.toml (local override)", LOCAL_MAIN_TOML),
        ("config/mode.toml (system)", SYSTEM_MODE_TOML),
        ("mode.toml (local override)", LOCAL_MODE_TOML),
    )

    for label, path in sources:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                data = tomli.load(f)
        except tomli.TOMLDecodeError as exc:
            print("FAIL {} failed to parse ({})".format(path, type(exc).__name__))
            continue
        except OSError as exc:
            print("FAIL {} could not be read ({})".format(
                path, type(exc).__name__))
            continue

        for table_path, step in find_step_tables(data):
            source = "{} {}".format(label, table_path)

            channel = step.get("channel")
            if channel is None:
                continue
            if not isinstance(channel, str):
                # A TOML value can legally be an int/bool/array/table, and
                # configparser's optionxform crashes (AttributeError) on a
                # non-string option -- FAIL this one entry and keep going,
                # rather than letting one bad entry drop every later
                # source into the single generic "crashed" FAIL.
                print("FAIL channel in ({}) is not a string (got {})".format(
                    source, type(channel).__name__))
                continue

            method = step.get("method")
            if method is not None and not isinstance(method, str):
                print("FAIL method in ({}) is not a string (got {})".format(
                    source, type(method).__name__))
                continue
            if method is not None and method != "gchat":
                continue

            key_file_raw = step.get("key_file")
            if key_file_raw is not None and not isinstance(key_file_raw, str):
                print("FAIL key_file in ({}) is not a string (got {})".format(
                    source, type(key_file_raw).__name__))
                continue

            key_file = expand_pi_home(key_file_raw or DEFAULT_KEY_FILE)
            report_keyfile(key_file)

            result = load_key_file(key_file, cache)
            if not result.ok:
                print("FAIL channel {!r} ({}) -- key file {} {}".format(
                    channel, source, key_file, result.reason))
                continue
            if not result.parser["channel"].get(channel):
                print("FAIL channel {!r} ({}) not found (or empty) "
                      "in key file {}".format(channel, source, key_file))
            else:
                print("PASS channel {!r} ({}) present in key file {}".format(
                    channel, source, key_file))

    return 0


def run_and_report():
    """Must always exit 0 and report through stdout: the caller treats a
    nonzero exit or empty output as a crash (FAIL), but this must not
    rely on that -- any bug here is reported as a FAIL line, not an abort
    of the rest of verify_deployment.sh. BaseException (not just
    Exception) is intentional: nothing in main() should ever call
    sys.exit() or raise SystemExit, but if a future change (or a library
    it calls) did, this must still exit 0. Factored out from
    __main__ so a test can call it directly against a main() that was
    made to raise, without needing a real bug to demonstrate the catch.
    """
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        print("FAIL Google Chat check helper crashed unexpectedly ({})".format(
            type(exc).__name__))


if __name__ == "__main__":
    run_and_report()
    sys.exit(0)
