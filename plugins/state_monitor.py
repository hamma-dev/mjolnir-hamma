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
# On tmpfs so it stays writable when the SD root fills (the incident condition).
DEFAULT_SCRUB_STATUS_FILE = "/dev/shm/hamma_scrub_status.json"
# Durable log capturing the scrub's own stdout/stderr (mj05 ran blind because
# this was DEVNULL'd). Rotated at SCRUB_LOG_MAX_BYTES so it can't fill the disk.
DEFAULT_SCRUB_LOG = os.path.expanduser("~/brokkr/hamma/log/hamma_scrub.log")
SCRUB_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate the scrub log past this size
# Mountpoint base for the mj-side DATA drives. Deliberately the same template
# brokkr's own get_output_drive uses (utils/output.py), rather than a hardcoded
# /media/pi, so this cannot drift from where brokkr actually writes.
# check_drive discovers drives by *label* under /dev/disk/by-label, but free
# space is a property of the mounted filesystem, so this check must work from
# mountpoints instead.
RECOVERY_MOUNT_BASE = "/media/{current_user}"


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
        purge_space=200,
        alert_space=75,
        scrub_cooldown_s=300,
        hs_stale_cycles=15,
        ping_max=3,
        channel=None,
        key_file=None,
        low_pi_space=5,
        enable_drive_checks=True,
        scrub_command="",
        scrub_hang_timeout_s=900,
        scrub_status_file=DEFAULT_SCRUB_STATUS_FILE,
        scrub_auto_recover=False,
        scrub_log=DEFAULT_SCRUB_LOG,
        recovery_low_gb=25,
        low_space=None,   # DEPRECATED: replaced by purge_space/alert_space
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
        purge_space : numeric, optional
            GB free on the AGS drive below which to run the scrub proactively
            (level-triggered, every scrub_cooldown_s). No alert -- this is the
            "drain harder" band. Default 200.
        alert_space : numeric, optional
            GB free below which to alert: free fell this far *despite* the
            purging, so purge is losing. Default 75 (< purge_space).
        scrub_cooldown_s : numeric, optional
            Minimum seconds between level-triggered scrub launches while free is
            below purge_space (retry cadence). Default 300 (5 min) -- more responsive
            than the 15-min §3.3 timer, which is the coarse backstop.
        hs_stale_cycles : int, optional
            Alert once if the sensor's `bytes_remaining` reads NA this many
            consecutive monitor cycles (default 15, ~15 min). NA means the AGS is
            dark, so every space-based decision is blind and `/ags/data` can fill
            unseen -- this watchdog is the only thing that speaks up in that state.
            Reset (and re-armed) by any numeric reading.
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
            If True, kill a genuinely-hung scrub to free the lock (self-healing);
            else only alert. Default **False** -- this SIGKILLs a process group,
            so it stays off until validated on a bench unit (§3.2 deploy gate).
        scrub_log : str, optional
            Durable file capturing the scrub's stdout/stderr (rotated); empty
            string disables (falls back to DEVNULL).
        recovery_low_gb : numeric, optional
            GB free on the roomiest mj-side DATA drive below which to alert.
            These drives are both brokkr's science-data write target and where
            the scrubber lands recovered AGS triggers -- if they all fill,
            writes stop and recovery has nowhere to go. A single drive at 100%
            is normal rotation, so only the *roomiest* drive is judged.
            Alert-only: a full DATA drive is not fixed by scrubbing the AGS.
            Default 25.
        low_space : numeric, optional
            **Deprecated.** The old single edge-triggered threshold, replaced by
            purge_space/alert_space (§3.4). Accepted only so a stale per-unit
            override (e.g. mj54's `low_space=10`) does not crash pipeline build --
            it is warned about and otherwise ignored.
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
        # Two-threshold drain/escalation on the AGS drive (§3.4). purge_space =
        # proactive-drain band; alert_space = "purge is losing" alarm.
        self.purge_space = purge_space
        self.alert_space = alert_space
        self.scrub_cooldown_s = scrub_cooldown_s
        self._last_scrub_spawn = None   # monotonic; level-trigger retry gate
        self._low_space_alerted = False
        # Telemetry-staleness watchdog: bytes_remaining goes NA when the AGS is
        # dark, so the two-threshold path is blind exactly then -- alert instead
        # of silently missing a fill (the mj05 condition).
        self.hs_stale_cycles = hs_stale_cycles
        self._hs_stale_count = 0
        self._hs_stale_alerted = False
        # Legacy config compatibility: a deployed per-unit override may still set
        # `low_space` (removed in favour of purge_space/alert_space). Accept and
        # ignore it -- brokkr's Executable.__init__ has no **kwargs, so an
        # unconsumed key would crash the whole telemetry pipeline at build time.
        if low_space is not None:
            self.logger.warning(
                "state_monitor: `low_space=%s` is deprecated and ignored; use "
                "purge_space/alert_space", low_space)
        if alert_space >= purge_space:
            self.logger.warning(
                "state_monitor: alert_space (%s) >= purge_space (%s) -- the "
                "'purge is losing' alert will fire on every routine drain; "
                "expected alert_space < purge_space", alert_space, purge_space)
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
        self._scrub_first_held = None      # monotonic; when lock streak began
        self._scrub_last_progress = None   # monotonic; last heartbeat advance
        self._scrub_progress_token = None  # last-seen heartbeat identity
        self._stuck_scrub_alerted = False
        # mj-side recovery-target drives (where brokkr writes science data and
        # where the scrubber lands recovered AGS triggers).
        self.recovery_low_gb = recovery_low_gb
        self._recovery_low_active = False  # latch: alert once per descent

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
            checks.append(self.check_recovery_drives)
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

    def check_recovery_drives(self, input_data):
        """
        Check free space on the mj-side recovery-target DATA drives.

        These are both brokkr's science-data write target and where the
        scrubber lands recovered AGS triggers. If they all fill, writes stop
        and recovery has nowhere to go. A single drive at 100% is normal
        rotation, so only the *roomiest* drive is judged. Local and
        telemetry-independent -- no AGS dependency, so this still reports when
        the AGS is dark. Alert-only: a full DATA drive is not fixed by
        scrubbing the AGS.

        Drives are resolved with `brokkr.utils.output.find_drives`, which drops
        any match that is a directory but not a mountpoint. That filter is
        load-bearing here: when udisks hits a stale `/media/pi/DATA07` dir it
        mounts the real drive at `DATA071`, and `shutil.disk_usage()` on the
        leftover directory returns the *SD root's* free space. Because this
        check takes `max()`, a roomy root would then mask genuinely full DATA
        drives. The trailing `*` on the glob catches the suffixed mountpoint
        that a bare `DATA??` would miss.

        Returns
        -------
        str | None
            Message if the roomiest drive is below `recovery_low_gb`, else None.

        """
        # `from ... import name` rather than `import brokkr.utils.misc`: the
        # latter would bind a local `brokkr`, shadowing the module-global one
        # that brokkr.utils.output is resolved through below.
        from brokkr.config.main import CONFIG
        from brokkr.utils.misc import get_actual_username
        drive_glob = (CONFIG['steps']['science_binary_output']
                      ['drive_kwargs']['drive_glob'])

        # Trailing "*" tolerates udisks-suffixed mountpoints (DATA07 -> DATA071).
        # Both parts matter: the "*" finds the suffixed mount, and find_drives'
        # ismount filter drops the stale directory that caused the suffix.
        # Either alone is wrong -- see test_both_fixes_are_required.
        paths = brokkr.utils.output.find_drives(
            drive_glob + "*", RECOVERY_MOUNT_BASE,
            filename_kwargs={"current_user": get_actual_username()})
        if not paths:
            # Nothing mounted -- leave the "no drives" alert to check_drive.
            return None

        frees = []
        for path in paths:
            try:
                frees.append(shutil.disk_usage(str(path)).free)
            except OSError:
                # Drive unmounted mid-check; skip it rather than aborting.
                continue
        if not frees:
            # Paths resolved but every one errored -- avoid max([]) ValueError.
            return None

        threshold = self.recovery_low_gb * (2 ** 30)
        best_free = max(frees)
        if best_free >= threshold:
            self._recovery_low_active = False   # re-arm for the next descent
            return None
        if self._recovery_low_active:
            return None                          # already alerted this descent
        self._recovery_low_active = True
        below = sum(1 for free in frees if free < threshold)
        return ("Recovery drives low: roomiest {} has {:.1f} GB free "
                "({}/{} below {} GB) -- brokkr writes and AGS recovery both "
                "land here".format(
                    drive_glob, best_free / (2 ** 30), below, len(frees),
                    self.recovery_low_gb))

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
        """Two-threshold drain + escalation on the AGS drive (§3.4).

        LEVEL-triggered, not edge (the old edge fired once at the crossing and
        never retried -- the mj05 failure mode). While free < `purge_space`, run
        the scrub every `scrub_cooldown_s` (proactive drain, more responsive
        than the §3.3 timer). If free falls below `alert_space` *despite* the
        purging -- purge is losing -- alert once (re-arm when free recovers
        above `purge_space`).

        Fair-weather layer: `bytes_remaining` goes NA when the AGS is dark, so a
        non-numeric reading is skipped; the §3.3 timer + §3.2 stuck-detector
        cover the AGS-dark case.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Alert message when free first drops below `alert_space`, else None.
        """
        space_now, _space_pre = self.now_then(input_data, 'bytes_remaining')
        if not isinstance(space_now, (int, float)) or space_now != space_now:
            # NA / non-numeric: can't evaluate space. Persistent NA means the
            # AGS is dark (not sending H&S) -- the drive can fill unseen, so
            # alert once rather than fail silent (the mj05 blind spot).
            self._hs_stale_count += 1
            if (self._hs_stale_count >= self.hs_stale_cycles
                    and not self._hs_stale_alerted):
                self._hs_stale_alerted = True
                return ("AGS telemetry (bytes_remaining) NA for {} cycles -- "
                        "the AGS may be dark/unreachable; /ags/data can fill "
                        "unseen".format(self._hs_stale_count))
            return None
        # Numeric reading: AGS is reporting -> clear the staleness watchdog.
        self._hs_stale_count = 0
        self._hs_stale_alerted = False
        if space_now >= self.purge_space:
            # Healthy: re-arm the alert and the immediate-spawn on next descent.
            self._low_space_alerted = False
            self._last_scrub_spawn = None
            return None
        # Below the purge threshold: drain proactively (level-triggered + cooldown).
        self._maybe_spawn_scrub()
        if space_now >= self.alert_space:
            # Drain band [alert_space, purge_space): draining, no alert. Re-arm
            # the alert here (hysteresis) so a fresh drop below alert_space in a
            # later oscillation pages again -- not just once per full recovery.
            self._low_space_alerted = False
            return None
        # Below the alert threshold: purge is losing -> alert once.
        if not self._low_space_alerted:
            self._low_space_alerted = True
            return ("Sensor drive critically low: {:.1f} GB free (below the "
                    "{:g} GB alert floor); auto-scrub is running -- "
                    "intervention may be needed".format(
                        space_now, self.alert_space))
        return None

    def _maybe_spawn_scrub(self):
        """Spawn a scrub at most once per `scrub_cooldown_s` (level-trigger retry).

        The cooldown stops check_sensor_drive from re-launching every 60 s
        monitor cycle. It is armed only when a scrub *actually launches* -- a
        no-op attempt (lock held by a running scrub, or a spawn error) does NOT
        consume the cooldown, so we re-probe the (cheap) flock every cycle and
        launch the instant the lock frees.
        """
        now = time.monotonic()
        if (self._last_scrub_spawn is not None
                and now - self._last_scrub_spawn < self.scrub_cooldown_s):
            return False
        if self._spawn_scrub():
            self._last_scrub_spawn = now
            return True
        return False

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

        Returns
        -------
        bool
            True iff a scrub was actually launched (so the level-trigger's
            cooldown is armed only on a real launch, not a no-op).
        """
        if not self.scrub_command or not self.scrub_command.strip():
            return False
        state = self._scrub_lock_state()
        if state == "held":
            self.logger.warning(
                "Scrub NOT spawned: a prior scrub still holds %s "
                "(possibly hung -- see check_scrub_health)", SCRUB_LOCK_FILE)
            return False
        if state == "unknown":
            self.logger.warning(
                "Scrub NOT spawned: lock %s unreadable (disk full?)",
                SCRUB_LOCK_FILE)
            return False
        log_fh = self._open_scrub_log()
        if log_fh is not None:
            out, err = log_fh, subprocess.STDOUT  # capture stderr into the log
        else:
            out, err = subprocess.DEVNULL, subprocess.DEVNULL
        launched = False
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
            launched = True
        except (OSError, ValueError) as e:
            self.logger.error("Failed to spawn scrub: %s", e)
        finally:
            if log_fh is not None:
                log_fh.close()  # Popen dup'd the fd; the parent can close
        return launched

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
        return "hamma_scrub.py" in cmdline

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

    @staticmethod
    def _progress_token(status):
        """Identity of a heartbeat -- changes whenever the scrub advances.

        We compare tokens for *change* across cycles; we never interpret the
        scrub's ``timestamp`` as an absolute time, so a skewed/NTP-stepped
        sensor clock can neither disable detection nor cause a false kill.
        """
        if not status:
            return None
        return (status.get("pid"), status.get("timestamp"),
                status.get("phase"), status.get("recovered"),
                status.get("purged"))

    def check_scrub_health(self, input_data):
        """Detect and (optionally) recover a *hung* scrub -- progress-gated.

        "Hung" = the lock is held AND the heartbeat has not *advanced* for
        `scrub_hang_timeout_s`, measured in the monitor's own ``monotonic``
        clock. Advancement (not absolute heartbeat age) distinguishes a hang
        from a long legit recovery (fresh heartbeat) -> no cry-wolf; monotonic
        + change-detection makes it immune to sensor clock skew and to a stale
        heartbeat left by a *previous* run (grace is counted from the moment
        THIS monitor first saw the lock held, not from the file's timestamp).
        On a genuine hang, if `scrub_auto_recover`, kill the stale scrub AND
        re-spawn a fresh one to use the freed lock; alert once either way.
        'unknown' lock state (e.g. disk full) is treated as suspicious.

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
            self._scrub_last_progress = None
            self._scrub_progress_token = None
            self._stuck_scrub_alerted = False
            return None

        # held or unknown -> a scrub may be running/hung; track PROGRESS
        now = time.monotonic()
        token = self._progress_token(self._read_scrub_status())
        if self._scrub_first_held is None:
            # New held-streak: start the clock NOW and record (but do not judge)
            # any pre-existing heartbeat -- it may belong to a previous run.
            self._scrub_first_held = now
            self._scrub_last_progress = now
            self._scrub_progress_token = token
            return None
        if token is not None and token != self._scrub_progress_token:
            self._scrub_progress_token = token
            self._scrub_last_progress = now  # heartbeat advanced -> progress

        idle = now - self._scrub_last_progress
        if idle <= self.scrub_hang_timeout_s:
            return None  # progressing, or not yet idle long enough

        # Genuine hang: lock held with no heartbeat advance for the timeout
        recovered = False
        if self.scrub_auto_recover:
            status = self._read_scrub_status()
            recovered = self._recover_stuck_scrub(status)
            if recovered:
                self._scrub_first_held = None  # streak ends; re-measure next hold
                # Route the respawn through the cooldown gate so a scrub that
                # re-hangs on the same root cause can't drive an unbounded
                # kill/respawn cycle (bounded only by scrub_hang_timeout_s).
                # The gate also arms _last_scrub_spawn; the timer is the backstop.
                self._maybe_spawn_scrub()  # use the freed lock, cooldown-gated
        if self._stuck_scrub_alerted:
            return None
        self._stuck_scrub_alerted = True
        action = ("killed the stale scrub and re-spawned" if recovered
                  else "manual intervention needed")
        return ("Auto-scrub hung: lock held, no progress for {:.0f}s -- {}"
                .format(idle, action))

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
