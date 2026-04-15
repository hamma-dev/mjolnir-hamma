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
import glob
import json
import logging
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

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
        Date string: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH'.

    Returns
    -------
    str
        Normalized to 'YYYY-MM-DDTHH' format for directory comparison.

    Raises
    ------
    ValueError
        If format is not recognized.
    """
    s = since_str.strip()
    # YYYY-MM-DDTHH (already has hour)
    if len(s) == 13 and s[10] == 'T':
        return s
    # YYYY-MM-DD (add T00 for start of day)
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s + 'T00'
    raise ValueError(
        "Invalid --since format '{}': expected YYYY-MM-DD or YYYY-MM-DDTHH".format(s)
    )


def scan_mj_files(base_path, since=None):
    """Scan local mjolnir .bin files and collect headers.

    Parameters
    ----------
    base_path : str
        Base path containing DATA?? drives (e.g., /media/pi).
    since : str or None
        If set, skip directories with names before this cutoff
        (format: 'YYYY-MM-DDTHH').

    Returns
    -------
    dict
        headers: set of bytes (128-byte raw headers)
        file_count: int (total .bin files found)
        duplicate_count: int (files with headers already seen)
        skipped: int (files < 128 bytes)
        dirs_skipped: int (directories before --since cutoff)
        elapsed: float (seconds)
    """
    headers = set()
    file_count = 0
    duplicate_count = 0
    skipped = 0
    dirs_skipped = 0
    t0 = time.time()

    pattern = os.path.join(base_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        logger.info("No DATA drives found at %s", base_path)

    for drive in drives:
        try:
            if since:
                # Per-directory filtering: only glob .bin in qualifying dirs
                dir_pattern = os.path.join(drive, "*")
                subdirs = sorted(glob.glob(dir_pattern))
                bin_files = []
                for subdir in subdirs:
                    if not os.path.isdir(subdir):
                        continue
                    dirname = os.path.basename(subdir)
                    if dirname < since:
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
                    duplicate_count += 1
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
        "duplicate_count": duplicate_count,
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


def scan_ags_files(ags_host, ags_path):
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
        elapsed: float (seconds)

    Raises
    ------
    RuntimeError
        If SSH connection fails.
    """
    t0 = time.time()

    cmd = ["ssh", ags_host, "python3 - " + ags_path]
    logger.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        input=STRIDER_SCRIPT.encode('utf-8'),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3600,
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
    duplicate_count = 0
    for entry in entries:
        if entry["header"] in headers:
            duplicate_count += 1
        else:
            headers.add(entry["header"])

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


def extract_trigger(ags_host, ags_path, filename, offset, size):
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

    Returns
    -------
    bytes or None
        Extracted data, or None on failure.
    """
    filepath = "{}/{}".format(ags_path, filename)
    dd_cmd = (
        "dd if={} iflag=skip_bytes,count_bytes bs=4096"
        " skip={} count={} status=none"
    ).format(filepath, offset, size)
    cmd = ["ssh", ags_host, dd_cmd]
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


def recover_triggers(candidates, ags_host, ags_path, mj_path, dry_run=False):
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
            })
            continue

        # Extract trigger via SSH dd
        data = extract_trigger(ags_host, ags_path, src_file, src_offset, size)
        if data is None:
            results.append({
                "source_file": src_file,
                "source_offset": src_offset,
                "trigger_index": trig_idx,
                "target_path": rel_target,
                "size": size,
                "status": "failed",
                "error": "dd extraction failed",
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
            })

    return results


def format_human_report(results, limit=DEFAULT_LIMIT, recovery=None):
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

    return "\n".join(lines)


def format_json_report(results, ags_host, recovery=None):
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
        report["recovery"] = recovery
    return json.dumps(report, indent=2)


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
        help="Only scan MJ directories at or after this date (YYYY-MM-DD or YYYY-MM-DDTHH)",
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
    return parser


def run(ags_host, ags_path, mj_path, json_output=False, output_file=None,
        limit=DEFAULT_LIMIT, since=None, recover=False, dry_run=False):
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

    Returns
    -------
    int
        Exit code.
    """
    if dry_run and not recover:
        logger.warning("--dry-run has no effect without --recover")

    # Parse --since into normalized directory prefix
    since_cutoff = None
    if since:
        try:
            since_cutoff = _parse_since(since)
            logger.info("Filtering MJ directories to >= %s", since_cutoff)
        except ValueError as e:
            logger.error("%s", e)
            return EXIT_NO_DATA

    try:
        ags = scan_ags_files(ags_host, ags_path)
    except RuntimeError as e:
        logger.error("AGS scan failed: %s", e)
        return EXIT_SSH_ERROR

    mj = scan_mj_files(mj_path, since=since_cutoff)

    if not ags["entries"]:
        logger.info("No AGS data found — nothing to compare")
        return EXIT_OK

    if mj["file_count"] == 0 and mj["skipped"] == 0:
        logger.error("No DATA drives or .bin files found at %s", mj_path)
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

    # Recovery flow
    recovery_results = None
    if recover and comparison["missing_on_mj"]:
        cleanup_orphaned_temps(mj_path)
        candidates = filter_recovery_candidates(
            comparison["missing_on_mj"], ags["entries"],
            since_cutoff=since_cutoff,
        )
        recovery_results = recover_triggers(
            candidates, ags_host, ags_path, mj_path, dry_run=dry_run,
        )
        recovered_count = len([r for r in recovery_results
                               if r["status"] == "recovered"])
        failed_count = len([r for r in recovery_results
                            if r["status"] == "failed"])
        if recovered_count:
            logger.info("Recovery: %d succeeded", recovered_count)
        if failed_count:
            logger.warning("Recovery: %d failed", failed_count)

    if json_output:
        print(format_json_report(results, ags_host, recovery=recovery_results))
    else:
        print(format_human_report(results, limit=limit, recovery=recovery_results))

    if output_file:
        with open(output_file, 'w') as f:
            f.write(format_json_report(results, ags_host, recovery=recovery_results))
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
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
