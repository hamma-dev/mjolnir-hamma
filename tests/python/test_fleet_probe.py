"""Tests for server/fleet_probe.py -- the daily fleet configuration probe.

Everything here exercises the pure functions with fabricated rows; nothing
contacts the fleet. probe_unit() is covered by mocking subprocess.run so the
stdout parser is tested without ssh.
"""

import csv
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
from unittest.mock import MagicMock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "server" / "fleet_probe.py"


@pytest.fixture(scope="module")
def fp():
    """Load fleet_probe by path. Its module-level `import ags` resolves because
    the module inserts ../scripts on sys.path relative to its own __file__."""
    spec = importlib.util.spec_from_file_location("fleet_probe", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_network_release_head(request, fp):
    """main() always reads the release head via a real `git ls-remote` --
    a network call. Default it to None here so a test that calls main()
    without caring about the release-drift section can't hang (or flake)
    offline; a test that patches release_head itself inside its own
    `with`/context overrides this fixture's patch for exactly its own
    scope and is restored to it afterward.

    Excludes TestReleaseHead: those tests call the REAL release_head() to
    exercise its own implementation (they patch subprocess.run instead),
    so patching the function itself here would hide what they test.
    """
    if request.cls is not None and request.cls.__name__ == "TestReleaseHead":
        yield
        return
    with patch.object(fp, "release_head", return_value=None):
        yield


def row(fp, unit, front_end="on", mode="default",
        t1="450", t2="450", gf="10", gs="1",
        mh="a1b2c3d", brk="e4f5a6b", snd="0c1d2e3", ham="4455667", ntf="ce1dcb5"):
    """A full snapshot row. Every field independent, so prev/cur never alias."""
    return {"unit": unit, "front_end": front_end, "brokkr_mode": mode,
            "threshold_1_mv": t1, "threshold_2_mv": t2,
            "gain_fast": gf, "gain_slow": gs,
            "mjolnir_hamma": mh, "brokkr": brk, "sindri": snd,
            "hamma": ham, "notifiers": ntf}


# --------------------------------------------------------------------------
# diff() -- the power-transition collapse is the heart of this iteration
# --------------------------------------------------------------------------
class TestDiffCollapse:
    def test_threshold_change_without_power_flip_is_reported(self, fp):
        prev = {"mjolnir04": row(fp, "mjolnir04", t1="450", t2="450")}
        cur = {"mjolnir04": row(fp, "mjolnir04", t1="750", t2="750")}
        changes = fp.diff(prev, cur)
        fields = {c[1] for c in changes}
        assert fields == {"threshold_1_mv", "threshold_2_mv"}

    def test_gain_change_without_power_flip_is_reported(self, fp):
        prev = {"mjolnir03": row(fp, "mjolnir03", gf="10")}
        cur = {"mjolnir03": row(fp, "mjolnir03", gf="20")}
        assert fp.diff(prev, cur) == [("mjolnir03", "gain_fast", "10", "20")]

    def test_power_on_suppresses_the_ags_derived_cascade(self, fp):
        # off/blk -> on/populated: report the front_end flip, not the six lines
        prev = {"mjolnir06": row(fp, "mjolnir06", front_end="off",
                                 mode="nosensor_nochargecontroller",
                                 t1="", t2="", gf="", gs="")}
        cur = {"mjolnir06": row(fp, "mjolnir06", front_end="on", mode="default")}
        fields = [c[1] for c in fp.diff(prev, cur)]
        assert "front_end" in fields
        assert not (set(fields) & fp.POWER_DERIVED_FIELDS)

    def test_power_off_suppresses_the_ags_derived_cascade(self, fp):
        prev = {"mjolnir42": row(fp, "mjolnir42", front_end="on")}
        cur = {"mjolnir42": row(fp, "mjolnir42", front_end="off", mode="nosensor",
                                t1="", t2="", gf="", gs="")}
        fields = [c[1] for c in fp.diff(prev, cur)]
        assert "front_end" in fields
        assert not (set(fields) & fp.POWER_DERIVED_FIELDS)

    def test_mode_change_is_NOT_suppressed_on_a_power_flip(self, fp):
        # mode is a distinct decision, not a mechanical consequence of the relay
        prev = {"mjolnir06": row(fp, "mjolnir06", front_end="off",
                                 mode="nosensor", t1="", t2="", gf="", gs="")}
        cur = {"mjolnir06": row(fp, "mjolnir06", front_end="on", mode="default")}
        fields = {c[1] for c in fp.diff(prev, cur)}
        assert "brokkr_mode" in fields

    def test_repo_sha_change_is_reported(self, fp):
        prev = {"mjolnir08": row(fp, "mjolnir08", brk="e4f5a6b")}
        cur = {"mjolnir08": row(fp, "mjolnir08", brk="9988776")}
        assert fp.diff(prev, cur) == [("mjolnir08", "brokkr", "e4f5a6b", "9988776")]


class TestReachabilityCollapse:
    """A unit coming back into view is ONE event, not eleven field changes.

    Found against real fleet data: three units unreachable at baseline produced
    21 of 24 lines in the digest simply by becoming reachable again.
    """

    def _never_probed(self, fp, unit):
        """The row merge() writes for a unit with no prior row: all UNKNOWN."""
        r = {f: fp.UNKNOWN for f in fp.FIELDS}
        r["unit"] = unit
        r["front_end"] = fp.UNREACHABLE
        return r

    def test_coming_back_reports_only_front_end(self, fp):
        prev = {"mjolnir02": self._never_probed(fp, "mjolnir02")}
        cur = {"mjolnir02": row(fp, "mjolnir02", front_end="on")}
        changes = fp.diff(prev, cur)
        assert changes == [("mjolnir02", "front_end", fp.UNREACHABLE, "on")]

    def test_coming_back_as_no_relay_also_collapses(self, fp):
        prev = {"mjolnir50": self._never_probed(fp, "mjolnir50")}
        cur = {"mjolnir50": row(fp, "mjolnir50", front_end="no_relay")}
        fields = [c[1] for c in fp.diff(prev, cur)]
        assert fields == ["front_end"]

    def test_a_real_old_value_that_changed_is_still_reported(self, fp):
        """Only genuinely-UNKNOWN fields are suppressed, not everything."""
        prev = self._never_probed(fp, "mjolnir02")
        prev["brokkr"] = "0000000"          # a real value, not UNKNOWN
        cur = row(fp, "mjolnir02", front_end="on", brk="9999999")
        fields = {c[1] for c in fp.diff({"mjolnir02": prev},
                                        {"mjolnir02": cur})}
        assert "front_end" in fields
        assert "brokkr" in fields           # must survive the collapse

    def test_normal_change_on_a_reachable_unit_is_unaffected(self, fp):
        prev = {"mjolnir08": row(fp, "mjolnir08", brk="e4f5a6b")}
        cur = {"mjolnir08": row(fp, "mjolnir08", brk="9988776")}
        assert fp.diff(prev, cur) == [
            ("mjolnir08", "brokkr", "e4f5a6b", "9988776")]

    def test_going_unreachable_is_still_skipped(self, fp):
        """The reverse direction was already handled; keep it that way."""
        prev = {"mjolnir41": row(fp, "mjolnir41")}
        cur = {"mjolnir41": row(fp, "mjolnir41", front_end=fp.UNREACHABLE)}
        assert fp.diff(prev, cur) == []


class TestDiffEdges:
    def test_first_seen_unit_is_one_entry(self, fp):
        cur = {"mjolnir05": row(fp, "mjolnir05")}
        assert fp.diff({}, cur) == [("mjolnir05", "*", "-", "first seen")]

    def test_unreachable_unit_is_skipped_entirely(self, fp):
        prev = {"mjolnir41": row(fp, "mjolnir41", t1="600")}
        cur = {"mjolnir41": row(fp, "mjolnir41", front_end=fp.UNREACHABLE)}
        assert fp.diff(prev, cur) == []

    def test_new_unreachable_unit_is_not_first_seen(self, fp):
        cur = {"mjolnir09": row(fp, "mjolnir09", front_end=fp.UNREACHABLE)}
        assert fp.diff({}, cur) == []

    def test_stable_fleet_produces_no_changes(self, fp):
        state = {"mjolnir02": row(fp, "mjolnir02")}
        assert fp.diff(state, dict(state)) == []


# --------------------------------------------------------------------------
# merge() -- unreachable-preserves-row
# --------------------------------------------------------------------------
class TestMerge:
    def test_unreachable_carries_previous_row_forward(self, fp):
        prev = {"mjolnir41": row(fp, "mjolnir41", t1="600", gf="20")}
        cur = {"mjolnir41": row(fp, "mjolnir41", front_end=fp.UNREACHABLE)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir41"]["threshold_1_mv"] == "600"
        assert merged["mjolnir41"]["front_end"] == "on"  # last known, not blanked

    def test_new_unreachable_unit_is_added(self, fp):
        cur = {"mjolnir09": row(fp, "mjolnir09", front_end=fp.UNREACHABLE)}
        merged = fp.merge({}, cur)
        assert merged["mjolnir09"]["front_end"] == fp.UNREACHABLE

    def test_reachable_unit_overwrites(self, fp):
        prev = {"mjolnir02": row(fp, "mjolnir02", t1="450")}
        cur = {"mjolnir02": row(fp, "mjolnir02", t1="500")}
        assert fp.merge(prev, cur)["mjolnir02"]["threshold_1_mv"] == "500"


# --------------------------------------------------------------------------
# render() / read_snapshot() round trip
# --------------------------------------------------------------------------
class TestSnapshotIO:
    def test_render_has_header_and_sorted_rows(self, fp):
        rows = {"mjolnir08": row(fp, "mjolnir08"),
                "mjolnir02": row(fp, "mjolnir02")}
        text = fp.render(rows)
        lines = text.strip().splitlines()
        assert lines[0].startswith("unit,front_end,")
        assert lines[1].startswith("mjolnir02,")   # sorted before 08
        assert lines[2].startswith("mjolnir08,")

    def test_round_trip(self, fp, tmp_path):
        rows = {"mjolnir02": row(fp, "mjolnir02", t1="450"),
                "mjolnir41": row(fp, "mjolnir41", t1="600")}
        snap = tmp_path / "fleet-state.csv"
        snap.write_text(fp.render(rows))
        back = fp.read_snapshot(str(snap))
        assert set(back) == {"mjolnir02", "mjolnir41"}
        assert back["mjolnir41"]["threshold_1_mv"] == "600"

    def test_read_missing_snapshot_is_empty(self, fp, tmp_path):
        assert fp.read_snapshot(str(tmp_path / "nope.csv")) == {}


# --------------------------------------------------------------------------
# expected_offline() -- reads inventory-manifest.csv status column
# --------------------------------------------------------------------------
class TestExpectedOffline:
    def _manifest(self, tmp_path, body):
        (tmp_path / "inventory-manifest.csv").write_text(
            "unit,status\n" + body)
        return str(tmp_path)

    def test_recognises_offline_states_case_insensitively(self, fp, tmp_path):
        repo = self._manifest(tmp_path,
                              "mjolnir01,Offline\nmjolnir09,retired\n"
                              "mjolnir10,SHELF\nmjolnir11,Lab\n")
        got = fp.expected_offline(repo)
        assert set(got) == {"mjolnir01", "mjolnir09", "mjolnir10", "mjolnir11"}

    def test_ok_and_blank_are_not_offline(self, fp, tmp_path):
        repo = self._manifest(tmp_path,
                              "mjolnir02,OK\nmjolnir03,\nmjolnir04,deployed\n")
        assert fp.expected_offline(repo) == {}

    def test_missing_manifest_is_empty(self, fp, tmp_path):
        assert fp.expected_offline(str(tmp_path)) == {}

    def test_no_repo_is_empty(self, fp):
        assert fp.expected_offline(None) == {}


# --------------------------------------------------------------------------
# not_capturing() -- the standing section
# --------------------------------------------------------------------------
class TestNotCapturing:
    def test_front_end_off_is_listed(self, fp):
        rows = {"mjolnir06": row(fp, "mjolnir06", front_end="off")}
        assert [u for u, _, _ in fp.not_capturing(rows)] == ["mjolnir06"]

    def test_nosensor_mode_is_listed(self, fp):
        rows = {"mjolnir06": row(fp, "mjolnir06", front_end="on",
                                 mode="nosensor_nochargecontroller")}
        assert [u for u, _, _ in fp.not_capturing(rows)] == ["mjolnir06"]

    def test_healthy_unit_is_silent(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", front_end="on", mode="default")}
        assert fp.not_capturing(rows) == []

    @pytest.mark.parametrize("fe", ["unreachable", "no_relay", "unknown"])
    def test_non_operable_states_are_not_counted_as_not_capturing(self, fp, fe):
        rows = {"mjolnir00": row(fp, "mjolnir00", front_end=fe)}
        assert fp.not_capturing(rows) == []


# --------------------------------------------------------------------------
# field_report() -- the new fleet consistency check
# --------------------------------------------------------------------------
class TestFieldReport:
    def test_uniform_fleet(self, fp):
        rows = {"mjolnir0%d" % n: row(fp, "mjolnir0%d" % n, brk="9988776")
                for n in (2, 3, 4)}
        text = fp.field_report(rows, "brokkr")
        assert "=> uniform" in text
        assert "1 distinct value" in text

    def test_outlier_is_flagged_and_last(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", brk="9988776"),
                "mjolnir03": row(fp, "mjolnir03", brk="9988776"),
                "mjolnir06": row(fp, "mjolnir06", brk="e4f5a6b")}
        text = fp.field_report(rows, "brokkr")
        assert "NOT uniform (2 distinct values)" in text
        # majority group leads, the lone outlier line comes after it
        assert text.index("9988776") < text.index("e4f5a6b")

    def test_blank_value_grouped_as_blank(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", t1="450"),
                "mjolnir50": row(fp, "mjolnir50", front_end="off", t1="")}
        text = fp.field_report(rows, "threshold_1_mv")
        assert "(blank)" in text


# --------------------------------------------------------------------------
# digest() -- what the team actually receives
# --------------------------------------------------------------------------
class TestDigest:
    def test_baseline_message(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02")}
        text = fp.digest(rows, [], [], baseline=True)
        assert "baseline established, 1 units" in text

    def test_change_count_and_lines(self, fp):
        rows = {"mjolnir03": row(fp, "mjolnir03", t1="700")}
        changes = [("mjolnir03", "threshold_1_mv", "450", "700")]
        text = fp.digest(rows, changes, [], baseline=False)
        assert "1 units, 1 change(s)" in text
        assert "mjolnir03  threshold_1_mv 450 -> 700" in text

    def test_unreachable_and_expected_are_split(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02")}
        text = fp.digest(rows, [], ["mjolnir05", "mjolnir01"], baseline=False,
                         expected={"mjolnir01": "Offline"})
        assert "UNREACHABLE: mjolnir05" in text
        assert "mjolnir05" not in text.split("expected offline")[1]  # not double-listed
        assert "expected offline: mjolnir01" in text

    def test_offline_reason_is_bare_but_distinctive_reason_annotated(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02")}
        text = fp.digest(rows, [], ["mjolnir01", "mjolnir09"], baseline=False,
                         expected={"mjolnir01": "Offline", "mjolnir09": "Retired"})
        line = [ln for ln in text.splitlines()
                if ln.startswith("expected offline:")][0]
        assert "mjolnir01" in line and "mjolnir01 (Offline)" not in line
        assert "mjolnir09 (Retired)" in line

    def test_not_capturing_section_present(self, fp):
        rows = {"mjolnir06": row(fp, "mjolnir06", front_end="off")}
        text = fp.digest(rows, [], [], baseline=False)
        assert "not capturing:" in text
        assert "mjolnir06  front_end=off" in text


# --------------------------------------------------------------------------
# probe_unit() -- stdout parsing, mocked ssh (no fleet contact)
# --------------------------------------------------------------------------
class TestProbeUnit:
    def _run(self, stdout, returncode=0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_parses_fields_from_stdout(self, fp):
        out = ("front_end=on\n"
               "brokkr_mode=default\n"
               "mjolnir_hamma=a1b2c3d\n"
               "brokkr=e4f5a6b\n"
               "sindri=0c1d2e3\n"
               "hamma=4455667\n"
               "notifiers=ce1dcb5\n")
        with patch.object(fp.subprocess, "run", return_value=self._run(out)):
            r = fp.probe_unit(2)
        assert r["unit"] == "mjolnir02"
        assert r["front_end"] == "on"
        assert r["brokkr"] == "e4f5a6b"

    def test_nonzero_return_is_unreachable(self, fp):
        with patch.object(fp.subprocess, "run",
                          return_value=self._run("", returncode=255)):
            r = fp.probe_unit(50)
        assert r["front_end"] == fp.UNREACHABLE

    def test_timeout_is_unreachable_not_raised(self, fp):
        with patch.object(fp.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ssh", 90)):
            r = fp.probe_unit(51)
        assert r["front_end"] == fp.UNREACHABLE

    def test_ags_lines_feed_threshold_parser(self, fp):
        # AGS: lines are handed to ags.parse_startup_state; assert the plumbing,
        # not ags's own parsing (that has its own tests).
        out = "front_end=on\nAGS:some startup content\n"
        with patch.object(fp, "parse_startup_state",
                          return_value={"threshold_1_mv": 123,
                                        "gain_fast": 7}) as parser:
            with patch.object(fp.subprocess, "run",
                              return_value=self._run(out)):
                r = fp.probe_unit(2)
        parser.assert_called_once()
        assert r["threshold_1_mv"] == 123
        assert r["gain_fast"] == 7


# --------------------------------------------------------------------------
# main() --field short-circuit -- reads snapshot, must NOT probe
# --------------------------------------------------------------------------
class TestFieldCliDoesNotProbe:
    def test_field_report_never_calls_subprocess(self, fp, tmp_path, capsys):
        rows = {"mjolnir02": row(fp, "mjolnir02", brk="9988776"),
                "mjolnir06": row(fp, "mjolnir06", brk="e4f5a6b")}
        snap = tmp_path / "fleet-state.csv"
        snap.write_text(fp.render(rows))
        argv = ["fleet_probe", "--field", "brokkr", "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp.subprocess, "run",
                             side_effect=AssertionError("must not probe")):
            rc = fp.main()
        assert rc == 0
        assert "NOT uniform" in capsys.readouterr().out

    def test_field_missing_snapshot_returns_1(self, fp, tmp_path):
        argv = ["fleet_probe", "--field", "brokkr",
                "--snapshot", str(tmp_path / "absent.csv")]
        with patch.object(fp.sys, "argv", argv):
            assert fp.main() == 1


# --------------------------------------------------------------------------
# The ags parser must never degrade silently. Without it, thresholds/gains read
# "unknown" for every unit AND -- because "unknown" != the previous value --
# every unit registers a spurious change, polluting the snapshot history.
# --------------------------------------------------------------------------
class TestAgsParserIsRequiredToProbe:
    def test_probing_refuses_when_ags_is_missing(self, fp, tmp_path):
        argv = ["fleet_probe", "--snapshot", str(tmp_path / "snap.csv")]
        with patch.object(fp, "parse_startup_state", None), \
                patch.object(fp, "load_ags_parser", return_value=(None, None)), \
                patch.object(fp.sys, "argv", argv), \
                patch.object(fp.subprocess, "run",
                             side_effect=AssertionError("must not probe")):
            assert fp.main() == 1
        assert not (tmp_path / "snap.csv").exists()   # and wrote nothing

    def test_allow_missing_ags_overrides_deliberately(self, fp, tmp_path):
        argv = ["fleet_probe", "--allow-missing-ags", "-p", "2",
                "--snapshot", str(tmp_path / "snap.csv"), "--dry-run"]
        run = MagicMock(returncode=0, stdout="front_end=on\n", stderr="")
        with patch.object(fp, "parse_startup_state", None), \
                patch.object(fp, "load_ags_parser", return_value=(None, None)), \
                patch.object(fp.sys, "argv", argv), \
                patch.object(fp.subprocess, "run", return_value=run):
            assert fp.main() == 0

    def test_field_report_does_not_require_ags(self, fp, tmp_path):
        """--field reads the snapshot and never probes, so it is exempt."""
        rows = {"mjolnir02": row(fp, "mjolnir02", brk="9988776")}
        snap = tmp_path / "fleet-state.csv"
        snap.write_text(fp.render(rows))
        argv = ["fleet_probe", "--field", "brokkr", "--snapshot", str(snap)]
        with patch.object(fp, "parse_startup_state", None), \
                patch.object(fp, "load_ags_parser", return_value=(None, None)), \
                patch.object(fp.sys, "argv", argv):
            assert fp.main() == 0

    def test_loader_finds_the_sibling_scripts_dir(self, fp):
        """In the mjolnir-hamma layout ../scripts/ags.py must resolve."""
        found, source = fp.load_ags_parser()
        assert found is not None
        assert source is not None

    def test_explicit_path_is_searched_first(self, fp, tmp_path):
        (tmp_path / "ags.py").write_text(
            "def parse_startup_state(text):\n    return {'threshold_1_mv': 999}\n")
        found, source = fp.load_ags_parser(str(tmp_path))
        assert found is not None
        assert source == str(tmp_path)


# --------------------------------------------------------------------------
# pending_items() -- deferred per-unit actions, read from the unit's profile
# --------------------------------------------------------------------------
def write_profile(tmp_path, array, unit, body):
    """Write a unit profile into a fake sensor-log checkout."""
    directory = tmp_path / "deployments" / array
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "{}.md".format(unit)
    path.write_text(body)
    return path


class TestPendingItems:
    def test_unchecked_item_is_returned(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir51", (
            "# mjolnir51: Somewhere\n"
            "\n"
            "## Pending\n"
            "\n"
            "- [ ] Confirm DATA07/DATA08 mount without a suffix (HAM-185)\n"
        ))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == [
            "Confirm DATA07/DATA08 mount without a suffix (HAM-185)"]

    def test_block_ends_at_the_next_heading(self, fp, tmp_path):
        # A checkbox further down the profile is NOT a pending action.
        write_profile(tmp_path, "pamma", "mjolnir51", (
            "## Pending\n"
            "\n"
            "- [ ] Real pending action\n"
            "\n"
            "## Field Log\n"
            "\n"
            "- [ ] Not a pending action, it lives under another heading\n"
        ))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == [
            "Real pending action"]


# --------------------------------------------------------------------------
# digest() -- surfacing pending actions at the moment the unit is back
# --------------------------------------------------------------------------
class TestDigestPending:
    def test_a_returned_unit_surfaces_its_pending_items(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir51", (
            "## Pending\n\n- [ ] Confirm the mountpoints (HAM-185)\n"))
        rows = {"mjolnir51": row(fp, "mjolnir51")}
        changes = [("mjolnir51", "front_end", "unreachable", "on")]
        text = fp.digest(rows, changes, [], repo=str(tmp_path))
        assert "Confirm the mountpoints (HAM-185)" in text

    def test_a_unit_with_no_pending_items_adds_nothing(self, fp, tmp_path):
        rows = {"mjolnir51": row(fp, "mjolnir51")}
        changes = [("mjolnir51", "front_end", "unreachable", "on")]
        text = fp.digest(rows, changes, [], repo=str(tmp_path))
        assert "pending" not in text.lower()


class TestPendingItemsEdges:
    def test_an_empty_checkbox_is_not_an_action(self, fp, tmp_path):
        # The profile template ships a bare `- [ ]`; an unfilled copy of it
        # must not become a nightly digest line saying nothing.
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n\n- [ ] \n- [ ] Real one\n")
        assert fp.pending_items(str(tmp_path), "mjolnir51") == ["Real one"]

    def test_a_checked_item_is_not_returned(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n\n- [x] Already done\n- [ ] Still open\n")
        assert fp.pending_items(str(tmp_path), "mjolnir51") == ["Still open"]

    def test_missing_profile_is_empty(self, fp, tmp_path):
        assert fp.pending_items(str(tmp_path), "mjolnir99") == []

    def test_profile_without_a_pending_block_is_empty(self, fp, tmp_path):
        write_profile(tmp_path, "hamma", "mjolnir08", "# mjolnir08\n\n## Banner\n\n> hi\n")
        assert fp.pending_items(str(tmp_path), "mjolnir08") == []

    def test_no_repo_is_empty(self, fp):
        assert fp.pending_items(None, "mjolnir51") == []

    def test_continuation_lines_are_not_actions(self, fp, tmp_path):
        # Regression guard, not a driven test: the digest goes to chat, so the
        # checkbox line is the short action and the rationale sits under it on
        # indented lines. Capturing those would put paragraphs in the digest.
        write_profile(tmp_path, "pamma", "mjolnir51", (
            "## Pending\n"
            "\n"
            "- [ ] Verify the mountpoints (HAM-185)\n"
            "  mj51 is the sensor-log #52 topology, the only real-world\n"
            "  instance of the hidden-partition case.\n"))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == [
            "Verify the mountpoints (HAM-185)"]


# --------------------------------------------------------------------------
# The trigger, end to end. These drive diff()/merge()/digest() the way main()
# does, rather than hand-building change tuples -- which is how the original
# transition-gated version shipped broken: merge() never stores `unreachable`
# for a unit that was reachable when it entered the snapshot, so the return
# produced no change line at all and the block never printed.
# --------------------------------------------------------------------------
class TestPendingSurvivesARealOutage:
    def test_pending_shows_after_a_unit_goes_dark_and_returns(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n\n- [ ] Verify the mountpoints (HAM-185)\n")
        repo = str(tmp_path)
        previous = {"mjolnir51": row(fp, "mjolnir51", front_end="on")}

        dark = {"mjolnir51": row(fp, "mjolnir51", front_end="unreachable")}
        snapshot = fp.merge(previous, dark)

        back = {"mjolnir51": row(fp, "mjolnir51", front_end="on")}
        changes = fp.diff(snapshot, back)
        text = fp.digest(fp.merge(snapshot, back), changes, [], repo=repo)
        assert "Verify the mountpoints (HAM-185)" in text

    def test_pending_shows_every_day_not_just_on_the_return(self, fp, tmp_path):
        # A quiet fleet produces no changes at all. The item must still show,
        # or it is announced once and invisible thereafter.
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n\n- [ ] Verify the mountpoints (HAM-185)\n")
        rows = {"mjolnir51": row(fp, "mjolnir51", front_end="on")}
        text = fp.digest(rows, [], [], repo=str(tmp_path))
        assert "Verify the mountpoints (HAM-185)" in text

    def test_pending_is_hidden_while_the_unit_is_unreachable(self, fp, tmp_path):
        # Nobody can act on it, so it is noise until the unit is back.
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n\n- [ ] Verify the mountpoints (HAM-185)\n")
        rows = {"mjolnir51": row(fp, "mjolnir51", front_end="on")}
        text = fp.digest(rows, [], ["mjolnir51"], repo=str(tmp_path))
        assert "Verify the mountpoints (HAM-185)" not in text

    def test_the_pending_block_is_bounded(self, fp, tmp_path):
        # An over-long chat message is REJECTED, not truncated, so an
        # unbounded block loses the whole digest -- changes and all -- exactly
        # when a whole array has just come back. Measured 18 units x 3 items
        # ~= 6.2 kB against a 4 kB ceiling.
        rows = {}
        for n in range(12):
            unit = "mjolnir{:02d}".format(n)
            rows[unit] = row(fp, unit)
            write_profile(tmp_path, "hamma", unit,
                          "## Pending\n\n- [ ] a\n- [ ] b\n- [ ] c\n")
        text = fp.digest(rows, [], [], repo=str(tmp_path))
        body = [ln for ln in text.splitlines() if "[ ]" in ln]
        assert len(body) == fp.PENDING_DIGEST_MAX
        assert "and 16 more" in text


class TestPendingParserHardening:
    """Each case here was a demonstrated failure in review, not a hypothetical."""

    def test_an_example_inside_an_html_comment_is_not_an_action(self, fp, tmp_path):
        # Both the template and every profile carry their instructions in a
        # comment INSIDE this block, complete with an example item. The old
        # parser was saved only by that example happening to be indented;
        # dedent it and every new profile broadcasts boilerplate nightly.
        write_profile(tmp_path, "pamma", "mjolnir51", (
            "## Pending\n"
            "<!--\n"
            "Example:\n"
            "- [ ] TEMPLATE BOILERPLATE\n"
            "-->\n"
            "- [ ] A real action\n"))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == ["A real action"]

    @pytest.mark.parametrize("terminator", ["### Notes", "# Other", "##Field Log"])
    def test_any_heading_ends_the_block(self, fp, tmp_path, terminator):
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "## Pending\n- [ ] real\n{}\n- [ ] LEAKED\n".format(terminator))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == ["real"]

    @pytest.mark.parametrize("heading", [
        "## Pending", "### Pending", "##Pending", "## pending", "## Pending:"])
    def test_heading_spellings_people_actually_write(self, fp, tmp_path, heading):
        # Silently ignoring these means somebody's deferred work vanishes while
        # the profile still looks correct when rendered.
        write_profile(tmp_path, "pamma", "mjolnir51",
                      "{}\n\n- [ ] found it\n".format(heading))
        assert fp.pending_items(str(tmp_path), "mjolnir51") == ["found it"]

    def test_a_non_utf8_byte_does_not_kill_the_probe(self, fp, tmp_path):
        # digest() is built BEFORE the snapshot is written and pushed, so an
        # exception here discarded every unit's configuration change for the
        # day, not just this line. A pasted Word em-dash was enough.
        directory = tmp_path / "deployments" / "pamma"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mjolnir51.md").write_bytes(
            b"## Pending\n\n- [ ] Word em-dash \x97 here\n")
        items = fp.pending_items(str(tmp_path), "mjolnir51")
        assert len(items) == 1 and items[0].startswith("Word em-dash")

    def test_duplicate_profiles_resolve_deterministically(self, fp, tmp_path):
        # Profiles do move between array dirs (mj05 hamma<->lab, mj07 camma->lab).
        # A stale copy left behind must not make the VPS and a laptop disagree.
        write_profile(tmp_path, "mjolnir-lab", "mjolnir05", "## Pending\n- [ ] lab\n")
        write_profile(tmp_path, "hamma", "mjolnir05", "## Pending\n- [ ] hamma\n")
        assert fp.pending_items(str(tmp_path), "mjolnir05") == ["hamma"]


INSTALL_FIELDS = ["brokkr_install", "sindri_install", "hamma_install",
                  "notifiers_install"]


class TestInstallFields:
    def test_install_fields_are_appended_to_the_schema(self, fp):
        # Appending is safe; reordering rewrites every snapshot row.
        assert fp.FIELDS[-len(INSTALL_FIELDS):] == INSTALL_FIELDS
        assert fp.FIELDS[:12] == list(row(fp, "x").keys())

    def test_probe_parses_install_fields(self, fp):
        out = ("front_end=on\n"
               "brokkr_install=installed\n"
               "sindri_install=editable\n"
               "hamma_install=installed\n"
               "notifiers_install=missing\n")
        run = MagicMock(returncode=0, stdout=out, stderr="")
        with patch.object(fp.subprocess, "run", return_value=run):
            r = fp.probe_unit(8)
        assert r["brokkr_install"] == "installed"
        assert r["sindri_install"] == "editable"
        assert r["notifiers_install"] == "missing"

    def test_remote_script_classifies_each_package_by_import_path(self, fp):
        # sindri runs from its own venv; the rest from ltgenv.
        for pkg, env in [("brokkr", "ltgenv"), ("notifiers", "ltgenv"),
                         ("hamma", "ltgenv"), ("sindri", "sindrienv")]:
            assert "{}:{}".format(pkg, env) in fp.REMOTE
        for label in ("editable", "installed", "missing", "broken"):
            assert label in fp.REMOTE

    def test_remote_interpreter_calls_are_timeout_bounded(self, fp):
        # `timeout 10` inside the per-package loop body (HAM-227) -- it runs
        # once per package at runtime (4 calls worst case = 40s), so a hung
        # interpreter can't eat the whole 90s per-unit probe budget.
        assert "timeout 10 /home/pi/dev/$ENV/bin/python3" in fp.INSTALL_PROBE

    def test_install_mode_change_is_reported(self, fp):
        prev = {"mjolnir06": dict(row(fp, "mjolnir06"),
                                  notifiers_install="installed")}
        cur = {"mjolnir06": dict(row(fp, "mjolnir06"),
                                 notifiers_install="editable")}
        assert fp.diff(prev, cur) == [
            ("mjolnir06", "notifiers_install", "installed", "editable")]

    def test_new_schema_field_is_not_a_change(self, fp):
        # The first run after upgrading must not report four "changes" per
        # unit just because the old snapshot predates the columns.
        prev = {"mjolnir02": row(fp, "mjolnir02")}          # no *_install keys
        cur = {"mjolnir02": dict(row(fp, "mjolnir02"),
                                 **{f: "editable" for f in INSTALL_FIELDS})}
        assert fp.diff(prev, cur) == []

    def test_old_snapshot_without_columns_round_trips(self, fp, tmp_path):
        path = tmp_path / "fleet-state.csv"
        path.write_text(",".join(fp.FIELDS[:12]) + "\n"
                        + ",".join(row(fp, "mjolnir02").values()) + "\n")
        rows = fp.read_snapshot(str(path))
        text = fp.render(rows)
        assert text.splitlines()[0] == ",".join(fp.FIELDS)

    def test_remote_invokes_isolated_mode(self, fp):
        # Red-team finding 3: a plain `python3 -c` puts '' (the ssh
        # session's cwd) first on sys.path. mj02's /home/pi/brokkr/ has no
        # __init__.py, so a genuinely missing brokkr resolves as a
        # namespace package there and reads `unknown`, not `missing`.
        assert "/bin/python3 -I -c" in fp.INSTALL_PROBE


# --------------------------------------------------------------------------
# Red-team finding 4: the blank-old-value skip in diff() is specific to
# *_install -- a future field that legitimately goes blank must still be
# able to report its first real value as a change.
# --------------------------------------------------------------------------
class TestBlankSkipIsNotGeneric:
    def test_a_blank_to_value_change_in_a_non_install_field_is_reported(
            self, fp):
        prev = {"mjolnir02": dict(row(fp, "mjolnir02"), brokkr_mode="")}
        cur = {"mjolnir02": dict(row(fp, "mjolnir02"), brokkr_mode="default")}
        assert fp.diff(prev, cur) == [
            ("mjolnir02", "brokkr_mode", "", "default")]

    def test_a_blank_old_install_value_is_still_not_a_change(self, fp):
        # The actual *_install case stays suppressed.
        prev = {"mjolnir02": dict(row(fp, "mjolnir02"), notifiers_install="")}
        cur = {"mjolnir02": dict(row(fp, "mjolnir02"),
                                 notifiers_install="installed")}
        assert fp.diff(prev, cur) == []


# --------------------------------------------------------------------------
# Red-team finding 1 (SHOULD FIX): the classifier really imports each
# package (hamma pulls numpy/pandas), so a slow or cold unit can flap
# between its real mode and timeout/unknown run to run. merge() steadies
# that back to the last real value for *_install specifically; broken/
# missing are real, persistent outcomes and are never overridden.
# --------------------------------------------------------------------------
class TestInstallFlapIsSteadied:
    """Red-team round 3, merge-blocking finding 1: the previous steadying
    rule treated `timeout` and `unknown` alike, so a LOST interpreter
    (exit 127, reads `unknown`) was restored right back to its old real
    value -- 0 changes, no commit, no digest line, forever. The new rule
    has three cases (see merge()): `timeout` steadies over a real value;
    `unknown` does NOT (it is a real, reportable change); a never-measured
    field stays "" across either; and a field already stuck in
    timeout/unknown does not flip between the two.
    """

    def test_a_lost_interpreter_is_a_real_reported_change(self, fp):
        # (a)/else: `unknown` over a REAL old value is NOT steadied.
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install="editable")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.UNKNOWN)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == fp.UNKNOWN
        assert fp.diff(prev, merged) == [
            ("mjolnir08", "notifiers_install", "editable", fp.UNKNOWN)]

    def test_a_lost_interpreter_is_committed_and_appears_in_install_problems(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        stable = dict(row(fp, "mjolnir08"), notifiers_install="editable")
        snap.write_text(fp.render({"mjolnir08": stable}))
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]
        lost = dict(stable, notifiers_install=fp.UNKNOWN)
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=lost):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "0 change(s)" not in out, out
        assert "notifiers_install editable -> unknown" in out, out
        assert "mjolnir08  notifiers=unknown" in out, out
        row_line = next(l for l in snap.read_text().splitlines()
                        if l.startswith("mjolnir08,"))
        assert row_line.split(",")[-1] == fp.UNKNOWN

    def test_timeout_over_a_real_value_steadies_with_no_change_or_commit(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        stable = dict(row(fp, "mjolnir08"), notifiers_install="installed")
        snap.write_text(fp.render({"mjolnir08": stable}))
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]
        flapped = dict(stable, notifiers_install=fp.TIMEOUT)
        before = snap.read_text()
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=flapped):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "0 change(s)" in out, out
        assert "notifiers_install installed ->" not in out, out
        # Still surfaced today, from the RAW probe, even with no commit.
        assert "mjolnir08  notifiers=timeout" in out, out
        assert snap.read_text() == before, \
            "a timeout over a real value must not change what is on disk"

    def test_broken_is_never_overridden(self, fp):
        # broken/missing are real, persistent classifier outcomes, not
        # transient failures -- they must stand even over a real old value.
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install="installed")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install="broken")}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == "broken"

    def test_missing_is_never_overridden(self, fp):
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install="installed")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install="missing")}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == "missing"

    def test_a_first_reading_that_is_a_timeout_stays_blank(self, fp):
        # (b): nothing real to fall back to -- record "not yet measured",
        # not the transient reading itself, so its EVENTUAL first real
        # value still reads as a first measurement, not a change.
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"), notifiers_install="")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.TIMEOUT)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == ""

    def test_a_first_reading_that_is_unknown_also_stays_blank(self, fp):
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"), notifiers_install="")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.UNKNOWN)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == ""

    def test_timeout_and_unknown_flapping_from_blank_never_commits(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        blank = row(fp, "mjolnir08")                 # no *_install keys yet
        snap.write_text(",".join(fp.FIELDS[:12]) + "\n"
                        + ",".join(blank.values()) + "\n")
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]
        readings = [fp.TIMEOUT, fp.UNKNOWN, fp.TIMEOUT]
        for reading in readings:
            probed = dict(blank, brokkr_install="", sindri_install="",
                          hamma_install="", notifiers_install=reading)
            with patch.object(fp.sys, "argv", argv), \
                    patch.object(fp, "probe_unit", return_value=probed):
                status = fp.main()
            assert status == 0
            capsys.readouterr()
        row_line = next(l for l in snap.read_text().splitlines()
                        if l.startswith("mjolnir08,"))
        assert row_line.split(",")[-1] == "", \
            "a field that has never had a real value must stay blank"

    def test_flapping_between_timeout_and_unknown_with_a_real_history_steadies(
            self, fp):
        # (c): once a field is already sitting in a degraded state (from an
        # earlier real -> unknown transition -- see the lost-interpreter
        # test above), a further timeout/unknown reading does not overwrite
        # it either way -- it is the SAME standing problem, not a new one.
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install=fp.UNKNOWN)}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.TIMEOUT)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == fp.UNKNOWN
        assert fp.diff(prev, merged) == []

    def test_a_real_value_arriving_after_a_blank_is_not_steadied_away(
            self, fp):
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"), notifiers_install="")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install="installed")}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == "installed"
        assert fp.diff(prev, merged) == []    # first measurement, no change

    def test_m20_broken_then_timeout_is_held_by_rule_a_not_overwritten(
            self, fp):
        # Red-team round 4, mutant M20: `broken` IS a real value for rule
        # (a)'s purposes -- a timeout over it must steady back to
        # `broken`, not be stored (and must not commit either).
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install="broken")}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.TIMEOUT)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == "broken"
        assert fp.diff(prev, merged) == []

    def test_m5_timeout_then_unknown_is_held_by_rule_c(self, fp):
        # Red-team round 4, mutant M5: a field already sitting in
        # `timeout` (a genuine prior degraded state, not a placeholder --
        # see merge()'s `never_measured`) does not flip to `unknown`.
        prev = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                  notifiers_install=fp.TIMEOUT)}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.UNKNOWN)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == fp.TIMEOUT
        assert fp.diff(prev, merged) == []


class TestFirstSeenTransientDoesNotFabricateALaterChange:
    """Red-team round 4: a degraded (timeout/unknown) *_install reading
    with no GENUINE prior measurement behind it must never be STORED as
    that degraded value -- only as "" (not yet measured) -- or its
    eventual first real value looks like a fake recovery later.
    """

    def test_a_brand_new_unit_first_probed_with_a_timeout_stays_blank(
            self, fp):
        # No row in `previous` at all.
        prev = {}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.TIMEOUT)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == ""

    def test_a_brand_new_unit_does_not_fabricate_a_change_the_next_day(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]

        # Day 1: mjolnir08 has never been seen before, and its first
        # *_install reading is a transient timeout.
        day1 = dict(row(fp, "mjolnir08"), notifiers_install=fp.TIMEOUT)
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=day1):
            assert fp.main() == 0
        capsys.readouterr()

        # Day 2: a real reading. Must be a first measurement, not a
        # reported "timeout -> editable" recovery.
        day2 = dict(row(fp, "mjolnir08"), notifiers_install="editable")
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=day2):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "notifiers_install" not in out, out

    def test_an_unreachable_placeholder_returning_with_unknown_stays_blank(
            self, fp):
        # The placeholder merge() itself creates for a brand-new,
        # unreachable unit: front_end stored as `unreachable`, every
        # field (including *_install) at the UNKNOWN default.
        placeholder = {f: fp.UNKNOWN for f in fp.FIELDS}
        placeholder["unit"] = "mjolnir08"
        placeholder["front_end"] = fp.UNREACHABLE
        prev = {"mjolnir08": placeholder}
        cur = {"mjolnir08": dict(row(fp, "mjolnir08"),
                                 notifiers_install=fp.UNKNOWN)}
        merged = fp.merge(prev, cur)
        assert merged["mjolnir08"]["notifiers_install"] == ""

    def test_a_placeholder_does_not_fabricate_a_recovery_later(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]

        # Day 1: mjolnir08 is unreachable on its very first probe --
        # merge() stores the all-UNKNOWN placeholder.
        day1 = dict(row(fp, "mjolnir08"), front_end=fp.UNREACHABLE)
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=day1):
            assert fp.main() == 0
        capsys.readouterr()

        # Day 2: reachable now, but the classifier itself is transient.
        day2 = dict(row(fp, "mjolnir08"), notifiers_install=fp.UNKNOWN)
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=day2):
            assert fp.main() == 0
        capsys.readouterr()

        # Day 3: a real reading. Must not read as `unknown -> editable`,
        # a "lost interpreter recovered" event that never happened --
        # this unit was never confirmed to import in the first place.
        day3 = dict(row(fp, "mjolnir08"), notifiers_install="editable")
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=day3):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "notifiers_install" not in out, out


# --------------------------------------------------------------------------
# Red-team finding 2 (SHOULD FIX): a unit that is broken/missing on its
# FIRST measurement is never reported by diff() (a brand-new row is one
# "first seen" line, not a per-field change), so it needs its own standing
# digest line, like "not capturing".
# --------------------------------------------------------------------------
class TestInstallProblemsStandingLine:
    def test_a_first_measurement_broken_package_is_listed(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                  notifiers_install="broken")}
        text = fp.digest(rows, [], [], baseline=False)
        assert "install problems:" in text
        assert "mjolnir42  notifiers=broken" in text

    def test_missing_is_also_listed(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                  brokkr_install="missing")}
        text = fp.digest(rows, [], [], baseline=False)
        assert "mjolnir42  brokkr=missing" in text

    def test_real_values_are_not_listed(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                  notifiers_install="installed")}
        text = fp.digest(rows, [], [], baseline=False)
        assert "install problems" not in text

    def test_an_unreachable_unit_is_not_listed(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"), front_end=fp.UNREACHABLE,
                                  notifiers_install="broken")}
        text = fp.digest(rows, [], ["mjolnir42"], baseline=False)
        assert "install problems" not in text

    def test_scoped_to_probed_units(self, fp):
        # Not probed this run (a -p/-a subset) -- must not surface a stale
        # reading as if it were today's finding.
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"), notifiers_install="broken"),
               "mjolnir02": row(fp, "mjolnir02")}
        text = fp.digest(rows, [], [], baseline=False,
                         probed_units={"mjolnir02"})
        assert "install problems" not in text

    def test_multiple_bad_packages_on_one_unit_are_both_named(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"), brokkr_install="missing",
                                  notifiers_install="broken")}
        text = fp.digest(rows, [], [], baseline=False)
        line = next(l for l in text.splitlines() if l.startswith("  mjolnir42"))
        assert "brokkr=missing" in line and "notifiers=broken" in line

    def test_unknown_is_also_listed(self, fp):
        # Round 3 merge-blocking finding 1(d): unknown is a real problem
        # (a lost interpreter), not swept under the rug like a timeout.
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                  notifiers_install=fp.UNKNOWN)}
        text = fp.digest(rows, [], [], baseline=False)
        assert "mjolnir42  notifiers=unknown" in text

    def test_timeout_is_also_listed(self, fp):
        rows = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                  notifiers_install=fp.TIMEOUT)}
        text = fp.digest(rows, [], [], baseline=False)
        assert "mjolnir42  notifiers=timeout" in text

    def test_built_from_current_not_the_merged_stored_row(self, fp):
        # Round 3 finding 1(d): the stored/merged row can have a STEADIED
        # real value (merge() restoring a timeout -- see merge()), but
        # "install problems" must still see today's RAW reading.
        stored = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                    notifiers_install="installed")}
        probed_today = {"mjolnir42": dict(row(fp, "mjolnir42"),
                                          notifiers_install=fp.TIMEOUT)}
        text = fp.digest(stored, [], [], baseline=False, current=probed_today)
        assert "mjolnir42  notifiers=timeout" in text
        assert "installed" not in text.split("install problems:")[-1]

    def test_caps_a_long_list(self, fp):
        # Red-team round 4 item 3: capped like pending/behind -- an
        # over-long digest is REJECTED, not truncated, by the chat sender.
        rows = {"mjolnir{:02d}".format(n): dict(
                    row(fp, "mjolnir{:02d}".format(n)),
                    notifiers_install="broken")
               for n in range(25)}
        text = fp.digest(rows, [], [], baseline=False)
        body = [ln for ln in text.splitlines() if ln.startswith("  mjolnir")]
        assert len(body) == fp.INSTALL_PROBLEMS_DIGEST_MAX
        assert "... and 5 more" in text

    def test_cap_boundary_at_exactly_the_max_shows_no_more_line(self, fp):
        n = fp.INSTALL_PROBLEMS_DIGEST_MAX
        rows = {"mjolnir{:02d}".format(i): dict(
                    row(fp, "mjolnir{:02d}".format(i)),
                    notifiers_install="broken")
               for i in range(n)}
        text = fp.digest(rows, [], [], baseline=False)
        assert "more" not in text


# --------------------------------------------------------------------------
# The import classifier (HAM-227) -- a package that is installed but fails
# to import must be `broken`, not `missing`. Run the embedded python itself
# under a real interpreter, against controlled modules, rather than check
# REMOTE by substring: that is how the notifiers crash case went unnoticed.
# --------------------------------------------------------------------------
class TestInstallClassifierPy:
    def _run(self, fp, pkg, env=None):
        src = fp.INSTALL_CLASSIFIER_PY.format(pkg=pkg)
        result = subprocess.run([sys.executable, "-c", src],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, universal_newlines=True, timeout=15)
        return result.stdout.strip()

    def test_missing_package_prints_missing(self, fp):
        assert self._run(fp, "definitely_not_a_real_pkg_xyz") == "missing"

    def test_package_that_raises_on_import_prints_broken(self, fp, tmp_path):
        # The notifiers-2026-06 case: find_spec() succeeds (it's on disk and
        # importable by name) but the module body raises.
        pkg_dir = tmp_path / "brokenpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("raise RuntimeError('boom')\n")
        env = dict(os.environ, PYTHONPATH=str(tmp_path))
        assert self._run(fp, "brokenpkg", env=env) == "broken"

    def test_importable_package_prints_its_realpath(self, fp, tmp_path):
        pkg_dir = tmp_path / "goodpkg"
        pkg_dir.mkdir()
        init = pkg_dir / "__init__.py"
        init.write_text("")
        env = dict(os.environ, PYTHONPATH=str(tmp_path))
        assert self._run(fp, "goodpkg", env=env) == os.path.realpath(str(init))

    def test_realpath_resolves_a_symlinked_checkout(self, fp, tmp_path):
        # A dropped realpath() would report the symlink target's own path
        # instead of the real location, which can look like a different
        # install mode than the one that actually runs.
        real_pkg = tmp_path / "actual" / "goodpkg"
        real_pkg.mkdir(parents=True)
        (real_pkg / "__init__.py").write_text("")
        linked = tmp_path / "linked"
        linked.mkdir()
        (linked / "goodpkg").symlink_to(real_pkg)
        env = dict(os.environ, PYTHONPATH=str(linked))
        assert self._run(fp, "goodpkg", env=env) == os.path.realpath(
            str(real_pkg / "__init__.py"))


# --------------------------------------------------------------------------
# The install-mode case statement (HAM-227) -- exercised directly under bash
# with a crafted $F/$PKG, so a broadened glob or a swapped branch is caught
# even though no real venv is on PATH.
# --------------------------------------------------------------------------
class TestClassifyInstallModeSh:
    def _classify(self, fp, pkg, f_value):
        script = ("PKG='{}'\nF='{}'\n".format(pkg, f_value)
                  + fp.CLASSIFY_INSTALL_MODE_SH + "echo \"$I\"\n")
        result = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True,
                                timeout=15)
        return result.stdout.strip()

    def test_checkout_path_is_editable(self, fp):
        assert self._classify(
            fp, "sindri", "/home/pi/dev/sindri/sindri/__init__.py") == "editable"

    def test_similarly_named_sibling_checkout_is_not_editable(self, fp):
        # Guards against a broadened glob (e.g. "/home/pi/dev/$PKG*" instead
        # of "/home/pi/dev/$PKG/*"), which would also match this sibling.
        assert self._classify(
            fp, "sindri", "/home/pi/dev/sindri-old/foo.py") == "unknown"

    def test_site_packages_path_is_installed_not_editable(self, fp):
        assert self._classify(
            fp, "sindri",
            "/home/pi/dev/sindrienv/lib/python3.7/site-packages/sindri/"
            "__init__.py") == "installed"

    def test_missing_and_broken_pass_through_unchanged(self, fp):
        assert self._classify(fp, "sindri", "missing") == "missing"
        assert self._classify(fp, "sindri", "broken") == "broken"


# --------------------------------------------------------------------------
# The RC/timeout handling around each interpreter call (HAM-227 re-check
# finding 4): a hung interpreter must read as `timeout`, not the same
# `unknown` as a missing interpreter or a crash, and only the classifier's
# LAST output line is read, so a warning printed before it can't be
# mistaken for the real answer.
# --------------------------------------------------------------------------
class TestInstallProbeExitCodeMapping:
    # macOS (this dev machine) ships no GNU `timeout`; the units run Linux,
    # where it's part of coreutils. This minimal POSIX shim reproduces just
    # the behavior INSTALL_PROBE depends on (a bounded child, SIGTERM on
    # expiry, exit 124), so the test exercises the real $RC-mapping logic in
    # INSTALL_PROBE rather than skipping on this platform.
    _TIMEOUT_SHIM = (
        "#!/bin/sh\n"
        't=$1; shift\n'
        '"$@" &\n'
        "pid=$!\n"
        '( sleep "$t"; kill -TERM "$pid" 2>/dev/null ) &\n'
        "watcher=$!\n"
        'wait "$pid"\n'
        "rc=$?\n"
        'kill "$watcher" 2>/dev/null\n'
        'wait "$watcher" 2>/dev/null\n'
        "if [ $rc -eq 143 ]; then exit 124; fi\n"
        "exit $rc\n")

    def test_rc_124_is_timeout_other_failures_are_unknown_last_line_wins(
            self, fp, tmp_path):
        ltgenv_bin = tmp_path / "ltgenv" / "bin"
        sindrienv_bin = tmp_path / "sindrienv" / "bin"
        shim_bin = tmp_path / "shim"
        ltgenv_bin.mkdir(parents=True)
        sindrienv_bin.mkdir(parents=True)
        shim_bin.mkdir(parents=True)
        # One fake interpreter per env; it inspects its own arguments (one
        # of which embeds the real package name) to behave differently per
        # package, so one script stands in for all three ltgenv calls.
        # "$*", not a fixed positional slot: real INSTALL_PROBE passes
        # `-I -c <script>`, so the script text itself is $3, not $2.
        (ltgenv_bin / "python3").write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *brokkr*) sleep 5 ;;\n"                      # killed -> 124
            "  *notifiers*) exit 7 ;;\n"                    # crash -> unknown
            "  *) printf 'a warning\\nnot the answer\\n"
            "/x/site-packages/hamma/__init__.py\\n' ;;\n"   # last line wins
            "esac\n")
        (ltgenv_bin / "python3").chmod(0o755)
        (sindrienv_bin / "python3").write_text("#!/bin/sh\necho missing\n")
        (sindrienv_bin / "python3").chmod(0o755)
        if shutil.which("timeout") is None:
            (shim_bin / "timeout").write_text(self._TIMEOUT_SHIM)
            (shim_bin / "timeout").chmod(0o755)
            path = str(shim_bin) + os.pathsep + os.environ["PATH"]
        else:
            path = os.environ["PATH"]

        script = (fp.INSTALL_PROBE
                  .replace("/home/pi/dev", str(tmp_path))
                  .replace("timeout 10", "timeout 1"))
        result = subprocess.run(
            ["bash", "-c", script], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=30,
            env=dict(os.environ, PATH=path))
        out = dict(line.split("=", 1) for line in result.stdout.splitlines())
        assert out["brokkr_install"] == "timeout"
        assert out["notifiers_install"] == "unknown"
        assert out["hamma_install"] == "installed"     # last line, not the
                                                        # warning before it
        assert out["sindri_install"] == "missing"


# --------------------------------------------------------------------------
# Branch and dirty parsing (HAM-205/227 re-check finding 2) -- RELEASE_EXTRA_
# FIELDS is seeded by probe_unit() regardless of ssh outcome, and the real
# bash fragment that reads them is exercised against a real temp git repo,
# not just checked by substring.
# --------------------------------------------------------------------------
class TestReleaseExtraFieldsSeeding:
    def test_unreachable_probe_still_has_the_release_extra_fields(self, fp):
        # Dropping the RELEASE_EXTRA_FIELDS seeding in probe_unit() must fail
        # this test: an unreachable unit would otherwise come back with no
        # mjolnir_hamma_branch/_dirty keys at all.
        with patch.object(fp.subprocess, "run",
                          side_effect=OSError("no route")):
            r = fp.probe_unit(2)
        assert r["front_end"] == fp.UNREACHABLE
        for field in fp.RELEASE_EXTRA_FIELDS:
            assert field in r
            assert r[field] == fp.UNKNOWN

    def test_a_reachable_probe_with_no_matching_output_still_has_them(self, fp):
        run = MagicMock(returncode=0, stdout="front_end=on\n", stderr="")
        with patch.object(fp.subprocess, "run", return_value=run):
            r = fp.probe_unit(2)
        for field in fp.RELEASE_EXTRA_FIELDS:
            assert field in r
            assert r[field] == fp.UNKNOWN

    def test_probe_parses_branch_and_dirty(self, fp):
        out = ("front_end=on\n"
               "mjolnir_hamma_branch=test/noise-diag-rsync\n"
               "mjolnir_hamma_dirty=dirty\n")
        run = MagicMock(returncode=0, stdout=out, stderr="")
        with patch.object(fp.subprocess, "run", return_value=run):
            r = fp.probe_unit(3)
        assert r["mjolnir_hamma_branch"] == "test/noise-diag-rsync"
        assert r["mjolnir_hamma_dirty"] == "dirty"


class TestBranchAndDirtyBashFragment:
    """The two `echo mjolnir_hamma_*=...` lines straight out of fp.REMOTE,
    run under real bash against a real temp repo -- not a reimplementation.
    """

    @staticmethod
    def _fragment(fp, repo_path):
        start = fp.REMOTE.index("\nR=/home/pi/dev/mjolnir-hamma") + 1
        end = fp.REMOTE.index("\nfor pair in brokkr:ltgenv")
        return fp.REMOTE[start:end].replace(
            "/home/pi/dev/mjolnir-hamma", str(repo_path))

    def _probe(self, fp, repo_path):
        script = self._fragment(fp, repo_path)
        result = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True,
                                timeout=15)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def _git(self, repo, *args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)

    def _repo(self, tmp_path):
        repo = tmp_path / "mjolnir-hamma"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "test@test")
        self._git(repo, "config", "user.name", "test")
        self._git(repo, "checkout", "-q", "-b", "0.4.x")
        (repo / "f").write_text("1\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "init")
        return repo

    def test_clean_checkout_on_a_named_branch(self, fp, tmp_path):
        repo = self._repo(tmp_path)
        out = self._probe(fp, repo)
        assert out == {"mjolnir_hamma_branch": "0.4.x",
                       "mjolnir_hamma_dirty": "clean"}

    def test_dirty_working_tree(self, fp, tmp_path):
        repo = self._repo(tmp_path)
        (repo / "f").write_text("2\n")
        out = self._probe(fp, repo)
        assert out == {"mjolnir_hamma_branch": "0.4.x",
                       "mjolnir_hamma_dirty": "dirty"}

    def test_detached_head(self, fp, tmp_path):
        repo = self._repo(tmp_path)
        self._git(repo, "checkout", "-q", "--detach", "HEAD")
        out = self._probe(fp, repo)
        # git prints the literal string "HEAD" for a detached checkout --
        # pinned here so a future change to this expectation is deliberate.
        assert out == {"mjolnir_hamma_branch": "HEAD",
                       "mjolnir_hamma_dirty": "clean"}

    def test_missing_repo_is_unknown_not_clean(self, fp, tmp_path):
        # A failed `git status` must not read as a clean tree.
        out = self._probe(fp, tmp_path / "does-not-exist")
        assert out == {"mjolnir_hamma_branch": "unknown",
                       "mjolnir_hamma_dirty": "unknown"}

    def test_uses_no_optional_locks(self, fp):
        # The bare flag also appears in an explanatory comment, which would
        # make a substring check on just "--no-optional-locks" pass even if
        # the real invocation dropped it. Pin the actual command instead.
        assert "--no-optional-locks status --porcelain" in fp.REMOTE


# --------------------------------------------------------------------------
# Behind the release head (HAM-205) -- standing, like "not capturing"
# --------------------------------------------------------------------------
class TestReleaseHead:
    def test_reads_head_with_ls_remote(self, fp):
        out = "52bb35d0123456789abcdef0123456789abcdef0\trefs/heads/0.4.x\n"
        run = MagicMock(returncode=0, stdout=out, stderr="")
        with patch.object(fp.subprocess, "run", return_value=run) as call:
            head = fp.release_head("0.4.x")
        assert head == "52bb35d0123456789abcdef0123456789abcdef0"
        cmd = call.call_args[0][0]
        assert cmd[:2] == ["git", "ls-remote"]
        assert "refs/heads/0.4.x" in cmd

    def test_failure_returns_none_not_raise(self, fp):
        with patch.object(fp.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("git", 30)):
            assert fp.release_head("0.4.x") is None
        run = MagicMock(returncode=128, stdout="", stderr="fatal")
        with patch.object(fp.subprocess, "run", return_value=run):
            assert fp.release_head("0.4.x") is None

    def test_a_typo_branch_name_returncode_0_empty_stdout_is_none(self, fp):
        # ls-remote on a branch that doesn't exist exits 0 with nothing on
        # stdout -- not a nonzero return. Both must read as "can't check".
        run = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(fp.subprocess, "run", return_value=run):
            assert fp.release_head("0.4.xx-typo") is None

    def test_release_remote_constant_is_the_mjolnir_hamma_repo(self, fp):
        # Pins the literal so a mutated RELEASE_REMOTE value is caught, not
        # just that *some* string gets passed through.
        assert fp.RELEASE_REMOTE == "https://github.com/hamma-dev/mjolnir-hamma.git"

    def test_default_remote_is_the_module_constant(self, fp):
        run = MagicMock(returncode=0, stdout="abc\trefs/heads/0.4.x\n", stderr="")
        with patch.object(fp.subprocess, "run", return_value=run) as call:
            fp.release_head("0.4.x")
        cmd = call.call_args[0][0]
        assert cmd[2] == "https://github.com/hamma-dev/mjolnir-hamma.git"

    def test_ls_remote_has_a_timeout(self, fp):
        run = MagicMock(returncode=0, stdout="abc\trefs/heads/0.4.x\n", stderr="")
        with patch.object(fp.subprocess, "run", return_value=run) as call:
            fp.release_head("0.4.x")
        assert call.call_args.kwargs.get("timeout") == 30


class TestBehindRelease:
    HEAD = "52bb35d0123456789abcdef0123456789abcdef0"

    def test_lists_reachable_units_not_at_head(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", mh="52bb35d"),
                "mjolnir08": row(fp, "mjolnir08", mh="a6147a1"),
                "mjolnir05": row(fp, "mjolnir05", mh="0bdbf68")}
        out = fp.behind_release(rows, set(rows), ["mjolnir05"], self.HEAD)
        assert out == [("mjolnir08", "a6147a1", fp.UNKNOWN, fp.UNKNOWN)]

    def test_unknown_sha_is_listed(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", mh=fp.UNKNOWN)}
        assert fp.behind_release(rows, set(rows), [], self.HEAD) == [
            ("mjolnir02", fp.UNKNOWN, fp.UNKNOWN, fp.UNKNOWN)]

    def test_blank_sha_is_listed_not_treated_as_at_head(self, fp):
        # A blank CSV cell must not be read as "starts with everything".
        rows = {"mjolnir02": row(fp, "mjolnir02", mh="")}
        out = fp.behind_release(rows, set(rows), [], self.HEAD)
        assert out == [("mjolnir02", "", fp.UNKNOWN, fp.UNKNOWN)]

    def test_a_too_short_sha_is_never_treated_as_at_head(self, fp):
        # Red-team finding 5: head.startswith(sha) accepts any prefix,
        # including a 1-character one that trivially "starts" the head.
        rows = {"mjolnir02": row(fp, "mjolnir02", mh=self.HEAD[0])}
        out = fp.behind_release(rows, set(rows), [], self.HEAD)
        assert out == [("mjolnir02", self.HEAD[0], fp.UNKNOWN, fp.UNKNOWN)]

    def test_a_seven_char_sha_at_head_is_not_listed(self, fp):
        # The boundary itself must still work: a real `--short` sha (>= 7
        # chars, the git default) that IS a real prefix is still "at head".
        rows = {"mjolnir02": row(fp, "mjolnir02", mh=self.HEAD[:7])}
        assert fp.behind_release(rows, set(rows), [], self.HEAD) == []

    def test_restricted_to_the_probed_set_not_all_known_rows(self, fp):
        # A --ports/--array subset run must not report units it never
        # touched this run, even though they're still in the merged rows.
        rows = {"mjolnir02": row(fp, "mjolnir02", mh="52bb35d"),
                "mjolnir08": row(fp, "mjolnir08", mh="a6147a1")}
        out = fp.behind_release(rows, {"mjolnir02"}, [], self.HEAD)
        assert out == []

    def test_branch_and_dirty_are_carried_through(self, fp):
        r = dict(row(fp, "mjolnir08", mh="a6147a1"),
                 mjolnir_hamma_branch="feature/x", mjolnir_hamma_dirty="dirty")
        rows = {"mjolnir08": r}
        out = fp.behind_release(rows, set(rows), [], self.HEAD)
        assert out == [("mjolnir08", "a6147a1", "feature/x", "dirty")]

    def test_digest_section_is_standing(self, fp):
        rows = {"mjolnir08": row(fp, "mjolnir08", mh="a6147a1")}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "not at 0.4.x head (52bb35d):" in text
        assert "a6147a1  mjolnir08" in text

    def test_digest_includes_branch_and_dirty_when_known(self, fp):
        r = dict(row(fp, "mjolnir08", mh="a6147a1"),
                 mjolnir_hamma_branch="0.3.x", mjolnir_hamma_dirty="dirty")
        rows = {"mjolnir08": r}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "branch=0.3.x" in text
        assert "dirty" in text

    def test_digest_says_detached_not_branch_equals_head(self, fp):
        # Red-team finding 5: `rev-parse --abbrev-ref HEAD` prints the
        # literal string "HEAD" for a detached checkout -- "branch=HEAD"
        # reads as a bug, not a state. Say "detached" instead.
        r = dict(row(fp, "mjolnir08", mh="a6147a1"),
                 mjolnir_hamma_branch="HEAD", mjolnir_hamma_dirty="clean")
        text = fp.digest({"mjolnir08": r}, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "detached" in text
        assert "branch=HEAD" not in text

    def test_digest_shows_unknown_not_a_blank_column_for_a_blank_sha(self, fp):
        # Red-team finding 5: a blank CSV cell must not print as an empty
        # column in the grouped line.
        r = row(fp, "mjolnir08", mh="")
        text = fp.digest({"mjolnir08": r}, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        line = next(l for l in text.splitlines() if "mjolnir08" in l)
        assert line.split()[0] == fp.UNKNOWN, line

    def test_digest_silent_when_all_at_head(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", mh="52bb35d")}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "not at 0.4.x head" not in text

    def test_digest_says_so_when_head_unreadable(self, fp):
        # Never silently drop the section: "nobody behind" and "couldn't
        # check" must not look the same.
        rows = {"mjolnir02": row(fp, "mjolnir02")}
        text = fp.digest(rows, [], [], baseline=False, release=("0.4.x", None))
        assert "not at 0.4.x head: could not read the 0.4.x head" in text

    def test_digest_without_release_is_unchanged(self, fp):
        rows = {"mjolnir02": row(fp, "mjolnir02", mh="a6147a1")}
        assert "not at" not in fp.digest(rows, [], [], baseline=False)

    def test_units_sharing_a_state_are_grouped_on_one_line(self, fp):
        # HAM-205/227 re-check finding 6: with rollouts held, every unit is
        # behind every day, which made the old per-unit listing a permanent
        # wall of text. Units with the SAME (branch, sha, dirty) collapse.
        rows = {"mjolnir41": dict(row(fp, "mjolnir41", mh="a6147a1"),
                                  mjolnir_hamma_branch="0.3.x",
                                  mjolnir_hamma_dirty="clean"),
                "mjolnir03": dict(row(fp, "mjolnir03", mh="a6147a1"),
                                  mjolnir_hamma_branch="0.3.x",
                                  mjolnir_hamma_dirty="clean")}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        body = [ln for ln in text.splitlines() if ln.startswith("  a6147a1")]
        assert len(body) == 1, text
        assert "mjolnir03" in body[0] and "mjolnir41" in body[0]
        assert "2 unit(s) total" in text

    def test_same_branch_and_sha_but_different_dirty_are_separate_groups(
            self, fp):
        # The grouping key is the full (branch, sha, dirty) triple -- a
        # clean and a dirty checkout at the same SHA are different states
        # and must not be merged into one line.
        rows = {"mjolnir41": dict(row(fp, "mjolnir41", mh="a6147a1"),
                                  mjolnir_hamma_branch="0.3.x",
                                  mjolnir_hamma_dirty="clean"),
                "mjolnir03": dict(row(fp, "mjolnir03", mh="a6147a1"),
                                  mjolnir_hamma_branch="0.3.x",
                                  mjolnir_hamma_dirty="dirty")}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        body = [ln for ln in text.splitlines() if ln.startswith("  a6147a1")]
        assert len(body) == 2, text
        assert "2 unit(s) total" in text

    def test_behind_digest_max_is_ten(self, fp):
        # Pins the literal, not just self-consistency with the constant --
        # a changed cap must fail this test even though the other cap tests
        # read fp.BEHIND_DIGEST_MAX dynamically.
        assert fp.BEHIND_DIGEST_MAX == 10

    def test_digest_caps_distinct_states_not_units(self, fp):
        # 15 units, each its OWN state (distinct sha), mirrors
        # PENDING_DIGEST_MAX's reasoning: an over-long digest is REJECTED,
        # not truncated, by the chat sender. The cap is on GROUP lines.
        rows = {"mjolnir{:02d}".format(n): row(
                    fp, "mjolnir{:02d}".format(n), mh="sha{:02d}".format(n))
               for n in range(15)}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        body = [ln for ln in text.splitlines()
               if ln.startswith("  sha")]
        assert len(body) == fp.BEHIND_DIGEST_MAX
        assert "... and 5 more state(s)" in text
        assert "15 unit(s) total" in text

    def test_digest_cap_boundary_at_exactly_the_max_shows_no_more_line(self, fp):
        # HAM-205/227 re-check finding 5: at exactly BEHIND_DIGEST_MAX
        # distinct states, there must be no "... and 0 more" line.
        n = fp.BEHIND_DIGEST_MAX
        rows = {"mjolnir{:02d}".format(i): row(
                    fp, "mjolnir{:02d}".format(i), mh="sha{:02d}".format(i))
               for i in range(n)}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "more state(s)" not in text
        assert "{} unit(s) total".format(n) in text


class TestMainReportsRelease:
    def _main(self, fp, argv, head):
        probed = dict(row(fp, "mjolnir08", mh="a6147a1"))
        with patch.object(fp.sys, "argv", ["fleet_probe.py"] + argv), \
                patch.object(fp, "parse_startup_state", lambda text: {}), \
                patch.object(fp, "probe_unit", return_value=probed), \
                patch.object(fp, "read_snapshot", return_value={}), \
                patch.object(fp, "release_head", return_value=head) as rh:
            status = fp.main()
        return status, rh

    def test_probe_run_reads_the_release_head(self, fp, capsys):
        head = "52bb35d0123456789abcdef0123456789abcdef0"
        status, rh = self._main(fp, ["-p", "8", "--dry-run"], head)
        assert status == 0
        rh.assert_called_once_with("0.4.x")
        assert "not at 0.4.x head (52bb35d):" in capsys.readouterr().out

    def test_release_branch_is_configurable(self, fp, capsys):
        _, rh = self._main(fp, ["-p", "8", "--dry-run",
                                "--release-branch", "0.5.x"], None)
        rh.assert_called_once_with("0.5.x")
        assert "not at 0.5.x head: could not read" in capsys.readouterr().out

    def test_subset_run_does_not_report_units_it_never_probed(self, fp, tmp_path,
                                                               capsys):
        # A --ports run must restrict "not at head" to units THIS run
        # actually contacted, not every unit the snapshot has ever known.
        head = "52bb35d0123456789abcdef0123456789abcdef0"
        snap = tmp_path / "fleet-state.csv"
        snap.write_text(fp.render(
            {"mjolnir02": row(fp, "mjolnir02", mh="a6147a1")}))
        probed = dict(row(fp, "mjolnir08", mh="a6147a1"))
        argv = ["fleet_probe.py", "-p", "8", "--dry-run",
               "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "parse_startup_state", lambda text: {}), \
                patch.object(fp, "probe_unit", return_value=probed), \
                patch.object(fp, "release_head", return_value=head):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "mjolnir08" in out       # probed this run, and behind
        assert "mjolnir02" not in out   # known from the snapshot, not probed


# --------------------------------------------------------------------------
# Schema migration through main() (HAM-227) -- an old 12-column snapshot
# must pick up the *_install columns even when nothing else changed. Before
# the fix, diff() correctly skips them (they're a schema addition, not a
# change) but main() only writes when `baseline or changes`, so the old
# header -- and therefore the new fields -- never reached disk.
# --------------------------------------------------------------------------
class TestSchemaMigrationThroughMain:
    def test_upgrade_then_a_real_flip_reports_exactly_that_change(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        old_row = row(fp, "mjolnir08")                     # 12-column row
        snap.write_text(",".join(fp.FIELDS[:12]) + "\n"
                        + ",".join(old_row.values()) + "\n")

        probed = dict(old_row, **{f: "installed" for f in INSTALL_FIELDS})
        argv = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=probed), \
                patch.object(fp, "release_head", return_value=None):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        assert "1 units, 0 change(s)" in out          # no spurious lines
        for field in INSTALL_FIELDS:
            assert field not in out
        header = snap.read_text().splitlines()[0]
        assert header == ",".join(fp.FIELDS)           # schema reached disk

        # Second run: flip one install field -> exactly that change reported.
        flipped = dict(probed, notifiers_install="editable")
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=flipped), \
                patch.object(fp, "release_head", return_value=None):
            status2 = fp.main()
        assert status2 == 0
        out2 = capsys.readouterr().out
        assert "1 units, 1 change(s)" in out2
        assert "mjolnir08  notifiers_install installed -> editable" in out2


# --------------------------------------------------------------------------
# Unreachable or subset-skipped at the moment of a schema upgrade (HAM-205/
# 227 re-check finding 1, HIGH): a carried-forward row that lacks the new
# columns must not fabricate "unknown", which would read as a real old
# value and fire a false change the day the unit is finally re-measured.
# --------------------------------------------------------------------------
class TestUpgradeDoesNotFabricateChanges:
    def test_unreachable_at_upgrade_then_back_reports_no_install_changes(
            self, fp, tmp_path, capsys):
        snap = tmp_path / "fleet-state.csv"
        old_row = row(fp, "mjolnir02")                      # pre-upgrade, 12 cols
        snap.write_text(",".join(fp.FIELDS[:12]) + "\n"
                        + ",".join(old_row.values()) + "\n")
        argv = ["fleet_probe.py", "-p", "2", "--snapshot", str(snap)]

        # Day 1: the upgrade lands, but the unit happens to be unreachable.
        unreachable = dict(old_row, front_end=fp.UNREACHABLE)
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=unreachable), \
                patch.object(fp, "release_head", return_value=None):
            assert fp.main() == 0
        capsys.readouterr()
        header = snap.read_text().splitlines()[0]
        assert header == ",".join(fp.FIELDS), "schema must still reach disk"

        # Day 2: the unit answers again, with real install values.
        probed = dict(old_row, **{f: "installed" for f in INSTALL_FIELDS})
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=probed), \
                patch.object(fp, "release_head", return_value=None):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        for field in INSTALL_FIELDS:
            assert field not in out, out

    def test_skipped_by_a_subset_run_at_upgrade_then_probed(self, fp, tmp_path,
                                                             capsys):
        snap = tmp_path / "fleet-state.csv"
        rows = {"mjolnir02": row(fp, "mjolnir02"),
               "mjolnir08": row(fp, "mjolnir08")}
        snap.write_text(",".join(fp.FIELDS[:12]) + "\n"
                        + "\n".join(",".join(r.values())
                                   for r in rows.values()) + "\n")

        # Day 1: upgraded code runs a subset probe (-p 2) that never touches
        # mjolnir08 at all -- not even "unreachable", simply not probed.
        probed_02 = dict(rows["mjolnir02"],
                         **{f: "installed" for f in INSTALL_FIELDS})
        argv_day1 = ["fleet_probe.py", "-p", "2", "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv_day1), \
                patch.object(fp, "probe_unit", return_value=probed_02), \
                patch.object(fp, "release_head", return_value=None):
            assert fp.main() == 0
        capsys.readouterr()
        header = snap.read_text().splitlines()[0]
        assert header == ",".join(fp.FIELDS)

        # Day 2: mjolnir08 is finally probed for real.
        probed_08 = dict(rows["mjolnir08"],
                         **{f: "editable" for f in INSTALL_FIELDS})
        argv_day2 = ["fleet_probe.py", "-p", "8", "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv_day2), \
                patch.object(fp, "probe_unit", return_value=probed_08), \
                patch.object(fp, "release_head", return_value=None):
            status = fp.main()
        assert status == 0
        out = capsys.readouterr().out
        for field in INSTALL_FIELDS:
            assert field not in out, out

    def test_a_real_flip_after_a_quiet_backfill_day_reaches_disk(
            self, fp, tmp_path, capsys):
        """Red-team B1: diff() correctly treats a blank->value transition
        as "no old value to compare against" (see diff()'s INSTALL_FIELDS
        check) -- a first real measurement is not a "change" any more than
        a schema addition is, and the digest is right to stay at "0
        change(s)" for it. The actual bug was the WRITE condition: without
        `text != render(previous)`, that first real value never reached
        disk at all, so it (and everything after it, forever) kept
        comparing against the same blank "old" value. Repro: a subset-run
        schema day, a quiet full-fleet day, then the first real reading.
        A first-time `broken` reading is still surfaced -- via the
        "install problems" standing line (finding 2), not a change line.
        """
        snap = tmp_path / "fleet-state.csv"
        old = {"mjolnir01": row(fp, "mjolnir01"),
              "mjolnir41": row(fp, "mjolnir41"),
              "mjolnir42": row(fp, "mjolnir42")}
        snap.write_text(",".join(fp.FIELDS[:12]) + "\n" + "\n".join(
            ",".join(r.values()) for r in old.values()) + "\n")

        # Day 1: a subset run (stand-in for `-a hamma`) probes only
        # mjolnir01, but the schema still reaches disk for every unit.
        probed_01 = dict(old["mjolnir01"],
                         **{f: "installed" for f in INSTALL_FIELDS})
        argv_day1 = ["fleet_probe.py", "-p", "1", "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv_day1), \
                patch.object(fp, "probe_unit", return_value=probed_01):
            assert fp.main() == 0
        capsys.readouterr()
        after_day1 = snap.read_text()
        assert after_day1.splitlines()[0] == ",".join(fp.FIELDS)

        # Day 2: a full run. mjolnir41/42 are unreachable; mjolnir01 is
        # unchanged. Nothing must be written -- this day is genuinely quiet.
        def probe_day2(n):
            if n == 1:
                return probed_01
            return dict(old["mjolnir{:02d}".format(n)],
                       front_end=fp.UNREACHABLE)
        argv_day2 = ["fleet_probe.py", "-p", "1", "41", "42",
                    "--snapshot", str(snap)]
        with patch.object(fp.sys, "argv", argv_day2), \
                patch.object(fp, "probe_unit", side_effect=probe_day2):
            status2 = fp.main()
        out2 = capsys.readouterr().out
        assert status2 == 0
        assert "0 change(s)" in out2, out2
        assert snap.read_text() == after_day1, \
            "a genuinely quiet day must not touch the file"

        # Day 3: mjolnir41 and mjolnir42 are finally measured for real.
        probed_41 = dict(old["mjolnir41"], brokkr_install="", sindri_install="",
                         hamma_install="", notifiers_install="installed")
        probed_42 = dict(old["mjolnir42"], brokkr_install="", sindri_install="",
                         hamma_install="", notifiers_install="broken")

        def probe_day3(n):
            return {1: probed_01, 41: probed_41, 42: probed_42}[n]
        with patch.object(fp.sys, "argv", argv_day2), \
                patch.object(fp, "probe_unit", side_effect=probe_day3):
            status3 = fp.main()
        out3 = capsys.readouterr().out
        assert status3 == 0
        # By design (confirmed correct in finding 1): a first real
        # measurement is not a reportable "change".
        assert "0 change(s)" in out3, out3
        # But mj42's broken reading must not go unseen just because it
        # arrived with no prior value to diff against.
        assert "mjolnir42  notifiers=broken" in out3, out3
        written = snap.read_text()
        assert written != after_day1, \
            "the real flip must reach disk, not stay blank forever"
        mj41_line = next(l for l in written.splitlines()
                         if l.startswith("mjolnir41,"))
        assert mj41_line.split(",")[-4:] == ["", "", "", "installed"]


class TestPendingRespectsWhatWasActuallyProbed:
    """A scoped run (-a/-p) probes a subset. Units that were never probed are
    not in `unreachable` either, so "rows minus unreachable" counted them as
    reachable and dumped their pending items into the digest -- 46 items on a
    3-unit `-a aumma` run, hitting the truncation cap. Hand-running the probe
    scoped is exactly how an operator uses it.
    """

    def test_a_scoped_run_ignores_units_it_did_not_probe(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir09",
                      "## Pending\n\n- [ ] Something for mj09\n")
        rows = {"mjolnir41": row(fp, "mjolnir41"),
                "mjolnir09": row(fp, "mjolnir09")}
        write_profile(tmp_path, "aumma", "mjolnir41",
                      "## Pending\n\n- [ ] Something for mj41\n")
        # only aumma was probed; mjolnir09 was never contacted
        text = fp.digest(rows, [], [], repo=str(tmp_path),
                         probed_units={"mjolnir41"})
        assert "Something for mj09" not in text, \
            "a unit that was never probed must not be treated as reachable"
        assert "Something for mj41" in text, \
            "the probed unit's own pending item must still show"

    def test_main_passes_what_it_probed(self, fp, tmp_path, capsys):
        """The fix is one argument in main(); without this, dropping it (or
        passing the snapshot's units) passed every test."""
        write_profile(tmp_path, "pamma", "mjolnir09",
                      "## Pending\n\n- [ ] Something for mj09\n")
        write_profile(tmp_path, "aumma", "mjolnir41",
                      "## Pending\n\n- [ ] Something for mj41\n")
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "fleet-state.csv").write_text(fp.render(
            {"mjolnir41": row(fp, "mjolnir41"),
             "mjolnir09": row(fp, "mjolnir09")}))
        argv = ["fleet_probe", "--allow-missing-ags", "-p", "41",
                "--repo", str(tmp_path)]
        with patch.object(fp, "parse_startup_state", None), \
                patch.object(fp, "load_ags_parser",
                             return_value=(None, None)), \
                patch.object(fp, "probe_unit",
                             lambda n: row(fp, "mjolnir{:02d}".format(n))), \
                patch.object(fp.sys, "argv", argv):
            assert fp.main() == 0
        out = capsys.readouterr().out
        assert "Something for mj41" in out, out
        assert "Something for mj09" not in out, \
            "main() did not scope the pending block to what it probed"

    def test_an_unscoped_run_still_shows_everything_reachable(self, fp, tmp_path):
        write_profile(tmp_path, "pamma", "mjolnir09",
                      "## Pending\n\n- [ ] Something for mj09\n")
        rows = {"mjolnir09": row(fp, "mjolnir09")}
        text = fp.digest(rows, [], [], repo=str(tmp_path))
        assert "Something for mj09" in text


# --------------------------------------------------------------------------
# Publishing the snapshot: pull before the write, and fail loudly
# --------------------------------------------------------------------------

# Policy (decided 2026-10-31): the probe only ever fast-forwards. It never
# rebases or merges unattended. When it cannot publish it keeps the day's
# snapshot as a local commit and says so in the digest, with a recovery command
# a human (as monitor) can paste. Every test here uses real git; none mocks it.

@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """Keep the developer's own git config (pull.ff, init.defaultBranch, hooks)
    out of the tests, so they behave as on the VPS's bare config."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _git(repo, *args, check=True):
    """Run git in `repo`; raise on failure so a broken fixture is obvious."""
    done = subprocess.run(("git", "-C", str(repo)) + args,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=60)
    if check:
        assert done.returncode == 0, done.stdout
    return done.stdout


@pytest.fixture
def clone_pair(tmp_path):
    """A bare 'origin' plus a clone, as the VPS has, plus a second clone
    ('work') standing in for humans committing to sensor-log.

    Avoids `init -b` (git >= 2.28) so the fixture also runs on git 2.25.
    Returns (clone, work, snapshot_rel).
    """
    origin, work, clone = (tmp_path / "origin.git", tmp_path / "work",
                           tmp_path / "clone")
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(tmp_path, "init", "-q", str(work))
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    for repo in (work,):
        _git(repo, "config", "user.email", "human@test")
        _git(repo, "config", "user.name", "human")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "README").write_text("sensor-log\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")
    _git(work, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "probe@test")
    _git(clone, "config", "user.name", "probe")
    return clone, work, pathlib.Path("state") / "fleet-state.csv"


def _human_commit(work, name="other.txt", text="human edit\n"):
    """A sensor-log commit made somewhere other than the probe's clone."""
    _git(work, "pull", "-q", "--ff-only", "origin", "main")
    path = work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "a human commit")
    _git(work, "push", "-q", "origin", "main")


def _head(repo, ref="HEAD"):
    return _git(repo, "rev-parse", ref).strip()


def _origin(clone):
    return clone.parent / "origin.git"


def _published(clone, rel):
    """The snapshot as GitHub would show it."""
    return _git(_origin(clone), "show", "main:" + str(rel), check=False)


def _run_main(fp, clone, extra=(), front_end="on", pull=None):
    """Run fp.main() against `clone` with one fake unit (mjolnir02). Only the
    fleet and chat are faked; git is real. Returns (rc, digest text)."""
    argv = (["fleet_probe", "--allow-missing-ags", "-p", "2",
             "--repo", str(clone)] + list(extra))
    sent = {}
    patches = [patch.object(fp, "parse_startup_state", None),
               patch.object(fp, "load_ags_parser", return_value=(None, None)),
               patch.object(fp, "probe_unit",
                            lambda n: row(fp, "mjolnir{:02d}".format(n),
                                          front_end=front_end)),
               patch.object(fp, "send_digest",
                            lambda text, key_file, channel:
                                sent.__setitem__("text", text)),
               # main() always reads the release head; nothing here cares
               # about it, and leaving it unpatched would make every test
               # using this helper hit github.com for real.
               patch.object(fp, "release_head", return_value=None),
               patch.object(fp.sys, "argv", argv)]
    if pull is not None:
        patches.append(patch.object(fp, "pull_snapshot", pull))
    for p in patches:
        p.start()
    try:
        rc = fp.main()
    finally:
        for p in reversed(patches):
            p.stop()
    return rc, sent.get("text", "")


PUBLISH = ["--commit", "--notify"]


def _strand_a_commit(fp, clone, rel):
    """Day 1 publishes a baseline; day 2's state change is committed but its
    push fails (origin unreachable), leaving one commit stranded locally."""
    assert _run_main(fp, clone, PUBLISH)[0] == 0
    url = _git(clone, "remote", "get-url", "origin").strip()
    _git(clone, "remote", "set-url", "origin", url + "-gone")
    rc, _ = _run_main(fp, clone, PUBLISH, front_end="off")
    _git(clone, "remote", "set-url", "origin", url)
    assert rc != 0
    assert "front_end" in _git(clone, "show", "HEAD:" + str(rel))


class TestPublishingTheNormalDay:

    def test_a_human_commit_then_a_change_publishes(self, fp, clone_pair):
        """The case that used to fail: sensor-log took a human commit, then
        the probe saw a change. It must fast-forward, commit and push."""
        clone, work, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _human_commit(work)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert _head(_origin(clone), "main") == _head(clone)
        assert "mjolnir02,off," in _published(clone, rel)
        assert "***" not in text, text

    def test_an_unchanged_day_leaves_no_commit(self, fp, clone_pair):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        before = _head(clone)
        rc, _ = _run_main(fp, clone, PUBLISH)
        assert rc == 0
        assert _head(clone) == before

    def test_only_the_snapshot_is_committed(self, fp, clone_pair):
        """`git commit -m` without a path swept a pre-staged file into the
        'state:' commit and pushed it."""
        clone, _, rel = clone_pair
        (clone / "secret.txt").write_text("not for the record\n")
        _git(clone, "add", "secret.txt")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        tree = _git(clone, "ls-tree", "-r", "--name-only", "HEAD")
        assert str(rel) in tree
        assert "secret.txt" not in tree, "a staged file rode along: " + tree

    def test_the_pull_happens_before_the_baseline_is_read(
            self, fp, clone_pair):
        """A pull after read_snapshot() would diff against a baseline it had
        just made stale."""
        clone, _, _ = clone_pair
        order = []
        real_pull, real_read = fp.pull_snapshot, fp.read_snapshot

        def spy_pull(repo):
            order.append("pull")
            return real_pull(repo)

        def spy_read(path):
            order.append("read")
            return real_read(path)

        with patch.object(fp, "read_snapshot", spy_read):
            _run_main(fp, clone, PUBLISH, pull=spy_pull)
        assert order[:2] == ["pull", "read"], order

    def test_problems_reach_the_log_even_without_the_digest(
            self, fp, clone_pair, capsys):
        """If the chat send fails, the cron log is all that is left."""
        clone, _, _ = clone_pair
        _git(clone, "checkout", "-q", "-b", "foo")
        _run_main(fp, clone, ["--commit"])
        err = capsys.readouterr().err
        assert "not on main" in err and "exit status 1" in err, err

    def test_untracked_files_in_the_clone_are_not_staged(
            self, fp, clone_pair):
        clone, _, _ = clone_pair
        (clone / "scratch.txt").write_text("someone's notes\n")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        assert _git(clone, "status", "--porcelain", "scratch.txt").startswith(
            "??"), "the probe staged a file it does not own"

    def test_exit_status_is_logged(self, fp, clone_pair, capsys):
        """Cron's log drops $?; the run must say how it ended itself."""
        clone, _, _ = clone_pair
        _run_main(fp, clone, PUBLISH)
        assert "exit status 0" in capsys.readouterr().err


class TestFastForwardOnly:

    def test_pull_never_merges_even_where_git_defaults_to_merge(
            self, fp, clone_pair):
        """git 2.25 (the VPS) merges on a plain `git pull` of a diverged
        clone; pull.rebase=false reproduces that on newer git."""
        clone, work, rel = clone_pair
        _git(clone, "config", "pull.rebase", "false")
        _human_commit(work)
        (clone / "local.txt").write_text("local\n")
        _git(clone, "add", "local.txt")
        _git(clone, "commit", "-q", "-m", "local")
        before = _head(clone)
        ok, detail, fetched = fp.pull_snapshot(str(clone))
        assert not ok, "a diverged clone was reconciled: " + detail
        assert fetched, "the fetch itself succeeded"
        assert _head(clone) == before
        assert not (clone / "other.txt").exists()

    def test_pull_failure_from_an_exception_is_a_value(self, fp, tmp_path):
        """A timeout must not escape: the digest is the liveness signal."""
        with patch.object(fp.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("git", 1)):
            ok, detail, fetched = fp.pull_snapshot(str(tmp_path))
        assert not ok and detail and not fetched

    def test_without_commit_the_clone_history_is_not_moved(
            self, fp, clone_pair):
        clone, work, _ = clone_pair
        _human_commit(work)
        before = _head(clone)
        _run_main(fp, clone)
        assert _head(clone) == before

    def test_dry_run_does_not_pull(self, fp, clone_pair):
        clone, work, _ = clone_pair
        _human_commit(work)
        before = _head(clone)
        _run_main(fp, clone, ["--commit", "--dry-run"])
        assert _head(clone) == before


class TestNothingStaysUnpublishedSilently:

    def test_a_refused_pull_commits_locally_and_says_so(self, fp, clone_pair):
        """Skipping the write dropped the day from history. Commit locally,
        push nothing, exit non-zero, and put the banner FIRST."""
        clone, work, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        _human_commit(work)                  # now the clone has diverged
        origin_before = _head(_origin(clone), "main")
        local_before = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="on")
        assert rc != 0
        assert _head(clone, "HEAD~1") == local_before, "day not committed"
        assert _head(_origin(clone), "main") == origin_before, "pushed"
        assert text.startswith("*** THE FLEET RECORD IS NOT BEING PUBLISHED")
        assert "not on GitHub: 2" in text, text
        assert "push FAILED" not in text, "pushed after a refused pull"
        assert "as of the last" not in text, "the fetch did succeed: " + text

    def test_a_stranded_commit_is_pushed_on_a_quiet_day(self, fp, clone_pair):
        """A transient push failure, then a day with no changes: nothing was
        written, so nothing was pushed -- exit 0 and no banner, for weeks."""
        clone, _, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert _head(_origin(clone), "main") == _head(clone)
        assert "mjolnir02,off," in _published(clone, rel)

    def test_an_uncommitted_snapshot_is_committed_next_run(
            self, fp, clone_pair):
        """A failed commit (or a hand run without --commit) leaves the new
        state in the working tree. The next run sees no change against that
        file, so deciding by 'changes' never committed it."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _run_main(fp, clone, front_end="off")            # hand run, no commit
        assert _git(clone, "status", "--porcelain", "--", str(rel)).strip()
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert "mjolnir02,off," in _published(clone, rel)
        assert not _git(clone, "status", "--porcelain", "--", str(rel)).strip()

    def test_a_failed_commit_is_reported_then_recovered(self, fp, clone_pair):
        """A stale index.lock makes the commit fail. That must reach the
        digest, and the next run must commit what was left behind."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        lock = clone / ".git" / "index.lock"
        lock.write_text("")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "commit FAILED" in text, text
        assert any(step.startswith("rm ") and step.endswith("index.lock")
                   for step in _fix_steps(text)), "no lock step: " + text
        assert text.startswith("*** THE FLEET RECORD IS NOT BEING PUBLISHED"), \
            "today's state is unpublished; wrong heading: " + text
        assert "fetch origin" not in text, "GitHub was fine: " + text
        assert "remove the file manually" not in text, \
            "git boilerplate instead of the error: " + text
        lock.unlink()
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert "mjolnir02,off," in _published(clone, rel)

    def test_a_lost_push_race_is_reported(self, fp, clone_pair):
        """Origin moves between the pull and the push: the commit stays local
        and the rejection reaches the digest."""
        clone, work, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        real_pull = fp.pull_snapshot

        def pull_then_race(repo):
            result = real_pull(repo)
            _human_commit(work, "raced.txt")
            return result

        rc, text = _run_main(fp, clone, PUBLISH, front_end="off",
                             pull=pull_then_race)
        assert rc != 0
        assert "push FAILED" in text, text
        assert "mjolnir02,off," in _git(clone, "show", "HEAD:" + str(rel))
        assert "not on GitHub: 1" in text, text

    def test_an_unreachable_origin_is_flagged_as_possibly_stale(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "not on GitHub: 1 (as of the last successful fetch)" in text, \
            text
        assert "next run retries" in text, text

    def test_a_missing_origin_main_still_counts_as_unpublished(
            self, fp, clone_pair, capsys):
        """unpushed() is None without origin/main; that must not read as
        'nothing unpublished' when a commit could not be pushed."""
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "NOT BEING PUBLISHED" in text, text
        assert "None" not in capsys.readouterr().err

    def test_an_unreachable_origin_with_nothing_pending_is_not_misreported(
            self, fp, clone_pair):
        """Origin unreachable on a quiet day: nothing is unpublished, so the
        digest must not claim the state is missing from GitHub."""
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert text.startswith(
            "*** COULD NOT REFRESH THE SENSOR-LOG CLONE ***"), text
        assert "Recover (as monitor)" not in text, \
            "offered RECOVER with nothing to recover: " + text
        assert "make sure you have the correct access rights" not in text, \
            "git boilerplate instead of the error: " + text
        assert "next run retries" in text, text


class TestRecovery:

    def test_the_recovery_hint_actually_recovers(self, fp, clone_pair,
                                                 tmp_path):
        """Run the pasted command from another directory; origin must then
        hold everything the clone had stacked."""
        clone, work, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        _human_commit(work)
        _, text = _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert "Recover (as monitor)" in text, text
        backout = [l for l in text.splitlines() if "rebase --abort" in l]
        assert len(backout) == 1 and backout[0].endswith("rebase --abort"), \
            "the back-out command must end its line (no trailing comma)"
        steps = _fix_steps(text)
        assert steps == [fp.RECOVER.format(repo=clone)], steps
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _run_steps(steps, elsewhere)
        assert _head(_origin(clone), "main") == _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert rc == 0, text

    def _conflicted_recover(self, clone, work, rel, fp):
        """A human edits the snapshot itself; RECOVER then stops mid-rebase."""
        _strand_a_commit(fp, clone, rel)
        _human_commit(work, str(rel), "unit,front_end\nmjolnir02,hand\n")
        done = subprocess.run(
            fp.RECOVER.format(repo=clone), shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=60)
        assert done.returncode != 0, "expected a conflict: " + done.stdout

    def test_a_clone_mid_rebase_is_not_written_or_committed(
            self, fp, clone_pair):
        """The next run used to parse conflict markers as units, commit them
        inside the rebase, and repeat a RECOVER that could no longer work."""
        clone, work, rel = clone_pair
        self._conflicted_recover(clone, work, rel, fp)
        head, on_disk = _head(clone), (clone / rel).read_text()
        rc, text = _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert rc != 0
        assert _head(clone) == head, "committed into an in-progress rebase"
        assert (clone / rel).read_text() == on_disk, "wrote over a conflict"
        assert "middle of a rebase" in text and "by hand" in text, text
        assert "Recover (as monitor)" not in text, "offered a dead RECOVER"
        assert "<<<<" not in text, "conflict markers reached the digest"

    def test_a_clone_off_main_is_not_published_elsewhere(
            self, fp, clone_pair):
        """`push origin HEAD` from another branch created that branch on
        GitHub and reported success; main never got the snapshot."""
        clone, _, _ = clone_pair
        _git(clone, "checkout", "-q", "-b", "foo")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        branches = _git(_origin(clone), "branch", "--list")
        assert "foo" not in branches, branches
        assert "not on main" in text and "by hand" in text, text


class TestReadSnapshotIsDefensive:

    def test_rows_that_are_not_units_are_ignored(self, fp, tmp_path):
        snap = tmp_path / "fleet-state.csv"
        snap.write_text(fp.render({"mjolnir02": row(fp, "mjolnir02")})
                        + "<<<<<<< HEAD\n=======\n>>>>>>> abc\n")
        assert set(fp.read_snapshot(str(snap))) == {"mjolnir02"}

    def test_a_file_without_a_unit_column_is_empty_not_a_crash(
            self, fp, tmp_path):
        snap = tmp_path / "fleet-state.csv"
        snap.write_text("name,state\nx,y\n")
        assert fp.read_snapshot(str(snap)) == {}


def _fix_steps(text):
    """The '$ ' command lines of a banner, in order."""
    return [l.split("$ ", 1)[1] for l in text.splitlines()
            if l.lstrip().startswith("$ ")]


def _run_steps(steps, cwd):
    for step in steps:
        done = subprocess.run(step, shell=True, cwd=str(cwd),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)
        assert done.returncode == 0, "{!r} failed: {}".format(step, done.stdout)


def _start_conflict(clone, work, rel, fp, operation):
    """Leave the clone in the middle of `operation` on the snapshot file."""
    assert _run_main(fp, clone, PUBLISH)[0] == 0
    _git(clone, "checkout", "-q", "-b", "side")
    (clone / rel).write_text((clone / rel).read_text() + "side\n")
    _git(clone, "commit", "-q", "-am", "side")
    _git(clone, "checkout", "-q", "main")
    (clone / rel).write_text((clone / rel).read_text() + "main\n")
    _git(clone, "commit", "-q", "-am", "main")
    if operation == "merge":
        _git(clone, "merge", "side", check=False)
    elif operation == "cherry-pick":
        _git(clone, "cherry-pick", "side", check=False)
    elif operation == "revert":
        # Reverting "main" conflicts once a later commit rewrote its line;
        # two commits make it a sequence (REVERT_HEAD plus sequencer/).
        (clone / rel).write_text((clone / rel).read_text()
                                 .replace("main\n", "main2\n"))
        _git(clone, "commit", "-q", "-am", "main2")
        _git(clone, "revert", "--no-edit", "HEAD~1", "HEAD", check=False)
        git_dir = pathlib.Path(_git(clone, "rev-parse",
                                    "--absolute-git-dir").strip())
        assert (git_dir / "REVERT_HEAD").exists(), "revert did not stop"
    elif operation == "am":
        patch_file = clone.parent / "side.patch"
        patch_file.write_text(_git(clone, "format-patch", "-1", "--stdout",
                                   "side"))
        _git(clone, "am", str(patch_file), check=False)
    elif operation == "bisect":
        _git(clone, "bisect", "start", "--no-checkout")
    elif operation == "sequencer":
        # Resolving the first pick by hand and committing leaves sequencer/
        # with no CHERRY_PICK_HEAD: the operation's own marker is gone.
        _git(clone, "checkout", "-q", "side")
        (clone / "more.txt").write_text("more\n")
        _git(clone, "add", "more.txt")
        _git(clone, "commit", "-q", "-m", "side2")
        _git(clone, "checkout", "-q", "main")
        _git(clone, "cherry-pick", "side~1", "side", check=False)
        (clone / rel).write_text("unit,front_end\nmjolnir02,hand\n")
        _git(clone, "add", str(rel))
        _git(clone, "commit", "-q", "--no-edit")
        git_dir = pathlib.Path(_git(clone, "rev-parse",
                                    "--absolute-git-dir").strip())
        assert (git_dir / "sequencer").exists()
        assert not (git_dir / "CHERRY_PICK_HEAD").exists()
    elif operation == "rebase-from-side":
        # Aborting returns to `side`, so the steps must also check out main.
        _git(clone, "checkout", "-q", "side")
        _git(clone, "rebase", "main", check=False)
    # Resolve and stage any conflict, as a human halfway through would.
    if _git(clone, "diff", "--name-only", "--diff-filter=U").strip():
        (clone / rel).write_text("unit,front_end\nmjolnir02,hand\n")
        _git(clone, "add", str(rel))


class TestEveryInProgressOperationBlocks:
    """A probe commit inside someone's half-finished operation, or a push of
    it, is never right. Each needs its own way out, so the banner names it."""

    @pytest.mark.parametrize("operation,named,abort", [
        ("merge", "merge", "merge --abort"),
        ("cherry-pick", "cherry-pick", "cherry-pick --abort"),
        ("revert", "revert", "revert --abort"),
        ("am", "git am", "am --abort"),
        ("bisect", "bisect", "bisect reset"),
        ("sequencer", "cherry-pick/revert sequence", "cherry-pick --abort"),
        ("rebase-from-side", "rebase", "rebase --abort"),
    ])
    def test_blocks_and_gives_a_working_way_out(
            self, fp, clone_pair, tmp_path, operation, named, abort):
        clone, work, rel = clone_pair
        _start_conflict(clone, work, rel, fp, operation)
        head = _head(clone)
        origin_before = _head(_origin(clone), "main")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert _head(clone) == head, "committed inside the " + operation
        assert _head(_origin(clone), "main") == origin_before, "pushed"
        assert named in text and abort in text, text
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _run_steps(_fix_steps(text), elsewhere)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, "the printed steps did not clear it: " + text

    def test_a_detached_head_is_never_pushed_to_main(self, fp, clone_pair,
                                                    tmp_path):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "x.txt").write_text("x\n")
        _git(clone, "add", "x.txt")
        _git(clone, "commit", "-q", "-m", "x")
        _git(clone, "checkout", "-q", "--detach", "HEAD")
        origin_before = _head(_origin(clone), "main")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert _head(_origin(clone), "main") == origin_before
        assert "detached" in text, text
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _run_steps(_fix_steps(text), elsewhere)
        assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0

    def test_unresolved_conflicts_alone_still_block(self, fp, clone_pair):
        """Unmerged index entries with the operation's marker file gone."""
        clone, work, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "checkout", "-q", "-b", "side")
        (clone / rel).write_text("side\n")
        _git(clone, "commit", "-q", "-am", "side")
        _git(clone, "checkout", "-q", "main")
        (clone / rel).write_text("main\n")
        _git(clone, "commit", "-q", "-am", "main")
        _git(clone, "merge", "side", check=False)
        git_dir = pathlib.Path(_git(clone, "rev-parse",
                                    "--absolute-git-dir").strip())
        (git_dir / "MERGE_HEAD").unlink()
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0 and _head(clone) == head
        assert "unresolved" in text, text
        _run_steps(_fix_steps(text), clone.parent)
        assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0

    def test_an_uninspectable_clone_blocks(self, fp, tmp_path):
        assert fp.clone_blocker(str(tmp_path)) is not None
        with patch.object(fp, "_git", side_effect=OSError("boom")):
            assert fp.clone_blocker(str(tmp_path)) is not None

    def test_the_off_main_steps_work(self, fp, clone_pair, tmp_path):
        clone, _, _ = clone_pair
        _git(clone, "checkout", "-q", "-b", "foo")
        _, text = _run_main(fp, clone, PUBLISH)
        assert "checkout main" in text, text
        assert "rebase --abort" not in text, "irrelevant advice: " + text
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _run_steps(_fix_steps(text), elsewhere)
        assert _run_main(fp, clone, PUBLISH)[0] == 0


class TestDigestWhileBlocked:

    def test_the_diff_is_against_what_github_has(self, fp, clone_pair):
        """Mid-operation the working file is not the published record; the
        digest used to say '0 change(s)' or 'baseline established'."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0      # GitHub: on
        _git(clone, "checkout", "-q", "-b", "foo")
        _run_main(fp, clone, front_end="off")             # local file: off
        _git(clone, "commit", "-q", "-am", "local only")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "1 change(s)" in text, text
        assert "baseline" not in text, text


class TestCommitMessages:

    def _last_message(self, clone):
        return _git(clone, "log", "-1", "--format=%s").strip()

    def test_a_change_left_by_a_hand_run_is_named(self, fp, clone_pair):
        """The message comes from what is actually committed, not from the
        day's diff, which is empty when the file already holds the change."""
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _run_main(fp, clone, front_end="off")            # hand run
        assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0
        assert self._last_message(clone) == "state: mjolnir02"

    def test_a_baseline_is_named(self, fp, clone_pair):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        assert self._last_message(clone) == "state: fleet baseline"

    def test_a_dirty_unrelated_file_does_not_trigger_a_commit(
            self, fp, clone_pair):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "README").write_text("edited by hand\n")
        head = _head(clone)
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        assert _head(clone) == head


class TestSnapshotMustBeTracked:

    def test_a_gitignored_snapshot_is_reported(self, fp, clone_pair):
        clone, work, _ = clone_pair
        _human_commit(work, ".gitignore", "state/\n")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert "not tracked" in text, text
        assert text.startswith("*** THE FLEET RECORD IS NOT BEING PUBLISHED"), text
        assert "check-ignore" in text and "fetch origin" not in text, text

    def test_an_assume_unchanged_snapshot_is_reported(self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "update-index", "--assume-unchanged", str(rel))
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "assume-unchanged" in text, text

    def test_a_first_run_commit_failure_is_not_called_gitignored(
            self, fp, clone_pair):
        clone, _, _ = clone_pair
        (clone / ".git" / "index.lock").write_text("")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert "commit FAILED" in text and "not tracked" not in text, text

    def test_a_skip_worktree_snapshot_is_reported(self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "update-index", "--skip-worktree", str(rel))
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "skip-worktree" in text, text


class TestGitIsNonInteractive:

    def test_no_stdin_and_no_prompt(self, fp, tmp_path):
        seen = {}

        def fake_run(*args, **kwargs):
            seen.update(kwargs)
            return MagicMock(returncode=0, stdout="")

        with patch.object(fp.subprocess, "run", fake_run):
            fp._git(str(tmp_path), "status")
        assert seen["stdin"] is subprocess.DEVNULL
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"



class TestUncommittedEditsInTheClone:

    def test_a_conflicting_hand_edit_keeps_the_day_and_a_working_way_out(
            self, fp, clone_pair, tmp_path):
        """A hand edit to a file origin then changes made --ff-only refuse
        every day, and RECOVER refused too ('unstaged changes'). The day is
        still committed locally; the steps set the edit aside first."""
        clone, work, _ = clone_pair
        _human_commit(work, "deployments/mj02.md", "v1\n")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "deployments" / "mj02.md").write_text("edited on the VPS\n")
        _human_commit(work, "deployments/mj02.md", "v2\n")
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert _head(clone, "HEAD~1") == head, "the day was dropped"
        assert "deployments/mj02.md" in text, "which file? " + text
        assert _fix_steps(text)[0].endswith("stash"), text
        _run_steps(_fix_steps(text), tmp_path)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert "edited on the VPS" in _git(clone, "stash", "show", "-p")

    def test_an_uncommitted_snapshot_on_a_diverged_clone_is_not_stashed(
            self, fp, clone_pair):
        """The snapshot is the probe's own file: a leftover change to it is
        committed locally (then RECOVER), never stashed away."""
        clone, work, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        _human_commit(work)
        _run_main(fp, clone, front_end="on")              # hand run, dirty
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="on")
        assert rc != 0
        assert "stash" not in text, text
        assert _head(clone, "HEAD~1") == head, "not committed locally"
        assert _fix_steps(text) == [fp.RECOVER.format(repo=clone)], text

    def test_off_main_with_an_edited_snapshot_can_still_be_fixed(
            self, fp, clone_pair, tmp_path):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "checkout", "-q", "-b", "side")
        (clone / rel).write_text("unit,front_end\nmjolnir02,side\n")
        _git(clone, "commit", "-q", "-am", "side")
        (clone / rel).write_text("unit,front_end\nmjolnir02,dirty\n")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        _run_steps(_fix_steps(text), tmp_path)
        assert _run_main(fp, clone, PUBLISH)[0] == 0


class TestRobustness:

    def test_brief_keeps_the_error_not_the_boilerplate(self, fp):
        out = ("fatal: Unable to create '/r/.git/index.lock': File exists.\n"
               "\n"
               "Another git process seems to be running in this repository\n"
               "may have crashed in this repository earlier:\n"
               "remove the file manually to continue.\n")
        assert fp._brief(out).startswith("fatal: Unable to create"), \
            fp._brief(out)
        ssh = ("ssh: connect to host github.com port 22: Operation timed out\n"
               "fatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\n"
               "and the repository exists.\n")
        assert "timed out" in fp._brief(ssh), fp._brief(ssh)

    def test_an_unreadable_snapshot_does_not_stop_the_digest(
            self, fp, clone_pair):
        """csv.Error (a NUL byte on Python 3.8; an over-long field on any
        version) escaped read_snapshot and killed the run before the digest."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / rel).write_text("unit,front_end\nmjolnir02," + "x" * 200000
                                 + "\n")
        _git(clone, "commit", "-q", "-am", "corrupt")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert text, "no digest was sent"
        assert rc != 0 and "unreadable" in text, text
        assert "mjolnir02,off," in _published(clone, rel), \
            "the next good snapshot was not published"

    def test_the_snapshot_write_is_atomic(self, fp, clone_pair):
        """A crash mid-write must leave the old file, not a truncated one."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        before = (clone / rel).read_text()
        with patch.object(fp.os, "replace", side_effect=OSError("crash")):
            with pytest.raises(OSError):
                _run_main(fp, clone, PUBLISH, front_end="off")
        assert (clone / rel).read_text() == before

    def test_a_narrowed_fetch_refspec_still_refreshes_origin_main(
            self, fp, clone_pair):
        clone, work, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(clone, "config", "remote.origin.fetch",
             "+refs/heads/other:refs/remotes/origin/other")
        _human_commit(work)
        assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0
        assert (clone / "other.txt").exists(), "origin/main went stale"

    def test_a_relative_repo_prints_absolute_paths(self, fp, clone_pair,
                                                   monkeypatch):
        clone, _, _ = clone_pair
        _git(clone, "checkout", "-q", "-b", "foo")
        monkeypatch.chdir(str(clone.parent))
        argv_repo = "clone"
        _, text = _run_main(fp, argv_repo, PUBLISH)
        assert "git -C {} ".format(clone) in text, text


class TestPublishApi:
    """The steps main() uses, callable by another writer (#98) so it does not
    re-implement them: prepare_clone() before reading the baseline,
    publish_snapshot() after writing."""

    def test_prepare_then_publish(self, fp, clone_pair):
        clone, work, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _human_commit(work)
        state = fp.prepare_clone(str(clone), str(rel))
        assert state["pulled"] and not state["problems"], state
        (clone / rel).write_text((clone / rel).read_text()
                                 .replace("mjolnir02,on,", "mjolnir02,off,"))
        state = fp.publish_snapshot(state)
        assert not state["problems"] and state["ahead"] == 0, state
        assert "mjolnir02,off," in _published(clone, rel)

    def test_commit_snapshot_only_commits(self, fp, clone_pair):
        """With push folded in, (False, ...) meant either 'not committed' or
        'committed, not pushed' -- the ambiguity #98's rollback tripped on."""
        clone, _, rel = clone_pair
        (clone / rel).parent.mkdir(exist_ok=True)
        (clone / rel).write_text("unit,front_end\nmjolnir02,on\n")
        before = _head(_origin(clone), "main")
        ok, _ = fp.commit_snapshot(str(clone), str(rel), "state: x")
        assert ok
        assert _head(_origin(clone), "main") == before, "commit pushed"


def test_field_report_on_an_unreadable_snapshot_fails_cleanly(fp, tmp_path,
                                                              capsys):
    snap = tmp_path / "fleet-state.csv"
    snap.write_text("unit,front_end\nmjolnir02," + "x" * 200000 + "\n")
    with patch.object(fp.sys, "argv", ["fleet_probe", "--snapshot",
                                       str(snap), "--field", "front_end"]):
        assert fp.main() == 1
    assert "unreadable" in capsys.readouterr().err



class TestRoundFive:
    """Findings of the red team on 924571a."""

    def test_an_untracked_file_in_the_way_blocks_with_a_working_fix(
            self, fp, clone_pair, tmp_path):
        """Origin adds a file the clone has untracked: --ff-only refuses, and
        RECOVER failed too ('untracked working tree files would be
        overwritten'). status -uno never saw it."""
        clone, work, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "deployments").mkdir()
        (clone / "deployments" / "mj02.md").write_text("local notes\n")
        _human_commit(work, "deployments/mj02.md", "from origin\n")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert "deployments/mj02.md" in text, text
        _run_steps(_fix_steps(text), tmp_path)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert "local notes" in _git(clone, "stash", "show", "-p",
                                     "--include-untracked", check=False) \
            or "local notes" in _git(clone, "show", "stash@{0}^3:"
                                     "deployments/mj02.md", check=False)

    def test_an_unrelated_edit_on_a_diverged_clone_keeps_the_day(
            self, fp, clone_pair, tmp_path):
        """Divergence refused the fast-forward, not the edit. The day is
        committed locally; the edit is set aside only because RECOVER (a
        rebase) refuses unstaged changes."""
        clone, work, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        _human_commit(work)
        (clone / "README").write_text("edited by hand\n")
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="on")
        assert rc != 0
        assert _head(clone, "HEAD~1") == head, "the day was dropped"
        assert "block the fast-forward" not in text, "wrong cause: " + text
        steps = _fix_steps(text)
        assert steps[0].endswith("stash") and steps[-1] == \
            fp.RECOVER.format(repo=clone), steps
        _run_steps(steps, tmp_path)
        assert _run_main(fp, clone, PUBLISH, front_end="on")[0] == 0

    def test_a_network_failure_with_a_dirty_file_is_not_a_blocker(
            self, fp, clone_pair):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "README").write_text("edited by hand\n")
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert text.startswith("*** COULD NOT REFRESH"), text
        assert "stash" not in text and "by hand" not in text, text

    def test_an_unreadable_working_snapshot_keeps_last_known_state(
            self, fp, clone_pair):
        """previous = {} let merge() drop everything it could not re-probe:
        an unreachable unit was published as unreachable,unknown,..."""
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0
        (clone / rel).write_text("unit,front_end\nmjolnir02," + "x" * 200000
                                 + "\n")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert rc != 0 and "unreadable" in text, text
        assert "baseline" not in text, text
        assert text.startswith("*** FLEET RECORD PROBLEM ***"), text
        assert "mjolnir02,off," in _published(clone, rel)

    def test_unreadable_while_blocked_does_not_claim_a_rewrite(
            self, fp, clone_pair):
        clone, work, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _human_commit(work, str(rel), "unit,front_end\nmjolnir02,"
                      + "x" * 200000 + "\n")
        _git(clone, "fetch", "-q", "origin")
        _git(clone, "checkout", "-q", "-b", "side")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert "rewritten" not in text, text
        assert "unreadable" in text, text

    def test_brief_keeps_the_key_error_and_the_blocking_files(self, fp):
        key = ("git@github.com: Permission denied (publickey).\n"
               "fatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\n")
        assert "Permission denied (publickey)" in fp._brief(key)
        merge = ("Updating a..b\n"
                 "error: Your local changes to the following files would be "
                 "overwritten by merge:\n\tdeployments/mj02.md\n"
                 "Please commit your changes or stash them before you merge.\n"
                 "Aborting\n")
        assert "deployments/mj02.md" in fp._brief(merge), fp._brief(merge)

    def test_commit_snapshot_with_nothing_to_commit_is_not_a_failure(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        ok, detail = fp.commit_snapshot(str(clone), str(rel), "state: x")
        assert ok and "nothing" in detail, detail

    def test_a_failing_commit_is_a_failure(self, fp, clone_pair):
        """Only `add` failures were tested; the commit step itself can fail
        too (a hook, a lock taken between add and commit)."""
        clone, _, _ = clone_pair
        hook = clone / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0 and "commit FAILED" in text, text

    def test_publish_without_prepare_is_refused(self, fp, clone_pair):
        clone, _, rel = clone_pair
        with pytest.raises(ValueError):
            fp.publish_snapshot(fp.new_publish_state(str(clone), str(rel)))

    def test_a_dotted_snapshot_path_is_not_mistaken_for_a_hand_edit(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / rel).write_text((clone / rel).read_text() + "\n")
        assert fp.dirty_tracked(str(clone), "./" + str(rel)) == []
        state = fp.new_publish_state(str(clone), "./" + str(rel))
        assert state["rel"] == str(rel)

    def test_a_dirty_file_with_a_space_is_named_plainly(self, fp, clone_pair):
        clone, work, _ = clone_pair
        _human_commit(work, "a b.txt", "x\n")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "a b.txt").write_text("edited\n")
        assert fp.dirty_tracked(str(clone), "state/fleet-state.csv") == \
            ["a b.txt"]

    def test_changed_since_survives_a_corrupt_historic_snapshot(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        (clone / rel).parent.mkdir(exist_ok=True)
        (clone / rel).write_text("unit,front_end\nmjolnir02," + "x" * 200000
                                 + "\n")
        _git(clone, "add", str(rel))
        _git(clone, "commit", "-q", "-m", "corrupt")
        assert fp.changed_since(str(clone), str(rel), "mjolnir02",
                                ("on", "default")) is None

    def test_a_problem_without_publishing_gets_its_own_heading(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        (clone / rel).parent.mkdir(exist_ok=True)
        (clone / rel).write_text("unit,front_end\nmjolnir02," + "x" * 200000
                                 + "\n")
        rc, text = _run_main(fp, clone, ["--notify"])
        assert text.startswith("*** FLEET PROBE PROBLEM ***"), text
        assert "unreadable" in text


class TestRoundFiveFallbackOrder:

    def _corrupt(self, clone, rel):
        (clone / rel).write_text("unit,front_end\nmjolnir02," + "x" * 200000
                                 + "\n")

    def test_the_local_commit_is_preferred_over_github(self, fp, clone_pair):
        """HEAD holds the probe's newest state (an unpushed 'off'); GitHub
        still says 'on'. The fallback must keep the newer local state."""
        clone, _, rel = clone_pair
        _strand_a_commit(fp, clone, rel)                 # HEAD off, origin on
        self._corrupt(clone, rel)
        _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert "mjolnir02,off," in _published(clone, rel)

    def test_github_is_used_when_the_local_commit_is_corrupt(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0     # origin: on
        self._corrupt(clone, rel)
        _git(clone, "commit", "-q", "-am", "corrupt, unpushed")
        _run_main(fp, clone, PUBLISH, front_end="unreachable")
        assert "mjolnir02,on," in _published(clone, rel), \
            "last-known state lost when HEAD was unreadable"


def test_a_staged_rename_is_listed_once_by_its_new_name(fp, clone_pair):
    clone, work, _ = clone_pair
    _human_commit(work, "old.txt", "x\n")
    _human_commit(work, "keep.txt", "y\n")
    _git(clone, "pull", "-q", "--ff-only", "origin", "main")
    _git(clone, "mv", "old.txt", "new.txt")
    (clone / "keep.txt").write_text("edited\n")
    assert sorted(fp.dirty_tracked(str(clone), "state/fleet-state.csv")) == \
        ["keep.txt", "new.txt"]



class TestRoundSix:
    """Findings of the red team on 48b106a."""

    def _blocked_then_fixed(self, fp, clone, tmp_path, keep):
        (clone / "scratch.txt").write_text("unrelated, untracked\n")
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc != 0
        assert keep in text, text
        assert "discards" not in text, "a stash discards nothing: " + text
        _run_steps(_fix_steps(text), tmp_path)
        rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
        assert rc == 0, text
        assert (clone / "scratch.txt").exists(), \
            "the stash swept up an unrelated untracked file"

    def test_an_untracked_file_where_origin_renames_one_to(
            self, fp, clone_pair, tmp_path):
        """A rename shows as R, not A, so --diff-filter=A missed it."""
        clone, work, _ = clone_pair
        _human_commit(work, "notes/old.md", "x\n")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        _git(work, "pull", "-q", "--ff-only", "origin", "main")
        (work / "deployments").mkdir()
        _git(work, "mv", "notes/old.md", "deployments/mj02.md")
        _git(work, "commit", "-q", "-m", "move a profile")
        _git(work, "push", "-q", "origin", "main")
        (clone / "deployments").mkdir()
        (clone / "deployments" / "mj02.md").write_text("local notes\n")
        self._blocked_then_fixed(fp, clone, tmp_path, "deployments/mj02.md")

    def test_an_untracked_file_where_origin_adds_a_directory(
            self, fp, clone_pair, tmp_path):
        clone, work, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "deployments").write_text("a file, not a directory\n")
        _human_commit(work, "deployments/mj02.md", "x\n")
        self._blocked_then_fixed(fp, clone, tmp_path, "deployments")

    def test_an_untracked_directory_where_origin_adds_a_file(
            self, fp, clone_pair, tmp_path):
        clone, work, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "notes").mkdir()
        (clone / "notes" / "x.md").write_text("local\n")
        _human_commit(work, "notes", "a file\n")
        self._blocked_then_fixed(fp, clone, tmp_path, "notes/x.md")

    def test_unreachable_github_with_an_edit_still_stashes_before_recover(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        (clone / "README").write_text("edited by hand\n")
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        _, text = _run_main(fp, clone, PUBLISH, front_end="off")
        steps = _fix_steps(text)
        assert steps[0].endswith("stash") and \
            steps[-1] == fp.RECOVER.format(repo=clone), steps

    def test_a_refused_quiet_day_still_prints_a_step(self, fp, clone_pair,
                                                     tmp_path):
        """Nothing to commit and nothing ahead, but the fast-forward is
        refused: the banner used to give no step at all."""
        clone, work, _ = clone_pair
        _human_commit(work, "deployments/mj02.md", "v1\n")
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        (clone / "deployments" / "mj02.md").write_text("edited\n")
        _human_commit(work, "deployments/mj02.md", "v2\n")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        steps = _fix_steps(text)
        assert steps, "no way out printed: " + text
        _run_steps(steps, tmp_path)
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        assert (clone / "deployments" / "mj02.md").read_text() == "v2\n"

    def test_brief_prefers_the_cause_over_warnings(self, fp):
        out = "".join("warning: unable to unlink 'f{}': Permission denied\n"
                      .format(i) for i in range(6))
        out += "fatal: cannot do a partial commit during a merge.\n"
        brief = fp._brief(out)
        assert brief.startswith("fatal: cannot do a partial commit"), brief

    def test_brief_lists_three_files_and_counts_the_rest(self, fp):
        out = ("error: Your local changes to the following files would be "
               "overwritten by merge:\n\ta\n\tb\n\tc\n\td\n\te\nAborting\n")
        brief = fp._brief(out)
        assert all(" / {}".format(f) in brief for f in "abc"), brief
        assert "(+2 more)" in brief, brief

    def test_brief_ignores_indented_lines_without_an_error(self, fp):
        out = "Updating a..b\n\tsomething indented\nfatal: boom\n"
        assert "indented" not in fp._brief(out)


def test_an_untracked_path_with_a_space_gets_a_working_stash_step(
        fp, clone_pair, tmp_path):
    clone, work, _ = clone_pair
    assert _run_main(fp, clone, PUBLISH)[0] == 0
    (clone / "my notes.md").write_text("local\n")
    _human_commit(work, "my notes.md", "from origin\n")
    rc, text = _run_main(fp, clone, PUBLISH, front_end="off")
    assert rc != 0
    _run_steps(_fix_steps(text), tmp_path)
    assert _run_main(fp, clone, PUBLISH, front_end="off")[0] == 0


# --------------------------------------------------------------------------
# HAM-205/227 re-check findings 5 (bullet 3) and 7: the schema write goes
# through #109's existing write-if-changed + atomic write + publish
# pipeline -- nothing new -- so it is tested the same way #109's own
# commit/publish behavior is: against a real git clone_pair, never mocked.
# --------------------------------------------------------------------------
class TestSchemaWriteReachesDiskAndIsCommitted:

    def test_schema_only_change_on_a_quiet_day_is_committed_and_pushed(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        old_row = row(fp, "mjolnir02")
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                 + ",".join(old_row.values()) + "\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "pre-upgrade snapshot")
        _git(clone, "push", "-q", "origin", "main")

        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc == 0, text
        assert "0 change(s)" in text, text

        message = _git(clone, "log", "-1", "--format=%s").strip()
        # Red-team B2: a pure schema day must name no unit, even though
        # every row's raw CSV text differs once the columns are appended.
        assert message == "state: schema +{}".format(
            ",".join(INSTALL_FIELDS)), message

        header = (clone / rel).read_text().splitlines()[0]
        assert header == ",".join(fp.FIELDS), "schema must reach the working file"
        assert _published(clone, rel).splitlines()[0] == ",".join(fp.FIELDS), \
            "schema must reach GitHub, not just the local commit"

    def test_a_stable_run_writes_and_commits_nothing(self, fp, clone_pair):
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0      # establishes baseline
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH)          # an identical day
        assert rc == 0, text
        assert "0 change(s)" in text, text
        assert _head(clone) == head, \
            "a stable run must not create a commit"

    def test_a_blocked_clone_with_only_a_schema_addition_writes_nothing(
            self, fp, clone_pair):
        clone, _, rel = clone_pair
        old_row = row(fp, "mjolnir02")
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                 + ",".join(old_row.values()) + "\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "pre-upgrade snapshot")
        _git(clone, "push", "-q", "origin", "main")
        _git(clone, "checkout", "-q", "--detach", "HEAD")

        before = (clone / rel).read_text()
        head = _head(clone)
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0, text
        assert (clone / rel).read_text() == before, \
            "a blocked clone must not be written"
        assert _head(clone) == head


class TestCommitSummaryNamesSchemaAndUnits:
    """commit_summary() is given schema_added (the columns added_fields()
    found) and must say so alongside whatever units its own diff names --
    the two are independent facts about the same commit.
    """

    def _repo(self, fp, tmp_path):
        repo = tmp_path / "repo"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old = row(fp, "mjolnir02")
        (repo / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                + ",".join(old.values()) + "\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        return repo, rel, old

    def test_a_header_change_and_a_real_value_change_are_both_named(
            self, fp, tmp_path):
        repo, rel, old = self._repo(fp, tmp_path)
        new_row = dict(old, front_end="off",
                       **{f: "installed" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render({"mjolnir02": new_row}))

        message = fp.commit_summary(str(repo), rel, schema_added=INSTALL_FIELDS)
        expected = "state: schema +{}; mjolnir02".format(
            ",".join(INSTALL_FIELDS))
        assert message == expected, message

    def test_a_schema_only_change_names_no_unit(self, fp, tmp_path):
        """Red-team B2: every row's raw CSV text differs once the new
        columns are appended, but if no unit's OLD-header values actually
        moved, the message must say only "schema +...", naming nobody.
        """
        repo, rel, old = self._repo(fp, tmp_path)
        unchanged_row = dict(old, **{f: "" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render({"mjolnir02": unchanged_row}))

        message = fp.commit_summary(str(repo), rel, schema_added=INSTALL_FIELDS)
        assert message == "state: schema +{}".format(
            ",".join(INSTALL_FIELDS)), message

    def test_a_schema_day_with_a_real_change_elsewhere_names_only_that_unit(
            self, fp, tmp_path):
        """Two units get the new header; only one's old-header values moved."""
        repo = tmp_path / "repo2"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old02, old03 = row(fp, "mjolnir02"), row(fp, "mjolnir03")
        (repo / rel).write_text(
            ",".join(fp.FIELDS[:12]) + "\n"
            + ",".join(old02.values()) + "\n"
            + ",".join(old03.values()) + "\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        unchanged03 = dict(old03, **{f: "" for f in INSTALL_FIELDS})
        changed02 = dict(old02, front_end="off",
                         **{f: "" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render(
            {"mjolnir02": changed02, "mjolnir03": unchanged03}))

        message = fp.commit_summary(str(repo), rel, schema_added=INSTALL_FIELDS)
        expected = "state: schema +{}; mjolnir02".format(
            ",".join(INSTALL_FIELDS))
        assert message == expected, message

    def test_no_schema_addition_is_unchanged(self, fp, tmp_path):
        """Pins the no-schema case to exactly #109's own behavior."""
        repo, rel, old = self._repo(fp, tmp_path)
        new_row = dict(old, front_end="off")
        (repo / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                + ",".join(new_row.values()) + "\n")
        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir02"

    def test_a_quoted_comma_in_a_value_does_not_hide_a_real_change(
            self, fp, tmp_path):
        """Red-team round 3 should-fix 2 (mutant M11): a value containing
        a comma is written quoted (csv.DictWriter does this automatically
        -- see render()). The SAME quoted value on both sides contributes
        no real difference by itself, but naively splitting the raw line
        on every comma shifts every column after it by one -- on a
        schema day, where the old row is also a different WIDTH, that
        shift can make a genuinely changed column (here, `notifiers`)
        compare equal to a wrong, shifted-in column instead, hiding the
        change entirely. parse_snapshot() is real CSV, so it isn't fooled.
        """
        repo = tmp_path / "repo_comma"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old_row = dict(row(fp, "mjolnir02"), brokkr_mode="a,b")
        # Written with csv, not a plain join: a real pre-upgrade snapshot
        # (written by this same render(), just with fewer columns back
        # then) would have quoted "a,b" too. A plain join would add no
        # quote at all, which is itself a difference from the new row's
        # quoted copy and would mask what this test is isolating.
        old_buf = io.StringIO()
        csv.writer(old_buf, lineterminator="\n").writerow(
            [old_row[f] for f in fp.FIELDS[:12]])
        (repo / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                + old_buf.getvalue())
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        new_row = dict(old_row, notifiers="deadbee",      # the real change
                       **{f: "" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render({"mjolnir02": new_row}))

        message = fp.commit_summary(str(repo), rel, schema_added=INSTALL_FIELDS)
        assert message == "state: schema +{}; mjolnir02".format(
            ",".join(INSTALL_FIELDS)), message

    def test_a_hand_edited_header_missing_a_column_still_finds_the_change(
            self, fp, tmp_path):
        """A middle column dropped by hand (not a schema change -- no
        schema_added) must not hide a real change elsewhere in the row.
        """
        repo, rel, old = self._repo(fp, tmp_path)
        fields = [f for f in fp.FIELDS[:12] if f != "gain_slow"]
        new_values = dict(old, threshold_1_mv="999")
        (repo / rel).write_text(
            ",".join(fields) + "\n"
            + ",".join(new_values[f] for f in fields) + "\n")

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir02", message

    def test_a_pure_backfill_day_is_named_as_a_count_not_by_unit(self, fp,
                                                                  tmp_path):
        """Red-team round 3 should-fix 3: a day where *_install goes
        blank -> real for several units, with nothing else changed, must
        not read in the git log as a configuration change to those units
        -- the daily digest already says "0 change(s)" for exactly this
        reason (see diff()'s INSTALL_FIELDS check).
        """
        repo = tmp_path / "repo_backfill"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old04, old05 = row(fp, "mjolnir04"), row(fp, "mjolnir05")
        # Already past the schema day: both rows have the full header,
        # blank for *_install (never yet measured).
        (repo / rel).write_text(fp.render(
            {"mjolnir04": old04, "mjolnir05": old05}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        new04 = dict(old04, **{f: "installed" for f in INSTALL_FIELDS})
        new05 = dict(old05, **{f: "editable" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render(
            {"mjolnir04": new04, "mjolnir05": new05}))

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: install backfill: 2 units", message

    def test_backfill_and_a_real_change_are_both_named(self, fp, tmp_path):
        repo = tmp_path / "repo_backfill2"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old04, old05 = row(fp, "mjolnir04"), row(fp, "mjolnir05")
        (repo / rel).write_text(fp.render(
            {"mjolnir04": old04, "mjolnir05": old05}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        new04 = dict(old04, **{f: "installed" for f in INSTALL_FIELDS})
        new05 = dict(old05, front_end="off")        # a real change
        (repo / rel).write_text(fp.render(
            {"mjolnir04": new04, "mjolnir05": new05}))

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir05; install backfill: 1 units", \
            message

    def test_a_unit_with_both_a_real_change_and_a_backfill_is_named_not_counted(
            self, fp, tmp_path):
        """A unit's real change takes priority over its OWN backfill: it
        is named by unit, not folded into the backfill count, which would
        otherwise hide that specific unit's actual configuration change.
        """
        repo = tmp_path / "repo_backfill3"
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rel = "state/fleet-state.csv"
        old06 = row(fp, "mjolnir06")
        (repo / rel).write_text(fp.render({"mjolnir06": old06}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        # Both on the SAME unit: a real front_end flip, and its first
        # *_install reading (blank -> real).
        new06 = dict(old06, front_end="off",
                     **{f: "installed" for f in INSTALL_FIELDS})
        (repo / rel).write_text(fp.render({"mjolnir06": new06}))

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir06", message


class TestMutationSurvivorCoverage:
    """Round 3/4: named survivors from the coordinator's mutation review."""

    def _repo2(self, tmp_path, name="repo_m"):
        repo = tmp_path / name
        (repo / "state").mkdir(parents=True)
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return repo

    def test_m14_a_new_unit_is_named_not_called_uncommitted(self, fp,
                                                             tmp_path):
        repo = self._repo2(tmp_path)
        rel = "state/fleet-state.csv"
        (repo / rel).write_text(fp.render({"mjolnir02": row(fp, "mjolnir02")}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        # mjolnir05 appears for the first time; mjolnir02 is unchanged.
        (repo / rel).write_text(fp.render(
            {"mjolnir02": row(fp, "mjolnir02"),
             "mjolnir05": row(fp, "mjolnir05")}))

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir05", message

    def test_m14_a_removed_unit_is_also_named(self, fp, tmp_path):
        repo = self._repo2(tmp_path, "repo_m2")
        rel = "state/fleet-state.csv"
        (repo / rel).write_text(fp.render(
            {"mjolnir02": row(fp, "mjolnir02"),
             "mjolnir05": row(fp, "mjolnir05")}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        (repo / rel).write_text(fp.render({"mjolnir02": row(fp, "mjolnir02")}))

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir05", message

    def test_m10_a_blank_to_value_change_in_a_non_install_column_is_named(
            self, fp, tmp_path):
        # The backfill classification is specific to *_install: a blank
        # old value in any OTHER column is a real, named change.
        repo = self._repo2(tmp_path, "repo_m10")
        rel = "state/fleet-state.csv"
        blank_mode = dict(row(fp, "mjolnir02"), brokkr_mode="")
        (repo / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                + ",".join(blank_mode.values()) + "\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "old"],
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

        filled_mode = dict(row(fp, "mjolnir02"), brokkr_mode="default")
        (repo / rel).write_text(",".join(fp.FIELDS[:12]) + "\n"
                                + ",".join(filled_mode.values()) + "\n")

        message = fp.commit_summary(str(repo), rel)
        assert message == "state: mjolnir02", message

    def test_m9_a_missing_key_skips_any_field_not_just_install(self, fp):
        # The "field not in old" skip in diff() applies to ANY future
        # schema addition, not just *_install -- drop a non-install key
        # entirely from `old` and confirm its first real value is still
        # not reported as a change.
        old = row(fp, "mjolnir02")
        del old["brokkr_mode"]
        new = dict(row(fp, "mjolnir02"), brokkr_mode="nochargecontroller")
        assert fp.diff({"mjolnir02": old}, {"mjolnir02": new}) == []

    def test_m22_timeout_constant_matches_the_bash_literal(self, fp):
        # Ties fp.TIMEOUT to the actual bash assignment in INSTALL_PROBE,
        # not just the python-side comparisons against the constant.
        assert "I=" + fp.TIMEOUT in fp.INSTALL_PROBE


class TestShortRowDoesNotPrintNone:
    """Red-team round 3, low-priority finding 5: csv.DictReader fills a
    short row's missing trailing cells with None, not "". A bare
    `.get(field, "")` default only covers a MISSING key, not a PRESENT
    None value, so str(None) == "None" could leak into a change line.
    """

    def test_a_short_row_never_prints_a_literal_none(self, fp):
        header = ",".join(fp.FIELDS)
        full = row(fp, "mjolnir02")
        # Drop the LAST of the 12 base cells (notifiers) -- the row has
        # fewer cells than the header, so csv.DictReader fills notifiers
        # AND every *_install column (also trailing) with None, not "".
        short_row = ",".join(list(full.values())[:-1])
        old_rows = fp.parse_snapshot(
            io.StringIO(header + "\n" + short_row + "\n"))
        new = dict(full, notifiers="9999999")
        changes = fp.diff(old_rows, {"mjolnir02": new})
        for _, _, old_val, new_val in changes:
            assert old_val != "None" and new_val != "None", changes

    def test_a_short_row_reports_the_real_change_as_blank_to_value(self, fp):
        header = ",".join(fp.FIELDS)
        full = row(fp, "mjolnir02")
        short_row = ",".join(list(full.values())[:-1])
        old_rows = fp.parse_snapshot(
            io.StringIO(header + "\n" + short_row + "\n"))
        new = dict(full, notifiers="9999999")
        assert fp.diff(old_rows, {"mjolnir02": new}) == [
            ("mjolnir02", "notifiers", "", "9999999")]
