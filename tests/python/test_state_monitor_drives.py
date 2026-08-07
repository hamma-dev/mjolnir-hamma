"""Tests for check_recovery_drives (mj-side DATA drive free space).

Ported from the disk-safety-monitors work (PR #80) onto the post-#82
state_monitor. Only Layer 1 is carried over -- Layer 2 (H&S staleness) is
already implemented in check_sensor_drive as hs_stale_cycles, and Layer 3
(futile-scrub alerting) needs redesign against the purge_space/alert_space
model rather than a port.

Drive discovery deliberately goes through brokkr.utils.output.find_drives
rather than a raw glob, because find_drives drops matches that are directories
but not mountpoints. See test_glob_pattern_tolerates_suffixed_mount and
test_uses_find_drives_so_stale_dirs_are_excluded.
"""

import fnmatch
import importlib.util
import os
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"


class MockOutputStep:
    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_state_monitor_module():
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep
    mock_pipeline = MagicMock()
    mock_pipeline.base = mock_base
    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base
    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.pipeline.decode": MagicMock(),
        "brokkr.utils": MagicMock(),
        "brokkr.utils.output": MagicMock(),
        "notifiers": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location(
            "state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


MODULE = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor

DU = namedtuple("DU", ["total", "used", "free"])
GB = 2 ** 30


def du(free_gb):
    return DU(2000 * GB, 0, int(free_gb * GB))


class FakeDataValue:
    def __init__(self, value):
        self.value = value


def make_input_data(bytes_remaining):
    return {"bytes_remaining": FakeDataValue(bytes_remaining)}


def make_drive_monitor(recovery_low_gb=25, scrub_command=""):
    """Build a StateMonitor with just the attrs check_recovery_drives needs.

    Bypasses __init__ so this stays decoupled from the rest of the plugin's
    constructor surface.
    """
    mon = StateMonitor.__new__(StateMonitor)
    mon.logger = MagicMock()
    mon.scrub_command = scrub_command
    mon.recovery_low_gb = recovery_low_gb
    mon._recovery_low_active = False
    return mon


@contextmanager
def mounted(paths, drive_glob="DATA??"):
    """Patch the CONFIG lookup and find_drives to report `paths` as mounted."""
    mock_config = {"steps": {"science_binary_output": {
        "drive_kwargs": {"drive_glob": drive_glob}}}}
    find_drives = MODULE.brokkr.utils.output.find_drives
    find_drives.reset_mock()
    find_drives.return_value = list(paths)
    with patch.dict("sys.modules", {
            "brokkr.config.main": MagicMock(CONFIG=mock_config),
            "brokkr.utils.misc": MagicMock(
                get_actual_username=lambda: "pi")}):
        yield find_drives


def brokkr_find_drives(base, drive_glob, filename_kwargs=None):
    """Faithful stand-in for brokkr.utils.output.find_drives.

    Same filter as brokkr/src/brokkr/utils/output.py: keep a match unless it is
    a directory that is not a mountpoint. Used against a REAL temp tree so the
    udisks topology tests exercise real globbing and real is_dir(), rather than
    asserting against a hand-written list of paths.
    """
    return [p for p in Path(base).glob(drive_glob)
            if not p.is_dir() or os.path.ismount(p)]


# --- discovery: the glob fix -------------------------------------------------

class TestDriveDiscovery:
    """The bugs this port fixes relative to the original PR #80 implementation."""

    def test_uses_find_drives_so_stale_dirs_are_excluded(self):
        """Discovery must go through find_drives, not a raw glob.

        find_drives filters `not is_dir() or ismount()`, which is what keeps a
        stale /media/pi/DATA07 directory out of the results. shutil.disk_usage
        on such a directory reports the SD root's free space, and since this
        check takes max(), a roomy root would mask genuinely full DATA drives.
        """
        mon = make_drive_monitor()
        with mounted(["/media/pi/DATA56"]) as find_drives:
            with patch("shutil.disk_usage", side_effect=[du(500)]):
                mon.check_recovery_drives(make_input_data(100))
        find_drives.assert_called_once_with(
            "DATA??*", MODULE.RECOVERY_MOUNT_BASE,
            filename_kwargs={"current_user": "pi"})

    def test_glob_pattern_tolerates_suffixed_mount(self):
        """DATA??* matches both the plain and the udisks-suffixed mountpoint.

        A bare DATA?? matches exactly two trailing characters, so it misses
        DATA071 -- the name udisks uses when a stale DATA07 dir blocks it.
        """
        pattern = "DATA??" + "*"
        assert fnmatch.fnmatch("DATA07", pattern)
        assert fnmatch.fnmatch("DATA071", pattern)
        # still requires at least two chars after DATA, as the original did
        assert not fnmatch.fnmatch("DATA0", pattern)
        # and the unfixed pattern demonstrably misses the suffixed mount
        assert not fnmatch.fnmatch("DATA071", "DATA??")

    def test_glob_comes_from_config_not_hardcoded(self):
        """The pattern is sourced from science_binary_output.drive_kwargs."""
        mon = make_drive_monitor()
        with mounted(["/media/pi/XYZ99"], drive_glob="XYZ??") as find_drives:
            with patch("shutil.disk_usage", side_effect=[du(500)]):
                mon.check_recovery_drives(make_input_data(100))
        find_drives.assert_called_once_with(
            "XYZ??*", MODULE.RECOVERY_MOUNT_BASE,
            filename_kwargs={"current_user": "pi"})

    def test_mount_base_matches_brokkr_not_hardcoded_pi(self):
        """Base path is brokkr's own template, so it can't drift from where
        brokkr actually writes (it uses /media/{current_user}, not /media/pi)."""
        assert MODULE.RECOVERY_MOUNT_BASE == "/media/{current_user}"


# --- the udisks topology, against a real directory tree ----------------------

class TestUdisksTopology:
    """Reproduce sensor-log #52 (mj51) on a real filesystem.

    A dirty unmount left an empty /media/pi/DATA07 directory; udisks then
    mounted the real drive at DATA071. Brokkr's `DATA??` glob skipped the
    suffixed mount and science data fell back to the SD card for ~8 days
    with no alert.

    These tests use real directories and real globbing -- only os.path.ismount
    is stubbed, because creating an actual mount needs privileges. Mocking
    find_drives wholesale (as the other tests do) proves nothing about path
    resolution, which is where both bugs live.
    """

    @staticmethod
    def _tree(tmp_path):
        """stale DATA07 dir + real DATA071 and DATA55 mounts."""
        for name in ("DATA07", "DATA071", "DATA55"):
            (tmp_path / name).mkdir()
        mounts = {str(tmp_path / "DATA071"), str(tmp_path / "DATA55")}
        return mounts

    @contextmanager
    def _resolved(self, tmp_path, drive_glob, free_by_name):
        """Run check_recovery_drives against the real tree."""
        mounts = self._tree(tmp_path)
        mock_config = {"steps": {"science_binary_output": {
            "drive_kwargs": {"drive_glob": drive_glob}}}}

        def fake_find(glob_pat, base, filename_kwargs=None):
            return brokkr_find_drives(tmp_path, glob_pat, filename_kwargs)

        def fake_usage(path):
            return du(free_by_name[Path(str(path)).name])

        MODULE.brokkr.utils.output.find_drives.side_effect = fake_find
        with patch.dict("sys.modules", {
                "brokkr.config.main": MagicMock(CONFIG=mock_config),
                "brokkr.utils.misc": MagicMock(
                    get_actual_username=lambda: "pi")}), \
                patch("os.path.ismount", lambda p: str(p) in mounts), \
                patch("shutil.disk_usage", side_effect=fake_usage):
            yield
        MODULE.brokkr.utils.output.find_drives.side_effect = None

    def test_alerts_when_real_drives_full_behind_a_stale_dir(self, tmp_path):
        """Both real drives full; the stale dir would otherwise hide it."""
        mon = make_drive_monitor(recovery_low_gb=25)
        # stale dir would report the roomy root filesystem (40 GB)
        free = {"DATA07": 40, "DATA071": 0.5, "DATA55": 0.5}
        with self._resolved(tmp_path, "DATA??", free):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is not None, "stale dir masked a genuine fill"
        assert "2/2" in msg, "only the two real mounts should be counted"

    def test_suffixed_mount_is_examined(self, tmp_path):
        """DATA071 must be seen; a bare DATA?? glob would skip it."""
        mon = make_drive_monitor(recovery_low_gb=25)
        # DATA55 healthy, suffixed DATA071 nearly full -> roomiest is fine
        free = {"DATA07": 40, "DATA071": 0.5, "DATA55": 500}
        with self._resolved(tmp_path, "DATA??", free):
            msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None, "roomiest real drive has space; should not alert"

    def test_both_fixes_are_required(self, tmp_path):
        """Neither the ismount filter nor the glob widening suffices alone.

        Correct answer with both real drives at 0.5 GB and the root at 40 GB
        is ALERT. Only filter+widened-glob together get there.
        """
        mounts = self._tree(tmp_path)
        free = {"DATA07": 40, "DATA071": 0.5, "DATA55": 0.5}

        def usage(name):
            return du(free[name]).free

        def resolve(glob_pat, use_filter):
            paths = list(Path(tmp_path).glob(glob_pat))
            if use_filter:
                paths = [p for p in paths
                         if not p.is_dir() or str(p) in mounts]
            return [p.name for p in paths]

        def best(glob_pat, use_filter):
            names = resolve(glob_pat, use_filter)
            return max(usage(n) for n in names) if names else None

        threshold = 25 * GB
        # A: as PR #80 shipped -- raw glob, no filter
        assert best("DATA??", False) >= threshold, "unfixed: silent"
        # B: widened glob alone -- stale dir still dominates max()
        assert best("DATA??*", False) >= threshold, "glob alone: still silent"
        # C: filter alone -- correct here, but never examines DATA071
        assert "DATA071" not in resolve("DATA??", True)
        # D: both -- alerts, and sees the suffixed mount
        assert best("DATA??*", True) < threshold, "both fixes: alerts"
        assert "DATA071" in resolve("DATA??*", True)


# --- Layer 1: check_recovery_drives -----------------------------------------

class TestRecoveryDrives:
    def test_alert_when_all_drives_low(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with mounted(["/media/pi/DATA55", "/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(2), du(10)]):
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is not None
        assert "GB" in msg
        assert "2/2" in msg, "message should report how many drives are below"

    def test_no_alert_when_one_drive_has_room(self):
        """DATA55 at 0 (normal rotation), DATA56 has room -> no alert."""
        mon = make_drive_monitor(recovery_low_gb=25)
        with mounted(["/media/pi/DATA55", "/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(0), du(800)]):
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None

    def test_no_drives_returns_none(self):
        mon = make_drive_monitor()
        with mounted([]):
            with patch("shutil.disk_usage") as usage:
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None
        usage.assert_not_called(), "must not stat anything when nothing is mounted"

    def test_all_paths_oserror_returns_none(self):
        """Every path raises -> None, NOT a ValueError from max([])."""
        mon = make_drive_monitor()
        with mounted(["/media/pi/DATA55", "/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=OSError("unmounted")):
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None
        mon.logger.error.assert_not_called()

    def test_per_path_oserror_skipped_healthy_evaluated(self):
        """One path errors, the other is low -> still alerts on the good one."""
        mon = make_drive_monitor(recovery_low_gb=25)

        def side(path):
            if "DATA55" in str(path):
                raise OSError("unmounted")
            return du(3)

        with mounted(["/media/pi/DATA55", "/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=side):
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is not None
        assert "1/1" in msg, "only the surviving drive should be counted"

    def test_debounce_and_rearm(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with mounted(["/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(2)]):
                m1 = mon.check_recovery_drives(make_input_data(100))
            with patch("shutil.disk_usage", side_effect=[du(1)]):
                m2 = mon.check_recovery_drives(make_input_data(100))   # still low
            with patch("shutil.disk_usage", side_effect=[du(500)]):
                m3 = mon.check_recovery_drives(make_input_data(100))   # recovered
            with patch("shutil.disk_usage", side_effect=[du(1)]):
                m4 = mon.check_recovery_drives(make_input_data(100))   # low again
        assert m1 is not None
        assert m2 is None      # debounced
        assert m3 is None      # recovery is silent
        assert m4 is not None  # re-armed -> re-alert

    def test_boundary_equal_threshold_no_alert(self):
        mon = make_drive_monitor(recovery_low_gb=25)
        with mounted(["/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(25)]):
                msg = mon.check_recovery_drives(make_input_data(100))
        assert msg is None  # free == threshold is NOT below

    def test_alert_only_never_spawns(self):
        """This check must never launch a scrub -- a full DATA drive is not
        fixed by scrubbing the AGS."""
        mon = make_drive_monitor(recovery_low_gb=25,
                                 scrub_command="python3 scrub.py")
        with mounted(["/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(1)]), \
                    patch("subprocess.Popen") as popen:
                mon.check_recovery_drives(make_input_data(100))
        popen.assert_not_called()

    def test_no_agsdependency(self):
        """Runs off local mounts only -- still reports when the AGS is dark."""
        mon = make_drive_monitor(recovery_low_gb=25)
        with mounted(["/media/pi/DATA56"]):
            with patch("shutil.disk_usage", side_effect=[du(1)]):
                msg = mon.check_recovery_drives(make_input_data("NA"))
        assert msg is not None


# --- wiring ------------------------------------------------------------------

class TestRunChecksWiring:
    """Nothing previously proved the checks are actually invoked."""

    def _monitor_with_stubbed_checks(self, enable_drive_checks):
        mon = StateMonitor.__new__(StateMonitor)
        mon.logger = MagicMock()
        mon.send_message = MagicMock()
        mon.log_error = MagicMock()
        mon.enable_drive_checks = enable_drive_checks
        for name in ("check_pi_space", "check_ping", "check_power",
                     "check_battery_voltage", "check_drive",
                     "check_recovery_drives", "check_sensor_drive",
                     "check_scrub_health"):
            setattr(mon, name, MagicMock(return_value=None))
        return mon

    def test_recovery_check_is_registered(self):
        mon = self._monitor_with_stubbed_checks(enable_drive_checks=True)
        mon.run_checks(make_input_data(100))
        mon.check_recovery_drives.assert_called_once()

    def test_recovery_check_suppressed_without_drive_checks(self):
        """Units with no sensor hardware (enable_drive_checks=False) skip it."""
        mon = self._monitor_with_stubbed_checks(enable_drive_checks=False)
        mon.run_checks(make_input_data(100))
        mon.check_recovery_drives.assert_not_called()

    def test_alert_is_sent_when_check_returns_a_message(self):
        mon = self._monitor_with_stubbed_checks(enable_drive_checks=True)
        mon.check_recovery_drives.return_value = "Recovery drives low: ..."
        mon.run_checks(make_input_data(100))
        mon.send_message.assert_called_once_with("Recovery drives low: ...")

    def test_check_exception_does_not_abort_other_checks(self):
        mon = self._monitor_with_stubbed_checks(enable_drive_checks=True)
        mon.check_recovery_drives.side_effect = OSError("boom")
        mon.run_checks(make_input_data(100))
        mon.log_error.assert_called_once()
        mon.check_scrub_health.assert_called_once()
