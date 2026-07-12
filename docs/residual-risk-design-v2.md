# Residual-risk closure — design v2 (post Round-1; for Round-2 adversarial review)

**Scope:** `plugins/state_monitor.py` + `config/main.toml` in **mjolnir-hamma only**. No
brokkr/sindri. New `check_*` methods slot into `run_checks` under the existing
`enable_drive_checks` guard; new `__init__` kwargs + in-memory state, mirroring PR #78.

## What changed from v1 (Round-1 verdict was convergent)
Both Round-1 reviewers independently said: **ship Layers 1+2, cut Layers 3+4.**
- **CUT — Layer 3 (SSH `df` probe):** ships dark (`ags_probe_enabled=false`), needs a
  per-unit deploy-check (pi→AGS keys, a recurring fleet gap), duplicates Layer 2's human
  alert, and adds SSH-in-the-monitor-loop as a new failure class. Deferred.
- **CUT — Layer 4 (futile-scrub auto-detection):** false-positives "futile" on healthy
  nothing-to-purge scrubs (the still-below-`low_space` guard is nearly inert), is **blind
  in the exact incident scenario** (needs the telemetry that's missing), depends on Layer 3,
  and rests on an unresolved policy question. `scrub_log` visibility (already in PR #78) is
  the proportionate answer. Deferred.
These are recorded as tracked follow-ups (below), not lost.

## v2 ships exactly two checks

### Layer 1 — `check_recovery_drives` (closes risk 2: `/media/pi/DATA??` unmonitored)
Local, telemetry-independent; the one risk rated **WILL-RECUR** (DATA55 is already 100%).
- `glob.glob("/media/pi/DATA??")` (`import glob`; `shutil` already imported). Per-path
  `shutil.disk_usage(path)` in try/except — a drive unmounting mid-check raises
  `OSError`/`FileNotFoundError` → skip that path, don't abort the check.
- Alert when the **roomiest** resolved drive has free `< recovery_low_gb`. That is the real
  "nowhere left to recover to / brokkr can't write" condition. A single drive at 100% is
  **normal** (rotation; DATA55) and must not alert. Message: `"Recovery drives low: best
  DATA?? has N GB free (M/K drives below X GB)"`.
- **Alert-only — never spawns a scrub.** A full DATA drive is not fixed by scrubbing the
  AGS; the remedy is human (rotate/add a drive).
- Empty glob → `None` (leave the "no drives" alert to the existing `check_drive`).
- Debounce: `_recovery_low_active` descent-entry latch (mirrors `_low_space_active`); alert
  once on entry, re-arm when the roomiest drive climbs back above threshold.
- **Documented limitation (Round-1 #7):** this catches the *all-drives-full* case, not a
  "brokkr's current write-target drive fills while another sits empty" case. `select_target_
  drive` in `hamma_scrub.py` picks the most-recent-hour dir with ≥100 MB, not the roomiest.
  Watching the roomiest is the correct signal for "recovery has nowhere to land," which is
  the catastrophic risk; per-drive rotation is normal. Accepted for v2; a per-write-target
  check is a possible later refinement.

### Layer 2 — H&S staleness watchdog (closes the actionable half of risk 1)
Zero external dependency; fires precisely when the value trigger is blind (AGS dark).
- Track `_last_numeric_hs_time` = monotonic time of the last **numeric** `bytes_remaining`.
  **Lazy-init:** on the first `check_sensor_drive`/staleness call, if `None`, set it to
  `time.monotonic()` (same lazy-init idiom as `_previous_data` in `execute`). So a
  never-yet-seen-numeric state measures from process start rather than staying silent.
- Each cycle: if the current `bytes_remaining` is numeric → update `_last_numeric_hs_time`
  and clear the latch. If NOT numeric (NA/nan) and `now - _last_numeric_hs_time >
  hs_stale_s` → alert `"H&S from AGS stale for N min — AGS may be silent/reboot-looping"`,
  debounced via `_hs_stale_active`.
- Lives as a small sibling method `check_hs_staleness` (kept separate from the scrub logic
  so its clock never entangles with the scrub cooldown), added to `run_checks`.
- **Restart note (Round-1 #6, assessed):** `_last_numeric_hs_time` resets on brokkr
  restart. In THIS architecture that's benign — brokkr runs on the mj Pi, which stays up
  through an AGS reboot loop (verified up 5+ days across the mj05 incident); it does **not**
  flap with the AGS. So the "restart defeats the clock" concern (which assumed brokkr
  co-flapping) does not apply. Lazy-init-to-now is correct and simple; no state file needed.

## Config keys (only 2; in `[steps.state_monitor]` + matching `__init__` kwargs)
```
recovery_low_gb = 25    # GB free on the roomiest /media/pi/DATA?? below which to alert
hs_stale_s      = 900    # s without a numeric bytes_remaining -> stale-H&S alert (~2-15x cadence)
```
`recovery_low_gb` default raised from v1's 5 (Round-1: 5 GB ≈ minutes of runway at 22 MB/
trigger; 25 GB gives real early-warning lead). `hs_stale_s=900` = ~4–15 missed samples at
the 1–4 min H&S cadence.

New in-memory state (3 flags): `_recovery_low_active=False`, `_hs_stale_active=False`,
`_last_numeric_hs_time=None`. All reset on restart (safe direction; see restart note).

## Alerting
Distinct message strings per condition (low-space / no-landing-zone / stale-H&S), each
descent-entry latched (alert once, quiet while persisting, re-arm on recovery). No CRITICAL
throttle, no severity ladder — with only these conditions, distinct strings are triage-
adequate (Round-1 #6/#7), and latched conditions don't spam.

## Deferred (tracked follow-ups — file as issues, not built in v2)
1. **Direct `df /ags/data` over SSH** (telemetry-independent *automated* fill detection) —
   revisit only if a truly-silent-AGS fill is shown to recur; consider a cron/external probe
   rather than SSH inside the monitor pipeline step.
2. **Futile-scrub auto-detection** — resolve the policy question ("nothing purgeable" vs
   "purged but still full") and pull the scrub's own purged/retained counts (a `scripts/
   hamma_scrub.py` summary line) before building it; `scrub_log` visibility suffices for now.
3. **Per-write-target DATA drive check** (Layer-1 limitation above).

## Test plan (new file `tests/python/test_state_monitor_drives.py`, PR #78 harness)
`load_state_monitor_module()` mocks; `StateMonitor.__new__`; patch `glob.glob` /
`shutil.disk_usage` / `time.monotonic`.
- **Layer 1:** all-drives-low → alert (+ count in msg); one-drive-has-room → None
  (per-drive-full is normal); empty glob → None (no error, no double "no drives");
  per-path `OSError` skipped, healthy path still evaluated; descent-entry debounce (two low
  cycles → one alert; recover-above then below → re-alert); alert-only (never calls Popen).
- **Layer 2:** NA past `hs_stale_s` → stale alert; numeric sample resets the clock (no
  alert); lazy-init measures from first call (AGS-dark-from-start → alert after `hs_stale_s`);
  fires with `scrub_command=""` (proves no shared path with the scrub); debounce (one alert
  while persisting).
- **Init wiring:** real `__init__` sets the 3 new attrs + 2 config defaults.

## Assumptions to verify before/at implementation
1. `bytes_remaining` reaches `check_sensor_drive`/staleness as the same value used today
   (numeric float or `'NA'`) — confirmed in PR #78 (`now_then`; float via `_convert_custom`).
2. `/media/pi/DATA??` is the correct recovery-target glob (matches `drive_glob="DATA??"`,
   `main.toml`). udisks suffixed-mount gotcha (`DATA071`) is a pre-existing blind spot in the
   glob, out of scope.

---
## v2-FINAL (post Round-2) — implemented

Both Round-2 reviewers cleared the scope ("YES, v2 is the right scope"). Changes folded in:

**Layer 2 non-redundancy with `check_ping` (verified, now recorded):** `ping` targets the AGS
Pi ICMP at `10.10.10.1`, which comes up every ~60 s boot during the reboot loop → `bad_ping`
resets below `ping_max=3` → `check_ping` does **not** reliably fire. H&S needs the AGS *app*
(never stays up) → `bytes_remaining` continuously NA. Layer 2 catches "AGS pingable but not
sending data" — the gap ping leaves open (the mj05 FPGA-missing variant ran a reboot loop
**~5.5 days** unnoticed *because* ping stayed green). This is Layer 2's core justification.

**ADD-NEW — Layer 3: repeating-futile-scrub ALERT** (Round-2 scope finding #4). In
`check_sensor_drive`, count consecutive low-state scrub respawns (`_scrub_respawn_count`,
incremented when `_maybe_spawn_scrub` actually launches; reset to 0 when space recovers ≥
`low_space`). After `futile_scrub_alert_after` (default 3 ≈ 3×30 min cooldown ≈ 1.5 h) with
no recovery, emit a distinct one-shot alert (`_futile_scrub_alerted` latch): "auto-scrub
re-fired Nx without clearing low space — manual intervention needed." A **symptom** alert —
sidesteps the "nothing-purgeable vs purged-but-full" policy question that killed old Layer 4,
needs no SSH/`df`/log-parsing. Complements Layer 2 (covers the AGS-sending-data case).

**Correctness fixes (Round-2 correctness BLOCKERs/mechanical):**
1. Layer 1: accumulate resolved `disk_usage().free` into a list; `if not frees: return None`
   (guards `max([])` when every DATA path raises OSError — non-empty glob, all skipped).
2. Layer 2: exact body — lazy-init `_last_numeric_hs_time` to now; on numeric sample reset
   clock + clear latch BEFORE measuring; numeric guard is PR#78's
   `isinstance(x,(int,float)) and x == x`.
3. GB threshold uses `* 2**30` (binary GiB, matches `check_pi_space` display).
4. Boundary: alert on `best_free < threshold` (strict), re-arm on `>=`.
5. Tests: NEW file `test_state_monitor_drives.py` with its own `make_drive_monitor` helper
   (does not reuse/break PR#78's `make_monitor`); add "all-paths-OSError → None" test.

**Final config keys (3):** `recovery_low_gb=25`, `hs_stale_s=900`, `futile_scrub_alert_after=3`.
**Final new state (5 flags):** `_recovery_low_active`, `_hs_stale_active`,
`_last_numeric_hs_time`, `_scrub_respawn_count`, `_futile_scrub_alerted`.
