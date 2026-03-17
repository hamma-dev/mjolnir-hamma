# Setup Datasync User Script

## Overview

A bash script (`scripts/setup_datasync.sh`) that creates and configures the `datasync` user on remote HAMMA sensors so that `hamma_download.py` can pull data via rsync.

**Location:** `mjolnir-hamma/scripts/setup_datasync.sh`

**Runs from:** Any machine with SSH access to `pi@hamma.dev` (typically the local Mac).

**Runs on sensors as:** `pi` user (passwordless sudo).

## Problem

Setting up the `datasync` user on each sensor requires 6 manual commands over SSH. With multiple sensors to configure, this is tedious and error-prone. The process should be scripted so it's repeatable and idempotent.

## Design

### Usage

```bash
./setup_datasync.sh --key /path/to/id_rsa.pub 5 7 8
```

**Arguments:**

| Flag | Required | Description |
|------|----------|-------------|
| `--key PATH` | Yes | Path to the public key file to install in `datasync`'s `authorized_keys` |
| `--dry-run` | No | Print commands without executing |
| Positional | Yes | One or more sensor numbers |

### SSH Connection

Each setup command is a separate SSH call:

```bash
ssh -J pi@hamma.dev -p 100XX pi@localhost '<command>'
```

- Uses `pi` user for both the jump host and the sensor (since `datasync` doesn't exist yet on unconfigured sensors).
- Port derived as `10000 + sensor_number`.
- Separate SSH call per command for clear per-step error reporting.
- If any step fails for a sensor, the script logs the error and moves on to the next sensor.

**Note on jump host users:** Setup uses `pi@hamma.dev` because it runs from the local Mac where the user has SSH access as `pi`. Verification uses `monitor@hamma.dev` because that is the user identity `hamma_download.py` uses from the matrix server.

### Setup Steps Per Sensor

For each sensor number, the script runs these commands remotely as `pi` with `sudo`:

1. **Check if `datasync` exists** — `id datasync`. If it exists, skip user creation.
2. **Create user** — `sudo useradd -m -s /bin/bash datasync`
3. **Add to `pi` group** — `sudo usermod -a -G pi datasync`
4. **Create `.ssh/` directory** — `sudo -H mkdir -p /home/datasync/.ssh && sudo -H chmod 700 /home/datasync/.ssh`
5. **Write `authorized_keys`** — pipe the public key content via `echo '...' | sudo tee /home/datasync/.ssh/authorized_keys > /dev/null && sudo chmod 600 /home/datasync/.ssh/authorized_keys`
6. **Fix ownership** — `sudo -H chown -R datasync:datasync /home/datasync/.ssh`
7. **Set media permissions** — `sudo chmod o+rx /media/pi/` (needed because `/media/pi/` is created by the automounter with restrictive permissions; `pi` group membership alone is not sufficient to traverse it)

### Idempotency

The script is safe to re-run:

- User creation is skipped if `datasync` already exists (`id datasync` check).
- `authorized_keys` is overwritten (not appended), so re-running does not duplicate keys.
- `usermod`, `chmod`, `chown` are idempotent by nature.

### Dependencies

Sources `unified_install/lib/common.sh` via `source "$(dirname "${BASH_SOURCE[0]}")/../unified_install/lib/common.sh"` for:

- `log_info`, `log_error`, `log_success`, `log_warn` — structured logging
- `validate_sensor_num` — validates sensor numbers are positive integers
- `format_sensor_num` — zero-pads sensor numbers for display (e.g., 5 -> "05")

### Error Handling

- Per-step: if a command fails, log the error and skip to the next sensor.
- No `set -e` — errors are handled per-command so one sensor's failure doesn't abort the rest.
- The script tracks which sensors succeeded and which failed.

### Output

**During execution:** logs each step per sensor with structured output from `common.sh`.

**At completion:** prints a summary of successes/failures, then prints verification commands for all successfully configured sensors:

```
Verification commands (run from matrix):
  ssh -J monitor@hamma.dev -p 10005 datasync@localhost 'ls /media/pi/'
  ssh -J monitor@hamma.dev -p 10007 datasync@localhost 'ls /media/pi/'
```

The user runs these manually from matrix to confirm the full chain works.

### File Structure

Single file: `scripts/setup_datasync.sh` in the mjolnir-hamma repo alongside `hamma_download.py`.

No external dependencies beyond bash, ssh, and the existing `common.sh` library.

## Constraints

- Must run from a machine with SSH access to `pi@hamma.dev`
- Requires `pi` user on sensors to have passwordless sudo
- Public key file must exist and be readable locally; validated to look like a public key (starts with `ssh-rsa`, `ssh-ed25519`, or `ecdsa-sha2`)
- Does not verify the `datasync` SSH chain (requires running from matrix)
