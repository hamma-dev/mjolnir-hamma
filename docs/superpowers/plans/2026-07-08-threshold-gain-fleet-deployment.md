# Threshold & Gain Control — Fleet Deployment Plan

**Status:** Feature merged to `0.4.x` (PR #70, merge commit `01dfff7`). **Nothing deployed yet.**
This is a review checklist; run nothing on the fleet/VPS until approved.

**Key simplifier:** both changed scripts are invoked **on demand** — `ags.py` over SSH per
command, `mjol_array.py` by hand on the VPS. **No brokkr/sindri/service restarts** are
needed anywhere. Deployment = `git` updates in the right checkouts.

Survey date 2026-07-08 (read-only, via `hamma-fleet` + `ssh vps`).

---

## Target A — VPS operator tool (`mjol_array.py`)

**Current state:** `/home/monitor/dev/mjolnir-hamma` on branch **`0.3.x`**, old `mjol_array.py`
(no `set_threshold`). Login to the VPS lands as `pi`; **`pi` cannot passwordless-sudo to
`monitor`** — so this step must be run by a human logged in as `monitor`.

**Recommendation: switch monitor's checkout to `0.4.x`.** Verified safe:
- `server/webgen.py` (the only thing monitor's crontab runs) is **byte-identical** on
  `0.3.x` and `0.4.x`; imports only `argparse/pathlib/datetime/pandas/numpy` (already
  present); does not import `website/main.py`, `sindri`, or `mjol_array`.
- Only server-side diff `0.3.x → 0.4.x` is `mjol_array.py` (desired) + `scripts/ags.py`
  (VPS never runs it). No server-side file removed.
- Rejected alternative: cherry-pick `mjol_array.py` onto `0.3.x` — commits are interleaved
  with `ags.py`, messier, and keeps `0.3.x` diverging.

**Steps (run as `monitor` on the VPS):**
1. `cd ~/dev/mjolnir-hamma`
2. `git status` → confirm working tree clean.
3. `git log --oneline origin/0.3.x..HEAD` → confirm **no VPS-local commits** on 0.3.x. If
   any exist, STOP and reconcile before switching branches (don't strand them).
4. `git fetch origin`
5. `git checkout 0.4.x` (first time: `git checkout -b 0.4.x origin/0.4.x`)
6. `git pull --ff-only origin 0.4.x`
7. Verify: `grep -c 'def set_threshold' server/mjol_array.py` → `1`; `./server/mjol_array.py --help`
   shows `--set-threshold` / `--set-gain` / `--persist`.

**Rollback:** `git checkout 0.3.x` (scripts only; no data risk).

---

## Target B — Sensors (`ags.py`), 21 units

Per-unit handling from the survey. Deploy (when approved) as `pi` on each Pi:
`cd /home/pi/dev/mjolnir-hamma && git pull --ff-only` then
`grep -c 'def set_threshold' scripts/ags.py` → `1`.

### B1. Clean, on `0.4.x`, behind — simple fast-forward (8 units)
mj03, mj04, mj06, mj08, mj41, mj42, mj43, mj50.

Batch command (when approved):
```
hamma-fleet run mj03,mj04,mj06,mj08,mj41,mj42,mj43,mj50 \
  'cd /home/pi/dev/mjolnir-hamma && git pull --ff-only 2>&1 | tail -1; grep -c "def set_threshold" scripts/ags.py'
```
Expect each: fast-forward to include `01dfff7`, then `1`.

### B2. Special cases — resolve individually BEFORE deploying (3 units)
- **mj05** — on `0.4.x` but **3 dirty files**. First inspect: `git -C /home/pi/dev/mjolnir-hamma status --porcelain` and diff. Do NOT discard unknown local changes. If inconsequential, stash/commit-aside, then ff-pull.
- **mj00** — on **`0.3.x`** (not 0.4.x). `0.3.x` does NOT carry this feature (merged to 0.4.x only). Investigate why mj00 sits on 0.3.x before switching it to 0.4.x; it may be intentional/old. Flag for decision.
- **mj02** — parked on **`feature/noise-alert-persistence`** (unmerged validation work). Returning to `0.4.x` would drop that deployment. Coordinate: wait until that PR merges to `0.4.x` (then 0.4.x carries both) OR deploy `ags.py` deliberately. This overlaps the pre-existing "return mj02 to 0.4.x after PR" action — don't disturb mj02's current validation without deciding.

### B3. Unreachable — queue for next contact (10 units)
mj01, mj07, mj09, mj10, mj51, mj52, mj53, mj54, mj55, mj56. Deploy on next contact
(several are long-down). Track so they aren't silently missed.

---

## Verification (post-deploy)

- **Sensor:** `grep -c 'def set_threshold' scripts/ags.py` = 1; `ags.py get-state` returns
  threshold/gain values.
- **VPS:** `mjol_array.py --help` shows the new flags.
- **End-to-end / deferred fan-out E2E (VPS → sensor):** against one freshly-updated unit,
  `./server/mjol_array.py -p 03 --set-gain fast-e <current_level>` (use the unit's CURRENT
  gain level so the set is idempotent / no real change), confirm `[OK]`-style output, then
  `mjol_array.py -p 03 --status`. This proves the operator path end-to-end without altering
  sensor tuning.

## Rollback (any target)
`git checkout <previous branch/sha>` in the affected checkout. Scripts only — no data risk,
no service state.

## Open flags to resolve
1. mj00 on `0.3.x` — why, and should it move to `0.4.x`?
2. mj02 feature-branch dependency (`noise-alert-persistence`).
3. mj05's 3 dirty files.
4. 10 unreachable units — deployment backlog.
5. No `sensor-log` issue filed for this feature yet (was deferred per earlier decision).
