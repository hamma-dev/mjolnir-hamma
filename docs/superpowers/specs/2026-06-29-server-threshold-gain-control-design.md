# Server-side threshold & gain control — design

**Date:** 2026-06-29
**Branch base:** `0.4.x` (feature branch `feature/server-threshold-gain-control`)
**Status:** Design approved, pending written-spec review

## Goal

Let an operator change a HAMMA2 sensor's **trigger threshold** and **FCM gain** from
the server (VPS `hamma.dev`), for one sensor, several, or a whole array in a single
command — mirroring the existing "trigger a sensor" / "power on/off" tooling. A
`--persist` flag makes a change survive a sensor (DAS/FPGA) reset; without it the
change is live-only and is lost on the next DAS reset/power-cycle.

A web UI is explicitly **out of scope** for this work (desired follow-on, phase 2).

## Background: how the hardware exposes these

The threshold and gain live in **volatile registers on the HAMMA2 sensor's DAS/FPGA**
(`10.10.10.1`), not in any brokkr/sindri config. The only live command channel is the
AGS command socket at `10.10.10.1:8082`, wrapped by `scripts/ags.py`
(`send_ags_command()`). Two firmware verbs matter, confirmed live on mj02 via
`ags.py help`:

- `das_set_threshold <1-8> <volts 0-5>` — sets a DAC threshold (per channel).
- `das_send_command <hex_reg> <hex_value>` — generic register write.

The AGS register map (from the Confluence "ags packet" attachment, authoritative):

| Function | Register | Value | How set |
|---|---|---|---|
| Threshold 1 (DAC threshold) | `0x02` | 12-bit | `das_set_threshold 1 <volts>` |
| Threshold 2 (DAC threshold) | `0x04` | 12-bit | `das_set_threshold 2 <volts>` |
| FCM **FAST-E gain** | `0x08` | low 2 bits | `das_send_command 8 <0-3>` |
| FCM **SLOW-E gain** | `0x10` | low 2 bits | `das_send_command 10 <0-3>` |
| Channels 5–8 | `0x20`–`0x100` | — | not used |

The firmware labels the two threshold registers only "Threshold 1/2" (it does **not**
say which is fast vs slow E-field); only the *gain* registers are explicitly labeled
FAST-E (`0x08`) / SLOW-E (`0x10`). The threshold CLI therefore takes the **channel
number** (1 or 2) directly, so the fast/slow labeling is irrelevant to its behavior;
confirm the physical fast/slow correspondence during E2E if it matters for operator
docs.

Notes grounding the design:

- **Gain is a 2-bit digital level (0–3), not a voltage** — 4 discrete FCM gain
  settings. It is *not* expressible in mV.
- The firmware reads the `das_send_command` register as **bare hex** (`8`, `10` mean
  `0x08`, `0x10`) — matching what the live startup file already uses.
- **Both threshold and gain are reported back in the science-packet header**: the
  decoded header fields `threashold_1..4` carry threshold 1, threshold 2, FAST-E gain,
  SLOW-E gain respectively. There is **no AGS "get" command**, so header readback is
  the only confirmation channel.

### Threshold unit conversion (operator works in mV)

`das_set_threshold` takes a DAC-side "volts 0–5" value. The scientifically meaningful
number is the **input-referred threshold = DAC_volts ÷ 6.024** (the fixed analog gain
factor, per the `hamma` package: `version20.py` and `header/utilities.py`). The
operator specifies the **input-referred threshold in mV**, and the script converts:

```
das_volts = (mV / 1000) * 6.024
```

- Valid `das_volts` ∈ [0, 5] → valid input-referred **mV ∈ [0, ~829.8]**.
- Readback: header raw `threashold_1/2` → input-referred volts = raw·(5/4096)/6.024 →
  display in mV.

### Persistence: the sensor startup script

The DAS restores its registers at boot from a single line-oriented script on the
**AGS host**, `/ags/scripts/startup`, reached via `ssh hamma` from the brokkr Pi.
Current mj02 contents (reference):

```
ds_enable
hs_set_host 10.10.10.2
hs_set_port 8084
hs_enable
das_enable
das_set_threshold 1 0.5
das_set_threshold 2 0
das_send_command 8 1
das_send_command 10 1
das_set_interval 14400
das_set_mask 3
ds_set_period 1
das_reset
```

The file uses the **same command syntax** as the live commands, so persisting a value
means rewriting the one matching line (or inserting it before `das_reset` if absent).
Today this is hand-edited.

## Architecture

Three layers, each reusing an existing pattern:

```
operator @ VPS
  └─ server/mjol_array.py   (NEW actions: --set-threshold, --set-gain, --persist)
       │  reuse: _pi_ssh_cmd, status() liveness gate, -a/-p fan-out, [OK]/[FAIL] relay
       │  (near-clone of existing trigger()/trigger_array())
       └─ ssh pi@localhost -p 1000N  scripts/ags.py set-threshold|set-gain ... [--persist]
            └─ scripts/ags.py   (NEW named functions + subcommands)
                 ├─ live set: send_ags_command() → 10.10.10.1:8082
                 └─ --persist: ssh hamma → rewrite /ags/scripts/startup line
```

### 1. `scripts/ags.py` (runs on the brokkr Pi)

New, clearly-named functions on top of the existing `send_ags_command()`:

- `set_threshold(channel, millivolts, *, persist=False, host, port)`
  - `channel ∈ {1, 2}` (the firmware's threshold channel number). Reject 3–8.
  - Convert mV → das_volts; validate das_volts ∈ [0, 5] (i.e. mV ∈ [0, 829.8]).
  - Send `das_set_threshold <channel> <das_volts>`; return firmware reply.
  - If `persist`: rewrite the matching `das_set_threshold <channel> ...` line in
    `/ags/scripts/startup`.
- `set_gain(channel, level, *, persist=False, host, port)`
  - `channel ∈ {"fast-e", "slow-e"}` → register `8` / `10`.
  - `level ∈ {0, 1, 2, 3}`. Reject others.
  - Send `das_send_command <reg> <level>`; return firmware reply.
  - If `persist`: rewrite the matching `das_send_command <reg> ...` line.
- `persist_startup(match_prefix, new_line)` — read `/ags/scripts/startup` via
  `ssh hamma`, replace the unique line beginning with `match_prefix` (e.g.
  `das_set_threshold 1` or `das_send_command 8`), preserving every other line; if no
  match, insert `new_line` immediately before the trailing `das_reset`. Write back
  atomically (temp file + move) so a failure never leaves a truncated startup script.
  Idempotent.
- `get_state()` — parse `/ags/scripts/startup` and report the *persisted* threshold
  (in mV) and gain levels. (Live current values are already visible via
  `mjol_array.py --status`, which reads the latest header threshold.)

New CLI subcommands (argparse subparsers; the existing generic
`ags.py <command>` passthrough is preserved):

```
ags.py set-threshold <channel 1|2> <millivolts> [--persist]
ags.py set-gain <fast-e|slow-e> <level 0-3> [--persist]
ags.py get-state
```

Constants added: `GAIN_FACTOR = 6.024`, register map for gain channels, startup-file
path, and the `hamma` ssh alias for the AGS host.

### 2. `server/mjol_array.py` (runs on the VPS)

Add static methods mirroring the existing `trigger()` (same tunnel, liveness gate,
timeout, reply-surfacing), plus array wrappers mirroring `trigger_array()`:

- `set_threshold(port, channel, millivolts, persist=False, quiet=False)`
- `set_gain(port, channel, level, persist=False, quiet=False)`
- `set_threshold_array(ports, channel, millivolts, persist=False)`
- `set_gain_array(ports, channel, level, persist=False)`

Each builds `ags.py set-threshold|set-gain ... [--persist]` and runs it over
`_pi_ssh_cmd(port)` with a 30s timeout (matching `trigger()`), relaying the Pi's
output and flagging nonzero exit / tunnel-down `[SKIP]`.

New CLI options, combined with the existing `-a {hamma,pamma,aumma}` / repeated `-p N`:

```
mjol_array.py -a hamma --set-threshold <channel> <mV> [--persist]
mjol_array.py -p 2 -p 3 --set-gain <fast-e|slow-e> <level> [--persist]
```

`--set-threshold` takes two args (channel, mV); `--set-gain` takes two (channel,
level); `--persist` is a shared boolean. Wire into the `main()` if/elif dispatch
alongside `--status` / `--trigger` / `--up` / `--down`.

## Validation & safety

- Reject out-of-range up front (channel, mV, gain level) before touching the sensor —
  bad threshold can blind the sensor (too high → misses lightning) or fill disk (too
  low → noise triggering).
- `--persist` writes are atomic and line-exact; never rewrite unrelated lines, never
  reorder relative to `das_reset`.
- Tunnel-down → `[SKIP]`, consistent with `updown`/`trigger`.
- The change does **not** require a brokkr restart (brokkr only reads the header).

## Testing

**Unit (pytest, mock socket + ssh subprocess):**
- mV ↔ das_volts conversion (round-trip, boundaries 0 and 829.8 mV).
- Validation rejects channel ∉ {1,2}, mV out of [0, 829.8], gain channel name typos,
  level ∉ {0..3}.
- Correct firmware command strings built (`das_set_threshold 1 <v>`,
  `das_send_command 8 <l>`, etc.).
- `persist_startup` line replacement: replaces only the matching line, preserves all
  others byte-for-byte, inserts before `das_reset` when absent, is idempotent.
- `mjol_array` action methods build the right `ags.py` invocation and honor
  `--persist`; array wrappers fan out over the right ports; tunnel-down → `[SKIP]`.

**E2E on mj02 (requires explicit go-ahead at run time; restore originals after):**
1. Record mj02 originals: `das_set_threshold 1 0.5`, `das_set_threshold 2 0`,
   gains `8→1`, `10→1`.
2. `ags.py set-threshold 1 <mV>` (live) → read back via `mjol_array.py --status`
   (threshold column) / header `threashold_1` → confirm mV matches within rounding.
3. `ags.py set-gain fast-e <level>` (live) → read back header `threashold_3` → confirm.
4. `--persist` variant → inspect `/ags/scripts/startup`; confirm only the target line
   changed.
5. Fan-out smoke: `mjol_array.py -p 2 --set-threshold ...` end-to-end over the tunnel.
6. **Restore** mj02 to recorded originals (live + persisted) and verify.

Per project rule: file a `hamma-dev/sensor-log` issue for the mj02 config touch.

## Deployment notes (for the plan, not this spec)

- `scripts/ags.py` is consumed on the **sensors** (they run mjolnir-hamma 0.4.x; update
  by `git pull`).
- `server/mjol_array.py` is consumed on the **VPS** (`~/dev/mjolnir-hamma`). Confirm the
  VPS checkout's branch contains this change (merge-forward / branch update) so both
  deployment lines pick it up.

## Out of scope / YAGNI

- Web UI (phase 2).
- Setting interval/mask/period or other startup-script lines.
- Changing channels 5–8 (firmware "not used").
- An automated set→trigger→readback verify loop inside the script (E2E covers
  verification; `--status` already surfaces live threshold).
