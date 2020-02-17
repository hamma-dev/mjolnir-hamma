#!/home/thor/dev/brokkrenv/bin/python

"""A simple script to change the load state of a MPPT-15L charge controller."""

# Standard library imports
import argparse
import logging
from pathlib import Path
import time

# Third party imports
import pymodbus.client.sync
try:
    import brokkr.monitoring.sunsaver as sunsaver
except ModuleNotFoundError:
    import brokkr.sunsaver as sunsaver


COIL_STATES = {"off": 1, "on": 0}
LOAD_STATES = {
    0: "Start",
    1: "Load on",
    2: "LVD Warning",
    3: "Low Voltage Disconnect",
    4: "Load Fault",
    5: "Manual Disconnect",
    }

def _setup_mppt_client():
    # Setup modbus connection
    if Path("/dev/ttyUSB0").exists():
        port = "/dev/ttyUSB0"
    elif Path("/dev/ttyUSB1").exists():
        port = "/dev/ttyUSB1"
    else:
        raise RuntimeError("Could not find serial port.")
    mppt_client = pymodbus.client.sync.ModbusSerialClient(
        port=port, **sunsaver.SERIAL_PARAMS_SUNSAVERMPPT15L)
    mppt_client.connect()
    return mppt_client


def _print_load_state(load_state, verbose=False):
    try:
        load_state = load_state.registers[0]
    except Exception:
        pass
    load_message = f"Load state: {load_state} - {LOAD_STATES[load_state]}"
    if verbose:
        print(load_message)
    return load_message


def get_load_state(verbose=False):
    """Check the current load state on the attached charge controller."""
    mppt_client = _setup_mppt_client()

    # Check current load state
    current_state = mppt_client.read_holding_registers(0x001A, 1, unit=1)
    _print_load_state(current_state, verbose)

    mppt_client.close()
    return current_state


def change_load_state(new_state, verbose=False):
    """Turn power to attached load ``off`` or ``on``."""
    mppt_client = _setup_mppt_client()

    # Check initial load state
    initial_state = mppt_client.read_holding_registers(0x001A, 1, unit=1)
    _print_load_state(initial_state, verbose)

    # Change load state
    if verbose:
        print(f"Turning load power {new_state}...")
    mppt_client.write_coil(0x0001, COIL_STATES[new_state], unit=1)

    # Wait for it to take effect
    time.sleep(1)

    # Check final load state
    final_state = mppt_client.read_holding_registers(0x001A, 1, unit=1)
    _print_load_state(final_state, verbose)

    mppt_client.close()
    return (initial_state, final_state)


if __name__ == "__main__":
    parser_main = argparse.ArgumentParser(
        description=("Change the load state of an attached MPPT-15L. "
                     "Run without arguments to report current status."),
        epilog=("Load States: 0 - Start; 1 - Load on; 2 - LVD Warning; "
                "3 - Low Voltage Disconnect; 4 - Fault; 5 - Manual Disconnect")
        )
    parser_main.add_argument(
        "-v", "--verbose", action="store_true",
        help="Get detailed debug information.")

    arg_group = parser_main.add_mutually_exclusive_group(required=False)
    arg_group.add_argument(
        "--off", action="store_const", const="off", dest="new_state",
        help="Turn the attached load off.")
    arg_group.add_argument(
        "--on", action="store_const", const="on", dest="new_state",
        help="Turn the attached load on.")

    parsed_args = parser_main.parse_args()
    if vars(parsed_args)["verbose"]:
        logging.basicConfig(format="{message}", style="{", level=logging.DEBUG)
    
    if vars(parsed_args).get("new_state", None) is not None:
        change_load_state(new_state=vars(parsed_args)["new_state"],
                          verbose=True)
    else:
        print("Pass -h or --help for usage information")
        get_load_state(verbose=True)
