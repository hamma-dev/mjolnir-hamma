"""Tests for ags.py — AGS command wrapper (threshold/gain control)."""

import importlib.util
import pathlib

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
