#!/home/pi/dev/ltgenv/bin/python
"""HAM-118 helper for verify_deployment.sh's check_googlechat_key.

Resolves each notifier step's effective channel/method/key_file from
brokkr's own rendered config (brokkr.config.main.CONFIG -- presets, system
config/main.toml, local override, and the active mode overlay, in that
order, exactly as brokkr itself applies them at import time) and checks
the channel against the key file using the same parser notifiers uses:
configparser.ConfigParser(interpolation=None), reading config['channel'].

Must run under brokkr's own interpreter (ltgenv) since brokkr is only
importable there -- verify_deployment.sh invokes this with
/home/pi/dev/ltgenv/bin/python and skips the check entirely if that
interpreter is missing.

Never reads or prints a webhook value, or the text of a parse exception
(which can echo the offending line) -- only step names, channel/option
names, key-file paths, and exception *type* names.

Output, one line per step plus one "KEYFILE <path>" line per step that
reached the parsing stage (deduplicated by the caller):
    <step> PASS <channel>
    <step> FAIL <reason, may include a channel or path, never a value>
    <step> SKIP <reason>
    KEYFILE <path>
"""

import configparser
import os
import sys

STEPS = ("state_monitor", "noise_diag")


def _check_step(step, step_config):
    if step_config is None:
        print("{} SKIP absent from rendered config".format(step))
        return

    if step_config.get("_enabled", True) is False:
        print("{} SKIP disabled (_enabled = false)".format(step))
        return

    method = step_config.get("method")
    if method != "gchat":
        print("{} SKIP method is {!r}, not gchat".format(step, method))
        return

    channel = step_config.get("channel")
    if not channel:
        print("{} FAIL no channel configured".format(step))
        return

    key_file = step_config.get("key_file")
    if not key_file:
        print("{} FAIL no key_file configured".format(step))
        return

    print("KEYFILE {}".format(key_file))

    if not os.path.exists(key_file):
        print("{} FAIL key file not found ({})".format(step, key_file))
        return
    if os.path.getsize(key_file) == 0:
        print("{} FAIL key file is empty ({})".format(step, key_file))
        return

    parser = configparser.ConfigParser(interpolation=None)
    try:
        read_ok = parser.read(key_file)
    except configparser.Error as exc:
        # Covers DuplicateSectionError, DuplicateOptionError,
        # MissingSectionHeaderError, ParsingError, etc. Never str(exc):
        # ParsingError in particular embeds the offending source line.
        print("{} FAIL key file failed to parse ({})".format(
            step, type(exc).__name__))
        return
    except OSError as exc:
        print("{} FAIL key file could not be read ({})".format(
            step, type(exc).__name__))
        return

    if not read_ok:
        print("{} FAIL key file could not be opened (permissions?)".format(
            step))
        return

    if "channel" not in parser:
        print("{} FAIL key file has no [channel] section".format(step))
        return

    # configparser lowercases option names (and the lookup below), so
    # "STATUS = ..." satisfies channel "status". A present-but-empty value
    # is falsy, same as an absent key -- both must FAIL per HAM-118.
    if not parser["channel"].get(channel):
        print("{} FAIL channel {!r} not found (or empty) in key file".format(
            step, channel))
        return

    print("{} PASS {}".format(step, channel))


def main():
    try:
        import brokkr.config.main as brokkr_main
    except Exception as exc:  # noqa: BLE001 -- report, never crash the check
        for step in STEPS:
            print("{} SKIP brokkr import failed ({})".format(
                step, type(exc).__name__))
        return 0

    steps_config = brokkr_main.CONFIG.get("steps", {})
    for step in STEPS:
        _check_step(step, steps_config.get(step))
    return 0


if __name__ == "__main__":
    sys.exit(main())
