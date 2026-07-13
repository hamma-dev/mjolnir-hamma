# AGS scrub resilience — design

**Scope:** `mjolnir-hamma` only (`plugins/state_monitor.py`, `scripts/hamma_scrub.py`,
`config/main.toml`, a new systemd timer unit). No brokkr/sindri changes.

**This spec is about one thing: keeping the AGS-pi drive (`/ags/data`) clean.** The scrub's
job is to offload triggers from `/ags/data` to the mj-pi and purge what's confirmed.

**Non-goals (explicitly out of scope):**
- **mj-pi `/media/pi/DATA??` drive fullness.** A full MJ recovery drive is a *different*
  problem with a *different* remedy (rotate/replace the drive), not something the scrub
  fixes. It matters here only as a *precondition* (recover needs somewhere to land — §3.3);
  monitoring/alerting on it belongs in a separate MJ-drive-management effort, not here.
  (See the DATA55 observation in §1 — a real standing issue, filed separately.)
- The AGS reboot loop / VL805 USB wedge (hardware; fixed by cold power-cycle).

**Status:** design. Supersedes the direction of PR #78 (scrub-trigger-fix) and PR #80
(disk-safety-monitors) — their useful pieces are folded in below; their framing (an
edge-vs-level trigger fix) is not the root cause.

---

## 1. What actually happened (mj05, Jul 9–13 2026)

Reconstructed from the telemetry CSVs and recovered-file forensics on mj05. `/ags/data`
is the AGS USB drive (462 GB); `bytes_remaining` (H&S "Disk Remaining") is its free space.

| Time (UTC) | Evidence | State |
|---|---|---|
| Jul 9 20:56 | telemetry 100.22 → 99.54 (clean, no NA) | free crosses `low_space`=100 GB |
| Jul 9 20:56 → Jul 10 17:59 | **monotonic** 100 → 0, no upward tick | 21 h fill, **no purge freed a byte** |
| entire fill window | **zero `*_recovered.bin` written** | scrub silent |
| Jul 10 18:00 → Jul 11 03:34 | telemetry pinned at 0 | drive FULL ~10 h, data loss |
| Jul 11 03:34 | first & only recovered-file cluster (5 files) | one scrub, ~31 h late |
| Jul 11 ~04:20 → dark | free reads frozen 448.96 then NA; ping flaps | AGS drive drops → reboot loop |
| Jul 13 ~11:48 | cold power-cycle | VL805 USB firmware reloads, recovers |

Causal chain (settled): **`/ags/data` full → AGS reboot cycle → USB/FPGA (VL805) wedged →
persistent reboots** until the operator stopped AGS (~Jul 11 12:00) and cold-cycled Jul 13.
The reboot/USB half is understood and out of scope here.

### What we proved about the scrubber

1. **The trigger fired.** In the deployed code (`0.4.x @ 426ff2b`), `check_sensor_drive`
   does `if (space_now < low_space) and (space_pre >= low_space): self._spawn_scrub()`.
   At the crossing that is `(99.54 < 100) and (100.22 >= 100)` → **True**. The scrub
   spawned. The failure is **downstream of the trigger** — this is why an edge-vs-level
   trigger change (PR #78) does not address the root cause.
2. **The spawned scrub freed nothing.** `bytes_remaining` fell 100 → 0 *monotonically*.
   Any successful purge would show sawtooth; there is none. No purge freed space in 21 h.
3. **It had somewhere to write.** DATA56 had 851 GB free throughout (the Jul 11 recovery
   landed there). "No landing zone" is ruled out *for this incident*.
4. **Why the pass failed is unrecoverable.** The scrub's output was `DEVNULL`'d, and the
   journal plus all 10 rotated `brokkr_hamma_005.log.*` files were overwritten within
   **7 minutes** by reboot-loop error spam (HAM-112). No trace survives.

### What the current system tells us (measured Jul 13, healthy)

- A full dry-run scrub (`--recover --purge -n --since auto`) completes in **21.5 s**, of
  which **19.8 s is the MJ header scan** (40,565 files); it correctly finds 1 missing
  trigger, 7 purgeable files. **The code path works** — the Jul 9–11 failure was
  **condition-dependent** (sustained storm + a degrading AGS), not an always-broken scrub.
- **Throughput is the constraint.** `recover` issues one `ssh "dd…"` **per trigger**;
  `purge` issues one `ssh "rm…"` **per file** — sequential, plain SSH, no batching, no
  persistent connection. Measured round-trip to the AGS: **0.29 s each plain vs 0.018 s
  with `ControlMaster`** (16×). Purging ~444 files ≈ **2 min of SSH overhead on a healthy
  AGS**, far worse on a degrading one. `select_target_drive` is also re-called *per
  trigger*, each time `os.listdir`-ing all 904 DATA55 dirs.
- **The recovery landing zone was fine.** DATA56 had 851 GB free throughout — "no landing
  zone" is ruled out as a cause here. (Aside, out of scope: DATA55 has been 100 % full
  since June 1, unmonitored — a real standing mj-pi issue to file separately, not something
  this scrub spec addresses.)
- **The trigger telemetry failed exactly when needed.** As the AGS degraded,
  `bytes_remaining` went to 0/NA. Any purge decision that *reads* `bytes_remaining` is
  blind in precisely this state.

---

## 2. Root cause

Not a single bug. The safety net is **reactive, single-shot, blind, and throughput-
limited**, and it depends on a telemetry signal that fails under the very condition it
must handle:

- **Reactive + single-shot:** it fires only on the downward `low_space` *crossing*. By the
  time free space crosses the threshold you are already behind, and the trigger gives
  exactly one attempt — no retry while space stays low.
- **Blind:** the scrub runs with `DEVNULL`'d output; nobody can see whether it ran, hung on
  the `flock`, crashed, or purged nothing. The one incident where we needed the record, the
  logs were destroyed within minutes.
- **Throughput-limited:** per-item sequential SSH means a heavy scrub can take minutes to
  tens of minutes — potentially longer than the fill window it is racing.
- **Telemetry-dependent:** the trigger reads `bytes_remaining`, which went NA as the AGS
  degraded.

At max trigger rate (≈500 triggers ≈ 100 GB in ~8 min ⇒ ~12.5 GB/min) a slow, single-shot,
blind scrub cannot keep pace. At the *observed* incident rate (~5 GB/hr) it could have —
had it simply **kept running**.

---

## 3. Design

Four changes, layered. The first is the backbone; the rest harden it.

### 3.1 Timer-driven periodic scrub (backbone)

Run the scrub on a **systemd timer (~15 min)**, independent of `bytes_remaining`.

- **Why a timer, not the low-space trigger:** it is **telemetry-independent** — it runs
  whether or not `bytes_remaining` is reporting, which is the failure that broke this
  incident. It keeps `/ags/data` drained in steady state so free space never *approaches*
  the threshold under normal storms.
- **Write-cost is not a concern** (evaluated): the scrub is read-dominated. The MJ scan is
  pure reads (`relatime` vfat ⇒ no atime writes within a day); `recover` writes only
  genuinely-missing triggers (~0–1/run in steady state); `purge` is KB-scale vfat metadata
  deletes. Against the ~120 GB/day these drives already log 24/7, the timer adds **< 1 %**
  write load — a rounding error on a 2 TB SSD's ~600–1200 TBW. Add `noatime` to be safe.
- **The real timer cost is I/O contention**, not wear: a 20 s full scan every 15 min
  competes with the write pipeline for USB bandwidth. Mitigated by the incremental scan
  (§3.3).
- Implementation: a `hamma-scrub.timer` + `hamma-scrub.service` (oneshot) running the
  scrub as `pi`. Keep the existing `flock -n /tmp/hamma_scrub.lock` so timer and any
  threshold-driven run can never overlap.

### 3.2 Two-threshold escalation (on top of the timer, when telemetry is available)

Split the single `low_space` into a **purge** threshold and a higher-urgency **alert**
threshold, both level-evaluated on the 60 s monitor cycle:

- **Purge at `xx = 200 GB` free** (higher): while free < `xx`, run the scrub every cooldown
  (level-triggered, *not* a one-shot edge). This is proactive draining that starts well
  before the situation is critical and is more responsive (every few minutes) than the
  15-min timer. **No alert** — this is normal "working hard."
- **Alert at `yy = 75 GB` free** (lower): fire a distinct notification only if free space
  punches *through* `xx` down to `yy` **despite** the purging. That means **purge is
  losing** — the safety net is failing and a human is needed. Debounced (alert once on
  entry; re-arm when free climbs back above `yy`). If space stabilizes above `yy`, silence.

The `xx → yy` band (200 → 75 GB = 125 GB ≈ 10 min of max-rate runway) is the window where
purge gets to prove it can keep up before anyone is paged.

**Parameter justification** (drive 462 GB; max fill 12.5 GB/min; fast scrub target 1–2 min):

| Knob | Value | Rationale |
|---|---|---|
| timer interval | 15 min | steady-state drain; at observed ~5 GB/hr only ~1.25 GB accrues/interval |
| `xx` purge start | 200 GB free | ~16 min max-rate runway to empty; comfortably above a fast scrub |
| `yy` alert | 75 GB free | ~6 min max-rate runway left when paged — urgent but actionable; at observed rate ~15 h |

At **max** rate the 15-min timer alone is too coarse (187 GB can accrue between ticks) —
that is exactly the regime the level-triggered `xx` purge covers between timer ticks. At
**observed** rate the timer alone suffices. The two mechanisms cover different regimes.

### 3.3 Make the scrub fast (so it can survive surges)

**Measured (mj05 → AGS, Jul 13):** per-file purge baseline **~100 s / 500 files** (plain
per-op SSH); **batched over one `ControlMaster` connection: 0.163 s / ~480 files** (4
chunked `rm` calls) — a **~600×** speedup. Purge ceases to be a bottleneck; a full scrub's
cost collapses onto the MJ scan (~20 s), which the incremental scan below then attacks. A
fast scrub of ~20–30 s against the 200 GB purge start leaves ~16 min of max-rate runway.

**Prototype validated on-sensor (mj05, Jul 13, Python 3.7.3, real files):** the implemented
`open_control_master` + batched `purge_ags_files` deleted 230 throwaway AGS files in
**0.117 s** vs a **~63 s** plain-per-file baseline (~500×), confirmed the drive empty after,
and tore the master down cleanly. See `perf(scrub)` commit; 26 unit tests.

**Scan-cost-under-load finding (important, drives the incremental scan):** the same day,
under active AGS recording, a full scrub's **AGS scan alone rose from ~1 s to 99 s** (9 →
35 files) and the MJ scan stretched to minutes — pure USB read/write contention with the
live recording. Once purge is ~free, the scan is *the* bottleneck, and it inflates exactly
when the system is busiest. The incremental scan (below) is therefore not optional polish —
it is the load-bearing piece for keeping a periodic scrub cheap.


- **Persistent SSH** to the AGS via `ControlMaster`/`ControlPersist` (one connection reused
  for all `dd`/`rm`): measured **16× per-op speedup** (0.29 s → 0.018 s). Add
  `-o BatchMode=yes -o ConnectTimeout=10` so a sick AGS fails fast instead of hanging.
- **Batch the deletes:** one `ssh "rm f1 f2 … fN"` (chunked) instead of N round-trips.
- **Fix `select_target_drive`:** compute the target **once per run**, not per trigger
  (drop the per-trigger `os.listdir` over 904 dirs).
- **Incremental MJ scan:** cache the confirmed-header set keyed by hourly dir; on each run
  scan only new/changed hours. Cuts the 20 s scan to near-zero in steady state, removing
  the I/O-contention cost of a frequent timer.
- **Tighten timeouts:** the 3600 s scan cap lets one futile pass hold the lock for an hour;
  reduce to minutes now that SSH is fast and fails fast.

### 3.4 Observability + failure detection (so the next incident is diagnosable)

- **Persistent, rotation-safe scrub log** (`scrub_log`, already added in PR #78): capture
  every run's scan/recover/purge **counts, per-phase timings, errors, and lock-acquisition
  result**. Never `DEVNULL`.
- **Futile-scrub alert:** if consecutive runs purge 0 files while free space keeps falling,
  alert ("scrub running but not freeing space"). This is the symptom the 21-h silent ramp
  never surfaced.
- **Stuck-lock detection:** if `flock` fails to acquire for N consecutive runs, alert (a
  prior scrub is hung).

---

## 4. What this supersedes

| Prior work | Disposition |
|---|---|
| PR #78 edge→level trigger + cooldown | **Reframed.** Level-triggering survives as the `xx` purge behavior, but as *part of* the two-threshold + timer design, not as "the fix." The trigger was never the failure. |
| PR #78 `scrub_log` | **Kept** — it is the one directly useful piece (§3.4). |
| PR #80 Layer 1 `check_recovery_drives` | **Out of scope** — mj-pi `DATA??` fullness is a separate MJ-drive-management issue, not part of keeping `/ags/data` clean. Belongs in its own effort. |
| PR #80 Layer 2 H&S staleness watchdog | **Folded in** — the timer's telemetry-independence is the structural answer; a staleness alert remains useful as a cheap signal. |
| PR #80 Layer 3 futile-scrub alert | **Kept** (§3.4). |

---

## 5. Test plan

- **Unit (pytest):** two-threshold state machine (purge below `xx`, alert crossing `yy`,
  debounce/re-arm, no alert while `yy < free < xx`); `select_target_drive` called once;
  batched-delete command construction; incremental-scan cache hit/miss; futile/stuck
  detection latches.
- **Scrub-script:** ControlMaster path (mock ssh), batched `rm` chunking, timeout/fail-fast
  behavior, dry-run still deletes nothing.
- **Integration (on a bench/idle unit):** timer fires and completes; a forced near-full
  condition drives purge below `xx`, alert at `yy`, recovery re-arms; measure real scrub
  wall-clock with ControlMaster+batch vs baseline.
- **Load reproduction:** near-full `/ags/data` + synthetic sustained influx, instrumentation
  on — confirm the scrub keeps pace or that the futile alert fires. This is the test the
  original incident lacked.

---

## 6. Open questions / risks

1. **Fast-scrub wall-clock under load** is still to be *measured*, not assumed — §5 load
   test sets `xx`/timer definitively. Values in §3.2 are starting points.
2. **Incremental-scan cache invalidation** (compression rewrites files, udisks suffixed
   mounts `DATA071`) — must fall back to a full scan on cache miss/anomaly.
3. **Timer + brokkr write contention** during a real storm — the incremental scan should
   remove most of it; verify under load.
4. **`bytes_remaining` = `/ags/data` free** is established empirically here (fills to 0,
   scrub targets the same drive, 448.96 GB ceiling ≈ 462 GB drive). This **contradicts an
   older internal note** claiming it is *not* the AGS drive — that note should be corrected.
5. Per-unit variation (mj05 is `nochargecontroller`, no ttyUSB; PAMMA units differ) — keep
   thresholds configurable per unit.
