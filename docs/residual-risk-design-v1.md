# Residual-risk closure — synthesized design v1 (for Round-1 adversarial review)

**Scope:** `plugins/state_monitor.py` + `config/main.toml` (+ optional `scripts/`) in
**mjolnir-hamma only**. No brokkr/sindri code. New `check_*` methods slot into the
existing `run_checks` list under the `enable_drive_checks` guard; new `__init__` kwargs +
in-memory state, mirroring PR #78. Synthesized from a lean proposal (A) and a
defense-in-depth proposal (B); this v1 commits to specific choices for review to attack.

## The three risks (recap)
1. **Telemetry blind spot** — scrub trigger reads AGS-pushed `bytes_remaining`; the AGS
   stops sending H&S when `/ags/data` fills + reboot-loops → trigger blind when needed.
2. **`/media/pi/DATA??` unmonitored** — no free-space check on the mj-side recovery
   targets; if all fill, brokkr can't write and recovery has nowhere to land.
3. **Firing-but-futile scrub** — a scrub can run and free nothing; undetected/unescalated.

## Corrected metric (a flaw found in synthesis — reviewers verify)
Futility (risk 3) must be judged on **`/ags/data` free space** (the drive the scrub's
`--purge` is meant to free), NOT on the mj-side `DATA??` free space. A *working* scrub
**recovers files onto** `DATA??` (decreasing its free space) while purging `/ags/data`, so
a `DATA??` delta is the wrong signal. Design A had this backwards; corrected here.

---

## Layer 1 — `check_recovery_drives` (risk 2 keystone; both designs agree)
Local, telemetry-independent. Cheapest and highest-certainty; anchors the shared
free-space primitive.
- Glob `/media/pi/DATA??` (`import glob`; `shutil` already imported). Per-path
  `shutil.disk_usage` in a try/except (a drive unmounting mid-check raises `OSError` →
  skip that path, don't abort).
- Alert when the **roomiest** drive's free `< recovery_low_gb` (that's the real "nowhere
  to recover to" condition; per-drive 100% is normal — DATA55 — and must not spam).
  Message includes the per-drive-low count ("3/4 DATA drives below 5 GB").
- **Alert-only, never spawns a scrub** — a full DATA drive is not fixed by scrubbing the
  AGS; the fix is human (rotate/add a drive).
- Empty glob → `None` (defer the "no drives" alert to the existing `check_drive`).
- Debounce: `_recovery_low_active` descent-entry latch, mirroring `_low_space_active`.

## Layer 2 — H&S staleness watchdog (risk 1a; from design B — high value, no SSH)
Closes the "stream of NAs → total silence" gap with **zero external dependency**.
- In `check_sensor_drive` (or a sibling), track `_last_numeric_hs_time` = monotonic time
  of the last *numeric* `bytes_remaining`. When the current sample is numeric, update it.
- If `now - _last_numeric_hs_time > hs_stale_s`, emit a distinct alert: "H&S from AGS
  stale for N min — AGS may be silent / reboot-looping." Debounced (`_hs_stale_active`).
- This fires *precisely when the value trigger can't* (AGS dark) and needs no network.

## Layer 3 — direct SSH `df /ags/data` probe (risk 1b; both designs; DEFAULT-OFF)
The only **automated** telemetry-independent detection of an actual `/ags/data` fill
(layer 2 only alerts a human). Included but conservative because SSH-in-the-monitor-loop
is untried here.
- New `check_ags_space`: `subprocess.run(["ssh","-o","BatchMode=yes","-o",
  f"ConnectTimeout={ags_ssh_timeout_s}", ags_host, "df","-B1","--output=avail",ags_path],
  timeout=ags_ssh_timeout_s, capture_output=True, text=True)`. Parse avail bytes.
- **Bounded:** gated to once per `ags_probe_interval_s` (default 300 s = every 5th cycle)
  via a monotonic stamp; `BatchMode=yes` (never blocks on prompt) + `ConnectTimeout` +
  `subprocess timeout` (belt-and-suspenders). `TimeoutExpired`/nonzero/unparseable →
  `logger.warning` + return `None` (inconclusive ≠ full; unreachable is already covered by
  `check_ping`, so don't double-alert).
- On "reachable AND `/ags/data` free `< ags_low_gb`": alert **and** call the existing
  `self._maybe_spawn_scrub()` (same cooldown, same flock, same lock file — no second scrub
  path). Separate `_ags_low_active` latch for its alert lifecycle.
- **`ags_probe_enabled = false` by default.** Dark-launch on mj05, enable fleet-wide only
  after `verify_deployment.sh check_ags_connection` confirms pi→AGS SSH keys (MEMORY:
  recurring missing keys → a missing key would make this a false "unreachable" every 300s,
  which we handle as `None`/silent, so fail-safe, but the probe is then useless there).

## Layer 4 — futile-scrub detection (risk 3; design B's correct metric, corrected)
- On a real scrub launch (`_maybe_spawn_scrub` returns True), snapshot
  `_scrub_baseline_ags_free` = current `/ags/data` free and `_scrub_baseline_time`.
  "Current `/ags/data` free" = `bytes_remaining` if numeric this cycle, else the latest
  `check_ags_space` df result if the probe is enabled+fresh, else `None` (can't measure).
- After `scrub_effect_wait_s` (default 300 s, > typical scrub runtime) since launch, if
  `/ags/data` free did **not** rise by `>= scrub_min_freed_gb` **AND** the drive is still
  below `low_space` → classify **futile**. Require the verdict to persist **two**
  consecutive post-wait cycles before escalating (so a single flock-rejected no-op, per
  PR #78 §6, isn't misread as futile).
- Escalate with a distinct message: "auto-scrub ran but freed <1 GB and `/ags/data` still
  low — manual intervention needed." If baseline is `None` (post-restart, or no free-space
  signal) → skip (no false alarm).
- **Dependency (flagged):** if the AGS is fully silent (`bytes_remaining` NA) AND the df
  probe is disabled, futility can't be measured — but that case is already covered by the
  staleness alert (layer 2), which is the actionable signal ("AGS is dark").

## Escalation policy (light; deliberately not a full tier ladder in v1)
- Preserve PR #78 behavior: one descent-entry alert, then quiet while recovering.
- **Distinct message strings** per condition (low-space / stale-H&S / no-landing-zone /
  futile-scrub) so a human triaging chat reads the *diagnosis* directly.
- A single **CRITICAL re-alert throttle** `critical_realert_s` (default 21600 = 6 h) for the
  persistent conditions (futile / stale / no-landing-zone) so a multi-hour outage yields a
  few messages/day, not one per cycle. (Full INFO/WARN/CRITICAL ladder from design B is
  deferred as over-engineering for v1 — reviewers may argue it back in.)

## Config keys (all in `[steps.state_monitor]`; constructor kwargs w/ same defaults)
```
recovery_low_gb      = 5      # GB free on roomiest /media/pi/DATA?? below which to alert
hs_stale_s           = 900    # s without a numeric bytes_remaining -> stale-H&S alert
ags_probe_enabled    = false  # enable the SSH df probe (dark-launch per-unit first)
ags_host             = "hamma"      # ssh alias (matches scrubber default)
ags_path             = "/ags/data"  # remote path (matches scrubber default)
ags_probe_interval_s = 300    # s between SSH df probes
ags_ssh_timeout_s    = 10     # subprocess + ssh ConnectTimeout, s
ags_low_gb           = 20     # GB free on /ags/data below which to alert + scrub
scrub_effect_wait_s  = 300    # s after a scrub launch before judging effect
scrub_min_freed_gb   = 1      # min GB /ags/data must gain to count as effective
critical_realert_s   = 21600  # s between re-alerts for persistent conditions (6 h)
```
New in-memory state: `_recovery_low_active`, `_hs_stale_active`, `_last_numeric_hs_time`,
`_ags_low_active`, `_last_ags_probe_time`, `_last_ags_free_gb`, `_scrub_baseline_ags_free`,
`_scrub_baseline_time`, `_futile_pending`, `_last_critical_alert` (all reset on restart —
safe direction, per PR #78 §6).

## Deliberately deferred (follow-ups, not in v1)
- **Design B layer 3b** (parse a `SCRUB_RESULT` marker from `scrub_log`) — adds scrub
  output-format coupling; the local `/ags/data`-free delta (layer 4) answers "did it help?"
  without it. Defer.
- Full 3-tier severity ladder + `escalate_after_n`.
- udisks suffixed-mount gotcha (`DATA071`) — layer 1's glob inherits it; out of scope.

## Test plan (mirrors `tests/python/test_state_monitor_scrub.py`; new file
`test_state_monitor_drives.py`)
- **Layer 1:** all-drives-low → alert; one-drive-has-room → None; empty glob → None (no
  error); per-path OSError skipped, healthy path still evaluated; descent-entry debounce.
- **Layer 2:** NA stream past `hs_stale_s` → stale alert; numeric sample resets the clock;
  staleness fires with `scrub_command=""` (proves no shared path).
- **Layer 3:** df parsed → drives decision; `TimeoutExpired` → None + warning, no alert;
  nonzero/unparseable → None; interval-gated (run once per window); disabled by default
  (subprocess.run never called); **blind-spot test** — `bytes_remaining`=NA but df low →
  scrub still spawns via the df path; argv contains `BatchMode=yes`+`ConnectTimeout`+host+path.
- **Layer 4:** `/ags/data` free flat after wait + still low → futile (after 2-cycle
  debounce); free rose → no futile alert; single flat cycle (flock-reject) → no escalation;
  baseline None → no alarm; **uses /ags/data free, not DATA free** (regression test for the
  corrected metric).
- **Escalation:** distinct message strings; `critical_realert_s` throttle (two criticals in
  window → one message); recovering path stays quiet.
- **Init wiring:** real `__init__` sets every new attr/config default.

## Assumptions to verify before implementing (flagged)
1. `ssh hamma df -B1 --output=avail /ags/data` works non-interactively from the mj Pi and
   `--output=avail` (GNU df) exists on the AGS Pi. (Scrubber SSHes to `hamma`, so creds/path
   exist; `--output` is GNU-coreutils.)
2. `bytes_remaining` (GB) and `/ags/data` `df` describe the **same** volume (so `low_space`
   and `ags_low_gb` are the same disk). Thresholds kept independent in case not.
3. Typical scrub runtime `< scrub_effect_wait_s` (300 s) — else an in-progress scrub could
   look futile; the 2-cycle debounce mitigates but may need per-fleet tuning.
4. **Policy question for the user:** on the mj05 incident a correctly-firing scrub freed
   little (data already on MJ; manual drain), so layer 4 would flag "futile" while the drive
   was still full. That is arguably the *correct* alert ("scrubbing isn't freeing this drive
   — intervene"), but confirm we want it, vs a softer "nothing to purge" classification.
