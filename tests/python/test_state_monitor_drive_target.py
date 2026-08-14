"""Tests for check_drive_target -- brokkr's science write target (HAM-185).

Two acceptance tests, and they pull against each other:

* `TestMj51Topology::test_mj51_topology_alerts` -- the literal sensor-log #52
  shape (stale `/media/pi/DATA31` directory, real partition mounted at
  `DATA311`) must alert.
* `TestPreTriggerState` -- a quiet or freshly-booted unit shows exactly the
  same *structural* shape and must stay silent. brokkr is the only thing that
  mounts /media/<user>/DATA*, it does so once per science packet, and udisks
  deletes the mountpoint directories at boot, so "labelled but not mounted"
  is the ordinary state of a unit that has not been struck by lightning yet.
  Measured on mj03's 2026-08-07 reboot: brokkr mounted at T+90 s. mj43 has
  been in that state for 6.5 days.

What separates them is evidence that brokkr's writer actually ran. Anything
that alerts on the structure alone pages the whole fleet on its next reboot.

Test-harness notes:

* Every behavioural test builds its StateMonitor through the real `__init__`.
  `MockOutputStep` mirrors brokkr's `Executable.__init__` signature exactly --
  it has no `**kwargs` -- so a kwarg that would be a TypeError at pipeline
  build on a sensor is a TypeError here too.
* `Tree.drive_kwargs()` READS the shipped `config/main.toml` and redirects
  exactly two values, `base_path` and `mount_base_path`, at the temp tree.
  Everything else is the shipped value; deleting `drive_glob` from the config
  must fail this suite, not pass it. In particular `fallback_path` keeps its
  shipped `~/brokkr/{system_name}/science` form and `sensor()` redirects HOME,
  because substituting a pre-expanded absolute path there once hid a
  production bug that made the acceptance case silent.
* `brokkr.utils.output` is the real module, loaded by `_real_brokkr`; the
  stand-in is used only where brokkr cannot be found at all.
* The tuning knobs are module constants, so tests exercise them with
  `tuning(...)` rather than through config.
"""

import ast
import importlib.util
import inspect
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

try:
    import tomllib as toml_reader
except ImportError:  # Python < 3.11 (the sensors are on 3.7)
    import tomli as toml_reader

try:
    from . import _real_brokkr
except ImportError:
    import _real_brokkr


REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"
MAIN_TOML = REPO_ROOT / "config" / "main.toml"

USER = "pi"
GIB = 2 ** 30


# --- brokkr stand-ins --------------------------------------------------------
#
# Used only when no brokkr source can be found. `find_drives` is copied from
# brokkr/src/brokkr/utils/output.py verbatim (minus its debug logging) and
# `get_output_drive` reproduces brokkr's signature exactly; TestBrokkrContract
# fails if either drifts, whenever real brokkr is available to compare against.

def _convert_path(path):
    """brokkr.utils.misc.convert_path, verbatim."""
    return Path(
        str(path).replace("~", "~" + os.getenv("SUDO_USER", ""))).expanduser()


def _find_drives(drive_glob, base_path, filename_kwargs=None):
    """brokkr.utils.output.find_drives, verbatim."""
    if filename_kwargs is None:
        filename_kwargs = {}
    base_path = _convert_path(base_path.format(**filename_kwargs))
    drive_glob = drive_glob.format(**filename_kwargs)
    return [drive for drive in base_path.glob(drive_glob)
            if not drive.is_dir() or os.path.ismount(drive)]


def _get_output_drive(
        drive_glob,
        base_path="/media/{current_user}",
        mount_glob=None,
        mount_base_path="/dev/disk/by-label",
        fallback_path=None,
        select_criteria="name",
        select_descending=False,
        min_free_gb=1,
        filename_kwargs=None,
        ):
    """brokkr.utils.output.get_output_drive's SIGNATURE, verbatim.

    The monitor reads brokkr's defaults out of this signature but must never
    *call* it: brokkr's implementation shells out to ``udisksctl mount``.
    """
    raise AssertionError(
        "check_drive_target called get_output_drive, which mounts drives")


def _load_real_brokkr_output():
    """Return the real brokkr.utils.output, or None if it cannot be loaded.

    Goes through `unavailable_reason`, which raises under
    HAMMA_REQUIRE_REAL_BROKKR=1 -- so CI cannot silently fall back to the
    stand-in and still report a green, byte-identical run.
    """
    if _real_brokkr.unavailable_reason() is not None:
        return None
    return _real_brokkr.modules()["brokkr.utils.output"]


REAL_BROKKR_OUTPUT = _load_real_brokkr_output()


def make_output_module():
    """The brokkr.utils.output the plugin will see (the real one if found)."""
    if REAL_BROKKR_OUTPUT is not None:
        return REAL_BROKKR_OUTPUT
    module = ModuleType("brokkr.utils.output")
    module.find_drives = _find_drives
    module.get_output_drive = _get_output_drive
    return module


def real_convert_path():
    """brokkr's own convert_path, which is what expands `~` in its paths.

    Must be the real function: the plugin routes every path it resolves by
    hand through it, and a MagicMock here would make the resolution untested
    exactly where the bug was.
    """
    if REAL_BROKKR_OUTPUT is not None:
        return _real_brokkr.modules()["brokkr.utils.misc"].convert_path
    return _convert_path


# --- Module loading ----------------------------------------------------------

class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep.

    Mirrors brokkr's `Executable.__init__` signature EXACTLY. That signature
    has no `**kwargs`, so an unconsumed config key is a TypeError at pipeline
    build that takes down the whole telemetry pipeline on the sensor -- a
    permissive stub here would swallow exactly the mistake this suite exists
    to catch.
    """

    def __init__(self, name="Unnamed", input_data=None, exit_event=None,
                 skip_na=False):
        self.name = name
        self.input_data = input_data
        self.exit_event = exit_event
        self.skip_na = skip_na
        self.logger = MagicMock()


OUTPUT_MODULE = make_output_module()


def load_state_monitor_module():
    """Load the plugin, with brokkr.utils.output wired to the real module."""
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep

    mock_pipeline = MagicMock()
    mock_pipeline.base = mock_base

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base
    # The plugin reaches brokkr.utils.output through attribute access, so the
    # real module has to be reachable that way, not just via sys.modules.
    mock_brokkr.utils.output = OUTPUT_MODULE
    # get_actual_username is stubbed because the test machine is not "pi";
    # convert_path is REAL, because path resolution is under test.
    mock_brokkr.utils.misc.get_actual_username.return_value = USER
    mock_brokkr.utils.misc.convert_path = real_convert_path()

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.pipeline.decode": MagicMock(),
        "brokkr.utils": mock_brokkr.utils,
        "brokkr.utils.output": OUTPUT_MODULE,
        "notifiers": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location(
            "state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module, mock_brokkr


MODULE, MOCK_BROKKR = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor


@contextmanager
def tuning(**overrides):
    """Temporarily override the check's module-level tuning constants."""
    saved = {}
    for name, value in overrides.items():
        saved[name] = getattr(MODULE, name)   # KeyError-free: must exist
        setattr(MODULE, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(MODULE, name, value)


@pytest.fixture
def prompt():
    """Alert on the first faulty cycle (a non-default damping window)."""
    with tuning(DRIVE_TARGET_CYCLES=1):
        yield


# --- The shipped configuration ----------------------------------------------

def shipped_config():
    with open(str(MAIN_TOML), "rb") as config_file:
        return toml_reader.load(config_file)


def config_state_monitor_kwargs():
    """The exact kwargs config/main.toml passes to the state_monitor step."""
    step = shipped_config()["steps"]["state_monitor"]
    return {key: value for key, value in step.items()
            if not key.startswith("_")}


def config_drive_kwargs():
    """The shipped science_binary_output.drive_kwargs."""
    step = shipped_config()["steps"]["science_binary_output"]
    return dict(step["drive_kwargs"])


def make_monitor(**overrides):
    """Build a StateMonitor through the REAL __init__ with the real config."""
    kwargs = config_state_monitor_kwargs()
    kwargs.update(overrides)
    return StateMonitor(**kwargs)


# --- Fake filesystem topology ------------------------------------------------

class FakeStatVFS:
    """os.statvfs result: free space, the read-only flag, realistic geometry.

    `f_bsize != f_frsize` and `f_bfree != f_bavail` on purpose -- they differ
    on a real ext4 volume (I/O block size, and the root reserve), and the
    implementation must use the same pair `shutil.disk_usage` does. The
    reserve is a realistic ~5% of 1.8 TB, not a token offset: at 512 KB an
    f_bavail -> f_bfree swap is undetectable, which is the whole point of
    setting them apart.
    """

    RESERVE_BYTES = int(0.05 * 1.8e12)      # ~90 GB, as ext4 actually reserves

    def __init__(self, free_bytes, readonly=False):
        self.f_frsize = 512                 # fragment size: the one that counts
        self.f_bsize = 4096                 # preferred I/O size: NOT the one
        self.f_blocks = int(1.8e12) // 512  # the fleet's 1.8 TB disks
        self.f_bavail = int(free_bytes) // 512
        self.f_bfree = self.f_bavail + self.RESERVE_BYTES // 512
        self.f_flag = os.ST_RDONLY if readonly else 0


class Tree:
    """A real temp directory tree standing in for a sensor's /media and /dev."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.media_base = tmp_path / "media"
        self.media = self.media_base / USER
        self.by_label = tmp_path / "dev" / "disk" / "by-label"
        # The SD-card fallback is NOT redirected by rewriting the config
        # value. `sensor()` points HOME here instead, so the shipped
        # `~/brokkr/{system_name}/science` template is the one under test --
        # including the `~` expansion, which is where the real bug was.
        self.home = tmp_path / "home" / USER
        self.fallback = self.home / "brokkr" / "hamma" / "science"
        # Stands in for /dev/shm. Redirected by `sensor()` so the suite never
        # reads or writes the real one, and so each test starts with no latch.
        self.state_file = tmp_path / "shm" / "drive_state.json"
        self.media.mkdir(parents=True)
        self.by_label.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.state_file.parent.mkdir(parents=True)
        self.mounts = set()      # paths os.path.ismount() will answer True for
        self.stats = {}          # path -> FakeStatVFS or OSError to raise

    # -- attached hardware ---------------------------------------------------
    def label(self, name):
        """A labelled partition under /dev/disk/by-label (a symlink, as udev)."""
        target = self.root / "dev" / ("sd" + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        (self.by_label / name).symlink_to(target)

    # -- what is mounted where -----------------------------------------------
    def mount(self, name, free_gib=500, readonly=False, idle_s=86400):
        """A mounted partition, last written to `idle_s` ago.

        Backdated on purpose. On a real unit the mountpoint's mtime is the
        mounted volume's root-directory mtime, which changes when brokkr
        creates an hour directory in it -- not when udisks made the mountpoint.
        Leaving it at "now" would make every freshly built fixture look like
        brokkr had just written, quietly defeating the evidence gate.
        """
        path = self.media / name
        path.mkdir()
        self.mounts.add(str(path))
        self.stats[str(path)] = FakeStatVFS(free_gib * GIB, readonly=readonly)
        self._backdate(path, idle_s)
        return path

    @staticmethod
    def _backdate(path, age_s):
        when = time.time() - age_s
        os.utime(str(path), (when, when))

    def set_free(self, name, free_bytes):
        self.stats[str(self.media / name)] = FakeStatVFS(free_bytes)

    def unmount(self, name):
        self.mounts.discard(str(self.media / name))

    def unreadable_mount(self, name, idle_s=86400):
        path = self.media / name
        path.mkdir()
        self.mounts.add(str(path))
        self.stats[str(path)] = OSError(5, "Input/output error")
        self._backdate(path, idle_s)
        return path

    def stale_dir(self, name, age_s=86400):
        """An empty leftover directory -- present, but not a mountpoint."""
        path = self.media / name
        path.mkdir()
        self._backdate(path, age_s)

    def stray_file(self, name):
        """A non-directory that matches brokkr's drive pattern."""
        (self.media / name).write_text("not a partition")

    # -- brokkr's science-write history --------------------------------------
    def science_write(self, path, age_s=0, hour="2026-08-09T12"):
        """Write a science file as brokkr would, `age_s` seconds ago.

        brokkr's output_path is `{drive_path}/{utc_date}T{utc_hour}`, so the
        file lands in a per-hour subdirectory. Both the hour directory and its
        parent are dated, because creating an hour directory is itself what
        bumps the parent's mtime on a real unit.
        """
        path = Path(path)
        hour_dir = path / hour
        hour_dir.mkdir(parents=True, exist_ok=True)
        (hour_dir / "hamma31_{}-00-00-000.bin".format(hour)).write_bytes(b"x")
        self._backdate(hour_dir, age_s)
        self._backdate(path, age_s)

    def wrote_to_sd(self, age_s=0, hour="2026-08-09T12"):
        self.science_write(self.fallback, age_s=age_s, hour=hour)

    def wrote_to(self, name, age_s=0, hour="2026-08-09T12"):
        self.science_write(self.media / name, age_s=age_s, hour=hour)

    #: Absolute, NOT derived from any constant in the module under test. An
    #: earlier version used `2 * MODULE.DRIVE_TARGET_EVIDENCE_S`, so shrinking
    #: that constant to 1 shrank "quiet" with it and the mutation survived.
    QUIET_AGE_S = 7 * 86400

    def go_quiet(self, age_s=None):
        """Age every science write into the past.

        What a lull between storms looks like: the fault is untouched, but
        nothing has been written since to prove brokkr tried.
        """
        if age_s is None:
            age_s = self.QUIET_AGE_S
        roots = [self.fallback] + list(self.media.iterdir())
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                self._backdate(child, age_s)
            self._backdate(root, age_s)

    # -- brokkr's settings for this tree -------------------------------------
    def drive_kwargs(self, **overrides):
        """The SHIPPED drive_kwargs, with exactly two path roots redirected.

        `base_path` and `mount_base_path` are rewritten to point at the temp
        tree, keeping `{current_user}` so the substitution stays exercised.
        Everything else -- `drive_glob`, `mount_glob`, `min_free_gb` and
        crucially `fallback_path` -- is whatever config/main.toml actually
        ships, so changing or deleting it there is visible here.

        `fallback_path` is deliberately NOT rewritten. An earlier version of
        this fixture substituted an absolute, pre-expanded path for the
        shipped `~/brokkr/{system_name}/science`, which quietly fixed the
        production bug inside the test: the plugin resolved the template with
        `.format()` alone, leaving a literal `~` that matched nothing, and the
        mj51 acceptance test passed anyway. Redirect HOME, never the value.
        """
        kwargs = config_drive_kwargs()
        kwargs["base_path"] = str(self.media_base) + "/{current_user}"
        kwargs["mount_base_path"] = str(self.by_label)
        kwargs.update(overrides)
        return kwargs


@contextmanager
def sensor(tree, drive_kwargs=None):
    """Present `tree` to the plugin as the sensor's real filesystem."""
    config = {"steps": {"science_binary_output": {
        "drive_kwargs": drive_kwargs if drive_kwargs is not None
        else tree.drive_kwargs()}}}

    def fake_ismount(path):
        return str(path) in tree.mounts

    def fake_statvfs(path):
        result = tree.stats.get(str(path))
        if result is None:
            raise OSError(2, "No such file or directory", str(path))
        if isinstance(result, OSError):
            raise result
        return result

    modules = {
        "brokkr": MOCK_BROKKR,
        "brokkr.utils": MOCK_BROKKR.utils,
        "brokkr.utils.misc": MOCK_BROKKR.utils.misc,
        "brokkr.utils.output": OUTPUT_MODULE,
        "brokkr.config": MagicMock(),
        "brokkr.config.main": MagicMock(CONFIG=config),
        "brokkr.config.metadata": MagicMock(METADATA={"name": "hamma"}),
        "brokkr.config.unit": MagicMock(
            UNIT_CONFIG={"number": 31, "site_description": "test"}),
        }
    with patch.dict("sys.modules", modules), \
            patch.dict(os.environ, {"HOME": str(tree.home)}), \
            patch.object(MODULE, "DEFAULT_DRIVE_STATE_FILE",
                         str(tree.state_file)), \
            patch("os.path.ismount", side_effect=fake_ismount), \
            patch("os.statvfs", side_effect=fake_statvfs):
        # brokkr's convert_path rewrites "~" to "~$SUDO_USER" before
        # expanding, which would resolve to the invoking user's real home
        # rather than HOME. patch.dict restores the whole environment on exit.
        os.environ.pop("SUDO_USER", None)
        yield


def run_cycles(monitor, count):
    """Run the check `count` times; return the list of messages produced."""
    return [monitor.check_drive_target(None) for _ in range(count)]


def named(alert):
    """The DATA partition names an alert mentions, prefix-safely."""
    return set(re.findall(r"DATA\d+", alert or ""))


# --- Topologies --------------------------------------------------------------

def mj51_tree(tmp_path):
    """sensor-log #52: stale DATA31/DATA42 dirs, real partitions at DATA*1.

    Structure only. The evidence -- brokkr falling back to the SD card -- has
    to post-date the topology, so tests add it with `falls_back` after the
    monitor has observed the tree at least once.
    """
    tree = Tree(tmp_path)
    tree.label("DATA31")
    tree.label("DATA42")
    tree.stale_dir("DATA31")        # empty, root:root, left by a dirty unmount
    tree.stale_dir("DATA42")
    tree.mount("DATA311", free_gib=500)   # udisks' suffixed mountpoints
    tree.mount("DATA421", free_gib=500)
    return tree


def falls_back(monitor, tree, settle_cycles=1):
    """Observe the topology, then have brokkr write to the SD card.

    Models what actually happens on a sensor: brokkr starts (or the
    mountpoints are rearranged), the monitor sees that arrangement, and only
    the NEXT lightning trigger produces evidence attributable to it. A fault
    that predates the monitor is confirmed by the next trigger, not by writes
    left over from before.
    """
    messages = run_cycles(monitor, settle_cycles)
    tree.wrote_to_sd(age_s=0)
    return messages


def healthy_tree(tmp_path, free_gib=500):
    tree = Tree(tmp_path)
    tree.label("DATA31")
    tree.label("DATA42")
    tree.mount("DATA31", free_gib=free_gib)
    tree.mount("DATA42", free_gib=free_gib)
    tree.wrote_to("DATA31")
    return tree


# --- The config/constructor contract ----------------------------------------

#: The ONLY keys `Tree.drive_kwargs()` is allowed to change. Everything else
#: must reach the plugin exactly as config/main.toml ships it.
#:
#: This is the rule that kept getting broken in a new place each round: first a
#: test-local `find_drives`, then `fallback_path` rewritten to a pre-expanded
#: absolute path (which hid a production bug the suite was named after), then
#: constants read back out of the module under test. Stating it in a docstring
#: did not stop it, so it is enforced here instead.
FIXTURE_MAY_OVERRIDE = frozenset(["base_path", "mount_base_path"])


class TestFixtureIntegrity:
    """The fixture must not substitute the values under test."""

    def test_fixture_overrides_only_the_path_roots(self, tmp_path):
        tree = Tree(tmp_path)
        shipped = config_drive_kwargs()
        fixture = tree.drive_kwargs()
        added = set(fixture) - set(shipped)
        assert added <= FIXTURE_MAY_OVERRIDE, (
            "the fixture added {}".format(sorted(added - FIXTURE_MAY_OVERRIDE)))
        assert not set(shipped) - set(fixture), "the fixture dropped keys"
        changed = {key for key in shipped if fixture[key] != shipped[key]}
        assert changed <= FIXTURE_MAY_OVERRIDE, (
            "{} differ from the shipped config; redirect HOME or the base "
            "paths, never the value under test".format(sorted(
                changed - FIXTURE_MAY_OVERRIDE)))

    def test_fallback_path_reaches_the_plugin_unmodified(self, tmp_path):
        """The specific substitution that hid the convert_path bug."""
        tree = Tree(tmp_path)
        assert (tree.drive_kwargs()["fallback_path"]
                == config_drive_kwargs()["fallback_path"])
        assert tree.drive_kwargs()["fallback_path"].startswith("~")

    def test_tuning_constants_hold_their_shipped_values(self):
        """Pin the values, not just the types.

        Mutating a constant downward otherwise survives, because fixtures that
        derive their own magnitudes from the module scale with the mutation.
        """
        assert MODULE.DRIVE_TARGET_CYCLES == 5
        assert MODULE.DRIVE_TARGET_RENOTIFY_S == 3600
        assert MODULE.DRIVE_TARGET_BLIND_CYCLES == 60
        assert MODULE.DRIVE_TARGET_CLOCK_SLACK_S == 60
        assert MODULE.DRIVE_TARGET_STATE_MAX_AGE_S == 3600
        assert MODULE.DEFAULT_DRIVE_STATE_FILE.startswith("/dev/shm/"), (
            "the latch must live on tmpfs; see the docstring for why the SD "
            "card is the wrong place")

    def test_quiet_age_is_not_derived_from_the_module(self):
        source = Path(__file__).read_text()
        quiet = source.split("QUIET_AGE_S = ", 1)[1].split("\n", 1)[0]
        assert "MODULE" not in quiet


class TestConfigContract:
    """A config key with no constructor parameter is a fleet-wide outage.

    brokkr's `Executable.__init__` has no `**kwargs`, so an unconsumed key in
    main.toml raises TypeError while the pipeline is being built and takes the
    whole telemetry pipeline down on every sensor.
    """

    def test_every_config_key_is_an_init_parameter(self):
        accepted = set(inspect.signature(StateMonitor.__init__).parameters)
        assert "output_step_kwargs" in accepted
        for key in config_state_monitor_kwargs():
            assert key in accepted, (
                "config/main.toml sets `{}` but StateMonitor.__init__ does not "
                "accept it -- this is a TypeError at pipeline build".format(key))

    def test_constructs_with_the_shipped_config(self):
        assert make_monitor() is not None

    def test_constructs_against_the_real_output_step(self):
        """The real base class, not a stub that might swallow stray kwargs."""
        module = _real_brokkr.load_plugin("state_monitor")
        monitor = module.StateMonitor(**config_state_monitor_kwargs())
        assert monitor._drive_target_count == 0

    def test_shipped_config_does_not_disable_drive_checks(self):
        """`enable_drive_checks = false` fleet-wide would silently mute this."""
        kwargs = config_state_monitor_kwargs()
        assert kwargs.get("enable_drive_checks", True) is True

    def test_shipped_science_output_is_resolvable(self):
        """Deleting drive_glob breaks brokkr AND disables this check."""
        drive_kwargs = config_drive_kwargs()
        assert drive_kwargs.get("drive_glob"), (
            "science_binary_output.drive_kwargs.drive_glob is missing from the "
            "shipped config; brokkr's science output would have no drives")
        assert drive_kwargs.get("mount_glob"), (
            "without mount_glob brokkr never mounts and this check has no "
            "second view to compare against")
        assert drive_kwargs.get("fallback_path"), (
            "without fallback_path there is no SD-card write to detect")
        assert drive_kwargs.get("min_free_gb") is not None

    def test_tuning_is_module_constants_not_config(self):
        """No new config key: it would be a reverse-path TypeError hazard."""
        keys = set(config_state_monitor_kwargs())
        assert not [key for key in keys if key.startswith("drive_target")]
        for name in ("DRIVE_TARGET_CYCLES", "DRIVE_TARGET_RENOTIFY_S",
                     "DRIVE_TARGET_BLIND_CYCLES",
                     "DRIVE_TARGET_CLOCK_SLACK_S",
                     "DRIVE_TARGET_STATE_MAX_AGE_S"):
            assert isinstance(getattr(MODULE, name), (int, float))

    def test_real_init_sets_up_the_latch_state(self):
        monitor = make_monitor()
        assert monitor._drive_target_count == 0
        assert monitor._drive_target_alerted is None
        assert monitor._drive_target_alerted_at is None
        assert monitor._drive_target_blind_count == 0

    def test_check_runs_under_the_shipped_config(self):
        """Not an override -- the value main.toml actually ships."""
        monitor = make_monitor()
        for name in ("check_drive", "check_drive_target", "check_pi_space",
                     "check_ping", "check_power", "check_battery_voltage",
                     "check_sensor_drive", "check_scrub_health"):
            setattr(monitor, name, MagicMock(return_value=None))
        monitor.run_checks({})
        assert monitor.check_drive_target.called

    def test_check_is_skipped_on_units_without_sensor_hardware(self):
        monitor = make_monitor(enable_drive_checks=False)
        for name in ("check_drive", "check_drive_target", "check_pi_space",
                     "check_ping", "check_power", "check_battery_voltage",
                     "check_sensor_drive", "check_scrub_health"):
            setattr(monitor, name, MagicMock(return_value=None))
        monitor.run_checks({})
        assert not monitor.check_drive_target.called


# --- Fidelity to brokkr ------------------------------------------------------

class TestBrokkrContract:
    """The monitor's view of "which drives" must be brokkr's, not a new rule."""

    def test_defaults_come_from_brokkrs_own_signature(self):
        """Provenance, not just the values."""
        monitor = make_monitor()
        defaults = monitor._output_drive_defaults()
        expected = {
            name: parameter.default
            for name, parameter in inspect.signature(
                OUTPUT_MODULE.get_output_drive).parameters.items()
            if parameter.default is not inspect.Parameter.empty}
        assert defaults == expected
        assert defaults["base_path"] == "/media/{current_user}"
        assert defaults["mount_base_path"] == "/dev/disk/by-label"

    @pytest.mark.skipif(REAL_BROKKR_OUTPUT is None,
                        reason="needs real brokkr to resolve the real paths")
    def test_production_config_shape_resolves_end_to_end(self, tmp_path):
        """The shipped config sets NEITHER base_path NOR mount_base_path.

        Production therefore gets both from `_output_drive_defaults()`. Every
        other behavioural test overrides them at the temp tree, so the method
        with the longest justification in the file had no end-to-end coverage
        -- `defaults = {}` was green. Here the shipped drive_kwargs is passed
        through untouched and the temp tree is reached by pointing
        `{current_user}` and `/dev/disk/by-label` at it instead.
        """
        shipped = config_drive_kwargs()
        assert "base_path" not in shipped and "mount_base_path" not in shipped

        # /media/<user> and /dev/disk/by-label, relocated under tmp_path by
        # patching only what brokkr itself would consult.
        media = tmp_path / "media"
        user_dir = media / USER
        by_label = tmp_path / "dev" / "disk" / "by-label"
        user_dir.mkdir(parents=True)
        by_label.mkdir(parents=True)
        tree = Tree(tmp_path / "unused")
        tree.media_base, tree.media, tree.by_label = media, user_dir, by_label
        tree.label("DATA31")
        tree.stale_dir("DATA31")
        tree.mount("DATA311", free_gib=500)
        tree.wrote_to_sd(age_s=0)

        real_convert = real_convert_path()

        def relocate(path):
            """Resolve as brokkr does, then re-root the two absolute prefixes."""
            resolved = str(real_convert(path))
            for original, replacement in (("/media", str(media)),
                                          ("/dev/disk/by-label",
                                           str(by_label))):
                if resolved.startswith(original):
                    return Path(replacement + resolved[len(original):])
            return Path(resolved)

        # brokkr's find_drives resolves paths through its OWN reference to
        # convert_path, so the real module has to be patched too -- patching
        # only the plugin's view would leave brokkr globbing the real /media.
        misc = _real_brokkr.modules()["brokkr.utils.misc"]
        monitor = make_monitor()
        with sensor(tree, shipped):
            with patch.object(misc, "convert_path", relocate), \
                    patch.object(MOCK_BROKKR.utils.misc, "convert_path",
                                 relocate):
                with tuning(DRIVE_TARGET_CYCLES=1):
                    falls_back(monitor, tree)
                    alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "DATA31" in named(alert)

    def test_no_glob_literal_in_the_plugin(self):
        source = PLUGIN_PATH.read_text()
        literals = set()
        for node in ast.walk(ast.parse(source)):
            # ast.Constant on 3.8+, ast.Str on the sensors' 3.7
            kind = type(node).__name__
            if kind not in ("Constant", "Str"):
                continue
            value = node.value if kind == "Constant" else node.s
            if isinstance(value, str) and "\n" not in value:
                literals.add(value)
        assert config_drive_kwargs()["drive_glob"] not in literals, (
            "the drive pattern must come from brokkr's config, not the plugin")

    def test_widening_the_glob_is_caught_behaviourally(self, tmp_path, prompt):
        """A future editor "improving" the check to see suffixed mounts.

        This is PR #84's exact mistake. A widened glob pulls the suffixed
        mountpoint into the set brokkr's writer is assumed to use, and the
        capacity half then measures a filesystem brokkr cannot write to:

            correct  -> "every DATA partition ... nowhere left to go"
            widened  -> "1 of 2 ... writing to the last one, DATA311, 900 GiB"

        A string blocklist over the source cannot catch `drive_glob += "*"`.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=0)        # the real partition, full
        tree.mount("DATA311", free_gib=900)     # a stray suffixed mount
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "min_free_gb" in alert and "ENOSPC" in alert
        assert named(alert) == {"DATA31"}, (
            "the check must measure only what brokkr's own glob resolves")
        assert "DATA311" not in alert

    def test_fallback_path_is_expanded_the_way_brokkr_expands_it(
            self, tmp_path):
        """The shipped fallback is `~`-relative; `.format()` alone leaves it.

        brokkr resolves it with `.format()` AND `convert_path`
        (render_output_filename, output.py:203). Resolving only the first half
        leaves a literal tilde that matches nothing on disk -- and since this
        path is the check's only evidence source, that made the mj51 case
        silent.
        """
        tree = mj51_tree(tmp_path)
        # Guard on the value actually handed to the plugin. Reading the
        # shipped config here instead let a fixture that overrode
        # fallback_path defeat this test while the guard still passed.
        assert tree.drive_kwargs()["fallback_path"].startswith("~"), (
            "this test is meaningless unless the plugin receives the "
            "~-relative template")
        monitor = make_monitor()
        with sensor(tree):
            view = monitor._brokkr_drive_view()
        resolved = str(view.fallback_path)
        assert "~" not in resolved
        assert resolved == str(tree.fallback)
        assert os.path.isabs(resolved)

    def test_base_path_is_expanded_the_way_brokkr_expands_it(self, tmp_path):
        """Same rule for the other path the plugin resolves by hand."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(
                base_path="~/media/{current_user}")):
            view = monitor._brokkr_drive_view()
        assert "~" not in str(view.base_path)
        assert str(view.base_path) == str(tree.home / "media" / USER)

    def test_only_one_place_in_the_plugin_resolves_a_path_template(self):
        """A standing red flag, not a one-off bug.

        brokkr renders a path template in two steps and the plugin's rule is
        "never restate brokkr's resolution, reuse it". `base_path` and
        `mount_base_path` get both steps for free by going through
        `find_drives`; `fallback_path` was the one path resolved by hand, and
        it was the one that was wrong. Every `.format(**kwargs)` on a path must
        now live in `_resolve_path`, which also applies `convert_path`.
        """
        offenders = []
        for node in ast.walk(ast.parse(PLUGIN_PATH.read_text())):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "format"
                        and any(keyword.arg is None
                                for keyword in inner.keywords)):
                    offenders.append(node.name)
        assert set(offenders) <= {"_resolve_path"}, (
            "{} resolve a path template by hand; route it through "
            "_resolve_path so brokkr's convert_path is applied".format(
                sorted(set(offenders) - {"_resolve_path"})))

    def test_uses_the_configured_glob_unchanged(self, tmp_path):
        tree = Tree(tmp_path)
        tree.label("XYZ99")
        tree.mount("XYZ99", free_gib=500)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(drive_glob="XYZ??")):
            view = monitor._brokkr_drive_view()
        assert [drive.name for drive in view.candidates] == ["XYZ99"]

    def test_get_output_drive_is_never_called(self, tmp_path, prompt):
        """It mounts drives as a side effect; the monitor is alert-only."""
        tripwire = MagicMock(side_effect=AssertionError(
            "get_output_drive was called -- it mounts drives"))
        # Keep the real signature so the defaults still introspect; only the
        # call is poisoned.
        tripwire.__signature__ = inspect.signature(
            OUTPUT_MODULE.get_output_drive)
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            with patch.object(OUTPUT_MODULE, "get_output_drive", tripwire):
                falls_back(monitor, tree)
                assert monitor.check_drive_target(None) is not None

    def test_require_real_brokkr_guard_raises(self, monkeypatch):
        """The CI guard was inert once and shipped that way (S06).

        Without this, re-breaking it is green and a CI run with no brokkr
        source is byte-identical to one with it.
        """
        monkeypatch.setitem(_real_brokkr._state, "loaded", False)
        monkeypatch.setitem(_real_brokkr._state, "reason", "simulated absence")
        monkeypatch.setenv("HAMMA_REQUIRE_REAL_BROKKR", "1")
        with pytest.raises(RuntimeError, match="simulated absence"):
            _real_brokkr.unavailable_reason()

    def test_require_real_brokkr_guard_is_opt_in(self, monkeypatch):
        monkeypatch.setitem(_real_brokkr._state, "loaded", False)
        monkeypatch.setitem(_real_brokkr._state, "reason", "simulated absence")
        monkeypatch.delenv("HAMMA_REQUIRE_REAL_BROKKR", raising=False)
        assert "simulated absence" in _real_brokkr.unavailable_reason()

    @pytest.mark.skipif(REAL_BROKKR_OUTPUT is None,
                        reason="no brokkr source available")
    def test_standin_matches_real_brokkr(self, tmp_path):
        """Pin the fallback stand-in to real brokkr while brokkr is loadable."""
        assert (inspect.signature(_get_output_drive)
                == inspect.signature(REAL_BROKKR_OUTPUT.get_output_drive))
        tree = mj51_tree(tmp_path)
        base = str(tree.media_base) + "/{current_user}"
        kwargs = {"current_user": USER}

        def fake_ismount(path):
            return str(path) in tree.mounts

        with patch("os.path.ismount", side_effect=fake_ismount):
            mine = _find_drives("DATA??", base, filename_kwargs=kwargs)
            theirs = REAL_BROKKR_OUTPUT.find_drives(
                "DATA??", base, filename_kwargs=kwargs)
        assert sorted(mine) == sorted(theirs)


# --- The premise: no automounter --------------------------------------------

class TestPreTriggerState:
    """"Labelled but not mounted" is ORDINARY, not a fault.

    brokkr is the only thing that mounts these partitions and it does so only
    when writing a science packet -- on a lightning trigger. udisks deletes the
    mountpoint directories at boot. Any check that pages on the structure alone
    pages every sensor on its next reboot, and mj43 (quiet since 2026-08-03)
    would page immediately.
    """

    def freshly_booted(self, tmp_path):
        """Labelled partitions, no mountpoints, no writes since boot."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        return tree

    def test_freshly_booted_unit_is_silent(self, tmp_path, prompt):
        tree = self.freshly_booted(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            assert run_cycles(monitor, 10) == [None] * 10

    def test_a_unit_quiet_for_days_is_silent(self, tmp_path, prompt):
        """mj43: last science packet 6.5 days ago, nothing mounted."""
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=6.5 * 86400)
        monitor = make_monitor()
        with sensor(tree):
            assert run_cycles(monitor, 10) == [None] * 10

    def test_stale_fallback_data_is_not_evidence(self, tmp_path, prompt):
        """mj03 holds 63 MB of fallback science data from 2025."""
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=365 * 86400)
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None

    def test_evidence_is_the_newest_write_not_mere_existence(
            self, tmp_path, prompt):
        """Old data present AND a newer write -> the fault is real."""
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=365 * 86400, hour="2025-08-11T03")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None
            tree.wrote_to_sd(age_s=0, hour="2026-08-09T12")
            assert monitor.check_drive_target(None) is not None

    def test_reboot_within_the_old_window_is_silent(self, tmp_path, prompt):
        """Operator reboots to clear a stale mountpoint (N1a).

        udisks removed the mountpoints; brokkr has not run at all yet. But the
        pre-reboot fallback writes are only 18 minutes old, so a bare
        "within the last hour" window opened the gate and the check announced
        that brokkr had tried and failed. Every clause of that was false.
        Evidence must post-date the CURRENT topology, and a restart makes the
        first observed topology current.
        """
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=18 * 60)
        monitor = make_monitor()
        with sensor(tree):
            assert run_cycles(monitor, 10) == [None] * 10

    def test_half_finished_manual_remedy_is_silent(self, tmp_path, prompt):
        """Operator has unmounted and rmdir'd, and is waiting for a trigger (N1b).

        The alert's own prescribed remedy, mid-flight. Unmounting changes the
        topology, so the writes from before it stop counting.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            # the remedy: unmount the suffixed mounts, rmdir the stale dirs
            for suffixed, stale in (("DATA311", "DATA31"),
                                    ("DATA421", "DATA42")):
                tree.unmount(suffixed)
                shutil.rmtree(str(tree.media / suffixed))
                shutil.rmtree(str(tree.media / stale))
            # Clear ALL the latch state, so the only thing that can keep the
            # check quiet is the evidence gate itself. Leaving
            # `_drive_target_alert_at` set let the renotify floor do the
            # silencing and the test passed without the gate working.
            before = monitor._drive_topology_since
            monitor._drive_target_alerted = None
            monitor._drive_target_count = 0
            monitor._drive_target_alert_at = None
            assert run_cycles(monitor, 10) == [None] * 10
            assert monitor._drive_topology_since > before, (
                "unmounting is a topology change and must restart the clock")

    def test_scrub_activity_is_not_evidence(self, tmp_path, prompt):
        """hamma_scrub --recover writes into the DATA partitions (N1c).

        It reproduces brokkr's own directory and filename convention there and
        mkstemps in the partition root, and THIS class spawns it every
        scrub_cooldown_s while the AGS drive is low -- so a check that accepted
        writes under /media/<user>/DATA* as evidence would manufacture, every
        five minutes, the proof it then consumed. Only the SD-card fallback
        counts, which the scrub never touches.
        """
        # The mj51 shape, where the scrub's unfiltered glob lands in the STALE
        # directory on the SD card -- and brokkr's writer has not run at all.
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            run_cycles(monitor, 1)              # establish the topology
            tree.wrote_to("DATA31")             # scrub recovers into the orphan
            tree.wrote_to("DATA42")
            assert run_cycles(monitor, 5) == [None] * 5

        # (The partial shape -- one partition working, one missing -- DOES
        # page, but on structural evidence the scrub cannot produce. See
        # TestDiagnosis::test_partial_loss_never_consults_the_mtime_gate.)

    def test_a_future_timestamp_is_not_evidence(self, tmp_path, prompt):
        """A clock we cannot trust proves nothing (N4).

        fake-hwclock steps this fleet's clocks backwards across a reboot, and
        FAT32 stores local time with a mount-time offset. Treating a negative
        age as "recent" turned a clock step on a quiet, freshly-booted unit
        into a full false alert.
        """
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=-6 * 3600)       # six hours in the future
        monitor = make_monitor()
        with sensor(tree):
            with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                messages = run_cycles(monitor, 4)
        assert all("drive target problem" not in (m or "") for m in messages)
        assert messages[2] is not None
        assert "clock cannot be trusted" in messages[2]


# --- The acceptance test -----------------------------------------------------

class TestMj51Topology:
    """sensor-log #52 on mj51: eight days of science data onto the SD card."""

    def test_widened_glob_would_have_reported_healthy(self, tmp_path):
        """Why PR #84's approach cannot work, stated as an assertion."""
        tree = mj51_tree(tmp_path)
        base = str(tree.media_base) + "/{current_user}"
        kwargs = {"current_user": USER}

        def fake_ismount(path):
            return str(path) in tree.mounts

        with patch("os.path.ismount", side_effect=fake_ismount):
            widened = OUTPUT_MODULE.find_drives(
                "DATA??" + "*", base, filename_kwargs=kwargs)
            brokkrs = OUTPUT_MODULE.find_drives(
                "DATA??", base, filename_kwargs=kwargs)
        assert sorted(d.name for d in widened) == ["DATA311", "DATA421"]
        assert brokkrs == [], "brokkr's own glob sees no drive -> SD fallback"

    def test_mj51_topology_alerts(self, tmp_path):
        """ACCEPTANCE: the shipped tuning must alert on this topology."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()          # DRIVE_TARGET_CYCLES = 5, as shipped
        with sensor(tree):
            falls_back(monitor, tree)
            messages = run_cycles(monitor, 5)
        assert messages[:4] == [None] * 4          # damping
        alert = messages[4]
        assert alert is not None
        assert named(alert) == {"DATA31", "DATA42", "DATA311", "DATA421"}
        assert "tried and failed to mount them" in alert
        assert "going to the SD card" in alert

    def test_alert_names_every_hidden_partition(self, tmp_path, prompt):
        """Reporting only the first offender must not pass."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            alert = monitor.check_drive_target(None)
        assert {"DATA31", "DATA42"} <= named(alert)

    def test_alert_gives_the_remedy_and_not_the_glob(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            alert = monitor.check_drive_target(None)
        assert config_drive_kwargs()["drive_glob"] not in alert
        assert "rmdir the leftover empty directory" in alert
        # The alert must carry the remedy for THIS fault and nothing else.
        # It used to append a blanket "the auto-scrub cannot help with any of
        # this" caveat to every drive-target alert. Operators already know the
        # scrub is AGS-side, so it was noise on a message that has to be
        # scannable -- and it was wrong to attach it to non-capacity faults
        # like this one at all.
        assert "auto-scrub" not in alert
        assert "only deletes on the AGS" not in alert

    def test_healthy_tree_is_silent(self, tmp_path, prompt):
        tree = healthy_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            assert run_cycles(monitor, 3) == [None, None, None]

    def test_no_labelled_partitions_is_not_this_checks_problem(
            self, tmp_path, prompt):
        """`check_drive` already alerts for that; do not double-page."""
        tree = Tree(tmp_path)
        tree.wrote_to_sd()
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None


# --- Diagnosis accuracy ------------------------------------------------------

class TestDiagnosis:
    """The alert must not assert things that are not true of this topology."""

    def test_a_partial_loss_pages(self, tmp_path, prompt):
        """One partition hidden while another still works.

        Without this the unit runs on half its storage until the survivor
        fills and the operator finds out then -- by which point they have an
        emergency AND the original fault. (Formerly "~120 days at the measured
        14.75 GiB/day"; that is mj03's rate, and mj08 runs 6x faster.)
        Caught here the fix is still remount + rmdir the stale directory.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)      # brokkr still has somewhere
        tree.stale_dir("DATA42")
        tree.mount("DATA421", free_gib=500)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "DATA42" in named(alert)
        assert "brokkr is not using them" in alert
        # It must not read as an emergency: data is still landing.
        assert "NO DATA IS BEING LOST" in alert
        assert "going to the SD card" not in alert
        assert "rmdir the leftover empty directory" in alert

    def test_partial_loss_never_consults_the_mtime_gate(self, tmp_path, prompt):
        """Its evidence is structural: a mounted candidate proves the mounter ran.

        `mount_drives` mounts EVERY labelled drive it does not already see
        mounted, and nothing else on these units mounts them, so no timestamp
        is needed -- and none may be consulted, or the scrub could suppress
        this the way it could have faked the total-loss case.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.stale_dir("DATA42")
        tree.mount("DATA421", free_gib=500)
        monitor = make_monitor()
        tripwire = MagicMock(side_effect=AssertionError(
            "the partial branch consulted the mtime evidence gate"))
        with sensor(tree):
            with patch.object(StateMonitor, "_fell_back_to_sd_since", tripwire):
                alert = monitor.check_drive_target(None)
        assert alert is not None and "DATA42" in named(alert)
        assert not tripwire.called

    def test_partial_loss_pages_with_no_write_history_at_all(
            self, tmp_path, prompt):
        """A unit that has never written anything still reports this."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.stale_dir("DATA42")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is not None

    def test_total_and_partial_read_differently(self, tmp_path, prompt):
        """Total loss is an emergency; partial loss explicitly is not."""
        total = mj51_tree(tmp_path / "total")
        monitor = make_monitor()
        with sensor(total):
            falls_back(monitor, total)
            total_alert = monitor.check_drive_target(None)
        partial = Tree(tmp_path / "partial")
        partial.label("DATA31")
        partial.label("DATA42")
        partial.mount("DATA31", free_gib=500)
        partial.stale_dir("DATA42")
        monitor = make_monitor()
        with sensor(partial):
            partial_alert = monitor.check_drive_target(None)
        assert "going to the SD card" in total_alert
        assert "NO DATA IS BEING LOST" not in total_alert
        assert "NO DATA IS BEING LOST" in partial_alert
        assert "going to the SD card" not in partial_alert

    def test_the_sd_card_claim_is_only_made_when_it_is_true(
            self, tmp_path, prompt):
        """`hidden` implies brokkr had no candidate, so the claim always holds."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            alert = monitor.check_drive_target(None)
        assert "going to the SD card" in alert
        assert str(tree.fallback) in alert

    def test_rmdir_remedy_only_when_a_stray_mount_exists(
            self, tmp_path, prompt):
        """No suffixed mount -> a different fault and a different remedy."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            alert = monitor.check_drive_target(None)
        assert alert is not None
        # (substring, not "rmdir" -- pytest's tmp_path is named after the test)
        assert "rmdir the leftover empty directory" not in alert
        assert "nothing at all is mounted for them" in alert
        assert "udisksctl status" in alert


# --- Faults other than the hidden mount --------------------------------------

class TestNonDirectoryMatches:
    """brokkr's ismount filter screens directories ONLY.

    `not drive.is_dir() or os.path.ismount(drive)` keeps every non-directory
    match unconditionally, and statvfs on a plain file reports the containing
    filesystem. A single stray file silently disarmed PR #84.
    """

    def test_stray_file_is_reported_not_silently_kept(self, tmp_path, prompt):
        tree = healthy_tree(tmp_path)
        tree.stray_file("DATA55")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "DATA55" in alert
        assert "are not directories" in alert

    def test_stray_file_cannot_stand_in_for_its_label(self, tmp_path, prompt):
        """A file named DATA55 must not satisfy the DATA55 label.

        Total-loss shape, so the hidden clause is in play: if the stray file
        counted as a mounted partition, DATA55 would look satisfied and the
        unit would read as healthy while brokkr wrote to the SD card.
        """
        tree = Tree(tmp_path)
        tree.label("DATA55")
        tree.stray_file("DATA55")
        monitor = make_monitor()
        # Two cycles, so the settle cycle (which already sees the stray file)
        # does not consume the page before the hidden clause appears.
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=2):
            falls_back(monitor, tree)
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "tried and failed to mount them" in alert
        assert "are not directories" in alert


class TestUnusableMounts:

    def test_readonly_mount_alerts(self, tmp_path, prompt):
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=500, readonly=True)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "READ-ONLY" in alert and "DATA31" in alert

    def test_a_readonly_candidate_is_not_named_as_where_data_lands(
            self, tmp_path, prompt):
        """The reassurance must name only partitions brokkr can WRITE to.

        `candidate_dirs` is appended before the ST_RDONLY test, so it holds
        read-only mounts; `usable` does not. Naming the former would print
        "science data is still landing on DATA80" in the same alert whose
        read-only clause says writes to DATA80 fail.
        """
        tree = Tree(tmp_path)
        for name in ("DATA80", "DATA81", "DATA82"):
            tree.label(name)
        tree.mount("DATA80", free_gib=1465, readonly=True)
        tree.mount("DATA81", free_gib=500)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "NO DATA IS BEING LOST" in alert
        landing = alert.split("still landing on ")[1]
        assert "DATA81" in landing
        assert "DATA80" not in landing.split(".")[0], (
            "named a read-only partition as a place data is landing")

    def test_no_writable_candidate_retracts_the_reassurance(
            self, tmp_path, prompt):
        """mj54's topology with the writable partition gone.

        One labelled partition is unmounted (so the mounter demonstrably ran
        and failed) and the only mounted candidate is read-only. brokkr
        selects on free space alone, so it picks the read-only one, fails the
        write and falls back to the SD card. Data IS being lost, and the
        alert must not say otherwise.
        """
        tree = Tree(tmp_path)
        tree.label("DATA80")
        tree.label("DATA82")
        tree.mount("DATA80", free_gib=1465, readonly=True)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "DATA82" in alert, "the unused partition is still reported"
        assert "NO DATA IS BEING LOST" not in alert, (
            "claimed no loss while nothing writable remained")
        assert "SD card" in alert

    def test_min_free_gb_zero_is_a_setting_not_an_absence(
            self, tmp_path, prompt):
        """0 means "no capacity floor", not "unset".

        brokkr's `select_drive` has no reserved sentinel for "disabled", so 0
        is a legitimate value distinct from a missing key. A falsy test read
        it as absent and silently skipped the whole check.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=500, readonly=True)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(min_free_gb=0)):
            alert = monitor.check_drive_target(None)
        assert alert is not None, "min_free_gb=0 disabled the entire check"
        assert "READ-ONLY" in alert and "DATA31" in alert

    def test_unreadable_mount_alerts_and_logs(self, tmp_path, prompt):
        """PR #84's `except OSError: continue` had no logger call at all."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.unreadable_mount("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "Input/output error" in alert
        assert monitor.logger.warning.called

    def test_candidate_enumeration_failure_evaluates_nothing(
            self, tmp_path, prompt):
        """Seed the counter first, so "unchanged" is distinguishable from 0."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            with tuning(DRIVE_TARGET_CYCLES=99):
                falls_back(monitor, tree)
                run_cycles(monitor, 3)
                assert monitor._drive_target_count == 3
                with patch.object(OUTPUT_MODULE, "find_drives",
                                  side_effect=OSError(13, "Permission denied")):
                    assert monitor.check_drive_target(None) is None
                assert monitor._drive_target_count == 3
        assert monitor.logger.warning.called

    def test_incomplete_settings_are_logged_not_raised(self, tmp_path, prompt):
        """Every key the check depends on must be guarded, not just the first.

        Uses a tree with real mounted candidates on purpose: with none, the
        capacity arithmetic never runs, so an unguarded `min_free_gb=None`
        never gets multiplied and the test passes without the guard existing.
        """
        tree = healthy_tree(tmp_path)
        monitor = make_monitor()
        for broken in ("drive_glob", "base_path", "min_free_gb"):
            with sensor(tree, tree.drive_kwargs(**{broken: None})):
                assert monitor.check_drive_target(None) is None, (
                    "{}=None should disable the check, not evaluate".format(
                        broken))
        assert monitor.logger.warning.called


# --- Blind: the check reporting its own failure ------------------------------

class TestBlindWatchdog:
    """Five paths return None after logging. A monitor whose own failures go
    only to the log is a monitor nobody hears."""

    def test_label_enumeration_failure_is_not_read_as_healthy(
            self, tmp_path, prompt):
        """"Could not look" must never become "nothing is hidden".

        Verified against the mj51 topology: with /dev/disk/by-label
        unreadable, the previous version reported the unit healthy.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        real_find = OUTPUT_MODULE.find_drives

        def only_labels_fail(drive_glob, base_path, filename_kwargs=None):
            if str(base_path) == str(tree.by_label):
                raise OSError(13, "Permission denied")
            return real_find(drive_glob, base_path,
                             filename_kwargs=filename_kwargs)

        with sensor(tree):
            with patch.object(OUTPUT_MODULE, "find_drives", only_labels_fail):
                with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                    messages = run_cycles(monitor, 4)
        assert messages[:2] == [None, None]
        assert messages[2] is not None
        assert "not been able to evaluate" in messages[2]
        assert "labelled DATA partitions could not be enumerated" in messages[2]
        assert messages[3] is None      # said once

    def test_settings_failure_pages_once_when_chronic(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(drive_glob=None)):
            with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                messages = run_cycles(monitor, 6)
        assert [m is not None for m in messages] == [
            False, False, True, False, False, False]

    def test_missing_mount_glob_is_blind_not_silent(self, tmp_path):
        """A per-unit drive_kwargs without mount_glob (N5).

        There is then no second view to compare against, so the check cannot
        do its job -- but it used to take the healthy path and report nothing
        for 70 cycles on a live mj51 fault, with blind_count at 0.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(mount_glob=False)):
            with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                tree.wrote_to_sd(age_s=0)
                messages = run_cycles(monitor, 4)
        assert messages[2] is not None
        assert "not configured to mount by label" in messages[2]

    def test_missing_fallback_path_is_blind_not_silent(self, tmp_path):
        """Same for fallback_path: no evidence source means blind (N5)."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(fallback_path=None)):
            with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                tree.wrote_to_sd(age_s=0)
                messages = run_cycles(monitor, 4)
        assert messages[2] is not None
        assert "no fallback_path configured" in messages[2]

    def test_unreadable_by_label_directory_is_blind_not_empty(self, tmp_path):
        """Path.glob returns [] for chmod 000 as well as for missing (N6).

        So an unreadable /dev/disk/by-label read as "no labelled partitions"
        -- observation, not blindness -- which is the None-vs-[] contract this
        file claims to keep.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        mode = tree.by_label.stat().st_mode
        os.chmod(str(tree.by_label), 0o000)
        try:
            if os.access(str(tree.by_label), os.R_OK):
                pytest.skip("running as root; permissions are not enforced")
            with sensor(tree):
                with tuning(DRIVE_TARGET_BLIND_CYCLES=3):
                    messages = run_cycles(monitor, 4)
        finally:
            os.chmod(str(tree.by_label), mode)
        assert messages[2] is not None
        assert "cannot be read" in messages[2]

    def test_recovery_rearms_the_blind_watchdog(self, tmp_path, prompt):
        tree = healthy_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(drive_glob=None)):
            with tuning(DRIVE_TARGET_BLIND_CYCLES=2):
                assert run_cycles(monitor, 2)[1] is not None
        with sensor(tree):
            assert monitor.check_drive_target(None) is None
            assert monitor._drive_target_blind_count == 0
            assert monitor._drive_target_blind_alerted is False

    def test_a_blind_cycle_does_not_clear_an_outstanding_fault(
            self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            latched = monitor._drive_target_alerted
            with patch.object(OUTPUT_MODULE, "find_drives",
                              side_effect=OSError(13, "nope")):
                run_cycles(monitor, 3)
            assert monitor._drive_target_alerted == latched


# --- Damping and the latch ---------------------------------------------------

class TestDampingAndLatch:
    """PR #84 had no damping; the first version of this check had damping that
    a changing fault set could reset forever."""

    @pytest.mark.parametrize("cycles", [2, 7])
    def test_the_damping_window_is_live(self, tmp_path, cycles):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=cycles):
            falls_back(monitor, tree)
            messages = run_cycles(monitor, cycles + 1)
        assert messages[:cycles - 1] == [None] * (cycles - 1)
        assert messages[cycles - 1] is not None
        assert messages[cycles] is None      # latched after the first page

    def test_a_flapping_fault_still_pages(self, tmp_path):
        """A fault that keeps changing shape must not silence the counter.

        Measured on the previous version: two partitions alternating produced
        0 alerts over 40 consecutive faulty cycles, because every cycle reset
        the damping count.
        """
        tree = self.flaky_enclosure(tmp_path)
        monitor = make_monitor()
        messages = []
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=5):
            for cycle in range(40):
                self.flap(tree, cycle)
                messages.append(monitor.check_drive_target(None))
        assert any(message is not None for message in messages), (
            "40 consecutive faulty cycles produced no alert")

    @staticmethod
    def flaky_enclosure(tmp_path):
        """Two labelled partitions, neither mounted, brokkr on the SD card."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        return tree

    @staticmethod
    def flap(tree, cycle):
        """One label drops off the bus on odd cycles; brokkr keeps writing.

        Both states are genuinely faulty -- brokkr has no partition either way
        -- but the fault SET alternates, which is what the damping counter and
        the renotify floor have to cope with together.
        """
        link = tree.by_label / "DATA42"
        if cycle % 6 == 0 and link.is_symlink():
            link.unlink()
        elif cycle % 6 == 3 and not link.is_symlink():
            link.symlink_to(tree.root / "dev" / "sdDATA42")
        # brokkr keeps falling back to the SD card throughout. On the cycle a
        # label moves, that write pre-dates the new topology and cannot
        # confirm anything -- which must freeze the machine, not reset it.
        tree.wrote_to_sd(age_s=0,
                         hour="2026-08-09T{:02d}".format(cycle % 24))

    def test_a_flapping_fault_does_not_storm(self, tmp_path):
        """Re-arming on a changed fault set must not page every cycle."""
        tree = self.flaky_enclosure(tmp_path)
        monitor = make_monitor()
        messages = []
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=2,
                                  DRIVE_TARGET_RENOTIFY_S=3600):
            for cycle in range(60):
                self.flap(tree, cycle)
                messages.append(monitor.check_drive_target(None))
        pages = sum(message is not None for message in messages)
        assert pages == 1, "60 flapping cycles produced {} pages".format(pages)

    def test_a_persistent_fault_pages_once(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            messages = run_cycles(monitor, 30)
        assert sum(message is not None for message in messages) == 1

    def test_latch_rearms_for_a_new_fault_after_the_renotify_floor(
            self, tmp_path):
        """A partition swap onto a second broken partition must page again."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.stale_dir("DATA31")
        tree.mount("DATA311", free_gib=500)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1,
                                  DRIVE_TARGET_RENOTIFY_S=3600):
            falls_back(monitor, tree)
            first = monitor.check_drive_target(None)
            assert first is not None and named(first) == {"DATA31", "DATA311"}
            shutil.rmtree(str(tree.media / "DATA31"))
            (tree.by_label / "DATA31").unlink()
            tree.unmount("DATA311")
            tree.label("DATA42")
            tree.stale_dir("DATA42")
            tree.mount("DATA421", free_gib=500)
            falls_back(monitor, tree)
            assert run_cycles(monitor, 4) == [None] * 4, (
                "a different fault inside the floor must stay quiet")
            # the floor expires (wall clock, so age the recorded page)
            monitor._drive_target_alerted_at -= 3601
            late = monitor.check_drive_target(None)
        assert late is not None
        assert "DATA42" in named(late)

    def test_latch_clears_and_rearms_after_recovery(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            # operator fixes it: rmdir the orphans, remount properly
            for name in ("DATA311", "DATA421"):
                tree.unmount(name)
                shutil.rmtree(str(tree.media / name))
            for name in ("DATA31", "DATA42"):
                tree.mounts.add(str(tree.media / name))
                tree.stats[str(tree.media / name)] = FakeStatVFS(500 * GIB)
            assert monitor.check_drive_target(None) is None
            assert monitor._drive_target_alerted is None
            assert monitor._drive_target_alerted_at is None
            # and it breaks again -- a new topology, so a fresh fallback
            # write is needed before it can be confirmed
            for name in ("DATA31", "DATA42"):
                tree.unmount(name)
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None

    def test_quiet_gaps_are_not_recovery(self, tmp_path):
        """One never-fixed fault, four storms: exactly one page.

        The evidence gate empties `hidden` whenever brokkr goes quiet. If that
        runs the healthy branch it clears the latch AND `_drive_target_alert_at`
        -- and the renotify floor is guarded on `alert_at is not None`, so the
        storm protection is bypassed too. Measured before the fix: 4 pages for
        4 bursts, one per storm, for a fault that never cleared.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        pages = []
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=2):
            run_cycles(monitor, 1)      # establish the topology
            for burst in range(4):
                tree.wrote_to_sd(age_s=0,
                                 hour="2026-08-09T{:02d}".format(10 + burst))
                pages += [m for m in run_cycles(monitor, 5) if m]
                assert monitor._drive_target_alerted is not None, (
                    "the latch must survive a storm")
                tree.go_quiet()
                pages += [m for m in run_cycles(monitor, 5) if m]
                assert monitor._drive_target_alerted is not None, (
                    "a lull is not recovery -- the fault is still there")
        assert len(pages) == 1, (
            "one never-fixed fault paged {} times".format(len(pages)))

    def test_a_lull_does_not_reset_the_renotify_floor(self, tmp_path):
        """A *changed* fault after a lull is still subject to the floor."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1,
                                  DRIVE_TARGET_RENOTIFY_S=3600):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            tree.go_quiet()
            run_cycles(monitor, 5)
            # the fault changes shape, and brokkr writes again
            tree.label("DATA53")
            tree.wrote_to_sd(age_s=0, hour="2026-08-09T13")
            assert run_cycles(monitor, 5) == [None] * 5

    def test_real_recovery_during_a_lull_still_clears(self, tmp_path):
        """The freeze must not outlive the fault."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            tree.go_quiet()
            for name in ("DATA311", "DATA421"):
                tree.unmount(name)
                shutil.rmtree(str(tree.media / name))
            for name in ("DATA31", "DATA42"):
                tree.mounts.add(str(tree.media / name))
                tree.stats[str(tree.media / name)] = FakeStatVFS(500 * GIB)
            assert monitor.check_drive_target(None) is None
            assert monitor._drive_target_alerted is None
            assert monitor._drive_target_alerted_at is None

    def test_a_blind_label_cycle_does_not_clear_an_outstanding_fault(
            self, tmp_path):
        """Same defect via the other suppression path."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        real_find = OUTPUT_MODULE.find_drives

        def only_labels_fail(drive_glob, base_path, filename_kwargs=None):
            if str(base_path) == str(tree.by_label):
                raise OSError(13, "Permission denied")
            return real_find(drive_glob, base_path,
                             filename_kwargs=filename_kwargs)

        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            latched = monitor._drive_target_alerted
            with patch.object(OUTPUT_MODULE, "find_drives", only_labels_fail):
                run_cycles(monitor, 3)
            assert monitor._drive_target_alerted == latched

    def test_label_flapping_does_not_storm(self, tmp_path):
        """THE INVARIANT, on the branch that was still missing it.

        A flaky USB enclosure resetting on the bus makes the by-label symlink
        disappear for a cycle. `missing` then empties, the fault set empties,
        and a state machine that reads that as health resets the latch AND the
        floor. Measured before the invariant restructure: 11 pages in 66
        cycles against a floor that intends <=1/hour. A vanished label has not
        been proven fixed -- it has stopped being observable.
        """
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        # EVERY label drops, so the fault genuinely stops being observable --
        # `missing` empties and the fault set with it. Dropping only one of
        # two leaves the fault visible and does not exercise the invariant.
        links = {name: (tree.by_label / name, tree.root / "dev" / ("sd" + name))
                 for name in ("DATA31", "DATA42")}
        pages = []
        # Three, not five: the runs of confirmable cycles between drops are
        # four long, so a five-cycle damping window would never page at all
        # and `<= 1` would pass without the invariant doing anything.
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=3):
            for cycle in range(66):
                for link, target in links.values():
                    if cycle % 6 == 5:
                        if link.is_symlink():
                            link.unlink()               # enclosure resets
                    elif not link.is_symlink():
                        link.symlink_to(target)         # and comes back
                tree.wrote_to_sd(age_s=0,
                                 hour="2026-08-09T{:02d}".format(cycle % 24))
                message = monitor.check_drive_target(None)
                if message:
                    pages.append(message)
        assert len(pages) == 1, (
            "label flapping produced {} pages".format(len(pages)))

    def test_a_flap_shorter_than_the_window_never_pages(self, tmp_path):
        tree = healthy_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=5):
            tree.unmount("DATA31")
            assert run_cycles(monitor, 3) == [None, None, None]
            tree.mounts.add(str(tree.media / "DATA31"))
            assert run_cycles(monitor, 3) == [None, None, None]
            assert monitor._drive_target_count == 0


# --- Capacity ----------------------------------------------------------------

class TestCapacity:
    """The actionable event is the partition transition, not a fixed floor.

    On 1.8 TB disks a fixed 25 GiB floor gives one short warning very late.
    "The first partition is full, the unit is on its last one" arrives well
    before that, and "full" is brokkr's own `min_free_gb`, so no new threshold
    is introduced.

    How much earlier is NOT months, and this docstring used to say it was, on
    mj03's measured 14.75 GiB/day. mj08 measured 91 GiB/day over 2.68 days and
    ranged 36-218 GiB/day within that window; sensor-log #111 predicted its
    transition at ~21 days and it arrived the next day. Do not restate a
    fleet-wide rate here.
    """

    def test_two_roomy_partitions_are_silent(self, tmp_path, prompt):
        tree = healthy_tree(tmp_path, free_gib=900)
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None

    def test_last_partition_transition_alerts(self, tmp_path, prompt):
        tree = healthy_tree(tmp_path, free_gib=900)
        tree.set_free("DATA31", 0)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "1 of 2 DATA partitions are full" in alert
        assert "writing to the last one, DATA42" in alert
        assert "auto-scrub" not in alert
        # GOLDEN STRING, not substrings.
        #
        # Earlier versions of this test asserted things like `"empty" not in
        # alert` and `"the full disk" not in alert`. Those pin the literal
        # phrasing of one prior draft, not the defect. Verified by mutation:
        # rewording the remedy to "delete the old data to reclaim the space"
        # (the deletion instruction sensor-log #98 warns against) or to
        # "replace the full drive before this one fills" (the two-disk claim
        # that is wrong -- DATA69 and DATA70 are one physical disk) BOTH
        # passed 109/109 against those substring assertions.
        #
        # Pinning the whole remedy verbatim is the only form that survives a
        # synonym. Any reword fails here, which is the point: the wording is
        # operator-approved and changing it should require deliberately
        # updating this line.
        # ENDSWITH, not `in`. `in` pins a prefix and lets anything be appended
        # -- verified: adding " at your convenience next week" to the remedy
        # passed a golden `in` assertion while destroying the urgency it was
        # written to protect. This is the only capacity fault in this fixture,
        # so the reason is the tail of the message.
        assert alert.endswith(
            "brokkr is now writing to the last one, DATA42, with "
            "900.0 GiB free. File a Jira ticket to schedule a drive swap.")

    def test_composed_alert_with_a_second_fault_reads_correctly(
            self, tmp_path, prompt):
        """Read a WHOLE multi-fault alert, not a fragment of a single one.

        Every capacity-wording defect found across three attempts survived a
        green suite because nothing here ever composed two reasons and read
        the result. `_evaluate_drive_target` joins reasons with "; " into one
        message, so a remedy that reads fine alone can contradict or collide
        with its neighbour.

        This case is specifically the collision that broke an earlier version
        of these tests: the stray-mount remedy contains the word "empty"
        ("rmdir the leftover empty directory"), so an `assert "empty" not in
        alert` written to guard the capacity remedy fails here -- on a real,
        reachable topology -- while passing on the single-fault fixture it was
        written against.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.label("DATA43")
        tree.stale_dir("DATA31")            # forces the suffixed mountpoint
        tree.mount("DATA311", free_gib=500)  # brokkr cannot see this one
        tree.mount("DATA42", free_gib=0)     # full
        tree.mount("DATA43", free_gib=900)   # the survivor
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None

        # Both faults are present, each with its own remedy, joined by "; ".
        assert "rmdir the leftover empty directory" in alert
        assert ("brokkr is now writing to the last one, DATA43, with "
                "900.0 GiB free. File a Jira ticket to schedule a "
                "drive swap") in alert

        # The composed message must still be well-formed: one leading stem,
        # one terminating period, no doubled punctuation from the join.
        assert alert.startswith("Science drive target problem -- ")
        assert alert.endswith(".")
        assert ".." not in alert
        assert "; ;" not in alert

        # The emergency register belongs to `all_full` alone. A unit that
        # still has a writable partition must not be told data is being lost.
        assert "SCIENCE DATA IS BEING LOST NOW" not in alert

    def test_full_uses_brokkrs_decimal_min_free_gb(self, tmp_path, prompt):
        """min_free_gb is decimal GB (min_free_gb * 1e9), as select_drive uses.

        0.1 GB is 1.00e8 bytes decimal but 1.07e8 binary, so 1.02e8 discriminates.
        """
        assert config_drive_kwargs()["min_free_gb"] == 0.1
        tree = healthy_tree(tmp_path, free_gib=900)
        monitor = make_monitor()
        with sensor(tree):
            tree.set_free("DATA31", 1.02e8)     # above decimal, below binary
            assert monitor.check_drive_target(None) is None
            tree.set_free("DATA31", 0.98e8)     # below both
            assert monitor.check_drive_target(None) is not None

    def test_all_partitions_full_alerts(self, tmp_path, prompt):
        """With >1 candidate, brokkr's select_drive really does refuse."""
        tree = healthy_tree(tmp_path, free_gib=0)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "All drives full!" in alert
        assert "ENOSPC" not in alert
        assert {"DATA31", "DATA42"} <= named(alert)
        # The `all_full` branch is a SEPARATE reason string from
        # `lastpartition`, so pinning wording on only one of them leaves the
        # other free to regress. A mutation reverting just this branch passed
        # 109/109 before these lines existed.
        assert "auto-scrub" not in alert
        # GOLDEN STRING -- see the note in test_last_partition_transition_alerts.
        # Asserted from the outcome clause onward rather than from the start of
        # the reason, because `usable` is not sorted, so the partition-name list
        # ahead of it can render in either order.
        #
        # This branch is ACTIVE LOSS, not a scheduling problem, and it pages
        # once and then latches -- the wording is the only urgency signal there
        # is. It must not converge on the lastpartition text.
        # ENDSWITH, not `in` -- see the note in the lastpartition test.
        assert alert.endswith(
            "brokkr's own drive selection now fails outright "
            "(RuntimeError: All drives full!). SCIENCE DATA IS BEING LOST "
            "NOW -- free space or attach a drive today, and file a Jira "
            "ticket.")
        assert "schedule a drive swap" not in alert

    def test_a_single_full_partition_says_enospc_not_refusal(
            self, tmp_path, prompt):
        """brokkr applies min_free_gb only inside select_drive, and
        get_output_drive calls it only when len(canidate_drives) > 1. With one
        candidate it returns it unchecked and the write fails at ENOSPC."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=0)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert "ENOSPC" in alert
        assert "All drives full!" not in alert

    def test_single_full_partition_alerts(self, tmp_path, prompt):
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=0)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is not None

    def test_single_partition_units_get_no_early_warning(
            self, tmp_path, prompt):
        """A documented gap, asserted so it stays visible.

        `last_partition` needs two usable partitions. A one-partition unit gets
        only brokkr's 100 MB floor, which at mj03's 14.75 GiB/day is ~9
        minutes -- and proportionally less on faster units.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=1)      # 1 GiB left: hours, not months
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None

    def test_a_readonly_partition_is_not_offered_as_capacity(
            self, tmp_path, prompt):
        """The live mj54 shape, verified in the field 2026-08-09.

            DATA80  1465.1 GiB free, mounted ro, FAT-fs cluster-chain errors
            DATA81  writable, and the only partition brokkr can actually use

        Counting DATA80's free space made it `remaining`, so when DATA81 fills
        the alert offered DATA80 as "the last one, with 1465.1 GiB free" and
        ~100 days of runway -- for a partition brokkr cannot write one byte
        to, contradicting the readonly clause in the same message.
        """
        tree = Tree(tmp_path)
        tree.label("DATA80")
        tree.label("DATA81")
        tree.mount("DATA80", free_gib=1465, readonly=True)
        tree.mount("DATA81", free_gib=0)
        tree.wrote_to("DATA81")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "READ-ONLY" in alert and "DATA80" in alert
        assert "1465" not in alert, "must not quote a read-only partition's free space"
        assert "last one" not in alert, "must not offer it as somewhere to write"
        assert "min_free_gb" in alert, "the true state is: nothing writable left"
        assert named(alert) == {"DATA80", "DATA81"}

    def test_capacity_is_reported_alongside_another_fault(
            self, tmp_path, prompt):
        """Never suppressed by a co-occurring fault: it is the emergency.

        (The co-occurring fault here is a non-directory match rather than a
        hidden partition, because `hidden` is by construction only reported
        when brokkr has no candidate at all, and then there is no capacity to
        report. The rule under test -- report both -- is the same.)
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=0)       # the only one brokkr can use
        tree.stray_file("DATA55")
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "are not directories" in alert                # the other fault
        assert "min_free_gb" in alert and "ENOSPC" in alert   # the emergency

    def test_last_partition_is_reported_with_another_fault(
            self, tmp_path, prompt):
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=0)
        tree.mount("DATA42", free_gib=900)
        tree.stray_file("DATA53")
        tree.wrote_to("DATA42")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert "1 of 2 DATA partitions are full" in alert
        assert "DATA53" in named(alert)


# --- Surviving a brokkr restart ----------------------------------------------

class TestLatchPersistence:
    """The latch outlives a brokkr restart -- and fails OPEN whenever it cannot.

    A missing latch repeats a page; a wrong latch suppresses one. This check
    exists because a suppressed page cost eight days of science data, so every
    doubt about the stored note resolves to "page".
    """

    def restarted(self, tree, first=None):
        """Page a fault, then hand the tree to a brand-new StateMonitor."""
        monitor = first if first is not None else make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
        return make_monitor()

    def latched_tree(self, tmp_path):
        return mj51_tree(tmp_path)

    def test_the_latch_survives_a_restart(self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            reborn = self.restarted(tree)
            falls_back(reborn, tree)
            assert run_cycles(reborn, 5) == [None] * 5, (
                "a restart must not re-page a fault already reported")
            assert reborn._drive_target_alerted is not None

    def test_a_restart_still_pages_a_different_fault(self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1,
                                  DRIVE_TARGET_RENOTIFY_S=0):
            reborn = self.restarted(tree)
            tree.label("DATA53")           # the fault set changes
            falls_back(reborn, tree)
            assert reborn.check_drive_target(None) is not None

    def test_a_restart_pages_again_once_the_fault_clears_and_returns(
            self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            reborn = self.restarted(tree)
            for name in ("DATA311", "DATA421"):
                tree.unmount(name)
                shutil.rmtree(str(tree.media / name))
            for name in ("DATA31", "DATA42"):
                tree.mounts.add(str(tree.media / name))
                tree.stats[str(tree.media / name)] = FakeStatVFS(500 * GIB)
            assert reborn.check_drive_target(None) is None      # CLEAR
            assert not tree.state_file.exists(), (
                "a cleared fault must not leave a note behind")

    # --- fail-open paths, one test each ----------------------------------

    def assert_pages_after_restart(self, tree, corrupt):
        """Page a fault, damage the stored note, and require a fresh page."""
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            reborn = self.restarted(tree)
            corrupt(tree.state_file)
            falls_back(reborn, tree)
            assert reborn.check_drive_target(None) is not None, (
                "a latch that cannot be trusted must fail OPEN")

    def test_absent_state_pages(self, tmp_path):
        self.assert_pages_after_restart(
            self.latched_tree(tmp_path), lambda path: path.unlink())

    def test_malformed_json_pages(self, tmp_path):
        self.assert_pages_after_restart(
            self.latched_tree(tmp_path),
            lambda path: path.write_text("{not json"))

    def test_non_object_state_pages(self, tmp_path):
        self.assert_pages_after_restart(
            self.latched_tree(tmp_path), lambda path: path.write_text("[1,2]"))

    def test_unreadable_state_pages_and_says_why(self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            reborn = self.restarted(tree)
            os.chmod(str(tree.state_file), 0o000)
            if os.access(str(tree.state_file), os.R_OK):
                pytest.skip("running as root; permissions are not enforced")
            falls_back(reborn, tree)
            assert reborn.check_drive_target(None) is not None
        logged = " ".join(str(c) for c in reborn.logger.info.call_args_list)
        assert "not restoring the drive-target latch" in logged, (
            "failing open silently is how a broken store stays broken")

    def test_wrong_schema_version_pages(self, tmp_path):
        def bump(path):
            stored = json.loads(path.read_text())
            stored["version"] = MODULE.DRIVE_TARGET_STATE_VERSION + 1
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), bump)

    def test_another_units_state_pages(self, tmp_path):
        def reassign(path):
            stored = json.loads(path.read_text())
            stored["identity"] = ["hamma", 99]
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), reassign)

    def test_malformed_signature_pages(self, tmp_path):
        def mangle(path):
            stored = json.loads(path.read_text())
            stored["signature"] = [{"not": "a string"}]
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), mangle)

    def test_malformed_timestamps_page(self, tmp_path):
        def mangle(path):
            stored = json.loads(path.read_text())
            stored["alerted_at"] = "yesterday"
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), mangle)

    def test_stale_state_pages(self, tmp_path):
        """Bounded staleness -- do not lean on the tmpfs wipe as the expiry."""
        def age(path):
            stored = json.loads(path.read_text())
            stored["last_seen"] -= MODULE.DRIVE_TARGET_STATE_MAX_AGE_S + 60
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), age)

    def test_state_from_the_future_pages(self, tmp_path):
        """fake-hwclock steps these clocks; a future note is not trustworthy."""
        def skew(path):
            stored = json.loads(path.read_text())
            # Inside the staleness bound on purpose: a larger offset would be
            # rejected as stale and the sign check would go untested.
            stored["last_seen"] += MODULE.DRIVE_TARGET_STATE_MAX_AGE_S / 6
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), skew)

    def test_alerted_in_the_future_pages(self, tmp_path):
        def skew(path):
            stored = json.loads(path.read_text())
            stored["alerted_at"] += 6 * 3600
            path.write_text(json.dumps(stored))
        self.assert_pages_after_restart(self.latched_tree(tmp_path), skew)

    # --- the store must not become a failure mode -------------------------

    def test_an_unwritable_store_costs_only_the_suppression(
            self, tmp_path, prompt):
        """A full or read-only /dev/shm must not break the check."""
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            with patch("builtins.open", side_effect=OSError(28, "No space")):
                falls_back(monitor, tree)
                assert monitor.check_drive_target(None) is not None
        assert monitor.logger.warning.called

    def test_a_broken_store_never_raises_into_the_loop(self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            with patch.object(MODULE.os, "replace",
                              side_effect=OSError(30, "Read-only")):
                falls_back(monitor, tree)
                assert monitor.check_drive_target(None) is not None
                assert run_cycles(monitor, 3) == [None] * 3

    def test_the_note_is_one_small_file_that_cannot_grow(
            self, tmp_path, prompt):
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            monitor.check_drive_target(None)
            first = tree.state_file.stat().st_size
            run_cycles(monitor, 20)
            # Bounded, not byte-identical: `last_seen` is refreshed each cycle
            # and its float repr varies by a couple of characters. What
            # matters is that 20 cycles do not accumulate anything.
            assert abs(tree.state_file.stat().st_size - first) < 16
            assert first < 2048
            assert sorted(path.name for path in tree.state_file.parent.iterdir()
                          ) == [tree.state_file.name], "no leftover temp files"

    def test_a_vanished_partition_does_not_clear_a_partial_latch(
            self, tmp_path, prompt):
        """THE INVARIANT applies to the partial fault too.

        If the missing partition's label drops off the bus, the fault has
        stopped being observable, not been repaired.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.stale_dir("DATA42")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is not None
            latched = monitor._drive_target_alerted
            (tree.by_label / "DATA42").unlink()          # enclosure resets
            assert run_cycles(monitor, 3) == [None] * 3
            assert monitor._drive_target_alerted == latched

    def test_an_unverifiable_identity_pages(self, tmp_path, prompt):
        """Cannot check whose note it is => cannot trust it => page."""
        tree = self.latched_tree(tmp_path)
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            reborn = self.restarted(tree)
            with patch.object(StateMonitor, "_drive_state_identity",
                              staticmethod(lambda: None)):
                falls_back(reborn, tree)
                assert reborn.check_drive_target(None) is not None

    def test_a_failed_write_leaves_the_previous_note_intact(
            self, tmp_path, prompt):
        """Atomicity: a note is never half-written.

        A direct write would truncate the good note on the way to failing;
        the temp-file-and-replace keeps it, so a crash mid-save costs nothing.
        """
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            good = tree.state_file.read_text()
            with patch.object(MODULE.json, "dump",
                              side_effect=OSError(28, "No space")):
                run_cycles(monitor, 3)
            assert tree.state_file.read_text() == good

    def test_a_long_lived_fault_keeps_its_note_fresh(self, tmp_path, prompt):
        """`last_seen` is refreshed every cycle, not frozen at the alert.

        Otherwise a fault paged hours ago has a note older than the staleness
        bound, and the restart it exists to cover re-pages anyway.
        """
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            # the fault was first reported well beyond the staleness bound
            monitor._drive_target_alerted_at -= (
                2 * MODULE.DRIVE_TARGET_STATE_MAX_AGE_S)
            monitor.check_drive_target(None)
            reborn = make_monitor()
            falls_back(reborn, tree)
            assert run_cycles(reborn, 3) == [None] * 3, (
                "the note should still be fresh enough to trust")

    def test_a_backwards_clock_step_does_not_suppress(self, tmp_path, prompt):
        """The floor must fail open when elapsed time is negative."""
        tree = self.latched_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1,
                                  DRIVE_TARGET_RENOTIFY_S=3600):
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None
            # fake-hwclock steps the clock back: the page now reads as future
            monitor._drive_target_alerted_at += 600
            tree.label("DATA53")               # a different fault set
            falls_back(monitor, tree)
            assert monitor.check_drive_target(None) is not None

    def test_the_shipped_location_is_tmpfs(self):
        """Moving this to the SD card trades a repeat for a suppression."""
        assert MODULE.DEFAULT_DRIVE_STATE_FILE.startswith("/dev/shm/")
        assert MODULE.DEFAULT_SCRUB_STATUS_FILE.startswith("/dev/shm/"), (
            "one storage idiom, not two")


# --- Alert-only --------------------------------------------------------------

class TestAlertOnly:
    """HAM-185: repair belongs in HAM-173's boot-time oneshot, not here."""

    def test_nothing_is_mounted_removed_or_spawned(self, tmp_path, prompt):
        """Alert-only, judged against the SENSOR's paths.

        Not "os.remove is never called": the check removes its own latch note
        on tmpfs when a fault clears, which is neither a mount nor sensor data.
        The rule is that nothing under /media, /dev/disk/by-label or the
        science output is touched, and that nothing is spawned at all.
        """
        tree = mj51_tree(tmp_path)
        protected = (str(tree.media_base), str(tree.by_label),
                     str(tree.fallback))
        monitor = make_monitor()
        with sensor(tree):
            with patch("subprocess.Popen") as popen, \
                    patch("subprocess.run") as run, \
                    patch("os.rmdir") as rmdir, \
                    patch("os.remove") as remove, \
                    patch("os.unlink") as unlink, \
                    patch("shutil.rmtree") as rmtree:
                falls_back(monitor, tree)
                assert monitor.check_drive_target(None) is not None
                # clearing the fault is what exercises the removal path
                for name in ("DATA311", "DATA421"):
                    tree.unmount(name)
                for name in ("DATA31", "DATA42"):
                    tree.mounts.add(str(tree.media / name))
                    tree.stats[str(tree.media / name)] = FakeStatVFS(500 * GIB)
                assert monitor.check_drive_target(None) is None
        for mock in (popen, run):
            assert not mock.called, "the check must never spawn anything"
        for mock in (rmdir, remove, unlink, rmtree):
            for call in mock.call_args_list:
                target = str(call[0][0]) if call[0] else ""
                assert not any(target.startswith(path) for path in protected), (
                    "the check touched {}".format(target))

    def test_the_tree_is_unchanged(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        before = sorted(path.name for path in tree.media.iterdir())
        monitor = make_monitor()
        with sensor(tree):
            falls_back(monitor, tree)
            run_cycles(monitor, 3)
        assert sorted(path.name for path in tree.media.iterdir()) == before
