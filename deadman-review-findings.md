# Deadman Script Review — Round 4 (Post-Patch)

## Context

The deadman script (`scripts/migration-deadman.sh`) was patched after Round 3 review (commit `1a3a93c`). Round 4 dispatched four independent expert reviewers:

1. **Bash correctness** — 10 categories of bash semantics (quoting, traps, fd inheritance, etc.)
2. **Failure mode analysis** — 20 specific failure scenarios traced step-by-step
3. **Adversarial input** — two reviewers focused on hostile/unexpected inputs
4. **Operational scenario** — full migration procedure walkthrough against actual repo contents

**What prior rounds fixed:** SIGPIPE trap, arithmetic overflow regex, sync after tar, daemon-reload after unit deletion, `--unlink-first`, persistent log file, chmod +x error check, unit file write verification, defuse stop ordering, defuse `deactivating` state check, RO FS detection + attempt counter, both `/var/lib` and `/etc` writability test, atomic attempt counter write, rollback script deletion ordering, exit after reboot cascade, rollback script write verification, leading-zero octal rejection, non-numeric attempt counter sanitization, arm() rollback service state check, arm() unit file existence guard, per-signal trap declarations, `timeout 5 logger` in rollback log(), RO_FS remount rw + sleep throttle.

---

## Round 4 Findings

### RESOLVED from Round 3 (confirmed fixed)

| Old # | Issue | Fix verified |
|-------|-------|-------------|
| R3-1 (P1) | arm() doesn't check rollback service state | ActiveState check + unit file guard |
| R3-2 (P1) | logger can hang indefinitely in rollback | `timeout 5 logger` in log() |
| R3-3 (P1) | RO_FS infinite reboot loop (deeper) | remount rw attempt + 5min sleep throttle |
| R3-4 (P2) | is-active false-negative during early boot | Unit file existence secondary guard |
| R3-5 (P3) | cleanup_partial_arm wrong exit code | Per-signal trap declarations |

---

### NEW issues found in Round 4

#### R4-1 (P1) — `timeout` in rollback can't kill children due to inherited SIG_IGN

**Source:** Bash correctness reviewer

The rollback script's `trap '' TERM INT HUP PIPE` sets SIGTERM disposition to SIG_IGN. Per POSIX, SIG_IGN survives `fork()`+`exec()`. When `timeout 60 sudo git ...` or `timeout 30 systemctl restart systemd-networkd` hangs, `timeout` sends SIGTERM, but the child inherits SIGTERM-ignored and ignores it. Without `--kill-after`, `timeout` blocks forever. `sudo` likely resets signal handling internally (security-sensitive), so the git path is probably safe. But `systemctl` is a standard D-Bus client that doesn't reset inherited signal dispositions — the high-risk line.

**Impact:** If networkd is in a bad state (exactly when rollback fires), the rollback hangs permanently. Sensor unreachable, requires physical access in Australia.

**Fix:** Add `--kill-after=10` to all `timeout` invocations in the rollback script. SIGKILL cannot be caught or ignored. GNU coreutils 8.30 on Buster supports `--kill-after`. **FIXED.**

---

#### R4-2 (P1) — `logger` in `cleanup_partial_arm` not wrapped in timeout

**Source:** Bash correctness reviewer

Line 84: `logger -t "deadman-arm" "Arm interrupted, cleaning up partial state"` is bare — same class of bug as R3-2. If journald is stuck, cleanup hangs, partial arm state is never cleaned up.

**Fix:** `timeout 5 logger ... 2>/dev/null || true`. **FIXED.**

---

#### R4-3 (P2) — `logger` in arm/defuse paths not wrapped in timeout

**Source:** Bash correctness reviewer

Lines 416, 444, 468, 477: bare `logger` calls in arm() success and defuse() error/success paths. Less critical than R4-2 (these are at the end of operations, not blocking cleanup), but still a hang risk.

**Fix:** `timeout 5 logger ... 2>/dev/null || true` on all four lines. **FIXED.**

---

#### R4-4 (P2) — `systemctl daemon-reload` in rollback has no timeout

**Source:** Failure mode analyst

Line 319: `systemctl daemon-reload || true` has no timeout, but the very next line (`systemctl restart systemd-networkd`) correctly uses `timeout 30`. If systemd's D-Bus interface hangs, the rollback blocks at daemon-reload and never reaches the reboot chain.

**Fix:** `timeout --kill-after=10 30 systemctl daemon-reload || true`. **FIXED.**

---

#### R4-5 (P2) — Orphaned wwan-check files after rollback

**Source:** Operational scenario reviewer

On the current `wwan_install` sensors, `/usr/local/bin/wwan-check.sh`, `/etc/systemd/system/wwan-check.timer`, and `/etc/systemd/system/wwan-check.service` do not exist. The backup tar won't include them. If install.sh creates these files and then rollback fires, the rollback restores old files but doesn't remove the NEW files absent from the tar. The orphaned `wwan-check.timer` fires every 5 minutes, running wwan-check.sh, which calls the OLD `50_bring_wwan0_up.py` that does `os.environ["IFACE"]` → `KeyError`.

**Impact:** After rollback, journal fills with KeyError crashes every 5 minutes. Connectivity NOT affected (carrier.d mechanism is restored), but wastes CPU/SD writes.

**Fix:** Before restarting wwan-check.timer, check whether it was in the original backup tar. If not, remove the orphaned files and disable the timer. **FIXED.**

---

### Confirmed Correct

**Failure mode analyst** traced 20 failure scenarios — 19 of 20 pass clean (scenario 14 was R4-4 above).

**Adversarial reviewers** found no exploitable issues. One suggested adding `--` to `git checkout` — **REJECTED** as this was a previously-fixed bug (adding `--` makes bash treat the branch name as a pathspec/file, not a branch).

**Operational scenario reviewer** verdict: **OPERATIONALLY READY.** Full migration procedure walkthrough confirmed:
- 30-minute timer gives 6x margin for ~5-minute procedure
- SSH tunnel survives all install.sh operations (wwan0 is Unmanaged=yes)
- Kill switch interaction correctly handled
- Sensors are fully independent
- Backup coverage matches all non-skipped install.sh actions

**Systemd reviewer** (Round 3, still valid): all design choices verified correct for systemd 241.

---

### Design Limitations (unchanged)

1. **OnActiveSec resets on every reboot** — documented, acceptable given kill-switch cron timing.
2. **Rollback deletes backup before confirming reboot** — intentional tradeoff for loop prevention.
3. **Pip packages / brokkr not rolled back** — by design.
4. **`--unlink-first` tradeoff** — file-type-change protection vs power-loss risk. Documented.

---

## Round 5 Findings

Round 5 dispatched 4 fresh independent experts (bash, systemd, SRE, networking) against the fully patched script (post-Round 10 commit `616f7fc`).

### RESOLVED from Round 4 (confirmed fixed)

| Old # | Issue | Fix verified |
|-------|-------|-------------|
| R4-1 (P1) | timeout can't kill children (SIG_IGN inheritance) | `--kill-after=10` on all timeout calls |
| R4-2 (P1) | cleanup_partial_arm logger unwrapped | `timeout 5 logger` |
| R4-3 (P2) | arm/defuse logger calls unwrapped | `timeout 5 logger` on all 4 lines |
| R4-4 (P2) | daemon-reload in rollback has no timeout | `timeout --kill-after=10 30` |
| R4-5 (P2) | Orphaned wwan-check files after rollback | tar tf check before remove/restart |

---

### NEW issues found in Round 5

#### R5-1 (P1) — Corrupted tar causes incorrect file deletion in orphan cleanup

**Source:** Networking expert + SRE expert (independently found same bug)

Lines 331, 334: The orphan cleanup logic uses `tar tf ... | grep -q ...` to check whether wwan-check files were in the original backup. When `tar tf` fails on a corrupted archive (SD card sector failure after arm), the pipeline returns exit 1 (grep's exit code, not tar's — bash pipelines return the last command's exit code). The `if !` negation sees exit 1 as "file not in tar" and **deletes wwan-check.sh and wwan-check.timer that should have been preserved**.

**Impact:** On a sensor with SD card degradation (the exact scenario these Pis face), a corrupted backup tar causes the rollback to actively destroy the cellular recovery mechanism. Sensor loses wwan-check and cannot recover cellular connectivity after reboot.

**Fix:** Check tar readability before processing orphan cleanup. If tar is unreadable, skip cleanup entirely (conservative — leave files in place rather than risk deleting needed ones).

---

#### R5-2 (P3) — `daemon-reload` in defuse() missing error handling

**Source:** Systemd expert

Line 475: `systemctl daemon-reload` has no `|| true`, unlike every other daemon-reload call in the script. If daemon-reload fails (systemd in degraded state), defuse exits non-zero even though the operation is logically complete (timer stopped, disabled, unit files deleted).

**Fix:** `systemctl daemon-reload 2>/dev/null || true`

---

### Clean Bills of Health

**Bash expert:** "NO NEW genuine bugs that 10 prior reviews missed. The script is production-quality." Exhaustive 25-item review found all edge cases handled correctly.

**Systemd expert:** All systemd semantics verified correct. Timer/service lifecycle, TOCTOU handling, daemon-reload timing, ConditionPathExists, arm() guards — all pass.

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
| R4-1 | ~~P1~~ | timeout can't kill children (SIG_IGN inheritance) | **FIXED** (Round 10) |
| R4-2 | ~~P1~~ | cleanup_partial_arm logger unwrapped | **FIXED** (Round 10) |
| R4-3 | ~~P2~~ | arm/defuse logger calls unwrapped | **FIXED** (Round 10) |
| R4-4 | ~~P2~~ | daemon-reload in rollback has no timeout | **FIXED** (Round 10) |
| R4-5 | ~~P2~~ | Orphaned wwan-check files after rollback | **FIXED** (Round 10) |
| **R5-1** | **P1** | Corrupted tar → incorrect orphan file deletion | **OPEN** |
| R5-2 | P3 | defuse() daemon-reload missing `|| true` | **OPEN** |

## Review History

| Round | Reviewers | New P0/P1 found | Commit |
|-------|-----------|-----------------|--------|
| 5-6 | 4 internal | 2 P0, 4 P1 | `b211b1b` |
| 7 | 4 internal | 3 risks | `f4f134e` |
| 8 (independent R1) | 3 independent | 2 P0, 4 P1 | `384a5ac` |
| 8 (independent R2) | 3 independent | 2 P1 | `70a5557` |
| 9 | 4 independent (3 returned) | 3 P1 | `1a3a93c` |
| 10 | 4 independent | 2 P1 | `616f7fc` |
| 11 | 4 independent (2 clean) | 1 P1 | (pending) |
