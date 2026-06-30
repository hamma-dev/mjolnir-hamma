#!/usr/bin/env python3

"""A simple wrapper script to send AGS commands to a HAMMA2 sensor."""

# Standard library imports
import argparse
import socket
import time


AGS_COMMAND_PORT = 8082
SENSOR_IP = "10.10.10.1"
SOCKET_BUFFER = 4096

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


def set_threshold(channel, millivolts, persist=False,
                  host=SENSOR_IP, port=AGS_COMMAND_PORT):
    """Set a DAC trigger threshold (input-referred mV) on a HAMMA2 sensor."""
    channel = int(channel)
    if channel not in THRESHOLD_CHANNELS:
        raise ValueError("threshold channel must be 1 or 2")
    ags_value = mv_to_ags(millivolts)
    command = "das_set_threshold {} {}".format(channel, _format_ags(ags_value))
    return send_ags_command(command, host=host, port=port)


GAIN_REGISTERS = {"fast-e": "8", "slow-e": "10"}
GAIN_LEVELS = (0, 1, 2, 3)


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
    return send_ags_command(command, host=host, port=port)


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


def main():
    parser_main = argparse.ArgumentParser(
        description=(
            "Send an AGS command to a HAMMA2 sensor."
            "Send 'help' to list all commands supported by the sensor."),
        argument_default=argparse.SUPPRESS)

    parser_main.add_argument(
        "command", nargs="?", default="help",
        help="The AGS command to send; send 'help' for a list.")
    parser_main.add_argument(
        "--host", help="The hostname/IP of the sensor (default 10.10.10.1)")
    parser_main.add_argument(
        "--port", help="The AGS command port of the sensor (default 8082)")

    parsed_args = parser_main.parse_args()
    print(send_ags_command(**vars(parsed_args)))


if __name__ == "__main__":
    main()
