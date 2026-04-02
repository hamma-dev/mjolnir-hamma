#!/usr/bin/env python3
"""Download HAMMA sensor data via rsync over SSH tunnels.

Pulls data from remote HAMMA sensors to the local server. Designed to run
on the meteor server, connecting to sensors via SSH tunnels on hamma.dev.

Usage:
    python hamma_download.py -s 41 -d /rgroup/hammadev/ignis/mj41 --start 2025-11-05
    python hamma_download.py -s 41 -d /rgroup/hammadev/ignis/mj41 --sync --slurm
"""

# Standard library imports
import argparse
import fnmatch
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PORT_OFFSET = 10000
SSH_USER = "datasync"
SSH_HOST = "localhost"
JUMP_HOST = "monitor@hamma.dev"
SSH_OPTIONS = [
    "-J", JUMP_HOST,
]
SSH_TIMEOUT = 30
RSYNC_TIMEOUT = 300
MEDIA_PATH = "/media/pi"
COMPRESSED_SUBDIR = "compressed"
DRIVE_PATTERN = "DATA??"
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")

SLURM_TEMPLATE = """#!/bin/bash
#SBATCH --mail-user=bitzerp@uah.edu
#SBATCH -J {jobname}
#SBATCH -p standard
#SBATCH --ntasks 1
#SBATCH -t 0-04:00
#SBATCH --mem-per-cpu=2G
#SBATCH --mail-type=END,FAIL
#SBATCH -o {log_dir}/{jobname}-%j.out
#SBATCH -e {log_dir}/{jobname}-%j.err

echo "Starting at $(date)"
echo "Running on host: $(hostname)"

{command}
rc=$?

echo "Finished at $(date) with exit code $rc"
exit $rc
"""


def _ssh_run(sensor, command):
    """Run a command on a sensor via SSH tunnel.

    Parameters
    ----------
    sensor : int
        Sensor number (derives port as PORT_OFFSET + sensor).
    command : str
        Shell command to execute on the remote sensor.

    Returns
    -------
    str
        Stripped stdout from the command.

    Raises
    ------
    RuntimeError
        If SSH connection fails or times out.
    """
    port = str(PORT_OFFSET + sensor)
    cmd = (
        ["ssh"]
        + SSH_OPTIONS
        + ["-p", port, "{}@{}".format(SSH_USER, SSH_HOST), command]
    )
    logger.debug("SSH command: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=SSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "SSH to sensor {} timed out after {}s".format(sensor, SSH_TIMEOUT)
        )
    if result.returncode != 0:
        raise RuntimeError(
            "SSH to sensor {} failed (exit {}): {}".format(
                sensor, result.returncode, result.stderr.strip()
            )
        )
    return result.stdout.strip()


def _discover_drives(sensor):
    """Discover DATA drives mounted on a sensor.

    Parameters
    ----------
    sensor : int
        Sensor number.

    Returns
    -------
    list of str
        Drive names matching DATA?? pattern (e.g., ['DATA37']).

    Raises
    ------
    RuntimeError
        If no DATA drives are found.
    """
    output = _ssh_run(sensor, "ls -1 {}".format(MEDIA_PATH))
    entries = output.split("\n")
    drives = [e for e in entries if fnmatch.fnmatch(e, DRIVE_PATTERN)]
    if not drives:
        raise RuntimeError("No DATA drive found on sensor {}".format(sensor))
    logger.info("Found drives on sensor %d: %s", sensor, ", ".join(drives))
    return drives


def _parse_date(value):
    """Parse a date string or datetime into (date_str, hour_str|None).

    Parameters
    ----------
    value : str or datetime
        Date input. Strings: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH'.
        datetime objects: date and hour extracted, minutes/seconds ignored.

    Returns
    -------
    tuple of (str, str or None)
        (date_str 'YYYY-MM-DD', hour_str 'HH' or None if date-only)
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d"), value.strftime("%H")
    value = str(value)
    # Try YYYY-MM-DDTHH
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H")
    except ValueError:
        pass
    # Try YYYY-MM-DD
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value, None
    except ValueError:
        pass
    raise ValueError("Cannot parse date: '{}'. Use YYYY-MM-DD or YYYY-MM-DDTHH".format(value))


def _filter_dirs(dirs, start, end):
    """Filter directory names by date/time range.

    Parameters
    ----------
    dirs : list of str
        Directory names in YYYY-MM-DDTHH format.
    start : str
        Start date ('YYYY-MM-DD' or 'YYYY-MM-DDTHH').
    end : str or None
        End date (same formats). If None, uses start only.

    Returns
    -------
    list of str
        Sorted list of matching directory names.
    """
    start_date, start_hour = _parse_date(start)
    if end is not None:
        end_date, end_hour = _parse_date(end)
    else:
        end_date, end_hour = start_date, start_hour

    # Build min/max datetime strings for comparison
    if start_hour is not None:
        range_min = "{}T{}".format(start_date, start_hour)
    else:
        range_min = "{}T00".format(start_date)

    if end_hour is not None:
        range_max = "{}T{}".format(end_date, end_hour)
    else:
        range_max = "{}T23".format(end_date)

    matched = []
    for d in sorted(dirs):
        if not DATE_DIR_RE.match(d):
            continue
        if range_min <= d <= range_max:
            matched.append(d)
    return matched


def _list_remote_dirs(sensor, drive, compressed=True):
    """List date directories on a remote sensor drive.

    Parameters
    ----------
    sensor : int
        Sensor number.
    drive : str
        Drive name (e.g., 'DATA37').
    compressed : bool
        If True, list compressed/ subdir. If False, list drive root.

    Returns
    -------
    list of str
        Directory names matching YYYY-MM-DDTHH pattern.
    """
    if compressed:
        path = "{}/{}/{}".format(MEDIA_PATH, drive, COMPRESSED_SUBDIR)
    else:
        path = "{}/{}".format(MEDIA_PATH, drive)

    try:
        output = _ssh_run(sensor, "ls -1 {}".format(path))
    except RuntimeError:
        logger.debug("Path %s not found on sensor %d drive %s", path, sensor, drive)
        return []
    entries = output.split("\n")
    return [e for e in entries if DATE_DIR_RE.match(e)]


def download(sensor, dest, start, end=None, compressed=True, dry_run=False):
    """Download data from a HAMMA sensor via rsync.

    Parameters
    ----------
    sensor : int
        Sensor number (e.g., 41). Derives SSH port as 10000 + sensor.
    dest : str
        Local destination path.
    start : str or datetime
        Start of time window ('YYYY-MM-DD' or 'YYYY-MM-DDTHH').
    end : str or datetime or None
        End of time window. If None, downloads only what start specifies.
    compressed : bool
        If True, pull from compressed/ subdirectory. If False, pull raw data.
    dry_run : bool
        If True, pass --dry-run to rsync.

    Returns
    -------
    int
        rsync return code (0 = success). Returns 0 if no directories match.

    Raises
    ------
    RuntimeError
        If SSH connection fails or no DATA drive found.
    """
    drives = _discover_drives(sensor)
    port = str(PORT_OFFSET + sensor)
    last_rc = 0

    for drive in drives:
        remote_dirs = _list_remote_dirs(sensor, drive, compressed=compressed)
        matched = _filter_dirs(remote_dirs, start, end)

        if not matched:
            logger.warning(
                "No directories matching request on sensor %d drive %s",
                sensor, drive,
            )
            continue

        # Build rsync source paths
        if compressed:
            base = "{}/{}/{}".format(MEDIA_PATH, drive, COMPRESSED_SUBDIR)
        else:
            base = "{}/{}".format(MEDIA_PATH, drive)

        sources = [
            "{}@{}:{}/{}".format(SSH_USER, SSH_HOST, base, d)
            for d in matched
        ]

        cmd = (
            ["rsync", "-avz", "--timeout={}".format(RSYNC_TIMEOUT)]
            + (["--dry-run"] if dry_run else [])
            + ["-e", "ssh {} -p {}".format(" ".join(SSH_OPTIONS), port)]
            + sources
            + [dest]
        )

        logger.debug("Rsync command: %s", " ".join(cmd))
        logger.info(
            "Downloading %d directories from sensor %d drive %s",
            len(matched), sensor, drive,
        )

        result = subprocess.run(
            cmd,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

        if result.returncode != 0:
            logger.error(
                "Rsync failed (exit %d): %s",
                result.returncode, result.stderr.strip(),
            )
            last_rc = result.returncode
        else:
            logger.info("Download complete for drive %s", drive)

    return last_rc


def sync(sensor, dest, cleanup=False, dry_run=False):
    """Sync all compressed data from a sensor, optionally cleaning up.

    Downloads everything in compressed/ directories on the sensor,
    skipping the current hour's directory to avoid partial files.
    With cleanup=True, successfully transferred files are removed
    from the sensor via rsync --remove-source-files.

    Parameters
    ----------
    sensor : int
        Sensor number (e.g., 41).
    dest : str
        Local destination path.
    cleanup : bool
        If True, delete source .hmc files after successful transfer.
        Suppressed when dry_run is True. Default is False.
    dry_run : bool
        If True, pass --dry-run to rsync. Default is False.

    Returns
    -------
    int
        rsync return code (0 = success).
    """
    drives = _discover_drives(sensor)
    port = str(PORT_OFFSET + sensor)
    last_rc = 0

    # Skip the current hour to avoid grabbing files mid-compression
    now = datetime.utcnow()
    current_dir = now.strftime("%Y-%m-%dT%H")

    for drive in drives:
        remote_dirs = _list_remote_dirs(sensor, drive, compressed=True)

        # Filter out current hour
        safe_dirs = [d for d in remote_dirs if d < current_dir]

        if not safe_dirs:
            logger.info(
                "No completed directories to sync on sensor %d drive %s",
                sensor, drive)
            continue

        base = "{}/{}/{}".format(MEDIA_PATH, drive, COMPRESSED_SUBDIR)
        sources = [
            "{}@{}:{}/{}".format(SSH_USER, SSH_HOST, base, d)
            for d in safe_dirs
        ]

        cmd = (
            ["rsync", "-avz", "--timeout={}".format(RSYNC_TIMEOUT)]
            + (["--dry-run"] if dry_run else [])
            + (["--remove-source-files"] if cleanup and not dry_run else [])
            + ["-e", "ssh {} -p {}".format(" ".join(SSH_OPTIONS), port)]
            + sources
            + [dest]
        )

        logger.info(
            "Syncing %d directories from sensor %d drive %s%s",
            len(safe_dirs), sensor, drive,
            " (cleanup after)" if cleanup and not dry_run else "",
        )

        result = subprocess.run(
            cmd,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

        if result.returncode != 0:
            logger.error(
                "Rsync failed (exit %d): %s",
                result.returncode, result.stderr.strip())
            last_rc = result.returncode
        else:
            logger.info(
                "Sync complete for drive %s (%d dirs)", drive, len(safe_dirs))

    return last_rc


def _submit_slurm(args, log_dir):
    """Generate a slurm batch script and submit via sbatch.

    Rebuilds the command line from parsed args, excluding --slurm,
    and wraps it in a SLURM batch script.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    log_dir : str
        Directory for slurm log files.

    Returns
    -------
    int
        0 if sbatch submission succeeded, 1 otherwise.
    """
    # Rebuild the command without --slurm
    script_path = os.path.abspath(__file__)
    cmd_parts = [sys.executable, script_path]
    cmd_parts.extend(["-s", str(args.sensor)])
    cmd_parts.extend(["-d", args.dest])
    if args.sync:
        cmd_parts.append("--sync")
        if args.cleanup:
            cmd_parts.append("--cleanup")
    else:
        cmd_parts.extend(["--start", args.start])
        if args.end is not None:
            cmd_parts.extend(["--end", args.end])
    if args.raw:
        cmd_parts.append("--raw")
    if args.dry_run:
        cmd_parts.append("--dry-run")
    if args.verbose:
        cmd_parts.append("-v")

    command = " ".join(cmd_parts)

    # Build job name
    if args.sync:
        jobname = "hamma_dl_mj{:02d}_sync".format(args.sensor)
    else:
        jobname = "hamma_dl_mj{:02d}_{}".format(args.sensor, args.start)

    os.makedirs(log_dir, exist_ok=True)

    script_content = SLURM_TEMPLATE.format(
        jobname=jobname,
        log_dir=log_dir,
        command=command,
    )

    # Write to a temp file, submit, clean up
    fd, script_path = tempfile.mkstemp(suffix=".sh", prefix=jobname + "_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script_content)
        logger.info("Submitting slurm job: %s", jobname)
        logger.debug("Batch script:\n%s", script_content)
        result = subprocess.run(
            ["sbatch", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        if result.returncode == 0:
            logger.info("%s", result.stdout.strip())
            return 0
        else:
            logger.error("sbatch failed: %s", result.stderr.strip())
            return 1
    finally:
        os.unlink(script_path)


def _build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="Download data from HAMMA sensors via rsync.",
    )
    parser.add_argument(
        "-s", "--sensor", type=int, required=True,
        help="Sensor number (e.g., 41)",
    )
    parser.add_argument(
        "-d", "--dest", required=True,
        help="Destination path (e.g., /rgroup/hammadev/ignis/mj41)",
    )
    parser.add_argument(
        "--start", default=None,
        help="Start date/time (YYYY-MM-DD or YYYY-MM-DDTHH)",
    )
    parser.add_argument(
        "--end", default=None,
        help="End date/time (YYYY-MM-DD or YYYY-MM-DDTHH)",
    )
    parser.add_argument(
        "--sync", action="store_true", default=False,
        help="Sync mode: download all available compressed data",
    )
    parser.add_argument(
        "--cleanup", action="store_true", default=False,
        help="Remove .hmc files from sensor after successful transfer (sync mode only)",
    )
    parser.add_argument(
        "--raw", action="store_true", default=False,
        help="Download raw data instead of compressed",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", default=False,
        dest="dry_run",
        help="Rsync dry run (show what would transfer)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--slurm", action="store_true", default=False,
        help="Submit as a slurm batch job instead of running directly",
    )
    parser.add_argument(
        "--log-dir", default=os.path.expanduser("~/slurm_log"),
        dest="log_dir",
        help="Directory for slurm log files (default: ~/slurm_log)",
    )
    return parser


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    # Validate: either --sync or --start is required
    if not args.sync and args.start is None:
        parser.error("--start is required (or use --sync mode)")
    if args.sync and (args.start is not None or args.end is not None):
        parser.error("--sync cannot be combined with --start/--end")

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )

    if args.slurm:
        rc = _submit_slurm(args, args.log_dir)
        sys.exit(rc)

    try:
        if args.sync:
            rc = sync(
                sensor=args.sensor,
                dest=args.dest,
                cleanup=args.cleanup,
                dry_run=args.dry_run,
            )
        else:
            rc = download(
                sensor=args.sensor,
                dest=args.dest,
                start=args.start,
                end=args.end,
                compressed=not args.raw,
                dry_run=args.dry_run,
            )
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    sys.exit(rc)


if __name__ == "__main__":
    main()
