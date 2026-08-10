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
    on a real ext4 volume (I/O block size, and the 5% root reserve), and the
    implementation must use the same pair `shutil.disk_usage` does.
    """

    def __init__(self, free_bytes, readonly=False):
        self.f_frsize = 512                 # fragment size: the one that counts
        self.f_bsize = 4096                 # preferred I/O size: NOT the one
        self.f_blocks = int(1.8e12) // 512  # the fleet's 1.8 TB disks
        self.f_bavail = int(free_bytes) // 512
        self.f_bfree = self.f_bavail + 1000  # root reserve
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
        self.media.mkdir(parents=True)
        self.by_label.mkdir(parents=True)
        self.home.mkdir(parents=True)
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

    def go_quiet(self, age_s=None):
        """Age every science write out of the evidence window.

        What a lull between storms looks like: the fault is untouched, but
        nothing has been written recently enough to prove brokkr tried.
        """
        if age_s is None:
            age_s = 2 * MODULE.DRIVE_TARGET_EVIDENCE_S
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

def mj51_tree(tmp_path, sd_write_age_s=0):
    """sensor-log #52: stale DATA31/DATA42 dirs, real partitions at DATA*1.

    With `sd_write_age_s` seconds since brokkr last wrote to the SD-card
    fallback -- the evidence that brokkr tried to mount and failed.
    """
    tree = Tree(tmp_path)
    tree.label("DATA31")
    tree.label("DATA42")
    tree.stale_dir("DATA31")        # empty, root:root, left by a dirty unmount
    tree.stale_dir("DATA42")
    tree.mount("DATA311", free_gib=500)   # udisks' suffixed mountpoints
    tree.mount("DATA421", free_gib=500)
    if sd_write_age_s is not None:
        tree.wrote_to_sd(age_s=sd_write_age_s)
    return tree


def healthy_tree(tmp_path, free_gib=500):
    tree = Tree(tmp_path)
    tree.label("DATA31")
    tree.label("DATA42")
    tree.mount("DATA31", free_gib=free_gib)
    tree.mount("DATA42", free_gib=free_gib)
    tree.wrote_to("DATA31")
    return tree


# --- The config/constructor contract ----------------------------------------

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
        for name in ("DRIVE_TARGET_CYCLES", "DRIVE_TARGET_RENOTIFY_CYCLES",
                     "DRIVE_TARGET_BLIND_CYCLES", "DRIVE_TARGET_EVIDENCE_S"):
            assert isinstance(getattr(MODULE, name), (int, float))

    def test_real_init_sets_up_the_latch_state(self):
        monitor = make_monitor()
        assert monitor._drive_target_count == 0
        assert monitor._drive_target_alerted is None
        assert monitor._drive_target_alert_at is None
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
        path is the check's evidence that brokkr wrote to the SD card, and the
        only evidence source in the mj51 topology, that made the total-loss
        case silent while the partial case still alerted.
        """
        assert config_drive_kwargs()["fallback_path"].startswith("~"), (
            "this test is meaningless unless the shipped value is ~-relative")
        tree = mj51_tree(tmp_path)
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
                assert monitor.check_drive_target(None) is not None

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

    def test_a_write_to_a_working_partition_is_also_evidence(
            self, tmp_path, prompt):
        """One partition mounted, one hidden: brokkr's mounter demonstrably ran."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.stale_dir("DATA42")
        tree.mount("DATA421", free_gib=500)
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None   # no writes yet
            tree.wrote_to("DATA31")
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert named(alert) == {"DATA42", "DATA421", "DATA31"}

    def test_the_evidence_window_is_the_module_constant(self, tmp_path, prompt):
        tree = self.freshly_booted(tmp_path)
        tree.wrote_to_sd(age_s=1800)          # 30 min
        monitor = make_monitor()
        with sensor(tree):
            with tuning(DRIVE_TARGET_EVIDENCE_S=600):
                assert monitor.check_drive_target(None) is None
            with tuning(DRIVE_TARGET_EVIDENCE_S=7200):
                assert monitor.check_drive_target(None) is not None


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
            alert = monitor.check_drive_target(None)
        assert {"DATA31", "DATA42"} <= named(alert)

    def test_alert_gives_the_remedy_and_not_the_glob(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert config_drive_kwargs()["drive_glob"] not in alert
        assert "rmdir the leftover empty directory" in alert
        assert "only deletes on the AGS" in alert

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

    def test_sd_card_claim_only_when_brokkr_has_no_candidate(
            self, tmp_path, prompt):
        """brokkr falls back only on `not canidate_drives`."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)      # brokkr still has somewhere
        tree.stale_dir("DATA42")
        tree.mount("DATA421", free_gib=500)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert "SD card" not in alert
        assert "data is not being lost yet" in alert

    def test_rmdir_remedy_only_when_a_stray_mount_exists(
            self, tmp_path, prompt):
        """No suffixed mount -> a different fault and a different remedy."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.wrote_to_sd()
        monitor = make_monitor()
        with sensor(tree):
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
        """A file named DATA55 must not satisfy the DATA55 label."""
        tree = healthy_tree(tmp_path)
        tree.label("DATA55")
        tree.stray_file("DATA55")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "tried and failed to mount them" in alert


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
                run_cycles(monitor, 3)
                assert monitor._drive_target_count == 3
                with patch.object(OUTPUT_MODULE, "find_drives",
                                  side_effect=OSError(13, "Permission denied")):
                    assert monitor.check_drive_target(None) is None
                assert monitor._drive_target_count == 3
        assert monitor.logger.warning.called

    def test_incomplete_settings_are_logged_not_raised(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        for broken in ("drive_glob", "min_free_gb"):
            with sensor(tree, tree.drive_kwargs(**{broken: None})):
                assert monitor.check_drive_target(None) is None
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
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.mount("DATA42", free_gib=500)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        messages = []
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=5):
            for cycle in range(40):
                # a flaky enclosure: exactly one partition visible, alternating
                visible = "DATA42" if cycle % 2 else "DATA31"
                tree.unmount("DATA31" if cycle % 2 else "DATA42")
                tree.mounts.add(str(tree.media / visible))
                # brokkr keeps writing throughout -- that is how we know the
                # missing partition is a fault and not a quiet unit.
                tree.wrote_to(visible)
                messages.append(monitor.check_drive_target(None))
        assert any(message is not None for message in messages), (
            "40 consecutive faulty cycles produced no alert")

    def test_a_flapping_fault_does_not_storm(self, tmp_path):
        """Re-arming on a changed fault set must not page every cycle."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=500)
        tree.mount("DATA42", free_gib=500)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        messages = []
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=2,
                                  DRIVE_TARGET_RENOTIFY_CYCLES=30):
            for cycle in range(60):
                visible = "DATA42" if cycle % 2 else "DATA31"
                tree.unmount("DATA31" if cycle % 2 else "DATA42")
                tree.mounts.add(str(tree.media / visible))
                # brokkr keeps writing throughout -- that is how we know the
                # missing partition is a fault and not a quiet unit.
                tree.wrote_to(visible)
                messages.append(monitor.check_drive_target(None))
        pages = sum(message is not None for message in messages)
        assert 1 <= pages <= 3, "60 flapping cycles produced {} pages".format(
            pages)

    def test_a_persistent_fault_pages_once(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            messages = run_cycles(monitor, 30)
        assert sum(message is not None for message in messages) == 1

    def test_latch_rearms_for_a_new_fault_after_the_renotify_floor(
            self, tmp_path):
        """A partition swap onto a second broken partition must page again."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.stale_dir("DATA31")
        tree.mount("DATA311", free_gib=500)
        tree.wrote_to_sd()
        monitor = make_monitor()
        with sensor(tree), tuning(DRIVE_TARGET_CYCLES=1,
                                  DRIVE_TARGET_RENOTIFY_CYCLES=3):
            first = monitor.check_drive_target(None)
            assert first is not None and named(first) == {"DATA31", "DATA311"}
            shutil.rmtree(str(tree.media / "DATA31"))
            (tree.by_label / "DATA31").unlink()
            tree.unmount("DATA311")
            tree.label("DATA42")
            tree.stale_dir("DATA42")
            tree.mount("DATA421", free_gib=500)
            tree.wrote_to_sd()
            messages = run_cycles(monitor, 4)
        assert messages[:2] == [None, None]      # inside the renotify floor
        assert messages[2] is not None
        assert "DATA42" in named(messages[2])

    def test_latch_clears_and_rearms_after_recovery(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
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
            assert monitor._drive_target_alert_at is None
            # and it breaks again
            for name in ("DATA31", "DATA42"):
                tree.unmount(name)
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
                                  DRIVE_TARGET_RENOTIFY_CYCLES=30):
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
            assert monitor._drive_target_alert_at is None

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
            assert monitor.check_drive_target(None) is not None
            latched = monitor._drive_target_alerted
            with patch.object(OUTPUT_MODULE, "find_drives", only_labels_fail):
                run_cycles(monitor, 3)
            assert monitor._drive_target_alerted == latched

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

    1.8 TB disks at mj03's measured 14.75 GiB/day: a 25 GiB floor is silent for
    ~226 days and then gives 41 hours of warning, once. "The first partition is
    full, the unit is on its last one" arrives months earlier, and "full" is
    brokkr's own `min_free_gb`, so no new threshold is introduced.
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
        assert "900.0 GiB free" in alert

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
        only brokkr's 100 MB floor, which at 14.75 GiB/day is ~9 minutes.
        """
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.mount("DATA31", free_gib=1)      # 1 GiB left: hours, not months
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            assert monitor.check_drive_target(None) is None

    def test_capacity_is_reported_alongside_a_hidden_partition(
            self, tmp_path, prompt):
        """Both are true and the capacity one is the emergency."""
        tree = Tree(tmp_path)
        tree.label("DATA31")
        tree.label("DATA42")
        tree.mount("DATA31", free_gib=0)       # the only one brokkr can use
        tree.stale_dir("DATA42")
        tree.mount("DATA421", free_gib=900)
        tree.wrote_to("DATA31")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "tried and failed to mount them" in alert    # the hidden one
        assert "min_free_gb" in alert and "ENOSPC" in alert  # the emergency

    def test_last_partition_is_reported_with_a_hidden_third(
            self, tmp_path, prompt):
        tree = Tree(tmp_path)
        for name in ("DATA31", "DATA42", "DATA53"):
            tree.label(name)
        tree.mount("DATA31", free_gib=0)
        tree.mount("DATA42", free_gib=900)
        tree.stale_dir("DATA53")
        tree.wrote_to("DATA42")
        monitor = make_monitor()
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert "1 of 2 DATA partitions are full" in alert
        assert "DATA53" in named(alert)


# --- Alert-only --------------------------------------------------------------

class TestAlertOnly:
    """HAM-185: repair belongs in HAM-173's boot-time oneshot, not here."""

    def test_nothing_is_mounted_removed_or_spawned(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            with patch("subprocess.Popen") as popen, \
                    patch("subprocess.run") as run, \
                    patch("os.rmdir") as rmdir, \
                    patch("os.remove") as remove, \
                    patch("os.unlink") as unlink, \
                    patch("shutil.rmtree") as rmtree:
                assert monitor.check_drive_target(None) is not None
        for mock in (popen, run, rmdir, remove, unlink, rmtree):
            assert not mock.called

    def test_the_tree_is_unchanged(self, tmp_path, prompt):
        tree = mj51_tree(tmp_path)
        before = sorted(path.name for path in tree.media.iterdir())
        monitor = make_monitor()
        with sensor(tree):
            run_cycles(monitor, 3)
        assert sorted(path.name for path in tree.media.iterdir()) == before
