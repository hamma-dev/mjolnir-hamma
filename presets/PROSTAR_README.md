# ProStar MPPT Preset

This preset provides support for the Morningstar ProStar MPPT solar charge controller with Modbus RTU communication.

## Overview

The ProStar MPPT preset (`prostar_mppt.preset.toml`) enables monitoring and data collection from ProStar MPPT charge controllers. It reads a comprehensive set of registers including:

- Voltage and current measurements (battery, array, load)
- Temperature sensors (heatsink, battery, ambient, remote, inductors)
- Charge state and control status
- Power output and MPPT sweep data
- Accumulation registers (Ah, kWh, hourmeter)
- Daily statistics (min/max voltages, daily Ah, time in each charge stage)
- Fault and alarm bitfields

## Configuration

### Using the Preset in Your Pipeline

To use the ProStar MPPT preset in your Brokkr configuration, add it to your pipeline configuration in `config/main.toml`:

```toml
[pipelines.telemetry]
    monitor_input_steps = [
        "builtins.inputs.current_time",
        "prostar_mppt.inputs.ram",  # Add this line
        # ... other inputs
    ]
```

### Serial Port Configuration

The preset is configured for Modbus RTU communication with these default settings:
- **Baudrate**: 9600
- **Parity**: None (N)
- **Stop bits**: 2
- **Byte size**: 8
- **Unit ID**: 1

These settings match the factory defaults for ProStar MPPT controllers. If your controller uses different settings, you'll need to modify the preset file.

### Register Coverage

The preset reads registers 16-81 (0x10-0x51) in a single contiguous block, which includes:

| Address Range | Contents |
|---------------|----------|
| 16-25 | Current and voltage ADC readings |
| 26-32 | Temperature sensors |
| 33-37 | Charge state and reference voltages |
| 38-43 | Energy accumulation (Ah, kWh) |
| 44-49 | Load control and limits |
| 50-57 | Load accumulation, hourmeter, alarms |
| 58-64 | DIP switch, LED states, power, sweep data |
| 65-81 | Daily statistics and configuration |

## Utility Script

A command-line utility script is provided at `scripts/prostar.py` for direct interaction with the ProStar MPPT controller.

### Usage Examples

#### Get Help
```bash
~/prostar.py --help
~/prostar.py log --help
```

#### Read Historical Log Data
```bash
# Print log data to terminal
~/prostar.py log

# Write log data to CSV file
~/prostar.py log --output-path /path/to/output.csv
```

#### Control Load Power
```bash
# Check current load status
~/prostar.py power

# Turn load on or off
~/prostar.py power on
~/prostar.py power off
```

#### Read/Write Registers and Coils
```bash
# Read a register by hex address
~/prostar.py register 0x0021

# Write a value to a register
~/prostar.py register 0xE000 3440

# Read a coil
~/prostar.py coil 0x0001

# Write to a coil
~/prostar.py coil 0x0001 1
```

#### Reset Controller
```bash
# Reset the controller (clears faults and reloads EEPROM)
~/prostar.py reset
```

## Data Types and Conversions

The preset uses several data type conversions to properly decode the Modbus register values:

| Type | Description | Conversion Formula |
|------|-------------|-------------------|
| voltage | Battery/array voltages | n × 96.667 × 2^-15 V |
| current | Charge/load currents | n × 79.16 × 2^-15 A |
| power | Charger output power | n × 989.5 × 2^-16 W |
| temperature | Temperature sensors | n × 96.667 × 2^-15 °C |
| amphours | Amp-hour accumulation | n × 0.1 Ah |
| bitfield | Status/fault flags | Binary bitfield |

## Differences from SunSaver MPPT

While the ProStar MPPT and SunSaver MPPT-15L share similar Modbus communication settings, there are some key differences:

1. **Voltage Scaling**: ProStar uses 96.667 × 2^-15 scaling, while SunSaver uses 100 × 2^-15
2. **Register Addresses**: Different register maps (though some overlap)
3. **Additional Registers**: ProStar includes:
   - Phase inductor temperatures (U, V, W)
   - MPPT sweep data (Vmp, Pmax, Voc)
   - Load high voltage disconnect
   - More detailed daily statistics

## Troubleshooting

### Cannot Connect to Controller

1. Verify the USB-to-serial adapter is connected and recognized
2. Check that no other program is using the serial port
3. Verify the controller's Modbus settings match the preset configuration
4. Try specifying the port explicitly in the preset configuration

### Incorrect Data Values

1. Verify the Unit ID matches your controller's configuration (default is 1)
2. Check that the controller firmware is up to date
3. Ensure the serial communication settings match the controller configuration

### Read Timeouts

1. Increase the timeout in the preset configuration
2. Check for electrical noise on the communication lines
3. Verify proper RS-232 or RS-485 wiring and termination

## Technical References

- ProStar MPPT Modbus Specification (Morningstar Corporation)
- Register definitions based on MStar-WLAN project (mike-s123/MStar-WLAN)
- Brokkr framework documentation

## Version History

- **0.1.0** (2024): Initial release
  - Full register map for RAM registers 16-81
  - Utility script for command-line interaction
  - Compatible with Brokkr >= 0.4.0
