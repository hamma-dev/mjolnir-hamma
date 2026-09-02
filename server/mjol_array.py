#!/usr/bin/env python

import importlib.util
import os
import subprocess
import sys
import argparse
from pathlib import Path

# pandas and numpy are imported lazily inside the methods that need them
# (collect_data, status_latest_trigger). Importing at module scope makes
# `--help`, `--up`, and `--down` fail on hosts without those packages
# installed in the active Python.

# Define constants that hold the "mjolnir numbers" for each array.
# We should only ever need to pass this into the class,
# so having it as a global constant is overkill.
# But, it gives us a place up top to change/add if necessary.
HAMMA_SENSORS = list(range(1, 10))
PAMMA_SENSORS = [50, 51, 52, 53, 54, 56]
AUMMA_SENSORS = [41, 42, 43, ]


# Bounds on ssh round trips to a unit.
#
# These are NOT belt-and-braces. `ConnectTimeout=5` in _pi_ssh_cmd bounds only
# establishing the TCP connection. A tunnel that accepts the connection and then
# stalls -- "Connection timed out during banner exchange", routine on this fleet
# -- leaves subprocess.run() blocking with no bound at all.
#
# The VPS array_status pipeline wedged this way: worker processes blocked on ssh,
# never returned, and ended up defunct while brokkr's main process stayed alive,
# so `systemctl` reported `active (running)` and webgen kept regenerating the
# public status page from a stale CSV. NOTE the service was NOT quiet about it --
# `NRestarts` read 202 at the time. It was restarting repeatedly and nobody was
# watching; the root cause of the recurring wedge is still not established, and
# these bounds do not claim to explain it.
#
# Values are ~15x the round trip measured across the fleet, including the worst
# geography (mj43/Australia, `brokkr status` 9.5 s -- 32% of its 30 s budget).
#
# Worst case per unit, if every call stalls: services 2x15 (it loops over TWO
# services) + trigger 20 + fcm 30 = 80 s. Sweeps are sequential, so:
#     hamma  9 units = 720 s  vs 600 s interval  -- EXCEEDS, see below
#     pamma  6 units = 480 s  vs 900 s interval
#     aumma  3 units = 240 s  vs 600 s interval
# Overrunning does not overlap sweeps -- brokkr's run_periodic is a single
# blocking loop, so hamma degrades to ~720 s cadence rather than compounding.
# That is a bounded ~20% cadence loss in an all-units-stalled scenario, and is
# accepted rather than fixed by tightening, which would risk false negatives.
SSH_SERVICES_TIMEOUT_S = 15
SSH_TRIGGER_TIMEOUT_S = 20
SSH_STATUS_TIMEOUT_S = 30


# These deliberately re-check what ags.py validates on the Pi. mjol_array
# runs on the VPS and ags.py on the sensor; they deploy as separate git
# checkouts on different hosts and cannot share a module. Validating here
# fails fast AND keeps unchecked operator input out of the ssh command line
# (the values are interpolated into a remote shell command). ags.py remains
# the authority on valid ranges; keep these in sync with it.
def _validate_threshold_cli(channel, millivolts):
    if str(channel) not in ("1", "2"):
        raise ValueError("threshold channel must be 1 or 2")
    if float(millivolts) < 0:
        raise ValueError("threshold mV must be non-negative")


def _validate_gain_cli(channel, level):
    if channel not in ("fast-e", "slow-e"):
        raise ValueError("gain channel must be fast-e or slow-e")
    if int(level) not in (0, 1, 2, 3):
        raise ValueError("gain level must be 0, 1, 2, or 3")


# HAM-189 write-hook. fleet_probe.py (server/fleet_probe.py) snapshots each
# unit's configuration to state/fleet-state.csv in a sensor-log clone and
# writes it only when it changed, so `git log` on that file is the fleet's
# state-change history. Its own docstring says every change surfaces until
# something updates the snapshot as part of MAKING a change. This is that:
# after a control operation below actually succeeds, the affected field of
# the affected unit's row is updated -- and, once per SWEEP rather than once
# per unit (see _log_field_changes_batch()), committed+pushed -- so the next
# probe run finds no diff and stays quiet about it.
#
# This must never be able to break fleet control -- see
# _log_field_changes_batch() and the _resolve_*_entry() functions below,
# every one of which reports a failure to stderr and swallows it rather
# than raising, and none of which run until every control operation in the
# sweep has already completed.
#
# It reuses fleet_probe's FIELDS/read_snapshot/render/commit_snapshot rather
# than re-implementing CSV handling a second time, so the two cannot drift.
#
# Threshold/gain are a double special case:
#
#  1. fleet_probe reads them from the sensor's PERSISTED startup file
#     (/ags/scripts/startup), not the live AGS register -- so a
#     set-threshold/set-gain WITHOUT --persist is invisible to the probe.
#     Logging it anyway would make the snapshot claim a value the probe can
#     never confirm, and the very next probe run would "revert" it as an
#     unlogged change -- exactly the false alarm this feature exists to
#     prevent. So only a persisted change is logged.
#
#  2. ags.py's CLI has no sys.exit() calls (its set-threshold/set-gain
#     subcommands just print() the AGS reply and return), so a successful
#     ssh round trip proves nothing about whether the FIRMWARE accepted the
#     value -- a rejected value (out of range) or a socket timeout with no
#     reply both still exit 0. Logging in that case has the same false-alarm
#     failure mode as (1): the persisted file was never actually written, so
#     the next probe run reads the real, unchanged value and reports a
#     spurious "unlogged change". _resolve_threshold_entry()/
#     _resolve_gain_entry() gate on the AGS reply itself, reusing ags.py's
#     own _reply_indicates_error() -- the same check ags.py uses to gate
#     persist_startup() -- so the two cannot drift.
#
# See _resolve_threshold_entry()/_resolve_gain_entry().
def _fleet_probe_module():
    """Load server/fleet_probe.py (a sibling file) by path.

    Returns the module, or None if it could not be loaded -- caller must
    treat that as "logging unavailable", not raise.
    """
    try:
        here = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "fleet_probe", str(here / "fleet_probe.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _load_ags_module():
    """Find ags.py the same way fleet_probe.py does (sibling ../scripts,
    $FLEET_PROBE_AGS_PATH, then the VPS checkout) and return the module, or
    None. Reuses fleet_probe.load_ags_parser() so this does not grow a
    second, divergent way to find ags.py.
    """
    fp = _fleet_probe_module()
    if fp is None:
        return None
    parser, _source = fp.load_ags_parser()
    if parser is None:
        return None
    return sys.modules.get("ags")


def _log_field_changes_batch(log_repo, entries, reason=None):
    """Apply and commit MULTIPLE (unit, field, value) entries in ONE
    read+write+commit+push, instead of one per entry.

    HAM-189 red-team finding #2: updown_array()/set_threshold_array()/
    set_gain_array() used to commit+push a single field change INSIDE the
    per-port loop, so e.g. a 9-unit sweep with a slow network could
    serialize up to ~9x the git round trip (up to ~240s each -- see
    fleet_probe.commit_snapshot()) BETWEEN individual sensors' control
    operations. That is the same class of problem PR #92 fixed for ssh --
    unrelated slow I/O blocking a live fleet-control fan-out. Every control
    operation in the sweep now runs to completion first (see
    updown_array() etc.); this is called once at the end with every entry
    whose op actually succeeded, mirroring fleet_probe.py's own main()
    (one commit for all probed changes).

    Best-effort: every failure is reported to stderr and swallowed, never
    raised -- this bookkeeping must never be able to fail (or slow down)
    the control operations it is recording, all of which have already
    completed by the time this runs. Returns True only if every entry with
    a snapshot row was applied and (if anything actually changed) committed
    and pushed.

    Entries are applied to one in-memory snapshot dict before anything is
    written, so one entry with no baseline row yet (see below) is skipped
    without discarding the others -- a partial batch still writes and
    commits everything that COULD be applied, preserving per-unit accuracy.
    """
    if not entries:
        return True
    if not log_repo:
        for unit, field, _value in entries:
            print(f"[LOG] no --log-repo/$FLEET_LOG_REPO configured; change "
                  f"to {unit} {field} not recorded in the fleet-state "
                  f"snapshot.", file=sys.stderr)
        return False

    # Everything below is inside one try/except, deliberately including
    # loading fleet_probe.py itself: this is bookkeeping about control
    # operations that have ALREADY succeeded, and no failure in it --
    # however unexpected -- may be allowed to propagate back out.
    try:
        fp = _fleet_probe_module()
        if fp is None:
            for unit, field, _value in entries:
                print(f"[LOG] could not load fleet_probe.py; change to "
                      f"{unit} {field} not recorded in the fleet-state "
                      f"snapshot.", file=sys.stderr)
            return False

        snapshot_rel = os.path.join("state", "fleet-state.csv")
        snapshot = os.path.join(log_repo, snapshot_rel)

        rows = fp.read_snapshot(snapshot)

        applied = []
        for unit, field, value in entries:
            row = rows.get(unit)
            if row is None:
                # No baseline row for this unit yet -- inventing one here
                # (with the other ten fields reading "unknown") would
                # itself pollute the history fleet_probe exists to keep
                # clean. Let the next probe run establish the row; this
                # change becomes its baseline value rather than a change.
                print(f"[LOG] no snapshot row for {unit} yet; change to "
                      f"{field} not recorded (the next fleet probe will "
                      f"establish one).", file=sys.stderr)
                continue
            if row.get(field) == value:
                # Already at rest -- nothing to apply for this entry.
                continue
            row[field] = value
            rows[unit] = row
            applied.append((unit, field, value))

        if not applied:
            # Nothing to commit is not a failure -- and must not attempt
            # one (an empty `git commit` fails).
            return True

        directory = os.path.dirname(snapshot)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(snapshot, "w") as handle:
            handle.write(fp.render(rows))

        message = "state: " + ", ".join(
            f"{u} {f} = {v}" for u, f, v in applied)
        if reason:
            message += f"\n\n{reason}"
        ok, detail = fp.commit_snapshot(log_repo, snapshot_rel, message)
        if not ok:
            print(f"[LOG] fleet-state snapshot updated locally but "
                  f"commit/push failed for {len(applied)} change(s): "
                  f"{detail}", file=sys.stderr)
        return ok
    except Exception as error:            # noqa: BLE001 - report, don't mask
        print(f"[LOG] failed to record {len(entries)} change(s) in the "
              f"fleet-state snapshot: {type(error).__name__}: {error}",
              file=sys.stderr)
        return False


def _resolve_front_end_entry(port, bring_up):
    """Build the (unit, field, value) entry for a successful --up/--down,
    or None on an unexpected failure. Pure -- no I/O; the caller batches
    this with every other successful entry from the same sweep and commits
    them together (see updown_array() / _log_field_changes_batch()).

    Front-end resolution cannot really fail (there is no reply-acceptance
    check to make -- sensors.py's own sys.exit(1), already checked by the
    caller before this runs, is the only success signal). Wrapped in
    try/except anyway, consistent with the threshold/gain resolvers below,
    because this must never be able to break fleet control either.
    """
    try:
        unit = "mjolnir{:02d}".format(port - 10000)
        return (unit, "front_end", "on" if bring_up else "off")
    except Exception as error:            # noqa: BLE001 - report, don't mask
        print(f"[LOG] failed to build a front_end log entry for port "
              f"{port}: {type(error).__name__}: {error}", file=sys.stderr)
        return None


def _resolve_threshold_entry(port, channel, millivolts, reply):
    """Build a (unit, field, value) entry for a persisted threshold change,
    or None if it must not be recorded. Pure -- no I/O; see
    _resolve_front_end_entry().

    HAM-189 red-team finding #1: gate on the AGS reply itself (via ags.py's
    own _reply_indicates_error(), reused rather than re-implemented so the
    two cannot drift) before ever computing a value to log -- see the
    module comment above for why. This also folds in the mV<->AGS round
    trip so the value recorded matches exactly what fleet_probe's own
    parser will read back out of the persisted startup file.

    Design decision (finding #1): a rejected/unconfirmed reply suppresses
    ONLY this log entry, not the control op's own reported outcome.
    mjol_array.py has no exit-code convention to plug a "the value was
    rejected" signal into -- main() never calls sys.exit() for any control
    op here, unlike sensors.py -- so changing it would be a separate,
    larger design change. The operator is not left silently unaware,
    though: _run_ags_command()'s non-quiet path already prints the raw AGS
    reply (including any "Error -" line) to stdout, and the [LOG] notice
    below adds an explicit, unmissable flag that the persisted value was
    NOT recorded, right at the point the false alarm would otherwise start.
    """
    # unit/field are computed INSIDE the try (not before it, as an earlier
    # revision of this pairing had it for the threshold path only) --
    # matching _resolve_gain_entry() and _resolve_front_end_entry(): this
    # must never be able to raise back out to the control-op caller either.
    try:
        unit = "mjolnir{:02d}".format(port - 10000)
        field = "threshold_1_mv" if str(channel) == "1" else "threshold_2_mv"
        ags = _load_ags_module()
        if ags is None:
            print(f"[LOG] ags.py not found; threshold change on {unit} "
                  f"ch{channel} not recorded (cannot verify AGS accepted "
                  f"it or compute the persisted-file round-trip value "
                  f"without it).", file=sys.stderr)
            return None
        if ags._reply_indicates_error(reply):
            print(f"[LOG] {unit} ch{channel}: AGS reply indicates the "
                  f"persisted threshold value was REJECTED or not "
                  f"confirmed; NOT recording it in the fleet-state "
                  f"snapshot -- see the AGS reply above.", file=sys.stderr)
            return None
        ags_value = ags.mv_to_ags(millivolts)
        formatted = ags._format_ags(ags_value)
        roundtrip_mv = round(ags.ags_to_mv(float(formatted)), 1)
    except Exception as error:            # noqa: BLE001 - report, don't mask
        print(f"[LOG] could not compute the persisted threshold value for "
              f"ch{channel} on port {port}: {type(error).__name__}: "
              f"{error}", file=sys.stderr)
        return None

    return (unit, field, str(roundtrip_mv))


def _resolve_gain_entry(port, channel, level, reply):
    """Build a (unit, field, value) entry for a persisted gain change, or
    None if it must not be recorded. See _resolve_threshold_entry() -- same
    AGS-reply gate and the same reasoning for suppressing only the log
    entry, not the control op's own reported outcome.
    """
    try:
        unit = "mjolnir{:02d}".format(port - 10000)
        field = "gain_fast" if channel == "fast-e" else "gain_slow"
        ags = _load_ags_module()
        if ags is None:
            print(f"[LOG] ags.py not found; gain change on {unit} "
                  f"{channel} not recorded (cannot verify AGS accepted "
                  f"it).", file=sys.stderr)
            return None
        if ags._reply_indicates_error(reply):
            print(f"[LOG] {unit} {channel}: AGS reply indicates the "
                  f"persisted gain value was REJECTED or not confirmed; "
                  f"NOT recording it in the fleet-state snapshot -- see "
                  f"the AGS reply above.", file=sys.stderr)
            return None
    except Exception as error:            # noqa: BLE001 - report, don't mask
        print(f"[LOG] could not verify the AGS reply for {channel} on "
              f"port {port}: {type(error).__name__}: {error}",
              file=sys.stderr)
        return None

    return (unit, field, str(int(level)))


class MjolnirArray():

    def __init__(self, sensors, sensor_name='Mjolnir'):
        # sensors should be numeric, and correspond to Mjolnir hostname number.

        self.sensors = sensors
        self.sensor_name = sensor_name


    @staticmethod
    def _pi_ssh_cmd(port):
        # We build a ssh command to the pi's in several places.
        # The command is usually passed to subprocess.
        # port is a numeric, fully qualified
        # returns list

        cmd = ['ssh', '-o', 'ConnectTimeout=5', 'pi@localhost', '-p', str(port)]
        return cmd

    @staticmethod
    def status(port):
        # Determine the status of a Pi
        # port is fully qualified integer
        # Return a boolean if the Pi is up (True) or down (False)

        cmd = ['nc', '-w', '1', 'localhost', str(port)]

        out = subprocess.run(cmd, stdout=subprocess.DEVNULL, timeout=5, stderr=subprocess.DEVNULL)
        nc_code = out.returncode

        return not nc_code

    @staticmethod
    def status_services(port):
        # port is fully qualified
        services = ['brokkr-hamma-default', 'sindri-hamma-client']
        base_cmd = ['systemctl', 'is-active', '--quiet', ]

        cmd = MjolnirArray._pi_ssh_cmd(port) + base_cmd

        ret = list()
        for _s in services:
            # Unlike the two callers below, this one has no surrounding
            # try/except, so TimeoutExpired must be caught here or it would abort
            # the whole sweep instead of degrading one reading.
            try:
                out = subprocess.run(cmd + [_s], stdout=subprocess.PIPE,
                                     timeout=SSH_SERVICES_TIMEOUT_S)
                ret.append(not out.returncode)
            except subprocess.TimeoutExpired:
                # Report the service as down. A unit we cannot reach must not be
                # reported as healthy -- that is the direction that hides faults.
                ret.append(False)

        return ret

    @staticmethod
    def status_latest_trigger(port):
        # port is fully qualified

        import ast

        # numpy is only present under the ltgenv python (the brokkr plugin
        # context). The VPS operator runs `mjol_array --status` under the
        # system python, which has no numpy -- so degrade gracefully to
        # stdlib equivalents. When numpy IS present the behaviour is
        # byte-identical to before (np.datetime64 / np.nan), so the
        # log.hamma.dev dashboard is unaffected.
        try:
            import numpy as np
            _nan = np.nan

            def _to_time(epoch_s):
                return np.datetime64(int(epoch_s), 's')
        except ImportError:
            import datetime as _datetime
            _nan = float('nan')

            def _to_time(epoch_s):
                # timezone.utc is available since 3.2 (unlike datetime.UTC,
                # which is 3.11+); keeps this 3.6+ compatible and warning-free.
                return _datetime.datetime.fromtimestamp(
                    int(epoch_s), _datetime.timezone.utc)

        # Name the interpreter explicitly rather than relying on the script's
        # shebang. The shebang is only correct once a unit has pulled the fix for
        # it, so invoking by bare path makes this reading depend on per-unit
        # deployment state: on any unit still carrying `#!/usr/bin/env python`
        # (= Python 2.7 on Buster) the script dies on `from pathlib import Path`
        # and Last trigger / GPS Satellites / Threshold all read `nan`.
        #
        # The legacy array.py did it this way and hamma's trigger columns were
        # populated throughout; pamma and aumma, which have always used this
        # script, have read `nan` for as long as they have been on it. Being
        # explicit here fixes all three now and keeps working whatever state a
        # unit's checkout is in.
        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/ltgenv/bin/python',
                     '/home/pi/dev/mjolnir-hamma/scripts/latest_trigger.py']

        try:
            # TimeoutExpired is an Exception, so the handler below catches it and
            # the reading degrades to nan -- the same as any other failure here.
            out = subprocess.run(cmd, stdout=subprocess.PIPE,
                                 universal_newlines=True,
                                 timeout=SSH_TRIGGER_TIMEOUT_S)
            if out.returncode:
                raise Exception
            ret_val = ast.literal_eval(out.stdout)
            ret_val['time'] = _to_time(ret_val['time'])
        except Exception:
            ret_val = dict()
            ret_val['threshold'] = _nan
            ret_val['num_sat'] = _nan
            ret_val['time'] = _nan

        return ret_val

    @staticmethod
    def updown(port, bring_up, quiet=False):
        # Here port is fully qualified
        #
        # Returns success (bool): True only once sensors.py has actually run
        # and exited 0. Used by the HAM-189 write-hook (updown_array) to
        # decide whether the front_end change is safe to record in the
        # fleet-state snapshot.
        sensor_num = port - 10000
        action = "up" if bring_up else "down"

        # First, make sure the SSH tunnel for this Pi is reachable...
        is_pi_up = MjolnirArray.status(port)

        if not is_pi_up:
            if not quiet:
                print(f"[SKIP] mj{sensor_num:02} (port {port}): tunnel down, "
                      f"sensor not brought {action}.")
            return False

        flag = "--on" if bring_up else "--off"

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/mjolnir-hamma/scripts/sensors.py', flag]

        if not quiet:
            print(f"--- mj{sensor_num:02}: bringing sensor {action} ---")

        try:
            out = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: sensors.py did not complete "
                      f"in 120s.")
            return False
        except Exception as e:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: error running sensors.py: {e}")
            return False

        if not quiet:
            # Surface remote stdout/stderr so the operator sees what
            # happened. sensors.py prints [OK]/[FAIL] lines describing each
            # step.
            stdout = out.stdout.decode(errors="replace") if out.stdout else ""
            stderr = out.stderr.decode(errors="replace") if out.stderr else ""
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n")
            if stderr:
                print(stderr, end="" if stderr.endswith("\n") else "\n")
            if out.returncode != 0:
                print(f"[FAIL] mj{sensor_num:02}: sensors.py exited "
                      f"with code {out.returncode}.")

        return out.returncode == 0

    @staticmethod
    def _run_ags_command(port, ags_args, action_label, quiet=False, timeout=30):
        # Run ags.py on the Pi over its autossh tunnel with the given args.
        # port is fully qualified (10000 + unit number).
        #
        # Returns (success, stdout_text). success is True only once ags.py
        # has actually run and exited 0 -- the HAM-189 write-hook uses it to
        # decide whether a threshold/gain change is safe to log; trigger()
        # ignores it.
        sensor_num = port - 10000

        if not MjolnirArray.status(port):
            if not quiet:
                print(f"[SKIP] mj{sensor_num:02} (port {port}): tunnel down, "
                      f"{action_label} not sent.")
            return False, ""

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/mjolnir-hamma/scripts/ags.py'] + list(ags_args)

        if not quiet:
            print(f"--- mj{sensor_num:02}: {action_label} ---")

        try:
            out = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: ags.py did not complete "
                      f"in {timeout}s.")
            return False, ""
        except Exception as e:
            if not quiet:
                print(f"[FAIL] mj{sensor_num:02}: error running ags.py: {e}")
            return False, ""

        stdout = out.stdout.decode(errors="replace") if out.stdout else ""
        stderr = out.stderr.decode(errors="replace") if out.stderr else ""

        if not quiet:
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n")
            if stderr:
                print(stderr, end="" if stderr.endswith("\n") else "\n")
            if out.returncode != 0:
                print(f"[FAIL] mj{sensor_num:02}: ags.py exited "
                      f"with code {out.returncode}.")

        return out.returncode == 0, stdout

    @staticmethod
    def trigger(port, command="das_manual_trigger", quiet=False):
        # Send an AGS command (default: a manual trigger) to a sensor.
        MjolnirArray._run_ags_command(
            port, [command], f"sending AGS '{command}'", quiet=quiet)

    @staticmethod
    def set_threshold(port, channel, millivolts, persist=False, quiet=False):
        # Returns (success, stdout_text) -- see _run_ags_command().
        ags_args = ["set-threshold", str(channel), str(millivolts)]
        if persist:
            ags_args.append("--persist")
        return MjolnirArray._run_ags_command(
            port, ags_args,
            f"set threshold ch{channel} = {millivolts} mV"
            + (" (persist)" if persist else ""),
            quiet=quiet)

    @staticmethod
    def set_gain(port, channel, level, persist=False, quiet=False):
        # Returns (success, stdout_text) -- see _run_ags_command().
        ags_args = ["set-gain", str(channel), str(level)]
        if persist:
            ags_args.append("--persist")
        return MjolnirArray._run_ags_command(
            port, ags_args,
            f"set gain {channel} = {level}"
            + (" (persist)" if persist else ""),
            quiet=quiet)

    @staticmethod
    def status_fcm(port):
        # Return a boolean if the FCM sensor is up (True) or down (False)
        # port is fully qualified

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/ltgenv/bin/brokkr', 'status']

        try:
            # On timeout the except below sets ping_code = 1, i.e. "sensor down".
            # Failing toward down is correct: a hung probe must never read as up.
            out = subprocess.run(cmd, stdout=subprocess.PIPE,
                                 timeout=SSH_STATUS_TIMEOUT_S)
            retval = out.stdout.decode()
            retval = retval.split('\n')

            ping_code = next((x for x in retval if 'Ping Retcode' in x))
            ping_code = int(ping_code.split(':')[-1])
        except Exception as e:
            # If anything goes wrong, we'll assume we don't see the sensor
            ping_code = 1

        return not ping_code

    def updown_array(self, bring_up, ports=None, log_repo=None, no_log=False,
                     reason=None):
        # Bring one or more sensors up or down.
        # bring up is boolean
        # ports is mod 10000.
        #    Can be string, but if it is, in needs to be in a list
        #    Even a one element list
        #
        # log_repo/no_log/reason are the HAM-189 write-hook: on a successful
        # change, front_end is recorded in the fleet-state snapshot unless
        # no_log is set. Every port's control op runs to completion first;
        # the snapshot is then committed ONCE for the whole sweep -- see
        # _log_field_changes_batch().

        if ports is None:
            ports = [10000 + i for i in self.sensors]
        else:
            # Because sensor nums start at 1, we need to subtract one when indexing
            # local_sensor_nums = [self.sensors[int(p) - 1] for p in ports]
            ports = [10000 + int(p) for p in ports]  # Make this an integer

        pending = []
        for p in ports:
            success = MjolnirArray.updown(p, bring_up)
            if success and not no_log:
                entry = _resolve_front_end_entry(p, bring_up)
                if entry is not None:
                    pending.append(entry)

        if pending:
            _log_field_changes_batch(log_repo, pending, reason=reason)

    def _resolve_ports(self, ports):
        # Resolve sensor numbers (or None = all of this array's sensors) into
        # fully-qualified tunnel ports (10000 + number).
        if ports is None:
            return [10000 + i for i in self.sensors]
        return [10000 + int(p) for p in ports]

    def trigger_array(self, ports=None, command="das_manual_trigger"):
        # Send an AGS command (default: a manual trigger) to one or more sensors.
        # ports is mod 10000 (list). If None, use all of this array's sensors.
        for p in self._resolve_ports(ports):
            MjolnirArray.trigger(p, command=command)

    def set_threshold_array(self, ports=None, channel=None, millivolts=None,
                            persist=False, log_repo=None, no_log=False,
                            reason=None):
        # log_repo/no_log/reason: HAM-189 write-hook, see updown_array().
        # Only a PERSISTED change that the AGS reply confirms is logged --
        # fleet_probe reads the sensor's startup file, not the live
        # register, so a live-only or firmware-rejected change is invisible
        # to (or wrong in) it, and logging it would just be reverted
        # (falsely) by the next probe run. See _resolve_threshold_entry().
        pending = []
        for p in self._resolve_ports(ports):
            success, stdout = MjolnirArray.set_threshold(
                p, channel, millivolts, persist=persist)
            if success and persist and not no_log:
                entry = _resolve_threshold_entry(p, channel, millivolts,
                                                 stdout)
                if entry is not None:
                    pending.append(entry)

        if pending:
            _log_field_changes_batch(log_repo, pending, reason=reason)

    def set_gain_array(self, ports=None, channel=None, level=None,
                       persist=False, log_repo=None, no_log=False,
                       reason=None):
        # log_repo/no_log/reason: HAM-189 write-hook, see updown_array() and
        # set_threshold_array() (same persist-only, AGS-reply-gated rule
        # applies here).
        pending = []
        for p in self._resolve_ports(ports):
            success, stdout = MjolnirArray.set_gain(
                p, channel, level, persist=persist)
            if success and persist and not no_log:
                entry = _resolve_gain_entry(p, channel, level, stdout)
                if entry is not None:
                    pending.append(entry)

        if pending:
            _log_field_changes_batch(log_repo, pending, reason=reason)

    def status_array(self, ports=None, quiet=False):
        # Get status report for a number of sensors in an array
        # ports is a list (even a one element list)

        # todo: break out pi up to a standalone function

        if ports is None:
            local_sensor_nums = self.sensors
            ports = [10000 + i for i in self.sensors]
        else:
            # Because sensor nums start at 1, we need to subtract one when indexing
            # local_sensor_nums = [self.sensors[int(p) - 1] for p in ports]
            local_sensor_nums = [int(p) for p in ports]
            ports = [10000 + int(p) for p in ports]  # Make this an integer

        is_up = list()
        is_sensor_up = list()
        is_brokkr_up = list()
        is_sindri_up = list()
        trig_attr = list()

        for p in ports:
            this_up = MjolnirArray.status(p)

            is_up.append(this_up)

            if this_up:
                this_brokkr_up, this_sindri_up = MjolnirArray.status_services(p)
            else:
                this_brokkr_up = False
                this_sindri_up = False

            # This will populate a dummy set of attrs if not up....
            this_trig_attr = MjolnirArray.status_latest_trigger(p)

            is_brokkr_up.append(this_brokkr_up)
            is_sindri_up.append(this_sindri_up)
            trig_attr.append(this_trig_attr)

            if this_up:
                this_sensor_up = MjolnirArray.status_fcm(p)
            else:
                this_sensor_up = False

            is_sensor_up.append(this_sensor_up)

        if not quiet:
            def _convert_updown(val):
                return 'Up' if val else 'Down'

            zipped = zip(is_up, is_sensor_up, local_sensor_nums, is_brokkr_up, is_sindri_up, trig_attr)

            for up, sensor_up, s, brokkr_up, sindri_up, t_attr in zipped:
                # status_up = 'Up' if up else 'Down'
                # sensor_up = 'Up' if sensor_up else 'Down'
                print(f"{self.sensor_name}{s:02} is {_convert_updown(up):>6}; "
                      f"FCM is {_convert_updown(sensor_up):>6}; "
                      f"Brokkr {_convert_updown(brokkr_up)!s:>6}; "
                      f"Sindri {_convert_updown(sindri_up)!s:>6}; "
                      f"Trig time {t_attr['time']!s:>20}; "
                      f"Num Sat {t_attr['num_sat']!s:>3}; "
                      f"Threshold {t_attr['threshold']!s:>6}; "
                      )
                # print(t_attr)

        return is_up, is_sensor_up, is_brokkr_up, is_sindri_up, trig_attr

    def collect_data(self):
        # TODO: only get some - subset sensor_nums
        # Provide an easy to collect a bunch of data about the array
        import pandas as pd

        hamma_ports = self.sensors

        mjol_up, sensor_up, brokkr_up, sindri_up, trig_attr = self.status_array(ports=hamma_ports, quiet=True)

        # n_sensor = len(sensor_nums)
        # print(trig_attr[0])

        thresh = [t['threshold'] for t in trig_attr]
        sat = [t['num_sat'] for t in trig_attr]
        trig_time = [t['time'] for t in trig_attr]

        v = {'Mjolnir Up': mjol_up,
             'Brokkr Up': brokkr_up,
             'Sindri Up': sindri_up,
             'Sensor Up': sensor_up,
             'Last trigger': trig_time,
             'Num GPS': sat,
             'Threshold': thresh,
             }

        df = pd.DataFrame(v)
        df.index = [self.sensor_name + f"{_i:02}" for _i in hamma_ports]

        # Apply some formatting. Note that df.style requires Jinja2, which is optional dep
        df['Threshold'] = [f"{val:.2f}" for val in df['Threshold']]

        return df


def _warn_if_persist_silently_omitted(log_repo, parsed_args, what):
    """HAM-189 cheap improvement: --persist is opt-in and its CLI default
    (False) silently produces a change the fleet-state snapshot can never
    see -- functionally identical to --no-log, but without --no-log's
    self-documenting opt-in flag. An operator who passes --log-repo (or has
    $FLEET_LOG_REPO set) and simply forgets --persist gets no notice, and
    the dashboard keeps showing the old value with nothing pointing at why.
    Mirrors the existing "[LOG] no --log-repo configured" notice; silent
    only when the operator explicitly said --no-log, since that is already
    self-documenting.
    """
    if log_repo and not parsed_args.persist and not parsed_args.no_log:
        print(f"[LOG] --persist not set; this {what} change will not be "
              f"recorded in the fleet-state snapshot (same effect as "
              f"--no-log, without saying so).", file=sys.stderr)


def main(argv=None):
    arg_parser = argparse.ArgumentParser(description="Bring the array up or down")

    arg_parser.add_argument(
        "-a",
        dest="array",
        help="Which array (e.g., hamma, pamma)",
        default=None

    )

    arg_parser.add_argument("-p",
                            dest="ports",
                            action="append",  # if argparse 3.8, then can use extend
                            help="Which ports to bring up/down, mod 10000"
    )
    # NOTE: PASS multiple -p for multiple ports until argparse -> 3.8

    # grp = arg_parser.add_mutually_exclusive_group(required=True)

    arg_parser.add_argument("--status",
                            help="Get a status report",
                            dest="do_status",
                            action='store_true',
                            default=False,
                            )

    arg_parser.add_argument("--trigger",
                            help="Send a manual trigger (ags das_manual_trigger) "
                                 "to the sensor(s)",
                            dest="do_trigger",
                            action='store_true',
                            default=False,
                            )

    arg_parser.add_argument("--set-threshold", nargs=2,
                            metavar=("CHANNEL", "MV"), default=None,
                            help="Set threshold: channel (1|2) and mV")
    arg_parser.add_argument("--set-gain", nargs=2,
                            metavar=("CHANNEL", "LEVEL"), default=None,
                            help="Set gain: channel (fast-e|slow-e) and level (0-3)")
    arg_parser.add_argument("--persist", action="store_true", default=False,
                            help="Also persist to /ags/scripts/startup")

    arg_parser.add_argument(
        "--log-repo", dest="log_repo", default=None,
        help="Path to a sensor-log clone. On a successful --up/--down/"
             "--set-threshold/--set-gain (the last two only when combined "
             "with --persist), update <repo>/state/fleet-state.csv (the "
             "HAM-189 write-hook) so the change leaves no diff for the next "
             "fleet probe. Also settable via $FLEET_LOG_REPO. Omitting both "
             "is the same as --no-log.")
    arg_parser.add_argument(
        "--no-log", dest="no_log", action="store_true", default=False,
        help="Make the change but do NOT update the fleet-state snapshot, "
             "so the next fleet probe surfaces it as an unlogged change. "
             "For a deliberate bench/test toggle that should not read as a "
             "real fleet configuration change.")
    arg_parser.add_argument(
        "--reason", default=None,
        help="Free-text reason for a logged change. Goes in the fleet-state "
             "git commit message, NOT the snapshot itself -- a free-text "
             "column would differ on every row and defeat the "
             "write-if-changed signal the snapshot exists to preserve. "
             "Ignored with --no-log.")

    grp = arg_parser.add_mutually_exclusive_group()
    grp.add_argument("--up",
                     dest='bring_up',
                     action="store_true",
                     default=False,
                     help="Bring array up",
                     )

    grp.add_argument("--down",
                     dest='bring_down',
                     action="store_true",
                     default=False,
                     help="Bring array down",
                     )

    parsed_args = arg_parser.parse_args(argv)

    # HAM-189 write-hook: --log-repo wins over $FLEET_LOG_REPO, mirroring
    # fleet_probe.py's own --ags-path / $FLEET_PROBE_AGS_PATH precedence.
    log_repo = parsed_args.log_repo or os.environ.get("FLEET_LOG_REPO")

    if parsed_args.ports is None:
        if parsed_args.array == 'hamma':
            mj_array = MjolnirArray(sensors=HAMMA_SENSORS)
        elif parsed_args.array == 'pamma':
            mj_array = MjolnirArray(sensors=PAMMA_SENSORS)
        elif parsed_args.array == 'aumma':
            mj_array = MjolnirArray(sensors=AUMMA_SENSORS)
        elif parsed_args.array is None:
            print('You must pass either the sensors/ports or the array')
            return
        else:
            print('Invalid array name.')
            return
    else:
        mj_array = MjolnirArray(sensors=parsed_args.ports)

    if parsed_args.do_status:
        _ = mj_array.status_array(ports=parsed_args.ports)
    elif parsed_args.do_trigger:
        mj_array.trigger_array(ports=parsed_args.ports)
    elif parsed_args.set_threshold is not None:
        channel, millivolts = parsed_args.set_threshold
        try:
            _validate_threshold_cli(channel, millivolts)
        except ValueError as e:
            print(f"[ERROR] {e}")
            return
        _warn_if_persist_silently_omitted(log_repo, parsed_args, "threshold")
        mj_array.set_threshold_array(
            ports=parsed_args.ports, channel=channel, millivolts=millivolts,
            persist=parsed_args.persist, log_repo=log_repo,
            no_log=parsed_args.no_log, reason=parsed_args.reason)
    elif parsed_args.set_gain is not None:
        channel, level = parsed_args.set_gain
        try:
            _validate_gain_cli(channel, level)
        except ValueError as e:
            print(f"[ERROR] {e}")
            return
        _warn_if_persist_silently_omitted(log_repo, parsed_args, "gain")
        mj_array.set_gain_array(
            ports=parsed_args.ports, channel=channel, level=level,
            persist=parsed_args.persist, log_repo=log_repo,
            no_log=parsed_args.no_log, reason=parsed_args.reason)
    elif parsed_args.bring_up | parsed_args.bring_down:
        # Is it up or down?
        # bring_up = parsed_args.bring_up

        mj_array.updown_array(parsed_args.bring_up, ports=parsed_args.ports,
                              log_repo=log_repo, no_log=parsed_args.no_log,
                              reason=parsed_args.reason)
    else:
        pass


if __name__ == '__main__':
    main()
