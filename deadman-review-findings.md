# Deadman Script Review — Round 3 (Post-Patch)

## Context

The deadman script (`scripts/migration-deadman.sh`) was patched after Round 2 review (commit `70a5557`). Round 3 dispatched four independent expert reviewers:

1. **Rollback script logic** — focused on the rollback heredoc (lines 183-315)
2. **Arm/defuse/concurrency** — focused on arm(), defuse(), flock, cleanup
3. **Systemd unit correctness** — focused on timer/service units and systemd 241 behavior
4. **Hostile environment edge cases** — focused on degraded hardware scenarios (hit rate limit, no results)

**What prior rounds fixed:** SIGPIPE trap, arithmetic overflow regex, sync after tar, daemon-reload after unit deletion, `--unlink-first`, persistent log file, chmod +x error check, unit file write verification, defuse stop ordering, defuse `deactivating` state check, RO FS detection + attempt counter, both `/var/lib` and `/etc` writability test, atomic attempt counter write, rollback script deletion ordering, exit after reboot cascade, rollback script write verification, leading-zero octal rejection, non-numeric attempt counter sanitization.

---

## Round 3 Findings

### RESOLVED from Round 2 (confirmed fixed)

| Old # | Issue | Fix verified |
|-------|-------|-------------|
| R2-1 (P1) | RO test only checks `/var/lib`, not `/etc` | Both tested (line 238) |
| R2-2 (P1) | Attempt counter non-atomic write | `echo > .tmp && mv` pattern (line 253) |
| R2-3 (P1) | `--unlink-first` + power loss tradeoff | Documented in comment (line 291) |
| R2-4 (P2) | No exit after reboot cascade | `exit 1` before ROLLBACK_EOF (line 353) |
| R2-5 (P2) | Partial `rm -rf` re-arms deadman | `rm -f rollback.sh` first (line 338) |

---

### NEW issues found in Round 3

#### R3-1 (P1) — `arm()` doesn't check if rollback service is running

**Source:** Arm/defuse/concurrency reviewer

flock prevents concurrent `migration-deadman.sh` invocations, but the rollback is a separate process launched by systemd — it does NOT hold the flock. If the timer fires and a user runs `arm` again:
1. flock succeeds (nobody else holds it)
2. `is-active` on the timer returns false (already fired)
3. arm() overwrites `BACKUP_DIR` while the rollback script is actively reading from it

**Impact:** Corrupted file restore — the Pi could end up with mixed old/new config files.

**Fix:** Add `ActiveState` check (same as defuse) and unit-file-existence check to arm(). **FIXED.**

---

#### R3-2 (P1) — `logger` can hang indefinitely in rollback

**Source:** Rollback logic reviewer

Every `log()` call uses `logger` which communicates with journald over `/dev/log`. If journald is stuck (disk full, etc.), `logger` blocks forever. The PIPE trap covers broken-pipe, but not a blocked socket. The rollback script hangs and never reaches reboot.

**Impact:** System stuck in half-rolled-back state. Kill switch reboots in 4h, but the same hang recurs.

**Fix:** `timeout 5 logger ...` in the `log()` function. **FIXED.**

---

#### R3-3 (P1) — RO_FS infinite reboot loop (deeper than R2 fix)

**Source:** Rollback logic reviewer

Round 2 fixed the initial RO detection (testing both `/var/lib` and `/etc`). But the deeper issue: when `RO_FS=1`, the script falls through to cleanup code (disable timer, delete unit files, delete backup dir) that ALL silently fails on a read-only filesystem. ConditionPathExists still passes on next boot, timer fires again, and the cycle repeats forever. The attempt counter is in the `else` (writable) branch, so it never increments. The RO_FS path has no loop-breaker.

**Impact:** Infinite reboot loop on any sensor with a read-only SD card. No escape without physical access.

**Fix:** When RO_FS is detected: (1) try `mount -o remount,rw /`, (2) if that succeeds, continue normally, (3) if remount fails, sleep 5 minutes and reboot (throttles the loop). Jump straight to reboot instead of falling through to cleanup that can't work. **FIXED.**

---

#### R3-4 (P2) — `is-active` on timer can false-negative during early boot

**Source:** Arm/defuse/concurrency reviewer

After reboot, there's a brief window during early boot where the timer may not yet be active (systemd is still processing `timers.target`). If the user runs `arm` during this window, the check passes and arm() overwrites the backup with post-migration state.

**Fix:** Secondary guard: also check for existence of unit files on disk. **FIXED.**

---

#### R3-5 (P3) — `cleanup_partial_arm` gets wrong exit code when invoked as trap handler

**Source:** Arm/defuse/concurrency reviewer

When bash invokes `cleanup_partial_arm` as a trap handler, `$1` is whatever the calling function had in its local `$1` (e.g., the `minutes` value). So `exit "${1:-130}"` exits with the minutes value instead of a signal-based exit code.

**Fix:** Per-signal trap declarations: `trap 'cleanup_partial_arm 130' INT`, etc. **FIXED.**

---

### Confirmed Correct (systemd reviewer — no issues found)

The systemd reviewer verified all design choices for systemd 241:
- `OnActiveSec` correctly re-fires after reboot via `WantedBy=timers.target`
- `ConditionPathExists` evaluated at service start time (correct)
- `TimeoutStartSec=infinity` + `TimeoutStopSec=infinity` both necessary
- `RemainAfterExit=yes` correctly omitted
- TOCTOU race in defuse correctly handled by re-check pattern
- Self-deletion of unit files during execution is safe on systemd 241
- Three-layer cleanup (disable, rm unit, rm rollback.sh) provides proper defense in depth
- `daemon-reload` at line 280 correctly precedes `wwan-check.timer` restart

---

### Design Limitations (unchanged)

1. **OnActiveSec resets on every reboot** — documented, acceptable given kill-switch cron timing.
2. **Rollback deletes backup before confirming reboot** — intentional tradeoff for loop prevention.
3. **Pip packages / brokkr not rolled back** — by design.
4. **`--unlink-first` tradeoff** — file-type-change protection vs power-loss risk. Documented.

---

## Cumulative Summary Table

| # | Sev | Issue | Status |
|---|-----|-------|--------|
| R1-1 | ~~P0~~ | Defuse "DEFUSED" lie (deactivating state) | **FIXED** (Round 5-6) |
| R1-2 | ~~P0~~ | Read-only FS infinite reboot | **FIXED** (Round 7) |
| R1-3 | ~~P1~~ | SIGPIPE kills rollback | **FIXED** (Round 7) |
| R1-4 | ~~P1~~ | Arithmetic overflow | **FIXED** (Round 7) |
| R1-5 | ~~P1~~ | No sync after tar extract | **FIXED** (Round 7) |
| R1-7–13 | ~~P2/P3~~ | All Round 1 P2/P3 items | **FIXED** (Round 7) |
| R2-1 | ~~P1~~ | RO test only checks `/var/lib` | **FIXED** (Round 8) |
| R2-2 | ~~P1~~ | Attempt counter non-atomic write | **FIXED** (Round 8) |
| R2-3 | ~~P1~~ | `--unlink-first` + power loss tradeoff | **Documented** (Round 8) |
| R2-4 | ~~P2~~ | No exit after reboot cascade | **FIXED** (Round 8) |
| R2-5 | ~~P2~~ | Partial `rm -rf` re-arms deadman | **FIXED** (Round 8) |
| R3-1 | ~~P1~~ | arm() doesn't check rollback service state | **FIXED** (Round 9) |
| R3-2 | ~~P1~~ | logger can hang indefinitely | **FIXED** (Round 9) |
| R3-3 | ~~P1~~ | RO_FS infinite reboot loop (deeper fix) | **FIXED** (Round 9) |
| R3-4 | ~~P2~~ | is-active false-negative during early boot | **FIXED** (Round 9) |
| R3-5 | ~~P3~~ | cleanup_partial_arm wrong exit code | **FIXED** (Round 9) |

## Review History

| Round | Reviewers | New P0/P1 found | Commit |
|-------|-----------|-----------------|--------|
| 5-6 | 4 internal | 2 P0, 4 P1 | `b211b1b` |
| 7 | 4 internal | 3 risks | `f4f134e` |
| 8 (independent R1) | 3 independent | 2 P0, 4 P1 | `384a5ac` |
| 8 (independent R2) | 3 independent | 2 P1 | `70a5557` |
| 9 | 4 independent (3 returned) | 3 P1 | (this commit) |
