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


def aggregate_results(results):
    """Aggregate per-trigger noise measurements.

    Parameters
    ----------
    results : list of dict
        Each dict from measure_noise().

    Returns
    -------
    dict
        Aggregated stats with threshold_V, slow{}, fast{} sub-dicts.
    """
    thresholds = np.array([r["threshold"] for r in results])
    threshold = float(np.median(thresholds))
    threshold_varied = len(np.unique(thresholds)) > 1

    def _channel_stats(key_noise, key_offset):
        noise_vals = np.array([r[key_noise] for r in results])
        offset_vals = np.array([r[key_offset] for r in results])

        if np.all(np.isnan(noise_vals)):
            return {
                "noise_vpp_median": None, "noise_vpp_max": None,
                "noise_vpp_iqr": None, "offset_median": None,
                "offset_max": None, "offset_iqr": None,
                "noise_thresh_pct": None,
            }

        q25, q75 = np.percentile(noise_vals, [25, 75])
        oq25, oq75 = np.percentile(offset_vals, [25, 75])
        noise_max = float(np.max(noise_vals))

        if threshold > 0:
            noise_thresh_pct = round(100.0 * noise_max / threshold, 1)
        else:
            noise_thresh_pct = None

        return {
            "noise_vpp_median": float(np.median(noise_vals)),
            "noise_vpp_max": noise_max,
            "noise_vpp_iqr": float(q75 - q25),
            "offset_median": float(np.median(offset_vals)),
            "offset_max": float(np.max(offset_vals)),
            "offset_iqr": float(oq75 - oq25),
            "noise_thresh_pct": noise_thresh_pct,
        }

    return {
        "threshold_V": threshold,
        "threshold_varied": threshold_varied,
        "slow": _channel_stats("slow_noise", "slow_offset"),
        "fast": _channel_stats("fast_noise", "fast_offset"),
    }


def check_warnings(agg, warn_pct):
    """Check if noise levels exceed warning threshold.

    Returns list of warning messages (empty if OK).
    """
    warnings = []
    for channel in ["slow", "fast"]:
        pct = agg[channel].get("noise_thresh_pct")
        if pct is not None and pct >= warn_pct:
            warnings.append(
                "{} channel noise at {:.0f}% of threshold".format(channel, pct)
            )
    return warnings


def format_report(sensor_id, files_analyzed, agg, warnings):
    """Format human-readable noise report.

    Parameters
    ----------
    sensor_id : str
        Sensor identifier (e.g., 'mj05').
    files_analyzed : int
        Number of files successfully analyzed.
    agg : dict
        Aggregated results from aggregate_results().
    warnings : list of str
        Warning messages.

    Returns
    -------
    str
        Formatted report string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append("=== Noise Check: {} | {} ===".format(sensor_id, now))
    lines.append("Files analyzed: {}".format(files_analyzed))
    lines.append("Threshold: {:.4f}V".format(agg["threshold_V"]))
    if agg.get("threshold_varied"):
        lines.append("  NOTE: threshold varied across triggers (using median)")
    lines.append("")

    # Noise table
    hdr = "{:<10s} {:>12s} {:>10s} {:>10s} {:>18s}".format(
        "Channel", "Median(Vpp)", "Max(Vpp)", "IQR(Vpp)", "Noise/Thresh(max)")
    lines.append(hdr)
    for ch in ["slow", "fast"]:
        s = agg[ch]
        if s["noise_vpp_median"] is None:
            lines.append("{:<10s} {:>12s} {:>10s} {:>10s} {:>18s}".format(
                ch, "N/A", "N/A", "N/A", "N/A"))
        else:
            pct_str = "{:.1f}%".format(s["noise_thresh_pct"]) if s["noise_thresh_pct"] is not None else "N/A"
            lines.append("{:<10s} {:>11.4f}V {:>9.4f}V {:>9.4f}V {:>17s}".format(
                ch, s["noise_vpp_median"], s["noise_vpp_max"],
                s["noise_vpp_iqr"], pct_str))
    lines.append("")

    # Offset table
    hdr2 = "{:<10s} {:>12s} {:>10s} {:>10s}".format(
        "Channel", "Median(Off)", "Max(Off)", "IQR(Off)")
    lines.append(hdr2)
    for ch in ["slow", "fast"]:
        s = agg[ch]
        if s["offset_median"] is None:
            lines.append("{:<10s} {:>12s} {:>10s} {:>10s}".format(
                ch, "N/A", "N/A", "N/A"))
        else:
            lines.append("{:<10s} {:>11.4f}V {:>9.4f}V {:>9.4f}V".format(
                ch, s["offset_median"], s["offset_max"], s["offset_iqr"]))
    lines.append("")

    # Status
    if warnings:
        for w in warnings:
            lines.append("Status: WARNING - {}".format(w))
    else:
        lines.append("Status: OK")

    return "\n".join(lines)


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
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug logging",
    )
    return parser


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
        mj_path=args.mj_path,
        count=args.count,
        warn_pct=args.warn_pct,
        output=args.output,
        no_save=args.no_save,
    )
    sys.exit(rc)


def run(mj_path, count, warn_pct, output, no_save):
    """Run noise check and produce report.

    Parameters
    ----------
    mj_path : str
        Base path for DATA drive discovery.
    count : int
        Number of most recent files to analyze.
    warn_pct : int
        Warning percentage threshold.
    output : str
        Path for JSON output file.
    no_save : bool
        If True, skip saving JSON.

    Returns
    -------
    int
        Exit code (EXIT_OK or EXIT_ERROR).
    """
    # Discover files
    files = discover_files(mj_path, count)
    if not files:
        print("ERROR: No .bin files found under {}".format(mj_path))
        return EXIT_ERROR

    # Measure each file
    results = []
    failures = 0
    for filepath in files:
        result = measure_noise(filepath)
        if result is None:
            failures += 1
        else:
            results.append(result)

    if not results:
        print("ERROR: All {} files failed to read".format(len(files)))
        return EXIT_ERROR

    if failures > 0:
        logger.warning("%d of %d files failed to read", failures, len(files))

    # Extract sensor ID from first successful file
    sensor_id = extract_sensor_id(files[0])

    # Aggregate and check warnings
    agg = aggregate_results(results)
    warnings = check_warnings(agg, warn_pct)

    # Print report
    report = format_report(sensor_id, len(results), agg, warnings)
    print(report)

    # Save JSON
    if not no_save:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        json_data = {
            "sensor": sensor_id,
            "timestamp": now,
            "files_analyzed": len(results),
            "threshold_V": agg["threshold_V"],
            "slow": agg["slow"],
            "fast": agg["fast"],
            "status": "WARNING" if warnings else "OK",
            "warnings": warnings,
        }
        try:
            with open(output, "w") as f:
                json.dump(json_data, f, indent=2)
            logger.info("Results saved to %s", output)
        except IOError as e:
            logger.warning("Could not save JSON: %s", e)

    return EXIT_OK


if __name__ == "__main__":
    main()
