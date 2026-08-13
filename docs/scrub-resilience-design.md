# AGS scrub resilience — design

**Scope:** `mjolnir-hamma` only (`plugins/state_monitor.py`, `scripts/hamma_scrub.py`,
`config/main.toml`, a new systemd timer unit). No brokkr/sindri changes.

**This spec is about one thing: keeping the AGS-pi drive (`/ags/data`) clean.** The scrub's
job is to offload triggers from `/ags/data` to the mj-pi and purge what's confirmed.

**Non-goals (explicitly out of scope):**
- **mj-pi `/media/pi/DATA??` drive fullness.** A full MJ recovery drive is a *different*
  problem with a *different* remedy (rotate/replace the drive). It matters here only as a
  *precondition* (recover needs somewhere to land). Monitoring it belongs in a separate
  MJ-drive-management effort. (DATA55 has been 100 % full since June 1 — file separately.)
- The AGS reboot loop / VL805 USB wedge (hardware; fixed by cold power-cycle).

**Status:** design, **re-ranked after adversarial (red-team) review** — see the changelog at
the end. Supersedes the direction of PR #78 (scrub-trigger-fix) and PR #80
(disk-safety-monitors); their useful pieces are folded in. **Their framing (an edge-vs-level
trigger fix) is not the root cause — and neither is a timer (see §2/§3).**

---

## 1. What actually happened (mj05, Jul 9–13 2026)

Reconstructed from telemetry CSVs and recovered-file forensics on mj05. `/ags/data` is the
AGS USB drive (462 GB); `bytes_remaining` (H&S "Disk Remaining") is its free space.

| Time (UTC) | Evidence | State |
|---|---|---|
| Jul 9 20:56 | telemetry 100.22 → 99.54 (clean, no NA in CSV) | free crosses `low_space`=100 GB |
| Jul 9 20:56 → Jul 10 17:59 | **monotonic** 100 → 0, no upward tick | 21 h fill, no purge visibly freed space |
| entire fill window | **zero `*_recovered.bin` written** | scrub did no recovery |
| Jul 10 18:00 → Jul 11 03:34 | telemetry pinned at 0 | drive FULL ~10 h, data loss |
| Jul 11 03:34 | first & only recovered-file cluster (5 files) | one scrub, ~31 h late |
| Jul 11 ~04:20 → dark | free reads frozen 448.96 then NA; ping flaps | AGS drive drops → reboot loop |
| Jul 13 ~11:48 | cold power-cycle | VL805 USB firmware reloads, recovers |

Causal chain (settled): **`/ags/data` full → AGS reboot cycle → USB/FPGA (VL805) wedged →
persistent reboots** until the operator stopped AGS (~Jul 11 12:00) and cold-cycled Jul 13.
The reboot/USB half is understood and out of scope.

### What we can and cannot claim about the scrubber

1. **The trigger *should* have fired — but we cannot prove it *effectively* did.** In the
   deployed code (`0.4.x @ 426ff2b`), `check_sensor_drive` does
   `if (space_now < low_space) and (space_pre >= low_space): self._spawn_scrub()`. At the
   crossing that is `(99.54 < 100) and (100.22 >= 100)` → **True**, so the *edge condition*
   was met. But "condition met" ≠ "a scrub ran and did work." `_spawn_scrub` wraps the
   command in **`flock -n`** (non-blocking) and logs `"Spawned scrub"` **unconditionally**
   after `Popen` succeeds — *before* `flock` has acquired anything. So if a **prior scrub was
   hung** holding the lock, the spawn is a **silent no-op** and even the log line would have
   lied. **Two possibilities are indistinguishable from the destroyed evidence:** (a) a scrub
   ran and freed nothing, or (b) the spawn silently no-op'd against a hung prior scrub. They
   have *different fixes*. (Verified downstream: the deployed scrub had **no** fail-fast SSH
   and a **3600 s** scan timeout — so a scan against the degrading AGS could hang up to an
   hour, and with `flock -n` + the single-shot edge, one hang stalls the safety net for the
   whole window. This makes (b) a live, code-supported hypothesis, not a footnote.)
2. **No purge *visibly* freed space — strongly indicated, not proven.** `bytes_remaining`
   fell 100 → 0 monotonically with no up-tick. At the observed ~5 GB/hr, a purge freeing even
   a few GB *would* show (a 3 GB purge erases ~36 min of inflow in one 1-min sample), so this
   inference is sound **at this rate**. It would NOT hold at the design's "max rate"
   (12.5 GB/min), and it rests on the same `bytes_remaining` signal we elsewhere call
   unreliable — so: *strongly indicated for the fill window, not proven.*
3. **It had somewhere to write.** DATA56 had 851 GB free throughout. "No landing zone" is
   ruled out for this incident.
4. **Why it failed is unrecoverable.** Scrub output was `DEVNULL`'d; the journal + all 10
   rotated `brokkr_hamma_005.log.*` were overwritten within **7 minutes** by reboot-loop
   spam (HAM-112). No trace survives.

### What the current system tells us (measured Jul 13, healthy)

- A full dry-run scrub completes in ~21.5 s healthy, but under **active AGS recording** the
  **AGS scan alone rose ~1 s → 99 s** and the MJ scan to minutes (see §3.5). The code path
  works; the Jul 9–11 failure was **condition-dependent** (storm + degrading AGS).
- **Throughput:** `recover` = one `ssh dd` per trigger, `purge` = one `ssh rm` per file —
  sequential plain SSH. Round-trip 0.29 s plain / 0.018 s multiplexed.
- **Telemetry failed when needed:** as the AGS degraded, `bytes_remaining` went NA. Any
  decision that *reads* `bytes_remaining` is blind in exactly this state.

---

## 2. Root cause (honest version)

**The specific mechanism is unrecoverable** (§1.4). What we *can* name is a set of
structural defects, and — critically — **we must design for the failure mode we cannot rule
out (a hung / silently-no-op'd scrub), not just the one that's easier to fix (a scrub that
ran but was too slow).**

- **Silent single-point failure (the one the timer does NOT fix).** `flock -n` turns a
  hung prior scrub into a silent no-op for every subsequent spawn; the spawn-side log lies;
  the edge is single-shot so it never retries. A single hang → zero effective scrubs for the
  whole window, invisibly.
- **Blind.** `DEVNULL`'d output; the logs that might have caught it were destroyed in
  minutes.
- **No fail-fast.** Deployed SSH had no `ConnectTimeout`/`BatchMode`; the scan could hang up
  to 3600 s against a sick AGS, holding the lock.
- **Reactive + single-shot + telemetry-dependent.** Fires only on the downward crossing, one
  attempt, off a signal (`bytes_remaining`) that went NA.
- **Throughput-limited.** Per-item sequential SSH; a heavy scrub can outlast its fill window.

**The trap to avoid:** PR #78 was tangential because it fixed trigger *arming* when the
trigger provably armed. A **timer**, made the headline, is tangential *in the same way* if
the real cause was a hang — more timer ticks just no-op against the held lock. So the timer
is **not** the primary fix. The primary fixes are the ones that make a hang **impossible to
happen silently** and **visible when it does**.

---

## 3. Design — re-ranked

Ordered by how directly each addresses the failure mode we cannot rule out (the hang), not
by how appealing it sounds.

### 3.1 Make silent failure impossible + visible (partially built) — highest priority

**Built (commit `§3.1`), and honestly bounded by red-team review:**
- **Fail-fast SSH** (`BatchMode` + `ConnectTimeout=10`) + **bounded scan timeout** (3600 →
  `SCAN_TIMEOUT=600 s`). Real, unconditional wins: they shorten a hung scan's lock-hold from
  up to an hour to ≤10 min. *(But 600 s bounds only the scan phase; recover/purge are
  bounded per-item, not in aggregate — so total lock-hold is still not capped. §3.2.)*
- **Honest `_spawn_scrub`** — probes the lock (real `flock(2)`, interoperates with the
  spawned `flock -n`) and logs whether it actually spawned vs. skipped, instead of logging
  "Spawned" unconditionally. **Limit:** only useful if the log survives — and mj05's logs
  were destroyed in 7 min (HAM-112). So this is load-bearing only *with* §3.2's durable log.
- **Stuck-lock detector** `check_scrub_health` — alerts once if the lock is held for
  `stuck_scrub_cycles` (default 10 ≈ 10 min) consecutive cycles. **Three limits found in
  review, all deferred to §3.2 because they need a *progress signal*:**
  1. **Alert-only, no recovery.** It *narrates* a hang; it does not free the lock. The drive
     still fills until a human acts (mj05: ~2 days unwatched).
  2. **Lock-age ≠ progress → cry-wolf.** A legit large recovery (60 s/trigger, unbounded
     aggregate) can hold the lock > 10 cycles → false "hung" alert. Can't distinguish
     "grinding" from "hung" without a heartbeat.
  3. **`/tmp`-full blind spot.** During a disk-full event the lockfile can be uncreatable
     (`ENOSPC`) → the probe currently fails to "free" → the alert *cannot fire in the exact
     incident it targets*, and `_spawn_scrub` resumes lying. Needs a tri-state
     (held/free/**unknown**) where the detector escalates "unknown" but the spawner does not
     suppress the scrub.

**Conclusion:** §3.1's detection/visibility half is built; its **teeth (safe recovery) and
robustness (no cry-wolf, no disk-full blind spot) all require a progress heartbeat** — which
is §3.2. §3.1 and §3.2 are therefore implemented together (see §3.2).

### 3.2 Progress heartbeat → durable log, real stuck-detection, self-healing — second (carries §3.1's teeth)

The scrub writes a **heartbeat** (timestamp + phase + running counts) to a durable,
rotation-safe file as it advances. Everything §3.1 couldn't do safely follows from it:
- **Persistent, rotation-safe scrub log** (`scrub_log`, from PR #78): scan/recover/purge
  counts, per-phase timings, errors, lock result. Survives the reboot-spam, so §3.1's honest
  logging becomes durable.
- **Progress-gated stuck detection:** redefine "stuck" as **lock held AND heartbeat stale**
  (not just lock-age). This kills the cry-wolf false positive (a working recovery keeps the
  heartbeat fresh) and is the only safe basis for auto-recovery.
- **Self-healing recovery (§3.1's dropped teeth):** on a *genuine* hang (lock held +
  heartbeat stale beyond threshold), **kill the stale scrub + free the lock** so the next
  cycle re-spawns. Safe precisely because the heartbeat proves it's hung, not working.
- **Tri-state lock probe** (held/free/unknown) so the `/tmp`-full case escalates in the
  detector without suppressing the scrub in the spawner.
- **Futile-scrub alert:** consecutive runs that purge 0 files while free space keeps falling
  → "running but not freeing space" (the symptom the 21-h ramp never surfaced).

### 3.3 Timer-driven periodic scrub — steady-state drain, NOT the cure for a hang

**Built:** `files/hamma-scrub.{sh,service,timer}` + install wiring in
`unified_install/lib/brokkr.sh`. Oneshot service runs the wrapper (`flock -n -E 0`
over the same `/tmp/hamma_scrub.lock` + `--recover --purge --since auto`, kept in sync
with `state_monitor`'s `scrub_command` and validated by `test_scrub_timer.py`); timer =
`OnBootSec=10min` / `OnUnitActiveSec=15min`. Service is `Nice=10` + `IOSchedulingClass=best-
effort/7` so the periodic scan yields to brokkr's write pipeline without starving. **Two
roles:** the steady-state drain, and the reliable **re-spawn backstop** for §3.2 (after a
hung scrub is killed, the next tick re-runs it — the low-space edge won't re-fire on its own).

**Red-team corrections (3 reviewers) — no CRITICAL ship-blocker; honest scope:**
- **I/O-priority stanza was on the wrong machine — removed.** The scrub runs on the mj-pi;
  `/ags/data` + the write pipeline are on the *AGS Pi* (separate host, over SSH), so an mj-pi
  `ionice` governs nothing on the contended drive (and `mq-deadline` ignores ionice anyway).
  Kept `Nice=10` (mj-pi CPU only).
  **Because the scrub does no local block I/O on the AGS drive, the timer does NOT make the
  fill worse** — it lengthens its own scan, not the writer. So §3.3 is safe to ship *before*
  §3.5 (incremental scan), which remains the real scan-cost fix.
- **AGS-side commands are CPU-niced to the DAS floor (`AGS_NICE = "nice -n 19 "`).** Fleet
  check (mj05 AGS) settled the priority question with data: active I/O scheduler is
  `mq-deadline` (ignores ionice — the DAS's *realtime* ioprio is **set but inert**), and the
  DAS java runs at CPU `nice 19` while an unniced remote command runs at `nice 0` — i.e. the
  scrub would **outrank** the writer on CPU. Fix: wrap the heavy AGS-side commands (header
  scan, recover `dd` reads, purge `rm` incl. the per-file retry) in `nice -n 19` so they sit
  at the writer's floor instead of preempting it. `ionice` deliberately **not** used (theatre
  under mq-deadline). The real CRC/fifo-overflow contention has a separate fix; this is just
  the scrub being a good citizen so it can't steal CPU from the writer.
- **`Wants=brokkr` → `After=` only:** a 15-min tick must not resurrect a deliberately-stopped
  brokkr. **`StartLimitIntervalSec=0`:** so repeated §3.2 SIGKILLs (a killed scrub = a systemd
  *failed* activation) can't trip the start-limit and silently disable the timer under
  sustained AGS-sick conditions.
- **Effective cadence is `>= 15 min`, not strict:** under storm load a scrub runs minutes and
  a tick that lands mid-scrub is skipped by `flock -n`. Fine for a drain; don't advertise 15 min.
- **`--since auto` kept (not narrowed):** a bounded `--since` would cut scan cost but
  *under-purge* old confirmed files (they'd fall outside the MJ scan window) → less drain.
  Completeness wins for a drain; §3.5 is the cost fix.
- **A §3.2 kill leaves the oneshot in systemd `failed` for ≤15 min** (until the next tick
  re-activates it) — a sitrep/health check must not misread that as a fault.
- **M1 (inert recovery) closed for the healthy-disk case:** the kill releases the flock
  synchronously, so §3.2's immediate `_spawn_scrub` succeeds; the ENOSPC case (where
  `_spawn_scrub` refuses an `unknown` lock) falls back to the timer, a ≤15-min gap.
- **Deploy coupling:** ship §3.3 only *with* §3.2 — a bare timer no-ops against a hung lock
  until §3.1/§3.2 make a held lock visible/recoverable.

A systemd timer (~15 min) that runs the scrub regardless of `bytes_remaining`.

- **What it genuinely buys:** it keeps `/ags/data` drained in steady state so free space
  never *approaches* the threshold under normal storms, and it removes the **telemetry**
  dependency (doesn't read `bytes_remaining`).
- **What it does NOT buy (correction after review):** it is **not** "telemetry-independent
  backbone" — it removes the telemetry dependency but **inherits the AGS-reachability
  dependency, which is the thing that actually broke.** A timer firing into a rebooting AGS
  still fails/hangs (now fast, *if* §3.1's fail-fast is deployed). And against a hung scrub
  holding `flock -n`, **every timer tick no-ops** — the timer adds firing cadence, not
  hang-recovery. It is only load-bearing once §3.1 is in place.
- **Write-cost is not a concern** (evaluated): scrub is read-dominated (scan = reads, vfat
  `relatime`; recover writes ~0–1/run steady-state; purge = KB vfat metadata). < 1 % of the
  ~120 GB/day these drives already log. Add `noatime`.
- Keep `flock -n` so timer and threshold runs can't overlap — **but only after §3.1 makes a
  held lock visible**, else the timer just multiplies silent no-ops.

### 3.4 Two-threshold escalation — fair-weather improvement (would NOT have prevented mj05)

**Built:** `check_sensor_drive` in `state_monitor.py` is now level-triggered across
`purge_space=200` / `alert_space=75` (replacing the single edge-triggered `low_space`);
`_maybe_spawn_scrub` gates re-launches to `scrub_cooldown_s=300` (5 min, so it's more
responsive than the 15-min §3.3 timer), stamped **only on an actual launch** (a no-op while
the lock is held doesn't burn the cooldown); NA readings are skipped (fair-weather). Config
keys in `main.toml`; tests in `test_state_monitor.py::TestTwoThresholdDrain`.

**Red-team corrections:** the alert re-arms on recovery into the drain band (≥ `alert_space`,
hysteresis) not only above `purge_space`, so a unit oscillating around the floor pages each
new dip rather than once ever; `__init__` warns if `alert_space >= purge_space`; the alert
wording no longer claims "still falling" (it states the level + that the scrub is running).
**Release note:** the 75–200 GB *purge band is now silent by design* — the old edge-trigger's
"Remaining GB on drive" message is gone; the first operator page is at `alert_space=75`.

Split `low_space` into a **purge** and a higher-urgency **alert** threshold, level-evaluated:

- **Purge at `xx = 200 GB` free**: while free < `xx`, run the scrub every cooldown
  (level-triggered, not one-shot). Proactive draining before it's critical.
- **Alert at `yy = 75 GB` free**: fire only if free punches *through* `xx` to `yy` **despite**
  purging — i.e. **purge is losing**. Debounced; re-arm above `yy`.

**Honest caveats (post-review):**
- Both thresholds **read `bytes_remaining`**, which went NA in the failure mode — so this
  layer is **blind exactly when it mattered**. It's a good-weather improvement.
- **It would not have prevented this incident.** The scrub had ~21 h of runway after the
  crossing and used none of it — *time was never the binding constraint*. Raising 100→200 GB
  just moves the single edge-spawn earlier; if that spawn no-op'd (§1.1b), 200 vs 100 changes
  nothing. 200/75 are reasonable belt-and-suspenders, **not** the fix.

| Knob | Value | Rationale (arithmetic, not evidence) |
|---|---|---|
| timer interval | 15 min | steady-state drain; observed ~5 GB/hr ⇒ ~1.25 GB/interval |
| `xx` purge start | 200 GB free | ~16 min max-rate runway above a fast scrub |
| `yy` alert | 75 GB free | ~6 min max-rate runway when paged; ~15 h at observed rate |

### 3.5 Make the scrub fast + incremental scan — hardening (claims corrected)

- **Batch the deletes** (one `rm -f f1…fN` per 100 files) — **this is the robust win**, and
  it works even on plain SSH (fewer round-trips). *Built.*
- **Persistent SSH (`ControlMaster`)** — **credit corrected:** the headline "600×" is
  *batching × multiplexing*; **batching alone gets ~100×** without ControlMaster's fragility.
  And ControlMaster **self-disables in the failure mode** — on a sick AGS, `open_control_master`
  fails → fallback to per-call SSH = the slow baseline. So the speedup is *fair-weather*; the
  incident-relevant part of the same commit is the fail-fast flags (§3.1), not the speed.
- **Measurement honesty (corrected):**
  - Lab: batched+CM 0.163 s vs ~100 s / 500 files. On-sensor: 0.117 s vs ~63 s / 230 files.
    Real, but **the on-sensor files were 0-byte throwaways** (the earlier "real AGS files"
    wording was wrong — corrected in the commit trailer). Real 20 MB triggers cost *more* to
    `rm` on vfat (cluster-chain freeing); the 0.117 s is a **floor**, not a representative
    number for real payloads.
  - **`recover` was never measured on-sensor.** The 16×/round-trip does **not** transfer to
    `recover`'s `dd` of ~20 MB payloads (transfer-bound, not handshake-bound). Recover
    throughput under storm rate is **unvalidated**.
- **Incremental MJ scan (BUILT)** — `scan_mj_files(cache_file=...)` caches each hourly dir's
  header set keyed by a cheap `(mtime, .bin-count)` signature; an unchanged dir is reused
  without re-reading a single file, so only the current (being-written) hour and genuinely
  new dirs pay the per-file cost. **This is the load-bearing scan fix** — the scan, not
  purge, is the wall-clock driver (the MJ scan is the ~20 s / minutes-under-load cost). Cache
  lives on **tmpfs** (`/dev/shm/hamma_scrub_mj_cache.pkl`, no SD wear; a reboot costs one full
  scan); it self-prunes (dirs not seen are dropped) and falls back to a full re-read on any
  cache anomaly (corrupt file, changed signature). `--mj-cache ""` forces a full scan. The
  full scanner (`_scan_mj_full`) is unchanged and used when no cache is configured. Tests:
  `test_hamma_scrub.py::TestIncrementalScan` (13, incl. a patched `_read_dir_headers` proving
  older-dir cache hits don't re-read, parity-vs-full-scan, corrupt-cache fallback, self-prune).
  **Red-team corrections (2 CRITICAL):** (1) cache is **JSON, not pickle** — `/dev/shm` is
  world-writable (1777), so unpickling it was a local-RCE-as-`pi` vector; JSON stores headers
  as hex and can't execute code on load. (2) brokkr **append-writes** `.bin` in place
  (`mode="ab"`, no atomic rename) on **2 s-granularity vfat**, so an in-place completion can
  leave `(mtime, count)` unchanged → stale header → wrong purge; fixed by **always
  re-reading the newest hourly dir per drive** (the only one brokkr appends to — past dirs are
  immutable once the hour rolls over). Tests pin both signature components on non-newest dirs
  and the in-place-growth case. `duplicate_count` legitimately diverges from the full scanner
  for cross-dir dups (log stat only, never a control input — commented).
- **`select_target_drive` once per run**, not per trigger (drops a per-trigger `os.listdir`
  over 904 dirs). Minor for *this* incident (0 triggers recovered during the fill).
- **`--since auto` for the timer path** needs pinning down — under a storm it pushes the MJ
  scan back over many dirs, undercutting the incremental-scan win.

---

## 4. What this supersedes

| Prior work | Disposition |
|---|---|
| PR #78 edge→level trigger + cooldown | **Reframed** — level-triggering survives as the `xx` purge behavior (§3.4), not "the fix." |
| PR #78 `scrub_log` | **Kept & promoted** — observability (§3.2) is now second-priority, not an afterthought. |
| PR #80 Layer 1 `check_recovery_drives` | **Out of scope** — mj-pi `DATA??` fullness is a separate effort. |
| PR #80 Layer 2 H&S staleness watchdog | **Folded in** — cheap signal; but note (§3.3) it shares the AGS-reachability dependency. |
| PR #80 Layer 3 futile-scrub alert | **Partly reframed, not fully built.** §3.2 builds the *stuck/hung*-scrub alert (lock held, heartbeat not advancing) — NOT a per-run "scrub completed but freed ~0 bytes" delta check. The operator-facing half of "futile" (space keeps falling *despite* auto-scrubs) is covered instead by `alert_space=75` (§3.4). A true per-scrub freed-bytes assertion is unbuilt (would need a stable free-space read before/after, which is exactly the NA-prone signal). |

---

## 5. Test plan

- **Unit (pytest):** honest-lock logging + stuck-lock latch; bounded scan timeout; two-
  threshold state machine (purge below `xx`, alert crossing `yy`, debounce/re-arm);
  `select_target_drive` once; batched-delete chunking incl. **exact-multiple boundaries** and
  **partial-chunk per-file attribution**; **`run()`-level ControlMaster wiring** (open →
  thread to recover+purge → close); incremental-scan cache hit/miss; futile-scrub latch.
  *(The batched-purge/ControlMaster/run-wiring tests exist; the rest are TODO.)*
- **Integration (bench/idle unit):** timer fires; a forced near-full condition drives purge
  below `xx`, alert at `yy`; **inject a hung scrub and verify §3.1 makes it visible + a timer
  tick does NOT silently no-op.** Measure real scrub wall-clock incl. **recover** (unmeasured
  so far).
- **Load reproduction:** near-full `/ags/data` + sustained influx, instrumentation on —
  confirm the scrub keeps pace *or* the futile/stuck alert fires. The test the incident
  lacked.

---

## 6. Open questions / risks

1. **Which failure mode actually occurred is unrecoverable** (hang vs ran-but-freed-nothing).
   The design now covers both, but §3 priorities assume the hang is at least as likely — if
   later evidence points to "ran but slow," the timer (§3.3) rises in value.
2. **Fast-scrub wall-clock under load** must be *measured* (recover especially); §3.4 values
   are arithmetic starting points.
3. **Incremental-scan cache invalidation** (compression rewrites, udisks suffixed mounts
   `DATA071`) — fall back to full scan on cache miss/anomaly.
4. **`bytes_remaining` = free space at the `/ags/data` mount on the AGS Pi, in GB** —
   **user-confirmed 2026-07-14**, consistent with the empirical read (fills to 0; scrub targets
   the same mount; 448.96 ≈ 462 GB) and with the df cross-check on mj03/mj08. Do *not*
   over-specify the medium: `/ags/data` is usually a USB drive but can be internal/SD on some
   units (mj54 lacks the USB drive, HAM-110). Thresholds key off this signal; the residual
   risk is only that it goes **NA** when the AGS is dark (§6/§3.4, mitigated by the
   `hs_stale_cycles` watchdog), not that it points at the wrong drive.
5. **ControlMaster socket** is `/tmp/hamma_scrub_cm_<pid>.sock` — PID reuse after a hard
   crash can collide with a stale socket (degrades to slow per-call SSH, silently). Consider
   `%C`-hashed paths or stale-unlink. And `/tmp`-full during a disk-fill disables the
   multiplexing exactly when needed — log the degradation.
6. Per-unit variation (mj05 `nochargecontroller`; PAMMA differs) — thresholds configurable.
7. **Observability rides the SD; durable observability is a *hard dependency* on HAM-112/113.**
   The tmpfs heartbeat (`/dev/shm`) is the load-bearing *live* signal and survives an SD fill,
   but the **post-mortem** — the durable `scrub_log` on the SD — self-disables (→DEVNULL) once
   the SD floods with brokkr/rsyslog spam (the exact incident condition, HAM-112/113, both
   still To Do). So a repeat could still leave *no on-disk trace of the scrub run* even with
   this work deployed. The two are complementary, not substitutes: this effort makes the
   *live* state visible and recoverable; HAM-112/113 is what makes the *forensic* record
   survive. Ship both or accept the forensic gap.

---

## §3.2 as-built + red-team corrections (honest scope)

Built and reviewed (3 adversarial reviewers). **What §3.2 genuinely delivers:** it
eliminates the *permanently-stuck lock* (the §1.1b silent-no-op mode) and the DEVNULL
blindness, and auto-recovers **as soon as the AGS is reachable again**. **What it does NOT
do (and no code can):** prevent the `/ags/data` fill while the AGS itself is wedged — killing
a hung scrub frees the lock, not AGS bytes; only a *completed purge* drains the drive, and
that needs a reachable AGS. Re-using the freed lock relies on the re-spawn below **plus the
§3.3 timer** as the real backstop (the space trigger is edge-based and won't re-fire).

Corrections applied after review (all three were CRITICAL false-kill or fail-open vectors):
- **Heartbeat on tmpfs, not the SD root.** The SD fills from logs during the
  incident (HAM-112/113); a heartbeat that can't be written would make a healthy scrub look
  hung. tmpfs stays writable when the SD is full.

  > **Superseded 2026-08-13 — the tmpfs must be `/run/hamma`, not `/dev/shm`.**
  > systemd-logind's `RemoveIPC=yes` (the compiled-in default, left commented in
  > `logind.conf`) deletes every object in `/dev/shm` owned by a user when that user's
  > last login session ends. The heartbeat, the MJ-scan cache and the drive-target latch
  > are all written by `pi`, so a routine `ssh pi@sensor` logout wiped all three. On mj05
  > this made every post-logout scrub run cold — a >1000 s re-read of 101k files against
  > 4 s warm — *and* left it with no heartbeat, so `check_scrub_health` paged the working
  > scrub as hung. `/run` is tmpfs, still cleared on reboot, and logind does not touch it.
  > Provisioned pi-owned by `files/tmpfiles-hamma.conf`.
- **Detection by heartbeat *advancement* in the monitor's own `monotonic` clock**, never the
  scrub's wall-clock `timestamp`. Immune to (a) sensor clock skew / NTP steps (a future
  timestamp no longer disables detection; a backward step no longer false-kills) and (b) a
  stale heartbeat left by a *previous* run (grace is counted from when the monitor first sees
  the lock held, so a fresh scrub is never judged against an old file).
- **Re-spawn after a successful kill** so the freed lock is used (§3.3 timer is the backstop).
- Kill-path test hardening: assert the process **group** (not the pid) is `SIGKILL`ed, and
  that the PID-reuse guard actually guards (both mutations now caught); `_pid_is_scrub`
  matches `hamma_scrub.py`, not a loose substring; `write_status` atomicity asserted by
  mechanism (temp + `os.replace`).

**Deployment gate:** `scrub_auto_recover=true` is defensible now that the false-kill vectors
are closed, but it kills processes — validate on a bench/idle unit (inject a real hung scrub)
before enabling fleet-wide. **Ships off; enabling is tracked in
[hamma-dev/mjolnir-hamma#81](https://github.com/hamma-dev/mjolnir-hamma/issues/81).** The durable `scrub_log` on the SD still self-disables (→DEVNULL)
under a full SD; accepted (degrades to old behavior; the tmpfs heartbeat is the load-bearing
signal).

## 7. Deployment notes (branch-only; nothing deployed)

This whole effort lives on `feature/scrub-resilience` and is **not deployed anywhere**.
When it does roll out, two things do **not** happen automatically and must be in the runbook:

1. **The systemd timer only auto-installs on a fresh `unified_install` run.** The install step
   (`unified_install/lib/brokkr.sh` "Config 5/5") copies the units and `enable --now
   hamma-scrub.timer`. **Already-deployed units get nothing from a `git pull`** — the timer,
   `.service`, and `.sh` wrapper must be copied and enabled by hand (same class of gap as the
   DNS/40-eth0 redeploy). Until then those units still rely solely on the level-triggered
   spawn from `check_sensor_drive`.
2. **Clean the per-unit `low_space` override before pushing config.** mj54 carries a local
   `low_space=10` (sensor-log#41). The new plugin **accepts + warns + ignores** `low_space`
   (so it will not crash the pipeline, C1), but the override is now dead config — replace it
   with per-unit `purge_space`/`alert_space` in `~/.config/brokkr/hamma/*.toml` so the unit
   actually drains at the intended point instead of silently falling back to the fleet default.
3. **`scrub_auto_recover` ships `false`.** The SIGKILL self-heal stays off until it is validated
   on a bench/idle unit by injecting a genuinely hung scrub (§3.2 deployment gate). Flip to
   `true` per-unit only after that.
4. **`scrub_metrics.csv` is self-capped, not logrotate-managed.** The per-run cache-metrics CSV
   (`~/brokkr/hamma/log/scrub_metrics.csv`) has no external rotation, so `write_scan_metrics`
   size-caps it in place (rolls to `.1` past `SCAN_METRICS_MAX_BYTES=1MB`, one generation). It
   is on the SD but bounded — no HAM-112/113 fill vector. No install/config change needed;
   both spawn paths run as `pi` so `~` resolves to `/home/pi`.

---

## Changelog

- **v4.2 (PR #82 review round + AGS-side priority):** Addressed jcburchfield's two review
  gaps — (1) the post-kill auto-recover respawn now routes through the cooldown gate
  (`_maybe_spawn_scrub`) so a re-hang can't drive an unbounded kill/respawn cycle; (2) added a
  final purge heartbeat so the last chunk's deletions reach the durable status before `done`.
  Then, per Phillip: made the scrub a **lower CPU priority than the DAS on the AGS itself**.
  Fleet check (mj05 AGS) found `mq-deadline` (ionice inert) with the DAS at CPU `nice 19` and
  unniced remote commands at `nice 0` — so the scrub was *outranking* the writer. Wrapped the
  heavy AGS-side commands in `nice -n 19` (`AGS_NICE`); verified `nice -n 19` runs over the
  scrub's SSH path on real hardware. ionice deliberately not used (no effect under mq-deadline).
- **v4.1 (red-team pass on the v4 follow-ups):** A 3-agent adversarial review caught that
  `_refresh_cache_dirs` was fed `recover_triggers`' **relative** `target_path` while the cache
  is keyed by **absolute** dirs — so the refresh silently no-op'd (feature did nothing), and the
  original tests missed it by passing absolute paths. Fixed (rejoin `mj_path`) + added a run()-
  level regression test driving the real relative output. Also: the metrics CSV claimed to be
  "rotated by log tooling" but nothing rotates it — added an in-place size cap
  (`SCAN_METRICS_MAX_BYTES`, roll to `.1`) and a test; added a test asserting `run()` actually
  calls `write_scan_metrics` (the fixtures mocked it without asserting, hiding wiring regressions).
  Root-vs-pi home-dir concern cleared (both spawn paths run as `pi`).
- **v4 (mj05 hardware validation + cache hardening):** Ran the scrubber on mj05
  against real `/ags/data` (AGS stopped): recovered to `DATA56`, purged 78 files,
  freed ~72GB, retained the 1 corrupt + active file (recover-before-delete held).
  Resilience features confirmed live: bounded 5.6s AGS scan, honest logging,
  tmpfs heartbeat advancing. Incremental MJ-scan cache proven at scale —
  **174× speedup** (1587s cold → 9.1s warm, 1191/1193 dirs cached on 1193 dirs);
  an initial "cache never hits" report was chased to ground and shown to be
  *not* a bug (RemoveIPC is off, no cleaner exists, aged cache still hits).
  Two follow-ups landed: (1) `_refresh_cache_dirs()` refreshes the cache entries
  for dirs that recovery wrote into (the cache is saved mid-scan, pre-recover, so
  those dirs were re-read next run); (2) `write_scan_metrics()` appends a durable
  per-run CSV (`~/brokkr/hamma/log/scrub_metrics.csv`: dirs_cached/total, cold
  flag, scan_seconds, recovered, purged) so cache hit-rate is reviewable over
  time and a real-world cold scan leaves a trail. `scrub_auto_recover` still off
  (#81 unchanged).
- **v3 (final capstone pass):** Closed the deploy/coverage gaps from the two integration/
  completeness reviews. Code: `low_space` now accepted-and-ignored (won't crash mj54's
  override, C1); `scrub_auto_recover` default flipped to **false** (bench-gate, M1); added a
  telemetry-staleness watchdog (`hs_stale_cycles`) so a *dark* AGS — `bytes_remaining` NA —
  alerts instead of silently going unmonitored (GAP2); `purge_ags_files` now writes a
  per-chunk heartbeat so a long purge can't be misread as hung (GAP1). Docs: corrected the
  Layer-3 "futile-scrub alert" claim (stuck≠futile; operator half is `alert_space`, GAP3);
  added the HAM-112/113 *hard dependency* for durable forensics (§6.7, GAP5); added this
  Deployment-notes section (timer not auto-installed on existing units + mj54 cleanup, M2).
  All 258 scrub-suite tests green.
- **v2 (post red-team):** Re-ranked §3 — silent-failure-mode elimination (§3.1) and
  observability (§3.2) promoted above the timer (§3.3), which is demoted to steady-state
  drain and no longer called the "backbone." Downgraded "the trigger fired" → "should have
  fired / may have silently no-op'd" (§1.1) and "no purge, proved" → "strongly indicated,
  rate-dependent" (§1.2). Corrected the fast-scrub claims (§3.5): batching-vs-multiplexing
  credit, self-disables on sick AGS, 0-byte throwaway files, recover unmeasured, scan-
  contention a hypothesis. Noted the two-threshold layer is blind in the failure mode and
  would not have prevented mj05 (§3.4). Added the ControlMaster socket/`/tmp` risks (§6.5).
- **v1:** timer-first design (superseded).
