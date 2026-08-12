# Power-state mismatch detection — design

**Date:** 2026-08-10
**Tickets:** HAM-182 (this), HAM-189 (operator-transition logging), mjolnir-hamma #86 (`--status` false positive)
**Status:** implemented on `feature/ham182-power-reconcile` (uncommitted; branch not yet pushed)

---

## Problem

There are two independent representations of "is this sensor on":

1. **Physical truth** — the relay, and whether the front end draws power.
2. **Declared intent** — brokkr's mode.

They are coupled at exactly one moment: when `sensors.py --on/--off` runs. Nothing re-checks afterwards.

The dangerous divergence is **powered but declared off**. A cold power loss resets the GPIO pads to `INPUT`; undriven, the relay board pulls the line LOW, which on an `active_high` unit is energised. The front end comes back ON while brokkr is still in `nosensor`, so the AGS records to its own stick but brokkr ingests nothing and none of it reaches the DATA drives or the server.

Pi up, tunnel up, brokkr running, disk fine. **Every check passes and no data arrives.**

**The gap is that nothing compares declared against measured.** That is the whole problem, and detection is the whole fix.

---

## Decision: detect and alert. Do not remediate.

An hourly check in `state_monitor` compares the relay against brokkr's own running mode and alerts when they disagree. It changes nothing.

### Why not fix it automatically

Three independent reasons, in order of weight:

1. **"Was off, now on" is an anomaly that wants a human.** Auto-resuming both collects data from a unit somebody deliberately stopped *and* erases the evidence that it came back. Fixing it papers over the event.
2. **The mismatch has several possible causes and the detector cannot tell them apart.** Cold loss, a half-failed operator transition, a manual `relay.py` call, a stuck relay. A single automated response to a state with several causes is a guess.
3. **Every remediation path examined was worse than the mismatch.** Boot-time remediation was safe but only covers cold loss. Mid-day remediation required stopping and restarting brokkr and sindri unattended, and red-team review found it killed itself deterministically (the spawned child shares brokkr's cgroup, and `KillMode=control-group` kills it at the moment it stops brokkr), announced success on failure, and could delete a custom `mode.conf`.

### What this costs, stated plainly

While mismatched, the front end draws power and brokkr does not ingest. The AGS keeps recording to its own stick, so the data is recoverable (`hamma_scrub --recover` restored 13/13 triggers on mj43, HAM-182 comment 12186) — but the AGS is a **lossy** buffer under sustained high trigger rates, and `enable_drive_checks=false` in `nosensor` mode disables the auto-scrub, so nothing drains the stick while it sits. A long unattended mismatch during storm season is real loss, not just delay.

That cost is accepted: it is bounded, visible, and a human's call.

---

## Implementation

One method, `StateMonitor.check_power_state_divergence`, in `plugins/state_monitor.py`. No new script, no systemd unit, no state directory, no spool.

| Concern | Choice | Why |
|---|---|---|
| Where | Inside `state_monitor` | A mismatch only *matters* while brokkr is running. With brokkr down nothing captures regardless. |
| Mode source | brokkr's own `MODE_CONFIG["mode"]` | There are **four** placements: drop-in `Environment=`, drop-in `ExecStart=`, the unit's own `ExecStart=`, and a `mode` key in the unit's local `mode.toml` (mj05 uses that). Only the running process knows which took effect. Inferring from `mode.conf` presence is wrong. |
| Relay config | `UNIT_CONFIG["relay"]` | brokkr already parses `unit.toml`; no second parser. |
| Interval gate | Elapsed wall-clock, not cycle count | `config/mode.toml` overrides `monitor_interval_s` to 1 s under `realtime`; counting cycles would mean "every minute" there. (`test` also shortens it but disables `state_monitor` outright.) |
| First run | Immediately on the first cycle | A cold-loss mismatch is most likely to be sitting there right after brokkr starts. |
| Repetition | Level-triggered, re-fires every hour | Deliberately loud. Also removes any need for store-and-forward: an alert raised with no link simply lands on the next cycle after the link returns. |
| Polarity | `(level == 0) == active_high` | The algebraic inverse of `sensors.compute_relay_flag`. Handles mj42's inverted config. |
| `nosensor` test | `"nosensor" in mode` | Matches both `nosensor` and `nosensor_nochargecontroller`. |

Both messages carry an `ACTION:` line naming the `mjol_array.py` command.

---

## Deployment

The only artifact is `plugins/state_monitor.py`. No unit file, so no `verify_deployment.sh` check, no bring-up checklist row, and no partial-deployment state where half the feature ships.

**But it is two steps, not one.** `git pull` brings the file; a running brokkr has the old module already imported, and units routinely stay up for weeks (mj08 14 d, mj54 26 d as measured 2026-08-11). The check does not activate until `brokkr-hamma-default.service` is restarted.

---

## Known limits

- **Repetition can train people to ignore it.** mj42 is the case to watch: `active_high=false` plus `gpio=4=op,dh` in `config.txt` means it boots powered by construction, so if it is ever declared off it will alert hourly forever with nothing actually wrong. Fix mj42's config or exempt it rather than let it desensitise the channel.
- **If brokkr is down, nothing checks.** Accepted — brokkr being down is a louder, separately-alarmed problem.
- **Notifications are fire-and-forget** (mjolnir-hamma #85). Level-triggered repetition makes this tolerable here: a dropped alert re-fires next hour.
- **A mismatch arising and being resolved inside one hour is never reported.** Accepted.
- **This does not cover the HAM-182 incident, and that is the most important limit.** It detects relay-vs-mode *disagreement*. It cannot see the two drift into agreement on the wrong state, which is what happened: mj43 was re-powered on 2026-07-02 by a `sensors.py --on` sweep that moved both together. Measured 2026-08-11, mj43 is powered (1.454 A, GPIO 4 low) in `default` mode — self-consistent, so this check is **silent**, while sensor-log #69 and #103 both still record it as off. The stale representation is the field log, a third thing not modelled here. That is HAM-189. I had already recorded this limitation on HAM-182 on 2026-08-06 ("a declared-vs-measured detector ... would have returned MATCH, exit 0 ... It does not address this defect") and then wrote a docstring claiming the opposite; both are now corrected.
- **4 of 10 reachable units are silent by construction** — mj05, mj06, mj50 and mj54 have no `[relay]` section. mj06 is the interesting one: it reads `level=0 func=INPUT pull=UP`, so something external overcomes the pull-up and "no `[relay]` section" does not prove "no relay board".
- **Deployment needs a brokkr restart**, not just a `git pull` — a running process has the old module imported, and units stay up for weeks.
- **Alert volume breaks this file's convention.** Every other check is edge-triggered or latches once; this one re-fires hourly, indefinitely, by explicit design decision. A shared-cause outage across several units multiplies it.

---

## History

An earlier revision of this work built a boot-time systemd oneshot that remediated automatically, then grew an hourly trigger that restarted brokkr. Four red-team reviews found: the spawned child dies in brokkr's cgroup at the moment it stops brokkr; the operator notification announced success on failure because `kind` was written before the action; `sensors.sensor_on()` silently discards `stop_sindri()`'s return code; `relay.py` transits the pin through `INPUT` on every invocation so the "no-op toggle" claim was false; and the hourly trigger's residual coverage set was empty — every cause it claimed to catch was boot-covered, HAM-189-covered, or structurally undetectable.

All of it is deleted. What remains is the detection that was the actual requirement.
