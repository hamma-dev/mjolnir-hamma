"""Download HAMMA sensor data via rsync over SSH tunnels.

Pulls data from remote HAMMA sensors to the local server. Designed to run
on the matrix server where SSH tunnels to sensors terminate on localhost.

Usage:
    python hamma_download.py -s 41 -d /rgroup/hammadev/ignis/mj41 --start 2025-11-05
"""

# Standard library imports
import argparse
import fnmatch
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PORT_OFFSET = 10000
SSH_USER = "pi"
SSH_HOST = "localhost"
SSH_OPTIONS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
]
SSH_TIMEOUT = 30
MEDIA_PATH = "/media/pi"
COMPRESSED_SUBDIR = "compressed"
DRIVE_PATTERN = "DATA??"
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


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
    output = _ssh_run(sensor, "ls {}".format(MEDIA_PATH))
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

    output = _ssh_run(sensor, "ls {}".format(path))
    entries = output.split("\n")
    return [e for e in entries if DATE_DIR_RE.match(e)]
