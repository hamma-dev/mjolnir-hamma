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
3. Update `mj_headers` with successfully recovered triggers (see "MJ Header Update" below)
4. `--purge` identifies eligible AGS files for deletion:
   - Group AGS entries by filename
   - A file is eligible only if every trigger in it is present in the updated MJ header set
   - The lexicographically newest AGS file is never eligible (it may be actively written)
   - Files with any unsafe recovery status are not eligible (see "Status Whitelist" below)
5. Delete eligible files via `ssh ags_host "rm <ags_path>/<filename>"` (timeout: 15s per file)
6. Report which files were deleted and which were retained (with reasons)

`--dry-run --recover --purge` previews what would be deleted without touching anything.

## CLI

```
python hamma_scrub.py --recover --purge            # recover then delete
python hamma_scrub.py --recover --purge --dry-run   # show plan, no action
python hamma_scrub.py --purge                        # error: requires --recover
```

## Safety Guardrails

- `--purge` without `--recover` exits with an error message. This prevents deleting files that still have un-recovered triggers.
- The lexicographically newest AGS file is never purged, even if all its triggers are on MJ. It may be actively receiving new triggers from the AGS.
- Each deletion is a single `rm` per file, not `rm -rf` or a glob pattern.
- Every deletion is logged at INFO level with the full path.
- Files are only eligible when ALL triggers in the file are confirmed on MJ. A file with even one unconfirmed trigger is retained.
- Files with any unsafe recovery status are retained (see "Status Whitelist").
- Partial purge is safe: each file deletion is independent. If SSH drops mid-purge, already-deleted files are gone and remaining files are untouched. No rollback needed.
- Eligibility is checked per-file based on that file's own trigger list, not a deduplicated global set. If the same header appears in files A and B, each file is evaluated independently.

## MJ Header Update

`recover_triggers()` must be modified to include `"header"` (the 128-byte header bytes) in each result dict. The candidate dict already has `candidate["header"]` available — it just needs to be carried through to the result. This is a small code change to an existing function.

After recovery completes in `run()`, update `mj_headers` before calling `identify_purgeable_files`:

```python
for r in recovery_results:
    if r["status"] == "recovered":
        mj_headers.add(r["header"])
```

Only `"recovered"` status entries are added — skipped/failed entries are not assumed to be on MJ.

## Status Whitelist

Recovery results use these status values. For purge eligibility, they are classified as:

| Status | Meaning | Safe for purge? |
|--------|---------|-----------------|
| `"recovered"` | Trigger copied to MJ this run | Yes |
| `"skipped"` with `error == "file already exists"` | Trigger already on MJ from prior recovery | Yes |
| `"dry_run"` | Not actually recovered | No — retain file |
| `"skipped"` (active file guard) | Skipped because it's the newest trigger | No — retain file |
| `"skipped_before_since"` | Skipped by `--since` date filter | No — retain file |
| `"failed"` | Extraction or verification failed | No — retain file |

A recovery result is safe if `status == "recovered"` or `(status == "skipped" and error == "file already exists")`. All other status/error combinations are unsafe.

A file is eligible for purge only if every trigger in it either (a) was already in `mj_headers` before recovery, or (b) has a "safe" recovery status.

## New Functions

### `identify_purgeable_files(ags_entries, mj_headers, recovery_results)`

Groups AGS entries by filename. Excludes the lexicographically newest AGS file unconditionally. For each remaining file, determines eligibility using this algorithm:

1. Build a lookup from `recovery_results`: map `(source_file, source_offset)` -> result dict
2. For each trigger in the file:
   - If `header in mj_headers` -> confirmed (regardless of recovery status)
   - Else, look up recovery result by `(filename, offset)`. If safe status -> confirmed.
   - Else -> unconfirmed. File is retained.
3. A file is purgeable only if all its triggers are confirmed.

**Parameters:**
- `ags_entries` — list of dict from `scan_ags_files()`, each with `filename`, `offset`, `index`, `header`
- `mj_headers` — set of bytes (128-byte headers confirmed on MJ, already updated post-recovery)
- `recovery_results` — list of dict from `recover_triggers()`, each with `status`, `source_file`, `header`, and optional `error`. May be `None` or empty if no recovery was needed (all triggers already matched).

**Returns:**
- dict with:
  - `purgeable` — list of filenames safe to delete (sorted)
  - `retained` — list of dict, each with `filename` and `reason` (e.g., "2/15 triggers not on MJ", "1 recovery failed", "active file")

**Edge cases:**
- `recovery_results` is `None` or empty: all files evaluated purely on header matching (recovery wasn't needed, so no unsafe statuses to check)
- Zero-trigger AGS files (< 128 bytes, skipped by strider): never appear in `ags_entries`, so they are never purged. This is a known limitation — small/corrupt AGS files accumulate. Out of scope for Phase C.

### `purge_ags_files(ags_host, ags_path, filenames, dry_run)`

Deletes each filename on the AGS via SSH. Remote path constructed as `"{}/{}".format(ags_path, filename)` (forward-slash join, matching `extract_trigger` pattern).

**Parameters:**
- `ags_host` — SSH host for AGS
- `ags_path` — path to AGS data directory
- `filenames` — list of filenames to delete (empty list is a no-op)
- `dry_run` — if True, log what would be deleted but take no action

**Returns:**
- list of dict, each with `filename`, `status` ("deleted", "failed", "dry_run"), and optional `error`

Each deletion runs as `subprocess.run(["ssh", ags_host, "rm " + shlex.quote("{}/{}".format(ags_path, filename))], timeout=15)`. The `shlex.quote()` guards against filenames with shell metacharacters (unlikely given AGS naming patterns, but correct for a destructive operation).

## Integration with `run()`

After recovery completes in `run()`:

1. Update `mj_headers` with recovered trigger headers
2. If `--purge` is set, call `identify_purgeable_files()` with updated headers and recovery results
3. Call `purge_ags_files()` with the purgeable list
4. Include purge results in the report

The purge path (steps 2-4) must run regardless of whether recovery actually executed. It is not gated on `comparison["missing_on_mj"]` being non-empty. If `--purge` is set but no recovery was needed (no missing triggers), `recovery_results` is `None` and purge evaluates eligibility purely on header matching.

## Exit Codes

Existing exit codes are unchanged. Purge failures do not change the exit code — the primary question the script answers is "are there missing triggers?" Purge is a secondary action. Purge failures are reported in the output and logged as warnings.

## Report Additions

### Human-readable

A "Purge" section after the Recovery section:

```
=== Purge ===
Deleted: 12 AGS files
Retained: 3 AGS files
  data_20260401_001 — 2/15 triggers not on MJ
  data_20260401_002 — 1 recovery failed
  data_20260401_003 — active file
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

### `identify_purgeable_files`

- All triggers in file matched on MJ -> purgeable
- Some triggers missing -> retained with reason "N/M triggers not on MJ"
- Recovery failure for one trigger -> retained with reason "1 recovery failed"
- Newest file (lexicographically) -> retained with reason "active file"
- `recovery_results` is `None` -> evaluated on header matching alone
- Empty `ags_entries` -> empty purgeable and retained lists
- `--since` caused `skipped_before_since` -> retained
- `dry_run` status in recovery -> retained

### `purge_ags_files`

- Successful deletion: SSH returns rc=0 -> status "deleted"
- SSH failure: rc!=0 -> status "failed" with error message
- SSH timeout -> status "failed" with timeout error
- Dry-run: logs "Would delete" -> status "dry_run"
- Empty filenames list -> returns empty list (no-op)
- Path construction: verify `ags_path/filename` uses forward slash

### CLI

- `--purge` without `--recover` -> error message and non-zero exit
- `--purge` with `--recover` -> accepted

### Integration (`run()`)

- Mock scan/compare returning known data, call `run(recover=True, purge=True)`:
  - Verify recovery runs before purge
  - Verify `mj_headers` updated with recovered headers before eligibility check
  - Verify eligible files passed to `purge_ags_files`
  - Verify purge results appear in report output
- `run(recover=True, purge=True)` with no missing triggers: purge still evaluates eligibility

### Report formatting

- Human report with purge results (deleted + retained)
- Human report dry-run variant
- Human report with no purge (`purge=None`)
- JSON report with purge results
- JSON report with no purge
