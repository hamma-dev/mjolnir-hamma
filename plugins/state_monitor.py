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
# enclosure alternating which partition is visible -- page at most this often.
# Without this floor, re-arming on a changed fault set turns a flap into an
# alert storm; with it, the damping counter can keep running instead of
# restarting, so a flapping fault can no longer mute itself forever.
#
# WALL CLOCK, not cycles. It used to be counted in this process's own monitor
# cycles, which is meaningless the moment the process restarts -- and is also
# not what "one hour" means when the counter is frozen during lulls.
DRIVE_TARGET_RENOTIFY_S = 3600

# --- Surviving a brokkr restart -------------------------------------------
# The latch lives in memory, so every brokkr restart re-pages every latched
# fault. On units whose fault needs a site visit (a drive swap in Australia,
# fsck on two others) that is pure noise: mj03's retained journal shows >=4
# brokkr starts in a month.
#
# ON TMPFS, DELIBERATELY. /dev/shm survives a service restart -- the case that
# produces the bursts, including the notifiers ImportError crash-loop -- and is
# wiped by a reboot, so a rebooted unit always re-evaluates from scratch and
# pages. That makes the dangerous failure mode structurally impossible rather
# than merely guarded: there is no stale note to suppress a genuine page. The
# SD card would also cover reboots, but it is the filesystem whose filling is a
# documented recurring incident (HAM-112/113), it is unwritable during exactly
# the disk-full event this check must survive, and it would reintroduce that
# suppression path. DEFAULT_SCRUB_STATUS_FILE is on /dev/shm for the same
# reason, so this is one storage idiom rather than two.
DEFAULT_DRIVE_STATE_FILE = "/dev/shm/hamma_drive_target_state.json"
DRIVE_TARGET_STATE_VERSION = 1
# A stored latch older than this is not trusted, independently of the tmpfs
# wipe. On tmpfs the only gap the note has to bridge is a service restart,
# which takes seconds; an hour is two orders of magnitude of headroom while
# still guaranteeing that a monitor absent for any substantial period
# re-evaluates and pages from scratch.
DRIVE_TARGET_STATE_MAX_AGE_S = 3600
# Cycles the check may fail to evaluate before it says so out loud (~1 h).
# Several paths here can silently self-disable, and a check that reports its
# own failures only to the log is a check nobody hears.
DRIVE_TARGET_BLIND_CYCLES = 60
# Seconds a file mtime may exceed the current clock before it is treated as
# untrustworthy rather than as evidence. fake-hwclock steps this fleet's
# clocks non-monotonically across reboots, and FAT32 stores local time with a
# mount-time offset, so future mtimes are a real occurrence here -- and a
# timestamp we cannot trust is not proof of anything.
DRIVE_TARGET_CLOCK_SLACK_S = 60

# THE PREMISE OF THE WHOLE CHECK. There is no automounter on these units --
# `setup_automount` installs a polkit *authorization* only, no fstab entry, no
# udev rule, no .mount unit. brokkr is the only thing that ever mounts
# /media/<user>/DATA*, and it does it from FileOutputStep.execute ->
# render_output_filename -> get_output_drive -> mount_drives: once per science
# packet, i.e. only when lightning triggers the sensor. udisks removes the
# mountpoint directories at boot. So "labelled but not mounted" is the
# ORDINARY state of a quiet or freshly-booted unit, and it lasts until the next
# lightning trigger -- an unbounded, weather-dependent wait.
#
# The only thing that separates the fault from that state is evidence that
# brokkr's writer ran and the partition still is not there. That evidence has
# to prove two things, and earlier versions of this check proved neither:
#
#   ATTRIBUTABLE -- written by brokkr's science writer and nothing else. Writes
#     to /media/<user>/DATA* do NOT qualify: `hamma_scrub.py --recover`
#     reproduces brokkr's own directory and filename convention there (see
#     `compute_target_path`) and mkstemps in the partition root, and it is
#     spawned by THIS class every scrub_cooldown_s while the AGS drive is low.
#     The check would have been manufacturing the evidence it consumed. Only
#     the SD-card fallback path qualifies: `hamma_scrub.select_target_drive`
#     globs the DATA partitions and returns None when it finds none -- it never
#     writes to ~/brokkr/<system>/science.
#
#   NEWER THAN THE CURRENT TOPOLOGY -- a write from before the mountpoints were
#     last rearranged says nothing about whether brokkr has tried since. A
#     reboot to clear a stale mountpoint, or an operator halfway through the
#     unmount/rmdir/remount remedy, both leave recent writes lying around; a
#     bare "within the last hour" window reported both as confirmed faults.
#     `_drive_topology_since` timestamps the last observed change in (labels,
#     brokkr's candidates, what is mounted under the base) and the evidence
#     must post-date it. This is also why no window constant is needed: on a
#     stable topology a single write is evidence indefinitely, which is exactly
#     right for a fault nobody has touched.

# THE INVARIANT, for anything that touches the state machine below:
#
#     The latch and its floor may be cleared only by positive evidence that
#     the fault is gone -- never by the fault becoming unobservable.
#
# Every cycle resolves to exactly one of three verdicts. CLEAR is reachable
# only from an affirmative "we looked, we could see, and there is no fault";
# everything else is UNKNOWN, which freezes the latch, the floor and the
# counter. Bugs in three separate rounds of review were all the same mistake:
# a path where the fault stopped being visible took the healthy branch.
_VERDICT_FAULTY = "faulty"     # a fault is present right now
_VERDICT_CLEAR = "clear"       # we could see everything, and it is fine
_VERDICT_UNKNOWN = "unknown"   # we could not establish either -- freeze

# Fault kinds whose offender simply being gone IS the repair. For every other
# kind the offender must be observed HEALTHY before the latch clears: a
# partition that vanished has not been proven fixed, it has stopped being
# observable.
_RESOLVED_BY_ABSENCE = frozenset(["notdir"])

# Both views the HAM-185 check compares. Nothing here is a new drive-discovery
# rule: `candidates` is brokkr's own science-output resolution and `labels` is
# brokkr's own mount-side resolution, both read out of brokkr's config.
_DriveView = collections.namedtuple("_DriveView", [
    "candidates",     # list[Path]: what brokkr's writer would consider, or None
    "labels",         # list[Path]: labelled DATA devices attached, or None
    "mount_configured",  # bool: brokkr is configured to mount by label at all
    "base_path",      # Path: mount base, resolved as brokkr resolves it
    "mount_base_path",   # str|None: where the labelled devices are looked up
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
        self._drive_target_alerted_at = None  # wall clock when it went out
        # Loaded lazily on the first check, not here: it needs brokkr's unit
        # config, and a unit with enable_drive_checks=False never needs it.
        self._drive_state_loaded = False
        # Fingerprint of (labels, brokkr's candidates, what is mounted under
        # the base) and when it last changed. Evidence older than the current
        # topology is not evidence -- see the PREMISE block. Starting at None
        # means the first observed cycle sets the clock to now, so a restart
        # correctly requires a fresh write before anything can be confirmed.
        self._drive_topology = None
        self._drive_topology_since = None
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
        except Exception as e:     # noqa: BLE001 - matches _drive_state_identity
            # Deliberately as broad as the identical lookup in
            # `_drive_state_identity`. `check_drive_target` has try/finally and
            # no `except`, so anything escaping here (an AttributeError from a
            # config proxy touched pre-init, say) would bypass this file's
            # WARNING-and-degrade path and surface as a generic run_checks
            # traceback instead.
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
        # `min_free_gb` is tested for PRESENCE, not truthiness: it is the one
        # numeric setting here, and 0 is a legitimate value meaning "no
        # capacity floor" -- brokkr's `select_drive` has no reserved sentinel
        # for "disabled". A falsy test read 0 as "config missing" and silently
        # skipped the entire check, leaving only the ~1-hour blind watchdog.
        # Every other key is a path or glob, where empty is as broken as
        # missing.
        absent = [name for name, value in required
                  if value is None
                  or (name != "min_free_gb" and not value)]
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
            mount_base_path=mount_base_path,
            base_path=resolved_base,
            fallback_path=fallback_path,
            min_free_gb=min_free_gb,
            )

    @staticmethod
    def _readable_dir(path):
        """'yes', 'absent' or 'unreadable' for a directory we need to see.

        `Path.glob` and `os.listdir` do not agree about failure: glob returns
        [] for a missing directory AND for one we lack permission on, so an
        unreadable /dev/disk/by-label would otherwise read as "no labelled
        partitions" rather than as "we cannot see". 'absent' is an observation
        (nothing has ever been mounted there); 'unreadable' is blindness.
        """
        if path is None:
            return "absent"
        if not os.path.isdir(str(path)):
            return "absent"
        return "yes" if os.access(str(path), os.R_OK | os.X_OK) else "unreadable"

    def _mounted_under(self, base_path):
        """Names of everything actually mounted under `base_path`, or None.

        Applies no pattern, so it is not a drive-discovery rule. Two uses: it
        lets the alert name the mountpoint the operator has to act on
        (``DATA311`` in the mj51 incident), which no glob-based view can report
        by construction, and it is part of the topology fingerprint, so an
        operator unmounting something mid-remedy invalidates stale evidence.

        Returns None if the directory exists but cannot be read -- blindness,
        not emptiness.
        """
        state = self._readable_dir(base_path)
        if state == "absent":
            return []      # nothing has ever been mounted here
        if state == "unreadable":
            self.logger.warning(
                "state_monitor: %s exists but cannot be read", base_path)
            return None
        try:
            entries = sorted(os.listdir(str(base_path)))
        except OSError as e:
            self.logger.warning(
                "state_monitor: cannot list %s (%s)", base_path, e)
            return None
        return [entry for entry in entries
                if os.path.ismount(os.path.join(str(base_path), entry))]

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

    def _fell_back_to_sd_since(self, view, since):
        """Did brokkr's writer write to the SD-card fallback after `since`?

        The one piece of evidence this check trusts. See the PREMISE block at
        the top of the file for why it has to be attributable and why it has to
        post-date the current topology; in short, writes under
        /media/<user>/DATA* are not attributable (the scrub makes them, and
        this class spawns the scrub) and a write from before the mountpoints
        were rearranged proves nothing about now.

        Returns
        -------
        (bool, str | None)
            Whether brokkr fell back to the SD card after `since`, and a
            blindness reason if the answer could not be trusted.
        """
        if not view.fallback_path:
            return False, ("brokkr has no fallback_path configured, so a "
                           "write to the SD card cannot be detected")
        newest = self._newest_write(view.fallback_path)
        if newest is None:
            return False, None      # brokkr has never fallen back here
        if newest > time.time() + DRIVE_TARGET_CLOCK_SLACK_S:
            # NOT evidence. fake-hwclock steps these clocks backwards across a
            # reboot and FAT32 stores local time, so future mtimes happen --
            # and a timestamp from an untrustworthy clock proves nothing. The
            # caller turns this into UNKNOWN, never into a fault or a clear.
            return False, ("a science-write timestamp at {} is in the future; "
                           "the clock cannot be trusted, so write recency "
                           "cannot be established".format(view.fallback_path))
        return newest > since, None

    @staticmethod
    def _drive_state_identity():
        """(system, unit) for this sensor, or None if it cannot be determined.

        Used to refuse a stored latch that did not come from this unit. None
        is a refusal, not a pass: an identity we cannot check is one we cannot
        trust, and the safe direction is to page.
        """
        try:
            from brokkr.config.metadata import METADATA
            from brokkr.config.unit import UNIT_CONFIG
            return [METADATA["name"], UNIT_CONFIG["number"]]
        except Exception:      # noqa: BLE001 - any failure is "unknown"
            return None

    def _load_drive_state(self):
        """Restore the latch from the previous process, or fail open.

        FAIL OPEN IS THE WHOLE CONTRACT. A latch that is absent, unreadable,
        malformed, from another schema version, from another unit, or simply
        too old is discarded, and the check then behaves exactly as it did
        before persistence existed: it pages. The asymmetry is deliberate --
        a missing latch repeats a page, a wrong latch suppresses one, and this
        check exists because a suppressed page cost eight days of science data.
        """
        def give_up(reason, *args):
            self.logger.info(
                "state_monitor: not restoring the drive-target latch (" +
                reason + "); the next fault will page", *args)
            self._drive_target_alerted = None
            self._drive_target_alerted_at = None

        try:
            with open(DEFAULT_DRIVE_STATE_FILE) as state_file:
                stored = json.load(state_file)
        except FileNotFoundError:
            return          # nothing stored: the normal first-run case
        except (OSError, ValueError) as e:
            give_up("it could not be read: %s", e)
            return

        try:
            if not isinstance(stored, dict):
                give_up("it is not an object")
                return
            if stored.get("version") != DRIVE_TARGET_STATE_VERSION:
                give_up("it is schema version %r, not %r",
                        stored.get("version"), DRIVE_TARGET_STATE_VERSION)
                return
            identity = self._drive_state_identity()
            if identity is None or stored.get("identity") != identity:
                give_up("it belongs to %r, not %r",
                        stored.get("identity"), identity)
                return
            signature = stored.get("signature")
            if (not isinstance(signature, list)
                    or not all(isinstance(item, str) for item in signature)):
                give_up("its fault set is malformed: %r", signature)
                return
            alerted_at = stored.get("alerted_at")
            last_seen = stored.get("last_seen")
            if not all(isinstance(value, (int, float))
                       for value in (alerted_at, last_seen)):
                give_up("its timestamps are malformed: %r, %r",
                        alerted_at, last_seen)
                return
            age = time.time() - last_seen
            if not 0 <= age <= DRIVE_TARGET_STATE_MAX_AGE_S:
                # Negative means the clock stepped (fake-hwclock does this);
                # too old means the monitor was away long enough that the
                # world could have changed under it. Neither is trustworthy.
                give_up("it was last confirmed %.0f s ago", age)
                return
            if not 0 <= time.time() - alerted_at:
                give_up("it was raised in the future")
                return
        except Exception as e:                      # pragma: no cover
            give_up("it could not be validated: %s", e)
            return

        self._drive_target_alerted = frozenset(signature)
        self._drive_target_alerted_at = alerted_at
        self.logger.info(
            "state_monitor: restored the drive-target latch for %s (raised "
            "%.0f s ago); it will not be re-paged unless it changes or clears",
            ", ".join(sorted(signature)), time.time() - alerted_at)

    def _save_drive_state(self):
        """Persist the latch, or give up quietly. NEVER raises, never blocks.

        Written on tmpfs, at most one small fixed-size object, replaced
        atomically, and removed entirely when there is no latch -- so it
        cannot grow, cannot be half-written, and cannot outlive the fault. A
        filesystem that is full or read-only costs the suppression, not the
        check.
        """
        try:
            if self._drive_target_alerted is None:
                try:
                    os.remove(DEFAULT_DRIVE_STATE_FILE)
                except FileNotFoundError:
                    pass
                return
            payload = {
                "version": DRIVE_TARGET_STATE_VERSION,
                "identity": self._drive_state_identity(),
                "signature": sorted(self._drive_target_alerted),
                "alerted_at": self._drive_target_alerted_at,
                "last_seen": time.time(),
                }
            temp_path = DEFAULT_DRIVE_STATE_FILE + ".tmp"
            with open(temp_path, "w") as state_file:
                json.dump(payload, state_file)
            os.replace(temp_path, DEFAULT_DRIVE_STATE_FILE)
        except Exception as e:     # noqa: BLE001 - must not reach the loop
            self.logger.warning(
                "state_monitor: could not persist the drive-target latch to "
                "%s (%s); it will not survive a brokkr restart",
                DEFAULT_DRIVE_STATE_FILE, e)

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
        this check's own first mistake. See the PREMISE block at the top of the
        file: there is no automounter, so a quiet or freshly-booted unit
        legitimately shows every partition labelled and none mounted, for an
        unbounded time. A hidden partition is reported only when
        `_fell_back_to_sd_since` confirms brokkr wrote to the SD-card fallback
        after the current topology was established -- the one signal that is
        both attributable to brokkr's writer and newer than the arrangement it
        is being used to judge. That gate cannot fire on a quiet unit, on a
        unit rebooted to clear a stale mountpoint, on a half-finished manual
        remedy, or on scrub activity, and it needs no window constant.

        Everything else in the state machine obeys THE INVARIANT stated at the
        top of the file: only an affirmative observation of health clears the
        latch. Anything that makes the fault unobservable freezes it instead.

        Faults reported, all of them "brokkr cannot write science data here":

        - **hidden** (mtime-gated, above): EVERY labelled DATA partition is
          missing from brokkr's candidate set and brokkr is falling back to
          the SD card -- the mj51 shape, and total loss of science data.
        - **unused**: some labelled partition is missing while another is
          mounted and working. Data is still landing, so this is not an
          emergency, but the unit is on part of its storage and will look
          healthy until the survivor fills -- about 120 days at the measured
          14.75 GiB/day, at which point the operator has both an emergency and
          the original fault. Caught here it is still just remount + rmdir.

          This branch does NOT use the mtime gate, and does not need it. Its
          evidence is structural and strictly stronger: `mount_drives`
          iterates and mounts EVERY labelled drive it does not already see
          mounted, and after a reboot nothing is mounted until brokkr does it
          (udisks removes the mountpoint directories). So a mounted candidate
          proves the mounter ran, and therefore that it ran on the missing
          ones and failed. The scrub cannot fake it, because the scrub mounts
          nothing.
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

        On restarts: the latch is persisted to `DEFAULT_DRIVE_STATE_FILE` so
        a brokkr restart does not re-page every latched fault -- on units whose
        fault needs a site visit that was ~16 pages a month of pure noise.

        THE FILE IS ON TMPFS ON PURPOSE, AND A REBOOT WILL RE-PAGE. That is
        the accepted cost, not an oversight: moving it to the SD card to "fix"
        the reboot case buys reboot coverage at the price of a
        stale-suppression path, on the one filesystem whose filling is a
        recurring incident (HAM-112/113) and which is unwritable during exactly
        the disk-full event this check must survive. A reboot losing the note
        means the check falls back to alerting, which is the correct direction
        for a check that exists because silence cost eight days. Every way of
        failing to read the note fails the same way: it pages.

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
        if not self._drive_state_loaded:
            self._drive_state_loaded = True
            self._load_drive_state()
        try:
            return self._evaluate_drive_target()
        finally:
            # One save point, on every exit path including exceptions, so the
            # stored latch cannot drift out of step with the in-memory one.
            self._save_drive_state()

    def _evaluate_drive_target(self):
        """The body of `check_drive_target`; see its docstring."""
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
                # A read-only partition is NOT usable capacity, so it must not
                # enter `usable` -- exactly like an unreadable one above.
                # Measured on mj54: DATA80 is mounted ro with 1465 GiB free
                # while DATA81 is the only writable partition. Counting DATA80
                # made it the "last one" with months of runway, in the same
                # alert whose readonly clause said writes to it fail.
                readonly.append(drive.name)
                continue
            usable.append(
                (drive.name, stat_result.f_bavail * stat_result.f_frsize))

        # --- observability -------------------------------------------------
        # Everything that stops us seeing the truth goes in one list. It is
        # what makes CLEAR unreachable except from an affirmative observation.
        blind_reasons = []

        mounted = self._mounted_under(view.base_path)
        if mounted is None:
            blind_reasons.append(
                "{} cannot be listed, so what is mounted there is "
                "unknown".format(view.base_path))
            mounted = []
        if not view.mount_configured:
            blind_reasons.append(
                "brokkr is not configured to mount by label (no mount_glob), "
                "so there is no second view to compare its drives against")
        elif view.labels is None:
            blind_reasons.append(
                "the labelled DATA partitions could not be enumerated")
        elif not view.labels and self._readable_dir(
                view.mount_base_path) == "unreadable":
            blind_reasons.append(
                "{} exists but cannot be read, so an attached partition would "
                "look identical to none".format(view.mount_base_path))

        # --- topology fingerprint -------------------------------------------
        # Evidence older than the current arrangement of mountpoints is not
        # evidence. Any change here restarts the clock, which is what makes a
        # reboot, a hot-replug and a half-finished manual remedy all wait for
        # a fresh write instead of reusing a stale one.
        fingerprint = (
            frozenset(drive.name for drive in (view.labels or ())),
            frozenset(candidate_dirs),
            frozenset(mounted),
            )
        if fingerprint != self._drive_topology:
            self._drive_topology = fingerprint
            self._drive_topology_since = time.time()

        hidden = []
        # A visible-but-unattributable fault: every partition is labelled and
        # none is mounted, but no fallback write proves brokkr has tried since.
        # That is NOT health -- it is the fault being unconfirmable -- so it
        # freezes the machine rather than clearing it. On a unit that has never
        # had a fault this is indistinguishable from CLEAR; on one that is
        # flapping it is what stops the damping counter being reset to zero on
        # every unconfirmable cycle, which would hold it below the threshold
        # forever.
        unconfirmed = False
        unused = []
        if view.labels:
            missing = sorted({drive.name for drive in view.labels}
                             - set(candidate_dirs))
            if missing and candidate_dirs:
                # STRUCTURAL evidence, and it is stronger than the mtime kind.
                # `mount_drives` iterates and mounts EVERY labelled drive it
                # does not already see mounted -- it is not "mount one and
                # stop" -- and after a reboot nothing is mounted until brokkr
                # does it, because udisks removes the mountpoint directories.
                # So a mounted candidate is itself proof that the mounter ran,
                # and therefore that it ran on these and failed (or that they
                # mounted where brokkr cannot see them). The scrub cannot
                # manufacture this: the scrub mounts nothing. No timestamp is
                # consulted on this path, and none is needed.
                unused = missing
            elif missing:
                fell_back, reason = self._fell_back_to_sd_since(
                    view, self._drive_topology_since)
                if reason:
                    blind_reasons.append(reason)
                elif fell_back:
                    hidden = missing
                else:
                    unconfirmed = True
                    # The ordinary state of a quiet or freshly-booted unit:
                    # brokkr has not been asked to write since the mountpoints
                    # were last arranged, so it has not had its chance yet.
                    self.logger.debug(
                        "state_monitor: %s labelled but not mounted; brokkr "
                        "has not fallen back to the SD card since the current "
                        "topology was established, so this is the pre-trigger "
                        "state, not a fault", ", ".join(missing))

        if blind_reasons:
            blind = self._note_blind("; ".join(blind_reasons))
        else:
            blind = None
            self._clear_blind()

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
        # THE INVARIANT block below. It does NOT nag; to make a persistent
        # data-loss condition re-page every DRIVE_TARGET_RENOTIFY_CYCLES,
        # delete the `== signature` early return below (the floor now holds
        # across lulls, so that change is bounded at one page per hour).
        faults = [("hidden", name) for name in hidden]
        faults += [("unused", name) for name in unused]
        faults += [("notdir", name) for name in notdir]
        faults += [("unreadable", name) for name, _err in unreadable]
        faults += [("readonly", name) for name in readonly]
        if all_full:
            faults += [("full", name) for name, _free in usable]
        elif last_partition:
            faults.append(("lastpartition", remaining[0][0]))
        signature = frozenset(
            "{}:{}".format(kind, name) for kind, name in faults)

        # --- THE INVARIANT ---------------------------------------------------
        # Exactly one verdict per cycle, and CLEAR is reachable only from an
        # affirmative observation. Two conditions have to hold for it: nothing
        # blinded us this cycle, AND every partition we previously latched a
        # fault against is now observed HEALTHY -- mounted where brokkr looks,
        # readable, writable and not full. A partition whose label merely
        # vanished has not been proven fixed; it has stopped being observable,
        # and clearing on that is how a flaky enclosure turned one fault into
        # 11 pages in 66 cycles.
        healthy_names = {name for name, _free in remaining}
        latched_names = set()
        for entry in (self._drive_target_alerted or ()):
            kind, _, name = entry.partition(":")
            if kind not in _RESOLVED_BY_ABSENCE:
                latched_names.add(name)
        if signature:
            verdict = _VERDICT_FAULTY
        elif (blind_reasons or unconfirmed
                or (latched_names - healthy_names)):
            verdict = _VERDICT_UNKNOWN
        else:
            verdict = _VERDICT_CLEAR

        if verdict is _VERDICT_UNKNOWN:
            # Freeze everything. Not recovery: the fault stopped being visible.
            return self._deliver_blind(blind)
        if verdict is _VERDICT_CLEAR:
            # Positive evidence that the fault is gone -- the only thing
            # allowed to clear the latch, the floor and the counter.
            self._drive_target_count = 0
            self._drive_target_alerted = None
            self._drive_target_alerted_at = None
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
        if (self._drive_target_alerted_at is not None
                and 0 <= (time.time() - self._drive_target_alerted_at)
                < DRIVE_TARGET_RENOTIFY_S):
            # A *different* fault set, but we paged recently. Re-arming on any
            # change is what keeps a drive swap from being muted; this floor is
            # what keeps a flapping enclosure from paging every cycle. A
            # negative elapsed time means the clock stepped, and falls through
            # to paging rather than to suppression.
            return self._deliver_blind(blind)

        reasons = []
        if hidden:
            # `hidden` is only ever set when brokkr has no candidate at all
            # and has been observed falling back, so the SD-card claim is
            # unconditional here by construction.
            consequence = ("science data is going to the SD card ({})"
                           .format(view.fallback_path
                                   or "brokkr's fallback path"))
            # Only prescribe the sensor-log #52 remedy when there really is a
            # mount sitting where brokkr does not look.
            stray = [name for name in mounted if name not in candidate_dirs]
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
                "labelled DATA partition(s) {} are attached but not mounted "
                "where brokkr looks, and brokkr has fallen back to the SD card "
                "since -- so it has tried and failed to mount them. {}. "
                "{}".format(", ".join(hidden), consequence, remedy))
        if unused:
            stray = [name for name in mounted if name not in candidate_dirs]
            if stray:
                remedy = ("something IS mounted under {} where brokkr does not "
                          "look ({}): the sensor-log #52 shape, where a stale "
                          "empty mountpoint directory forces udisks to mount "
                          "at a suffixed path. Unmount it, rmdir the leftover "
                          "empty directory, remount".format(
                              view.base_path, ", ".join(stray)))
            else:
                remedy = ("nothing is mounted for them under {}; check dmesg "
                          "and `udisksctl status` for a failing enclosure or "
                          "an unreadable partition".format(view.base_path))
            # The reassurance may name ONLY partitions brokkr can actually
            # write to. `candidate_dirs` is appended before the ST_RDONLY and
            # statvfs tests, so it still holds read-only and unreadable
            # mounts; `remaining` is what survives them AND has room. Using
            # the former here printed "science data is still landing on
            # DATA80" about the very partition whose read-only clause in this
            # same alert says writes to it fail -- mj54's topology, where the
            # claim is not merely imprecise but false: brokkr selects on free
            # space alone, so it picks that partition, fails, and falls back
            # to the SD card.
            receiving = sorted(name for name, _free in remaining)
            if receiving:
                consequence = (
                    "NO DATA IS BEING LOST: science data is still landing on "
                    "{}. But the unit is running on part of its storage and "
                    "will look healthy until that fills, so fix it now while "
                    "the fix is still cheap".format(", ".join(receiving)))
            else:
                consequence = (
                    "AND NOTHING WRITABLE IS LEFT: science data is going to "
                    "the SD card ({}). See this alert's other reasons for why "
                    "each mounted partition is unusable".format(
                        view.fallback_path or "brokkr's fallback path"))
            reasons.append(
                "labelled DATA partition(s) {} are attached but brokkr is not "
                "using them, while {} is mounted -- so brokkr's mounter has "
                "run and failed on them. {} -- {}".format(
                    ", ".join(unused), ", ".join(sorted(candidate_dirs)),
                    consequence, remedy))
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
        self._drive_target_alerted_at = time.time()
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
