#!/home/thor/dev/brokkrenv/bin/python

"""Monitor, program and control a Sunsaver MPPT-15L charge controller."""

# Standard library imports
import argparse
import csv
import logging
from pathlib import Path
import sys
import time

# Third party imports
import pymodbus.client.sync
import pymodbus.constants
import pymodbus.payload

# Local imports
import brokkr.output
try:
    import brokkr.monitoring.sunsaver as sunsaver
except ModuleNotFoundError:
    import brokkr.sunsaver as sunsaver


__version__ = "0.2.0"

LOAD_COIL_STATES = {True: "Off", False: "On"}
LOAD_INPUT_STATES = {None: None, "off": True, "on": False}
LOAD_REGISTER_STATES = {
    0: "Start",  # analysis:ignore
    1: "Load on",
    2: "LVD Warning",
    3: "Low Voltage Disconnect",
    4: "Load Fault",
    5: "Manual Disconnect",
    }

LOAD_STATE_COIL = 0x0001
LOAD_STATE_REGISTER = 0x001A

LOG_BLOCKS = 32
LOG_BLOCK_LENGH = 16
LOG_START_REGISTER = 0x8000
LOG_VARIABLES = [
    ("hourmeter", "I"),
    ("alarm_daily", "B"),
    ("vb_min_daily", "V"),
    ("vb_max_daily", "V"),
    ("ahc_daily", "Ah"),
    ("ahl_daily", "Ah"),
    ("array_fault_daily", "B"),
    ("load_fault_daily", "B"),
    ("va_max_daily", "V"),
    ("tune_ab_daily", "I"),
    ("tune_eq_daily", "I"),
    ("tune_fl_daily", "I"),
    ]

RESET_COIL = 0x00FF
RESET_REGISTER = LOAD_STATE_REGISTER

SUBCOMMAND_PARAM = "subcommand_name"

UNIT_ID = 1


def _hex(x):
    return int(x, 0)


def _setup_mppt_client(port="COM3"):
    # Setup modbus connection
    if port is None:
        try:
            port_object = sunsaver.get_serial_port()
            port = port_object.device
        except Exception:
            if Path("/dev/ttyUSB0").exists():
                port = "/dev/ttyUSB0"
            elif Path("/dev/ttyUSB1").exists():
                port = "/dev/ttyUSB1"
            else:
                raise RuntimeError("Could not find serial port.")
    serial_params = getattr(sunsaver, "SERIAL_PARAMS_MPPT15L",
                            getattr(sunsaver, "SERIAL_PARAMS_SUNSAVERMPPT15L",
                                    None))
    mppt_client = pymodbus.client.sync.ModbusSerialClient(
        port=port, **serial_params)
    mppt_client.connect()
    return mppt_client


def _default_print(value):
    try:
        return value.registers[0]
    except AttributeError:
        try:
            return value.bits[0]
        except AttributeError:
            return value


def _pretty_print_load_state(load_state):
    load_state = _default_print(load_state)
    if load_state is True or load_state is False:
        load_state_pretty = LOAD_COIL_STATES[load_state]
    else:
        load_state_pretty = LOAD_REGISTER_STATES[load_state]
    return f"{load_state} - {load_state_pretty}"


def _get_raw_log_data(blocks=LOG_BLOCKS, verbose=False):
    """Retrieve and format the logged data from the MPPT device."""
    with _setup_mppt_client() as mppt_client:
        # Get raw log data
        log_data_blocks = []
        for block_n in range(blocks):
            if verbose:
                sys.stdout.write(
                    f"\rGetting data block {block_n + 1} of {blocks}")
                sys.stdout.flush()
            log_data_block = mppt_client.read_holding_registers(
                address=LOG_START_REGISTER + block_n * LOG_BLOCK_LENGH,
                count=LOG_BLOCK_LENGH,
                unit=UNIT_ID,
                )
            log_data_blocks.append(log_data_block.registers)

        if verbose:
            print("")

    return log_data_blocks


def get_log_data(sort=True, filter_bad=True, verbose=False):
    """Get logged data from the sunsaver, and optionally sort and filter it."""
    log_data = []
    log_data_blocks = _get_raw_log_data(verbose=verbose)

    # Convert data via conversion functions to output format
    for data_block in log_data_blocks:
        log_data_block = {}
        decoder = pymodbus.payload.BinaryPayloadDecoder.fromRegisters(
            data_block,
            byteorder=pymodbus.constants.Endian.Big,
            wordorder=pymodbus.constants.Endian.Big,
            )
        for variable_name, variable_type in LOG_VARIABLES:
            if variable_name == "alarm_daily":
                log_data_block[variable_name] = (
                    sunsaver.CONVERSION_FUNCTIONS[variable_type](
                        decoder.decode_32bit_uint()))
            else:
                log_data_block[variable_name] = (
                    sunsaver.CONVERSION_FUNCTIONS[variable_type](
                        decoder.decode_16bit_uint()))
        log_data.append(log_data_block)

    if filter_bad:
        log_data = [log_data_block for log_data_block in log_data
                    if log_data_block["hourmeter"] not in {0x0000, 0xFFFF}]
    if sort:
        log_data.sort(key=lambda inner: inner["hourmeter"])

    return log_data


def write_log_data(
        log_data=None, output_path=None, verbose=False, **kwargs):
    """Write logged data to a CSV."""
    if log_data is None:
        log_data = get_log_data(verbose=verbose, **kwargs)
    if output_path is None:
        for log_data_block in log_data:
            print(log_data_block)
        return None
    if verbose:
        print(f"Writing logged data to CSV at {output_path}")

    with open(output_path, "w", encoding="utf-8", newline="") as data_csv:
        csv_writer = csv.DictWriter(
            data_csv, log_data[0].keys(), **brokkr.output.CSV_PARAMS)
        csv_writer.writeheader()
        csv_writer.writerows(log_data)

    return output_path


def get_set_value(
        address, address_type,
        write_value=None, write_address=None, write_address_type=None,
        print_fn=_default_print, verbose=False):
    """Get or set a Modbus register or coil."""

    write_address = address if write_address is None else write_address
    write_address_type = (address_type if write_address_type is None
                          else write_address_type)

    with _setup_mppt_client() as mppt_client:
        # Setup address types
        address_types = {"register", "coil"}
        if address_type == "register":
            read_fn = mppt_client.read_holding_registers
        elif address_type == "coil":
            read_fn = mppt_client.read_coils
        else:
            raise ValueError(
                f"Address type {address_type} not in {address_types}")
        if write_value is not None:
            if write_address_type == "register":
                write_fn = mppt_client.write_register
            elif write_address_type == "coil":
                write_fn = mppt_client.write_coil
            else:
                raise ValueError(f"Write address type {write_address_type} "
                                 f"not in {address_types}")

        # Check initial load state
        initial_state = read_fn(address, 1, unit=UNIT_ID)
        if verbose:
            print(f"Initial state of {address_type} 0x{address:X}: "
                  f"{print_fn(initial_state)}")

        # Return early if we don't need to change the value
        if write_value is None:
            return initial_state

        # Change the value
        if verbose:
            print(f"Writing {print_fn(write_value)} to "
                  f"{write_address_type} 0x{write_address:X}...")
        write_fn(write_address, write_value, unit=UNIT_ID)

        # Wait for it to take effect
        time.sleep(1)

        # Check final value
        final_state = read_fn(address, 1, unit=UNIT_ID)
        print(f"Final state: {print_fn(final_state)}")

    return (initial_state, final_state)


def reset_device(verbose=False):
    """Reset charge controller to clear faults and update with new EEPROM."""
    states = get_set_value(
        address=RESET_REGISTER,
        address_type="register",
        write_value=1,
        write_address=RESET_COIL,
        write_address_type="coil",
        print_fn=_pretty_print_load_state,
        verbose=verbose,
        )
    return states


def get_set_load_power(new_state=None, verbose=False):
    """Turn power to attached load ``off`` or ``on``."""
    states = get_set_value(
        address=LOAD_STATE_REGISTER,
        address_type="register",
        write_value=LOAD_INPUT_STATES[new_state],
        write_address=LOAD_STATE_COIL,
        write_address_type="coil",
        print_fn=_pretty_print_load_state,
        verbose=verbose,
        )
    return states


if __name__ == "__main__":
    parser_main = argparse.ArgumentParser(
        description=("Monitor, program and control a Sunsaver MPPT-15L."))
    parser_main.add_argument(
        "--version", action="store_true",
        help="If passed, will print the version and exit")
    parser_main.add_argument(
        "-v", "--verbose", action="store_true",
        help="Get detailed debug information.")
    subparsers = parser_main.add_subparsers(
        title="Subcommands", help="Subcommand to execute",
        metavar="Subcommand", dest=SUBCOMMAND_PARAM)

    parser_register = subparsers.add_parser(
        "register", help="Get or set the value of a register")
    parser_coil = subparsers.add_parser(
        "coil", help="Get or set the value of a coil")
    for parser in [parser_coil, parser_register]:
        parser.add_argument(
            "address", type=_hex,
            help="Address (PDU, not logical) to read or write, in hex")
        parser.add_argument(
            "write_value", type=int, nargs="?", default=None,
            help="Value to write to the specified address, in decimal")

    parser_reset = subparsers.add_parser(
        "reset", help="Reset the device, clearing faults and updating EEPROM")

    parser_power = subparsers.add_parser(
        "power", help="Power the load on or off, and check its load status")
    parser_power.add_argument(
        "new_state", choices={None, "on", "off"}, nargs="?", default=None,
        help="Whether to turn the load on or off; omit for current status")

    parser_log = subparsers.add_parser(
        "log", help="Get the sensor logging data, and print or write to CSV")
    parser_log.add_argument(
        "--output-path", default=None,
        help=("Write the data as CSV to the passed path, otherwise print it"))

    parsed_args = parser_main.parse_args()
    if parsed_args.version:
        print(f"Sunsaver version {__version__}")
        sys.exit(0)
    if parsed_args.verbose:
        logging.basicConfig(format="{message}", style="{", level=logging.DEBUG)

    elif getattr(parsed_args, SUBCOMMAND_PARAM, None) in {"register", "coil"}:
        get_set_value(
            address=parsed_args.address,
            address_type=getattr(parsed_args, SUBCOMMAND_PARAM),
            write_value=parsed_args.write_value,
            verbose=True,
            )
    elif getattr(parsed_args, SUBCOMMAND_PARAM, None) == "reset":
        reset_device(verbose=True)
    elif getattr(parsed_args, SUBCOMMAND_PARAM, None) == "power":
        get_set_load_power(new_state=parsed_args.new_state, verbose=True)
    elif getattr(parsed_args, SUBCOMMAND_PARAM, None) == "log":
        write_log_data(output_path=parsed_args.output_path, verbose=True)
    else:
        parser_main.print_usage()
