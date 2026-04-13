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
