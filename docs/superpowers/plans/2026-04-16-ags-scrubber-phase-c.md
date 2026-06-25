# Phase C: AGS File Purge Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--purge` flag to `hamma_scrub.py` that deletes AGS files whose triggers are all confirmed present on the MJ Pi.

**Architecture:** Two new functions (`identify_purgeable_files`, `purge_ags_files`) plus modifications to `recover_triggers` (add `"header"` to result dicts), `run()` (purge flow after recovery), report functions (purge section), and CLI (`--purge` flag). All in the existing single-file script.

**Tech Stack:** Python 3.6+, subprocess (SSH), shlex, pytest with unittest.mock

**Spec:** `docs/superpowers/specs/2026-04-16-ags-scrubber-phase-c-design.md`

---

## Chunk 1: Core Functions

### Task 1: Add `"header"` to `recover_triggers()` result dicts

The existing `recover_triggers()` function returns result dicts without the trigger's 128-byte header. Phase C needs headers in the results to update `mj_headers` after recovery. The candidate dict already has `candidate["header"]` — it just needs to be threaded through to all 8 `results.append()` sites.

**Files:**
- Modify: `scripts/hamma_scrub.py` — `recover_triggers()` function (lines 886-1044)
- Modify: `tests/python/test_hamma_scrub.py` — `TestRecoverTriggers` class (line 890)

- [ ] **Step 1: Write failing test — recovered result includes header**

Add to `TestRecoverTriggers` in `tests/python/test_hamma_scrub.py`:

```python
def test_result_includes_header(self, hamma_scrub, tmp_path):
    """Each recovery result dict includes the trigger's header bytes."""
    header, payload_pad = _make_trigger()

    candidates = [{
        "filename": "ags001.bin",
        "offset": 0,
        "index": 0,
        "header": header,
        "skip_status": None,
        "skip_reason": None,
    }]

    # Create a DATA_1 drive with enough space
    drive = tmp_path / "DATA_1"
    drive.mkdir()

    mock_data = header + payload_pad
    with patch.object(hamma_scrub, "extract_trigger", return_value=mock_data), \
         patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
         patch.object(hamma_scrub, "select_target_drive", return_value=str(drive)):
        results = hamma_scrub.recover_triggers(
            candidates, "hamma", "/ags/data", str(tmp_path), dry_run=False,
        )

    assert results[0]["header"] == header
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestRecoverTriggers::test_result_includes_header -v`
Expected: FAIL — KeyError: 'header'

- [ ] **Step 3: Add `"header": candidate["header"]` to all 9 result append sites**

In `scripts/hamma_scrub.py`, add `"header": candidate["header"],` to every `results.append({...})` call inside `recover_triggers()`. There are 9 sites:

1. Line 896 — skipped candidates
2. Line 921 — no drive space
3. Line 937 — dry_run
4. Line 950 — file already exists
5. Line 964 — dd extraction failed
6. Line 978 — verification failed
7. Line 1002 — race condition (file already exists)
8. Line 1022 — success (recovered)
9. Line 1034 — OSError during atomic write

Each one gets `"header": candidate["header"],` added as a new key in the dict.

- [ ] **Step 4: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all 109 tests PASS (108 existing + 1 new)

- [ ] **Step 5: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): include header bytes in recover_triggers result dicts"
```

---

### Task 2: `identify_purgeable_files`

Determines which AGS files are safe to delete. Groups entries by filename, excludes the newest file, checks every trigger against `mj_headers` and recovery results.

**Files:**
- Modify: `scripts/hamma_scrub.py` — add function after `recover_triggers()` (after line 1044)
- Modify: `tests/python/test_hamma_scrub.py` — add `TestIdentifyPurgeableFiles` class after `TestRecoverTriggers`

- [ ] **Step 1: Write failing test — all triggers matched, file is purgeable**

Add `TestIdentifyPurgeableFiles` class to `tests/python/test_hamma_scrub.py`:

```python
class TestIdentifyPurgeableFiles:
    """Test AGS file purge eligibility logic."""

    def test_all_matched_is_purgeable(self, hamma_scrub):
        """File with all triggers in mj_headers is purgeable."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            # Second file is newest — excluded
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1, h2}

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results=None,
        )

        assert result["purgeable"] == ["ags001.bin"]
        # ags002.bin retained as active file
        assert len(result["retained"]) == 1
        assert result["retained"][0]["filename"] == "ags002.bin"
        assert "active" in result["retained"][0]["reason"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestIdentifyPurgeableFiles::test_all_matched_is_purgeable -v`
Expected: FAIL — AttributeError: module has no attribute 'identify_purgeable_files'

- [ ] **Step 3: Implement `identify_purgeable_files`**

Add to `scripts/hamma_scrub.py` after `recover_triggers()` (after line 1044):

```python
def identify_purgeable_files(ags_entries, mj_headers, recovery_results=None):
    """Identify AGS files safe to delete.

    A file is purgeable when every trigger in it is confirmed on MJ
    (by header match or safe recovery status) and it is not the
    lexicographically newest file (which may be actively written).

    Parameters
    ----------
    ags_entries : list of dict
        From scan_ags_files(), each with filename, offset, index, header.
    mj_headers : set of bytes
        128-byte headers confirmed on MJ (already updated post-recovery).
    recovery_results : list of dict or None
        From recover_triggers(), each with status, source_file,
        source_offset, header, error. None if no recovery was needed.

    Returns
    -------
    dict
        purgeable: sorted list of filenames safe to delete.
        retained: list of dict with filename and reason.
    """
    if not ags_entries:
        return {"purgeable": [], "retained": []}

    # Group entries by filename
    files = {}
    for entry in ags_entries:
        fname = entry["filename"]
        if fname not in files:
            files[fname] = []
        files[fname].append(entry)

    # Build recovery result lookup: (source_file, source_offset) -> result
    recovery_lookup = {}
    if recovery_results:
        for r in recovery_results:
            key = (r["source_file"], r["source_offset"])
            recovery_lookup[key] = r

    # Newest file (lexicographically) is never purgeable
    newest = sorted(files.keys())[-1]

    purgeable = []
    retained = []

    for fname in sorted(files.keys()):
        if fname == newest:
            retained.append({"filename": fname, "reason": "active file"})
            continue

        triggers = files[fname]
        total = len(triggers)
        unconfirmed = 0
        failed_count = 0

        for entry in triggers:
            if entry["header"] in mj_headers:
                continue
            # Header not in mj_headers — check recovery result
            key = (fname, entry["offset"])
            r = recovery_lookup.get(key)
            if r is not None:
                if r["status"] == "recovered":
                    continue
                if (r["status"] == "skipped"
                        and r.get("error") == "file already exists"):
                    continue
                # Unsafe status
                if r["status"] == "failed":
                    failed_count += 1
                else:
                    unconfirmed += 1
            else:
                unconfirmed += 1

        if unconfirmed == 0 and failed_count == 0:
            purgeable.append(fname)
        else:
            parts = []
            if unconfirmed > 0:
                parts.append("{}/{} triggers not on MJ".format(
                    unconfirmed, total))
            if failed_count > 0:
                parts.append("{} recovery failed".format(failed_count))
            retained.append({
                "filename": fname,
                "reason": ", ".join(parts),
            })

    return {"purgeable": purgeable, "retained": retained}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestIdentifyPurgeableFiles::test_all_matched_is_purgeable -v`
Expected: PASS

- [ ] **Step 5: Write remaining tests for `identify_purgeable_files`**

Add to `TestIdentifyPurgeableFiles`:

```python
def test_some_missing_retained(self, hamma_scrub):
    """File with unmatched triggers is retained."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    h3 = b'\x03' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h3},
    ]
    mj_headers = {h1}  # h2 and h3 not matched

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results=None,
    )

    assert result["purgeable"] == []
    reasons = {r["filename"]: r["reason"] for r in result["retained"]}
    assert "1/2 triggers not on MJ" in reasons["ags001.bin"]
    assert reasons["ags002.bin"] == "active file"

def test_recovery_failure_retains(self, hamma_scrub):
    """File with a failed recovery is retained."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}
    recovery_results = [{
        "source_file": "ags001.bin",
        "source_offset": 1000,
        "status": "failed",
        "header": h2,
        "error": "dd extraction failed",
    }]

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results,
    )

    assert result["purgeable"] == []
    reasons = {r["filename"]: r["reason"] for r in result["retained"]}
    assert "1 recovery failed" in reasons["ags001.bin"]

def test_newest_file_always_retained(self, hamma_scrub):
    """Lexicographically newest file is always retained."""
    h1 = b'\x01' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results=None,
    )

    # Single file is both newest and only — retained
    assert result["purgeable"] == []
    assert result["retained"][0]["reason"] == "active file"

def test_recovery_results_none(self, hamma_scrub):
    """None recovery_results evaluates purely on header matching."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h2},
    ]
    mj_headers = {h1, h2}

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results=None,
    )

    assert result["purgeable"] == ["ags001.bin"]

def test_empty_entries(self, hamma_scrub):
    """Empty ags_entries returns empty results."""
    result = hamma_scrub.identify_purgeable_files(
        [], set(), recovery_results=None,
    )
    assert result["purgeable"] == []
    assert result["retained"] == []

def test_skipped_before_since_retains(self, hamma_scrub):
    """Trigger with skipped_before_since status retains the file."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}
    recovery_results = [{
        "source_file": "ags001.bin",
        "source_offset": 1000,
        "status": "skipped_before_since",
        "header": h2,
        "error": "before --since cutoff",
    }]

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results,
    )

    assert result["purgeable"] == []

def test_dry_run_status_retains(self, hamma_scrub):
    """Trigger with dry_run status retains the file."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}
    recovery_results = [{
        "source_file": "ags001.bin",
        "source_offset": 1000,
        "status": "dry_run",
        "header": h2,
        "error": None,
    }]

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results,
    )

    assert result["purgeable"] == []

def test_skipped_file_exists_is_safe(self, hamma_scrub):
    """Trigger skipped because file already exists is safe for purge."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}
    recovery_results = [{
        "source_file": "ags001.bin",
        "source_offset": 1000,
        "status": "skipped",
        "header": h2,
        "error": "file already exists",
    }]

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results,
    )

    assert "ags001.bin" in result["purgeable"]

def test_skipped_active_guard_retains(self, hamma_scrub):
    """Trigger skipped by active file guard retains the file."""
    h1 = b'\x01' * 128
    h2 = b'\x02' * 128
    ags_entries = [
        {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
        {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
    ]
    mj_headers = {h1}
    recovery_results = [{
        "source_file": "ags001.bin",
        "source_offset": 1000,
        "status": "skipped",
        "header": h2,
        "error": "last trigger in active file",
    }]

    result = hamma_scrub.identify_purgeable_files(
        ags_entries, mj_headers, recovery_results,
    )

    assert result["purgeable"] == []
```

- [ ] **Step 6: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): add identify_purgeable_files for Phase C purge eligibility"
```

---

### Task 3: `purge_ags_files`

Deletes eligible AGS files via SSH. Each file is an independent `rm` call with `shlex.quote()` for safety.

**Files:**
- Modify: `scripts/hamma_scrub.py` — add `import shlex` before `import shutil` (between lines 21-22, alphabetical), add function after `identify_purgeable_files`
- Modify: `tests/python/test_hamma_scrub.py` — add `TestPurgeAgsFiles` class

- [ ] **Step 1: Write failing test — successful deletion**

Add `TestPurgeAgsFiles` class to `tests/python/test_hamma_scrub.py`:

```python
class TestPurgeAgsFiles:
    """Test SSH-based AGS file deletion."""

    def test_successful_deletion(self, hamma_scrub):
        """Successful SSH rm returns status 'deleted'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
            )

        assert len(results) == 1
        assert results[0]["filename"] == "ags001.bin"
        assert results[0]["status"] == "deleted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestPurgeAgsFiles::test_successful_deletion -v`
Expected: FAIL — AttributeError: module has no attribute 'purge_ags_files'

- [ ] **Step 3: Add `import shlex` and implement `purge_ags_files`**

Add `import shlex` after line 22 (`import shutil`) in `scripts/hamma_scrub.py`.

Add `purge_ags_files` after `identify_purgeable_files`:

```python
def purge_ags_files(ags_host, ags_path, filenames, dry_run=False):
    """Delete AGS files via SSH.

    Parameters
    ----------
    ags_host : str
        SSH host for AGS sensor.
    ags_path : str
        Path to AGS data directory on sensor.
    filenames : list of str
        Filenames to delete.
    dry_run : bool
        If True, log what would be deleted but take no action.

    Returns
    -------
    list of dict
        Each with filename, status ('deleted', 'failed', 'dry_run'),
        and optional error.
    """
    results = []
    for fname in filenames:
        remote_path = "{}/{}".format(ags_path, fname)

        if dry_run:
            logger.info("Would delete: %s:%s", ags_host, remote_path)
            results.append({
                "filename": fname,
                "status": "dry_run",
                "error": None,
            })
            continue

        cmd = ["ssh", ags_host, "rm " + shlex.quote(remote_path)]
        logger.info("Deleting: %s:%s", ags_host, remote_path)
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timeout deleting %s on %s", fname, ags_host)
            results.append({
                "filename": fname,
                "status": "failed",
                "error": "SSH timeout (15s)",
            })
            continue

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace').strip()
            logger.warning("Failed to delete %s: %s", fname, stderr)
            results.append({
                "filename": fname,
                "status": "failed",
                "error": stderr,
            })
        else:
            results.append({
                "filename": fname,
                "status": "deleted",
                "error": None,
            })

    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestPurgeAgsFiles::test_successful_deletion -v`
Expected: PASS

- [ ] **Step 5: Write remaining tests**

Add to `TestPurgeAgsFiles`:

```python
def test_ssh_failure(self, hamma_scrub):
    """SSH rm failure returns status 'failed' with error."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = b'No such file or directory'

    with patch("subprocess.run", return_value=mock_result):
        results = hamma_scrub.purge_ags_files(
            "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
        )

    assert results[0]["status"] == "failed"
    assert "No such file" in results[0]["error"]

def test_ssh_timeout(self, hamma_scrub):
    """SSH timeout returns status 'failed'."""
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
        results = hamma_scrub.purge_ags_files(
            "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
        )

    assert results[0]["status"] == "failed"
    assert "timeout" in results[0]["error"].lower()

def test_dry_run(self, hamma_scrub):
    """Dry run logs but does not call subprocess."""
    with patch("subprocess.run") as mock_run:
        results = hamma_scrub.purge_ags_files(
            "hamma", "/ags/data", ["ags001.bin", "ags002.bin"],
            dry_run=True,
        )

    mock_run.assert_not_called()
    assert len(results) == 2
    assert all(r["status"] == "dry_run" for r in results)

def test_empty_filenames(self, hamma_scrub):
    """Empty filenames list returns empty results."""
    results = hamma_scrub.purge_ags_files(
        "hamma", "/ags/data", [], dry_run=False,
    )
    assert results == []

def test_path_uses_shlex_quote(self, hamma_scrub):
    """Remote path is shell-quoted for safety."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = b''

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        hamma_scrub.purge_ags_files(
            "hamma", "/ags/data", ["ags file.bin"], dry_run=False,
        )

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ssh"
    assert cmd[1] == "hamma"
    # shlex.quote wraps paths with spaces in single quotes
    assert "'/ags/data/ags file.bin'" in cmd[2]
```

- [ ] **Step 6: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): add purge_ags_files for SSH-based AGS file deletion"
```

---

## Chunk 2: CLI, Integration, and Reports

> **Note:** Line numbers below reference the source before Chunk 1 is applied. After Chunk 1, functions will have shifted. Search for function names (`_build_parser`, `run`, `format_human_report`, etc.) rather than relying on exact line numbers.

### Task 4: `--purge` CLI flag and validation

Add the `--purge` argument to the parser and validate that `--purge` requires `--recover`.

**Files:**
- Modify: `scripts/hamma_scrub.py` — `_build_parser()` (line 1183), `run()` (line 1231), `main()` (line 1340)
- Modify: `tests/python/test_hamma_scrub.py` — `TestCLI` (line 1235)

- [ ] **Step 1: Write failing tests**

Add to `TestCLI`:

```python
def test_purge_flag(self, hamma_scrub):
    """--purge flag is parsed."""
    parser = hamma_scrub._build_parser()
    args = parser.parse_args(["--recover", "--purge"])
    assert args.purge is True

def test_purge_default(self, hamma_scrub):
    """--purge defaults to False."""
    parser = hamma_scrub._build_parser()
    args = parser.parse_args([])
    assert args.purge is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestCLI::test_purge_flag -v`
Expected: FAIL — unrecognized arguments: --purge

- [ ] **Step 3: Add `--purge` to parser, `purge` param to `run()`, wire in `main()`**

In `_build_parser()`, after the `--recover` argument block (after line 1227), add:

```python
parser.add_argument(
    "--purge", action="store_true",
    help="After recovery, delete AGS files fully confirmed on MJ (requires --recover)",
)
```

In `run()` signature (line 1231), add `purge=False` parameter:

```python
def run(ags_host, ags_path, mj_path, json_output=False, output_file=None,
        limit=DEFAULT_LIMIT, since=None, recover=False, dry_run=False,
        purge=False):
```

Add validation at the top of `run()`, after the dry_run warning (after line 1259):

```python
if purge and not recover:
    logger.error("--purge requires --recover")
    return EXIT_NO_DATA
```

In `main()` (line 1352), add `purge=args.purge` to the `run()` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestCLI -v`
Expected: PASS

- [ ] **Step 5: Write test for --purge without --recover validation**

Add to `TestMain`:

```python
def test_purge_without_recover_errors(self, hamma_scrub):
    """--purge without --recover returns error exit code (early exit, no scanning)."""
    rc = hamma_scrub.run(
        "hamma", "/ags/data", "/home/pi/data",
        purge=True, recover=False,
    )
    assert rc == hamma_scrub.EXIT_NO_DATA
```

- [ ] **Step 6: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): add --purge CLI flag with --recover validation"
```

---

### Task 5: Report functions — purge section

Add purge results to both human and JSON report formatting.

**Files:**
- Modify: `scripts/hamma_scrub.py` — `format_human_report()` (line 1047), `format_json_report()` (line 1137)
- Modify: `tests/python/test_hamma_scrub.py` — add `TestPurgeReport` class

- [ ] **Step 1: Write failing tests for human report**

Add `TestPurgeReport` class after `TestRecoveryReport`:

```python
class TestPurgeReport:
    """Test purge section in reports."""

    def test_human_report_with_purge(self, hamma_scrub):
        """Human report includes purge section with deleted and retained."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "failed": [],
            "retained": [
                {"filename": "ags002.bin", "reason": "active file"},
            ],
            "dry_run": False,
        }
        report = hamma_scrub.format_human_report(
            results, purge=purge,
        )
        assert "=== Purge ===" in report
        assert "Deleted: 1" in report
        assert "Retained: 1" in report
        assert "ags002.bin" in report
        assert "active file" in report

    def test_human_report_purge_dry_run(self, hamma_scrub):
        """Human report shows 'Would delete' in dry-run mode."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "failed": [],
            "retained": [],
            "dry_run": True,
        }
        report = hamma_scrub.format_human_report(
            results, purge=purge,
        )
        assert "Would delete: 1" in report

    def test_human_report_no_purge(self, hamma_scrub):
        """Human report without purge has no purge section."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        report = hamma_scrub.format_human_report(results, purge=None)
        assert "Purge" not in report

    def test_json_report_with_purge(self, hamma_scrub):
        """JSON report includes purge key."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "retained": [{"filename": "ags002.bin", "reason": "active file"}],
            "dry_run": False,
        }
        report_str = hamma_scrub.format_json_report(
            results, "hamma", purge=purge,
        )
        data = json.loads(report_str)
        assert "purge" in data
        assert data["purge"]["deleted"] == ["ags001.bin"]
        assert data["purge"]["dry_run"] is False

    def test_json_report_no_purge(self, hamma_scrub):
        """JSON report without purge has no purge key."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        report_str = hamma_scrub.format_json_report(
            results, "hamma", purge=None,
        )
        data = json.loads(report_str)
        assert "purge" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestPurgeReport -v`
Expected: FAIL — TypeError: format_human_report() got an unexpected keyword argument 'purge'

- [ ] **Step 3: Add `purge=None` parameter to both report functions**

In `format_human_report()` (line 1047), add `purge=None` parameter:

```python
def format_human_report(results, limit=DEFAULT_LIMIT, recovery=None, purge=None):
```

After the recovery section (after line 1133), add:

```python
# Purge section (only when purge was performed)
if purge is not None:
    lines.append("")
    lines.append("=== Purge ===")
    if purge.get("dry_run"):
        lines.append("Would delete: {} AGS files".format(
            len(purge["deleted"])))
    else:
        lines.append("Deleted: {} AGS files".format(
            len(purge["deleted"])))
    if purge["retained"]:
        lines.append("Retained: {} AGS files".format(
            len(purge["retained"])))
        for r in purge["retained"]:
            lines.append("  {} \u2014 {}".format(
                r["filename"], r["reason"]))
```

In `format_json_report()` (line 1137), add `purge=None` parameter:

```python
def format_json_report(results, ags_host, recovery=None, purge=None):
```

After the recovery block (after line 1179), add:

```python
if purge is not None:
    report["purge"] = purge
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestPurgeReport -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): add purge section to human and JSON reports"
```

---

### Task 6: Wire purge into `run()` and `main()`

Connect the purge flow: update mj_headers after recovery, call `identify_purgeable_files`, call `purge_ags_files`, pass results to reports. The purge path runs regardless of whether recovery actually executed (i.e., not gated on missing triggers being non-empty).

**Files:**
- Modify: `scripts/hamma_scrub.py` — `run()` function (lines 1231-1337)
- Modify: `tests/python/test_hamma_scrub.py` — `TestMain` class (line 1284)

- [ ] **Step 1: Write failing test — purge called after recovery**

Add to `TestMain`:

```python
def test_run_with_purge_calls_purge(self, hamma_scrub):
    """run() with purge=True calls identify_purgeable_files and purge_ags_files."""
    h1 = b'\x01' * 128
    ags_result = {
        "entries": [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        ],
        "headers": {h1},
        "duplicate_count": 0,
        "elapsed": 1.0,
    }
    mj_result = {
        "headers": {h1},
        "file_count": 1,
        "duplicate_count": 0,
        "skipped": 0,
        "dirs_skipped": 0,
        "elapsed": 0.5,
    }

    with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
         patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
         patch.object(hamma_scrub, "identify_purgeable_files",
                      return_value={"purgeable": ["ags001.bin"],
                                    "retained": []}) as mock_identify, \
         patch.object(hamma_scrub, "purge_ags_files",
                      return_value=[{"filename": "ags001.bin",
                                     "status": "deleted",
                                     "error": None}]) as mock_purge:
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/home/pi/data",
            recover=True, purge=True,
        )

    mock_identify.assert_called_once()
    mock_purge.assert_called_once()
    assert rc == hamma_scrub.EXIT_OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestMain::test_run_with_purge_calls_purge -v`
Expected: FAIL — mock_identify.assert_called_once() fails (never called)

- [ ] **Step 3: Wire purge flow into `run()`**

In `run()`, after the recovery flow block (after line 1323), add the purge flow. The key points:

1. Update `mj_headers` with recovered headers (outside the recovery conditional)
2. Purge path runs if `purge` is True, regardless of whether recovery ran

```python
# Update mj_headers with recovered triggers
if recovery_results:
    for r in recovery_results:
        if r["status"] == "recovered":
            mj["headers"].add(r["header"])

# Purge flow
purge_results = None
if purge:
    eligibility = identify_purgeable_files(
        ags["entries"], mj["headers"], recovery_results,
    )
    if eligibility["purgeable"]:
        purge_deletions = purge_ags_files(
            ags_host, ags_path, eligibility["purgeable"],
            dry_run=dry_run,
        )
    else:
        purge_deletions = []

    deleted_names = [d["filename"] for d in purge_deletions
                     if d["status"] == "deleted"]
    dry_names = [d["filename"] for d in purge_deletions
                 if d["status"] == "dry_run"]
    failed_purge = [d for d in purge_deletions
                    if d["status"] == "failed"]

    if deleted_names:
        logger.info("Purge: deleted %d AGS files", len(deleted_names))
    if failed_purge:
        logger.warning("Purge: %d deletions failed", len(failed_purge))

    # Normalize to flat filename lists for reports (matching spec JSON shape)
    purge_results = {
        "deleted": [d["filename"] for d in purge_deletions
                    if d["status"] in ("deleted", "dry_run")],
        "failed": [{"filename": d["filename"], "error": d["error"]}
                   for d in purge_deletions if d["status"] == "failed"],
        "retained": eligibility["retained"],
        "dry_run": dry_run,
    }
```

Then update the report calls to pass `purge=purge_results`. Change the existing report lines:

```python
if json_output:
    print(format_json_report(results, ags_host,
                             recovery=recovery_results,
                             purge=purge_results))
else:
    print(format_human_report(results, limit=limit,
                              recovery=recovery_results,
                              purge=purge_results))

if output_file:
    with open(output_file, 'w') as f:
        f.write(format_json_report(results, ags_host,
                                   recovery=recovery_results,
                                   purge=purge_results))
    logger.info("JSON report written to %s", output_file)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py::TestMain::test_run_with_purge_calls_purge -v`
Expected: PASS

- [ ] **Step 5: Write additional integration tests**

Add to `TestMain`:

```python
def test_run_without_purge_no_purge(self, hamma_scrub):
    """run() without purge=True does not call purge functions."""
    h1 = b'\x01' * 128
    ags_result = {
        "entries": [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        ],
        "headers": {h1},
        "duplicate_count": 0,
        "elapsed": 1.0,
    }
    mj_result = {
        "headers": {h1},
        "file_count": 1,
        "duplicate_count": 0,
        "skipped": 0,
        "dirs_skipped": 0,
        "elapsed": 0.5,
    }

    with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
         patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
         patch.object(hamma_scrub, "identify_purgeable_files") as mock_identify, \
         patch.object(hamma_scrub, "purge_ags_files") as mock_purge:
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/home/pi/data",
            recover=True, purge=False,
        )

    mock_identify.assert_not_called()
    mock_purge.assert_not_called()

def test_run_purge_no_missing_still_purges(self, hamma_scrub):
    """Purge runs even when there are no missing triggers."""
    h1 = b'\x01' * 128
    ags_result = {
        "entries": [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ],
        "headers": {h1},
        "duplicate_count": 1,
        "elapsed": 1.0,
    }
    mj_result = {
        "headers": {h1},
        "file_count": 1,
        "duplicate_count": 0,
        "skipped": 0,
        "dirs_skipped": 0,
        "elapsed": 0.5,
    }

    with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
         patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
         patch.object(hamma_scrub, "identify_purgeable_files",
                      return_value={"purgeable": ["ags001.bin"],
                                    "retained": [{"filename": "ags002.bin",
                                                   "reason": "active file"}]}) as mock_identify, \
         patch.object(hamma_scrub, "purge_ags_files",
                      return_value=[{"filename": "ags001.bin",
                                     "status": "deleted",
                                     "error": None}]) as mock_purge:
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/home/pi/data",
            recover=True, purge=True,
        )

    # Purge was called even though no triggers were missing
    mock_identify.assert_called_once()
    mock_purge.assert_called_once()
```

- [ ] **Step 6: Run full test suite**

Run: `conda run -n sci pytest tests/python/test_hamma_scrub.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/hamma_scrub.py tests/python/test_hamma_scrub.py
git commit -m "feat(scrub): wire --purge into run() with mj_headers update and report output"
```
