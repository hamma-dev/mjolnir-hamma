"""Tests for ags.py — AGS command wrapper (threshold/gain control)."""

import importlib.util
import pathlib
import subprocess

import pytest
from unittest.mock import patch, MagicMock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "ags.py"


def load_ags():
    spec = importlib.util.spec_from_file_location("ags", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ags():
    return load_ags()


class TestConversion:
    def test_mv_to_ags_known_points(self, ags):
        assert ags.mv_to_ags(830) == pytest.approx(5.0, abs=1e-3)
        assert ags.mv_to_ags(83) == pytest.approx(0.5, abs=1e-3)
        assert ags.mv_to_ags(0) == 0.0

    def test_ags_to_mv_is_inverse(self, ags):
        assert ags.ags_to_mv(5.0) == pytest.approx(830, abs=1.0)
        assert ags.ags_to_mv(0.5) == pytest.approx(83, abs=1.0)

    def test_negative_mv_rejected(self, ags):
        with pytest.raises(ValueError):
            ags.mv_to_ags(-1)


class TestSetThreshold:
    def test_sends_das_set_threshold(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            reply = ags.set_threshold(1, 830)
        sent = mock_send.call_args[0][0]
        toks = sent.split()
        assert toks[0] == "das_set_threshold"
        assert toks[1] == "1"
        assert float(toks[2]) == pytest.approx(5.0, abs=1e-3)
        assert reply == "OK"

    def test_channel_2(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_threshold(2, 83)
        toks = mock_send.call_args[0][0].split()
        assert toks[1] == "2"
        assert float(toks[2]) == pytest.approx(0.5, abs=1e-3)

    def test_rejects_bad_channel(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_threshold(3, 83)
            mock_send.assert_not_called()

    def test_high_mv_sent_without_cap(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_threshold(1, 5000)  # far above nominal full-scale
        toks = mock_send.call_args[0][0].split()
        assert float(toks[2]) == pytest.approx(ags.mv_to_ags(5000), abs=1e-3)


class TestSetGain:
    def test_fast_e_register(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_gain("fast-e", 2)
        assert mock_send.call_args[0][0] == "das_send_command 8 2"

    def test_slow_e_register(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK") as mock_send:
            ags.set_gain("slow-e", 0)
        assert mock_send.call_args[0][0] == "das_send_command 10 0"

    def test_rejects_bad_channel(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_gain("middle-e", 1)
            mock_send.assert_not_called()

    def test_rejects_bad_level(self, ags):
        with patch.object(ags, "send_ags_command") as mock_send:
            with pytest.raises(ValueError):
                ags.set_gain("fast-e", 4)
            mock_send.assert_not_called()


STARTUP_SAMPLE = (
    "ds_enable\n"
    "das_enable\n"
    "das_set_threshold 1 0.5\n"
    "das_set_threshold 2 0\n"
    "das_send_command 8 1\n"
    "das_send_command 10 1\n"
    "das_set_mask 3\n"
    "das_reset\n"
)


class TestRewriteStartup:
    def test_replaces_matching_threshold_line(self, ags):
        out = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        assert "das_set_threshold 1 5\n" in out
        assert "das_set_threshold 1 0.5" not in out
        # other lines untouched
        assert "das_set_threshold 2 0\n" in out
        assert "das_send_command 8 1\n" in out

    def test_replaces_only_exact_register(self, ags):
        out = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_send_command", "8"], "das_send_command 8 3")
        assert "das_send_command 8 3\n" in out
        assert "das_send_command 10 1\n" in out  # 10 not touched by an "8" match

    def test_inserts_before_das_reset_when_absent(self, ags):
        text = "ds_enable\ndas_enable\ndas_reset\n"
        out = ags.rewrite_startup(
            text, ["das_set_threshold", "1"], "das_set_threshold 1 0.5")
        lines = out.splitlines()
        assert lines.index("das_set_threshold 1 0.5") < lines.index("das_reset")

    def test_idempotent(self, ags):
        once = ags.rewrite_startup(
            STARTUP_SAMPLE, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        twice = ags.rewrite_startup(
            once, ["das_set_threshold", "1"], "das_set_threshold 1 5")
        assert once == twice

    def test_preserves_trailing_newline_absence(self, ags):
        text = "ds_enable\ndas_reset"  # no trailing newline
        out = ags.rewrite_startup(text, ["ds_enable"], "ds_enable")
        assert not out.endswith("\n")


class TestParseStartupState:
    def test_parses_thresholds_and_gains(self, ags):
        state = ags.parse_startup_state(STARTUP_SAMPLE)
        assert state["threshold_1_mv"] == pytest.approx(83, abs=1.0)
        assert state["threshold_2_mv"] == 0.0
        assert state["gain_fast"] == 1
        assert state["gain_slow"] == 1

    def test_missing_lines_absent(self, ags):
        state = ags.parse_startup_state("ds_enable\ndas_reset\n")
        assert "threshold_1_mv" not in state
        assert "gain_fast" not in state


class TestPersist:
    def test_persist_startup_reads_then_writes(self, ags):
        read_result = MagicMock(returncode=0, stdout=STARTUP_SAMPLE.encode())
        write_result = MagicMock(returncode=0)
        with patch.object(ags, "subprocess") as mock_sub:
            mock_sub.run.side_effect = [read_result, write_result]
            ags.persist_startup(["das_set_threshold", "1"],
                                "das_set_threshold 1 5")
        # second call is the write; its input carries the rewritten file
        write_call = mock_sub.run.call_args_list[1]
        written = write_call.kwargs["input"].decode()
        assert "das_set_threshold 1 5\n" in written
        assert "das_set_threshold 2 0\n" in written

    def test_persist_raises_on_read_failure(self, ags):
        with patch.object(ags, "subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=1, stdout=b"",
                                                  stderr=b"no route")
            with pytest.raises(RuntimeError):
                ags.persist_startup(["ds_enable"], "ds_enable")

    def test_set_threshold_persist_calls_persist_startup(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_threshold(1, 830, persist=True)
        match_tokens, new_line = mock_persist.call_args[0][0], mock_persist.call_args[0][1]
        assert match_tokens == ["das_set_threshold", "1"]
        assert new_line.split()[:2] == ["das_set_threshold", "1"]

    def test_set_gain_persist_calls_persist_startup(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_gain("fast-e", 3, persist=True)
        assert mock_persist.call_args[0][0] == ["das_send_command", "8"]
        assert mock_persist.call_args[0][1] == "das_send_command 8 3"

    def test_set_threshold_no_persist_skips(self, ags):
        with patch.object(ags, "send_ags_command", return_value="OK"):
            with patch.object(ags, "persist_startup") as mock_persist:
                ags.set_threshold(1, 830, persist=False)
            mock_persist.assert_not_called()
