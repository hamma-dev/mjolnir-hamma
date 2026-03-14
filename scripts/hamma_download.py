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
