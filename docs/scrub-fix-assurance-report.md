# Assurance report — mj05 auto-scrub reliability fix

**Date:** 2026-07-11 · **Branch:** `feature/scrub-trigger-fix` (off `0.4.x`)
**Change:** `plugins/state_monitor.py` (+ `config/main.toml`, tests) — makes the
auto-scrub trigger level-triggered, numeric-robust, cooldown-retried, and observable.

## 1. What failed (established facts)

mj05's AGS drive `/ags/data` (462 GB) filled to 0 bytes. The data was **real** —
~20,446 legitimate 22 MB triggers for Jul 3–10; only 92 bad-GPS headers. brokkr's
real-time ingest had already copied ~20,400 of them to `DATA56` (the recovery scrub found
only **10** genuinely missing). The AGS then hit the disk-full backpressure reboot loop.
The last *effective* drain of the AGS side was a **manual** scrub on Jul 6; the automated
safety net did not keep the drive clear.

**Honesty note on the trigger:** whether the automated trigger *fired-but-was-ineffective*
or *never-fired* on Jul 9 is **not provable** — log rotation from the reboot-loop spam
destroyed that day's `state_monitor` logs. My first root cause (NA-poisoned edge detector)
was **refuted** by red team #1: the Jul 9 crossing was clean (`100.22 → 99.54`), so the
edge *should* have fired. I am not claiming a mechanism the evidence can't support.

## 2. What the fix changes (and why it's the right target)

Independent of the unprovable Jul 9 detail, the auto-scrub had four real, code-visible
weaknesses. The fix addresses three directly and makes the fourth diagnosable:

| Weakness (before) | After |
|---|---|
| **One-shot edge trigger, no retry** — fires only on a high→low crossing; a single missed/ineffective scrub is never retried while the drive stays low. | **Level-triggered**: fires on *any* below-threshold sample, **cooldown-rate-limited** (`scrub_cooldown_s`, default 1800 s) so it retries but doesn't spam. |
| **NA/nan fragility** — a nan sample poisons the edge; a `None`/string value raises `TypeError` that `run_checks` swallows, silently disabling the check. | **Numeric-robust**: `isinstance(x,(int,float)) and x==x` guard; non-numeric samples are skipped cleanly, never crash. `0` (full disk) still fires. |
| **Silent scrub failure** — output → `/dev/null`, exit status ignored; a scrub that runs and frees nothing is invisible (this is *why* Jul 9 is unreconstructable). | **Observable**: scrub stdout/stderr appended to `scrub_log`. Alerts now escalate — on descent-entry **and** on each cooldown-gated respawn — instead of one message then silence. |
| **Sole dependence on AGS-pushed `bytes_remaining`** — blind when the AGS is fully silent. | **Not fixed here** — documented residual (§5); needs a telemetry-independent check (follow-up). |

## 3. Evidence of effectiveness

- **Tests:** 34/34 state_monitor tests pass (`test_state_monitor.py` +
  `test_state_monitor_scrub.py`). TDD: 6 new-contract tests were written and watched to
  **fail** against the old code, then pass. Full suite: 446 passed; the only 3 failures
  (`test_hamma_noise`) are pre-existing and unrelated (an installed-`hamma` version issue;
  that module does not import `state_monitor`).
- **Against the actual incident:** on Jul 9, `bytes_remaining` sat below 100 for *hours*.
  At the ~1-4 min H&S cadence that is an estimated **tens-to-hundreds** of valid sub-100
  samples (an inference — the state_monitor log for that day was rotated away, see §1). The
  old edge trigger needed one specific high->low pair and then never retried. The level
  trigger fires on the first valid sub-100 sample and **re-fires every 30 min while still
  low** — converting the failure mode from "one shot, then silence" to "retry + escalate +
  on the record."
  **Caveat (honest completion):** this helps *only if* a triggered scrub actually frees
  `/ags/data`. On this incident ~20,400/20,446 triggers were already on MJ and the real
  drain was manual, and once the AGS entered its reboot loop it stops sending H&S — so a
  perfectly reliable trigger here could have fired, run scrubs that freed little, then gone
  blind. See residual risks §5.1 (telemetry blind spot) and §5.3 (futile scrub). The fix
  makes mj05 **more reliably retried and diagnosable**, not provably rescued.
- **Regression safety:** the change is confined to `state_monitor.py` (+tests, +2 config
  lines). No other module touched.

## 4. Peer review performed (adversarial)

Three red-team reviewers attacked the analysis and the fix; a debugger adjudicated the
root cause with live access. Dispositions (full table in
`scrub-trigger-fix-analysis.md`):

- **Caught 2 FATAL bugs in the draft** before implementation: missing `import time`
  (would have thrown `NameError` and eaten scrub *and* alert), and a test helper missing
  the new attrs. Both fixed and covered by tests.
- **Caught the root-cause over-claim** (nan theory) — conceded; report re-scoped to
  reliability+observability rather than a mechanism the evidence can't prove.
- **Hardened details:** numeric guard vs `x!=x`-only; cooldown stamped only on successful
  launch (a failed launch retries next cycle); alert escalation on respawn.

## 5. Can I assure you this is the last time? — Honest answer: **partially. No, not on its own.**

This fix makes the safety net **substantially more reliable and, critically, observable**
— the next occurrence will retry and leave a diagnosable trail instead of failing
silently. That is a real, tested improvement and it targets the mechanism that let a
transient problem become a total fill.

But a truthful "never again" requires more than this change, and the red team was right to
say so. **Residual risks this fix does NOT close:**

1. **Telemetry blind spot (MIGHT-RECUR):** the trigger still reads `bytes_remaining`,
   which the AGS stops sending when it reboot-loops. mj05's H&S was intermittent (so the
   level trigger catches it), but a tighter loop with *no* clean sub-threshold sample
   would still be missed. **Follow-up:** a direct `df /ags/data` probe over SSH,
   independent of AGS telemetry.
2. **`/media/pi/DATA??` unmonitored (WILL-RECUR eventually):** nothing watches the mj-side
   recovery-target drives' free space (`DATA55` is *also* at 100% now). If all DATA drives
   fill, brokkr can't write and recovery has nowhere to land. **Follow-up:** a local
   `shutil.disk_usage` check on `DATA??` (cheap, mirrors `check_pi_space`).
3. **Firing-but-futile scrub:** if targets are full, scrubs run and free nothing. Now at
   least **visible** in `scrub_log`, but not yet auto-detected. **Follow-up:** verify
   freed-bytes after a scrub and escalate if zero.
4. **The underlying driver** — a high sustained trigger rate that outpaces draining — is a
   capacity/tuning question, not a bug this fix addresses.

**Recommendation:** ship this fix (it's correct and net-positive), deploy to mj05 after
AGS is restored, and track items 1–2 as the follow-ups that, together with this, constitute
an honest "this shouldn't happen again." I would not close the incident on this change
alone.

## 6. Deployment notes (for whoever deploys this)

- **Cooldown resets on brokkr restart.** `_last_scrub_time` is in-memory, so a brokkr/Pi
  restart re-arms immediate scrubbing on the next low sample. This is the safe direction
  (biases toward scrubbing), but during a reboot loop expect a scrub launch shortly after
  each restart rather than strict 30-min spacing.
- **`scrub_log` directory must exist and be pi-writable.** Config points it at
  `/home/pi/brokkr/hamma/log/hamma_scrub_auto.log`. If the dir is missing or unwritable,
  `_spawn_scrub` logs a warning and **silently falls back to DEVNULL** — i.e. the
  observability feature no-ops. Confirm the path on each unit at deploy.
- **flock still guards concurrency, independently of the cooldown.** If a prior scrub is
  still running when the 30-min cooldown elapses, the new `flock -n` launch succeeds as a
  process but the flocked child exits immediately (lock held). `_spawn_scrub` returns True
  and stamps the cooldown, so you get one short-lived flock-rejected process per cooldown
  while a long scrub runs — benign, and now visible in `scrub_log`.
- **Numeric guard admits bool** (`isinstance(x,(int,float))`; `bool` subclasses `int`).
  Harmless because `bytes_remaining` is never boolean; noted for completeness.
