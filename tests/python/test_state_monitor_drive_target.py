"""Tests for check_drive_target -- brokkr's science write target (HAM-185).

The acceptance test is `TestMj51Topology::test_mj51_topology_alerts`: the
literal sensor-log #52 shape (stale `/media/pi/DATA07` directory, real drive
mounted at `DATA071`) must produce an alert. PR #84 reported that topology as
healthy, so a suite that does not assert this is not testing the requirement.

Two deliberate choices, both reactions to the review of PR #84:

* Every behavioural test builds its StateMonitor through the **real
  `__init__`** with the exact kwargs `config/main.toml` sets, so a config key
  without a matching constructor parameter -- a `TypeError` at pipeline build
  that takes down the whole telemetry pipeline -- fails the suite here rather
  than on the fleet.
* Drive discovery runs against a **real temporary directory tree** through the
  implementation's own code path. `brokkr.utils.output` is the real module when
  brokkr is importable; otherwise it is a stand-in whose `find_drives` is
  brokkr's source verbatim and whose `get_output_drive` carries brokkr's exact
  signature with a tripwire body. `TestBrokkrContract` pins the stand-in to
  real brokkr whenever brokkr is available.
"""

import ast
import importlib.util
import inspect
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

try:
    import tomllib as toml_reader
except ImportError:  # Python < 3.11 (the sensors are on 3.7)
    import tomli as toml_reader


REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"
MAIN_TOML = REPO_ROOT / "config" / "main.toml"

USER = "pi"
GIB = 2 ** 30


# --- brokkr stand-ins --------------------------------------------------------
#
# Used only when brokkr is not importable. `find_drives` is copied from
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

    The body is a tripwire. The monitor reads brokkr's defaults out of this
    signature, but must never *call* it: brokkr's implementation shells out to
    ``udisksctl mount``, and the monitor is alert-only (HAM-185).
    """
    raise AssertionError(
        "check_drive_target called get_output_drive, which mounts drives")


def _load_real_brokkr_output():
    """Return the real brokkr.utils.output, or None if brokkr is unavailable.

    A bare ``import brokkr`` cannot work here: brokkr is not installed, and
    importing it writes default TOMLs into ``~/.config`` and resolves its
    config relative to the CWD.  ``_real_brokkr`` locates the sibling checkout
    and imports it inside a sandboxed HOME with the system path pinned.
    """
    try:
        from . import _real_brokkr
    except ImportError:
        import _real_brokkr
    try:
        _real_brokkr._load()
        if not _real_brokkr._state["loaded"]:
            return None
        return _real_brokkr._state["modules"]["brokkr.utils.output"]
    except Exception:      # noqa: BLE001 - never let harness trouble fail collection
        return None


REAL_BROKKR_OUTPUT = _load_real_brokkr_output()


def make_output_module():
    """The brokkr.utils.output the plugin will see (real one if installed)."""
    if REAL_BROKKR_OUTPUT is not None:
        return REAL_BROKKR_OUTPUT
    module = ModuleType("brokkr.utils.output")
    module.find_drives = _find_drives
    module.get_output_drive = _get_output_drive
    return module


# --- Module loading ----------------------------------------------------------

class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep."""

    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


OUTPUT_MODULE = make_output_module()


def load_state_monitor_module():
    """Load the plugin, with brokkr.utils.output wired to the real/stand-in."""
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep

    mock_pipeline = MagicMock()
    mock_pipeline.base = mock_base

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base
    # The plugin reaches brokkr.utils.output through attribute access, so the
    # real/stand-in module has to be reachable that way, not just via
    # sys.modules.
    mock_brokkr.utils.output = OUTPUT_MODULE
    mock_brokkr.utils.misc.get_actual_username.return_value = USER

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


# --- Config-driven construction ---------------------------------------------

def config_state_monitor_kwargs():
    """The exact kwargs config/main.toml passes to the state_monitor step."""
    with open(str(MAIN_TOML), "rb") as config_file:
        config = toml_reader.load(config_file)
    step = config["steps"]["state_monitor"]
    return {key: value for key, value in step.items()
            if not key.startswith("_")}


def make_monitor(**overrides):
    """Build a StateMonitor through the REAL __init__ with the real config.

    PR #84 used `StateMonitor.__new__` everywhere, so its suite could not see
    that its new config key had no constructor parameter -- a fleet-wide
    pipeline crash. Everything here goes through the real constructor.
    """
    kwargs = config_state_monitor_kwargs()
    kwargs.update(overrides)
    return StateMonitor(**kwargs)


# --- Fake filesystem topology ------------------------------------------------

class FakeStatVFS:
    """os.statvfs result carrying free space and the read-only flag."""

    def __init__(self, free_bytes, readonly=False, frsize=4096):
        self.f_frsize = frsize
        self.f_bsize = frsize
        self.f_blocks = int(1.8e12) // frsize      # the fleet's 1.8 TB drives
        self.f_bavail = int(free_bytes) // frsize
        self.f_bfree = self.f_bavail
        self.f_flag = MODULE.ST_RDONLY if readonly else 0


class Tree:
    """A real temp directory tree standing in for a sensor's /media and /dev."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.media_base = tmp_path / "media"
        self.media = self.media_base / USER
        self.by_label = tmp_path / "dev" / "disk" / "by-label"
        self.media.mkdir(parents=True)
        self.by_label.mkdir(parents=True)
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
    def mount(self, name, free_gib=500, readonly=False):
        path = self.media / name
        path.mkdir()
        self.mounts.add(str(path))
        self.stats[str(path)] = FakeStatVFS(free_gib * GIB, readonly=readonly)
        return path

    def unreadable_mount(self, name):
        path = self.media / name
        path.mkdir()
        self.mounts.add(str(path))
        self.stats[str(path)] = OSError(5, "Input/output error")
        return path

    def stale_dir(self, name):
        """An empty leftover directory -- present, but not a mountpoint."""
        (self.media / name).mkdir()

    def stray_file(self, name):
        """A non-directory that matches brokkr's drive pattern."""
        (self.media / name).write_text("not a drive")

    # -- brokkr's settings for this tree -------------------------------------
    def drive_kwargs(self, **overrides):
        """science_binary_output.drive_kwargs, as main.toml sets it.

        Only `base_path`/`mount_base_path` are redirected at the temp tree;
        `drive_glob`, `mount_glob`, `fallback_path` and `min_free_gb` are the
        shipped values. `base_path` keeps the `{current_user}` placeholder so
        the substitution path stays exercised.
        """
        kwargs = {
            "drive_glob": "DATA??",
            "mount_glob": True,
            "fallback_path": "~/brokkr/{system_name}/science",
            "min_free_gb": 0.1,
            "base_path": str(self.media_base) + "/{current_user}",
            "mount_base_path": str(self.by_label),
            }
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
            UNIT_CONFIG={"number": 51, "site_description": "test"}),
        }
    with patch.dict("sys.modules", modules), \
            patch("os.path.ismount", side_effect=fake_ismount), \
            patch("os.statvfs", side_effect=fake_statvfs):
        yield


def run_cycles(monitor, count):
    """Run the check `count` times; return the list of messages produced."""
    return [monitor.check_drive_target(None) for _ in range(count)]


def mj51_tree(tmp_path):
    """sensor-log #52: stale DATA07/DATA08 dirs, real drives at DATA07?1."""
    tree = Tree(tmp_path)
    tree.label("DATA07")
    tree.label("DATA08")
    tree.stale_dir("DATA07")        # empty, root:root, left by a dirty unmount
    tree.stale_dir("DATA08")
    tree.mount("DATA071", free_gib=500)   # udisks' suffixed mountpoints
    tree.mount("DATA081", free_gib=500)
    return tree


def healthy_tree(tmp_path, free_gib=500):
    tree = Tree(tmp_path)
    tree.label("DATA07")
    tree.label("DATA08")
    tree.mount("DATA07", free_gib=free_gib)
    tree.mount("DATA08", free_gib=free_gib)
    return tree


# --- The config/constructor contract ----------------------------------------

class TestConfigContract:
    """A config key with no constructor parameter is a fleet-wide outage.

    brokkr's `Executable.__init__` has no `**kwargs`, so an unconsumed key in
    main.toml raises TypeError while the pipeline is being built and takes the
    whole telemetry pipeline down on every sensor. PR #84 shipped exactly that
    and its suite could not see it.
    """

    def test_every_config_key_is_an_init_parameter(self):
        parameters = inspect.signature(StateMonitor.__init__).parameters
        accepted = set(parameters)
        assert "output_step_kwargs" in accepted, (
            "StateMonitor must keep its **kwargs passthrough")
        for key in config_state_monitor_kwargs():
            assert key in accepted, (
                "config/main.toml sets `{}` but StateMonitor.__init__ does not "
                "accept it -- this is a TypeError at pipeline build".format(key))

    def test_constructs_with_the_shipped_config(self):
        monitor = make_monitor()
        assert monitor.drive_target_cycles == 5

    def test_drive_target_cycles_is_declared_in_main_toml(self):
        assert "drive_target_cycles" in config_state_monitor_kwargs()

    def test_real_init_sets_up_the_latch_state(self):
        monitor = make_monitor()
        assert monitor._drive_target_signature is None
        assert monitor._drive_target_count == 0
        assert monitor._drive_target_alerted is None

    def test_check_runs_only_when_drive_checks_are_enabled(self):
        for enabled in (True, False):
            monitor = make_monitor(enable_drive_checks=enabled)
            for name in ("check_drive", "check_drive_target", "check_pi_space",
                         "check_ping", "check_power", "check_battery_voltage",
                         "check_sensor_drive", "check_scrub_health"):
                setattr(monitor, name, MagicMock(return_value=None))
            monitor.run_checks({})
            assert monitor.check_drive_target.called is enabled


# --- Fidelity to brokkr ------------------------------------------------------

class TestBrokkrContract:
    """The monitor's view of "which drives" must be brokkr's, not a new rule."""

    def test_defaults_are_read_from_brokkrs_signature(self):
        monitor = make_monitor()
        defaults = monitor._output_drive_defaults()
        assert defaults["base_path"] == "/media/{current_user}"
        assert defaults["mount_base_path"] == "/dev/disk/by-label"

    def test_no_glob_is_hardcoded_or_widened_in_the_plugin(self):
        """The pattern comes from config; the source must not restate it.

        Both halves matter: PR #84 read `drive_glob` from config and then
        appended `"*"` to it, which is how it ended up measuring a filesystem
        brokkr cannot write to.
        """
        source = PLUGIN_PATH.read_text()
        literals = set()
        for node in ast.walk(ast.parse(source)):
            # ast.Constant on 3.8+, ast.Str on the sensors' 3.7
            value = getattr(node, "value", getattr(node, "s", None))
            if (type(node).__name__ in ("Constant", "Str")
                    and isinstance(value, str) and "\n" not in value):
                literals.add(value)     # single-line literals: skip docstrings
        assert "DATA??" not in literals, "no drive pattern may be hardcoded"
        for widening in ('drive_glob + "*"', "drive_glob + '*'",
                         'drive_glob) + "*"', '"{}*".format(drive_glob)'):
            assert widening not in source, (
                "the configured glob must be used unchanged")

    def test_drive_glob_is_not_widened(self, tmp_path):
        """Whatever pattern config gives is used unchanged -- no `+ '*'`.

        Widening it is what let PR #84 see the suffixed mount and then report
        a filesystem brokkr cannot write to as healthy.
        """
        tree = Tree(tmp_path)
        tree.label("XYZ99")
        tree.mount("XYZ99", free_gib=500)
        monitor = make_monitor()
        with sensor(tree, tree.drive_kwargs(drive_glob="XYZ??")):
            view = monitor._brokkr_drive_view()
        assert [drive.name for drive in view.candidates] == ["XYZ99"]

    def test_get_output_drive_is_never_called(self, tmp_path):
        """It mounts drives; the monitor is alert-only.

        Enforced by the stand-in's tripwire body, so this only asserts for real
        when brokkr is not installed.
        """
        if REAL_BROKKR_OUTPUT is not None:
            pytest.skip("real brokkr installed; tripwire body not in play")
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()
        with sensor(tree):
            run_cycles(monitor, 6)   # no AssertionError == never called

    @pytest.mark.skipif(REAL_BROKKR_OUTPUT is None,
                        reason="brokkr is not importable in this environment")
    def test_standin_matches_real_brokkr(self, tmp_path):
        """Pin the stand-in to real brokkr whenever brokkr is available."""
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


# --- The acceptance test -----------------------------------------------------

class TestMj51Topology:
    """sensor-log #52 on mj51: eight days of science data onto the SD card."""

    def test_widened_glob_would_have_reported_healthy(self, tmp_path):
        """Why PR #84's approach cannot work, stated as an assertion.

        `DATA??*` finds the suffixed mount and measures 500 GB free on it --
        but that filesystem is not the one brokkr writes to, which is the SD
        card, because brokkr's own `DATA??` finds nothing.
        """
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
        assert sorted(d.name for d in widened) == ["DATA071", "DATA081"]
        assert brokkrs == [], "brokkr's own glob sees no drive -> SD fallback"

    def test_mj51_topology_alerts(self, tmp_path):
        """ACCEPTANCE: the shipped config must alert on this topology."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor()          # drive_target_cycles = 5, as shipped
        with sensor(tree):
            messages = run_cycles(monitor, 5)
        assert messages[:4] == [None, None, None, None]   # damping
        alert = messages[4]
        assert alert is not None
        assert "DATA07" in alert and "DATA08" in alert    # the labelled drives
        assert "DATA071" in alert and "DATA081" in alert  # the real mountpoints
        assert "SD card" in alert

    def test_alert_names_drives_and_the_remedy_not_the_glob(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert "DATA??" not in alert, "name the drives, not the pattern"
        assert "rmdir" in alert
        assert "scrub" in alert.lower(), "must say the scrub cannot fix this"
        assert "only deletes on the AGS" in alert

    def test_healthy_tree_is_silent(self, tmp_path):
        tree = healthy_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert run_cycles(monitor, 3) == [None, None, None]

    def test_unmounted_drive_is_reported(self, tmp_path):
        """A labelled drive that is attached but nowhere mounted."""
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.label("DATA08")
        tree.mount("DATA08", free_gib=500)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None and "DATA07" in alert

    def test_no_labelled_drives_is_not_this_checks_problem(self, tmp_path):
        """`check_drive` already alerts for that; do not double-page."""
        tree = Tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert monitor.check_drive_target(None) is None


# --- Faults other than the hidden mount --------------------------------------

class TestNonDirectoryMatches:
    """brokkr's ismount filter screens directories ONLY.

    `not drive.is_dir() or os.path.ismount(drive)` keeps every non-directory
    match unconditionally, and statvfs on a plain file reports the containing
    filesystem. A single stray `/media/pi/DATA55.bin` silently disarmed PR #84.
    """

    def test_stray_file_is_reported_not_silently_kept(self, tmp_path):
        tree = healthy_tree(tmp_path)
        tree.stray_file("DATA55")          # matches DATA?? and is not a dir
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "DATA55" in alert and "not" in alert and "director" in alert

    def test_stray_file_cannot_stand_in_for_its_label(self, tmp_path):
        """A file named DATA09 must not satisfy the DATA09 label."""
        tree = healthy_tree(tmp_path)
        tree.label("DATA09")
        tree.stray_file("DATA09")
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "no write target" in alert   # still counted as hidden


class TestUnusableMounts:

    def test_readonly_mount_alerts(self, tmp_path):
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.mount("DATA07", free_gib=500, readonly=True)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "READ-ONLY" in alert and "DATA07" in alert

    def test_unreadable_mount_alerts_and_logs(self, tmp_path):
        """PR #84's `except OSError: continue` had no logger call at all."""
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.unreadable_mount("DATA07")
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None and "DATA07" in alert
        assert monitor.logger.warning.called

    def test_enumeration_failure_is_logged_and_evaluates_nothing(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            with patch.object(OUTPUT_MODULE, "find_drives",
                              side_effect=OSError(13, "Permission denied")):
                assert monitor.check_drive_target(None) is None
        assert monitor.logger.warning.called
        assert monitor._drive_target_count == 0

    def test_unresolvable_config_is_logged_not_raised(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree, tree.drive_kwargs(drive_glob=None)):
            assert monitor.check_drive_target(None) is None
        assert monitor.logger.warning.called


# --- Damping and the latch ---------------------------------------------------

class TestDampingAndLatch:
    """PR #84 had no damping (6 alerts in 12 minutes on a flap) and cleared its
    latch only on the healthy branch (permanently mute after a drive swap)."""

    @pytest.mark.parametrize("cycles", [2, 7])
    def test_the_damping_knob_is_live(self, tmp_path, cycles):
        """Non-default values must actually change when the alert fires."""
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=cycles)
        with sensor(tree):
            messages = run_cycles(monitor, cycles + 1)
        assert messages[:cycles - 1] == [None] * (cycles - 1)
        assert messages[cycles - 1] is not None
        assert messages[cycles] is None      # latched after the first page

    def test_a_persistent_fault_pages_once(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            messages = run_cycles(monitor, 12)
        assert sum(message is not None for message in messages) == 1

    def test_latch_rearms_when_the_offender_changes(self, tmp_path):
        """A drive swap onto a second broken drive must page again.

        PR #84 cleared its latch only on the healthy branch, so this second
        fault was silent forever.
        """
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.stale_dir("DATA07")
        tree.mount("DATA071", free_gib=500)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            first = monitor.check_drive_target(None)
            assert first is not None and "DATA07" in first
            # swap in a different drive, broken the same way
            shutil.rmtree(str(tree.media / "DATA07"))
            (tree.by_label / "DATA07").unlink()
            tree.mounts.discard(str(tree.media / "DATA071"))
            tree.label("DATA09")
            tree.stale_dir("DATA09")
            tree.mount("DATA091", free_gib=500)
            second = monitor.check_drive_target(None)
        assert second is not None and "DATA09" in second

    def test_latch_clears_and_rearms_after_recovery(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert monitor.check_drive_target(None) is not None
            # operator fixes it: rmdir the orphans, remount properly
            for name in ("DATA071", "DATA081"):
                tree.mounts.discard(str(tree.media / name))
                shutil.rmtree(str(tree.media / name))
            for name in ("DATA07", "DATA08"):
                tree.mounts.add(str(tree.media / name))
                tree.stats[str(tree.media / name)] = FakeStatVFS(500 * GIB)
            assert monitor.check_drive_target(None) is None
            assert monitor._drive_target_alerted is None
            # and it breaks again
            for name in ("DATA07", "DATA08"):
                tree.mounts.discard(str(tree.media / name))
            assert monitor.check_drive_target(None) is not None

    def test_a_flap_shorter_than_the_window_never_pages(self, tmp_path):
        """The mount comes back before the damping window closes."""
        tree = healthy_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=5)
        with sensor(tree):
            tree.mounts.discard(str(tree.media / "DATA07"))
            assert run_cycles(monitor, 3) == [None, None, None]
            tree.mounts.add(str(tree.media / "DATA07"))
            assert run_cycles(monitor, 3) == [None, None, None]
            assert monitor._drive_target_count == 0


# --- Capacity ----------------------------------------------------------------

class TestCapacity:
    """The actionable event is the drive transition, not a fixed free floor.

    1.8 TB drives at mj03's measured 14.75 GiB/day: a 25 GiB floor is silent
    for ~226 days and then gives 41 hours of warning, once. "The first drive is
    full, the unit is on its last one" arrives months earlier, and "full" is
    brokkr's own `min_free_gb`, so no new threshold is introduced.
    """

    def test_two_roomy_drives_are_silent(self, tmp_path):
        tree = healthy_tree(tmp_path, free_gib=900)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert monitor.check_drive_target(None) is None

    def test_last_drive_transition_alerts(self, tmp_path):
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.label("DATA08")
        tree.mount("DATA07", free_gib=0)        # below brokkr's min_free_gb
        tree.mount("DATA08", free_gib=900)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "1 of 2" in alert
        assert "DATA08" in alert and "900.0 GiB" in alert

    def test_full_is_brokkrs_min_free_gb_not_a_new_threshold(self, tmp_path):
        """min_free_gb is decimal GB, exactly as brokkr's select_drive uses it."""
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.label("DATA08")
        tree.mount("DATA08", free_gib=900)
        just_above = tree.mount("DATA07")
        tree.stats[str(just_above)] = FakeStatVFS(0.2 * 1e9)   # > 0.1 GB
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert monitor.check_drive_target(None) is None
            tree.stats[str(just_above)] = FakeStatVFS(0.05 * 1e9)  # < 0.1 GB
            assert monitor.check_drive_target(None) is not None

    def test_all_drives_full_alerts(self, tmp_path):
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.label("DATA08")
        tree.mount("DATA07", free_gib=0)
        tree.mount("DATA08", free_gib=0)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "every DATA drive" in alert
        assert "DATA07" in alert and "DATA08" in alert

    def test_single_full_drive_alerts(self, tmp_path):
        tree = Tree(tmp_path)
        tree.label("DATA07")
        tree.mount("DATA07", free_gib=0)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            assert monitor.check_drive_target(None) is not None

    def test_capacity_is_suppressed_while_a_drive_is_hidden(self, tmp_path):
        """A hidden drive makes the capacity picture meaningless."""
        tree = mj51_tree(tmp_path)
        tree.label("DATA09")
        tree.mount("DATA09", free_gib=0)
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            alert = monitor.check_drive_target(None)
        assert alert is not None
        assert "no write target" in alert
        assert "last one" not in alert and "every DATA drive" not in alert


# --- Alert-only --------------------------------------------------------------

class TestAlertOnly:
    """HAM-185: repair belongs in HAM-173's boot-time oneshot, not here."""

    def test_nothing_is_mounted_removed_or_spawned(self, tmp_path):
        tree = mj51_tree(tmp_path)
        monitor = make_monitor(drive_target_cycles=1)
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

    def test_the_tree_is_unchanged(self, tmp_path):
        tree = mj51_tree(tmp_path)
        before = sorted(path.name for path in tree.media.iterdir())
        monitor = make_monitor(drive_target_cycles=1)
        with sensor(tree):
            run_cycles(monitor, 3)
        assert sorted(path.name for path in tree.media.iterdir()) == before
