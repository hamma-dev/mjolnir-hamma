# Phase C: AGS File Purge

**Date:** 2026-04-16
**Branch:** `feature/ags-scrubber`
**Related:** hamma-dev/mjolnir-hamma#20

## Purpose

Delete AGS data files whose triggers have all been confirmed present on the MJ Pi, freeing storage on the AGS sensor.

## Context

Phase A scans AGS + MJ and reports missing triggers. Phase B (`--recover`) copies missing triggers from AGS to MJ. Phase C (`--purge`) completes the pipeline by removing AGS files that are now fully duplicated on MJ.

AGS files are binary blobs containing multiple triggers concatenated together (~22MB per trigger). Individual triggers cannot be removed from a file, so deletion operates at the whole-file level.

## Behavior

`--purge` requires `--recover` in the same invocation. The flow:

1. Normal scan + compare (Phase A)
2. `--recover` copies missing triggers to MJ (Phase B)
3. `--purge` identifies eligible AGS files for deletion:
   - Group AGS entries by filename
   - A file is eligible only if every trigger in it is present in the MJ header set (including triggers just recovered in step 2)
   - Files with any failed or skipped recovery are not eligible
4. Delete eligible files via `ssh ags_host "rm <ags_path>/<filename>"`
5. Report which files were deleted and which were retained (with reasons)

`--dry-run --recover --purge` previews what would be deleted without touching anything.

## CLI

```
python hamma_scrub.py --recover --purge            # recover then delete
python hamma_scrub.py --recover --purge --dry-run   # show plan, no action
python hamma_scrub.py --purge                        # error: requires --recover
```

## Safety Guardrails

- `--purge` without `--recover` exits with an error message. This prevents deleting files that still have un-recovered triggers.
- Each deletion is a single `rm` per file, not `rm -rf` or a glob pattern.
- Every deletion is logged at INFO level with the full path.
- Files are only eligible when ALL triggers in the file are confirmed on MJ. A file with even one unconfirmed trigger is retained.
- Files where recovery failed or was skipped for any trigger are retained.

## New Functions

### `identify_purgeable_files(ags_entries, mj_headers, recovery_results)`

Groups AGS entries by filename. For each file, checks whether every trigger's header is in `mj_headers`. Cross-references `recovery_results` to exclude files with failed/skipped recoveries.

**Parameters:**
- `ags_entries` — list of dict from `scan_ags_files()`, each with `filename`, `offset`, `index`, `header`
- `mj_headers` — set of bytes (128-byte headers confirmed on MJ)
- `recovery_results` — list of dict from `recover_triggers()`, each with `status` ("recovered", "skipped", "failed", etc.) and source `filename`

**Returns:**
- dict with:
  - `purgeable` — list of filenames safe to delete
  - `retained` — list of dict, each with `filename` and `reason` (e.g., "2/15 triggers not on MJ", "1 recovery failed")

### `purge_ags_files(ags_host, ags_path, filenames, dry_run)`

Deletes each filename on the AGS via SSH.

**Parameters:**
- `ags_host` — SSH host for AGS
- `ags_path` — path to AGS data directory
- `filenames` — list of filenames to delete
- `dry_run` — if True, log what would be deleted but take no action

**Returns:**
- list of dict, each with `filename`, `status` ("deleted", "failed", "dry_run"), and optional `error`

## Integration with `run()`

After recovery completes in `run()`:

1. If `--purge` is set, call `identify_purgeable_files()` using the current `mj_headers` (updated to include recovered triggers) and `recovery_results`
2. Call `purge_ags_files()` with the purgeable list
3. Include purge results in the report

The MJ header set must be updated after recovery to include headers from successfully recovered triggers, so that triggers recovered in this run count toward file eligibility.

## Report Additions

### Human-readable

A "Purge" section after the Recovery section:

```
=== Purge ===
Deleted: 12 AGS files
Retained: 3 AGS files
  data_20260401_001 — 2/15 triggers not on MJ
  data_20260401_002 — 1 recovery failed
  data_20260401_003 — 1/8 triggers not on MJ
```

In dry-run mode: "Would delete: 12 AGS files"

### JSON

```json
{
  "purge": {
    "deleted": ["data_20260401_001", "data_20260401_002"],
    "retained": [
      {"filename": "data_20260401_003", "reason": "2/15 triggers not on MJ"}
    ],
    "dry_run": false
  }
}
```

## Testing

- `identify_purgeable_files`: all triggers matched -> purgeable; some missing -> retained; recovery failure -> retained; empty input -> empty output
- `purge_ags_files`: successful deletion; SSH failure; dry-run mode
- CLI: `--purge` without `--recover` is an error; `--purge` with `--recover` passes through
- Integration in `run()`: purge called after recovery; MJ headers updated before eligibility check; purge results in report
