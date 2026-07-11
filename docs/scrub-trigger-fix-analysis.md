# Auto-scrub trigger failure on mj05 — root cause & fix

Date: 2026-07-11. Branch: `feature/scrub-trigger-fix` off `0.4.x`.

---
## ⚠️ REVISION 2 (post red-team + dev-team) — read this first

**My original root cause (NA-poisoned edge detector) was REFUTED by red team #1.**
The Jul 9 threshold crossing was **clean**, not NA-poisoned:
`20:55:11 = 100.22` (≥100) → `20:56:11 = 99.54` (<100) — a clean consecutive numeric
pair, so `(99.54 < 100) and (100.22 >= 100)` is **True**; the edge *should* have fired.

The dev-team debugger then ruled out every "it silently didn't run" hypothesis:
TypeError (value decodes to a real `float`), pipeline-not-running (CSV written at the
exact crossing timestamps), config override (none), and stale code (running commit has
the correct check). **BUT** it found the wall: **log rotation driven by the FPGA-fault
reboot spam destroyed all of Jul 9's `state_monitor` log output**, so whether the trigger
*fired-and-was-ineffective* vs *never-fired* on Jul 9 is **not provable from surviving
evidence.** The "0 alerts ever" datum is therefore unreliable.

### What is actually established (evidence-based)
1. The fill was **real data volume**: the recovery scrub found **20,446 legitimate
   22 MB triggers** (~450 GB) for Jul 3–10; only **92** bad-GPS headers. Not junk.
2. **Only 10 triggers were missing on MJ** — brokkr's real-time science ingest had
   already written ~20,400 of them to `DATA56`. So the AGS drive was full of data that
   was *already safely on MJ*. The last *effective* AGS drain was the **manual** scrub of
   Jul 6.
3. The auto-scrub is an **unreliable safety net**, independent of the Jul 9 mystery:
   - **one-shot edge trigger, no retry** — fires at most once per crossing; if the drive
     sits below threshold (or the one scrub is ineffective), it never re-fires;
   - **fails silently** — `_spawn_scrub` sends child stdout/stderr to `DEVNULL` and never
     checks exit status, so a scrub that runs and frees nothing is invisible (this is
     *why* Jul 9 is unreconstructable);
   - **sole dependence on AGS-pushed `bytes_remaining`** — blind when the AGS is silent;
   - **no direct free-space monitor** on `/ags/data` or on the `/media/pi/DATA??` drives.

### Consequence for the fix
The exact Jul 9 trigger event is unprovable, but the architectural weaknesses are real
and are the correct thing to fix (per systematic-debugging: when investigation hits an
evidence wall, implement retry + observability). The fix below is **re-scoped** from
"fix the nan bug" to **"make the safety net reliable and observable."** The NA-robustness
is retained (cheap, strictly better) but is no longer claimed as *the* cause.

---


## Incident

mj05's AGS data drive `/ags/data` (462 GB) filled to **100% / 0 bytes free**. A full
`/ags/data` stalls the AGS write thread → backs up the read queue → the AGS "No data
read" watchdog reboots the AGS Pi in a loop (documented mechanism; see hamma-expert
`ags-fpga-reboot-loop.md`, cause #2 "disk-full backpressure"). The automated scrubber,
whose entire job is to drain `/ags/data` by recovering triggers to the MJ `DATA??`
drives and purging, **never fired**. Humans had to manually scrub (Jul 2, Jul 6); it
refilled and hit 0 by Jul 11.

## Evidence (all from the live unit)

1. **`scrub_command` is correctly configured** on mj05 (`config/main.toml`):
   `scrub_command = "python3 .../hamma_scrub.py --recover --purge --since auto"`,
   `low_space = 100`, no local override. Not a config problem.
2. **The low-space alert `"Remaining GB on drive is ..."` fired 0 times, ever** (0
   matches across all brokkr log files); **0 "Spawned scrub" log lines**. The trigger
   path never executed its body.
3. **The telemetry proves why.** `bytes_remaining` (H&S "Disk Remaining", GB, UDP from
   AGS) declined smoothly through the `low_space=100` threshold late on Jul 9, but the
   H&S stream is **frequently NA** (54–80 NA samples/day). At the crossing the raw
   samples **alternate NA / numeric**:

   ```
   21:03  NA
   21:07  99.51   <- first sub-100 reading; its predecessor sample is NA
   21:11  NA
   21:15  99.51
   21:19  NA
   21:23  96.06   ... monotonic decline to 0 by Jul 11
   ```

## Root cause

`StateMonitor.check_sensor_drive` (plugins/state_monitor.py) is a **falling-edge
detector**:

```python
space_now, space_pre = self.now_then(input_data, 'bytes_remaining')
if (space_now < self.low_space) and (space_pre >= self.low_space):
    self._spawn_scrub()
    return f"Remaining GB on drive is {space_now:.1f}"
```

`now_then` maps `'NA' → nan`. Firing requires a **consecutive numeric pair** straddling
the threshold (`space_pre >= 100` AND `space_now < 100`). On mj05, every sub-100 reading
was preceded by an NA sample, so `space_pre` was `nan`, and **`nan >= 100` is `False`**.
No consecutive (>=100, <100) pair ever occurred → the edge was never seen → the scrub was
never spawned → the disk filled to 0.

The `>=` (vs `>`) already on 0.4.x fixed a *different* miss (mj07 exact-boundary
`100.0→99.96`); it does **nothing** for the nan case.

### Three compounding defects
1. **NA-fragility (primary):** a single NA at the crossing permanently defeats the edge
   detector, because `nan >= low_space` and `nan < low_space` are both `False`. NA is the
   norm, not the exception, on this channel.
2. **Edge-only, no re-arm:** even with clean data, it fires on one sample only. Once
   below threshold it never retries, so any single missed crossing is unrecoverable until
   the value climbs back above threshold and falls again.
3. **Sole dependence on AGS-pushed telemetry:** the metric that gates the scrub is pushed
   by the very component (AGS) that goes silent when the condition (disk full →
   backpressure → reboot loop) occurs. Detection blinds itself exactly when it's needed.

## Proposed fix (DRAFT — under review)

Make `check_sensor_drive` **level-triggered, NA-robust, and cooldown-rate-limited**.

```python
def check_sensor_drive(self, input_data):
    space_now, _ = self.now_then(input_data, 'bytes_remaining')

    # NA-robust: nan means the AGS sent no H&S this cycle (silent/rebooting).
    # We cannot assess free space; make no decision, leave prior state intact.
    if space_now != space_now:  # nan
        return None

    if space_now >= self.low_space:
        self._low_space_active = False   # healthy: re-arm the alert
        return None

    # Below threshold: level-triggered. Spawn a scrub, rate-limited so a
    # persistently-low drive does not respawn every 60 s cycle.
    self._maybe_spawn_scrub()

    if not self._low_space_active:       # alert once per descent, not every cycle
        self._low_space_active = True
        return f"Remaining GB on drive is {space_now:.1f}"
    return None

def _maybe_spawn_scrub(self):
    if not self.scrub_command or not self.scrub_command.strip():
        return
    now = time.monotonic()
    if (self._last_scrub_time is not None
            and (now - self._last_scrub_time) < self.scrub_cooldown_s):
        return                            # within cooldown; flock also guards concurrency
    self._last_scrub_time = now
    self._spawn_scrub()
```

New `__init__` state: `scrub_cooldown_s=1800`, `self._last_scrub_time=None`,
`self._low_space_active=False`. `_spawn_scrub` unchanged (flock + Popen).

### Why this fixes mj05
- **NA no longer poisons detection** — nan samples are skipped, and the decision uses
  only `space_now`. Any single valid sub-100 reading (there were hundreds) now fires.
- **Level + cooldown** re-arms: if a scrub fails or the drive stays low, the next valid
  low sample past the cooldown tries again, instead of one-and-done.
- flock still prevents concurrent scrubs; the cooldown prevents 60 s log/proc spam and
  hammering the AGS over SSH.

### Behavior change to flag
The existing test `test_no_scrub_when_already_below` asserts the OLD edge-only behavior
(no scrub when already below). Under the fix, a below-threshold sample **does** spawn
(subject to cooldown). That test must be rewritten to the new intended contract.

## Open questions for the red team
1. Is level+cooldown the right shape, or should we also add a **telemetry-independent**
   safety net (e.g. direct `df` of the drives, or a "no successful scrub in N hours"
   watchdog) to cover total AGS silence (no H&S at all)? On mj05 H&S was intermittent,
   not absent, so level-trigger suffices *here* — but defect #3 remains.
2. Cooldown default 1800 s — too long (slow retry) or too short (AGS SSH hammering)?
   Should it be config-driven (it is, via `scrub_cooldown_s`)?
3. `time.monotonic()` for cooldown vs a sample-count counter — testability and
   correctness across brokkr restarts (monotonic resets on reboot; is that acceptable?).
4. Any risk from firing a `--purge` scrub more eagerly (level vs edge)? Purge only deletes
   AGS files confirmed byte-identical on MJ, so more frequent runs are safe — confirm.

---
## Dev-team rebuttal of red-team findings + FINAL fix scope

| # | Red-team finding | Disposition |
|---|---|---|
| RT2-1 | FATAL: missing `import time` → NameError | **ACCEPTED** — add `import time`. |
| RT2-2 | FATAL: test helper omits new attrs → AttributeError | **ACCEPTED** — `make_monitor` sets all three; add real-`__init__` test. |
| RT2-3 | SERIOUS: verify `__init__` wires all three attrs | **ACCEPTED** — wired in `__init__`; test asserts. |
| RT2-4 | SERIOUS: `x != x` only catches float-nan; None/str → TypeError | **ACCEPTED** — use `not isinstance(x,(int,float)) or x!=x`. |
| RT2-5 | SERIOUS: total-AGS-silence still blinds trigger | **ACCEPTED as residual** — documented; direct-df follow-up (RT3-A). |
| RT2-6 | MINOR: cooldown stamped before launch success | **ACCEPTED** — stamp only on successful `Popen` (`_spawn_scrub` returns bool). |
| RT2-7 | MINOR: monotonic reset on reboot | **ACKNOWLEDGED** — safe direction (biases toward scrubbing); comment added. |
| RT2-8 | MINOR: re-alert churn on re-crossings | **ADDRESSED** — alert on descent-entry and on each (re)spawn only, not per cycle. |
| RT2-9 | MINOR: replacement test contract unspecified | **ACCEPTED** — explicit new tests (below-with-cooldown-elapsed spawns; within-cooldown does not; nan skips). |
| RT1-1..4 | Root-cause attribution underdetermined (nan not proven; evidence unreproducible; TypeError/spawned-failed not excluded) | **CONCEDED** — see REVISION 2. Debugger ruled out TypeError/pipeline/config/code; Jul 9 unprovable (logs rotated). Fix re-scoped to reliability+observability, not "the nan bug." |
| RT3-R1 | Telemetry-dependence (MIGHT-RECUR) | **RESIDUAL** — level-trigger needs ≥1 valid sub-threshold sample; documented; direct-df follow-up. |
| RT3-R2 | `/media/pi/DATA??` unmonitored (WILL-RECUR) | **FOLLOW-UP** — file issue for a local-`df` DATA?? check; out of scope for this trigger fix. |
| RT3-R3 | Firing-but-futile scrub, silent (WILL-RECUR) | **PARTIALLY ADDRESSED** — scrub output now logged (not DEVNULL) so futile runs are visible; success-verification is follow-up. |
| RT3-C  | One-shot alert, no escalation | **ADDRESSED** — re-alert on each cooldown-gated respawn while still low. |

### Final fix (implemented on this branch)
1. `check_sensor_drive`: **level-triggered + numeric-robust + cooldown-gated retry**, alert on
   descent-entry and on each respawn. (`import time`; `isinstance` guard.)
2. `_maybe_spawn_scrub`: cooldown via `time.monotonic()`, stamped **only on successful launch**.
3. `_spawn_scrub`: returns bool; **redirects scrub output to a log** (configurable `scrub_log`,
   defensive open, falls back to DEVNULL) so futile/failed scrubs are no longer invisible.
4. `__init__`: new `scrub_cooldown_s` (default 1800) + `scrub_log`; init `_last_scrub_time=None`,
   `_low_space_active=False`.
5. Tests updated + added; `test_no_scrub_when_already_below` rewritten to the new contract.

### Honestly out of scope here (tracked follow-ups — required for a true "never again")
- **Direct `df` of `/ags/data` over SSH**, independent of AGS H&S (closes RT2-5/RT3-R1 blind spot).
- **Local `df` monitor on `/media/pi/DATA??`** (closes RT3-R2).
- **Scrub success verification** (freed-bytes check) (closes RT3-R3 fully).
- **AGS-side policy for bad-GPS / unrecoverable files.**
