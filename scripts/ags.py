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
