"""Tests for server/fleet_probe.py -- the daily fleet configuration probe.

Everything here exercises the pure functions with fabricated rows; nothing
contacts the fleet. probe_unit() is covered by mocking subprocess.run so the
stdout parser is tested without ssh.
"""

import importlib.util
import pathlib
import subprocess

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
        assert "PUBLISH" not in text.upper().split("\n", 1)[0], text

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
        ok, detail = fp.pull_snapshot(str(clone))
        assert not ok, "a diverged clone was reconciled: " + detail
        assert _head(clone) == before
        assert not (clone / "other.txt").exists()

    def test_pull_failure_from_an_exception_is_a_value(self, fp, tmp_path):
        """A timeout must not escape: the digest is the liveness signal."""
        with patch.object(fp.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("git", 1)):
            ok, detail = fp.pull_snapshot(str(tmp_path))
        assert not ok and detail

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

    def test_a_refused_pull_with_nothing_pending_is_not_misreported(
            self, fp, clone_pair):
        """Origin unreachable on a quiet day: nothing is unpublished, so the
        digest must not claim the state is missing from GitHub."""
        clone, _, _ = clone_pair
        assert _run_main(fp, clone, PUBLISH)[0] == 0
        url = _git(clone, "remote", "get-url", "origin").strip()
        _git(clone, "remote", "set-url", "origin", url + "-gone")
        rc, text = _run_main(fp, clone, PUBLISH)
        assert rc != 0
        assert "NOT BEING PUBLISHED" not in text, text
        assert "could not refresh" in text.lower(), text


class TestRecovery:

    def test_the_recovery_hint_actually_recovers(self, fp, clone_pair,
                                                 tmp_path):
        """Run the pasted command from another directory; origin must then
        hold everything the clone had stacked."""
        clone, work, rel = clone_pair
        _strand_a_commit(fp, clone, rel)
        _human_commit(work)
        _, text = _run_main(fp, clone, PUBLISH, front_end="unreachable")
        hint = [l for l in text.splitlines() if "Recover" in l]
        assert len(hint) == 1, text
        assert "monitor" in hint[0]
        assert "rebase --abort" in text, "no way out of a conflicted rebase"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        done = subprocess.run(hint[0].split(": ", 1)[1], shell=True,
                              cwd=str(elsewhere), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)
        assert done.returncode == 0, done.stdout
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
        assert "main" in text and "by hand" in text, text


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
