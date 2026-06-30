#!/usr/bin/env python3

"""A simple wrapper script to send AGS commands to a HAMMA2 sensor."""

# Standard library imports
import argparse
import socket
import subprocess
import sys
import time


AGS_COMMAND_PORT = 8082
SENSOR_IP = "10.10.10.1"
SOCKET_BUFFER = 4096
AGS_SSH_HOST = "hamma"
STARTUP_PATH = "/ags/scripts/startup"

GAIN_FACTOR = 6.024  # fixed analog factor; input-referred volts = ags_value / GAIN_FACTOR


def mv_to_ags(millivolts):
    """Convert an input-referred threshold in mV to the AGS das value."""
    millivolts = float(millivolts)
    if millivolts < 0:
        raise ValueError("threshold mV must be non-negative")
    return millivolts / 1000.0 * GAIN_FACTOR


def ags_to_mv(ags):
    """Convert an AGS das value back to the input-referred threshold in mV."""
    return float(ags) / GAIN_FACTOR * 1000.0


def netcat(content, host, port, recieve_reply=True, timeout=1):
    recieved_data = None
    time_start = time.monotonic()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
        sock.sendall(content.encode())
        sock.shutdown(socket.SHUT_WR)

        if recieve_reply:
            recieved_chunks = []
            while not timeout or time.monotonic() <= (time_start + timeout):
                try:
                    recieved_data = sock.recv(SOCKET_BUFFER)
                except socket.timeout:
                    break
                if not recieved_data:
                    break
                recieved_chunks.append(recieved_data)
            recieved_data = b"".join(recieved_chunks)

        sock.shutdown(socket.SHUT_RD)

    return recieved_data


def send_ags_command(command, host=SENSOR_IP, port=AGS_COMMAND_PORT):
    reply_text = netcat(command, host=host, port=port).decode()
    return reply_text


THRESHOLD_CHANNELS = (1, 2)


def _format_ags(ags_value):
    return "{:g}".format(ags_value)


def _reply_indicates_error(reply):
    """True if any line of an AGS reply signals a firmware error."""
    return any(line.strip().startswith("Error") for line in reply.splitlines())


def set_threshold(channel, millivolts, persist=False,
                  host=SENSOR_IP, port=AGS_COMMAND_PORT):
    """Set a DAC trigger threshold (input-referred mV) on a HAMMA2 sensor."""
    channel = int(channel)
    if channel not in THRESHOLD_CHANNELS:
        raise ValueError("threshold channel must be 1 or 2")
    ags_value = mv_to_ags(millivolts)
    command = "das_set_threshold {} {}".format(channel, _format_ags(ags_value))
    reply = send_ags_command(command, host=host, port=port)
    # Only persist if the sensor accepted the live value. The firmware
    # replies with an "Error -" line when it rejects a value (e.g. out of
    # range); persisting that would store a bad value in the startup script.
    if persist and not _reply_indicates_error(reply):
        persist_startup(["das_set_threshold", str(channel)],
                        "das_set_threshold {} {}".format(channel, _format_ags(ags_value)))
    return reply


GAIN_REGISTERS = {"fast-e": "8", "slow-e": "10"}
GAIN_LEVELS = (0, 1, 2, 3)


def persist_startup(match_tokens, new_line, host=AGS_SSH_HOST):
    """Rewrite one line of the sensor's startup script via ssh, atomically."""
    read = subprocess.run(
        ["ssh", host, "cat {}".format(STARTUP_PATH)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if read.returncode != 0:
        raise RuntimeError("could not read {}: {}".format(
            STARTUP_PATH, read.stderr.decode(errors="replace")))
    new_text = rewrite_startup(read.stdout.decode(), match_tokens, new_line)
    write_cmd = "cat > {0}.tmp && mv {0}.tmp {0}".format(STARTUP_PATH)
    write = subprocess.run(
        ["ssh", host, write_cmd], input=new_text.encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if write.returncode != 0:
        raise RuntimeError("could not write {}: {}".format(
            STARTUP_PATH, write.stderr.decode(errors="replace")))


def set_gain(channel, level, persist=False,
             host=SENSOR_IP, port=AGS_COMMAND_PORT):
    """Set an FCM gain level (0-3) on a HAMMA2 sensor."""
    if channel not in GAIN_REGISTERS:
        raise ValueError("gain channel must be one of: "
                         + ", ".join(sorted(GAIN_REGISTERS)))
    level = int(level)
    if level not in GAIN_LEVELS:
        raise ValueError("gain level must be 0, 1, 2, or 3")
    register = GAIN_REGISTERS[channel]
    command = "das_send_command {} {}".format(register, level)
    reply = send_ags_command(command, host=host, port=port)
    # Only persist if the sensor accepted the live value (see set_threshold).
    if persist and not _reply_indicates_error(reply):
        persist_startup(["das_send_command", register],
                        "das_send_command {} {}".format(register, level))
    return reply


def rewrite_startup(text, match_tokens, new_line):
    """Return startup-file text with the line matching match_tokens replaced.

    Match is on leading whitespace-split tokens (so "8" never matches "80").
    If no line matches, new_line is inserted before the first das_reset line,
    or appended if there is no das_reset.
    """
    match_tokens = list(match_tokens)
    n = len(match_tokens)
    lines = text.splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.split()[:n] == match_tokens:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        insert_at = None
        for i, line in enumerate(out):
            if line.split()[:1] == ["das_reset"]:
                insert_at = i
                break
        if insert_at is None:
            out.append(new_line)
        else:
            out.insert(insert_at, new_line)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def parse_startup_state(text):
    """Parse persisted threshold (mV) and gain levels from startup-file text."""
    state = {}
    for line in text.splitlines():
        toks = line.split()
        if len(toks) >= 3 and toks[0] == "das_set_threshold":
            channel = toks[1]
            if channel in ("1", "2"):
                state["threshold_{}_mv".format(channel)] = round(
                    ags_to_mv(float(toks[2])), 1)
        elif len(toks) >= 3 and toks[0] == "das_send_command":
            if toks[1] == "8":
                state["gain_fast"] = int(toks[2])
            elif toks[1] == "10":
                state["gain_slow"] = int(toks[2])
    return state


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] == "set-threshold":
        parser = argparse.ArgumentParser(prog="ags.py set-threshold")
        parser.add_argument("channel", type=int, help="threshold channel (1 or 2)")
        parser.add_argument("millivolts", type=float,
                            help="input-referred threshold in mV")
        parser.add_argument("--persist", action="store_true",
                            help="also write /ags/scripts/startup")
        ns = parser.parse_args(argv[1:])
        print(set_threshold(ns.channel, ns.millivolts, persist=ns.persist))
        return

    if argv and argv[0] == "set-gain":
        parser = argparse.ArgumentParser(prog="ags.py set-gain")
        parser.add_argument("channel", choices=sorted(GAIN_REGISTERS),
                            help="gain channel")
        parser.add_argument("level", type=int, help="gain level (0-3)")
        parser.add_argument("--persist", action="store_true",
                            help="also write /ags/scripts/startup")
        ns = parser.parse_args(argv[1:])
        print(set_gain(ns.channel, ns.level, persist=ns.persist))
        return

    if argv and argv[0] == "get-state":
        read = subprocess.run(
            ["ssh", AGS_SSH_HOST, "cat {}".format(STARTUP_PATH)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if read.returncode != 0:
            print("[FAIL] could not read startup: "
                  + read.stderr.decode(errors="replace"))
            return
        state = parse_startup_state(read.stdout.decode())
        for key in ("threshold_1_mv", "threshold_2_mv", "gain_fast", "gain_slow"):
            if key in state:
                print("{}: {}".format(key, state[key]))
        return

    # Generic passthrough (original behaviour)
    parser_main = argparse.ArgumentParser(
        description=(
            "Send an AGS command to a HAMMA2 sensor. "
            "Subcommands: set-threshold, set-gain, get-state. "
            "Otherwise send a raw command; send 'help' for the sensor's list."),
        argument_default=argparse.SUPPRESS)
    parser_main.add_argument("command", nargs="?", default="help",
                             help="The AGS command to send.")
    parser_main.add_argument("--host", help="Sensor host/IP (default 10.10.10.1)")
    parser_main.add_argument("--port", help="AGS command port (default 8082)")
    parsed_args = parser_main.parse_args(argv)
    print(send_ags_command(**vars(parsed_args)))


if __name__ == "__main__":
    main()
