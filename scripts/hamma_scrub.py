#!/usr/bin/env python3
"""Compare AGS trigger data against mjolnir .bin files.

Strides through AGS data files on the sensor (via SSH), extracts 128-byte
headers, and compares against local mjolnir .bin file headers to detect
missing triggers.

Related: https://github.com/hamma-dev/mjolnir-hamma/issues/20

Usage:
    python hamma_scrub.py [--ags-host HOST] [--ags-path PATH] [--verbose]
"""

# Standard library imports
import argparse
import contextlib
import csv
import glob
import io
import json
import logging
import math
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# HAMMA 2.0 packet constants
SYNC_MARKER = b'\xf5\xff\x50\x5d'
HEADER_SIZE = 128
PACKET_PAD = 4
EXPECTED_DATASIZE = 11000000  # words (x2 = bytes)
MAX_DATASIZE = 20000000  # words; above this is corruption
DATASIZE_OFFSET = 10  # byte offset of datasize field in header
DATASIZE_FORMAT = '<I'  # uint32 little-endian

# Defaults
DEFAULT_AGS_HOST = "hamma"
DEFAULT_AGS_PATH = "/ags/data"
DEFAULT_MJ_PATH = "/media/pi"
DRIVE_PATTERN = "DATA??"
DEFAULT_LIMIT = 20  # max missing trigger detail lines in human report (0 = no limit)

# Recovery constants
MIN_FREE_SPACE = 104857600  # 100MB minimum free space on target drive
RECOVER_TIMEOUT = 60  # seconds per dd extraction
ORPHAN_MAX_AGE = 3600  # seconds (1 hour) before orphaned temps are deleted

# SSH / throughput constants
SSH_CONNECT_TIMEOUT = 10  # seconds to establish an SSH connection (fail fast)
CONTROL_PERSIST = 60      # seconds the shared ControlMaster lingers after last use
PURGE_CHUNK_SIZE = 100    # AGS files deleted per batched `rm` (one SSH round-trip)
PURGE_TIMEOUT = 30        # seconds per batched delete chunk
# CPU-nice the scrub's heavy AGS-side commands (header scan, recover reads,
# purge) so they cannot preempt the DAS writer on the AGS Pi. Verified on the
# fleet: the DAS runs at CPU nice 19 (the floor) while an unniced remote command
# runs at nice 0 -- i.e. the scrub would OUTRANK the writer on CPU. `nice -n 19`
# demotes it to the writer's floor. No ionice: the AGS's active I/O scheduler is
# mq-deadline, which ignores ionice classes entirely (the DAS's "realtime" I/O
# prio is set but inert), so ionice here would be theatre. `nice` is coreutils,
# always present. The mj-pi side is deprioritised separately via the service
# unit's Nice= (files/hamma-scrub.service).
AGS_NICE = "nice -n 19 "
# Bounds the SCAN phase's contribution to lock-hold time -- NOT the whole scrub:
# recover is per-trigger (RECOVER_TIMEOUT) and purge per-chunk (PURGE_TIMEOUT),
# so the aggregate scan+recover+purge lock-hold is NOT bounded by this alone.
# An auto-scrub runs under `flock -n`; the old 3600s scan cap let one hung scan
# stall the safety net for an hour. 600s is ~6x the observed worst case (~99s).
SCAN_TIMEOUT = 600        # seconds for the remote AGS strider scan

# Heartbeat/status file the scrub updates as it advances, so the monitor
# (state_monitor.check_scrub_health) can tell a working scrub from a hung one
# (progress, not just lock age) and safely recover only genuine hangs.
# On tmpfs (/dev/shm), NOT the SD root: the SD fills from logs during the exact
# incident (HAM-112/113), and a heartbeat that can't be written would make a
# healthy scrub look hung. tmpfs stays writable when the SD is full.
DEFAULT_STATUS_FILE = "/dev/shm/hamma_scrub_status.json"

# Incremental-scan cache: per-hourly-dir MJ header sets keyed by a cheap
# (mtime, .bin-count) signature, so unchanged dirs are reused instead of
# re-reading every file's header (the ~99s-under-load MJ scan). On tmpfs so it
# adds no SD wear; a reboot just costs one full scan.
DEFAULT_MJ_CACHE = "/dev/shm/hamma_scrub_mj_cache.json"

# Durable per-run CSV of MJ-scan cache performance (hit-rate over time). Unlike
# the /dev/shm status/cache, this lives on the SD so a cold scan (cache lost
# between runs) leaves a reviewable trail. Size-capped in-place (one .1
# generation) rather than relying on external logrotate -- keeps it bounded on
# the SD in line with the HAM-112/113 SD-fill stance, since nothing else rotates
# it (the sibling scrub_log is hand-rotated by state_monitor, not this file).
DEFAULT_METRICS_FILE = os.path.expanduser(
    "~/brokkr/hamma/log/scrub_metrics.csv")
SCAN_METRICS_MAX_BYTES = 1_000_000  # ~20k rows; rotate to .1 past this

# Exit codes
EXIT_OK = 0
EXIT_MISSING = 1
EXIT_SSH_ERROR = 2
EXIT_NO_DATA = 3

# GPS field offsets in raw HAMMA 2.0 header (little-endian)
GPS_TIME_WEEK_OFFSET = 80   # float32
GPS_WEEK_OFFSET = 84        # int16
GPS_UTC_OFFSET_OFFSET = 86  # float32
GPS_SUBSECOND_OFFSET = 94   # uint32
GPS_ECC_OFFSET = 98         # uint32
GPS_EPOCH = 315964800        # UTC epoch for GPS week 0


def ssh_cmd(host, remote_command, control_path=None):
    """Build an ssh command list for a remote command on the AGS.

    Adds ``BatchMode`` (never prompt) and ``ConnectTimeout`` (fail fast on a
    sick/unreachable AGS). When ``control_path`` is given, routes over an
    existing ControlMaster socket so many calls reuse one connection.

    Parameters
    ----------
    host : str
        SSH host (e.g. ``hamma``).
    remote_command : str
        The command to run on the remote host.
    control_path : str or None
        Path to a ControlMaster socket to reuse, or None for a fresh connection.

    Returns
    -------
    list of str
        The argv for ``subprocess.run``/``Popen``.
    """
    cmd = ["ssh", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout={}".format(SSH_CONNECT_TIMEOUT)]
    if control_path:
        cmd += ["-o", "ControlPath={}".format(control_path)]
    cmd += [host, remote_command]
    return cmd


def open_control_master(host):
    """Start a shared SSH ControlMaster to ``host``; return its socket path.

    Reusing the socket lets many ``ssh_cmd`` calls skip the per-connection
    handshake (~16x faster per round-trip, measured). The master lingers
    ``CONTROL_PERSIST`` seconds after the last use, so it self-closes even
    without an explicit teardown.

    Parameters
    ----------
    host : str
        SSH host (e.g. ``hamma``).

    Returns
    -------
    str or None
        ControlMaster socket path, or None if setup failed (callers then fall
        back to per-call connections transparently).
    """
    control_path = "/tmp/hamma_scrub_cm_{}.sock".format(os.getpid())
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout={}".format(SSH_CONNECT_TIMEOUT),
             "-o", "ControlMaster=yes",
             "-o", "ControlPersist={}".format(CONTROL_PERSIST),
             "-o", "ControlPath={}".format(control_path),
             host, "true"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=SSH_CONNECT_TIMEOUT + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("ControlMaster setup error (%s); using per-call SSH", e)
        return None
    if result.returncode != 0:
        logger.warning(
            "ControlMaster setup failed (%s); using per-call SSH",
            result.stderr.decode('utf-8', errors='replace').strip())
        return None
    return control_path


def close_control_master(host, control_path):
    """Tear down a ControlMaster socket opened by ``open_control_master``."""
    if not control_path:
        return
    try:
        subprocess.run(
            ["ssh", "-o", "ControlPath={}".format(control_path),
             "-O", "exit", host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


@contextlib.contextmanager
def ssh_control_master(host):
    """Context-manager form of :func:`open_control_master` with teardown.

    Yields the socket path (or None if setup failed) and always closes the
    master on exit.
    """
    control_path = open_control_master(host)
    try:
        yield control_path
    finally:
        close_control_master(host, control_path)


def write_status(path, phase, **counts):
    """Atomically write the scrub heartbeat/status file (best-effort).

    Records ``pid``, ``phase``, a wall-clock ``timestamp`` (the heartbeat), and
    any running ``counts`` (e.g. recovered=, purged=). Written via temp-file +
    ``os.replace`` so a reader never sees a partial file. Failures are logged at
    debug and swallowed -- a heartbeat that can't be written must never crash
    or block the scrub. ``path`` of None is a no-op.

    Parameters
    ----------
    path : str or None
        Destination status file, or None to disable.
    phase : str
        Current phase: 'start', 'scan', 'recover', 'purge', 'done', 'error'.
    **counts
        Extra fields to record (e.g. recovered, purged, missing).
    """
    if not path:
        return
    payload = {"pid": os.getpid(), "phase": phase, "timestamp": time.time()}
    payload.update(counts)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not write status file %s: %s", path, e)


def extract_headers(fileobj, file_size, filename):
    """Extract 128-byte headers from a concatenated AGS data file.

    Parameters
    ----------
    fileobj : file-like
        Readable/seekable file object positioned at start.
    file_size : int
        Total file size (snapshot at open time).
    filename : str
        Filename for logging.

    Returns
    -------
    list of dict
        Each dict has keys: header (bytes), offset (int), index (int).
    """
    results = []
    pos = 0
    index = 0

    while pos + HEADER_SIZE <= file_size:
        fileobj.seek(pos)
        header = fileobj.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            logger.debug("%s: truncated read at offset %d, stopping", filename, pos)
            break

        # Verify sync marker
        if header[:4] != SYNC_MARKER:
            logger.warning(
                "%s: bad sync marker at offset %d (trigger %d), scanning forward",
                filename, pos, index,
            )
            pos = _scan_forward(fileobj, pos + 1, file_size)
            if pos < 0:
                break
            continue

        # Read datasize to compute stride
        datasize = struct.unpack_from(DATASIZE_FORMAT, header, DATASIZE_OFFSET)[0]
        if datasize == 0 or datasize > MAX_DATASIZE:
            logger.warning(
                "%s: datasize %d out of bounds at offset %d, scanning forward",
                filename, datasize, pos,
            )
            pos = _scan_forward(fileobj, pos + 1, file_size)
            if pos < 0:
                break
            continue

        results.append({
            "header": header,
            "offset": pos,
            "index": index,
        })

        # Advance past payload + padding to next header
        stride = HEADER_SIZE + datasize * 2 + PACKET_PAD
        pos += stride
        index += 1

    logger.debug("%s: extracted %d headers", filename, len(results))
    return results


def _scan_forward(fileobj, start_pos, file_size):
    """Scan forward from start_pos to find the next SYNC_MARKER.

    Returns the offset of the sync marker, or -1 if not found.
    """
    chunk_size = 4096
    pos = start_pos
    while pos + 4 <= file_size:
        fileobj.seek(pos)
        read_size = min(chunk_size, file_size - pos)
        chunk = fileobj.read(read_size)
        if not chunk:
            break
        idx = chunk.find(SYNC_MARKER)
        if idx >= 0:
            return pos + idx
        # Overlap by 3 bytes to catch sync spanning chunk boundary
        pos += len(chunk) - 3
    return -1


def _parse_since(since_str):
    """Parse a --since value into a comparable directory prefix.

    Parameters
    ----------
    since_str : str
        Date string: 'YYYY-MM-DD', 'YYYY-MM-DDTHH', or 'auto'.

    Returns
    -------
    str
        Normalized to 'YYYY-MM-DDTHH' format, or 'auto' sentinel.

    Raises
    ------
    ValueError
        If format is not recognized.
    """
    s = since_str.strip()
    if s.lower() == 'auto':
        return 'auto'
    # YYYY-MM-DDTHH (already has hour)
    if len(s) == 13 and s[10] == 'T':
        return s
    # YYYY-MM-DD (add T00 for start of day)
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s + 'T00'
    raise ValueError(
        "Invalid --since format '{}': expected YYYY-MM-DD, YYYY-MM-DDTHH, or auto".format(s)
    )


def _load_scan_cache(path):
    """Load the incremental MJ-scan cache; {} on any problem (safe fallback).

    JSON, NOT pickle: the cache lives on world-writable tmpfs (`/dev/shm` is
    mode 1777), so unpickling it would be a local code-execution vector as the
    scrub's user. JSON stores headers as hex and can never execute code on load.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {
            k: {"sig": tuple(v["sig"]),
                "headers": {bytes.fromhex(h) for h in v["headers"]},
                "file_count": v["file_count"],
                "skipped": v["skipped"]}
            for k, v in raw.items()
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save_scan_cache(path, cache):
    """Atomically persist the MJ-scan cache as JSON (best-effort; never raises)."""
    try:
        raw = {k: {"sig": list(v["sig"]),
                   "headers": sorted(h.hex() for h in v["headers"]),
                   "file_count": v["file_count"],
                   "skipped": v["skipped"]}
               for k, v in cache.items()}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        logger.debug("Could not write MJ-scan cache %s: %s", path, e)


def _read_dir_headers(subdir, bin_names):
    """Read the 128-byte header of each .bin in one hourly dir.

    Returns (headers set, file_count, skipped). Used by the incremental scanner
    for the dirs it must (re)read; the full scanner has its own inline read with
    identical per-file semantics (the parity test keeps them in lockstep).
    """
    headers = set()
    skipped = 0
    for name in bin_names:
        fp = os.path.join(subdir, name)
        try:
            if os.path.getsize(fp) < HEADER_SIZE:
                skipped += 1
                continue
            with open(fp, "rb") as f:
                header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                skipped += 1
            else:
                headers.add(header)
        except OSError as e:
            logger.warning("Error reading %s: %s", fp, e)
            skipped += 1
    return headers, len(bin_names), skipped


def _refresh_cache_dirs(cache_file, dirs):
    """Re-read the given hourly dirs and update their entries in the scan cache.

    The incremental cache is saved *during* the MJ scan, before the recover
    phase writes recovered .bin files. Those dirs are therefore stale in the
    cache (old sig + missing the new headers), so the next scan would re-read
    them. Refreshing them here -- after recovery -- lets the next scan cache-hit
    them instead. Best-effort: a missing cache, empty ``dirs``, or an unreadable
    dir is a silent no-op (the only cost of skipping is a re-read next run).
    """
    if not cache_file or not dirs:
        return
    cache = _load_scan_cache(cache_file)
    if not cache:
        return
    updated = False
    for subdir in dirs:
        try:
            bin_names = sorted(
                e for e in os.listdir(subdir) if e.endswith(".bin"))
            sig = (os.stat(subdir).st_mtime, len(bin_names))
        except OSError:
            continue
        dir_headers, dir_files, dir_skipped = _read_dir_headers(
            subdir, bin_names)
        cache[subdir] = {"sig": sig, "headers": dir_headers,
                         "file_count": dir_files, "skipped": dir_skipped}
        updated = True
    if updated:
        _save_scan_cache(cache_file, cache)


SCAN_METRICS_HEADER = (
    "utc,dirs_cached,dirs_total,cold,scan_seconds,recovered,purged")


def write_scan_metrics(path, mj, recovered, purged):
    """Append one CSV row summarizing this run's MJ-scan cache performance.

    Durable (unlike the /dev/shm status file), so cache hit-rate can be reviewed
    over time. A ``cold`` row (0 dirs cached over a large total) flags that the
    cache was lost between runs -- the signal to catch a real-world cache miss.
    When the cache is disabled (full scanner, no ``cache_hits``) the cache
    columns are left blank rather than reporting a bogus cold flag. ``path`` of
    None disables it; best-effort, never raises.
    """
    if not path:
        return
    try:
        hits = mj.get("cache_hits")
        total = mj.get("dirs_total")
        if hits is None or total is None:
            hits_s = total_s = cold_s = ""      # full scanner: no cache stats
        else:
            hits_s, total_s = str(hits), str(total)
            cold_s = "1" if (total > 0 and hits == 0) else "0"
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = "{},{},{},{},{},{},{}\n".format(
            utc, hits_s, total_s, cold_s,
            round(mj.get("elapsed", 0), 1), recovered, purged)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Bound the file: nothing external rotates it. Past the cap, move it to
        # .1 (one generation) and start fresh -- so the SD can't fill from it.
        try:
            if os.path.getsize(path) >= SCAN_METRICS_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        new_file = not os.path.exists(path)
        with open(path, "a") as f:
            if new_file:
                f.write(SCAN_METRICS_HEADER + "\n")
            f.write(row)
    except OSError as e:
        logger.debug("Could not write scan metrics %s: %s", path, e)


def scan_mj_files(base_path, since=None, cache_file=None, until=None):
    """Scan local mjolnir .bin files and collect headers.

    Dispatches to the incremental scanner when ``cache_file`` is given (reuses
    per-hourly-dir header sets whose ``(mtime, .bin-count)`` signature is
    unchanged -- the fix for the O(all-files) MJ scan), else the full scanner.

    ``since``/``until`` bound the hourly directories read, inclusive, as
    ``'YYYY-MM-DDTHH'``. Both scanners honour both bounds: silently ignoring
    ``until`` on one path would make the caller's restriction a lie.
    """
    if cache_file:
        return _scan_mj_incremental(base_path, since, cache_file, until=until)
    return _scan_mj_full(base_path, since, until=until)


def _scan_mj_incremental(base_path, since, cache_file, until=None):
    """MJ scan that re-reads only new/changed hourly dirs; reuses the rest.

    A directory whose ``(mtime, .bin-count)`` signature matches the cache is
    reused without touching its files. TWO safety rules make this sound despite
    brokkr append-writing .bin in place on 2 s-granularity vfat (so an in-place
    completion may leave the signature unchanged):
      1. the newest hourly dir per drive -- the one brokkr is actively
         appending to -- is ALWAYS re-read (never cache-hit);
      2. once an hour rolls over, brokkr writes the next hour's dir, so past
         dirs are immutable and safe to cache.
    The cache lives on tmpfs (no SD wear), self-prunes (dirs not seen are
    dropped), and falls back to a full re-read on any anomaly.
    """
    t0 = time.time()
    old_cache = _load_scan_cache(cache_file)
    new_cache = {}
    headers = set()
    file_count = skipped = dirs_skipped = cache_hits = 0

    drives = sorted(glob.glob(os.path.join(base_path, DRIVE_PATTERN)))
    if not drives:
        logger.info("No DATA drives found at %s", base_path)
    for drive in drives:
        try:
            names = sorted(os.listdir(drive))
        except OSError as e:
            logger.warning("Error scanning %s: %s, skipping", drive, e)
            continue
        # First pass: the qualifying hourly dirs (cheap; per-dir, not per-file).
        subdirs = []
        for name in names:
            subdir = os.path.join(drive, name)
            if name == "compressed" or not os.path.isdir(subdir):
                continue
            if since and name < since:
                dirs_skipped += 1
                continue
            if until and name > until:
                dirs_skipped += 1
                continue
            subdirs.append((name, subdir))
        newest = subdirs[-1][0] if subdirs else None  # names are sorted
        for name, subdir in subdirs:
            try:
                bin_names = sorted(
                    e for e in os.listdir(subdir) if e.endswith(".bin"))
                sig = (os.stat(subdir).st_mtime, len(bin_names))
            except OSError:
                continue
            cached = old_cache.get(subdir)
            # Force-read the actively-written newest dir (rule 1).
            if (name != newest and cached is not None
                    and cached.get("sig") == sig):
                dir_headers = cached["headers"]
                dir_files = cached["file_count"]
                dir_skipped = cached["skipped"]
                cache_hits += 1
            else:
                dir_headers, dir_files, dir_skipped = _read_dir_headers(
                    subdir, bin_names)
            new_cache[subdir] = {"sig": sig, "headers": dir_headers,
                                 "file_count": dir_files, "skipped": dir_skipped}
            headers |= dir_headers
            file_count += dir_files
            skipped += dir_skipped

    _save_scan_cache(cache_file, new_cache)
    elapsed = time.time() - t0
    # NOTE: this differs from _scan_mj_full's per-file dup count for headers
    # duplicated ACROSS hourly dirs; it is a log stat only, never a control input
    # (compare_headers/identify_purgeable_files use the `headers` set alone).
    duplicate_count = max(0, file_count - skipped - len(headers))
    logger.info("MJ scan (incremental): %d unique from %d files, "
                "%d/%d dirs cached (%.1fs)", len(headers), file_count,
                cache_hits, len(new_cache), elapsed)
    return {
        "headers": headers,
        "file_count": file_count,
        "duplicate_count": duplicate_count,
        # None, not []: a cached dir stores a header SET, so which header was
        # duplicated inside it is not recoverable. [] would read as "no
        # duplicates" and silently disarm the --audit-loss blocker, so the
        # audit refuses on None instead (and run() forces cache_file=None for
        # that path, making this unreachable from there).
        "duplicate_headers": None,
        "skipped": skipped,
        "dirs_skipped": dirs_skipped,
        "elapsed": elapsed,
        "cache_hits": cache_hits,
        "dirs_total": len(new_cache),
    }


def _scan_mj_full(base_path, since=None, until=None):
    """Scan local mjolnir .bin files and collect headers.

    Parameters
    ----------
    base_path : str
        Base path containing DATA?? drives (e.g., /media/pi).
    since : str or None
        If set, skip directories with names before this cutoff
        (format: 'YYYY-MM-DDTHH').
    until : str or None
        If set, skip directories with names after this cutoff (same format).
        Inclusive, like ``since``.

    Returns
    -------
    dict
        headers: set of bytes (128-byte raw headers)
        file_count: int (total .bin files found)
        duplicate_count: int (files with headers already seen)
        duplicate_headers: list of bytes (one per duplicate OCCURRENCE)
        skipped: int (files < 128 bytes)
        dirs_skipped: int (directories before --since cutoff)
        elapsed: float (seconds)
    """
    headers = set()
    file_count = 0
    # See scan_ags_files: the duplicate headers themselves, so --audit-loss can
    # place each one in (or outside) its window.
    duplicate_headers = []
    skipped = 0
    dirs_skipped = 0
    t0 = time.time()

    pattern = os.path.join(base_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        logger.info("No DATA drives found at %s", base_path)

    for drive in drives:
        try:
            if since or until:
                # Per-directory filtering: only glob .bin in qualifying dirs
                dir_pattern = os.path.join(drive, "*")
                subdirs = sorted(glob.glob(dir_pattern))
                bin_files = []
                for subdir in subdirs:
                    if not os.path.isdir(subdir):
                        continue
                    dirname = os.path.basename(subdir)
                    if since and dirname < since:
                        dirs_skipped += 1
                        continue
                    if until and dirname > until:
                        dirs_skipped += 1
                        continue
                    try:
                        bin_files.extend(
                            sorted(glob.glob(os.path.join(subdir, "*.bin")))
                        )
                    except OSError:
                        continue
            else:
                # Fast path: single glob for all .bin files
                bin_files = sorted(glob.glob(os.path.join(drive, "*", "*.bin")))
        except PermissionError:
            logger.warning("Permission denied scanning %s, skipping", drive)
            continue
        except OSError as e:
            logger.warning("Error scanning %s: %s, skipping", drive, e)
            continue

        for filepath in bin_files:
            file_count += 1
            try:
                fsize = os.path.getsize(filepath)
                if fsize < HEADER_SIZE:
                    logger.warning("Truncated file (%d bytes): %s", fsize, filepath)
                    skipped += 1
                    continue
                with open(filepath, 'rb') as f:
                    header = f.read(HEADER_SIZE)
                if len(header) < HEADER_SIZE:
                    skipped += 1
                    continue
                if header in headers:
                    duplicate_headers.append(header)
                else:
                    headers.add(header)
            except PermissionError:
                logger.warning("Permission denied reading %s", filepath)
                skipped += 1
            except OSError as e:
                logger.warning("Error reading %s: %s", filepath, e)
                skipped += 1

    elapsed = time.time() - t0
    if dirs_skipped:
        logger.info("MJ scan: skipped %d directories before --since cutoff",
                     dirs_skipped)
    logger.info("MJ scan: %d unique headers from %d files (%.1fs)",
                len(headers), file_count, elapsed)
    return {
        "headers": headers,
        "file_count": file_count,
        "duplicate_count": len(duplicate_headers),
        "duplicate_headers": duplicate_headers,
        "skipped": skipped,
        "dirs_skipped": dirs_skipped,
        "elapsed": elapsed,
    }


# The strider script runs on the AGS via SSH. It is a self-contained Python
# script that strides through AGS files and writes headers to stdout using
# a simple binary protocol.
#
# Protocol per trigger:
#   - filename (null-terminated UTF-8 string)
#   - offset (uint64 LE, 8 bytes)
#   - index (uint32 LE, 4 bytes)
#   - header (128 raw bytes)

STRIDER_SCRIPT = r'''
import glob, os, struct, sys
SYNC = b'\xf5\xff\x50\x5d'
HDR_SIZE = 128
PAD = 4
MAX_DS = 20000000

def scan_fwd(f, start, fsize):
    p = start
    while p + 4 <= fsize:
        f.seek(p)
        c = f.read(min(4096, fsize - p))
        if not c:
            break
        i = c.find(SYNC)
        if i >= 0:
            return p + i
        p += len(c) - 3
    return -1

data_path = sys.argv[1]
out = sys.stdout.buffer
for fpath in sorted(glob.glob(os.path.join(data_path, '*'))):
    fname = os.path.basename(fpath)
    try:
        fsize = os.path.getsize(fpath)
    except OSError:
        continue
    if fsize < HDR_SIZE:
        continue
    try:
        with open(fpath, 'rb') as f:
            pos = 0
            idx = 0
            while pos + HDR_SIZE <= fsize:
                f.seek(pos)
                hdr = f.read(HDR_SIZE)
                if len(hdr) < HDR_SIZE:
                    break
                if hdr[:4] != SYNC:
                    pos = scan_fwd(f, pos + 1, fsize)
                    if pos < 0:
                        break
                    continue
                ds = struct.unpack_from('<I', hdr, 10)[0]
                if ds == 0 or ds > MAX_DS:
                    pos = scan_fwd(f, pos + 1, fsize)
                    if pos < 0:
                        break
                    continue
                out.write(fname.encode('utf-8') + b'\x00')
                out.write(struct.pack('<Q', pos))
                out.write(struct.pack('<I', idx))
                out.write(hdr)
                pos += HDR_SIZE + ds * 2 + PAD
                idx += 1
    except OSError:
        continue
out.flush()
'''


def decode_strider_output(data):
    """Decode binary output from the remote strider script.

    Parameters
    ----------
    data : bytes
        Raw stdout from strider script.

    Returns
    -------
    list of dict
        Each dict has: filename (str), offset (int), index (int),
        header (bytes).
    """
    entries = []
    pos = 0
    while pos < len(data):
        # Read null-terminated filename
        null_pos = data.index(b'\x00', pos)
        filename = data[pos:null_pos].decode('utf-8')
        pos = null_pos + 1
        # Read offset (uint64) and index (uint32)
        offset = struct.unpack_from('<Q', data, pos)[0]
        pos += 8
        index = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        # Read header
        header = data[pos:pos + HEADER_SIZE]
        pos += HEADER_SIZE
        entries.append({
            "filename": filename,
            "offset": offset,
            "index": index,
            "header": header,
        })
    return entries


def scan_ags_files(ags_host, ags_path, control_path=None):
    """Run remote strider on AGS sensor and collect headers.

    Parameters
    ----------
    ags_host : str
        SSH host for AGS sensor.
    ags_path : str
        Path to AGS data directory on sensor.

    Returns
    -------
    dict
        entries: list of dict (filename, offset, index, header)
        headers: set of bytes (unique 128-byte headers)
        duplicate_count: int
        duplicate_headers: list of bytes (one per duplicate OCCURRENCE)
        elapsed: float (seconds)

    Raises
    ------
    RuntimeError
        If SSH connection fails.
    """
    t0 = time.time()

    # Deploy strider to AGS as a temp file, then run it. We avoid piping
    # the script via stdin (ssh host "python3 -") because that crashes
    # the AGS SSH daemon.
    remote_script = "/tmp/hamma_strider.py"
    local_tmp = None
    try:
        fd, local_tmp = tempfile.mkstemp(suffix='.py', prefix='hamma_strider_')
        os.write(fd, STRIDER_SCRIPT.encode('utf-8'))
        os.close(fd)

        scp_cmd = ["scp", "-q", local_tmp,
                   "{host}:{path}".format(host=ags_host, path=remote_script)]
        logger.debug("Deploying strider: %s", " ".join(scp_cmd))
        deploy = subprocess.run(
            scp_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if deploy.returncode != 0:
            stderr = deploy.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(
                "Failed to deploy strider to {host}: {err}".format(
                    host=ags_host, err=stderr,
                )
            )
    finally:
        if local_tmp is not None and os.path.exists(local_tmp):
            os.unlink(local_tmp)

    run_cmd = ssh_cmd(
        ags_host,
        AGS_NICE + "python3 {script} {path}; rm -f {script}".format(
            script=remote_script, path=ags_path),
        control_path=control_path)
    logger.debug("Running: %s", " ".join(run_cmd))

    result = subprocess.run(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=SCAN_TIMEOUT,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace').strip()
        raise RuntimeError(
            "SSH to {host} failed (rc={rc}): {err}".format(
                host=ags_host, rc=result.returncode, err=stderr,
            )
        )

    entries = decode_strider_output(result.stdout)

    headers = set()
    # The duplicate HEADERS, not merely a tally. --audit-loss has to know which
    # window each duplicate falls in, and the raw header bytes are what carries
    # the trigger time (see duplicates_in_window).
    duplicate_headers = []
    for entry in entries:
        if entry["header"] in headers:
            duplicate_headers.append(entry["header"])
        else:
            headers.add(entry["header"])
    duplicate_count = len(duplicate_headers)

    elapsed = time.time() - t0
    file_count = len(set(e["filename"] for e in entries))
    logger.info("AGS scan: %d unique headers from %d entries in %d files (%.1fs)",
                len(headers), len(entries), file_count, elapsed)

    if duplicate_count > 0:
        logger.warning(
            "AGS: %d duplicate headers detected (likely bad GPS)", duplicate_count
        )

    return {
        "entries": entries,
        "headers": headers,
        "duplicate_count": duplicate_count,
        "duplicate_headers": duplicate_headers,
        "elapsed": elapsed,
    }


def compare_headers(ags_entries, mj_headers):
    """Compare AGS entries against mjolnir header set.

    Parameters
    ----------
    ags_entries : list of dict
        From scan_ags_files, each with 'header', 'filename', 'offset', 'index'.
    mj_headers : set of bytes
        From scan_mj_files.

    Returns
    -------
    dict
        matched: int
        missing_on_mj: list of dict (entries not found on mj)
        mj_only_count: int
    """
    ags_header_set = set()
    missing_on_mj = []
    matched = 0

    for entry in ags_entries:
        hdr = entry["header"]
        ags_header_set.add(hdr)
        if hdr in mj_headers:
            matched += 1
        else:
            missing_on_mj.append(entry)

    mj_only_count = len(mj_headers - ags_header_set)

    return {
        "matched": matched,
        "missing_on_mj": missing_on_mj,
        "mj_only_count": mj_only_count,
    }


def decode_gps_time(header):
    """Decode GPS trigger time from a raw 128-byte header.

    Parameters
    ----------
    header : bytes
        Raw 128-byte HAMMA 2.0 header.

    Returns
    -------
    str or None
        ISO 8601 timestamp at millisecond precision, or None if invalid.
    """
    try:
        time_of_week = struct.unpack_from('<f', header, GPS_TIME_WEEK_OFFSET)[0]
        week_num = struct.unpack_from('<h', header, GPS_WEEK_OFFSET)[0]
        utc_offset = struct.unpack_from('<f', header, GPS_UTC_OFFSET_OFFSET)[0]
        subsecond = struct.unpack_from('<I', header, GPS_SUBSECOND_OFFSET)[0]
        ecc = struct.unpack_from('<I', header, GPS_ECC_OFFSET)[0]
    except struct.error:
        return None

    # Bad-GPS records carry NaN/inf in the float fields, and the struct.error
    # guard above catches neither: math.floor() raises ValueError on NaN and
    # OverflowError on inf, from OUTSIDE the try below. One such record
    # therefore raises out of decode_gps_time() entirely.
    #
    # Observed: 8 such headers on mj08's data drive, which aborted a full-drive
    # header walk. The scheduled scrub is less exposed -- `--since auto` only
    # decodes the FIRST trigger of each AGS file -- but the same call sits on
    # the recovery write path (compute_target_path) and in both report
    # formatters, so a single bad record can take down a recover/purge run.
    # Such records now decode to None and land under the existing "unknown/"
    # target prefix, which the MJ scanners already sort above any date dir.
    if not (math.isfinite(time_of_week) and math.isfinite(utc_offset)):
        return None

    # Compute base time (seconds since Unix epoch)
    # Matches hamma version20 convert(): passes floor(gpsTimeWeek)+1 to base_trigger_time
    base_time = (GPS_EPOCH
                 + int(week_num) * 604800
                 + math.floor(time_of_week) + 1
                 - float(utc_offset))

    # Guard for zero ECC (use 1GHz default like hamma package)
    if ecc == 0:
        ecc_val = 1000000000
    else:
        ecc_val = ecc
    sub_seconds = float(subsecond) / float(ecc_val)

    try:
        ts = base_time + sub_seconds
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.year < 2000:
            return None
        return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}'.format(dt.microsecond // 1000)
    except (ValueError, OverflowError, OSError):
        return None


def earliest_ags_timestamp(ags_entries):
    """Find the earliest valid GPS timestamp across all AGS entries.

    Groups entries by filename, finds the first valid GPS timestamp
    in each file (triggers within a file are sequential, so the first
    valid one is the earliest), and returns the minimum across all files.

    Parameters
    ----------
    ags_entries : list of dict
        From scan_ags_files(), each with 'header', 'filename', 'offset',
        'index'.

    Returns
    -------
    str or None
        'YYYY-MM-DDTHH' cutoff, or None if no valid GPS found.
    """
    if not ags_entries:
        return None

    # Group entries by filename
    files = {}
    for entry in ags_entries:
        files.setdefault(entry["filename"], []).append(entry)

    earliest = None
    for filename in sorted(files):
        # Sort by index within file (triggers are sequential)
        triggers = sorted(files[filename], key=lambda e: e["index"])
        for trigger in triggers:
            timestamp = decode_gps_time(trigger["header"])
            if timestamp is not None:
                cutoff = timestamp[:13]  # YYYY-MM-DDTHH
                if earliest is None or cutoff < earliest:
                    earliest = cutoff
                logger.debug("Auto-detect: %s first valid GPS at %s",
                             filename, cutoff)
                break  # First valid in file = earliest in file

    if earliest:
        logger.info("Auto-detect: earliest AGS trigger at %s", earliest)
    return earliest


def detect_unit_name(hostname=None):
    """Detect unit prefix and number from hostname.

    Parameters
    ----------
    hostname : str or None
        Override hostname for testing. If None, uses socket.gethostname().

    Returns
    -------
    tuple of (str, str)
        (prefix, unit) e.g. ("mj", "41"). Falls back to ("recovered", "").
    """
    if hostname is None:
        hostname = socket.gethostname()
    match = re.match(r'^mjolnir(\d+)$', hostname)
    if match:
        return ("mj", match.group(1))
    return ("recovered", "")


def compute_target_path(header, offset, prefix, unit):
    """Compute target directory and filename for a recovered trigger.

    Parameters
    ----------
    header : bytes
        128-byte raw header.
    offset : int
        Byte offset in source AGS file (discriminator for bad GPS filenames).
    prefix : str
        Unit prefix (e.g., "mj").
    unit : str
        Unit number string (e.g., "41").

    Returns
    -------
    tuple of (str, str)
        (subdirectory, filename). Subdirectory is 'YYYY-MM-DDTHH' or 'unknown'.
    """
    gps_str = decode_gps_time(header)
    unit_tag = "{}{}".format(prefix, unit) if unit else prefix

    if gps_str is None:
        subdir = "unknown"
        filename = "{}_0000-00-00_00-00-00-000_off{}_recovered.bin".format(
            unit_tag, offset,
        )
    else:
        # gps_str is "YYYY-MM-DDTHH:MM:SS.mmm"
        subdir = gps_str[:13]  # "YYYY-MM-DDTHH"
        # Convert to filename: "YYYY-MM-DD_HH-MM-SS-mmm"
        ts = gps_str[0:10] + '_' + gps_str[11:].replace(':', '-').replace('.', '-')
        filename = "{}_{}_recovered.bin".format(unit_tag, ts)

    return (subdir, filename)


def select_target_drive(mj_path, min_free=MIN_FREE_SPACE):
    """Select the best DATA drive for writing recovered triggers.

    Picks the drive containing the most recent hourly directory.
    Falls back to next drive with sufficient free space.

    Parameters
    ----------
    mj_path : str
        Base path (e.g., /media/pi).
    min_free : int
        Minimum free bytes required (default: MIN_FREE_SPACE).

    Returns
    -------
    str or None
        Full path to selected DATA drive, or None if no suitable drive.
    """
    pattern = os.path.join(mj_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        return None

    drive_info = []
    for drive in drives:
        most_recent = ""
        try:
            for entry in os.listdir(drive):
                if entry == "compressed":
                    continue
                full = os.path.join(drive, entry)
                if os.path.isdir(full) and entry > most_recent:
                    most_recent = entry
        except OSError:
            continue
        drive_info.append((drive, most_recent))

    # Sort by most recent directory descending
    drive_info.sort(key=lambda x: x[1], reverse=True)

    for drive, _ in drive_info:
        try:
            usage = shutil.disk_usage(drive)
            if usage.free >= min_free:
                return drive
        except OSError:
            continue

    return None


def extract_trigger(ags_host, ags_path, filename, offset, size,
                    control_path=None):
    """Extract a single trigger from AGS via SSH dd.

    Parameters
    ----------
    ags_host : str
        SSH host for AGS sensor.
    ags_path : str
        AGS data directory on sensor.
    filename : str
        AGS filename (basename).
    offset : int
        Byte offset in file.
    size : int
        Total bytes to extract (header + payload + padding).
    control_path : str or None
        ControlMaster socket to reuse for the SSH call.

    Returns
    -------
    bytes or None
        Extracted data, or None on failure.
    """
    filepath = "{}/{}".format(ags_path, filename)
    dd_cmd = AGS_NICE + (
        "dd if={} iflag=skip_bytes,count_bytes bs=4096"
        " skip={} count={} status=none"
    ).format(filepath, offset, size)
    cmd = ssh_cmd(ags_host, dd_cmd, control_path=control_path)
    logger.debug("Extracting: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RECOVER_TIMEOUT,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace').strip()
            logger.warning(
                "dd failed for %s offset %d (rc=%d): %s",
                filename, offset, result.returncode, stderr,
            )
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning(
            "dd timed out after %ds for %s offset %d",
            RECOVER_TIMEOUT, filename, offset,
        )
        return None
    except OSError as e:
        logger.warning("SSH error extracting %s: %s", filename, e)
        return None


def verify_trigger(data, expected_size):
    """Verify extracted trigger data integrity.

    Parameters
    ----------
    data : bytes
        Extracted trigger data.
    expected_size : int
        Expected byte count.

    Returns
    -------
    tuple of (bool, str)
        (success, error_message). Error message is empty on success.
    """
    if len(data) != expected_size:
        return (False, "size mismatch: got {} expected {}".format(
            len(data), expected_size,
        ))
    if data[:4] != SYNC_MARKER:
        return (False, "sync marker mismatch: got {}".format(data[:4].hex()))
    return (True, "")


def filter_recovery_candidates(missing_entries, ags_entries, since_cutoff=None):
    """Filter missing entries to determine which should be recovered.

    Skips the last trigger in the lexicographically newest AGS file
    (may be actively written) and triggers with GPS time before the
    --since cutoff.

    Parameters
    ----------
    missing_entries : list of dict
        Missing entries from compare_headers().
    ags_entries : list of dict
        All AGS entries (to identify active file).
    since_cutoff : str or None
        Normalized since cutoff ('YYYY-MM-DDTHH').

    Returns
    -------
    list of dict
        Each dict is a copy of the missing entry with added keys:
        'skip_reason' (None or string), 'skip_status' (None, 'skipped',
        or 'skipped_before_since').
    """
    # Identify last trigger in newest AGS file
    active_trigger = None
    if ags_entries:
        newest_file = max(e["filename"] for e in ags_entries)
        newest_entries = [e for e in ags_entries if e["filename"] == newest_file]
        if newest_entries:
            last = max(newest_entries, key=lambda e: e["offset"])
            active_trigger = (last["filename"], last["offset"])

    results = []
    for entry in missing_entries:
        entry_copy = dict(entry)
        key = (entry["filename"], entry["offset"])

        if active_trigger and key == active_trigger:
            entry_copy["skip_reason"] = "last trigger in active file"
            entry_copy["skip_status"] = "skipped"
            results.append(entry_copy)
            continue

        if since_cutoff:
            gps_str = decode_gps_time(entry["header"])
            if gps_str is not None:
                gps_dir = gps_str[:13]
                if gps_dir < since_cutoff:
                    entry_copy["skip_reason"] = "before --since cutoff"
                    entry_copy["skip_status"] = "skipped_before_since"
                    results.append(entry_copy)
                    continue

        entry_copy["skip_reason"] = None
        entry_copy["skip_status"] = None
        results.append(entry_copy)

    return results


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


def purge_ags_files(ags_host, ags_path, filenames, dry_run=False,
                    control_path=None, status_file=None):
    """Delete AGS files via SSH, batched over one connection.

    Files are deleted in chunks of ``PURGE_CHUNK_SIZE`` — a single
    ``rm -f f1 f2 ...`` per chunk (one SSH round-trip) rather than one SSH per
    file. Reusing a ControlMaster socket (``control_path``) collapses the
    per-file cost by ~16x; batching collapses the round-trip count. Chunk
    status maps to every file in the chunk (delete succeeds/fails as a unit).

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
    control_path : str or None
        ControlMaster socket to reuse for the SSH calls.
    status_file : str or None
        If given, write a ``purge``-phase heartbeat (``purged``/``total``) to
        this status file after each chunk, so a long purge advances the progress
        token and the monitor's hung-scrub detector does not misjudge it as stuck.

    Returns
    -------
    list of dict
        Each with filename, status ('deleted', 'failed', 'dry_run'),
        and optional error.
    """
    results = []

    def _record(chunk, status, error):
        for fname in chunk:
            results.append({"filename": fname, "status": status, "error": error})

    for start in range(0, len(filenames), PURGE_CHUNK_SIZE):
        chunk = filenames[start:start + PURGE_CHUNK_SIZE]
        # Per-chunk heartbeat: purge over a wedging SSH pipe can take a while;
        # advancing the heartbeat here lets the monitor's hung-scrub detector
        # tell a working purge from a stalled one (else a long purge could look
        # hung and be killed).
        write_status(status_file, "purge",
                     purged=sum(1 for r in results if r["status"] == "deleted"),
                     total=len(filenames))

        if dry_run:
            for fname in chunk:
                logger.info("Would delete: %s:%s/%s", ags_host, ags_path, fname)
            _record(chunk, "dry_run", None)
            continue

        remote_paths = [
            shlex.quote("{}/{}".format(ags_path, fname)) for fname in chunk
        ]
        rm_command = AGS_NICE + "rm -f " + " ".join(remote_paths)
        cmd = ssh_cmd(ags_host, rm_command, control_path=control_path)
        logger.info("Deleting %d AGS file(s) on %s", len(chunk), ags_host)
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=PURGE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timeout deleting %d file(s) on %s",
                           len(chunk), ags_host)
            _record(chunk, "failed", "SSH timeout ({}s)".format(PURGE_TIMEOUT))
            continue

        if result.returncode != 0:
            # `rm -f f1..fN` exits non-zero if ANY file errored, but it still
            # deleted the others. Marking the whole chunk "failed" would lie
            # (the report would under-count deletions during a disk-fill).
            # Retry per-file to attribute status correctly -- only failing
            # chunks pay the per-file cost; healthy chunks stay batched-fast.
            stderr = result.stderr.decode('utf-8', errors='replace').strip()
            logger.warning("Batched delete on %s returned non-zero (%s); "
                           "retrying per-file to attribute status",
                           ags_host, stderr)
            for fname, quoted in zip(chunk, remote_paths):
                one_cmd = ssh_cmd(ags_host, AGS_NICE + "rm -f " + quoted,
                                  control_path=control_path)
                try:
                    one = subprocess.run(
                        one_cmd, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=PURGE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    results.append({"filename": fname, "status": "failed",
                                    "error": "SSH timeout ({}s)".format(
                                        PURGE_TIMEOUT)})
                    continue
                if one.returncode == 0:
                    results.append({"filename": fname, "status": "deleted",
                                    "error": None})
                else:
                    results.append({
                        "filename": fname, "status": "failed",
                        "error": one.stderr.decode(
                            'utf-8', errors='replace').strip()})
        else:
            _record(chunk, "deleted", None)

    # Final heartbeat: the per-chunk beat fires BEFORE its chunk's rm, so the
    # last chunk's deletions aren't reflected until here. Write the completed
    # count so the durable status is accurate before the 'done' phase.
    write_status(status_file, "purge",
                 purged=sum(1 for r in results if r["status"] == "deleted"),
                 total=len(filenames))

    return results


def cleanup_orphaned_temps(mj_path, max_age=ORPHAN_MAX_AGE):
    """Delete orphaned .tmp_recover_*.bin files older than max_age.

    Parameters
    ----------
    mj_path : str
        Base path containing DATA drives.
    max_age : int
        Maximum age in seconds before deletion (default: 1 hour).

    Returns
    -------
    int
        Number of files deleted.
    """
    count = 0
    now = time.time()
    for drive in glob.glob(os.path.join(mj_path, DRIVE_PATTERN)):
        for tmp_file in glob.glob(os.path.join(drive, ".tmp_recover_*.bin")):
            try:
                mtime = os.path.getmtime(tmp_file)
                if now - mtime > max_age:
                    os.unlink(tmp_file)
                    logger.info("Cleaned orphaned temp: %s", tmp_file)
                    count += 1
            except OSError:
                continue
    return count


def recover_triggers(candidates, ags_host, ags_path, mj_path, dry_run=False,
                     control_path=None, status_file=None):
    """Recover missing triggers from AGS to MJ DATA drives.

    Parameters
    ----------
    candidates : list of dict
        From filter_recovery_candidates(), each with 'skip_reason' and
        'skip_status' keys.
    ags_host : str
        SSH host for AGS sensor.
    ags_path : str
        AGS data directory on sensor.
    mj_path : str
        Base path for DATA drives.
    dry_run : bool
        If True, report what would be recovered without transferring.

    Returns
    -------
    list of dict
        Each with keys: source_file, source_offset, trigger_index,
        target_path, size, status, error.
    """
    prefix, unit = detect_unit_name()
    results = []

    for candidate in candidates:
        # Heartbeat per trigger: recover is the long phase, so this is the
        # granularity the monitor needs to tell "grinding" from "hung".
        write_status(status_file, "recover", recovered=len(
            [r for r in results if r["status"] == "recovered"]),
            total=len(candidates))
        src_file = candidate["filename"]
        src_offset = candidate["offset"]
        trig_idx = candidate["index"]

        # Handle skipped candidates
        if candidate["skip_reason"]:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": None,
                "size": 0,
                "status": candidate["skip_status"],
                "error": candidate["skip_reason"],
                "header": candidate["header"],
            })
            continue

        # Compute extraction size from header
        datasize = struct.unpack_from(
            DATASIZE_FORMAT, candidate["header"], DATASIZE_OFFSET,
        )[0]
        size = HEADER_SIZE + datasize * 2 + PACKET_PAD

        # Compute target path
        subdir, filename = compute_target_path(
            candidate["header"], src_offset, prefix, unit,
        )

        # Select drive (re-check free space per trigger)
        drive = select_target_drive(mj_path)
        if drive is None:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": None,
                "size": size,
                "status": "failed",
                "error": "no drive with sufficient free space",
                "header": candidate["header"],
            })
            continue

        target_dir = os.path.join(drive, subdir)
        target_path = os.path.join(target_dir, filename)
        rel_target = os.path.relpath(target_path, mj_path)

        if dry_run:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "dry_run",
                "error": None,
                "header": candidate["header"],
            })
            continue

        # Check if already exists (idempotent)
        if os.path.exists(target_path):
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "skipped",
                "error": "file already exists",
                "header": candidate["header"],
            })
            continue

        # Extract trigger via SSH dd
        data = extract_trigger(ags_host, ags_path, src_file, src_offset, size,
                               control_path=control_path)
        if data is None:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "failed",
                "error": "dd extraction failed",
                "header": candidate["header"],
            })
            continue

        # Verify extracted data
        ok, err = verify_trigger(data, size)
        if not ok:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "failed",
                "error": err,
                "header": candidate["header"],
            })
            continue

        # Atomic write: temp file on target drive, then rename
        try:
            os.makedirs(target_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".tmp_recover_", suffix=".bin", dir=drive,
            )
            try:
                os.write(fd, data)
                os.close(fd)
                fd = None
                # Race check: another process may have created the file
                if os.path.exists(target_path):
                    os.unlink(tmp_path)
                    results.append({
                        "source_file": src_file,
                        "source_offset": src_offset,
                        "trigger_index": trig_idx,
                        "target_path": rel_target,
                        "size": size,
                        "status": "skipped",
                        "error": "file already exists",
                        "header": candidate["header"],
                    })
                    continue
                os.rename(tmp_path, target_path)
            except Exception:
                if fd is not None:
                    os.close(fd)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "recovered",
                "error": None,
                "header": candidate["header"],
            })
            logger.info("Recovered: %s", rel_target)

        except OSError as e:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "failed",
                "error": str(e),
                "header": candidate["header"],
            })

    return results


def format_human_report(results, limit=DEFAULT_LIMIT, recovery=None, purge=None):
    """Format results as human-readable report text.

    Parameters
    ----------
    results : dict
        Combined results from scanning and comparison.
    limit : int
        Max missing trigger detail lines to show (0 = no limit).
    recovery : list or None
        Recovery result records from recover_triggers(), or None if recovery
        was not performed.
    purge : dict or None
        Purge result dict from purge_ags_files(), or None if purge was not
        performed.

    Returns
    -------
    str
    """
    lines = []
    lines.append("AGS Data Scrub Report")
    lines.append("=" * len("AGS Data Scrub Report"))
    lines.append("AGS scan: {:,} triggers from {:,} files ({:.1f}s)".format(
        results["ags_triggers"], results["ags_files"], results["ags_elapsed"],
    ))
    lines.append("MJ scan:  {:,} unique triggers from {:,} files ({:.1f}s)".format(
        results["mj_triggers"], results["mj_files_scanned"],
        results["mj_elapsed"],
    ))
    if results["mj_duplicate_count"] > 0:
        lines.append("MJ duplicate headers: {:,}".format(
            results["mj_duplicate_count"],
        ))
    lines.append("Matched:  {:,}".format(results["matched"]))
    lines.append("")

    missing = results["missing_on_mj"]
    if missing:
        lines.append("Missing on MJ (potential data loss): {:,}".format(len(missing)))
        show = missing if limit == 0 else missing[:limit]
        for entry in show:
            gps = decode_gps_time(entry["header"])
            time_str = gps if gps else "bad GPS"
            lines.append("  AGS file: {}, trigger #{}, GPS time: {}".format(
                entry["filename"], entry["index"], time_str,
            ))
        if limit > 0 and len(missing) > limit:
            lines.append("  ... and {:,} more (use --limit 0 to show all)".format(
                len(missing) - limit,
            ))
    else:
        lines.append("No missing triggers detected.")

    lines.append("")
    lines.append("On MJ only (expected, AGS drops under load): {:,}".format(
        results["mj_only_count"],
    ))

    for warning in results.get("warnings", []):
        lines.append("WARNING: {}".format(warning))

    # Recovery section (only when recovery was performed)
    if recovery is not None:
        lines.append("")
        is_dry = any(r["status"] == "dry_run" for r in recovery)
        if is_dry:
            count = len([r for r in recovery if r["status"] == "dry_run"])
            lines.append("Recovery (dry run): {} triggers would be recovered".format(count))
            for r in recovery:
                if r["status"] == "dry_run":
                    lines.append("  Would recover: {}".format(r["target_path"]))
        else:
            attempted = len([r for r in recovery
                             if r["status"] not in ("skipped", "skipped_before_since")])
            succeeded = len([r for r in recovery if r["status"] == "recovered"])
            failed = len([r for r in recovery if r["status"] == "failed"])
            lines.append("Recovery: {} attempted, {} succeeded, {} failed".format(
                attempted, succeeded, failed,
            ))
            for r in recovery:
                if r["status"] == "recovered":
                    lines.append("  Recovered: {}".format(r["target_path"]))
                elif r["status"] == "failed":
                    lines.append("  FAILED: {} trigger #{} \u2014 {}".format(
                        r["source_file"], r["trigger_index"], r["error"],
                    ))
                elif r["status"] == "skipped" and r.get("error") == "file already exists":
                    lines.append("  Skipped (exists): {}".format(r["target_path"]))

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

    return "\n".join(lines)


def format_json_report(results, ags_host, recovery=None, purge=None):
    """Format results as JSON string.

    Parameters
    ----------
    results : dict
        Combined results from scanning and comparison.
    ags_host : str
        AGS host for metadata.
    recovery : list or None
        Recovery result records from recover_triggers(), or None if recovery
        was not performed.
    purge : dict or None
        Purge result dict from purge_ags_files(), or None if purge was not
        performed.

    Returns
    -------
    str
        JSON string.
    """
    missing_entries = []
    for entry in results["missing_on_mj"]:
        gps = decode_gps_time(entry["header"])
        missing_entries.append({
            "ags_file": entry["filename"],
            "ags_offset": entry["offset"],
            "trigger_index": entry["index"],
            "gps_time": gps,
        })

    report = {
        "scan_time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "ags_host": ags_host,
        "ags_triggers": results["ags_triggers"],
        "ags_files": results["ags_files"],
        "mj_triggers": results["mj_triggers"],
        "mj_files_scanned": results["mj_files_scanned"],
        "mj_duplicate_headers": results["mj_duplicate_count"],
        "matched": results["matched"],
        "missing_on_mj": missing_entries,
        "mj_only_count": results["mj_only_count"],
        "warnings": results.get("warnings", []),
    }
    if recovery is not None:
        # Strip binary header bytes — not JSON-serializable
        report["recovery"] = [
            {k: v for k, v in r.items() if k != "header"}
            for r in recovery
        ]
    if purge is not None:
        report["purge"] = purge
    return json.dumps(report, indent=2)


# ---------------------------------------------------------------------------
# HAM-164 loss reconciliation
#
#     lost = delta valid_packets  -  |H_ags  union  H_mj|
#
# `valid_packets` counts records the FPGA produced and the AGS CRC-validated.
# It increments AFTER both sinks are offered the packet (SciencePacketParser),
# so it is an upstream reference independent of whether either sink accepted.
# H_ags / H_mj are the raw 128-byte record headers found on the AGS stick and
# the mjolnir data drives.
#
# This exists because `packets_dropped == 0` is NOT a sound acceptance
# criterion: ThreadedPacketProcessor only counts a drop when the writer is
# busy, so a unit whose stream is merely DISCONNECTED reports a perfect score
# while shedding every record.
#
# IDENTITY: records are matched on the RAW HEADER BYTES. Trigger time is used
# only to place a record in the window. The header is NOT guaranteed unique --
# per-record entropy is essentially the GPS fields, and when GPS freezes many
# records collapse to one header (this file already logs that: see the
# "duplicate headers detected" warnings in the scanners). Duplicates make the
# union UNDERCOUNT and therefore OVERSTATE loss, so a non-zero duplicate count
# is a blocker, not a footnote.
#
# THE PRECONDITION -- why both boundaries must be quiet:
# the window bounds come from brokkr telemetry `time` (the PI's wall clock),
# while the header set is keyed to GPS TRIGGER time, and `valid_packets`
# increments only when the last byte of a ~22 MB record has arrived -- >=905 ms
# after the trigger, plus transfer, plus up to 1 s of H&S emission phase, plus
# socket wait. So delta_valid counts triggers in (B0-L, B1-L] while the header
# set holds [B0, B1), with L ~ 1-3 s and NOT constant. The residual therefore
# carries a term N(B0-L, B0) - N(B1-L, B1) which is pure noise unless both
# boundaries are QUIET. Rather than loosen the predicate to "small is fine" --
# which trains operators to wave real loss through -- we measure activity
# around each boundary and REFUSE TO CERTIFY when it is non-zero.
#
# BLIND SPOT: valid_packets counts only CRC-VALID records, so loss upstream of
# the parser (FPGA FIFO overflow -> CRC failure) is in neither side of the
# equation. Track crc_errors and the wrap-aware fifo_overflow delta separately.
# ---------------------------------------------------------------------------

DEFAULT_TELEMETRY_DIR = os.path.expanduser("~/brokkr/hamma/telemetry")

# Derived, not restated: a record is header + payload + CRC pad. Reading this
# from the existing constants means it tracks if the datasize assumption moves.
RECORD_BYTES = HEADER_SIZE + EXPECTED_DATASIZE * 2 + PACKET_PAD

# bytes_written is reported in GB (base-10) in the telemetry CSV.
BYTES_WRITTEN_SCALE = 1e9

# Seconds either side of a boundary that must contain no records. Covers the
# trigger -> valid_packets latency L (~1-3 s) with margin.
DEFAULT_EDGE_SECONDS = 10.0

# How far the snapped telemetry row may sit from the requested bound before the
# result is considered to describe a different window than the one asked for.
DEFAULT_BOUND_TOLERANCE_S = 120.0

# How far a record's GPS trigger time may sit outside the hourly directory it
# was filed under. The two disagree because the directory name comes from the
# PI'S wall clock, sampled after a ~22 MB socket read, while the header carries
# the GPS trigger instant. Measured on mj08 (3,689 records over 25 random
# dirs, 0 undecodable): 3 records outside their dir hour, worst case 3 s early,
# none late. That sample is normal operation and this skew widens during
# exactly the episodes that cause drops, so this allowance is deliberately
# ~1000x the measurement rather than fitted to it. It only ever costs
# directories, and surplus directories are free (see window_dir_range).
DIR_NAME_SKEW_SECONDS = 3600.0

# The only telemetry columns this reconciliation reads. Projecting to these at
# parse time matters: units retain 240-270 days of 1/min telemetry (>500k rows,
# 49 columns), and on the sensors' Python 3.7 csv.DictReader yields OrderedDict.
# Keeping whole rows peaked ~3 GB on a 4 GB Pi -- enough to OOM it.
TELEMETRY_COLUMNS = ("time", "valid_packets", "bytes_written",
                     "packets_sent", "packets_dropped")

_TELEMETRY_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv(?:\.bak)?$")

# The fractional-seconds run of an ISO stamp. The only '.' in an ISO timestamp
# is this separator (the UTC offset uses ':'), so the first match is it.
_ISO_FRACTION_RE = re.compile(r"\.(\d+)")


def _normalize_stamp(raw):
    """Normalize a timestamp to ISO 'T' form for safe string comparison.

    brokkr writes "2026-09-10 23:59:18.717565+00:00" (space-separated) while
    decode_gps_time() emits "2026-09-10T23:59:18.717". These get compared
    against the same bounds, and ' ' (0x20) sorts BELOW 'T' (0x54) -- so mixing
    the forms silently puts a whole day on the wrong side of an edge. Applied
    to BOTH rows and caller-supplied bounds.
    """
    stamp = (raw or "").strip()
    return stamp.replace(" ", "T", 1) if stamp else ""


def window_dir_range(t0, t1, edge_seconds=DEFAULT_EDGE_SECONDS,
                     bound_tolerance_s=DEFAULT_BOUND_TOLERANCE_S):
    """Hourly-dir bounds (inclusive) covering every record the audit reads.

    Returns ``(since, until)`` as ``'YYYY-MM-DDTHH'`` -- the same lexicographic
    form the scanners already compare directory names against -- or
    ``(None, None)`` when a bound cannot be parsed, which means "do not
    restrict". Deriving nothing here rather than raising is deliberate: the
    authoritative bound validation lives in counter_window(), and a full scan
    reaches it with the same verdict, just slower.

    This is an I/O optimization and MUST NOT change the report. It is sound
    where --since is not: --since auto derives a cutoff from AGS retention,
    which is unrelated to the window and can therefore truncate the union and
    inflate loss, whereas a WINDOW-derived range can only drop records that
    headers_in_window() already discards.

    The range is wider than the literal bounds for two reasons:
      * the MJ scan runs before counter_window() picks the actual counter rows,
        which may sit up to ``bound_tolerance_s`` outside the request, and
        boundary_activity() then reaches a further ``edge_seconds`` past each;
      * a record's trigger time can fall outside its directory's hour
        (``DIR_NAME_SKEW_SECONDS``).

    The asymmetry of failure drives the margin: over-inclusion costs a few
    seconds of header reads and is discarded downstream, while
    under-inclusion silently shrinks the union and manufactures a false FAIL
    for someone hunting data loss that never happened. So the margin is summed
    from all three named constants and then rounded OUTWARD to whole hours.
    """
    start_epoch, end_epoch = _iso_epoch(t0), _iso_epoch(t1)
    if start_epoch is None or end_epoch is None:
        return None, None
    margin = bound_tolerance_s + edge_seconds + DIR_NAME_SKEW_SECONDS
    low = _floor_hour(start_epoch - margin)
    high = _floor_hour(end_epoch + margin)
    return _hour_dir_name(low), _hour_dir_name(high)


def _floor_hour(epoch):
    """Round an epoch down to the start of its UTC hour."""
    return epoch - (epoch % 3600.0)


def _hour_dir_name(epoch):
    """Format an epoch as the 'YYYY-MM-DDTHH' name of its hourly directory.

    UTC, because that is what the units run and what the existing --since
    comparison already assumes: earliest_ags_timestamp() builds this same form
    from GPS trigger times and compares it to directory names directly.
    """
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H")


def _pad_iso_fraction(text):
    """Normalize fractional seconds to exactly 6 digits, or leave them absent.

    datetime.fromisoformat() on Python 3.7-3.10 accepts ONLY 3 or 6 fractional
    digits -- it was written to read isoformat()'s own output, not ISO 8601 in
    general. The sensors run 3.7 (Buster) while dev boxes run 3.11+, which
    accepts any number of digits. Without this, an operator bound as ordinary
    as '2026-09-10T23:59:18.5' parses on the dev box and is rejected ON THE
    UNIT, i.e. a window bound that is "unparseable" only in the field. More
    than 6 digits is sub-microsecond, which datetime cannot represent at all,
    so truncating there matches what 3.11+ does itself.
    """
    found = _ISO_FRACTION_RE.search(text)
    if found is None:
        return text
    padded = (found.group(1) + "000000")[:6]
    return text[:found.start()] + "." + padded + text[found.end():]


def _iso_epoch(stamp):
    """Parse an ISO stamp to epoch seconds; None if unparseable.

    Naive stamps are treated as UTC -- decode_gps_time() emits UTC without a
    suffix, and brokkr telemetry carries an explicit +00:00.
    """
    text = _normalize_stamp(stamp)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _pad_iso_fraction(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _telemetry_file_date(name):
    """Extract the YYYY-MM-DD embedded in a telemetry filename, or None."""
    found = _TELEMETRY_DATE_RE.search(name)
    return found.group(1) if found else None


def load_telemetry_rows(telemetry_dir, since_date=None, until_date=None):
    """Load telemetry rows as (stamp, {column: value}), time-ordered.

    Only ``TELEMETRY_COLUMNS`` are kept (see that constant for why), and files
    whose embedded date falls outside [since_date, until_date] are not opened
    at all.

    ``.csv.bak`` is included -- brokkr rotates on restart, so a restart day is
    split across both and reading only the ``.csv`` loses the earlier part. The
    two can carry rows for the SAME timestamp, though, so rows are de-duplicated
    by timestamp with the live ``.csv`` winning: the ``.bak`` holds pre-rotation
    counter values, and letting those win manufactures a phantom reset.

    NUL bytes (a row torn by a mid-write death) are stripped, not fatal.
    """
    try:
        names = sorted(os.listdir(telemetry_dir))
    except OSError as e:
        raise RuntimeError(
            "cannot read telemetry dir {}: {}".format(telemetry_dir, e))

    def wanted(name):
        if not (name.endswith(".csv") or name.endswith(".csv.bak")):
            return False
        stamp = _telemetry_file_date(name)
        if stamp is None:
            return True          # unparseable name: read it rather than guess
        if since_date and stamp < since_date:
            return False
        if until_date and stamp > until_date:
            return False
        return True

    # .bak first so the live .csv overwrites it at equal timestamps.
    ordered = ([n for n in names if n.endswith(".csv.bak") and wanted(n)]
               + [n for n in names if n.endswith(".csv") and wanted(n)])

    by_stamp = {}
    for name in ordered:
        path = os.path.join(telemetry_dir, name)
        try:
            with open(path, "r", errors="replace") as fh:
                text = fh.read().replace("\x00", "")
        except OSError as e:
            logger.warning("skipping telemetry file %s: %s", name, e)
            continue
        for raw in csv.DictReader(io.StringIO(text)):
            stamp = _normalize_stamp(raw.get("time"))
            if stamp:
                by_stamp[stamp] = {k: raw.get(k) for k in TELEMETRY_COLUMNS}

    return sorted(by_stamp.items())


def _counter(row, field):
    """Read a numeric counter column by name; None if absent or not finite.

    Rejecting NaN/inf is load-bearing, not defensive: a bare float() accepts
    them, and a NaN latched into the reset scan makes every subsequent
    `value < previous` False -- silently disabling restart detection for the
    rest of the window, which turns a real loss into a PASS.
    """
    raw = (row.get(field) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _row_epochs(rows):
    """Attach an epoch to every telemetry row; None where it cannot be placed.

    Unplaceable rows are kept, not dropped: ``counter_window`` refuses on the
    ones that land inside the span it measures (see ``_refuse_unplaceable``),
    because a row missing from the reset scan is how a real loss becomes a
    PASS -- the same hole ``_counter`` closes for NaN. Outside that span a
    torn row cannot affect the result, so aborting on it would be noise.
    """
    return [(_iso_epoch(stamp), stamp, row) for stamp, row in rows]


def _refuse_unplaceable(placed, start_index, end_index):
    """Refuse if any row inside the measured span has no usable timestamp.

    The reset scan examines only ``start < t <= end``, so only unplaceable
    rows in that span can hide a ``valid_packets`` reset; a NUL-torn row a day
    away aborts a run it could not have influenced.

    "Inside" has to be POSITIONAL, because an unplaceable row has no instant to
    compare. That leans on load_telemetry_rows() returning rows in timestamp
    order, which holds for same-format stamps but could be disturbed by a clock
    jump that reorders the file. The failure direction is safe either way: a
    row misplaced by such a jump is at worst refused when it need not have
    been, never silently skipped while inside the span.
    """
    for epoch, stamp, _row in placed[start_index + 1:end_index + 1]:
        if epoch is None:
            raise RuntimeError(
                "telemetry row timestamp {!r} inside the measured window is "
                "unparseable, so a counter reset could hide behind it. "
                "Narrow the window or repair the telemetry file.".format(
                    stamp))


def counter_window(rows, t0, t1, bound_tolerance_s=DEFAULT_BOUND_TOLERANCE_S):
    """Snapshot H&S counters at the last telemetry row at or before each bound.

    The window is defined BY the counter-read instants so the delta is exact.
    Returns the row timestamps used, so the caller can bound the header sets
    identically.

    Refuses (RuntimeError) when the result would not describe the window that
    was asked for:
      * bounds out of order -- otherwise start/end are chosen independently and
        a reversed pair yields delta=0, union=0, lost=0: a PASS that examined
        nothing;
      * no bracketing rows, or both bounds snapping to the same row;
      * a snapped row further than ``bound_tolerance_s`` from its request,
        which happens across a telemetry gap and silently measures a different
        (often much wider) window;
      * ``valid_packets`` decreasing anywhere inside -- an AGS restart resets
        it, so end-start is meaningless. Stitching across a reset would
        reintroduce the boundary error this reconciliation exists to remove.
    """
    t0, t1 = _normalize_stamp(t0), _normalize_stamp(t1)
    if not (t0 and t1):
        raise RuntimeError("window bounds must both be supplied")

    # Compare INSTANTS, not strings. Telemetry rows always carry an explicit
    # +00:00 and the documented --window-start form does not; at an equal
    # prefix the longer string loses, so '...23:59:18+00:00' <= '...23:59:18'
    # is False. A bound landing exactly on a row therefore excluded that row
    # and snapped the window up to one telemetry interval (~60 s) early -- well
    # inside the 120 s drift tolerance, so nothing below caught it.
    t0_epoch, t1_epoch = _iso_epoch(t0), _iso_epoch(t1)
    if t0_epoch is None or t1_epoch is None:
        raise RuntimeError(
            "window bounds must be ISO timestamps; got {} .. {}".format(
                t0, t1))
    if t0_epoch >= t1_epoch:
        raise RuntimeError(
            "window start {} is not before end {}".format(t0, t1))

    placed = _row_epochs(rows)
    start = end = None
    start_index = end_index = None
    for index, entry in enumerate(placed):
        epoch, _stamp, row = entry
        if epoch is None:
            continue
        if _counter(row, "valid_packets") is None:
            continue
        if epoch <= t0_epoch:
            start, start_index = entry, index
        if epoch <= t1_epoch:
            end, end_index = entry, index

    if start is None or end is None:
        raise RuntimeError(
            "no telemetry rows bracket the window {} .. {}".format(t0, t1))
    if start[0] >= end[0]:
        raise RuntimeError(
            "both window bounds snap to the same telemetry row ({}): the "
            "window contains no counter movement".format(start[1]))

    for label, requested, req_epoch, chosen in (
            ("start", t0, t0_epoch, start), ("end", t1, t1_epoch, end)):
        drift = abs(req_epoch - chosen[0])
        if drift > bound_tolerance_s:
            raise RuntimeError(
                "window {} snapped to {}, {:.0f}s from the requested {} "
                "(telemetry gap?). That would measure a different window than "
                "the one asked for.".format(
                    label, chosen[1], drift, requested))

    _refuse_unplaceable(placed, start_index, end_index)

    # Seed from the START row: a reset between the start bound and the first
    # in-window row would otherwise go undetected, and that is exactly where a
    # restart tends to land.
    previous = _counter(start[2], "valid_packets")
    for epoch, stamp, row in placed:
        # Unplaceable rows survive only OUTSIDE the span -- _refuse_unplaceable
        # has already rejected any inside it -- so skipping them here cannot
        # hide a reset.
        if epoch is None:
            continue
        if not (start[0] < epoch <= end[0]):
            continue
        value = _counter(row, "valid_packets")
        if value is None:
            continue
        if previous is not None and value < previous:
            raise RuntimeError(
                "valid_packets reset at {} ({:.0f} -> {:.0f}): AGS restarted "
                "inside the window, so the delta is not meaningful. Choose a "
                "window inside a single AGS boot.".format(
                    stamp, previous, value))
        previous = value

    result = {"start_row": start[1], "end_row": end[1]}
    for field in ("valid_packets", "bytes_written", "packets_sent",
                  "packets_dropped"):
        first, last = _counter(start[2], field), _counter(end[2], field)
        result["delta_" + field] = (
            None if first is None or last is None else last - first)
    return result


def decode_header_times(headers):
    """Decode every header's trigger time once.

    Returns ``(pairs, undecodable)`` where pairs is a list of
    ``(epoch_seconds, header)``. Undecodable headers are bad-GPS records; they
    cannot be placed in the window at all, so they can only make the union an
    UNDERCOUNT -- never a false "loss" -- and are reported separately.
    """
    pairs, undecodable = [], 0
    for header in headers:
        epoch = _iso_epoch(decode_gps_time(header))
        if epoch is None:
            undecodable += 1
        else:
            pairs.append((epoch, header))
    return pairs, undecodable


def headers_in_window(pairs, start_epoch, end_epoch):
    """Headers whose trigger time falls in [start, end)."""
    return {h for epoch, h in pairs if start_epoch <= epoch < end_epoch}


def duplicates_in_window(scan, start_epoch, end_epoch):
    """Count duplicate record occurrences whose trigger time is in the window.

    Counts the same thing the scanners' ``duplicate_count`` does -- occurrences
    of a raw header beyond its first -- but only for the headers that land in
    the window being certified. The scan-wide count is a property of everything
    the unit still RETAINS, so a single past GPS-freeze episode (mj08) blocked
    every later audit on that unit permanently, with no way out: --since and
    --recover/--purge are all rejected alongside --audit-loss.

    A duplicate cannot straddle a bound. The trigger time is decoded FROM the
    header bytes, so every copy of a header decodes to the same instant and all
    copies land on the same side. Copies whose header is undecodable (bad GPS)
    are not counted: they are absent from the union whether duplicated or not,
    and are already reported as the undecodable_* totals.
    """
    # Absent key and explicit None are different faults and get different
    # messages, but neither may read as "no duplicates": that would silently
    # disarm the blocker. Both refuse as RuntimeError, which run()'s audit
    # branch already turns into an EXIT_NO_DATA refusal -- a bare subscript
    # would escape it as an unhandled traceback instead.
    if "duplicate_headers" not in scan:
        raise RuntimeError(
            "this scan result carries no 'duplicate_headers' key, so "
            "in-window duplicates cannot be counted. A scan producer must "
            "report the duplicated headers themselves, not duplicate_count.")
    occurrences = scan["duplicate_headers"]
    if occurrences is None:
        raise RuntimeError(
            "the incremental MJ scanner does not retain which headers were "
            "duplicated, so in-window duplicates cannot be counted; re-run "
            "the audit without --mj-cache")
    count = 0
    for header in occurrences:
        epoch = _iso_epoch(decode_gps_time(header))
        if epoch is not None and start_epoch <= epoch < end_epoch:
            count += 1
    return count


def boundary_activity(pairs, bound_epoch, edge_seconds):
    """Count records whose trigger time is within +/-edge_seconds of a bound.

    Non-zero means the trigger->valid_packets latency straddles that boundary,
    so the counter and the header set disagree about which records belong to
    the window. See THE PRECONDITION in this section's header comment.
    """
    return sum(1 for epoch, _ in pairs
               if abs(epoch - bound_epoch) <= edge_seconds)


def build_lost_report(ags, mj, rows, t0, t1,
                      edge_seconds=DEFAULT_EDGE_SECONDS,
                      bound_tolerance_s=DEFAULT_BOUND_TOLERANCE_S,
                      extra_blockers=()):
    """Reconcile parsed-record count against the union actually on disk.

    ``ags``/``mj`` are the scan result dicts (not bare header sets): the
    duplicate headers and file counts they carry decide whether the union can
    be trusted at all.
    """
    counters = counter_window(rows, t0, t1, bound_tolerance_s)

    # Bound the header sets by the ACTUAL counter-read instants, not the
    # caller's request. The delta is measured between those two rows, so
    # filtering headers by anything else reintroduces a start/end mismatch.
    start_epoch = _iso_epoch(counters["start_row"])
    end_epoch = _iso_epoch(counters["end_row"])

    ags_pairs, ags_undecodable = decode_header_times(ags["headers"])
    mj_pairs, mj_undecodable = decode_header_times(mj["headers"])

    ags_window = headers_in_window(ags_pairs, start_epoch, end_epoch)
    mj_window = headers_in_window(mj_pairs, start_epoch, end_epoch)
    union = ags_window | mj_window

    delta_valid = counters["delta_valid_packets"]
    lost = None if delta_valid is None else int(round(delta_valid)) - len(union)

    all_pairs = ags_pairs + mj_pairs
    edge_start = boundary_activity(all_pairs, start_epoch, edge_seconds)
    edge_end = boundary_activity(all_pairs, end_epoch, edge_seconds)

    # Independent cross-check of the local-write drop count. bytes_written is a
    # byte odometer, entirely separate from the drop counter. ADVISORY ONLY:
    # it assumes every record is exactly RECORD_BYTES, which is a config value
    # (the preset's data_length), not a format invariant.
    delta_written = counters.get("delta_bytes_written")
    local_drops = None
    if delta_valid is not None and delta_written is not None:
        written_records = delta_written * BYTES_WRITTEN_SCALE / RECORD_BYTES
        local_drops = int(round(delta_valid - written_records))

    duplicates = (duplicates_in_window(ags, start_epoch, end_epoch)
                  + duplicates_in_window(mj, start_epoch, end_epoch))

    blockers = list(extra_blockers)
    if edge_start or edge_end:
        blockers.append(
            "boundaries not quiet: {} record(s) within +/-{:.0f}s of the start "
            "and {} of the end. The trigger->valid_packets latency straddles "
            "the edge, so the counter and the header set disagree about which "
            "records are in the window. Re-run with bounds in a trigger gap."
            .format(edge_start, edge_seconds, edge_end))
    if duplicates:
        blockers.append(
            "{} duplicate header(s) inside the window: records are matched by "
            "raw header bytes, and duplicates collapse in the union, "
            "overstating loss.".format(duplicates))
    if not mj.get("file_count"):
        blockers.append(
            "no MJ .bin files were scanned: the union is empty by "
            "construction, so any delta reads as total loss.")

    return {
        "window_start": _normalize_stamp(t0),
        "window_end": _normalize_stamp(t1),
        "counter_row_start": counters["start_row"],
        "counter_row_end": counters["end_row"],
        "delta_valid_packets": (
            None if delta_valid is None else int(round(delta_valid))),
        "ags_in_window": len(ags_window),
        "mj_in_window": len(mj_window),
        "ags_only": len(ags_window - mj_window),
        "union": len(union),
        "lost": lost,
        "edge_records_start": edge_start,
        "edge_records_end": edge_end,
        "edge_seconds": edge_seconds,
        "duplicate_headers": duplicates,
        "undecodable_ags_total": ags_undecodable,
        "undecodable_mj_total": mj_undecodable,
        "stream_drops": _as_int(counters.get("delta_packets_dropped")),
        "local_drops_derived": local_drops,
        "blockers": blockers,
        "certified": bool(not blockers and lost == 0),
    }


def _as_int(value):
    return None if value is None else int(round(value))


def _fmt(value):
    """Format a possibly-None counter without raising."""
    return "n/a" if value is None else "{:>8}".format(value)


def format_lost_report(report):
    """Human-readable reconciliation summary."""
    lines = [
        "",
        "HAM-164 Loss Reconciliation",
        "===========================",
        "Window:   {} -> {}  (exclusive)".format(
            report["window_start"], report["window_end"]),
        "Counters: {} -> {}".format(
            report["counter_row_start"], report["counter_row_end"]),
        "",
        "  delta valid_packets      {}".format(
            _fmt(report["delta_valid_packets"])),
        "  |H_ags union H_mj|       {}".format(_fmt(report["union"])),
        "  " + "-" * 32,
        "  LOST                     {}".format(_fmt(report["lost"])),
        "",
        "  on AGS only (not on MJ)  {}".format(_fmt(report["ags_only"])),
        "  stream drops             {}".format(_fmt(report["stream_drops"])),
        "  local drops (derived)    {}  (advisory)".format(
            _fmt(report["local_drops_derived"])),
        "  records near start edge  {}".format(
            _fmt(report["edge_records_start"])),
        "  records near end edge    {}".format(
            _fmt(report["edge_records_end"])),
    ]
    undecodable = (report["undecodable_ags_total"]
                   + report["undecodable_mj_total"])
    if undecodable:
        lines.append(
            "  bad-GPS headers (whole scan, not window-scoped): {}".format(
                undecodable))
    lines.append("")

    lost = report["lost"]
    if report["certified"]:
        lines.append(
            "PASS: no records lost; every parsed record is on the AGS or MJ.")
    elif lost is not None and lost < 0:
        lines.append(
            "INVALID: more records on disk ({}) than the counter reports. The "
            "window bounds or the identity assumption are wrong -- this is not "
            "a loss measurement.".format(-lost))
    elif lost:
        lines.append("FAIL: {} parsed record(s) are on NEITHER the AGS nor "
                     "MJ.".format(lost))
    for blocker in report["blockers"]:
        lines.append("CANNOT CERTIFY: " + blocker)
    lines.append("")
    return "\n".join(lines)


def _build_parser():
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Compare AGS trigger data against mjolnir .bin files.",
    )
    parser.add_argument(
        "--ags-host", default=DEFAULT_AGS_HOST,
        help="AGS sensor SSH host (default: %(default)s)",
    )
    parser.add_argument(
        "--ags-path", default=DEFAULT_AGS_PATH,
        help="AGS data directory (default: %(default)s)",
    )
    parser.add_argument(
        "--mj-path", default=DEFAULT_MJ_PATH,
        help="Base path for DATA drive discovery (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Write JSON to stdout instead of human report",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug logging",
    )
    parser.add_argument(
        "--since",
        help="Only scan MJ directories at or after this date "
             "(YYYY-MM-DD, YYYY-MM-DDTHH, or 'auto' to detect from AGS)",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help="Max missing trigger detail lines in report; 0 = no limit (default: %(default)s)",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Show what would be recovered without transferring (use with --recover)",
    )
    parser.add_argument(
        "--recover", action="store_true",
        help="After scan, extract missing triggers from AGS to MJ DATA drives",
    )
    parser.add_argument(
        "--purge", action="store_true",
        help="After recovery, delete AGS files fully confirmed on MJ (requires --recover)",
    )
    parser.add_argument(
        "--status-file", default=DEFAULT_STATUS_FILE,
        help="Heartbeat/status JSON the scrub updates as it advances "
             "(default: %(default)s; empty string disables)",
    )
    parser.add_argument(
        "--mj-cache", default=DEFAULT_MJ_CACHE,
        help="Incremental MJ-scan cache file: reuse unchanged hourly dirs' "
             "headers instead of re-reading every file (default: %(default)s; "
             "empty string forces a full scan every run)",
    )
    parser.add_argument(
        "--metrics-file", default=DEFAULT_METRICS_FILE,
        help="Durable CSV appended one row per run with MJ-scan cache "
             "hit-rate/timing (default: %(default)s; empty string disables)",
    )
    # NOTE: named --audit-loss, not --lost-report, deliberately. "--l" already
    # resolves to --limit for existing callers; adding a second --l* flag makes
    # that abbreviation ambiguous and breaks them with "error: ambiguous
    # option". "--a" was already ambiguous (--ags-host/--ags-path), so this
    # name adds no new collision.
    parser.add_argument(
        "--audit-loss", action="store_true",
        help="HAM-164 reconciliation: compare delta valid_packets against the "
             "union of AGS+MJ headers over a window, to count records lost by "
             "BOTH paths. Requires --window-start/--window-end, and boundaries "
             "in a trigger gap. Use instead of 'packets_dropped == 0', which a "
             "DISCONNECTED stream satisfies while shedding every record. "
             "Incompatible with --recover/--purge/--since.",
    )
    parser.add_argument(
        "--window-start",
        help="Audit window start, ISO (e.g. 2026-09-04T23:59:18). Pick a "
             "moment with no triggers nearby -- see --edge-seconds.",
    )
    parser.add_argument(
        "--window-end",
        help="Audit window end, exclusive, ISO. Also must be quiet.",
    )
    parser.add_argument(
        "--telemetry-dir", default=DEFAULT_TELEMETRY_DIR,
        help="Directory of brokkr telemetry CSVs supplying valid_packets "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--edge-seconds", type=float, default=DEFAULT_EDGE_SECONDS,
        help="Each boundary must have no records within +/- this many seconds, "
             "or the audit refuses to certify: the trigger->valid_packets "
             "latency would straddle the edge (default: %(default)s)",
    )
    parser.add_argument(
        "--bound-tolerance", type=float, default=DEFAULT_BOUND_TOLERANCE_S,
        help="Refuse if a window bound snaps to a telemetry row further than "
             "this many seconds away, which would silently measure a different "
             "window (default: %(default)s)",
    )
    return parser


def run(ags_host, ags_path, mj_path, json_output=False, output_file=None,
        limit=DEFAULT_LIMIT, since=None, recover=False, dry_run=False,
        purge=False, status_file=DEFAULT_STATUS_FILE,
        mj_cache=DEFAULT_MJ_CACHE, metrics_file=DEFAULT_METRICS_FILE,
        audit_loss=False, window_start=None, window_end=None,
        telemetry_dir=DEFAULT_TELEMETRY_DIR,
        edge_seconds=DEFAULT_EDGE_SECONDS,
        bound_tolerance=DEFAULT_BOUND_TOLERANCE_S):
    """Run the scrubber and return exit code.

    Parameters
    ----------
    ags_host : str
    ags_path : str
    mj_path : str
    json_output : bool
        If True, print JSON to stdout.
    output_file : str or None
        Path to write JSON report.
    limit : int
        Max missing trigger detail lines in human report (0 = no limit).
    since : str or None
        Only scan MJ directories at or after this date.
    recover : bool
        If True, extract missing triggers from AGS to MJ DATA drives.
    dry_run : bool
        If True, show what would be recovered without transferring.
    purge : bool
        If True, delete AGS files fully confirmed on MJ after recovery.
    status_file : str or None
        Heartbeat/status file to update as the scrub advances (None disables).
    mj_cache : str or None
        Incremental MJ-scan cache file (None forces a full scan every run).

    Returns
    -------
    int
        Exit code.
    """
    if audit_loss:
        # Drop every shared-state path BEFORE the first write, which is what
        # makes the "writes no shared state" property below actually hold: the
        # three write_status() calls on the way to the audit branch ran
        # unconditionally, so a hand-run audit stamped three heartbeats into
        # the file the scheduled scrub shares, without holding
        # hamma-scrub.sh's flock. state_monitor.check_scrub_health() reads a
        # changing (pid, timestamp, phase) token as "the scrub is making
        # progress", so those heartbeats mask a real hang -- and
        # _recover_stuck_scrub() SIGKILLs the process group of whatever PID is
        # in that file, which would match this run too (_pid_is_scrub() only
        # checks that the cmdline names hamma_scrub.py).
        #
        # mj_cache goes with them: it is a world-writable tmpfs file the timer
        # also owns, and its cached per-dir header SETS cannot say which header
        # was duplicated -- which the in-window duplicate blocker needs. The
        # audit pays a full MJ scan instead; it is hand-run and already
        # refuses --since, so completeness over speed is the right trade here.
        # write_status()/write_scan_metrics() are no-ops on a None path.
        status_file = metrics_file = mj_cache = None

    write_status(status_file, "start")
    if dry_run and not recover:
        logger.warning("--dry-run has no effect without --recover")

    if purge and not recover:
        logger.error("--purge requires --recover")
        return EXIT_NO_DATA

    if audit_loss and not (window_start and window_end):
        logger.error("--audit-loss requires --window-start and --window-end")
        return EXIT_NO_DATA

    if audit_loss and (recover or purge):
        # --recover/--purge mutate the MJ and AGS sides mid-measurement, moving
        # records into and out of the union while it is being counted.
        logger.error("--audit-loss cannot be combined with --recover/--purge")
        return EXIT_NO_DATA

    if audit_loss and since:
        # --since truncates the MJ scan. The deployed command carries
        # `--since auto`, which derives its cutoff from the earliest AGS record
        # -- hours ago on a healthy purged unit. That would empty the union and
        # report the whole window as lost.
        logger.error(
            "--audit-loss cannot be combined with --since: it truncates the "
            "MJ scan and would report un-scanned records as lost")
        return EXIT_NO_DATA

    # Parse --since into normalized directory prefix
    since_cutoff = None
    auto_detect = False
    if since:
        try:
            parsed = _parse_since(since)
        except ValueError as e:
            logger.error("%s", e)
            return EXIT_NO_DATA

        if parsed == 'auto':
            auto_detect = True
        else:
            since_cutoff = parsed
            logger.info("Filtering MJ directories to >= %s", since_cutoff)

    # AGS scan runs first (needed for auto-detect and comparison)
    write_status(status_file, "scan")
    try:
        ags = scan_ags_files(ags_host, ags_path)
    except RuntimeError as e:
        logger.error("AGS scan failed: %s", e)
        write_status(status_file, "error", error="ags scan failed")
        return EXIT_SSH_ERROR

    # Auto-detect: derive cutoff from the scanned AGS entries
    if auto_detect:
        since_cutoff = earliest_ags_timestamp(ags["entries"])
        if since_cutoff:
            logger.info("Auto-detected --since cutoff: %s", since_cutoff)
        else:
            logger.info(
                "Auto-detect found no valid GPS data; scanning all MJ dirs")

    # HAM-164: read only the hourly dirs that can hold a record this
    # reconciliation will look at. The audit refuses --since (an AGS-retention
    # cutoff, unrelated to the window, which would truncate the union), but a
    # WINDOW-derived range cannot truncate anything headers_in_window() keeps
    # -- so walking all 714 of mj08's dirs to certify one hour was pure waste.
    # The scheduled scrub is fast for the same reason, not because of the
    # cache: its deployed `--since auto` leaves it 3-4 dirs (measured mean
    # 0.54 s over 6,787 runs, cache cold on 82% of them).
    until_cutoff = None
    if audit_loss:
        since_cutoff, until_cutoff = window_dir_range(
            window_start, window_end, edge_seconds, bound_tolerance)
        if since_cutoff:
            logger.info("Audit MJ scan restricted to dirs %s .. %s",
                        since_cutoff, until_cutoff)

    write_status(status_file, "scan_mj")
    mj = scan_mj_files(mj_path, since=since_cutoff, cache_file=mj_cache,
                       until=until_cutoff)

    # HAM-164 reconciliation. Deliberately ahead of the "no AGS data" bail
    # below: once the scrub has purged confirmed files, an EMPTY AGS is the
    # normal steady state and the union is carried entirely by the MJ side.
    # Bailing there would make this audit unavailable on exactly the healthy
    # units we most want to certify.
    #
    # This path writes NO shared state -- no write_status, no
    # write_scan_metrics, no MJ-scan cache. That is enforced at the top of
    # run(), where those paths are set to None; see the comment there for why
    # it must happen before the first write rather than here.
    if audit_loss:
        try:
            margin = timedelta(days=1)
            first = datetime.strptime(window_start[:10], "%Y-%m-%d") - margin
            last = datetime.strptime(window_end[:10], "%Y-%m-%d") + margin
            rows = load_telemetry_rows(
                telemetry_dir,
                since_date=first.strftime("%Y-%m-%d"),
                until_date=last.strftime("%Y-%m-%d"))
            extra = []
            # Compressed records never enter the union: the incremental MJ
            # scanner skips compressed/ and both scanners glob only *.bin. The
            # day compression is enabled, an un-guarded audit would report the
            # whole window as lost.
            if glob.glob(os.path.join(mj_path, DRIVE_PATTERN, "*", "*.hmc")):
                extra.append(
                    "compressed .hmc records present: they are invisible to "
                    "both MJ scanners, so the union undercounts.")
            report = build_lost_report(
                ags, mj, rows, window_start, window_end,
                edge_seconds=edge_seconds, bound_tolerance_s=bound_tolerance,
                extra_blockers=extra)
        except (RuntimeError, ValueError) as e:
            logger.error("--audit-loss failed: %s", e)
            return EXIT_NO_DATA

        if json_output:
            print(json.dumps(report, indent=2))
        else:
            print(format_lost_report(report))
        if output_file:
            with open(output_file, "w") as fh:
                json.dump(report, fh, indent=2)

        return EXIT_OK if report["certified"] else EXIT_MISSING

    if not ags["entries"]:
        logger.info("No AGS data found — nothing to compare")
        write_status(status_file, "done", recovered=0, purged=0)
        write_scan_metrics(metrics_file, mj, 0, 0)
        return EXIT_OK

    if mj["file_count"] == 0 and mj["skipped"] == 0:
        logger.error("No DATA drives or .bin files found at %s", mj_path)
        write_status(status_file, "error", error="no MJ drives/files")
        write_scan_metrics(metrics_file, mj, 0, 0)
        return EXIT_NO_DATA

    comparison = compare_headers(ags["entries"], mj["headers"])

    ags_file_count = len(set(e["filename"] for e in ags["entries"]))
    results = {
        "ags_triggers": len(ags["entries"]),
        "ags_files": ags_file_count,
        "ags_elapsed": ags["elapsed"],
        "ags_duplicate_count": ags["duplicate_count"],
        "mj_triggers": len(mj["headers"]),
        "mj_files_scanned": mj["file_count"],
        "mj_duplicate_count": mj["duplicate_count"],
        "mj_elapsed": mj["elapsed"],
        "matched": comparison["matched"],
        "missing_on_mj": comparison["missing_on_mj"],
        "mj_only_count": comparison["mj_only_count"],
        "warnings": [],
    }

    # Recovery + purge reuse one SSH connection (per-op cost ~16x lower).
    # The context manager guarantees teardown even if recover/purge raise
    # (the bare open/close pair leaked the socket on exceptions).
    recovery_results = None
    purge_results = None
    with ssh_control_master(ags_host) as control_path:
        # Recovery flow
        if recover and comparison["missing_on_mj"]:
            write_status(status_file, "recover", recovered=0,
                         missing=len(comparison["missing_on_mj"]))
            cleanup_orphaned_temps(mj_path)
            candidates = filter_recovery_candidates(
                comparison["missing_on_mj"], ags["entries"],
                since_cutoff=since_cutoff,
            )
            recovery_results = recover_triggers(
                candidates, ags_host, ags_path, mj_path, dry_run=dry_run,
                control_path=control_path, status_file=status_file,
            )
            recovered_count = len([r for r in recovery_results
                                   if r["status"] == "recovered"])
            failed_count = len([r for r in recovery_results
                                if r["status"] == "failed"])
            if recovered_count:
                logger.info("Recovery: %d succeeded", recovered_count)
            if failed_count:
                logger.warning("Recovery: %d failed", failed_count)

        # Update mj_headers and missing list with recovered triggers
        if recovery_results:
            recovered_headers = set()
            recovered_dirs = set()
            for r in recovery_results:
                if r["status"] == "recovered":
                    mj["headers"].add(r["header"])
                    recovered_headers.add(r["header"])
                    if r.get("target_path"):
                        # target_path is RELATIVE to mj_path (recover_triggers
                        # stores os.path.relpath); the scan cache is keyed by
                        # ABSOLUTE dirs, so rejoin mj_path before refreshing.
                        recovered_dirs.add(os.path.dirname(
                            os.path.join(mj_path, r["target_path"])))
            # Recovery wrote new .bin into these dirs AFTER the scan saved the
            # cache (pre-recovery), leaving them stale. Refresh so the next
            # scan cache-hits them instead of re-reading.
            _refresh_cache_dirs(mj_cache, recovered_dirs)
            if recovered_headers:
                comparison["missing_on_mj"] = [
                    e for e in comparison["missing_on_mj"]
                    if e["header"] not in recovered_headers
                ]
                results["missing_on_mj"] = comparison["missing_on_mj"]
                results["matched"] += len(recovered_headers)

        # Purge flow
        if purge:
            write_status(status_file, "purge")
            eligibility = identify_purgeable_files(
                ags["entries"], mj["headers"], recovery_results,
            )
            if eligibility["purgeable"]:
                purge_deletions = purge_ags_files(
                    ags_host, ags_path, eligibility["purgeable"],
                    dry_run=dry_run, control_path=control_path,
                    status_file=status_file,
                )
            else:
                purge_deletions = []

            deleted_names = [d["filename"] for d in purge_deletions
                             if d["status"] == "deleted"]
            failed_purge = [d for d in purge_deletions
                            if d["status"] == "failed"]

            if deleted_names:
                logger.info("Purge: deleted %d AGS files", len(deleted_names))
            if failed_purge:
                logger.warning("Purge: %d deletions failed", len(failed_purge))

            # Flatten to filename lists for reports (matching spec JSON shape)
            purge_results = {
                "deleted": [d["filename"] for d in purge_deletions
                            if d["status"] in ("deleted", "dry_run")],
                "failed": [{"filename": d["filename"], "error": d["error"]}
                           for d in purge_deletions if d["status"] == "failed"],
                "retained": eligibility["retained"],
                "dry_run": dry_run,
            }

    recovered_n = len([r for r in (recovery_results or [])
                       if r["status"] == "recovered"])
    purged_n = len((purge_results or {}).get("deleted", []))
    write_status(status_file, "done", recovered=recovered_n, purged=purged_n)
    write_scan_metrics(metrics_file, mj, recovered_n, purged_n)

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

    if comparison["missing_on_mj"]:
        return EXIT_MISSING
    return EXIT_OK


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    rc = run(
        ags_host=args.ags_host,
        ags_path=args.ags_path,
        mj_path=args.mj_path,
        json_output=args.json,
        output_file=args.output,
        limit=args.limit,
        since=args.since,
        recover=args.recover,
        dry_run=args.dry_run,
        purge=args.purge,
        status_file=args.status_file or None,
        mj_cache=args.mj_cache or None,
        metrics_file=args.metrics_file or None,
        audit_loss=args.audit_loss,
        window_start=args.window_start,
        window_end=args.window_end,
        telemetry_dir=args.telemetry_dir,
        edge_seconds=args.edge_seconds,
        bound_tolerance=args.bound_tolerance,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
