"""Tests for server/webgen.py — HTML status page generation."""

import importlib.util
import pathlib
import shutil
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "server" / "webgen.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "python" / "fixtures"


def load_webgen():
    """Load webgen module from server/."""
    spec = importlib.util.spec_from_file_location(
        "webgen", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def webgen():
    """Provide the webgen module."""
    return load_webgen()


FIXED_TIME = datetime(2026, 1, 15, 12, 0, 0)


class TestReadLatestLogFile:
    """Tests for read_latest_log_file()."""

    def test_returns_dataframe_and_time(self, webgen, tmp_path):
        """Should return (DataFrame, log_time_string)."""
        csv_content = (
            "time,Mjolnir 41 Mjolnir Up,Mjolnir 41 Brokkr Up,"
            "Mjolnir 41 Sindri Up,Mjolnir 41 Sensor Up,"
            "Mjolnir 41 Last trigger,Mjolnir 41 GPS Satellites,"
            "Mjolnir 41 Threshold\n"
            "2026-01-15 12:00:00.000,True,True,True,True,"
            "2026-01-15 11:55:00,8,0.45\n"
        )
        csv_file = tmp_path / "status_2026-01-15.csv"
        csv_file.write_text(csv_content)

        df, log_time = webgen.read_latest_log_file(tmp_path)

        assert log_time == "2026-01-15 12:00:00.000"
        assert "Mjolnir Up" in df.columns
        assert len(df) == 1

    def test_picks_latest_csv(self, webgen, tmp_path):
        """Should read the most recent CSV file by ctime."""
        import time as time_mod

        all_cols = (
            "time,Mjolnir 41 Mjolnir Up,Mjolnir 41 Brokkr Up,"
            "Mjolnir 41 Sindri Up,Mjolnir 41 Sensor Up,"
            "Mjolnir 41 Last trigger,Mjolnir 41 GPS Satellites,"
            "Mjolnir 41 Threshold\n"
        )
        old_csv = tmp_path / "status_2026-01-14.csv"
        old_csv.write_text(
            all_cols +
            "2026-01-14 12:00:00.000,False,False,False,False,"
            "2026-01-14 11:00:00,6,0.30\n"
        )
        time_mod.sleep(0.05)
        new_csv = tmp_path / "status_2026-01-15.csv"
        new_csv.write_text(
            all_cols +
            "2026-01-15 12:00:00.000,True,True,True,True,"
            "2026-01-15 11:55:00,8,0.45\n"
        )

        df, _ = webgen.read_latest_log_file(tmp_path)
        assert df.loc["Mjolnir 41"]["Mjolnir Up"] == True


class TestBody:
    """Tests for _body() HTML generation."""

    def test_contains_table(self, webgen):
        import pandas as pd
        values = pd.DataFrame(
            {"Mjolnir Up": [True], "Sensor Up": [True]},
            index=["Mjolnir 41"],
        )
        html = webgen._body(values, "2026-01-15 12:00:00.000", plot="aumma")
        assert "<table" in html
        assert "</table>" in html

    def test_color_coding_up(self, webgen):
        import pandas as pd
        values = pd.DataFrame(
            {"Mjolnir Up": [True]},
            index=["Mjolnir 41"],
        )
        html = webgen._body(values, "2026-01-15 12:00:00.000")
        assert "#8CC882" in html

    def test_color_coding_down(self, webgen):
        import pandas as pd
        values = pd.DataFrame(
            {"Mjolnir Up": [False]},
            index=["Mjolnir 41"],
        )
        html = webgen._body(values, "2026-01-15 12:00:00.000")
        assert "#C86464" in html

    def test_page_creation_time(self, webgen):
        import pandas as pd
        values = pd.DataFrame(
            {"Mjolnir Up": [True]},
            index=["Mjolnir 41"],
        )
        with patch.object(webgen, 'datetime') as mock_dt:
            mock_dt.utcnow.return_value = FIXED_TIME
            html = webgen._body(values, "2026-01-15 12:00:00.000")
        assert "2026-01-15 12:00:00" in html


class TestRegression:
    """Regression test: same input CSV produces same HTML output."""

    @pytest.mark.skipif(
        not (FIXTURES_DIR / "webgen_sample_status.csv").exists(),
        reason="Fixture CSV not yet captured from VPS",
    )
    def test_html_output_matches_baseline(self, webgen, tmp_path):
        """Output from fixture CSV should match saved baseline HTML."""
        fixture_csv = FIXTURES_DIR / "webgen_sample_status.csv"
        expected_html_file = FIXTURES_DIR / "webgen_expected_output.html"

        shutil.copy(fixture_csv, tmp_path / fixture_csv.name)

        output_file = tmp_path / "output.html"

        with patch.object(webgen, 'datetime') as mock_dt:
            mock_dt.utcnow.return_value = FIXED_TIME
            webgen.run_once(tmp_path, array='aumma', output_file=output_file)

        actual = output_file.read_text()
        expected = expected_html_file.read_text()

        assert actual == expected
