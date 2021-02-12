#!/usr/bin/env python3
"""
Run a command over SSH on one, many or all HAMMA2 sensors.
"""

# Standard library modules
import argparse
import subprocess


# Top-level constants
WRAPPER_COMMAND = "ssh"
SENSOR_PREFIX = "hamma"
SENSOR_NUMBERS_DEFAULT = {1, 2}
TIMEOUT = 2


def run_one(sensor_number, command):
    command_full = [
        "ssh", f"{SENSOR_PREFIX}{sensor_number}", f'"{command}"']
    output = subprocess.run(
        command_full,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        encoding="utf-8",
        timeout=TIMEOUT,
        )
    return output.stdout


def run_all_ssh(command, sensor_numbers=None):
    if sensor_numbers is None:
        sensor_numbers = SENSOR_NUMBERS_DEFAULT
    for sensor_number in sensor_numbers:
        try:
            command_output = run_one(sensor_number, command)
        except Exception as e:
            print(type(e).__name__, "occured on sensor", sensor_number, ":", e)
        else:
            print("Output for sensor", sensor_number, ":\n", command_output)


def main(sys_argv=None):
    parser_main = argparse.ArgumentParser(
        description="Send commands to multiple sensors at once.",
        argument_default=argparse.SUPPRESS)
    # loki "COMMAND" [-n SENSOR1 SENSOR2 ...]
    parser_main.add_argument(
        "command", help="The command to run on each sensor.")
    parser_main.add_argument(
        "-n, --sensor-number", type=int, nargs="+", dest="sensor_number",
        help="The sensor(s) to run the command on. All, by default.")
    parsed_args = parser_main.parse_args(sys_argv)
    run_all_ssh(**vars(parsed_args))


if __name__ == "__main__":
    main()
