# Deadman Script Hardening — Consolidated Findings

## Context
`scripts/migration-deadman.sh` is the last line of defense for remote Raspberry Pi sensor migrations in Australia. If anything goes wrong, it must restore the sensor to its pre-migration state. Four expert reviews (bash, systemd, networking, reliability) identified the following issues.

---

## Findings by Severity

### P0 — Defuse reports "DEFUSED" but rollback still completes and reboots

**Source:** Systemd expert
**Lines:** 274, 348, 352

The `TimeoutStopSec=infinity` on the service (line 274) combined with the rollback script's `trap '' TERM` (line 190) means `systemctl stop` can never actually kill the rollback. The `timeout 10` wrapper on line 348 only kills the `systemctl` *client process*, not the server-side stop job.

The re-check on line 352 looks for `ActiveState == "activating"`, but after the failed stop attempt, the state is `deactivating` — which doesn't match. So defuse prints "DEFUSED", removes unit files, and exits. Meanwhile the rollback script is still running, restores all files, and reboots.

**Impact:** Operator believes defuse succeeded. System reboots to rolled-back state anyway.

**Fix:** The re-check on line 352 must also match `deactivating` state. Additionally, stop the timer *before* the service (line 361 should come before line 348) to prevent the timer from re-triggering between checks.

---

### P0 — Read-only filesystem causes infinite reboot loop

**Source:** Reliability expert
**Lines:** 204, 233, 247, 252

If the SD card goes read-only (the most common Pi SD card failure mode), the rollback script runs but:
- `tar xf` fails (can't write files)
- `rm -rf "$BACKUP_DIR"` fails (can't delete)
- `systemctl restart systemd-networkd` runs (disrupts network with un-restored configs)
- `reboot` fires

On next boot, `ConditionPathExists` passes (backup dir still exists because `rm -rf` failed), so the rollback fires again. **Infinite reboot loop with network disruption on every cycle.**

The `ConditionPathExists` loop-breaker only works if the filesystem is writable.

**Impact:** Sensor bricked in infinite reboot loop.

**Fix:** At the top of `rollback.sh`, test writability (e.g., `touch /var/lib/.rw-test && rm -f /var/lib/.rw-test`). If read-only, skip everything except reboot. Add a reboot attempt counter to prevent infinite loops.

---

### P1 — Partial tar extraction leaves inconsistent config set

**Source:** Reliability expert
**Lines:** 200-210

A tar with a valid header can pass `tar tf` but fail mid-extraction (bad sectors developed since arm time). Result: first 3 of 7 files restored, rest still have new configs. After reboot, networkd loads a mix of old and new configs. On a cellular sensor this could mean no route to the internet.

The script logs the error (line 209) but continues to restart networkd and reboot with the broken config set.

**Impact:** Sensor unreachable with frankenstein config, rollback made things worse.

**Fix:** After `tar xf`, verify at least the critical network files (`40-eth0.network`, `20-wwan0.network`, `wwan-check.sh`) exist and are non-empty before rebooting.

---

### P1 — SIGPIPE can kill the rollback script mid-execution

**Source:** My initial review + Bash expert (implied)
**Lines:** 190

The rollback script traps `TERM INT HUP` but not `PIPE`. If `logger` writes to a dead syslog socket (journal crashed, disk full, early boot), SIGPIPE kills the rollback. Everything after that point — networkd restart, unit cleanup, backup dir deletion, reboot — never runs.

**Impact:** Partial rollback. Sensor left in inconsistent state with no reboot.

**Fix:** Add `PIPE` to the trap: `trap '' TERM INT HUP PIPE`

---

### P1 — Arithmetic overflow can produce a zero-second timer

**Source:** Bash expert
**Lines:** 98, 281

The regex `^[0-9]+$` allows arbitrarily large numbers. On bash 4.x (Raspbian Buster), `[[ 99999999999999999999 -gt 1440 ]]` can wrap to negative, passing the check. Then `$((minutes * 60))` overflows to zero or negative, producing `OnActiveSec=0` — the timer fires immediately.

**Impact:** Fat-fingered `arm 99999999999999999999` triggers instant rollback + reboot.

**Fix:** Constrain the regex to `^[0-9]{1,4}$` (max 4 digits).

---

### P1 — No `sync` after tar extraction in rollback

**Source:** My initial review
**Lines:** 204, 233, 250

After `tar xf` restores files (line 204), the script immediately restarts `systemd-networkd` (line 233). The `sync` doesn't come until line 250, 46 lines and several service restarts later. On a Pi with an SD card, a power glitch in that window could leave restored files half-written.

**Impact:** Corrupted network configs after power loss during rollback.

**Fix:** Add `sync` immediately after the tar extraction block (after line 211).

---

### P2 — No `daemon-reload` after removing deadman units in rollback

**Source:** My initial review
**Lines:** 242-243

The rollback deletes `/etc/systemd/system/deadman-rollback.{timer,service}` but never does `daemon-reload`. If the 3-tier reboot chain fails (unlikely but this is "bulletproof" territory), systemd still has the units cached in memory.

**Fix:** Add `systemctl daemon-reload 2>/dev/null || true` after line 243.

---

### P2 — `tar xf` without `--unlink-first` fails on file-type changes

**Source:** Bash expert
**Line:** 204

If `install.sh` replaces a file with a directory (or vice versa) between arm and rollback, `tar xf` fails on that entry. The exit code is checked (lines 205-210), but the file is not restored.

**Fix:** Add `--unlink-first` to the tar extract command.

---

### P2 — No persistent rollback log file

**Source:** My initial review
**Lines:** 195-258

Rollback only logs via `logger` (syslog). If journald isn't running, there's zero forensic trail. The backup dir is deleted at line 247, so a log there would also be lost.

**Fix:** Add a `tee -a /var/log/deadman-rollback.log` alongside logger calls, or redirect the entire rollback script's output to a persistent log.

---

### P2 — No error check on `chmod +x` of rollback script

**Source:** Bash expert
**Line:** 260

If `chmod +x` fails (e.g., read-only filesystem remount race), the arm succeeds but the rollback script is not executable. The systemd service will fail to run it. The deadman is "armed" but broken.

**Fix:** Check the return code and abort arm if it fails.

---

### P2 — Systemd unit file writes not verified

**Source:** Reliability expert
**Lines:** 263-275, 282-291

The `cat > /etc/systemd/system/...` heredocs can fail silently if `/etc` is full. The script checks `systemctl daemon-reload` and `enable`, but those can succeed with stale cached state. The backup dir exists, operator thinks the deadman is armed, but the timer unit was never written.

**Fix:** After writing each unit file, verify the file exists and is non-empty before proceeding.

---

### P3 — Defuse stops service before timer (ordering)

**Source:** Systemd expert
**Lines:** 348, 361

The timer could re-trigger the service between stopping the service (line 348) and stopping the timer (line 361). Reversing the order (stop timer first, then service) closes this window.

---

### P3 — Double tar read on degraded SD card

**Source:** Bash expert
**Line:** 203

`logger -t "$LOG_TAG" "Restoring files: $(tar tf ...)"` reads the entire tar file *before* the actual extraction. On a degraded SD card, this doubles I/O and slows rollback.

---

### Design Limitations (not bugs, but document explicitly)

1. **OnActiveSec resets on every reboot** — A reboot loop faster than the timer interval prevents rollback from ever firing. The kill-switch cron (4h) vs timer (30min) makes this unlikely, but a kernel panic loop could trigger it.

2. **Rollback deletes backup before confirming reboot** (line 247) — If all 3 reboot methods fail, the system has no backup data. This is an intentional tradeoff to prevent infinite rollback loops.

3. **Pip packages / brokkr not rolled back** — By design (migration uses `--skip-*` flags), but worth verifying the actual flags don't touch anything outside the backup list.

---

## Summary Table

| # | Sev | Lines | Issue |
|---|-----|-------|-------|
| 1 | **P0** | 274, 348, 352 | Defuse says "DEFUSED" but rollback runs and reboots anyway |
| 2 | **P0** | 204, 247, 252 | Read-only filesystem → infinite reboot loop |
| 3 | **P1** | 190 | SIGPIPE kills rollback mid-execution |
| 4 | **P1** | 98, 281 | Arithmetic overflow → zero-second timer |
| 5 | **P1** | 204, 250 | No sync after tar extraction |
| 6 | **P1** | 200-210 | Partial tar extraction → inconsistent config set |
| 7 | P2 | 242-243 | No daemon-reload after unit deletion in rollback |
| 8 | P2 | 204 | tar xf without --unlink-first |
| 9 | P2 | 195-258 | No persistent log file |
| 10 | P2 | 260 | No error check on chmod +x |
| 11 | P2 | 263-291 | Systemd unit file writes not verified |
| 12 | P3 | 348, 361 | Timer/service stop ordering in defuse |
| 13 | P3 | 203 | Double tar read on degraded SD |
