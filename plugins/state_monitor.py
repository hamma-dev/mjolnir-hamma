"""
Plugin to monitor state variables from the charge controller.
"""

from math import nan
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import time

# Third party imports
from notifiers import Notifier

# Local imports
import brokkr.pipeline.base
import brokkr.pipeline.decode
import brokkr.utils.output

# Lock file shared with the spawned `flock -n` scrub, so the monitor can probe
# whether a prior scrub is still running (and detect a hung one).
SCRUB_LOCK_FILE = "/tmp/hamma_scrub.lock"
# Heartbeat/status file the scrub writes as it advances (must match
# hamma_scrub.DEFAULT_STATUS_FILE); read to tell a working scrub from a hung one.
DEFAULT_SCRUB_STATUS_FILE = os.path.expanduser(
    "~/brokkr/hamma/log/hamma_scrub_status.json")
# Durable log capturing the scrub's own stdout/stderr (mj05 ran blind because
# this was DEVNULL'd). Rotated at SCRUB_LOG_MAX_BYTES so it can't fill the disk.
DEFAULT_SCRUB_LOG = os.path.expanduser("~/brokkr/hamma/log/hamma_scrub.log")
SCRUB_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate the scrub log past this size


def sensor_prefix():
    """Return the '<name><NN> (<site>): ' prefix for this unit's messages."""
    from brokkr.config.unit import UNIT_CONFIG
    from brokkr.config.metadata import METADATA

    sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"
    site = UNIT_CONFIG['site_description']
    return f"{sensor_name} ({site}): " if site else f"{sensor_name}: "


class StateMonitor(brokkr.pipeline.base.OutputStep):
    """Handle notifications for changes in state variables."""

    def __init__(
        self,
        method=None,
        power_delim=1,
        low_space=100,
        ping_max=3,
        channel=None,
        key_file=None,
        low_pi_space=5,
        enable_drive_checks=True,
        scrub_command="",
        scrub_hang_timeout_s=900,
        scrub_status_file=DEFAULT_SCRUB_STATUS_FILE,
        scrub_auto_recover=True,
        scrub_log=DEFAULT_SCRUB_LOG,
        **output_step_kwargs,
        ):
        """
        Handle notifications for changes in state variables.

        Parameters
        ----------
        method : str
            The method of how we're going to send notifications. If `None`, then
            we'll only log them.
        power_delim : numeric, optional
            The delimiter between normal power and low power.
            If power falls below this value, it is considered low
            and a notification will be generated.
        low_space : numeric, optional
            If the number of gigabytes remaining falls below this threshold, generate
            a notification.
        ping_max : int, optional
            The maximum number of consecutive ping errors before we send an error message
            via `method`. Any ping errors are still logged locally.
        channel : str, optional
            The chat channel in which to post notifications.
        key_file : str or pathlib.Path, optional
            The path to the file that contains the secret/webhook key for the given `method`.
        low_pi_space: numeric, optional
            Specifies the critical value, in gigabytes-ish, for hard drive space remaining
            on the backend Pi.
        enable_drive_checks : bool, optional
            If True (default), check for archive drives and sensor drive space.
            Set to False for units without HAMMA sensor hardware connected.
        scrub_command : str, optional
            Shell command to run hamma_scrub.py when drive space is low.
            If empty (default), no scrub is spawned. Protected by flock.
        scrub_hang_timeout_s : numeric, optional
            Seconds the scrub lock may be held with no heartbeat progress
            before it is judged hung (default 900). Must exceed the scrub's own
            longest single blocking phase (the AGS scan, SCAN_TIMEOUT=600s).
        scrub_status_file : str, optional
            Path to the scrub's heartbeat/status JSON (progress signal).
        scrub_auto_recover : bool, optional
            If True (default), kill a genuinely-hung scrub to free the lock
            (self-healing); else only alert.
        scrub_log : str, optional
            Durable file capturing the scrub's stdout/stderr (rotated); empty
            string disables (falls back to DEVNULL).
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        Returns
        -------
        None.
        """
        # Pass arguments to superclass init
        super().__init__(**output_step_kwargs)

        # Setup simpleeval parser and class initial state
        self._previous_data = None
        self.power_delim = power_delim
        self.low_space = low_space
        self.ping_max = ping_max
        self.bad_ping = 0  # Track the number of bad pings
        self.low_pi_space = low_pi_space*1000000000
        self.enable_drive_checks = enable_drive_checks
        self.scrub_command = scrub_command
        # Hung-scrub detection (progress-gated): a scrub is "hung" only if the
        # lock is held AND its heartbeat has been stale this long -- so a long
        # legit recovery (fresh heartbeat) is not mistaken for a hang.
        self.scrub_hang_timeout_s = scrub_hang_timeout_s
        self.scrub_status_file = scrub_status_file
        self.scrub_auto_recover = scrub_auto_recover
        self.scrub_log = scrub_log
        self._scrub_first_held = None
        self._stuck_scrub_alerted = False

        self.notifier = Notifier(
            method=method, key_file=key_file, channel=channel, logger=self.logger)

    def execute(self, input_data=None):
        """
        Execute an action upon detection an arbitrary condition in the data.

        Parameters
        ----------
        input_data : Mapping[str, DataValue], optional
            Per iteration input data passed to this function from previous
            PipelineSteps. Used to extract the data values to report.
            The default is None.

        Returns
        -------
        input_data : same as `input_data`
            Input data passed through unchanged, for further steps to consume.
        """

        # Handle first iteration
        if self._previous_data is None:
            self._previous_data = input_data

        # Go through several state variables.
        # If something is hinky, log it and send a message
        self.run_checks(input_data)

        # TODO detect this more reliably by checking array_fault and load_fault bitfields are non-zero,

        # Update state for next pass through the pipeline
        self._previous_data = input_data

        # Pass through the input for consumption by any further steps
        return input_data

    def now_then(self, input_data, key):
        """
        Simple method to extract the current (now) value and previous (then)
        value for the data structure passed around.

        Useful for getting any value from `input_data`, since this we turn
        string 'NA's to numeric NaNs.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.
        key : str
            The key of `input_data` you want the value of.

        Returns
        -------
        now_then_values : tuple
            Two element tuple of the (now value, then value)

        """

        now_val = input_data[key].value
        then_val = self._previous_data[key].value

        if now_val == 'NA':
            now_val = nan

        if then_val == 'NA':
            then_val = nan

        return now_val, then_val

    def log_error(self, input_data, exception_inst):
        """
        Log an error.

        Use this when catching Exceptions, especially in the methods of the class.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        exception_inst : Exception
            The exception you wish to log.

        """

        self.logger.error(
            "%s evaluating in %s on step %s: %s",
            type(exception_inst).__name__, type(self), self.name, exception_inst)
        self.logger.info("Error details:", exc_info=True)
        for pretty_name, data in [("Current", input_data),
                                  ("Previous", self._previous_data)]:
            self.logger.info(
                "%s data: %r", pretty_name, data)

    def run_checks(self, input_data):
        """
        Run the monitoring checks and log/send messages for the results.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        """
        checks = [
                self.check_pi_space,
                self.check_ping,
                self.check_power,
                self.check_battery_voltage,
        ]
        if self.enable_drive_checks:
            checks.insert(0, self.check_drive)
            checks.append(self.check_sensor_drive)
            checks.append(self.check_scrub_health)

        for check_fn in checks:
            try:
                # noinspection PyArgumentList
                msg = check_fn(input_data)
                if msg:
                    self.logger.info(msg)
                    self.send_message(msg)
            except Exception as e:
                self.log_error(input_data, e)

    def check_pi_space(self, input_data):
        """
        Check the remaining space on backend Pi.

        This will check to see how much space is remaining on a backend Pi.
        If it falls below the value given by the class attribute `low_pi_space`,
        send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """

        total, used, free = shutil.disk_usage("/")

        if free < self.low_pi_space:
            return f"Free space is low! Current {free/(2**30):.2f} GB; critical value:{self.low_pi_space/(2**30):.2f} GB."
        else:
            return None

    def check_drive(self, input_data):
        """Check to see if the archive drive is available"""
        from brokkr.config.main import CONFIG
        import brokkr.utils.output
        drive_settings = CONFIG['steps']['science_binary_output']['drive_kwargs']

        avail_drives = brokkr.utils.output.find_drives(
                 drive_settings['drive_glob'],
                 '/dev/disk/by-label',
        )

        if not avail_drives:
            return "No drives available."
        else:
            return None

    def check_power(self, input_data):
        """
        Check the power of the sensor.

        This will check to see of the power load of a sensor drops below
        the value given by the class attribute `power_delim`.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        load_now, load_pre = self.now_then(input_data, 'adc_vl_f')
        curr_now, curr_pre = self.now_then(input_data, 'adc_il_f')

        power_now, power_pre = load_now * curr_now, load_pre * curr_pre
        # Use >= for `pre` so a previous sample sitting exactly on the
        # threshold still counts as above it. Strict `>` silently misses
        # the edge when pre lands on power_delim.
        if (power_now < self.power_delim) and (power_pre >= self.power_delim):
            return f"Power has dropped from {power_pre:.2f} to {power_now:.2f}."
        return None

    def check_ping(self, input_data):
        """
        Check the ping status of the sensor.

        This will check to see if we can communicate with a sensor.
        If we can't an error is logged. If we can't communicate several
        consecutive times, given by the class attribute `bad_ping`, send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        no_comm_now, no_comm_pre = self.now_then(input_data, 'ping')
        # If the ping !=0, then we can't reach the sensor
        if no_comm_now:
            # If any bad ping, increment the counter.
            self.bad_ping += 1
            if not no_comm_pre:
                # If we pinged fine before, but not now, log it.
                self.logger.info("Sensor unable to be pinged!")
            # Once we reach the critical value, send an alert and log it.
            if self.bad_ping == self.ping_max:
                return f"No communication with sensor (consecutive bad pings: {self.bad_ping})"
        else:  # if we can communicate now, reset the counter
            self.bad_ping = 0
        return None

    def check_sensor_drive(self, input_data):
        """
        Check the remaining space on sensor.

        This will check to see how much space is remaining on a sensor USB drive.
        If it falls below the value given by the class attribute `low_space`,
        send a message and optionally spawn a scrub process.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        space_now, space_pre = self.now_then(input_data, 'bytes_remaining')
        # Use >= for `pre` so a previous sample sitting exactly on the
        # threshold still counts as above it. Strict `>` silently misses
        # the edge when pre lands on low_space (a real case on mj07:
        # 100.0 -> 99.96 with low_space=100 never fired).
        if (space_now < self.low_space) and (space_pre >= self.low_space):
            self._spawn_scrub()
            return f"Remaining GB on drive is {space_now:.1f}"
        return None

    def _scrub_lock_state(self):
        """Return 'held', 'free', or 'unknown' for the scrub flock.

        Uses the same flock(2) advisory lock the spawned ``flock -n`` uses, so
        the two interoperate. Returns 'unknown' (NOT 'free') when the lock file
        can't be opened -- during a disk-full event (the exact incident) the
        lockfile can be uncreatable (ENOSPC), and a stuck-detector must not go
        blind then: the consumers treat 'unknown' as suspicious. The tiny
        benign TOCTOU window is fine -- exclusion is enforced by the spawned
        ``flock -n``; this probe only makes lock state *visible*.
        """
        try:
            fd = os.open(SCRUB_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            self.logger.warning("Scrub lock %s unreadable (%s); state unknown",
                                SCRUB_LOCK_FILE, e)
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "held"  # someone else holds it
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "free"
        finally:
            os.close(fd)

    def _read_scrub_status(self):
        """Read the scrub heartbeat/status JSON, or None if absent/unreadable.

        The running scrub updates this file (`hamma_scrub.write_status`) as it
        advances; the ``timestamp`` field is the heartbeat used to tell a
        working scrub from a hung one.
        """
        try:
            with open(self.scrub_status_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _open_scrub_log(self):
        """Open the durable scrub log for append (rotating if oversized).

        Returns an open file object, or None to fall back to DEVNULL. Rotating
        past SCRUB_LOG_MAX_BYTES keeps the log from *becoming* a disk-fill.
        """
        if not self.scrub_log:
            return None
        try:
            os.makedirs(os.path.dirname(self.scrub_log), exist_ok=True)
            if (os.path.exists(self.scrub_log)
                    and os.path.getsize(self.scrub_log) > SCRUB_LOG_MAX_BYTES):
                os.replace(self.scrub_log, self.scrub_log + ".1")
            return open(self.scrub_log, "ab")
        except OSError as e:
            self.logger.warning("Could not open scrub log %s (%s); using DEVNULL",
                                self.scrub_log, e)
            return None

    def _spawn_scrub(self):
        """Spawn a detached scrub, but only if no prior scrub is running.

        The old code wrapped the command in ``flock -n`` and logged "Spawned
        scrub" whether or not the lock was acquired -- so a hung prior scrub
        made the spawn a silent no-op *and the log lied* (the mj05 failure).
        Here we probe the lock first and log the truth, and capture the scrub's
        output to a durable log (mj05 ran blind on DEVNULL'd output).
        """
        if not self.scrub_command or not self.scrub_command.strip():
            return
        state = self._scrub_lock_state()
        if state == "held":
            self.logger.warning(
                "Scrub NOT spawned: a prior scrub still holds %s "
                "(possibly hung -- see check_scrub_health)", SCRUB_LOCK_FILE)
            return
        if state == "unknown":
            self.logger.warning(
                "Scrub NOT spawned: lock %s unreadable (disk full?)",
                SCRUB_LOCK_FILE)
            return
        log_fh = self._open_scrub_log()
        if log_fh is not None:
            out, err = log_fh, subprocess.STDOUT  # capture stderr into the log
        else:
            out, err = subprocess.DEVNULL, subprocess.DEVNULL
        try:
            cmd = ["flock", "-n", SCRUB_LOCK_FILE] + shlex.split(
                self.scrub_command)
            subprocess.Popen(
                cmd,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            self.logger.info("Scrub spawned (lock was free): %s",
                             " ".join(cmd))
        except (OSError, ValueError) as e:
            self.logger.error("Failed to spawn scrub: %s", e)
        finally:
            if log_fh is not None:
                log_fh.close()  # Popen dup'd the fd; the parent can close

    def _pid_is_scrub(self, pid):
        """True if `pid` is (still) a running hamma_scrub process.

        Guards against killing a recycled PID: we only ever kill a process
        whose cmdline still names the scrub script.
        """
        try:
            with open("/proc/{}/cmdline".format(pid), "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(
                    "utf-8", "replace")
        except OSError:
            return False
        return "hamma_scrub" in cmdline

    def _recover_stuck_scrub(self, status):
        """Kill a genuinely-hung scrub to free the lock. Returns True if killed.

        Safe *because* the caller only invokes this once the heartbeat proves
        no progress (not merely that the lock is old). Kills the whole process
        group (scrub + its `flock` parent + any ssh children), after verifying
        the PID still looks like a scrub (guards PID reuse).
        """
        pid = status.get("pid") if status else None
        if not isinstance(pid, int):
            self.logger.error(
                "Stuck scrub detected but no usable PID in status; cannot "
                "auto-recover -- manual kill needed")
            return False
        if not self._pid_is_scrub(pid):
            self.logger.warning(
                "Stuck-scrub PID %s no longer looks like a scrub (exited or "
                "reused); not killing", pid)
            return False
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError as e:
            self.logger.warning("Could not kill hung scrub PID %s: %s", pid, e)
            return False
        self.logger.error(
            "Killed hung scrub PID %s (process group) to free the lock", pid)
        return True

    def check_scrub_health(self, input_data):
        """Detect and (optionally) recover a *hung* scrub -- progress-gated.

        "Hung" is defined as: the lock is held AND the scrub has made no
        progress (stale heartbeat) for `scrub_hang_timeout_s`. This
        distinguishes a hang from a legitimately long recovery (which keeps the
        heartbeat fresh), avoiding cry-wolf. On a genuine hang, if
        `scrub_auto_recover` is set, kill the stale scrub to free the lock
        (self-healing -- a timer alone can't do this); alert once either way.
        Re-arms when the lock frees. 'unknown' lock state (e.g. disk full) is
        treated as suspicious, not free.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute` (unused; kept for check uniformity).

        Returns
        -------
        str | None
            Alert message when a hang is first detected, else None.
        """
        if not self.scrub_command or not self.scrub_command.strip():
            return None
        state = self._scrub_lock_state()
        if state == "free":
            self._scrub_first_held = None
            self._stuck_scrub_alerted = False
            return None

        # held or unknown -> a scrub may be running/hung; measure progress
        now = time.monotonic()
        if self._scrub_first_held is None:
            self._scrub_first_held = now
        status = self._read_scrub_status()
        hb = status.get("timestamp") if status else None
        if isinstance(hb, (int, float)):
            progress_age = time.time() - hb          # time since last heartbeat
        else:
            progress_age = now - self._scrub_first_held  # no heartbeat fallback

        if progress_age <= self.scrub_hang_timeout_s:
            return None  # fresh heartbeat (working) or not held long enough yet

        # Genuine hang: lock held with no progress for scrub_hang_timeout_s
        recovered = False
        if self.scrub_auto_recover:
            recovered = self._recover_stuck_scrub(status)
        if self._stuck_scrub_alerted:
            return None
        self._stuck_scrub_alerted = True
        action = ("killed the stale scrub to free the lock" if recovered
                  else "manual intervention needed")
        return ("Auto-scrub hung: lock held with no progress for {:.0f}s -- {}"
                .format(progress_age, action))

    def check_battery_voltage(self, input_data):
        """
        Check the battery voltage.

        This will check to see if the battery voltage falls below a critical value.
        Right now, this is hardwired to be 0.5 V above the low voltage disconnect
        defined by the charge controller. If it falls below this value,
        send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        low_voltage_val, _ = self.now_then(input_data, 'v_lvd')
        CRITICAL_VOLTAGE = low_voltage_val + 0.5

        batt_now, batt_pre = self.now_then(input_data, 'adc_vb_f')
        # Use >= for `pre` so a previous sample sitting exactly on the
        # threshold still counts as above it. Strict `>` silently misses
        # the edge when pre lands on CRITICAL_VOLTAGE.
        if (batt_now <= CRITICAL_VOLTAGE) and (batt_pre >= CRITICAL_VOLTAGE):
            return f"Battery voltage critically low ({batt_now:.3f} V)"
        return None

    def send_message(self, msg):
        """Prefix with the sensor identity and send via the notifier."""
        self.notifier.send(sensor_prefix() + msg)
