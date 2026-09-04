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
5. Set the mode drop-in, adding `nosensor` and **keeping `nochargecontroller`**
6. Reload systemd
7. Start brokkr (now runs in a `nosensor*` mode)
8. Start sindri

### `--on` Sequence

1. Stop brokkr service
2. Stop sindri service
3. Archive today's telemetry CSV
4. Set the mode drop-in, clearing `nosensor` and **keeping `nochargecontroller`**
   (removes the drop-in entirely only when the result is plain `default`)
5. Reload systemd
6. Toggle relay to power on sensor
7. Start brokkr
8. Start sindri

### Mode is two axes, and on/off only moves one

`nochargecontroller` describes the unit's **hardware** — whether a SunSaver MPPT is
wired up. It has nothing to do with whether the sensor is powered, so it is **sticky**
across `--on` and `--off`:

| Current mode | `--off` → | `--on` → |
|---|---|---|
| `default` | `nosensor` | *(unchanged)* |
| `nochargecontroller` | `nosensor_nochargecontroller` | *(unchanged)* |
| `nosensor` | *(unchanged)* | `default` |
| `nosensor_nochargecontroller` | *(unchanged)* | `nochargecontroller` |

`mode.conf` is the **shared filename for every mode override** and appears in two
equally valid forms in the field — `Environment=BROKKR_MODE=<mode>` and an
`ExecStart=` override carrying `--mode <mode>`. Both are read, and the existing form
is preserved when writing back (the unit's own interpreter path is kept). A drop-in
that cannot be parsed is **refused, not overwritten**.

> Before HAM-184 this file was treated as a boolean — present meant `nosensor`,
> absent meant `default`. On mj06, mj50 and mj54 that made `--status` report
> `nosensor` regardless of the real mode, and `--on` silently dropped them to
> `default`, re-enabling charge-controller polling against hardware that isn't there.

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

## Reboot and Power-Loss Behavior

> **Corrected 2026-08-07 by controlled experiment on mj03 (HAM-182).** The previous
> version of this section said a *reboot* releases the pin and that `active_high=false`
> sensors are the ones that re-power. **Both statements were wrong.**

**A warm reboot is safe.** `reboot` does not power-cycle the SoC, so the GPIO pads keep
their configuration and the relay holds its state. Verified: mj41 went through at least
8 warm reboots while declared OFF and `adc_il_f` stayed at the OFF level (0.37–0.63 A)
every time.

**A COLD power loss is what re-powers the sensor.** If the charge controller itself
loses power — an outage, an LVD trip, a disconnected load terminal — the SoC is
power-cycled and GPIO 4 reverts to `func=INPUT`. Undriven, the relay board pulls the
line **LOW**, overriding the SoC's internal pull-up. On `active_high=true` units LOW is
energised, so **the front end comes back ON while the brokkr drop-in still says
`nosensor`**, and nothing detects the divergence.

Measured on mj03, 2026-08-07, after a ~30 s interruption at the CC load terminal:

```
BEFORE:  GPIO 4: level=1 fsel=1 func=OUTPUT pull=NONE   adc_il_f = 0.29 A   (off)
AFTER:   GPIO 4: level=0 fsel=0 func=INPUT  pull=UP     adc_il_f = 1.39 A   (ON)
         BROKKR_MODE=nosensor unchanged; mode.conf still present
```

Charge-controller `hourmeter` regressed 47006 → 46991, confirming a genuine cold loss
(the counter cannot decrease while powered).

### Which units are exposed

| Units | Why |
|---|---|
| mj02, mj03, mj04, mj08, mj41, mj43 | `active_high=true`; undriven pin reads LOW = energised = **ON** |
| **mj42** | `active_high=false`, but it is the only unit with `gpio=4=op,dh` in `/boot/firmware/config.txt` (line 67, added 2025-09-19). That forces the pin output-**high** at boot, and for `active_high=false` high = ON. **mj42 boots with the front end ON unconditionally.** |
| mj05, mj50, mj54 | No `[relay]` section, and GPIO 4 reads HIGH on the SoC pull-up. |
| **mj06** | No `[relay]` section either, but GPIO 4 reads **level=0 `func=INPUT pull=UP`** — something external overcomes the internal pull-up, so "no `[relay]` section" does **not** prove "no relay board". If a `[relay]` section is ever added, the check would immediately compute powered=True against its `nosensor_nochargecontroller` mode and alert. mj06 runs `NullInput`, so `adc_il_f` is NA and there is no way to cross-check. |

Confirmed occurrences: mj08 ran ON for 4 d 2 h while declared OFF after a cold loss on
2026-06-25 (sensor-log #77); mj03 probably did the same after an 11.6-day outage ending
2026-06-22; and mj03 again under the controlled test above.

**Practical consequence.** After any power interruption, do not trust the drop-in to tell
you whether a sensor is powered. Check `adc_il_f` (telemetry **field 7** — field 6 is
`adc_ic_f` solar charge current and is blind to the relay). Roughly: ~1.4 A front end on,
~0.4 A off, though the absolute level varies per unit and over time, so compare against
that unit's own recent history rather than a fixed threshold.


## Power-State Mismatch Alert

`state_monitor` compares the relay against brokkr's own running mode once an hour and
**alerts if they disagree**. It does not change anything.

This exists because of the cold-loss behaviour above: the front end comes back ON while
brokkr is still in `nosensor`, so the AGS records to its own stick while brokkr ingests
nothing — and every other check passes. Pi up, tunnel up, brokkr running, disk fine, no
data arriving.

**It deliberately does not fix anything.** "Was off, now on" is an anomaly that wants a
human, and every auto-remediation approach tried had failure modes worse than the
mismatch itself.

**What it does NOT cover.** It compares the relay against brokkr's mode and nothing else.
It cannot see the two drifting into agreement on the *wrong* state — which is exactly the
HAM-182 incident. mj43 was re-powered on 2026-07-02 by a deliberate `sensors.py --on`
sweep that moved the relay **and** the mode together, so it reads self-consistent
(powered + `default`) and this check is **silent on mj43 today**, while sensor-log #69 and
#103 still record it as off. The stale representation there is the **field log**, a third
thing not modelled here — that gap is HAM-189. Do not describe this check as protecting
units that are deliberately held off.

| What it sees | Alert |
|---|---|
| Relay and mode agree | Nothing |
| Front end POWERED, mode is `nosensor*` | Nothing is being ingested. Decide whether it should be capturing |
| Front end NOT powered, mode is not `nosensor*` | brokkr retries a dead front end ~58/s, filling the SD card; the AGS auto-scrub is also off in this state |
| No `[relay]` section (mj05, mj06, mj50, mj54) | Nothing — 4 of 10 reachable units are silent by construction |

Both messages carry an `ACTION:` line naming the `mjol_array.py` command to run.

**The alert repeats every hour while the mismatch persists.** That is deliberate: it makes
the alert loud, and it means an alert raised while the link is down simply lands on the
next cycle once the link returns — no queuing or replay needed. The flip side is that a
unit left mismatched will keep alerting, so resolve it or power the front end down.

Two details worth knowing:

- The mode comes from brokkr's own resolved `MODE_CONFIG`, not from the presence of
  `mode.conf`. There are **four** placements — drop-in `Environment=`, drop-in
  `ExecStart=`, the unit's own `ExecStart=`, and a `mode` key in the unit's local
  `~/.config/brokkr/hamma/mode.toml` (mj05 uses that one) — and only the running
  process knows which took effect.
- The interval is wall-clock, not a cycle count, because `config/mode.toml` overrides
  `monitor_interval_s` to 1 s under `realtime` and 5 s under `test`.

**Deploying it takes two steps, not one.** `git pull` brings the file, but a running
brokkr has the old module already imported — units routinely stay up for weeks — so
the check does not activate until `brokkr-hamma-default.service` is restarted. There
is no new unit file, config or state directory beyond that.

## File Locations

| File | Purpose |
|------|---------|
| `/home/pi/dev/mjolnir-hamma/scripts/sensors.py` | The script |
| `~/.config/brokkr/hamma/unit.toml` | Per-unit relay config (`[relay]` section) |
| `/etc/systemd/system/brokkr-hamma-default.service.d/mode.conf` | Systemd drop-in for nosensor mode. **Shared filename for every mode override** — may hold an `ExecStart=` override for `nosensor_nochargecontroller` etc., not just `Environment=BROKKR_MODE=nosensor` |
| `~/brokkr/hamma/telemetry/` | Telemetry CSVs and `.bak` archives |

## Prerequisites

- Passwordless sudo for `pi` user (standard on deployed sensors)
- `relay.py` executable at `/home/pi/dev/mjolnir-hamma/scripts/relay.py`
- `gpiozero` + `RPi.GPIO` installed in ltgenv
- `tomli` installed in ltgenv (used for TOML parsing)
