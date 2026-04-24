"""Tests for hamma_noise module."""

import importlib.util
import json
import os
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "hamma_noise.py"


def load_hamma_noise():
    """Load hamma_noise module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "hamma_noise", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hamma_noise():
    """Provide the hamma_noise module."""
    return load_hamma_noise()


class TestBuildParser:
    """Test argument parser construction."""

    def test_defaults(self, hamma_noise):
        parser = hamma_noise._build_parser()
        args = parser.parse_args([])
        assert args.mj_path == "/media/pi"
        assert args.count == 10
        assert args.warn_pct == 80
        assert args.output == "/tmp/noise_check.json"
        assert args.no_save is False

    def test_custom_args(self, hamma_noise):
        parser = hamma_noise._build_parser()
        args = parser.parse_args([
            "--mj-path", "/tmp/test",
            "--count", "5",
            "--warn-pct", "90",
            "--output", "/tmp/out.json",
            "--no-save",
        ])
        assert args.mj_path == "/tmp/test"
        assert args.count == 5
        assert args.warn_pct == 90
        assert args.output == "/tmp/out.json"
        assert args.no_save is True


class TestDiscoverFiles:
    """Test file discovery across DATA drives."""

    def test_finds_bin_files(self, hamma_noise, tmp_path):
        """Finds .bin files in DATA drive subdirectories."""
        drive = tmp_path / "DATA37"
        hour_dir = drive / "2026-04-23T14"
        hour_dir.mkdir(parents=True)
        f1 = hour_dir / "mj05_2026-04-23_14-00-00-000.bin"
        f2 = hour_dir / "mj05_2026-04-23_14-01-00-000.bin"
        f1.write_bytes(b"\x00" * 100)
        f2.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 2

    def test_no_drives_returns_empty(self, hamma_noise, tmp_path):
        """Returns empty list when no DATA drives exist."""
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert result == []

    def test_count_limits_files(self, hamma_noise, tmp_path):
        """Respects count limit, returning most recent first."""
        drive = tmp_path / "DATA37"
        hour_dir = drive / "2026-04-23T14"
        hour_dir.mkdir(parents=True)
        for i in range(5):
            f = hour_dir / "mj05_2026-04-23_14-0{}-00-000.bin".format(i)
            f.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=3)
        assert len(result) == 3

    def test_multiple_drives(self, hamma_noise, tmp_path):
        """Finds files across multiple DATA drives."""
        for drv in ["DATA37", "DATA38"]:
            d = tmp_path / drv / "2026-04-23T14"
            d.mkdir(parents=True)
            (d / "mj05_file.bin").write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 2

    def test_ignores_non_bin(self, hamma_noise, tmp_path):
        """Ignores .hmc and other non-.bin files."""
        drive = tmp_path / "DATA37" / "2026-04-23T14"
        drive.mkdir(parents=True)
        (drive / "mj05_file.bin").write_bytes(b"\x00" * 100)
        (drive / "mj05_file.hmc").write_bytes(b"\x00" * 100)
        (drive / "mj05_file.txt").write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert len(result) == 1

    def test_sorted_newest_first(self, hamma_noise, tmp_path):
        """Files are returned newest (by mtime) first."""
        import time
        drive = tmp_path / "DATA37" / "2026-04-23T14"
        drive.mkdir(parents=True)
        f_old = drive / "mj05_old.bin"
        f_old.write_bytes(b"\x00" * 100)
        time.sleep(0.05)
        f_new = drive / "mj05_new.bin"
        f_new.write_bytes(b"\x00" * 100)
        result = hamma_noise.discover_files(str(tmp_path), count=10)
        assert os.path.basename(result[0]) == "mj05_new.bin"
