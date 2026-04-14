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
DEFAULT_AGS_HOST = "hamma"
DEFAULT_AGS_PATH = "/ags/data"
DEFAULT_MJ_PATH = "/media/pi"
DRIVE_PATTERN = "DATA??"
DEFAULT_LIMIT = 20  # max missing trigger detail lines in human report (0 = no limit)

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

    import math

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
        from datetime import datetime, timezone
        ts = base_time + sub_seconds
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.year < 2000:
            return None
        return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}'.format(dt.microsecond // 1000)
    except (ValueError, OverflowError, OSError):
        return None


def format_human_report(results, limit=DEFAULT_LIMIT):
    """Format results as human-readable report text.

    Parameters
    ----------
    results : dict
        Combined results from scanning and comparison.
    limit : int
        Max missing trigger detail lines to show (0 = no limit).

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

    return "\n".join(lines)


def format_json_report(results, ags_host):
    """Format results as JSON string.

    Parameters
    ----------
    results : dict
        Combined results from scanning and comparison.
    ags_host : str
        AGS host for metadata.

    Returns
    -------
    str
        JSON string.
    """
    from datetime import datetime, timezone

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
        help="Report only, no actions (for future phases B/C)",
    )
    return parser


def run(ags_host, ags_path, mj_path, json_output=False, output_file=None,
        limit=DEFAULT_LIMIT, since=None):
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

    Returns
    -------
    int
        Exit code.
    """
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

    if json_output:
        print(format_json_report(results, ags_host))
    else:
        print(format_human_report(results, limit=limit))

    if output_file:
        with open(output_file, 'w') as f:
            f.write(format_json_report(results, ags_host))
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
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
