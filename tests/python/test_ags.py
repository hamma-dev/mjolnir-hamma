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
