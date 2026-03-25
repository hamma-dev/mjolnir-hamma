# Deadman Script Review — Round 2 (Post-Patch)

## Context

The deadman script (`scripts/migration-deadman.sh`) was patched after Round 1 review (commits `384a5ac`, `f4f134e`). Round 2 dispatched three fresh, independent expert reviewers (bash, systemd, reliability/SRE) against the **current** script to identify any remaining issues.

**What Round 1 fixes addressed:** SIGPIPE trap, arithmetic overflow regex, sync after tar, daemon-reload after unit deletion, `--unlink-first` on tar extract, persistent log file, chmod +x error check, unit file write verification, defuse stop ordering (timer before service), defuse `deactivating` state check, read-only FS detection + attempt counter.

---

## Round 2 Findings

### RESOLVED from Round 1 (confirmed fixed)

The following Round 1 issues were verified as fixed in the current script:

| Old # | Issue | Fix verified at line |
|-------|-------|---------------------|
| R1-1 (P0) | Defuse "DEFUSED" lie (deactivating state) | Line 388, 414: checks both `activating` and `deactivating` |
| R1-2 (P0) | Read-only FS infinite reboot | Lines 210-237: RO detection + attempt counter |
| R1-3 (P1) | SIGPIPE kills rollback | Line 192: `trap '' TERM INT HUP PIPE` |
| R1-4 (P1) | Arithmetic overflow | Line 99: regex `^[1-9][0-9]{0,3}$` |
| R1-5 (P1) | No sync after tar extract | Line 248: `sync` after tar xf |
| R1-7 (P2) | No daemon-reload after unit deletion | Line 287: `systemctl daemon-reload` |
| R1-8 (P2) | tar xf without --unlink-first | Line 246: `--unlink-first` present |
| R1-9 (P2) | No persistent log | Lines 196-201: `log()` writes to `/var/log/deadman-rollback.log` |
| R1-10 (P2) | No chmod +x error check | Line 304: `|| cleanup_partial_arm 1` |
| R1-11 (P2) | Unit file writes not verified | Lines 343-348: `-s` checks |
| R1-12 (P3) | Timer/service stop ordering | Lines 401-403: timer stopped first |

---

### NEW or REMAINING issues found in Round 2

#### P1 — Read-only FS detection only tests `/var/lib`, not `/etc`

**Source:** Reliability/SRE expert
**Line:** 212

The RO test does `touch /var/lib/.rw-test`. But the critical restored files live in `/etc/systemd/network/` and `/usr/local/bin/`. On an SD card with selective sector wear, `/var/lib` can be writable while `/etc` is read-only. The script thinks the filesystem is fine, attempts tar extraction to `/etc`, fails silently (tar returns non-zero, logged at line 252 as "continuing anyway"), and reboots with broken configs still in place.

**Impact:** Rollback provides zero recovery value. Sensor reboots into broken state.

**Fix:** Also test `/etc` writability: `touch /etc/.rw-test 2>/dev/null && rm -f /etc/.rw-test`.

---

#### P1 — Attempt counter can reset on power loss, enabling infinite loop

**Source:** Reliability/SRE expert
**Lines:** 225, 220-222

`echo "$attempts" > "$ATTEMPT_FILE"` (line 225) is not atomic. If power is lost mid-write, the file is corrupted. On next boot, line 222's sanitization (`[[ "$attempts" =~ ^[0-9]+$ ]] || attempts=0`) resets it to 0. The counter never reaches 3 if power keeps cutting during the write.

**Impact:** Infinite reboot loop that the attempt counter was designed to prevent.

**Fix:** Atomic write: `echo "$attempts" > "$ATTEMPT_FILE.tmp" && mv "$ATTEMPT_FILE.tmp" "$ATTEMPT_FILE"`. The `mv` is atomic on ext4 (the Pi's filesystem).

---

#### P1 — `--unlink-first` makes power-loss during tar extraction worse

**Source:** Reliability/SRE expert
**Line:** 246

`tar xf --unlink-first` deletes the existing file *before* extracting the replacement. If power is lost between the unlink and the write, the file is simply gone — neither old nor new version exists. Each power-loss-during-extraction cycle can incrementally destroy more files. By attempt #3, the counter gives up, and the sensor has missing critical network configs.

**Impact:** Incremental system destruction across power-loss retries.

**Note:** This is a tradeoff. Without `--unlink-first`, tar fails on file-type changes (Round 1 finding #8). With it, power loss is more destructive. The current choice is probably correct (file-type change is more likely than power loss during the ~0.1s extraction), but worth documenting.

---

#### P2 — No `exit` after reboot cascade in rollback script

**Source:** Bash expert
**Lines:** 296-302 (in rollback heredoc)

If all three reboot methods fail (`reboot`, `reboot -f`, `echo b > /proc/sysrq-trigger`), the script falls through to EOF and exits 0. systemd sees the service as "succeeded." The system continues running with restored files but no reboot, and the deadman won't fire again (ConditionPathExists will fail since backup dir was already deleted at line 291).

**Impact:** System runs without rebooting after rollback. Services may be in inconsistent state (old configs on disk, new configs in memory).

**Fix:** Add `sleep infinity` or `exit 1` after line 302, before `ROLLBACK_EOF`.

---

#### P2 — Backup dir `rm -rf` partial failure can re-arm the deadman

**Source:** Reliability/SRE expert
**Line:** 291

If power is lost during `rm -rf "$BACKUP_DIR"`, some files may be deleted (e.g., the attempt counter) while others remain. On reboot, ConditionPathExists passes (rollback script still exists), attempt counter is gone (resets to 0), and the deadman fires again from scratch.

**Impact:** Extra rollback cycles after supposedly successful rollback.

**Fix:** Delete the rollback script (`$ROLLBACK_SCRIPT`) *first* before `rm -rf`. Since `ConditionPathExists` checks specifically for the rollback script, deleting it first ensures the loop-breaker works even if `rm -rf` is interrupted.

---

#### P3 — Double tar read removed from Round 1 list but replaced by verbose logging

**Source:** Bash expert (Round 1 finding still partially relevant)
**Line:** 245

The Round 1 double-tar-read issue (logging `tar tf` output before extraction) was fixed — now it just logs "Restoring files from backup tar..." without a second tar read. Confirmed resolved.

---

### Design Limitations (unchanged from Round 1)

1. **OnActiveSec resets on every reboot** — documented, acceptable given kill-switch cron timing.
2. **Rollback deletes backup before confirming reboot** (line 291) — intentional tradeoff for loop prevention.
3. **Pip packages / brokkr not rolled back** — by design.
4. **`--unlink-first` tradeoff** — see P1 finding above. File-type-change protection vs power-loss risk.

---

## Summary Table

| # | Sev | Lines | Issue | Status |
|---|-----|-------|-------|--------|
| R1-1 | ~~P0~~ | 388, 414 | Defuse "DEFUSED" lie | **FIXED** |
| R1-2 | ~~P0~~ | 210-237 | Read-only FS infinite reboot | **FIXED** |
| R1-3 | ~~P1~~ | 192 | SIGPIPE kills rollback | **FIXED** |
| R1-4 | ~~P1~~ | 99 | Arithmetic overflow | **FIXED** |
| R1-5 | ~~P1~~ | 248 | No sync after tar extract | **FIXED** |
| R1-7–13 | ~~P2/P3~~ | various | All Round 1 P2/P3 items | **FIXED** |
| **R2-1** | **P1** | 212 | RO test only checks `/var/lib`, not `/etc` | **OPEN** |
| **R2-2** | **P1** | 225 | Attempt counter non-atomic write | **OPEN** |
| **R2-3** | **P1** | 246 | `--unlink-first` + power loss = file destruction | **OPEN (tradeoff)** |
| **R2-4** | P2 | 296-302 | No exit after reboot cascade failure | **OPEN** |
| **R2-5** | P2 | 291 | Partial `rm -rf` re-arms deadman | **OPEN** |
