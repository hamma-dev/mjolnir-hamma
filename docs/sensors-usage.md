# sensors.py Usage Guide

Single command to turn a HAMMA sensor on or off. Handles relay power, brokkr mode switching, sindri restart, and telemetry CSV archiving.

## Quick Reference

```bash
sensors.py --on          # Power on sensor, brokkr default mode
sensors.py --off         # Power off sensor, brokkr nosensor mode
sensors.py --status      # Show current state
sensors.py --off --dry-run  # Preview what --off would do
```

## Setup (One-Time Per Sensor)

### 1. Get the script onto the Pi

```bash
# Either git pull (if branch is merged) or scp:
cd /home/pi/dev/mjolnir-hamma && git pull

# Ensure it's executable
chmod +x /home/pi/dev/mjolnir-hamma/scripts/sensors.py
```

### 2. Configure relay settings

Add a `[relay]` section to the **local** unit config (not the repo-level file):

```bash
cat >> ~/.config/brokkr/hamma/unit.toml <<EOF

[relay]
pin = 4
active_high = true
EOF
```

- `pin` — BCM GPIO pin number connected to the relay
- `active_high` — does energizing the relay power the sensor **on** (`true`) or **off** (`false`)?

If you don't know the values, check the sensor-log or test with `relay.py` directly and observe the charge controller's load current via `brokkr status`.

### 3. Verify

```bash
sensors.py --status       # Confirm config is loaded
sensors.py --off --dry-run  # Confirm correct relay flag
```

## What Each Command Does

### `--off` Sequence

1. Stop brokkr service
2. Stop sindri service
3. Toggle relay to power off sensor
4. Archive today's telemetry CSV (rename to `.bak`)
5. Write systemd drop-in (`BROKKR_MODE=nosensor`)
6. Reload systemd
7. Start brokkr (now runs in nosensor mode)
8. Start sindri

### `--on` Sequence

1. Stop brokkr service
2. Stop sindri service
3. Archive today's telemetry CSV
4. Remove systemd drop-in (back to default mode)
5. Reload systemd
6. Toggle relay to power on sensor
7. Start brokkr (back to default mode)
8. Start sindri

### Why the CSV is archived

Brokkr writes different columns in `default` vs `nosensor` mode. Switching modes mid-day would produce a CSV with mismatched columns. Archiving forces a fresh file with the correct headers.

### Why sindri is stopped and restarted

Sindri reads the telemetry CSV periodically. If the CSV is archived (renamed) while sindri is running, sindri crashes with `FileNotFoundError`. Stopping it before the archive and restarting after brokkr creates the new file avoids this.

## Verifying the Relay Toggled

**Ping is not a valid indicator.** The sensor's network interface (10.10.10.1) stays up even when the sensor instrument is powered off. Instead, check the charge controller:

```bash
brokkr status | grep "Load Current"
```

A drop of ~0.9 A confirms the relay cut power to the sensor. Example:

```
# Sensor on:
Load Current: 1.481 A

# Sensor off:
Load Current: 0.563 A
```

## Relay Polarity

The `active_high` setting describes the relationship between the relay being **energized** and the sensor being **powered on**:

| `active_high` | Energize relay | De-energize relay |
|---------------|----------------|-------------------|
| `true`        | Sensor ON      | Sensor OFF        |
| `false`       | Sensor OFF     | Sensor ON         |

`sensors.py` picks the correct `relay.py --on`/`--off` flag automatically. You just use `sensors.py --on` or `--off`.

## Idempotency

Running `--off` when already off, or `--on` when already on, is safe. The script re-applies the same state without errors. Repeated CSV archives use a timestamp suffix (`.bak.HHMMSS`) to avoid overwriting previous backups.

## Status Output

```
$ sensors.py --status
Drop-in: no (default mode)
Brokkr service: active
Brokkr mode: default
Sensor reachable: yes (10.10.10.1)
Last telemetry: telemetry_hamma_003_2026-05-11.csv (2026-05-11 13:35:28)
Relay config: pin=4, active_high=True
```

## Error Handling

Each step prints `[OK]` or `[FAIL]` as it runs. If a critical step fails, the script stops and reports what state the system is in. There is no automatic rollback — the operator decides how to proceed.

```
--- Turning sensor OFF ---
[OK] Stopped brokkr service
[OK] Stopped sindri service
[OK] Relay off (de-energized) (pin 4)
[OK] Archived telemetry_hamma_003_2026-05-11.csv -> ...csv.bak
[OK] Created drop-in directory
[OK] Wrote mode drop-in (nosensor)
[OK] Reloaded systemd daemon
[OK] Started brokkr service
[OK] Started sindri service
```

## Reboot Behavior

On reboot, GPIO pins reset to input (floating), which de-energizes the relay. The systemd drop-in persists, so brokkr restarts in the correct mode. But for `active_high=false` sensors, a reboot after `--off` will re-power the sensor (de-energized = on) while brokkr stays in nosensor mode. This is a known limitation.

## File Locations

| File | Purpose |
|------|---------|
| `/home/pi/dev/mjolnir-hamma/scripts/sensors.py` | The script |
| `~/.config/brokkr/hamma/unit.toml` | Per-unit relay config (`[relay]` section) |
| `/etc/systemd/system/brokkr-hamma-default.service.d/mode.conf` | Systemd drop-in for nosensor mode |
| `~/brokkr/hamma/telemetry/` | Telemetry CSVs and `.bak` archives |

## Prerequisites

- Passwordless sudo for `pi` user (standard on deployed sensors)
- `relay.py` executable at `/home/pi/dev/mjolnir-hamma/scripts/relay.py`
- `gpiozero` + `RPi.GPIO` installed in ltgenv
- `tomli` installed in ltgenv (used for TOML parsing)
