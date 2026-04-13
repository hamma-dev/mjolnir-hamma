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
import os
import struct
import subprocess
import sys
import time

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
DEFAULT_AGS_HOST = "10.10.10.1"
DEFAULT_AGS_PATH = "/ags/data"
DEFAULT_MJ_PATH = "/media/pi"
DRIVE_PATTERN = "DATA??"

# Exit codes
EXIT_OK = 0
EXIT_MISSING = 1
EXIT_SSH_ERROR = 2
EXIT_NO_DATA = 3


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


def scan_mj_files(base_path):
    """Scan local mjolnir .bin files and collect headers.

    Parameters
    ----------
    base_path : str
        Base path containing DATA?? drives (e.g., /media/pi).

    Returns
    -------
    dict
        headers: set of bytes (128-byte raw headers)
        file_count: int (total .bin files found)
        duplicate_count: int (files with headers already seen)
        skipped: int (files < 128 bytes)
        elapsed: float (seconds)
    """
    headers = set()
    file_count = 0
    duplicate_count = 0
    skipped = 0
    t0 = time.time()

    pattern = os.path.join(base_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        logger.info("No DATA drives found at %s", base_path)

    for drive in drives:
        try:
            bin_pattern = os.path.join(drive, "*", "*.bin")
            bin_files = sorted(glob.glob(bin_pattern))
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
    logger.info("MJ scan: %d unique headers from %d files (%.1fs)",
                len(headers), file_count, elapsed)
    return {
        "headers": headers,
        "file_count": file_count,
        "duplicate_count": duplicate_count,
        "skipped": skipped,
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
