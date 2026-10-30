"""Tests for server/fleet_probe.py -- the daily fleet configuration probe.

Everything here exercises the pure functions with fabricated rows; nothing
contacts the fleet. probe_unit() is covered by mocking subprocess.run so the
stdout parser is tested without ssh.
"""

import importlib.util
import os
import pathlib
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


# --------------------------------------------------------------------------
# Install-mode fields (HAM-205) -- the git SHA is only what RUNS if the
# package is imported from the checkout. A frozen site-packages copy shadows
# it, which is how the 2026-06 notifiers ImportError hid from a git survey.
# --------------------------------------------------------------------------
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
        # One `timeout 15` inside the per-package loop body (HAM-227) -- it
        # runs once per package at runtime, so a hung interpreter can't eat
        # the whole 90s per-unit probe budget.
        assert "timeout 15 /home/pi/dev/$ENV/bin/python3" in fp.INSTALL_PROBE

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
        assert "mjolnir08  a6147a1" in text

    def test_digest_includes_branch_and_dirty_when_known(self, fp):
        r = dict(row(fp, "mjolnir08", mh="a6147a1"),
                 mjolnir_hamma_branch="0.3.x", mjolnir_hamma_dirty="dirty")
        rows = {"mjolnir08": r}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        assert "branch=0.3.x" in text
        assert "dirty" in text

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

    def test_digest_caps_a_long_list(self, fp):
        # Mirrors PENDING_DIGEST_MAX's reasoning: an over-long digest is
        # REJECTED, not truncated, by the chat sender.
        rows = {"mjolnir{:02d}".format(n): row(fp, "mjolnir{:02d}".format(n),
                                               mh="a6147a1")
               for n in range(15)}
        text = fp.digest(rows, [], [], baseline=False,
                         release=("0.4.x", self.HEAD))
        body = [ln for ln in text.splitlines() if ln.startswith("  mjolnir")]
        assert len(body) == fp.BEHIND_DIGEST_MAX
        assert "... and 5 more" in text


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

    def test_schema_only_write_uses_a_schema_commit_message(self, fp, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        snapshot_rel = os.path.join("state", "fleet-state.csv")
        (repo / "state").mkdir()
        old_row = row(fp, "mjolnir08")
        (repo / snapshot_rel).write_text(
            ",".join(fp.FIELDS[:12]) + "\n" + ",".join(old_row.values()) + "\n")

        probed = dict(old_row, **{f: "installed" for f in INSTALL_FIELDS})
        argv = ["fleet_probe.py", "-p", "8", "--repo", str(repo), "--commit"]
        with patch.object(fp.sys, "argv", argv), \
                patch.object(fp, "probe_unit", return_value=probed), \
                patch.object(fp, "release_head", return_value=None), \
                patch.object(fp, "commit_snapshot",
                             return_value=(True, "ok")) as commit:
            status = fp.main()
        assert status == 0
        message = commit.call_args[0][2]
        assert message.startswith("state: schema +")
        for field in INSTALL_FIELDS:
            assert field in message
