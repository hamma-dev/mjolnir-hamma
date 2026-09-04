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
