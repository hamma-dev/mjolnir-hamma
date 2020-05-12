#!/usr/bin/env python3

"""
Control a generic relay with a GPIO pin.
"""

# Standard library imports
import argparse

# Third pary imports
import gpiozero
import gpiozero.pins.rpigpio


RELAY_PIN = 17


def toggle_relay(pin=RELAY_PIN, new_state=False):
    """Toggle a generic relay connected via GPIO on or off."""

    # Hack to avoid pins being reset, until this is fixed in gpiozero
    def close(self):  # pylint: disable=unused-argument
        pass

    gpiozero.pins.rpigpio.RPiGPIOPin.close = close

    print(f"Turning relay {'on' if new_state else 'off'}")
    relay = gpiozero.DigitalOutputDevice(
        pin, active_high=False, initial_value=new_state,
        pin_factory=gpiozero.pins.rpigpio.RPiGPIOFactory())
    return relay


def main():
    """Toggle a generic relay connected via GPIO on or off."""
    arg_parser = argparse.ArgumentParser(description="Turn a relay on or off")
    arg_parser.add_argument("--pin", help="The BCM pin to use",
                            default=argparse.SUPPRESS)
    onoff = arg_parser.add_mutually_exclusive_group(required=True)
    onoff.add_argument("--on", action="store_true", dest="state",
                       help="Turn relay on")
    onoff.add_argument("--off", action="store_false", dest="state",
                       help="Turn relay off")
    parsed_args = arg_parser.parse_args()
    toggle_relay(**vars(parsed_args))


if __name__ == "__main__":
    main()
