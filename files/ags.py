#!/usr/bin/env python3

"""A simple wrapper script to send AGS commands to a HAMMA2 sensor."""

# Standard library imports
import argparse
import socket
import time


SOCKET_BUFFER = 4096

AGS_COMMAND_PORT = 8082
SENSOR_IP = "10.10.10.1"


def netcat(content, host, port, recieve_reply=True, timeout=1):
    recieved_data = None
    time_start = time.monotonic()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
        sock.sendall(content.encode())
        sock.shutdown(socket.SHUT_WR)

        recieved_chunks = []
        if recieve_reply:
            while not timeout or time.monotonic() <= (time_start + timeout):
                try:
                    recieved_data = sock.recv(SOCKET_BUFFER)
                except socket.timeout:
                    break
                if not recieved_data:
                    break
                recieved_chunks.append(recieved_data)
            recieved_data = b"".join(recieved_chunks)

    return recieved_data


def send_ags_command(command, host=SENSOR_IP, port=AGS_COMMAND_PORT):
    reply_text = netcat(command, host=host, port=port).decode()
    return reply_text


if __name__ == "__main__":
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
