#!/usr/bin/env bash
# Periodic AGS scrub: drain /ags/data (recover missing triggers + purge those
# confirmed on the mj-pi) under a non-blocking flock so a timer tick never
# overlaps a state_monitor-spawned scrub or a prior tick.
#
# The scrub invocation MUST stay in sync with state_monitor's `scrub_command`
# (config/main.toml): both share the /tmp lock and the /dev/shm heartbeat, so
# check_scrub_health can supervise whichever one is running.
#
# Invoked by hamma-scrub.service on the schedule in hamma-scrub.timer.
set -euo pipefail

LOCK=/tmp/hamma_scrub.lock
SCRUB=/home/pi/dev/mjolnir-hamma/scripts/hamma_scrub.py

# -n   : skip (don't queue) if a scrub already holds the lock -- overlapping
#        scrubs are pointless; the next tick retries.
# -E 0 : treat "lock already held" as success so a normal skip doesn't mark the
#        service failed; a real scrub error still propagates its exit code.
exec flock -n -E 0 "$LOCK" python3 "$SCRUB" --recover --purge --since auto
