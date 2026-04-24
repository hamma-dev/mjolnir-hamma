#!/usr/bin/env python3
"""On-demand sensor noise level check.

Analyzes recent trigger files on a HAMMA mj Pi and reports noise levels
relative to the AGS trigger threshold.

Usage:
    python hamma_noise.py [--mj-path PATH] [--count N] [--warn-pct N]
"""

import argparse
import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_MJ_PATH = "/media/pi"
DRIVE_PATTERN = "DATA??"
DEFAULT_COUNT = 10
DEFAULT_WARN_PCT = 80
DEFAULT_OUTPUT = "/tmp/noise_check.json"

# Noise measurement constants (from hamma.header.core._diagnostic_data)
MEDSIZE = 20000  # samples for slow channel offset/noise window
NOISE_PERCENTILES = [0.01, 99.9]

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1


def discover_files(mj_path, count):
    """Find the most recent .bin trigger files across DATA drives.

    Parameters
    ----------
    mj_path : str
        Base path containing DATA?? drives (e.g., /media/pi).
    count : int
        Maximum number of files to return.

    Returns
    -------
    list of str
        File paths sorted by modification time (newest first).
    """
    pattern = os.path.join(mj_path, DRIVE_PATTERN)
    drives = sorted(glob.glob(pattern))
    if not drives:
        logger.warning("No DATA drives found at %s", mj_path)
        return []

    bin_files = []
    for drive in drives:
        found = glob.glob(os.path.join(drive, "*", "*.bin"))
        bin_files.extend(found)

    # Sort by modification time, newest first
    bin_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return bin_files[:count]


def _load_header(filepath):
    """Load a single .bin file via hamma.Header."""
    import hamma
    return hamma.Header(filepath)


def extract_sensor_id(filepath):
    """Extract sensor ID from a .bin filename.

    Returns sensor ID (e.g., 'mj05') or 'unknown'.
    """
    basename = os.path.basename(filepath)
    parts = basename.split("_")
    if len(parts) >= 2:
        return parts[0]
    return "unknown"


def measure_noise(filepath):
    """Measure noise and offset from a single trigger file.

    Returns dict with keys: threshold, slow_noise, slow_offset,
    fast_noise, fast_offset. Returns None on read failure.
    """
    try:
        hdr = _load_header(filepath)
    except Exception as e:
        logger.warning("Failed to read %s: %s", filepath, e)
        return None

    data = hdr.get_data(0, noTimes=True)
    threshold = float(hdr.data.threshold[0])

    # Slow channel
    perc = np.percentile(data.volt[0:MEDSIZE], NOISE_PERCENTILES)
    slow_noise = perc[1] - perc[0]
    slow_offset = np.median(data.volt[0:MEDSIZE])

    # Fast channel
    if data.voltFast is not None:
        fast_med_size = MEDSIZE * 10
        perc_fast = np.percentile(data.voltFast[0:fast_med_size], NOISE_PERCENTILES)
        fast_noise = perc_fast[1] - perc_fast[0]
        fast_offset = np.median(data.voltFast[0:fast_med_size])
    else:
        fast_noise = np.nan
        fast_offset = np.nan

    return {
        "threshold": threshold,
        "slow_noise": float(slow_noise),
        "slow_offset": float(slow_offset),
        "fast_noise": float(fast_noise),
        "fast_offset": float(fast_offset),
    }


def _build_parser():
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Check sensor noise levels from recent trigger files.",
    )
    parser.add_argument(
        "--mj-path", default=DEFAULT_MJ_PATH,
        help="Base path for DATA drive discovery (default: %(default)s)",
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help="Number of most recent files to analyze (default: %(default)s)",
    )
    parser.add_argument(
        "--warn-pct", type=int, default=DEFAULT_WARN_PCT,
        help="Noise/threshold %% that triggers a warning (default: %(default)s)",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="Path for JSON results file (default: %(default)s)",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="Skip saving JSON, print only",
    )
    return parser


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if os.environ.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    rc = run(
        mj_path=args.mj_path,
        count=args.count,
        warn_pct=args.warn_pct,
        output=args.output,
        no_save=args.no_save,
    )
    sys.exit(rc)


def run(mj_path, count, warn_pct, output, no_save):
    """Main logic. Returns exit code."""
    # Placeholder — implemented in Task 5
    return EXIT_OK


if __name__ == "__main__":
    main()
