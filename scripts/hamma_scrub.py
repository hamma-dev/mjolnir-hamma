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
