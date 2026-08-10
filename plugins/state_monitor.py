"""
Plugin to monitor state variables from the charge controller.
"""

from math import nan
import collections
import fcntl
import inspect
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

# statvfs f_flag bit meaning "mounted read-only" (Linux; guarded for portability
# so the module still imports on a platform that does not define it).
ST_RDONLY = getattr(os, "ST_RDONLY", 1)

# UNITS (HAM-185 check): every byte quantity computed below is *displayed* in
# GiB (2**30), and every message that prints one says "GiB". The single
# threshold applied to a byte count is brokkr's own `min_free_gb` from
# science_binary_output.drive_kwargs, compared exactly the way brokkr compares
# it (`min_free_gb * 1e9`, decimal, see brokkr.utils.output.select_drive). We
# reuse brokkr's comparison rather than restating "full" in our own units.

# --- HAM-185 science-write-target check: tuning ----------------------------
# Module constants, not config keys, because none of these is plausibly a
# per-unit value and a config key buys exactly one thing: per-unit override.
# SCRUB_LOG_MAX_BYTES is the precedent.
#
# NOT because config keys are dangerous in general -- purge_space,
# alert_space, scrub_cooldown_s and hs_stale_cycles are all config keys on
# this same class, and test_every_config_key_is_an_init_parameter already
# guards the "key outlives its parameter" TypeError. An earlier version of
# this comment claimed that hazard as the reason; it proved too much.
#
# CONVENTION SPLIT, for whoever comes next: this file now has both styles. The
# rule applied here is "config key iff a unit might need a different value",
# but that is this author's rule, not an agreed one, and the older keys
# predate it. Worth settling deliberately rather than inheriting two habits.

# Consecutive monitor cycles a fault must persist before it pages (~5 min at
# the 60 s monitor interval).
DRIVE_TARGET_CYCLES = 5
# While a fault persists but its *shape* keeps changing -- a flaky USB
# enclosure alternating which partition is visible -- page at most this often
# (~1 h). Without this floor, re-arming on a changed fault set turns a flap
# into an alert storm; with it, the damping counter can keep running instead
# of restarting, so a flapping fault can no longer mute itself forever.
DRIVE_TARGET_RENOTIFY_CYCLES = 60
# Cycles the check may fail to evaluate before it says so out loud (~1 h).
# Several paths here can silently self-disable, and a check that reports its
# own failures only to the log is a check nobody hears.
DRIVE_TARGET_BLIND_CYCLES = 60
# How recently brokkr's science writer must have run for a labelled-but-absent
# partition to count as a fault (1 h).
#
# THIS IS THE PREMISE OF THE WHOLE CHECK. There is no automounter on these
# units -- `setup_automount` installs a polkit *authorization* only, no fstab
# entry, no udev rule, no .mount unit. brokkr is the only thing that ever
# mounts /media/<user>/DATA*, and it does it from FileOutputStep.execute ->
# render_output_filename -> get_output_drive -> mount_drives: once per science
# packet, i.e. only when lightning triggers the sensor. udisks removes the
# mountpoint directories at boot. So "labelled but not mounted" is the
# ORDINARY state of a quiet or freshly-booted unit, and it lasts until the
# next lightning trigger -- an unbounded, weather-dependent wait. No cycle
# count can separate the fault from that state; only evidence that brokkr's
# writer actually ran, and the drive still is not there, can.
DRIVE_TARGET_EVIDENCE_S = 3600

# Both views the HAM-185 check compares. Nothing here is a new drive-discovery
# rule: `candidates` is brokkr's own science-output resolution and `labels` is
# brokkr's own mount-side resolution, both read out of brokkr's config.
_DriveView = collections.namedtuple("_DriveView", [
    "candidates",     # list[Path]: what brokkr's writer would consider, or None
    "labels",         # list[Path]: labelled DATA devices attached, or None
    "mount_configured",  # bool: brokkr is configured to mount by label at all
    "base_path",      # Path: mount base, resolved as brokkr resolves it
    "fallback_path",  # Path|None: where brokkr writes when it finds no drive
    "min_free_gb",    # numeric: brokkr's own "this partition is full" floor
    ])


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
            This is also the only switch for `check_drive_target`, whose
            damping and evidence window are module constants
            (DRIVE_TARGET_*) rather than config keys.
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
        # HAM-185 science-write-target check. `_drive_target_count` counts
        # consecutive cycles with ANY fault present; it is deliberately NOT
        # restarted when the fault *set* changes, because a fault that keeps
        # changing shape (a flaky enclosure) would then reset the damping
        # counter every cycle and never reach the alert threshold at all. The
        # latch is keyed on which partitions are at fault so a genuine change
        # re-arms, with DRIVE_TARGET_RENOTIFY_CYCLES as the floor between
        # pages so re-arming cannot become a storm.
        self._drive_target_count = 0          # consecutive faulty cycles
        self._drive_target_alerted = None     # fault set already reported
        self._drive_target_alert_at = None    # count when that page went out
        # Self-report when the check cannot evaluate, instead of going quiet.
        self._drive_target_blind_count = 0
        self._drive_target_blind_alerted = False
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
            checks.insert(1, self.check_drive_target)
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

    # --- HAM-185: is brokkr's science write target actually usable? ---------

    def _output_drive_defaults(self):
        """Default kwargs of brokkr's ``get_output_drive``, from its signature.

        `drive_kwargs` in main.toml only overrides *some* of brokkr's drive
        settings; the rest come from `get_output_drive`'s own defaults (e.g.
        ``base_path="/media/{current_user}"``). Those defaults are read out of
        brokkr's signature instead of being restated here, so a change on the
        brokkr side cannot silently desynchronise this monitor from the writer
        it is supposed to be watching.

        This is more fragile than restating the two literals would be -- a
        rename on the brokkr side yields a missing key rather than a stale
        value. That is the intended trade: `_brokkr_drive_view` guards EVERY
        key it depends on and declines to evaluate if any is absent, and
        `check_drive_target` reports a chronic inability to evaluate. A stale
        literal would instead keep answering confidently and wrongly.

        Returns
        -------
        dict
            Parameter name -> default value. Empty if the signature could not
            be introspected.
        """
        try:
            parameters = inspect.signature(
                brokkr.utils.output.get_output_drive).parameters
        except (TypeError, ValueError) as e:
            self.logger.warning(
                "state_monitor: cannot introspect brokkr's get_output_drive "
                "(%s); skipping the science-drive-target check", e)
            return {}
        return {name: param.default for name, param in parameters.items()
                if param.default is not inspect.Parameter.empty}

    def _drive_filename_kwargs(self, drive_kwargs):
        """Build the substitutions brokkr applies to its drive paths.

        brokkr's paths are templates (``/media/{current_user}``,
        ``~/brokkr/{system_name}/science``); these are the same three sources
        `brokkr.utils.output.render_output_filename` fills them from.
        """
        import brokkr.utils.misc   # local, matching check_drive's style

        filename_kwargs = {
            "current_user": brokkr.utils.misc.get_actual_username()}
        try:
            from brokkr.config.metadata import METADATA
            from brokkr.config.unit import UNIT_CONFIG
            filename_kwargs["system_name"] = METADATA["name"]
            filename_kwargs["unit_number"] = UNIT_CONFIG["number"]
        except (ImportError, KeyError) as e:
            self.logger.debug(
                "state_monitor: no system/unit metadata for drive paths: %s", e)
        filename_kwargs.update(drive_kwargs.get("filename_kwargs") or {})
        return filename_kwargs

    def _resolve_path(self, path, filename_kwargs, what):
        """Resolve a brokkr path template exactly the way brokkr resolves it.

        brokkr renders every path in TWO steps -- `.format(**filename_kwargs)`
        and then `brokkr.utils.misc.convert_path` (see
        `brokkr.utils.output.render_output_filename`, `find_drives`,
        `get_output_drive`). `convert_path` is what expands the leading `~` in
        the shipped `fallback_path = "~/brokkr/{system_name}/science"`.

        Doing only the `.format()` half leaves a literal `~/...` that matches
        nothing on disk. That is not cosmetic: the fallback path is this
        check's evidence that brokkr wrote to the SD card, and in the mj51
        topology it is the ONLY evidence source (there are no candidate
        mountpoints), so the total-data-loss case -- the one this check exists
        for -- went silent while the partial case still alerted.

        ANY path this file resolves by hand must go through here. Paths that
        come back from `find_drives` are already resolved by brokkr and must
        be used as-is rather than rebuilt from names.

        Returns
        -------
        pathlib.Path | None
            None if the template could not be resolved (logged at WARNING).
        """
        import brokkr.utils.misc   # local, matching check_drive's style

        try:
            return brokkr.utils.misc.convert_path(
                str(path).format(**filename_kwargs))
        except (KeyError, IndexError, AttributeError, TypeError) as e:
            self.logger.warning(
                "state_monitor: cannot resolve brokkr's %s %r: %s",
                what, path, e)
            return None

    def _find_drives_safely(self, drive_glob, base_path, filename_kwargs, what):
        """Run brokkr's own `find_drives`, returning None (logged) on failure.

        Returning None is deliberately distinct from returning ``[]``: "could
        not look" must not be mistaken for "looked, found nothing".
        """
        try:
            return brokkr.utils.output.find_drives(
                drive_glob, base_path, filename_kwargs=filename_kwargs)
        except (OSError, KeyError, IndexError, ValueError, TypeError,
                AttributeError) as e:
            self.logger.warning(
                "state_monitor: could not enumerate %s (%r under %r): %s",
                what, drive_glob, base_path, e)
            return None

    def _brokkr_drive_view(self):
        """Resolve, from brokkr's own config and code, both views of the drives.

        `candidates` is what brokkr's science writer would consider -- produced
        by calling `brokkr.utils.output.find_drives` with the settings brokkr
        itself passes to `get_output_drive`. `labels` is the set of attached,
        labelled DATA devices, produced by the same function with brokkr's
        *mount-side* settings (``mount_glob``/``mount_base_path``), i.e. the
        list brokkr's own `mount_drives` works from.

        No pattern is invented and no glob is widened: both views are brokkr's,
        and the HAM-185 signal is the divergence between them.

        Read-only. `get_output_drive` is deliberately NOT called, because it
        has a side effect (`mount_drives` shells out to ``udisksctl mount``);
        the monitor must never mount, unmount or spawn anything.

        Returns
        -------
        _DriveView | None
            None if brokkr's settings could not be resolved at all (logged at
            WARNING). Either view is None on its own if that one enumeration
            failed.
        """
        try:
            from brokkr.config.main import CONFIG
            drive_kwargs = dict(
                CONFIG["steps"]["science_binary_output"]["drive_kwargs"])
        except (ImportError, KeyError, TypeError) as e:
            self.logger.warning(
                "state_monitor: cannot read science_binary_output "
                "drive_kwargs (%s); skipping the drive-target check", e)
            return None

        defaults = self._output_drive_defaults()
        drive_glob = drive_kwargs.get("drive_glob")
        base_path = drive_kwargs.get("base_path", defaults.get("base_path"))
        # brokkr's own rule: `mount_glob = True` means "reuse drive_glob".
        mount_glob = drive_kwargs.get("mount_glob", defaults.get("mount_glob"))
        if mount_glob is True:
            mount_glob = drive_glob
        mount_base_path = drive_kwargs.get(
            "mount_base_path", defaults.get("mount_base_path"))
        min_free_gb = drive_kwargs.get(
            "min_free_gb", defaults.get("min_free_gb"))

        # Guard EVERY key we depend on, not just the first one. A brokkr
        # rename that silently zeroed one of these would otherwise leave the
        # check answering "healthy" from a half-resolved view.
        required = [("drive_glob", drive_glob), ("base_path", base_path),
                    ("min_free_gb", min_free_gb)]
        if mount_glob:
            required.append(("mount_base_path", mount_base_path))
        absent = [name for name, value in required if not value]
        if absent:
            self.logger.warning(
                "state_monitor: brokkr's science-output drive settings are "
                "incomplete (missing/empty: %s); skipping the drive-target "
                "check", ", ".join(absent))
            return None

        filename_kwargs = self._drive_filename_kwargs(drive_kwargs)

        resolved_base = self._resolve_path(
            base_path, filename_kwargs, "science drive base path")
        if resolved_base is None:
            return None

        candidates = self._find_drives_safely(
            drive_glob, base_path, filename_kwargs,
            "brokkr's science-output drive candidates")
        labels = None
        if mount_glob:
            labels = self._find_drives_safely(
                mount_glob, mount_base_path, filename_kwargs,
                "attached labelled DATA partitions")

        fallback_path = drive_kwargs.get(
            "fallback_path", defaults.get("fallback_path"))
        if fallback_path:
            # NOT cosmetic. This is where brokkr writes when it finds no
            # drive, so its mtime is the evidence that brokkr fell back to the
            # SD card -- and in the mj51 topology it is the only evidence
            # there is. It must be resolved the way brokkr resolves it,
            # including the `~` expansion `convert_path` does.
            fallback_path = self._resolve_path(
                fallback_path, filename_kwargs, "SD-card fallback path")

        return _DriveView(
            candidates=candidates,
            labels=labels,
            mount_configured=bool(mount_glob),
            base_path=resolved_base,
            fallback_path=fallback_path,
            min_free_gb=min_free_gb,
            )

    def _mounted_under(self, base_path):
        """Names of everything actually mounted under `base_path`.

        Diagnostic only -- never used to decide anything, so it is not a drive
        discovery rule and applies no pattern. It exists so the alert can name
        the mountpoint the operator has to act on (``DATA071`` in the mj51
        incident), which no glob-based view can report by construction.
        """
        try:
            entries = sorted(os.listdir(base_path))
        except OSError as e:
            self.logger.warning(
                "state_monitor: cannot list %s (%s)", base_path, e)
            return []
        return [entry for entry in entries
                if os.path.ismount(os.path.join(base_path, entry))]

    def _newest_write(self, path):
        """Newest mtime at or one level under `path`, or None.

        brokkr writes each science file into a per-hour subdirectory
        (``{drive_path}/{utc_date}T{utc_hour}``), so a new file bumps the
        mtime of its hour directory. Scanning one level deep is therefore
        enough, and the cost is bounded by the number of hour directories
        rather than the number of files (mj03's fallback tree holds 63 MB of
        2025 data; walking it every cycle would not be free).
        """
        newest = None
        try:
            newest = os.stat(str(path)).st_mtime
            for entry in os.scandir(str(path)):
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
        except OSError as e:
            self.logger.debug(
                "state_monitor: no write history at %s (%s)", path, e)
        return newest

    def _writer_ran_recently(self, view, candidate_paths):
        """Has brokkr's science writer actually run in the recent past?

        This is the positive evidence that separates "brokkr tried to mount
        this partition and still cannot write to it" from "brokkr has not been
        asked to write anything yet". brokkr resolves (and mounts) drives once
        per science packet, so a write -- to a DATA partition or to the SD-card
        fallback -- proves `mount_drives` ran. Absent such a write, a missing
        mountpoint is the ordinary state of a quiet unit and means nothing.

        Returns
        -------
        (bool, float | None)
            Whether a write happened within DRIVE_TARGET_EVIDENCE_S, and the
            age in seconds of the newest write found (None if none was found).
        """
        # The candidate paths come straight from brokkr's `find_drives`, so
        # they are already resolved by brokkr; never rebuild them from names.
        paths = list(candidate_paths)
        if view.fallback_path:
            paths.append(view.fallback_path)
        newest = None
        for path in paths:
            mtime = self._newest_write(path)
            if mtime is not None and (newest is None or mtime > newest):
                newest = mtime
        if newest is None:
            return False, None
        age = time.time() - newest
        # A negative age means the clock stepped (fake-hwclock on these units
        # does that across a reboot). Count it as recent: going quiet on a
        # clock oddity is the failure mode this check exists to remove.
        return age <= DRIVE_TARGET_EVIDENCE_S, age

    def _note_blind(self, reason):
        """Count a cycle the check could not evaluate; page once if chronic.

        Several paths in this check return None after logging. A monitor whose
        own failures are reported only to the log is a monitor nobody hears,
        so a persistent inability to evaluate becomes an alert in its own
        right -- distinguishable from "evaluated, healthy".

        Returns a *candidate* message and deliberately does NOT latch: the
        caller may discard it in favour of a fault message, and a latch set
        for an alert that was never sent would swallow the only page this
        watchdog ever makes. `_deliver_blind` latches at the point of return,
        the same rule the fault latch follows.
        """
        self._drive_target_blind_count += 1
        if (self._drive_target_blind_count >= DRIVE_TARGET_BLIND_CYCLES
                and not self._drive_target_blind_alerted):
            return ("The science-drive-target check has not been able to "
                    "evaluate for {} cycles ({}); it is currently watching "
                    "nothing. See the state_monitor WARNING lines for the "
                    "underlying error.".format(
                        self._drive_target_blind_count, reason))
        return None

    def _deliver_blind(self, message):
        """Latch the blind watchdog only if its message is really being sent."""
        if message is not None:
            self._drive_target_blind_alerted = True
        return message

    def _clear_blind(self):
        """Note a cycle that evaluated cleanly, re-arming the blind watchdog."""
        self._drive_target_blind_count = 0
        self._drive_target_blind_alerted = False

    def check_drive_target(self, input_data):
        """Alert when brokkr cannot write science data to an attached DATA drive.

        The gap this closes (HAM-185, sensor-log #52 on mj51): a labelled DATA
        drive is attached and mounted, but *not where brokkr looks*, so brokkr
        silently falls back to the SD card. On mj51 a stale empty
        ``/media/pi/DATA07`` directory made udisks mount the real drive at
        ``DATA071``; brokkr's ``DATA??`` never matched it and eight days of
        science data went to the SD card with nothing alerting.

        The structural signal is the **divergence** between two views that
        brokkr itself maintains -- the partitions its writer would consider,
        and the labelled devices its mounter knows about. Widening the glob so
        the check can see the suffixed mount is what PR #84 did, and it reports
        "healthy, 500 GB free" on exactly this topology, because the filesystem
        it then measures is not the one brokkr writes to.

        Divergence alone is NOT sufficient, and treating it as sufficient was
        this check's own first mistake. brokkr is the only thing on these units
        that mounts /media/<user>/DATA* (there is no automounter; see
        DRIVE_TARGET_EVIDENCE_S), and it does so only when writing a science
        packet -- i.e. only when lightning triggers the sensor. udisks deletes
        the mountpoint directories at boot, so a quiet or freshly-booted unit
        legitimately shows every partition labelled and none mounted, for an
        unbounded time. A hidden partition is therefore reported only when
        `_writer_ran_recently` confirms brokkr's writer has run since -- proof
        that `mount_drives` was given its chance and the partition still is not
        there. That gate cannot fire on a quiet unit and needs no tuning.

        Faults reported, all of them "brokkr cannot write science data here":

        - **hidden** (evidence-gated, above): a labelled DATA partition has no
          matching mountpoint in brokkr's candidate set. If brokkr has no
          candidates at all it is writing to the SD card (the mj51 shape); if
          it has others, data is still landing but capacity is halved.
        - **not a directory**: brokkr's `find_drives` filter (``not is_dir()
          or ismount()``) screens *directories* only, so any non-directory
          match is kept unconditionally and `statvfs` on it reports the
          containing filesystem -- a stray file named like a partition would
          otherwise stand in for a real one. Such a match never satisfies a
          label here, and is reported.
        - **unreadable**: a candidate mountpoint that raises on `statvfs`.
          brokkr's own `select_drive` stats the same path and would raise too;
          it is a fault, and it is logged rather than skipped in silence.
        - **read-only**: a candidate mounted ``ro`` (what a dirty unmount
          leaves behind on a FAT volume) -- brokkr will select it and every
          write will fail.
        - **capacity**: see below. Reported alongside the others, never
          suppressed by them: "one partition is hidden" and "the one that is
          left is full" are both true and the second is the emergency.

        On capacity: a fixed "low free space" floor is close to useless here.
        The disks are 1.8 TB and mj03 writes 14.75 GiB/day, so a 25 GiB floor
        stays silent for ~226 days and then gives 41 hours of warning, once --
        and nothing on the sensor can free these partitions (`hamma_scrub`
        only ever deletes on the AGS). What is reported instead is the
        transition brokkr's name-ordered fill produces months earlier: the
        earlier partitions are full and the unit is on its last one. "Full" is
        brokkr's own `min_free_gb`, so no new threshold is introduced.

        Be honest about the limit of that: it needs at least two usable
        partitions. The surveyed units are two partitions of ONE physical disk
        (DATA31 -> sda1, DATA32 -> sda2), so they do get the early warning; a
        single-partition unit gets only the `min_free_gb` floor, which at
        100 MB and 14.75 GiB/day is about nine minutes. That is a real gap and
        it is not fixed here -- fixing it needs a capacity policy, not another
        threshold in this function.

        Alert-only, per HAM-185: nothing here unmounts, `rmdir`s, remounts or
        spawns. HAM-173's boot-time oneshot is where automatic repair belongs.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute` (unused; kept for check uniformity).

        Returns
        -------
        str | None
            Alert naming the offending drives and the remedy, or None.
        """
        view = self._brokkr_drive_view()
        if view is None or view.candidates is None:
            # Nothing was evaluated (already logged at WARNING). Leave BOTH
            # the fault latch and its counter untouched -- an error must
            # neither clear an outstanding alert nor count towards raising one
            # -- but do not fail silently either: count it, and say so if it
            # becomes chronic.
            return self._deliver_blind(self._note_blind(
                "brokkr's drive settings or its science-output drive list "
                "could not be read"))

        candidate_dirs = []   # names brokkr's writer would accept
        candidate_paths = []  # the same, as brokkr's own resolved Paths
        notdir = []
        unreadable = []       # (name, error text)
        readonly = []
        usable = []           # (name, free bytes)
        for drive in view.candidates:
            try:
                is_dir = drive.is_dir()
            except OSError as e:          # pragma: no cover - is_dir swallows
                self.logger.warning(
                    "state_monitor: cannot stat %s: %s", drive, e)
                unreadable.append((drive.name, str(e)))
                continue
            if not is_dir:
                self.logger.warning(
                    "state_monitor: %s matches brokkr's drive pattern but is "
                    "not a directory; brokkr keeps such matches unfiltered",
                    drive)
                notdir.append(drive.name)
                continue
            candidate_dirs.append(drive.name)
            candidate_paths.append(drive)
            try:
                stat_result = os.statvfs(str(drive))
            except OSError as e:
                # Logged, not swallowed: a permanently-erroring mount must not
                # be indistinguishable from a healthy one.
                self.logger.warning(
                    "state_monitor: %s is in brokkr's drive set but its free "
                    "space cannot be read: %s", drive, e)
                unreadable.append((drive.name, str(e)))
                continue
            if stat_result.f_flag & ST_RDONLY:
                readonly.append(drive.name)
            usable.append(
                (drive.name, stat_result.f_bavail * stat_result.f_frsize))

        hidden = []
        # True when this cycle produced no *confirmed* hidden partition but
        # also could not establish that there is none. An empty fault set then
        # means "not proven", not "recovered", and must not clear the latch --
        # see the `if not signature` branch below.
        unproven = False
        if view.labels is None and view.mount_configured:
            # "Could not look" is not "looked and found nothing". Letting a
            # failed label enumeration fall through to `hidden = []` would
            # report a unit in the mj51 state as healthy, which is exactly the
            # silence this check exists to remove.
            unproven = True
            blind = self._note_blind(
                "the labelled DATA partitions could not be enumerated")
        else:
            blind = None
            self._clear_blind()
            if view.labels is not None:
                missing = sorted({drive.name for drive in view.labels}
                                 - set(candidate_dirs))
                if missing:
                    ran, age = self._writer_ran_recently(view, candidate_paths)
                    if ran:
                        hidden = missing
                    else:
                        # The partitions are still missing; we just cannot
                        # prove brokkr has tried since. Suppressed, not fixed.
                        unproven = True
                        # The ordinary state of a quiet or freshly-booted
                        # unit, not a fault. See DRIVE_TARGET_EVIDENCE_S.
                        self.logger.debug(
                            "state_monitor: %s labelled but not mounted; "
                            "brokkr's science writer has not run recently "
                            "(%s), so this is the pre-trigger state, not a "
                            "fault", ", ".join(missing),
                            "no writes found" if age is None
                            else "{:.0f}s ago".format(age))

        # Capacity is NOT suppressed by a hidden partition: "DATA08 is hidden"
        # and "the DATA07 that is left is full" are both true, and the second
        # one is the emergency.
        full = sorted(name for name, free in usable
                      if free < view.min_free_gb * 1e9)
        remaining = [(name, free) for name, free in usable
                     if free >= view.min_free_gb * 1e9]
        all_full = bool(usable) and not remaining
        last_partition = len(usable) > 1 and len(remaining) == 1

        # Latch signature: the *set of offending partitions*, per fault kind.
        # A different offender is a different alert (subject to the renotify
        # floor below). An unchanged fault pages once and then stays quiet
        # until it GENUINELY clears -- matching _low_space_alerted /
        # _stuck_scrub_alerted in this file. "Genuinely" is load-bearing: see
        # the `unproven` handling below. It does NOT nag; to make a persistent
        # data-loss condition re-page every DRIVE_TARGET_RENOTIFY_CYCLES,
        # delete the `== signature` early return below (the floor now holds
        # across lulls, so that change is bounded at one page per hour).
        faults = [("hidden", name) for name in hidden]
        faults += [("notdir", name) for name in notdir]
        faults += [("unreadable", name) for name, _err in unreadable]
        faults += [("readonly", name) for name in readonly]
        if all_full:
            faults += [("full", name) for name, _free in usable]
        elif last_partition:
            faults.append(("lastpartition", remaining[0][0]))
        signature = frozenset(
            "{}:{}".format(kind, name) for kind, name in faults)

        if not signature:
            if unproven:
                # NOT recovery: the partitions are still missing, or we could
                # not enumerate them. Freeze the latch and the counter.
                # Clearing here would re-arm the alert for the next burst of
                # triggers -- and because the renotify floor below is guarded
                # on `_drive_target_alert_at is not None`, clearing that too
                # would bypass the storm floor entirely. Measured on the
                # version that did clear: one never-fixed mj51 fault paged
                # once per storm, four times over four bursts.
                return self._deliver_blind(blind)
            # Genuinely healthy: clear the latch AND the counter on this exit
            # path, so the next fault is timed and reported from scratch.
            self._drive_target_count = 0
            self._drive_target_alerted = None
            self._drive_target_alert_at = None
            return self._deliver_blind(blind)
        # A fault is present. Count it -- WITHOUT regard to which fault it is.
        # Restarting the count when the fault set changes lets a fault that
        # keeps changing shape hold the counter below the threshold forever;
        # measured, that silenced 40 consecutive faulty cycles entirely.
        self._drive_target_count += 1
        if self._drive_target_count < DRIVE_TARGET_CYCLES:
            # damping: not yet persistent enough to page
            return self._deliver_blind(blind)
        if self._drive_target_alerted == signature:
            # same fault, already reported; stay quiet
            return self._deliver_blind(blind)
        if (self._drive_target_alert_at is not None
                and (self._drive_target_count - self._drive_target_alert_at
                     < DRIVE_TARGET_RENOTIFY_CYCLES)):
            # A *different* fault set, but we paged recently. Re-arming on any
            # change is what keeps a drive swap from being muted; this floor is
            # what keeps a flapping enclosure from paging every cycle.
            return self._deliver_blind(blind)

        reasons = []
        if hidden:
            # Only claim the SD-card fallback when brokkr would actually take
            # it -- brokkr falls back on `not canidate_drives`, so with any
            # candidate left the data is still landing on a real partition.
            if not candidate_dirs:
                consequence = ("science data is going to the SD card ({})"
                               .format(view.fallback_path
                                       or "brokkr's fallback path"))
            else:
                consequence = ("brokkr is still writing to {}, so data is not "
                               "being lost yet, but the unit is down to part "
                               "of its storage".format(", ".join(
                                   sorted(candidate_dirs))))
            # Only prescribe the sensor-log #52 remedy when there really is a
            # mount sitting where brokkr does not look.
            stray = [name for name in self._mounted_under(view.base_path)
                     if name not in candidate_dirs]
            if stray:
                remedy = ("something IS mounted under {} where brokkr does not "
                          "look ({}): the sensor-log #52 shape, where a stale "
                          "empty mountpoint directory forces udisks to mount at "
                          "a suffixed path. Unmount it, rmdir the leftover "
                          "empty directory, remount".format(
                              view.base_path, ", ".join(stray)))
            else:
                remedy = ("nothing at all is mounted for them under {}; check "
                          "dmesg and `udisksctl status` for a failing "
                          "enclosure or an unreadable partition".format(
                              view.base_path))
            reasons.append(
                "labelled DATA partition(s) {} are attached and brokkr's "
                "science writer has run since, so it has tried and failed to "
                "mount them -- {}. {}".format(
                    ", ".join(hidden), consequence, remedy))
        if notdir:
            reasons.append(
                "{} under {} match brokkr's drive pattern but are not "
                "directories; brokkr keeps such matches and would stat the SD "
                "card through them. Move or delete them".format(
                    ", ".join(notdir), view.base_path))
        if unreadable:
            reasons.append(
                "{} are mounted where brokkr expects a partition but cannot "
                "be read; brokkr's own drive selection stats the same paths "
                "and will fail".format(", ".join(
                    "{} ({})".format(name, err) for name, err in unreadable)))
        if readonly:
            reasons.append(
                "{} is mounted READ-ONLY, so every brokkr write to it fails "
                "(a dirty unmount leaves a FAT volume this way); fsck and "
                "remount read-write".format(", ".join(readonly)))
        if all_full:
            # brokkr only applies min_free_gb inside `select_drive`, which
            # `get_output_drive` calls only when there is more than one
            # candidate; with exactly one it returns that partition unchecked
            # and the write fails later at ENOSPC. Say which of the two.
            if len(usable) > 1:
                outcome = ("brokkr's own drive selection now fails outright "
                           "(RuntimeError: All drives full!)")
            else:
                outcome = ("brokkr does not apply that floor with only one "
                           "candidate, so it will keep writing until the "
                           "write fails with ENOSPC")
            reasons.append(
                "every DATA partition brokkr can use ({}) is below its own "
                "min_free_gb={:g} floor; {}. Swap or empty the disk".format(
                    ", ".join(name for name, _f in usable), view.min_free_gb,
                    outcome))
        elif last_partition:
            name, free = remaining[0]
            reasons.append(
                "{} of {} DATA partitions are full ({}); brokkr is now "
                "writing to the last one, {}, with {:.1f} GiB free. Swap or "
                "empty the disk before it fills".format(
                    len(full), len(usable), ", ".join(full), name,
                    free / (2 ** 30)))

        # Latch only once the message is actually built, so a formatting bug
        # cannot mute the fault permanently by latching without alerting.
        self._drive_target_alerted = signature
        self._drive_target_alert_at = self._drive_target_count
        return ("Science drive target problem -- " + "; ".join(reasons)
                + ". Note the auto-scrub cannot help with any of this: it "
                  "only deletes on the AGS, never on the mj-side DATA "
                  "partitions.")

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
