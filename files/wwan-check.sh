#!/bin/sh

# This script is now just a wrapper to ensure only one instance of the
# main python connection script runs at a time. The actual connectivity
# check is handled within the python script itself.

# Path to the main python script
PYTHON_SCRIPT="/usr/local/bin/50_bring_wwan0_up.py"

# Path for the lock file
LOCK_FILE="/tmp/wwan_connect.lock"

# Use flock to acquire an exclusive non-blocking lock (-n).
# If the lock is already held, flock exits immediately with status 1.
# -E 1: Use exit code 1 if lock fails (explicitly, though default)
# -c "command": Run the command if the lock is acquired.
exec env flock -n -E 1 "$LOCK_FILE" -c "/usr/bin/python3 $PYTHON_SCRIPT"

# The exit status of this script will be the exit status of flock (0 on success, 1 if lock failed)
# or the exit status of the python script if it runs and fails.


