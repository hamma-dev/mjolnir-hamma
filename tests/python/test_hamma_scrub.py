"""Tests for hamma_scrub module."""

import importlib.util
import io
import json
import os
import pathlib
import struct
import subprocess
import time
from datetime import datetime, timezone

import pytest
from unittest.mock import patch, MagicMock

# Small datasize for tests to avoid 22MB allocations per trigger
TEST_DATASIZE = 100
SYNC_MARKER = b'\xf5\xff\x50\x5d'


def _make_trigger(datasize=TEST_DATASIZE, sync=SYNC_MARKER, pad=4):
    """Build a fake trigger: 128-byte header + payload + padding."""
    header = bytearray(128)
    header[0:4] = sync
    struct.pack_into('<I', header, 10, datasize)
    payload = b'\xAA' * (datasize * 2)
    padding = b'\x00' * pad
    return bytes(header), payload + padding


def _make_gps_header(week=2412, tow=522847.0, utc_offset=18.0,
                     subsecond=808000000, ecc=1000000000):
    """Build a 128-byte header with configurable GPS fields.

    Defaults produce timestamp 2026-04-04T01:13:50.808.
    Use week=0, tow=0.0 for bad GPS (year < 2000 -> decode returns None).
    """
    header = bytearray(128)
    header[0:4] = SYNC_MARKER
    struct.pack_into('<I', header, 10, TEST_DATASIZE)
    struct.pack_into('<f', header, 80, tow)
    struct.pack_into('<h', header, 84, week)
    struct.pack_into('<f', header, 86, utc_offset)
    struct.pack_into('<I', header, 94, subsecond)
    struct.pack_into('<I', header, 98, ecc)
    return bytes(header)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "hamma_scrub.py"


def load_hamma_scrub():
    """Load hamma_scrub module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "hamma_scrub", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hamma_scrub():
    """Provide the hamma_scrub module."""
    return load_hamma_scrub()


class TestConstants:
    """Verify constants match HAMMA 2.0 spec."""

    def test_sync_marker(self, hamma_scrub):
        assert hamma_scrub.SYNC_MARKER == b'\xf5\xff\x50\x5d'
        assert len(hamma_scrub.SYNC_MARKER) == 4

    def test_header_size(self, hamma_scrub):
        assert hamma_scrub.HEADER_SIZE == 128

    def test_packet_pad(self, hamma_scrub):
        assert hamma_scrub.PACKET_PAD == 4

    def test_expected_datasize(self, hamma_scrub):
        assert hamma_scrub.EXPECTED_DATASIZE == 11000000

    def test_max_datasize(self, hamma_scrub):
        assert hamma_scrub.MAX_DATASIZE == 20000000

    def test_ags_nice_prefix(self, hamma_scrub):
        """AGS-side commands are CPU-niced to 19 (the DAS writer's floor) so the
        scrub cannot preempt the writer. No ionice: the AGS runs mq-deadline,
        which ignores I/O priority classes."""
        assert hamma_scrub.AGS_NICE == "nice -n 19 "


class TestExtractHeaders:
    """Test extract_headers_from_file with synthetic data."""

    def test_single_trigger(self, hamma_scrub):
        hdr, rest = _make_trigger()
        data = hdr + rest
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        assert len(results) == 1
        assert results[0]["header"] == hdr
        assert results[0]["offset"] == 0
        assert results[0]["index"] == 0

    def test_multiple_triggers(self, hamma_scrub):
        data = b''
        headers = []
        for i in range(5):
            hdr, rest = _make_trigger()
            hdr = bytearray(hdr)
            hdr[50] = i  # unique byte per trigger
            hdr = bytes(hdr)
            headers.append(hdr)
            data += hdr + rest
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        assert len(results) == 5
        for i, r in enumerate(results):
            assert r["header"] == headers[i]
            assert r["index"] == i

    def test_truncated_last_trigger(self, hamma_scrub):
        """Partial trigger at end of file should be skipped."""
        hdr, rest = _make_trigger()
        full = hdr + rest
        # One full trigger + 64 bytes of a second (< HEADER_SIZE)
        data = full + b'\xf5\xff\x50\x5d' + b'\x00' * 60
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        assert len(results) == 1

    def test_empty_file(self, hamma_scrub):
        f = io.BytesIO(b'')
        results = hamma_scrub.extract_headers(f, 0, "test.bin")
        assert len(results) == 0

    def test_bad_sync_scans_forward(self, hamma_scrub):
        """Bad sync at expected position triggers scan-forward recovery."""
        hdr1, rest1 = _make_trigger()
        # Corrupt trigger: bad sync + junk, then a valid third trigger
        corrupt = b'\x00\x00\x00\x00' + b'\xBB' * (TEST_DATASIZE * 2 + 128 - 4 + 4)
        hdr3, rest3 = _make_trigger()
        hdr3 = bytearray(hdr3)
        hdr3[50] = 99  # unique
        hdr3 = bytes(hdr3)
        data = hdr1 + rest1 + corrupt + hdr3 + rest3
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        # Should find trigger 1, skip corrupt, find trigger 3 via scan-forward
        assert len(results) == 2
        assert results[1]["header"] == hdr3

    def test_datasize_out_of_bounds(self, hamma_scrub):
        """datasize > MAX_DATASIZE triggers scan-forward recovery."""
        bad_hdr = bytearray(128)
        bad_hdr[0:4] = SYNC_MARKER
        struct.pack_into('<I', bad_hdr, 10, 30000000)  # > MAX_DATASIZE
        good_hdr, good_rest = _make_trigger()
        good_hdr = bytearray(good_hdr)
        good_hdr[50] = 42
        good_hdr = bytes(good_hdr)
        # Put good trigger right after bad header
        data = bytes(bad_hdr) + good_hdr + good_rest
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        # Should skip bad, find good via scan-forward
        assert len(results) == 1
        assert results[0]["header"] == good_hdr

    def test_datasize_zero(self, hamma_scrub):
        """datasize == 0 triggers scan-forward recovery."""
        bad_hdr = bytearray(128)
        bad_hdr[0:4] = SYNC_MARKER
        struct.pack_into('<I', bad_hdr, 10, 0)  # zero datasize
        data = bytes(bad_hdr) + b'\x00' * 256
        f = io.BytesIO(data)
        results = hamma_scrub.extract_headers(f, len(data), "test.bin")
        assert len(results) == 0


class TestScanMjFiles:
    """Test local mjolnir .bin file scanning."""

    def test_reads_headers_from_bin_files(self, hamma_scrub, tmp_path):
        """Scan finds .bin files and reads 128-byte headers."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        hdr, rest = _make_trigger()
        (drive / "mj05_2026-04-10_14-00-00-000.bin").write_bytes(hdr + rest)
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert len(result["headers"]) == 1
        assert hdr in result["headers"]
        assert result["file_count"] == 1

    def test_multiple_drives(self, hamma_scrub, tmp_path):
        """Scan finds files across multiple DATA drives."""
        for drive_name in ["DATA37", "DATA38"]:
            d = tmp_path / drive_name / "2026-04-10T14"
            d.mkdir(parents=True)
            hdr, rest = _make_trigger()
            hdr = bytearray(hdr)
            hdr[50] = ord(drive_name[-1])  # unique per drive
            (d / "mj05_2026-04-10_14-00-00-000.bin").write_bytes(
                bytes(hdr) + rest
            )
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert len(result["headers"]) == 2
        assert result["file_count"] == 2

    def test_skips_hmc_files(self, hamma_scrub, tmp_path):
        """Compressed .hmc files are ignored."""
        drive = tmp_path / "DATA37" / "compressed" / "2026-04-10T14"
        drive.mkdir(parents=True)
        (drive / "mj05_2026-04-10_14-00-00-000.hmc").write_bytes(b'\x00' * 200)
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert len(result["headers"]) == 0
        assert result["file_count"] == 0

    def test_skips_truncated_files(self, hamma_scrub, tmp_path):
        """Files < 128 bytes are skipped with warning."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        (drive / "mj05_2026-04-10_14-00-00-000.bin").write_bytes(b'\x00' * 64)
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert len(result["headers"]) == 0
        assert result["file_count"] == 1
        assert result["skipped"] == 1

    def test_no_drives_found(self, hamma_scrub, tmp_path):
        """Empty base path returns empty result."""
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert len(result["headers"]) == 0
        assert result["file_count"] == 0

    def test_duplicate_headers_tracked(self, hamma_scrub, tmp_path):
        """Identical headers produce duplicate count."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        hdr, rest = _make_trigger()
        for i in range(3):
            fname = "mj05_2026-04-10_14-00-0{}-000.bin".format(i)
            (drive / fname).write_bytes(hdr + rest)
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert result["file_count"] == 3
        assert len(result["headers"]) == 1  # deduplicated
        assert result["duplicate_count"] == 2

    def test_duplicate_headers_are_reported_by_identity(self, hamma_scrub,
                                                        tmp_path):
        """--audit-loss needs WHICH header was duplicated, not just how many:
        it has to decide whether each one falls inside the window."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        hdr, rest = _make_trigger()
        for i in range(3):
            fname = "mj05_2026-04-10_14-00-0{}-000.bin".format(i)
            (drive / fname).write_bytes(hdr + rest)
        result = hamma_scrub.scan_mj_files(str(tmp_path))
        assert result["duplicate_headers"] == [hdr, hdr]
        # The invariant build_lost_report relies on.
        assert len(result["duplicate_headers"]) == result["duplicate_count"]

    def test_incremental_scanner_withholds_duplicate_identity(
            self, hamma_scrub, tmp_path):
        """A cached dir stores a header SET, so identity is unrecoverable.
        None (not []) keeps that from reading as "no duplicates"."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        hdr, rest = _make_trigger()
        (drive / "a.bin").write_bytes(hdr + rest)
        result = hamma_scrub.scan_mj_files(
            str(tmp_path), cache_file=str(tmp_path / "c.json"))
        assert result["duplicate_headers"] is None

    def test_permission_error_skips_drive(self, hamma_scrub, tmp_path):
        """Permission error on a drive skips it, continues."""
        drive = tmp_path / "DATA37" / "2026-04-10T14"
        drive.mkdir(parents=True)
        hdr, rest = _make_trigger()
        (drive / "mj05_2026-04-10_14-00-00-000.bin").write_bytes(hdr + rest)
        os.chmod(str(tmp_path / "DATA37"), 0o000)
        try:
            result = hamma_scrub.scan_mj_files(str(tmp_path))
            assert result["file_count"] == 0
        finally:
            os.chmod(str(tmp_path / "DATA37"), 0o755)

    def test_since_filters_old_directories(self, hamma_scrub, tmp_path):
        """--since skips directories before cutoff."""
        hdr_old, rest = _make_trigger()
        hdr_new, rest2 = _make_trigger()
        hdr_new = bytearray(hdr_new)
        hdr_new[50] = 99
        hdr_new = bytes(hdr_new)
        # Old directory (before cutoff)
        old_dir = tmp_path / "DATA37" / "2026-04-01T00"
        old_dir.mkdir(parents=True)
        (old_dir / "mj05_2026-04-01_00-00-00-000.bin").write_bytes(hdr_old + rest)
        # New directory (at/after cutoff)
        new_dir = tmp_path / "DATA37" / "2026-04-10T14"
        new_dir.mkdir(parents=True)
        (new_dir / "mj05_2026-04-10_14-00-00-000.bin").write_bytes(hdr_new + rest2)

        result = hamma_scrub.scan_mj_files(str(tmp_path), since="2026-04-10T00")
        assert len(result["headers"]) == 1
        assert hdr_new in result["headers"]
        assert result["file_count"] == 1
        assert result["dirs_skipped"] == 1

    def test_since_none_scans_all(self, hamma_scrub, tmp_path):
        """since=None scans everything (backward compat)."""
        for date in ["2026-04-01T00", "2026-04-10T14"]:
            d = tmp_path / "DATA37" / date
            d.mkdir(parents=True)
            hdr, rest = _make_trigger()
            hdr = bytearray(hdr)
            hdr[50] = ord(date[-1])
            (d / "test.bin").write_bytes(bytes(hdr) + rest)
        result = hamma_scrub.scan_mj_files(str(tmp_path), since=None)
        assert len(result["headers"]) == 2
        assert result["dirs_skipped"] == 0

    def test_since_includes_exact_match(self, hamma_scrub, tmp_path):
        """Directory matching --since exactly is included."""
        d = tmp_path / "DATA37" / "2026-04-10T14"
        d.mkdir(parents=True)
        hdr, rest = _make_trigger()
        (d / "test.bin").write_bytes(hdr + rest)
        result = hamma_scrub.scan_mj_files(str(tmp_path), since="2026-04-10T14")
        assert len(result["headers"]) == 1
        assert result["dirs_skipped"] == 0


class TestParseSince:
    """Test --since date parsing."""

    def test_date_only(self, hamma_scrub):
        assert hamma_scrub._parse_since("2026-04-10") == "2026-04-10T00"

    def test_date_with_hour(self, hamma_scrub):
        assert hamma_scrub._parse_since("2026-04-10T14") == "2026-04-10T14"

    def test_invalid_format(self, hamma_scrub):
        with pytest.raises(ValueError, match="Invalid --since"):
            hamma_scrub._parse_since("April 10")

    def test_strips_whitespace(self, hamma_scrub):
        assert hamma_scrub._parse_since("  2026-04-10  ") == "2026-04-10T00"

    def test_auto_value_returns_sentinel(self, hamma_scrub):
        """'auto' returns the sentinel string 'auto'."""
        assert hamma_scrub._parse_since("auto") == "auto"

    def test_auto_case_insensitive(self, hamma_scrub):
        """'AUTO' and 'Auto' also return 'auto'."""
        assert hamma_scrub._parse_since("AUTO") == "auto"
        assert hamma_scrub._parse_since("Auto") == "auto"


class TestStriderProtocol:
    """Test encoding/decoding of the strider binary protocol."""

    def test_decode_single_entry(self, hamma_scrub):
        """Decode one strider output entry."""
        filename = b"test.bin\x00"
        offset = struct.pack('<Q', 0)
        index = struct.pack('<I', 0)
        header = b'\xf5\xff\x50\x5d' + b'\x00' * 124
        raw = filename + offset + index + header
        entries = hamma_scrub.decode_strider_output(raw)
        assert len(entries) == 1
        assert entries[0]["filename"] == "test.bin"
        assert entries[0]["offset"] == 0
        assert entries[0]["index"] == 0
        assert entries[0]["header"] == header

    def test_decode_multiple_entries(self, hamma_scrub):
        """Decode multiple strider entries."""
        raw = b''
        for i in range(3):
            fname = "file{}.bin".format(i).encode() + b'\x00'
            raw += fname
            raw += struct.pack('<Q', i * 22000132)
            raw += struct.pack('<I', i)
            hdr = bytearray(128)
            hdr[0:4] = b'\xf5\xff\x50\x5d'
            hdr[50] = i
            raw += bytes(hdr)
        entries = hamma_scrub.decode_strider_output(raw)
        assert len(entries) == 3
        for i, e in enumerate(entries):
            assert e["filename"] == "file{}.bin".format(i)
            assert e["index"] == i

    def test_decode_empty(self, hamma_scrub):
        """Empty input returns empty list."""
        entries = hamma_scrub.decode_strider_output(b'')
        assert len(entries) == 0


class TestScanAgsFiles:
    """Test SSH-based AGS scanning."""

    def test_scan_uses_bounded_timeout(self, hamma_scrub):
        """AGS scan timeout is bounded to minutes (SCAN_TIMEOUT), not the old
        3600s cap: a hung scan holds the scrub lock for its whole timeout, so
        an hour-long cap lets one hung scan stall the safety net for an hour."""
        assert hamma_scrub.SCAN_TIMEOUT == 600
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b''
        mock_result.stderr = b''
        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("tempfile.mkstemp",
                   return_value=(99, "/tmp/local_strider.py")), \
             patch("os.write"), patch("os.close"), \
             patch("os.path.exists", return_value=True), patch("os.unlink"):
            hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")
        run_call = mock_run.call_args_list[1]
        assert run_call.kwargs["timeout"] == hamma_scrub.SCAN_TIMEOUT

    def test_deploys_strider_via_scp_then_runs(self, hamma_scrub):
        """scan_ags_files deploys strider via SCP, then runs via SSH."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b''
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("tempfile.mkstemp",
                   return_value=(99, "/tmp/local_strider.py")), \
             patch("os.write") as mock_write, \
             patch("os.close") as mock_close, \
             patch("os.path.exists", return_value=True), \
             patch("os.unlink") as mock_unlink:
            result = hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

        # Two subprocess.run calls: SCP deploy + SSH run
        assert mock_run.call_count == 2
        scp_call = mock_run.call_args_list[0]
        run_call = mock_run.call_args_list[1]

        # SCP deploys the strider script
        assert scp_call[0][0] == [
            "scp", "-q", "/tmp/local_strider.py",
            "10.10.10.1:/tmp/hamma_strider.py",
        ]

        # SSH runs the deployed script (no stdin piping). Command is built via
        # ssh_cmd(), so it carries BatchMode/ConnectTimeout opts; host and the
        # remote command are the last two argv elements.
        run_argv = run_call[0][0]
        assert run_argv[0] == "ssh"
        assert "BatchMode=yes" in run_argv
        assert run_argv[-2] == "10.10.10.1"
        # CPU-niced to the DAS writer's floor so the scan can't preempt it.
        assert run_argv[-1] == (
            "nice -n 19 python3 /tmp/hamma_strider.py /ags/data; "
            "rm -f /tmp/hamma_strider.py")

        # Local temp file written and cleaned up
        mock_write.assert_called_once_with(
            99, hamma_scrub.STRIDER_SCRIPT.encode('utf-8'))
        mock_close.assert_called_once_with(99)
        mock_unlink.assert_called_once_with("/tmp/local_strider.py")

        assert len(result["entries"]) == 0

    def test_decodes_strider_output(self, hamma_scrub):
        """Successful SSH returns decoded entries."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        raw = b'test.bin\x00'
        raw += struct.pack('<Q', 0)
        raw += struct.pack('<I', 0)
        raw += bytes(header)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = raw
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result):
            result = hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

        assert len(result["entries"]) == 1
        assert result["entries"][0]["filename"] == "test.bin"
        assert len(result["headers"]) == 1

    def test_scp_failure_raises(self, hamma_scrub):
        """SCP deploy failure raises RuntimeError."""
        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = b''
        fail_result.stderr = b'No route to host'

        with patch("subprocess.run", return_value=fail_result):
            with pytest.raises(RuntimeError, match="Failed to deploy strider"):
                hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

    def test_ssh_run_failure_raises(self, hamma_scrub):
        """SSH run failure (after successful deploy) raises RuntimeError."""
        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stdout = b''
        ok_result.stderr = b''

        fail_result = MagicMock()
        fail_result.returncode = 255
        fail_result.stdout = b''
        fail_result.stderr = b'Connection refused'

        with patch("subprocess.run",
                   side_effect=[ok_result, fail_result]):
            with pytest.raises(RuntimeError, match="Connection refused"):
                hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

    def test_duplicate_headers_detected(self, hamma_scrub):
        """Duplicate headers counted correctly."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        raw = b''
        for i in range(3):
            raw += b'test.bin\x00'
            raw += struct.pack('<Q', i * 22000132)
            raw += struct.pack('<I', i)
            raw += bytes(header)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = raw
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result):
            result = hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

        assert len(result["entries"]) == 3
        assert len(result["headers"]) == 1
        assert result["duplicate_count"] == 2
        # Identity, not just a tally -- --audit-loss windows these.
        assert result["duplicate_headers"] == [bytes(header)] * 2
        assert len(result["duplicate_headers"]) == result["duplicate_count"]


class TestCompareHeaders:
    """Test header set comparison logic."""

    def test_all_match(self, hamma_scrub):
        hdrs = [b'\xf5\xff\x50\x5d' + bytes([i]) + b'\x00' * 123
                for i in range(5)]
        ags_entries = [
            {"header": h, "filename": "f.bin", "offset": 0, "index": i}
            for i, h in enumerate(hdrs)
        ]
        mj_headers = set(hdrs)
        result = hamma_scrub.compare_headers(ags_entries, mj_headers)
        assert result["matched"] == 5
        assert len(result["missing_on_mj"]) == 0
        assert result["mj_only_count"] == 0

    def test_missing_on_mj(self, hamma_scrub):
        ags_hdrs = [b'\xf5\xff\x50\x5d' + bytes([i]) + b'\x00' * 123
                    for i in range(5)]
        ags_entries = [
            {"header": h, "filename": "f.bin", "offset": i * 22000132,
             "index": i}
            for i, h in enumerate(ags_hdrs)
        ]
        mj_headers = set(ags_hdrs[:3])
        result = hamma_scrub.compare_headers(ags_entries, mj_headers)
        assert result["matched"] == 3
        assert len(result["missing_on_mj"]) == 2
        assert result["missing_on_mj"][0]["index"] == 3

    def test_mj_only(self, hamma_scrub):
        ags_hdrs = [b'\xf5\xff\x50\x5d' + bytes([i]) + b'\x00' * 123
                    for i in range(3)]
        mj_hdrs = [b'\xf5\xff\x50\x5d' + bytes([i]) + b'\x00' * 123
                   for i in range(5)]
        ags_entries = [
            {"header": h, "filename": "f.bin", "offset": 0, "index": i}
            for i, h in enumerate(ags_hdrs)
        ]
        mj_headers = set(mj_hdrs)
        result = hamma_scrub.compare_headers(ags_entries, mj_headers)
        assert result["matched"] == 3
        assert result["mj_only_count"] == 2

    def test_no_overlap(self, hamma_scrub):
        ags_entries = [
            {"header": b'\xf5\xff\x50\x5d\x01' + b'\x00' * 123,
             "filename": "f.bin", "offset": 0, "index": 0}
        ]
        mj_headers = {b'\xf5\xff\x50\x5d\x02' + b'\x00' * 123}
        result = hamma_scrub.compare_headers(ags_entries, mj_headers)
        assert result["matched"] == 0
        assert len(result["missing_on_mj"]) == 1
        assert result["mj_only_count"] == 1

    def test_empty_sets(self, hamma_scrub):
        result = hamma_scrub.compare_headers([], set())
        assert result["matched"] == 0
        assert len(result["missing_on_mj"]) == 0
        assert result["mj_only_count"] == 0


class TestDecodeGpsTime:
    """Test GPS time extraction from raw headers."""

    def test_known_time(self, hamma_scrub):
        """Decode a header with known GPS fields to expected time."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<f', header, 80, 300000.0)
        struct.pack_into('<h', header, 84, 2356)
        struct.pack_into('<f', header, 86, 18.0)
        struct.pack_into('<I', header, 94, 500000000)
        struct.pack_into('<I', header, 98, 1000000000)
        result = hamma_scrub.decode_gps_time(bytes(header))
        assert result == "2025-03-05T11:19:43.500"

    def test_bad_gps_returns_none(self, hamma_scrub):
        """Header with year < 2000 returns None."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        result = hamma_scrub.decode_gps_time(bytes(header))
        assert result is None

    def test_zero_ecc_handled(self, hamma_scrub):
        """gpsSubSecondECC == 0 should not crash (div by zero guard)."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<f', header, 80, 300000.0)
        struct.pack_into('<h', header, 84, 2356)
        struct.pack_into('<f', header, 86, 18.0)
        struct.pack_into('<I', header, 94, 500000000)
        struct.pack_into('<I', header, 98, 0)
        result = hamma_scrub.decode_gps_time(bytes(header))
        assert result == "2025-03-05T11:19:43.500"

    def test_nan_time_of_week_returns_none(self, hamma_scrub):
        """NaN gpsTimeOfWeek must return None, not raise.

        HAM-164: math.floor(nan) raises ValueError, which the struct.error
        guard does not catch. Real bad-GPS records carry NaN here (8 of them
        on mj08's drive), and they aborted a full-drive reconciliation pass.
        """
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<f', header, 80, float('nan'))
        struct.pack_into('<h', header, 84, 2356)
        struct.pack_into('<f', header, 86, 18.0)
        struct.pack_into('<I', header, 94, 500000000)
        struct.pack_into('<I', header, 98, 1000000000)
        assert hamma_scrub.decode_gps_time(bytes(header)) is None

    @staticmethod
    def _gps_header(tow=300000.0, utc_offset=18.0):
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<f', header, 80, tow)
        struct.pack_into('<h', header, 84, 2356)
        struct.pack_into('<f', header, 86, utc_offset)
        struct.pack_into('<I', header, 94, 500000000)
        struct.pack_into('<I', header, 98, 1000000000)
        return bytes(header)

    @pytest.mark.parametrize("bad_tow", [float('nan'), float('inf'),
                                         float('-inf')])
    def test_non_finite_time_of_week_returns_none(self, hamma_scrub, bad_tow):
        """math.floor(nan) raises ValueError and math.floor(inf) raises
        OverflowError, from outside the try -- so without the guard a single
        bad-GPS record raises out of decode_gps_time() and takes down whatever
        is walking the headers."""
        assert hamma_scrub.decode_gps_time(
            self._gps_header(tow=bad_tow)) is None

    @pytest.mark.parametrize("bad_offset", [float('nan'), float('inf'),
                                            float('-inf')])
    def test_non_finite_utc_offset_returns_none(self, hamma_scrub,
                                                bad_offset):
        """NaN alone is a weak case here -- it is already caught downstream by
        the existing `except ValueError` around datetime.fromtimestamp(). The
        +/-inf cases are what make the utc_offset half of the guard matter."""
        assert hamma_scrub.decode_gps_time(
            self._gps_header(utc_offset=bad_offset)) is None

    def test_good_header_still_decodes(self, hamma_scrub):
        """The guard must not reject valid records."""
        assert hamma_scrub.decode_gps_time(
            self._gps_header()) == "2025-03-05T11:19:43.500"


class TestEarliestAgsTimestamp:
    """Test earliest_ags_timestamp() — derives --since cutoff from AGS data."""

    def test_returns_earliest_valid_gps(self, hamma_scrub):
        """Returns YYYY-MM-DDTHH from earliest valid GPS across files."""
        # week 2412, tow ~522847 -> 2026-04-04T01
        hdr_early = _make_gps_header(week=2412, tow=522847.0)
        # week 2413, tow ~522847 -> ~1 week later
        hdr_late = _make_gps_header(week=2413, tow=522847.0)
        entries = [
            {"header": hdr_late, "filename": "ags2026-04-11.bin",
             "offset": 0, "index": 0},
            {"header": hdr_early, "filename": "ags2026-04-04.bin",
             "offset": 0, "index": 0},
        ]
        expected = hamma_scrub.decode_gps_time(hdr_early)[:13]
        result = hamma_scrub.earliest_ags_timestamp(entries)
        assert result == expected

    def test_includes_1980_files(self, hamma_scrub):
        """1980-named files are examined, not skipped."""
        # 1980 file: first trigger bad GPS, second trigger has valid GPS
        hdr_bad = _make_gps_header(week=0, tow=0.0)  # bad GPS
        hdr_1980_good = _make_gps_header(week=2410, tow=100000.0)
        # ags2026 file: valid GPS but later
        hdr_2026 = _make_gps_header(week=2412, tow=522847.0)
        entries = [
            {"header": hdr_bad, "filename": "ags1980-01-06.bin",
             "offset": 0, "index": 0},
            {"header": hdr_1980_good, "filename": "ags1980-01-06.bin",
             "offset": 22000000, "index": 1},
            {"header": hdr_2026, "filename": "ags2026-04-04.bin",
             "offset": 0, "index": 0},
        ]
        expected = hamma_scrub.decode_gps_time(hdr_1980_good)[:13]
        result = hamma_scrub.earliest_ags_timestamp(entries)
        assert result == expected

    def test_skips_bad_gps_within_file(self, hamma_scrub):
        """Bad GPS triggers (year < 2000) are skipped; first valid wins."""
        hdr_bad = _make_gps_header(week=0, tow=0.0)
        hdr_good = _make_gps_header(week=2412, tow=522847.0)
        entries = [
            {"header": hdr_bad, "filename": "ags1980-01-06.bin",
             "offset": 0, "index": 0},
            {"header": hdr_good, "filename": "ags1980-01-06.bin",
             "offset": 22000000, "index": 1},
        ]
        expected = hamma_scrub.decode_gps_time(hdr_good)[:13]
        result = hamma_scrub.earliest_ags_timestamp(entries)
        assert result == expected

    def test_all_bad_gps_returns_none(self, hamma_scrub):
        """If no trigger has valid GPS, return None."""
        hdr_bad = _make_gps_header(week=0, tow=0.0)
        entries = [
            {"header": hdr_bad, "filename": "ags1980-01-06.bin",
             "offset": 0, "index": 0},
            {"header": hdr_bad, "filename": "ags1980-01-06.bin",
             "offset": 22000000, "index": 1},
        ]
        assert hamma_scrub.earliest_ags_timestamp(entries) is None

    def test_empty_entries_returns_none(self, hamma_scrub):
        """Empty entry list returns None."""
        assert hamma_scrub.earliest_ags_timestamp([]) is None

    def test_stops_at_first_valid_per_file(self, hamma_scrub):
        """Only examines triggers until first valid GPS in each file."""
        hdr_bad = _make_gps_header(week=0, tow=0.0)
        hdr_first = _make_gps_header(week=2412, tow=100000.0)
        hdr_second = _make_gps_header(week=2412, tow=200000.0)
        entries = [
            {"header": hdr_bad, "filename": "file.bin",
             "offset": 0, "index": 0},
            {"header": hdr_first, "filename": "file.bin",
             "offset": 22000000, "index": 1},
            {"header": hdr_second, "filename": "file.bin",
             "offset": 44000000, "index": 2},
        ]
        # Should return cutoff from hdr_first, not hdr_second
        expected = hamma_scrub.decode_gps_time(hdr_first)[:13]
        result = hamma_scrub.earliest_ags_timestamp(entries)
        assert result == expected


class TestDetectUnitName:
    """Test hostname-based unit name detection."""

    def test_mjolnir41(self, hamma_scrub):
        assert hamma_scrub.detect_unit_name("mjolnir41") == ("mj", "41")

    def test_mjolnir05(self, hamma_scrub):
        assert hamma_scrub.detect_unit_name("mjolnir05") == ("mj", "05")

    def test_mjolnir2(self, hamma_scrub):
        assert hamma_scrub.detect_unit_name("mjolnir2") == ("mj", "2")

    def test_unknown_hostname(self, hamma_scrub):
        assert hamma_scrub.detect_unit_name("raspberrypi") == ("recovered", "")

    def test_empty_hostname(self, hamma_scrub):
        assert hamma_scrub.detect_unit_name("") == ("recovered", "")

    def test_auto_detect(self, hamma_scrub):
        """With hostname=None, reads from socket.gethostname()."""
        with patch("socket.gethostname", return_value="mjolnir42"):
            assert hamma_scrub.detect_unit_name() == ("mj", "42")


class TestComputeTargetPath:
    """Test target directory and filename computation."""

    def _make_gps_header(self):
        """Build a header with known GPS time 2026-04-04T01:13:50.808."""
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        struct.pack_into('<f', header, 80, 522847.0)      # gpsTimeWeek (seconds into week)
        struct.pack_into('<h', header, 84, 2412)           # gpsWeek
        struct.pack_into('<f', header, 86, 18.0)           # utcOffset
        struct.pack_into('<I', header, 94, 808000000)      # gpsSubSecond
        struct.pack_into('<I', header, 98, 1000000000)     # gpsSubSecondECC
        return bytes(header)

    def test_good_gps(self, hamma_scrub):
        header = self._make_gps_header()
        subdir, filename = hamma_scrub.compute_target_path(header, 0, "mj", "41")
        assert subdir.startswith("2026-")
        assert "T" in subdir  # YYYY-MM-DDTHH format
        assert filename.startswith("mj41_")
        assert filename.endswith("_recovered.bin")
        assert "_recovered.bin" in filename

    def test_bad_gps_uses_unknown(self, hamma_scrub):
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        subdir, filename = hamma_scrub.compute_target_path(
            bytes(header), 924005544, "mj", "41",
        )
        assert subdir == "unknown"
        assert "off924005544" in filename
        assert filename.startswith("mj41_")
        assert filename.endswith("_recovered.bin")

    def test_bad_gps_different_offsets(self, hamma_scrub):
        """Different offsets produce different filenames (collision prevention)."""
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        _, f1 = hamma_scrub.compute_target_path(bytes(header), 100, "mj", "41")
        _, f2 = hamma_scrub.compute_target_path(bytes(header), 200, "mj", "41")
        assert f1 != f2

    def test_fallback_prefix(self, hamma_scrub):
        """No unit number uses prefix only."""
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        _, filename = hamma_scrub.compute_target_path(
            bytes(header), 0, "recovered", "",
        )
        assert filename.startswith("recovered_")


class TestSelectTargetDrive:
    """Test DATA drive selection for recovery writes."""

    def test_single_drive_with_space(self, hamma_scrub, tmp_path):
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        result = hamma_scrub.select_target_drive(str(tmp_path))
        assert result == str(drive)

    def test_picks_drive_with_most_recent_data(self, hamma_scrub, tmp_path):
        d37 = tmp_path / "DATA37"
        (d37 / "2026-04-01T00").mkdir(parents=True)
        d38 = tmp_path / "DATA38"
        (d38 / "2026-04-10T14").mkdir(parents=True)
        result = hamma_scrub.select_target_drive(str(tmp_path))
        assert result == str(d38)

    def test_no_drives(self, hamma_scrub, tmp_path):
        result = hamma_scrub.select_target_drive(str(tmp_path))
        assert result is None

    def test_drive_with_no_subdirs(self, hamma_scrub, tmp_path):
        (tmp_path / "DATA37").mkdir()
        result = hamma_scrub.select_target_drive(str(tmp_path))
        # Drive exists but has no hourly dirs; should still be returned
        assert result == str(tmp_path / "DATA37")

    def test_skips_compressed_subdir(self, hamma_scrub, tmp_path):
        """The 'compressed' subdirectory should not affect drive ranking."""
        d37 = tmp_path / "DATA37"
        (d37 / "compressed" / "2099-12-31T23").mkdir(parents=True)
        (d37 / "2026-04-01T00").mkdir(parents=True)
        d38 = tmp_path / "DATA38"
        (d38 / "2026-04-10T14").mkdir(parents=True)
        result = hamma_scrub.select_target_drive(str(tmp_path))
        assert result == str(d38)


class TestExtractTrigger:
    """Test SSH dd-based trigger extraction."""

    def test_successful_extraction(self, hamma_scrub):
        """Successful dd returns extracted bytes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b'\xf5\xff\x50\x5d' + b'\x00' * 100
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            data = hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "test.bin", 1000, 104,
            )

        assert data == mock_result.stdout
        cmd = mock_run.call_args[0][0]
        assert cmd[-1].startswith("nice -n 19 dd ")  # niced below the DAS writer
        assert "skip=1000" in cmd[-1]
        assert "count=104" in cmd[-1]
        assert "iflag=skip_bytes,count_bytes" in cmd[-1]
        assert "bs=4096" in cmd[-1]
        assert "status=none" in cmd[-1]

    def test_dd_failure_returns_none(self, hamma_scrub):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b''
        mock_result.stderr = b'No such file'

        with patch("subprocess.run", return_value=mock_result):
            data = hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "test.bin", 0, 100,
            )
        assert data is None

    def test_timeout_returns_none(self, hamma_scrub):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dd", 60)):
            data = hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "test.bin", 0, 100,
            )
        assert data is None

    def test_ssh_oserror_returns_none(self, hamma_scrub):
        with patch("subprocess.run", side_effect=OSError("Connection refused")):
            data = hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "test.bin", 0, 100,
            )
        assert data is None

    def test_constructs_correct_path(self, hamma_scrub):
        """Full path is ags_path/filename."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b'\x00' * 50
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "agsfile.bin", 500, 50,
            )
        cmd_str = mock_run.call_args[0][0][-1]
        assert "if=/ags/data/agsfile.bin" in cmd_str


class TestVerifyTrigger:
    """Test trigger data verification."""

    def test_valid_trigger(self, hamma_scrub):
        data = SYNC_MARKER + b'\x00' * 96
        ok, err = hamma_scrub.verify_trigger(data, 100)
        assert ok is True
        assert err == ""

    def test_size_mismatch(self, hamma_scrub):
        data = SYNC_MARKER + b'\x00' * 50
        ok, err = hamma_scrub.verify_trigger(data, 100)
        assert ok is False
        assert "size mismatch" in err

    def test_bad_sync_marker(self, hamma_scrub):
        data = b'\x00\x00\x00\x00' + b'\x00' * 96
        ok, err = hamma_scrub.verify_trigger(data, 100)
        assert ok is False
        assert "sync marker" in err

    def test_empty_data(self, hamma_scrub):
        ok, err = hamma_scrub.verify_trigger(b'', 100)
        assert ok is False
        assert "size mismatch" in err


class TestFilterRecoveryCandidates:
    """Test filtering of recovery candidates."""

    def _make_entry(self, filename, offset, index, bad_gps=False):
        """Build a mock AGS entry."""
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        if not bad_gps:
            # Set GPS fields for 2026-04-04T01:13:50.808
            struct.pack_into('<f', header, 80, 522847.0)
            struct.pack_into('<h', header, 84, 2412)
            struct.pack_into('<f', header, 86, 18.0)
            struct.pack_into('<I', header, 94, 808000000)
            struct.pack_into('<I', header, 98, 1000000000)
        return {
            "header": bytes(header),
            "filename": filename,
            "offset": offset,
            "index": index,
        }

    def test_no_filtering_needed(self, hamma_scrub):
        """All candidates are recoverable when not in newest file's last trigger."""
        entry = self._make_entry("ags_aaa.bin", 0, 0)
        all_ags = [
            self._make_entry("ags_aaa.bin", 0, 0),
            self._make_entry("ags_zzz.bin", 0, 0),
            self._make_entry("ags_zzz.bin", 22000132, 1),
        ]
        result = hamma_scrub.filter_recovery_candidates([entry], all_ags)
        assert len(result) == 1
        assert result[0]["skip_reason"] is None

    def test_skips_last_trigger_in_newest_file(self, hamma_scrub):
        """Last trigger in lexicographically newest AGS file is skipped."""
        last_entry = self._make_entry("ags_zzz.bin", 22000132, 1)
        all_ags = [
            self._make_entry("ags_zzz.bin", 0, 0),
            last_entry,
        ]
        result = hamma_scrub.filter_recovery_candidates([last_entry], all_ags)
        assert len(result) == 1
        assert result[0]["skip_reason"] is not None
        assert "active" in result[0]["skip_reason"]

    def test_since_skips_old_triggers(self, hamma_scrub):
        """Triggers with GPS time before --since cutoff are skipped."""
        # GPS decodes to 2026-04-04T01:...
        entry = self._make_entry("ags_aaa.bin", 0, 0)
        all_ags = [entry, self._make_entry("ags_zzz.bin", 0, 0)]
        result = hamma_scrub.filter_recovery_candidates(
            [entry], all_ags, since_cutoff="2026-04-10T00",
        )
        assert len(result) == 1
        assert "since" in result[0]["skip_reason"]

    def test_since_keeps_new_triggers(self, hamma_scrub):
        """Triggers at/after --since cutoff are kept."""
        entry = self._make_entry("ags_aaa.bin", 0, 0)
        all_ags = [entry, self._make_entry("ags_zzz.bin", 0, 0)]
        result = hamma_scrub.filter_recovery_candidates(
            [entry], all_ags, since_cutoff="2026-04-01T00",
        )
        assert len(result) == 1
        assert result[0]["skip_reason"] is None

    def test_bad_gps_still_recovered_with_since(self, hamma_scrub):
        """Bad GPS triggers are recovered even with --since (can't determine time)."""
        entry = self._make_entry("ags_aaa.bin", 0, 0, bad_gps=True)
        all_ags = [entry, self._make_entry("ags_zzz.bin", 0, 0)]
        result = hamma_scrub.filter_recovery_candidates(
            [entry], all_ags, since_cutoff="2026-04-10T00",
        )
        assert len(result) == 1
        assert result[0]["skip_reason"] is None

    def test_empty_input(self, hamma_scrub):
        result = hamma_scrub.filter_recovery_candidates([], [])
        assert result == []


class TestCleanupOrphanedTemps:
    """Test orphaned temp file cleanup."""

    def test_deletes_old_temps(self, hamma_scrub, tmp_path):
        drive = tmp_path / "DATA37"
        drive.mkdir()
        tmp_file = drive / ".tmp_recover_abc123.bin"
        tmp_file.write_bytes(b'\x00' * 100)
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(str(tmp_file), (old_time, old_time))
        count = hamma_scrub.cleanup_orphaned_temps(str(tmp_path))
        assert count == 1
        assert not tmp_file.exists()

    def test_keeps_recent_temps(self, hamma_scrub, tmp_path):
        drive = tmp_path / "DATA37"
        drive.mkdir()
        tmp_file = drive / ".tmp_recover_abc123.bin"
        tmp_file.write_bytes(b'\x00' * 100)
        count = hamma_scrub.cleanup_orphaned_temps(str(tmp_path))
        assert count == 0
        assert tmp_file.exists()

    def test_no_drives(self, hamma_scrub, tmp_path):
        count = hamma_scrub.cleanup_orphaned_temps(str(tmp_path))
        assert count == 0

    def test_ignores_non_matching_files(self, hamma_scrub, tmp_path):
        drive = tmp_path / "DATA37"
        drive.mkdir()
        normal_file = drive / "mj41_2026-04-04_01-13-50-808.bin"
        normal_file.write_bytes(b'\x00' * 100)
        old_time = time.time() - 7200
        os.utime(str(normal_file), (old_time, old_time))
        count = hamma_scrub.cleanup_orphaned_temps(str(tmp_path))
        assert count == 0
        assert normal_file.exists()


class TestRecoverTriggers:
    """Test the recovery orchestrator."""

    def _make_candidate(self, hamma_scrub, skip_reason=None, skip_status=None, bad_gps=False):
        """Build a candidate entry with proper header."""
        header = bytearray(128)
        header[0:4] = SYNC_MARKER
        struct.pack_into('<I', header, 10, TEST_DATASIZE)
        if not bad_gps:
            struct.pack_into('<f', header, 80, 522847.0)
            struct.pack_into('<h', header, 84, 2412)
            struct.pack_into('<f', header, 86, 18.0)
            struct.pack_into('<I', header, 94, 808000000)
            struct.pack_into('<I', header, 98, 1000000000)
        return {
            "header": bytes(header),
            "filename": "agsfile.bin",
            "offset": 0,
            "index": 0,
            "skip_reason": skip_reason,
            "skip_status": skip_status,
        }

    def test_dry_run(self, hamma_scrub, tmp_path):
        """Dry run produces dry_run status without extracting."""
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        candidate = self._make_candidate(hamma_scrub)
        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path), dry_run=True,
            )
        assert len(results) == 1
        assert results[0]["status"] == "dry_run"
        assert results[0]["target_path"] is not None

    def test_skipped_candidate(self, hamma_scrub, tmp_path):
        """Candidates with skip_reason produce skipped status."""
        candidate = self._make_candidate(
            hamma_scrub,
            skip_reason="before --since cutoff",
            skip_status="skipped_before_since",
        )
        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "skipped_before_since"

    def test_skipped_active_file(self, hamma_scrub, tmp_path):
        candidate = self._make_candidate(
            hamma_scrub,
            skip_reason="last trigger in active file",
            skip_status="skipped",
        )
        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "skipped"

    def test_successful_recovery(self, hamma_scrub, tmp_path):
        """Full recovery: extract, verify, write."""
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        candidate = self._make_candidate(hamma_scrub)

        size = 128 + TEST_DATASIZE * 2 + 4
        fake_data = SYNC_MARKER + b'\x00' * (size - 4)

        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
             patch.object(hamma_scrub, "extract_trigger", return_value=fake_data):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "recovered"
        # Verify file was actually written
        target = os.path.join(str(tmp_path), results[0]["target_path"])
        assert os.path.exists(target)
        assert os.path.getsize(target) == size

    def test_extraction_failure(self, hamma_scrub, tmp_path):
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        candidate = self._make_candidate(hamma_scrub)

        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
             patch.object(hamma_scrub, "extract_trigger", return_value=None):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "failed"

    def test_verification_failure(self, hamma_scrub, tmp_path):
        """Bad sync marker in extracted data -> failed."""
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        candidate = self._make_candidate(hamma_scrub)

        size = 128 + TEST_DATASIZE * 2 + 4
        bad_data = b'\x00' * size  # no sync marker

        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
             patch.object(hamma_scrub, "extract_trigger", return_value=bad_data):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "failed"
        assert "sync marker" in results[0]["error"]

    def test_no_drive_space(self, hamma_scrub, tmp_path):
        """No drive with sufficient space -> failed."""
        candidate = self._make_candidate(hamma_scrub)
        # No DATA drives at tmp_path
        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "failed"
        assert "space" in results[0]["error"]

    def test_file_already_exists_skipped(self, hamma_scrub, tmp_path):
        """If target file already exists, skip (idempotent)."""
        drive = tmp_path / "DATA37"
        candidate = self._make_candidate(hamma_scrub)
        # Pre-compute target path to create the file in advance
        gps_str = hamma_scrub.decode_gps_time(candidate["header"])
        subdir = gps_str[:13]
        target_dir = drive / subdir
        target_dir.mkdir(parents=True)
        # Create a file that would match the target
        ts = gps_str[0:10] + '_' + gps_str[11:].replace(':', '-').replace('.', '-')
        target_file = target_dir / "mj41_{}_recovered.bin".format(ts)
        target_file.write_bytes(b'\x00' * 100)

        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "skipped"
        assert "exists" in results[0]["error"]

    def test_result_includes_header(self, hamma_scrub, tmp_path):
        """Each recovery result dict includes the trigger's header bytes."""
        header, payload_pad = _make_trigger()

        candidates = [{
            "filename": "ags001.bin",
            "offset": 0,
            "index": 0,
            "header": header,
            "skip_status": None,
            "skip_reason": None,
        }]

        # Create a DATA_1 drive with enough space
        drive = tmp_path / "DATA_1"
        drive.mkdir()

        mock_data = header + payload_pad
        with patch.object(hamma_scrub, "extract_trigger", return_value=mock_data), \
             patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
             patch.object(hamma_scrub, "select_target_drive", return_value=str(drive)):
            results = hamma_scrub.recover_triggers(
                candidates, "hamma", "/ags/data", str(tmp_path), dry_run=False,
            )

        assert results[0]["header"] == header

    def test_bad_gps_writes_to_unknown(self, hamma_scrub, tmp_path):
        """Bad GPS trigger goes to unknown/ subdirectory."""
        drive = tmp_path / "DATA37"
        (drive / "2026-04-10T14").mkdir(parents=True)
        candidate = self._make_candidate(hamma_scrub, bad_gps=True)

        size = 128 + TEST_DATASIZE * 2 + 4
        fake_data = SYNC_MARKER + b'\x00' * (size - 4)

        with patch.object(hamma_scrub, "detect_unit_name", return_value=("mj", "41")), \
             patch.object(hamma_scrub, "extract_trigger", return_value=fake_data):
            results = hamma_scrub.recover_triggers(
                [candidate], "hamma", "/ags/data", str(tmp_path),
            )
        assert results[0]["status"] == "recovered"
        assert "unknown" in results[0]["target_path"]


class TestIdentifyPurgeableFiles:
    """Test AGS file purge eligibility logic."""

    def test_all_matched_is_purgeable(self, hamma_scrub):
        """File with all triggers in mj_headers is purgeable."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1, h2}

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results=None,
        )

        assert result["purgeable"] == ["ags001.bin"]
        assert len(result["retained"]) == 1
        assert result["retained"][0]["filename"] == "ags002.bin"
        assert "active" in result["retained"][0]["reason"].lower()

    def test_some_missing_retained(self, hamma_scrub):
        """File with unmatched triggers is retained."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        h3 = b'\x03' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h3},
        ]
        mj_headers = {h1}

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results=None,
        )

        assert result["purgeable"] == []
        reasons = {r["filename"]: r["reason"] for r in result["retained"]}
        assert "1/2 triggers not on MJ" in reasons["ags001.bin"]
        assert reasons["ags002.bin"] == "active file"

    def test_recovery_failure_retains(self, hamma_scrub):
        """File with a failed recovery is retained."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}
        recovery_results = [{
            "source_file": "ags001.bin",
            "source_offset": 1000,
            "status": "failed",
            "header": h2,
            "error": "dd extraction failed",
        }]

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results,
        )

        assert result["purgeable"] == []
        reasons = {r["filename"]: r["reason"] for r in result["retained"]}
        assert "1 recovery failed" in reasons["ags001.bin"]

    def test_newest_file_always_retained(self, hamma_scrub):
        """Lexicographically newest file is always retained."""
        h1 = b'\x01' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results=None,
        )

        assert result["purgeable"] == []
        assert result["retained"][0]["reason"] == "active file"

    def test_recovery_results_none(self, hamma_scrub):
        """None recovery_results evaluates purely on header matching."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h2},
        ]
        mj_headers = {h1, h2}

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results=None,
        )

        assert result["purgeable"] == ["ags001.bin"]

    def test_empty_entries(self, hamma_scrub):
        """Empty ags_entries returns empty results."""
        result = hamma_scrub.identify_purgeable_files(
            [], set(), recovery_results=None,
        )
        assert result["purgeable"] == []
        assert result["retained"] == []

    def test_skipped_before_since_retains(self, hamma_scrub):
        """Trigger with skipped_before_since status retains the file."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}
        recovery_results = [{
            "source_file": "ags001.bin",
            "source_offset": 1000,
            "status": "skipped_before_since",
            "header": h2,
            "error": "before --since cutoff",
        }]

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results,
        )

        assert result["purgeable"] == []

    def test_dry_run_status_retains(self, hamma_scrub):
        """Trigger with dry_run status retains the file."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}
        recovery_results = [{
            "source_file": "ags001.bin",
            "source_offset": 1000,
            "status": "dry_run",
            "header": h2,
            "error": None,
        }]

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results,
        )

        assert result["purgeable"] == []

    def test_skipped_file_exists_is_safe(self, hamma_scrub):
        """Trigger skipped because file already exists is safe for purge."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}
        recovery_results = [{
            "source_file": "ags001.bin",
            "source_offset": 1000,
            "status": "skipped",
            "header": h2,
            "error": "file already exists",
        }]

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results,
        )

        assert "ags001.bin" in result["purgeable"]

    def test_skipped_active_guard_retains(self, hamma_scrub):
        """Trigger skipped by active file guard retains the file."""
        h1 = b'\x01' * 128
        h2 = b'\x02' * 128
        ags_entries = [
            {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            {"filename": "ags001.bin", "offset": 1000, "index": 1, "header": h2},
            {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
        ]
        mj_headers = {h1}
        recovery_results = [{
            "source_file": "ags001.bin",
            "source_offset": 1000,
            "status": "skipped",
            "header": h2,
            "error": "last trigger in active file",
        }]

        result = hamma_scrub.identify_purgeable_files(
            ags_entries, mj_headers, recovery_results,
        )

        assert result["purgeable"] == []


class TestPurgeAgsFiles:
    """Test SSH-based AGS file deletion."""

    def test_successful_deletion(self, hamma_scrub):
        """Successful SSH rm returns status 'deleted'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
            )

        assert len(results) == 1
        assert results[0]["filename"] == "ags001.bin"
        assert results[0]["status"] == "deleted"

    def test_ssh_failure(self, hamma_scrub):
        """SSH rm failure returns status 'failed' with error."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b'No such file or directory'

        with patch("subprocess.run", return_value=mock_result):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
            )

        assert results[0]["status"] == "failed"
        assert "No such file" in results[0]["error"]

    def test_ssh_timeout(self, hamma_scrub):
        """SSH timeout returns status 'failed'."""
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags001.bin"], dry_run=False,
            )

        assert results[0]["status"] == "failed"
        assert "timeout" in results[0]["error"].lower()

    def test_dry_run(self, hamma_scrub):
        """Dry run logs but does not call subprocess."""
        with patch("subprocess.run") as mock_run:
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags001.bin", "ags002.bin"],
                dry_run=True,
            )

        mock_run.assert_not_called()
        assert len(results) == 2
        assert all(r["status"] == "dry_run" for r in results)

    def test_empty_filenames(self, hamma_scrub):
        """Empty filenames list returns empty results."""
        results = hamma_scrub.purge_ags_files(
            "hamma", "/ags/data", [], dry_run=False,
        )
        assert results == []

    def test_path_uses_shlex_quote(self, hamma_scrub):
        """Remote path is shell-quoted for safety."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["ags file.bin"], dry_run=False,
            )

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "hamma" in cmd
        # remote rm command is the last argv element; path is shlex-quoted
        assert "'/ags/data/ags file.bin'" in cmd[-1]
        assert cmd[-1].startswith("nice -n 19 rm -f ")  # niced below the DAS writer


class TestRecoveryReport:
    """Test recovery sections in human and JSON reports."""

    def _make_results_with_recovery(self):
        """Build results dict with recovery data."""
        missing_hdr = b'\xf5\xff\x50\x5d' + b'\x00' * 124
        return {
            "ags_triggers": 100,
            "ags_files": 2,
            "ags_elapsed": 5.2,
            "ags_duplicate_count": 0,
            "mj_triggers": 95,
            "mj_files_scanned": 95,
            "mj_duplicate_count": 0,
            "mj_elapsed": 3.1,
            "matched": 95,
            "missing_on_mj": [
                {"filename": "data.bin", "offset": 0, "index": 0,
                 "header": missing_hdr},
            ],
            "mj_only_count": 0,
            "warnings": [],
        }

    def test_human_report_with_recovery(self, hamma_scrub):
        results = self._make_results_with_recovery()
        recovery = [{
            "source_file": "data.bin",
            "source_offset": 0,
            "trigger_index": 0,
            "target_path": "DATA37/2026-04-04T01/mj41_2026-04-04_01-13-50-808_recovered.bin",
            "size": 22000132,
            "status": "recovered",
            "error": None,
        }]
        report = hamma_scrub.format_human_report(results, recovery=recovery)
        assert "Recovery:" in report
        assert "1 attempted" in report
        assert "1 succeeded" in report
        assert "Recovered:" in report
        assert "mj41_" in report

    def test_human_report_with_failed_recovery(self, hamma_scrub):
        results = self._make_results_with_recovery()
        recovery = [{
            "source_file": "data.bin",
            "source_offset": 0,
            "trigger_index": 0,
            "target_path": "DATA37/2026-04-04T01/mj41_2026-04-04_01-13-50-808_recovered.bin",
            "size": 22000132,
            "status": "failed",
            "error": "dd returned rc=1",
        }]
        report = hamma_scrub.format_human_report(results, recovery=recovery)
        assert "FAILED:" in report
        assert "dd returned rc=1" in report

    def test_human_report_dry_run(self, hamma_scrub):
        results = self._make_results_with_recovery()
        recovery = [{
            "source_file": "data.bin",
            "source_offset": 0,
            "trigger_index": 0,
            "target_path": "DATA37/2026-04-04T01/mj41_2026-04-04_01-13-50-808_recovered.bin",
            "size": 22000132,
            "status": "dry_run",
            "error": None,
        }]
        report = hamma_scrub.format_human_report(results, recovery=recovery)
        assert "dry run" in report.lower()
        assert "Would recover:" in report

    def test_human_report_no_recovery(self, hamma_scrub):
        """No recovery parameter -> no recovery section (backward compat)."""
        results = self._make_results_with_recovery()
        report = hamma_scrub.format_human_report(results)
        assert "Recovery:" not in report

    def test_json_report_with_recovery(self, hamma_scrub):
        results = self._make_results_with_recovery()
        recovery = [{
            "source_file": "data.bin",
            "source_offset": 0,
            "trigger_index": 0,
            "target_path": "DATA37/2026-04-04T01/mj41_recovered.bin",
            "size": 22000132,
            "status": "recovered",
            "error": None,
            "header": b'\x00' * 128,
        }]
        j = hamma_scrub.format_json_report(results, "hamma", recovery=recovery)
        parsed = json.loads(j)
        assert "recovery" in parsed
        assert len(parsed["recovery"]) == 1
        assert parsed["recovery"][0]["status"] == "recovered"
        assert "header" not in parsed["recovery"][0]

    def test_json_report_no_recovery(self, hamma_scrub):
        """No recovery parameter -> no recovery key in JSON."""
        results = self._make_results_with_recovery()
        j = hamma_scrub.format_json_report(results, "hamma")
        parsed = json.loads(j)
        assert "recovery" not in parsed


class TestPurgeReport:
    """Test purge section in reports."""

    def test_human_report_with_purge(self, hamma_scrub):
        """Human report includes purge section with deleted and retained."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "failed": [],
            "retained": [
                {"filename": "ags002.bin", "reason": "active file"},
            ],
            "dry_run": False,
        }
        report = hamma_scrub.format_human_report(
            results, purge=purge,
        )
        assert "=== Purge ===" in report
        assert "Deleted: 1" in report
        assert "Retained: 1" in report
        assert "ags002.bin" in report
        assert "active file" in report

    def test_human_report_purge_dry_run(self, hamma_scrub):
        """Human report shows 'Would delete' in dry-run mode."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "failed": [],
            "retained": [],
            "dry_run": True,
        }
        report = hamma_scrub.format_human_report(
            results, purge=purge,
        )
        assert "Would delete: 1" in report

    def test_human_report_no_purge(self, hamma_scrub):
        """Human report without purge has no purge section."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        report = hamma_scrub.format_human_report(results, purge=None)
        assert "Purge" not in report

    def test_json_report_with_purge(self, hamma_scrub):
        """JSON report includes purge key."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        purge = {
            "deleted": ["ags001.bin"],
            "retained": [{"filename": "ags002.bin", "reason": "active file"}],
            "dry_run": False,
        }
        report_str = hamma_scrub.format_json_report(
            results, "hamma", purge=purge,
        )
        data = json.loads(report_str)
        assert "purge" in data
        assert data["purge"]["deleted"] == ["ags001.bin"]
        assert data["purge"]["dry_run"] is False

    def test_json_report_no_purge(self, hamma_scrub):
        """JSON report without purge has no purge key."""
        results = {
            "ags_triggers": 10, "ags_files": 2, "ags_elapsed": 1.0,
            "ags_duplicate_count": 0,
            "mj_triggers": 10, "mj_files_scanned": 5, "mj_duplicate_count": 0,
            "mj_elapsed": 0.5, "matched": 10, "missing_on_mj": [],
            "mj_only_count": 0,
        }
        report_str = hamma_scrub.format_json_report(
            results, "hamma", purge=None,
        )
        data = json.loads(report_str)
        assert "purge" not in data


class TestFormatReport:
    """Test human-readable and JSON report generation."""

    def _make_results(self):
        """Build a sample results dict for testing."""
        missing_hdr = b'\xf5\xff\x50\x5d' + b'\x00' * 124
        return {
            "ags_triggers": 100,
            "ags_files": 2,
            "ags_elapsed": 5.2,
            "ags_duplicate_count": 0,
            "mj_triggers": 110,
            "mj_files_scanned": 112,
            "mj_duplicate_count": 2,
            "mj_elapsed": 3.1,
            "matched": 95,
            "missing_on_mj": [
                {"filename": "data.bin", "offset": 0, "index": 0,
                 "header": missing_hdr},
            ],
            "mj_only_count": 15,
            "warnings": [],
        }

    def test_human_report_contains_counts(self, hamma_scrub):
        results = self._make_results()
        report = hamma_scrub.format_human_report(results)
        assert "100" in report
        assert "110" in report
        assert "95" in report
        assert "Missing on MJ" in report
        assert "data.bin" in report

    def test_human_report_no_missing(self, hamma_scrub):
        results = self._make_results()
        results["missing_on_mj"] = []
        results["matched"] = 100
        report = hamma_scrub.format_human_report(results)
        assert "No missing triggers" in report

    def test_human_report_limit_truncates(self, hamma_scrub):
        """Default limit truncates missing trigger details."""
        results = self._make_results()
        # Add 30 missing entries (more than DEFAULT_LIMIT=20)
        missing_hdr = b'\xf5\xff\x50\x5d' + b'\x00' * 124
        results["missing_on_mj"] = [
            {"filename": "data.bin", "offset": i * 22000132, "index": i,
             "header": missing_hdr}
            for i in range(30)
        ]
        report = hamma_scrub.format_human_report(results, limit=20)
        # Should show 20 detail lines + truncation message
        assert "... and 10 more" in report
        assert "--limit 0" in report
        # Only 20 detail lines (trigger #0 through #19)
        assert "trigger #19" in report
        assert "trigger #20" not in report

    def test_human_report_limit_zero_shows_all(self, hamma_scrub):
        """limit=0 shows all missing trigger details."""
        results = self._make_results()
        missing_hdr = b'\xf5\xff\x50\x5d' + b'\x00' * 124
        results["missing_on_mj"] = [
            {"filename": "data.bin", "offset": i * 22000132, "index": i,
             "header": missing_hdr}
            for i in range(30)
        ]
        report = hamma_scrub.format_human_report(results, limit=0)
        assert "... and" not in report
        assert "trigger #29" in report

    def test_json_report_structure(self, hamma_scrub):
        results = self._make_results()
        j = hamma_scrub.format_json_report(results, "10.10.10.1")
        parsed = json.loads(j)
        assert parsed["ags_triggers"] == 100
        assert parsed["matched"] == 95
        assert len(parsed["missing_on_mj"]) == 1
        assert "scan_time" in parsed
        assert "ags_host" in parsed


class TestCLI:
    """Test argument parsing."""

    def test_default_args(self, hamma_scrub):
        parser = hamma_scrub._build_parser()
        args = parser.parse_args([])
        assert args.ags_host == "hamma"
        assert args.ags_path == "/ags/data"
        assert args.mj_path == "/media/pi"
        assert args.verbose is False
        assert args.json is False
        assert args.output is None
        assert args.limit == 20
        assert args.since is None

    def test_custom_args(self, hamma_scrub):
        parser = hamma_scrub._build_parser()
        args = parser.parse_args([
            "--ags-host", "192.168.1.1",
            "--ags-path", "/data",
            "--mj-path", "/mnt",
            "--output", "report.json",
            "--verbose",
            "--json",
        ])
        assert args.ags_host == "192.168.1.1"
        assert args.ags_path == "/data"
        assert args.mj_path == "/mnt"
        assert args.output == "report.json"
        assert args.verbose is True
        assert args.json is True

    def test_recover_flag(self, hamma_scrub):
        parser = hamma_scrub._build_parser()
        args = parser.parse_args(["--recover"])
        assert args.recover is True

    def test_recover_default(self, hamma_scrub):
        parser = hamma_scrub._build_parser()
        args = parser.parse_args([])
        assert args.recover is False

    def test_recover_with_dry_run(self, hamma_scrub):
        parser = hamma_scrub._build_parser()
        args = parser.parse_args(["--recover", "--dry-run"])
        assert args.recover is True
        assert args.dry_run is True

    def test_purge_flag(self, hamma_scrub):
        """--purge flag is parsed."""
        parser = hamma_scrub._build_parser()
        args = parser.parse_args(["--recover", "--purge"])
        assert args.purge is True

    def test_purge_default(self, hamma_scrub):
        """--purge defaults to False."""
        parser = hamma_scrub._build_parser()
        args = parser.parse_args([])
        assert args.purge is False

    def test_since_auto(self, hamma_scrub):
        """--since auto is accepted."""
        parser = hamma_scrub._build_parser()
        args = parser.parse_args(["--since", "auto"])
        assert args.since == "auto"


class TestMain:
    """Test main() integration."""

    @pytest.fixture(autouse=True)
    def _stub_control_master(self, hamma_scrub):
        """run() opens a real SSH ControlMaster + writes a status/metrics file
        to the real home; stub them out in unit tests."""
        with patch.object(hamma_scrub, "open_control_master",
                          return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "write_status"), \
             patch.object(hamma_scrub, "write_scan_metrics"):
            yield

    def test_exit_code_0_all_match(self, hamma_scrub):
        """All matched -> exit code 0."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {hdr},
            "file_count": 1,
            "duplicate_count": 0,
            "skipped": 0,
            "elapsed": 1.0,
        }
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result):
            rc = hamma_scrub.run("10.10.10.1", "/ags/data", "/media/pi")
        assert rc == 0

    def test_exit_code_1_missing(self, hamma_scrub):
        """Missing triggers -> exit code 1."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        other_hdr = b'\xf5\xff\x50\x5d' + b'\x02' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {other_hdr},
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "elapsed": 1.0,
        }
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result):
            rc = hamma_scrub.run("10.10.10.1", "/ags/data", "/media/pi")
        assert rc == 1

    def test_exit_code_0_empty_ags(self, hamma_scrub):
        """Empty AGS -> exit code 0 (not an error, per spec #9)."""
        ags_result = {
            "entries": [],
            "headers": set(),
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files") as mock_mj:
            rc = hamma_scrub.run("10.10.10.1", "/ags/data", "/media/pi")
        assert rc == 0
        mock_mj.assert_called_once()

    def test_exit_code_2_ssh_error(self, hamma_scrub):
        """SSH failure -> exit code 2."""
        with patch.object(hamma_scrub, "scan_ags_files",
                          side_effect=RuntimeError("SSH failed")):
            rc = hamma_scrub.run("10.10.10.1", "/ags/data", "/media/pi")
        assert rc == 2

    def test_exit_code_3_no_data_drives(self, hamma_scrub):
        """No DATA drives -> exit code 3."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": set(),
            "file_count": 0,
            "duplicate_count": 0,
            "skipped": 0,
            "elapsed": 1.0,
        }
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result):
            rc = hamma_scrub.run("10.10.10.1", "/ags/data", "/media/pi")
        assert rc == 3

    def test_run_with_recover_calls_recovery(self, hamma_scrub, tmp_path):
        """run() with recover=True invokes recovery flow."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        other_hdr = b'\xf5\xff\x50\x5d' + b'\x02' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {other_hdr},
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 1.0,
        }
        mock_recovery = [{
            "source_file": "f.bin",
            "source_offset": 0,
            "trigger_index": 0,
            "target_path": "DATA37/test/recovered.bin",
            "size": 100,
            "status": "recovered",
            "header": hdr,
            "error": None,
        }]
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "cleanup_orphaned_temps", return_value=0), \
             patch.object(hamma_scrub, "filter_recovery_candidates") as mock_filter, \
             patch.object(hamma_scrub, "recover_triggers", return_value=mock_recovery) as mock_recover:
            mock_filter.return_value = [
                {"header": hdr, "filename": "f.bin", "offset": 0,
                 "index": 0, "skip_reason": None}
            ]
            rc = hamma_scrub.run(
                "hamma", "/ags/data", str(tmp_path),
                recover=True,
            )
        assert rc == 0  # All missing recovered -> EXIT_OK
        mock_recover.assert_called_once()
        mock_filter.assert_called_once()

    def test_run_partial_recovery_still_exit_missing(self, hamma_scrub, tmp_path):
        """run() returns EXIT_MISSING when some recoveries fail."""
        hdr_ok = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        hdr_fail = b'\xf5\xff\x50\x5d' + b'\x02' + b'\x00' * 123
        ags_result = {
            "entries": [
                {"header": hdr_ok, "filename": "f.bin", "offset": 0, "index": 0},
                {"header": hdr_fail, "filename": "f.bin", "offset": 1000, "index": 1},
            ],
            "headers": {hdr_ok, hdr_fail},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": set(),
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 1.0,
        }
        mock_recovery = [
            {"source_file": "f.bin", "source_offset": 0, "trigger_index": 0,
             "target_path": "DATA37/test/ok.bin", "size": 100,
             "status": "recovered", "header": hdr_ok, "error": None},
            {"source_file": "f.bin", "source_offset": 1000, "trigger_index": 1,
             "target_path": None, "size": 0,
             "status": "failed", "header": hdr_fail, "error": "disk full"},
        ]
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "cleanup_orphaned_temps", return_value=0), \
             patch.object(hamma_scrub, "filter_recovery_candidates") as mock_filter, \
             patch.object(hamma_scrub, "recover_triggers", return_value=mock_recovery):
            mock_filter.return_value = [
                {"header": hdr_ok, "filename": "f.bin", "offset": 0,
                 "index": 0, "skip_reason": None},
                {"header": hdr_fail, "filename": "f.bin", "offset": 1000,
                 "index": 1, "skip_reason": None},
            ]
            rc = hamma_scrub.run(
                "hamma", "/ags/data", str(tmp_path),
                recover=True,
            )
        assert rc == 1  # One failed -> EXIT_MISSING

    def test_run_without_recover_no_recovery(self, hamma_scrub):
        """run() without recover=True does NOT invoke recovery."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        other_hdr = b'\xf5\xff\x50\x5d' + b'\x02' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {other_hdr},
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 1.0,
        }
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "recover_triggers") as mock_recover:
            rc = hamma_scrub.run("hamma", "/ags/data", "/media/pi")
        assert rc == 1
        mock_recover.assert_not_called()

    def test_purge_without_recover_errors(self, hamma_scrub):
        """--purge without --recover returns error exit code (early exit, no scanning)."""
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/home/pi/data",
            purge=True, recover=False,
        )
        assert rc == hamma_scrub.EXIT_NO_DATA

    def test_run_with_purge_calls_purge(self, hamma_scrub):
        """run() with purge=True calls identify_purgeable_files and purge_ags_files."""
        h1 = b'\x01' * 128
        ags_result = {
            "entries": [
                {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            ],
            "headers": {h1},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {h1},
            "file_count": 1,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 0.5,
        }

        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "identify_purgeable_files",
                          return_value={"purgeable": ["ags001.bin"],
                                        "retained": []}) as mock_identify, \
             patch.object(hamma_scrub, "purge_ags_files",
                          return_value=[{"filename": "ags001.bin",
                                         "status": "deleted",
                                         "error": None}]) as mock_purge:
            rc = hamma_scrub.run(
                "hamma", "/ags/data", "/home/pi/data",
                recover=True, purge=True,
            )

        mock_identify.assert_called_once()
        mock_purge.assert_called_once()
        assert rc == hamma_scrub.EXIT_OK

    def test_run_without_purge_no_purge(self, hamma_scrub):
        """run() without purge=True does not call purge functions."""
        h1 = b'\x01' * 128
        ags_result = {
            "entries": [
                {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
            ],
            "headers": {h1},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {h1},
            "file_count": 1,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 0.5,
        }

        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "identify_purgeable_files") as mock_identify, \
             patch.object(hamma_scrub, "purge_ags_files") as mock_purge:
            rc = hamma_scrub.run(
                "hamma", "/ags/data", "/home/pi/data",
                recover=True, purge=False,
            )

        mock_identify.assert_not_called()
        mock_purge.assert_not_called()

    def test_run_purge_no_missing_still_purges(self, hamma_scrub):
        """Purge runs even when there are no missing triggers."""
        h1 = b'\x01' * 128
        ags_result = {
            "entries": [
                {"filename": "ags001.bin", "offset": 0, "index": 0, "header": h1},
                {"filename": "ags002.bin", "offset": 0, "index": 0, "header": h1},
            ],
            "headers": {h1},
            "duplicate_count": 1,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {h1},
            "file_count": 1,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 0.5,
        }

        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj_result), \
             patch.object(hamma_scrub, "identify_purgeable_files",
                          return_value={"purgeable": ["ags001.bin"],
                                        "retained": [{"filename": "ags002.bin",
                                                       "reason": "active file"}]}) as mock_identify, \
             patch.object(hamma_scrub, "purge_ags_files",
                          return_value=[{"filename": "ags001.bin",
                                         "status": "deleted",
                                         "error": None}]) as mock_purge:
            rc = hamma_scrub.run(
                "hamma", "/ags/data", "/home/pi/data",
                recover=True, purge=True,
            )

        # Purge was called even though no triggers were missing
        mock_identify.assert_called_once()
        mock_purge.assert_called_once()


class TestRunSinceAuto:
    """Test --since auto integration in run()."""

    @pytest.fixture(autouse=True)
    def _stub_control_master(self, hamma_scrub):
        """run() opens a real SSH ControlMaster + writes a status/metrics file
        to the real home; stub them out in unit tests."""
        with patch.object(hamma_scrub, "open_control_master",
                          return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "write_status"), \
             patch.object(hamma_scrub, "write_scan_metrics"):
            yield

    def test_since_auto_derives_cutoff_from_ags(self, hamma_scrub):
        """run() with since='auto' derives cutoff from AGS entries."""
        hdr = _make_gps_header()
        expected_cutoff = hamma_scrub.decode_gps_time(hdr)[:13]
        ags_result = {
            "entries": [{"header": hdr, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {hdr},
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 1.0,
        }

        with patch.object(hamma_scrub, "scan_ags_files",
                          return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files",
                          return_value=mj_result) as mock_mj:
            rc = hamma_scrub.run(
                ags_host="hamma", ags_path="/ags/data",
                mj_path="/media/pi", since="auto",
            )
            # scan_mj_files called with cutoff derived from AGS data
            mock_mj.assert_called_once()
            assert mock_mj.call_args.args[0] == "/media/pi"
            assert mock_mj.call_args.kwargs["since"] == expected_cutoff
            assert rc == 0

    def test_since_auto_no_valid_gps_scans_all(self, hamma_scrub):
        """If no AGS entry has valid GPS, no since filter is applied."""
        hdr_bad = _make_gps_header(week=0, tow=0.0)
        hdr_mj = b'\xf5\xff\x50\x5d' + b'\x02' + b'\x00' * 123
        ags_result = {
            "entries": [{"header": hdr_bad, "filename": "f.bin",
                         "offset": 0, "index": 0}],
            "headers": {hdr_bad},
            "duplicate_count": 0,
            "elapsed": 1.0,
        }
        mj_result = {
            "headers": {hdr_mj},
            "file_count": 5,
            "duplicate_count": 0,
            "skipped": 0,
            "dirs_skipped": 0,
            "elapsed": 1.0,
        }

        with patch.object(hamma_scrub, "scan_ags_files",
                          return_value=ags_result), \
             patch.object(hamma_scrub, "scan_mj_files",
                          return_value=mj_result) as mock_mj:
            rc = hamma_scrub.run(
                ags_host="hamma", ags_path="/ags/data",
                mj_path="/media/pi", since="auto",
            )
            # scan_mj_files called without since filter
            mock_mj.assert_called_once()
            assert mock_mj.call_args.kwargs["since"] is None

    def test_since_auto_ags_scan_fails(self, hamma_scrub):
        """If AGS scan fails, return EXIT_SSH_ERROR."""
        with patch.object(hamma_scrub, "scan_ags_files",
                          side_effect=RuntimeError("SSH failed")):
            rc = hamma_scrub.run(
                ags_host="hamma", ags_path="/ags/data",
                mj_path="/media/pi", since="auto",
            )
            assert rc == hamma_scrub.EXIT_SSH_ERROR


class TestSshCmd:
    """Test the ssh command builder (BatchMode/ConnectTimeout + optional ControlMaster)."""

    def test_basic_command_has_host_and_remote(self, hamma_scrub):
        cmd = hamma_scrub.ssh_cmd("hamma", "rm -f /x")
        assert cmd[0] == "ssh"
        assert "hamma" in cmd
        assert cmd[-1] == "rm -f /x"

    def test_includes_batchmode_and_connecttimeout(self, hamma_scrub):
        joined = " ".join(hamma_scrub.ssh_cmd("hamma", "true"))
        assert "BatchMode=yes" in joined
        assert "ConnectTimeout=" in joined

    def test_no_control_path_by_default(self, hamma_scrub):
        joined = " ".join(hamma_scrub.ssh_cmd("hamma", "true"))
        assert "ControlPath" not in joined

    def test_control_path_added_when_given(self, hamma_scrub):
        joined = " ".join(
            hamma_scrub.ssh_cmd("hamma", "true", control_path="/tmp/cm.sock"))
        assert "ControlPath=/tmp/cm.sock" in joined


class TestPurgeBatching:
    """Purge deletes in chunks over one connection, not one SSH per file."""

    def _ok(self):
        m = MagicMock()
        m.returncode = 0
        m.stderr = b''
        return m

    def test_one_ssh_call_per_chunk_not_per_file(self, hamma_scrub):
        files = ["ags{:03d}.bin".format(i) for i in range(250)]
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", files, dry_run=False)
        # 250 files, chunk size 100 -> 3 calls, not 250
        assert mock_run.call_count == 3
        assert len(results) == 250
        assert all(r["status"] == "deleted" for r in results)

    def test_writes_per_chunk_heartbeat(self, hamma_scrub):
        """A long purge advances the heartbeat per chunk so the monitor can't
        mistake a working purge for a hung one (GAP: purge was heartbeat-blind)."""
        files = ["ags{:03d}.bin".format(i) for i in range(250)]  # 3 chunks
        with patch.object(hamma_scrub, "write_status") as mock_ws, \
             patch("subprocess.run", return_value=self._ok()):
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", files, dry_run=False,
                status_file="/tmp/s.json")
        purge_beats = [c for c in mock_ws.call_args_list
                       if len(c.args) >= 2 and c.args[1] == "purge"]
        # 3 pre-chunk beats + 1 final beat (reflects the last chunk's deletions)
        assert len(purge_beats) == 4
        assert all(c.args[0] == "/tmp/s.json" for c in purge_beats)

    def test_final_heartbeat_reflects_last_chunk(self, hamma_scrub):
        """A final heartbeat after the loop must report the FULL deleted count.
        The per-chunk beat fires BEFORE its chunk's rm, so without a trailing
        write the last chunk's deletions never reach the heartbeat before the
        'done' phase. Jeff review #2."""
        files = ["ags{:03d}.bin".format(i) for i in range(250)]  # 3 chunks
        with patch.object(hamma_scrub, "write_status") as mock_ws, \
             patch("subprocess.run", return_value=self._ok()):
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", files, dry_run=False,
                status_file="/tmp/s.json")
        purge_beats = [c for c in mock_ws.call_args_list
                       if len(c.args) >= 2 and c.args[1] == "purge"]
        assert purge_beats[-1].kwargs.get("purged") == 250  # all, not 200

    def test_chunk_command_deletes_multiple_files(self, hamma_scrub):
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin", "b.bin"], dry_run=False)
        remote = mock_run.call_args[0][0][-1]
        assert remote.startswith("nice -n 19 rm -f ")  # niced below the DAS writer
        assert "/ags/data/a.bin" in remote
        assert "/ags/data/b.bin" in remote

    def test_per_file_retry_rm_is_niced(self, hamma_scrub):
        """The per-file retry after a partial-chunk failure is niced too."""
        batch_fail = MagicMock()
        batch_fail.returncode = 1
        batch_fail.stderr = b'rm: cannot remove one'
        with patch("subprocess.run",
                   side_effect=[batch_fail, self._ok(), self._ok()]) as mock_run:
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin", "b.bin"], dry_run=False)
        # calls[1] and [2] are the per-file retries
        retry = mock_run.call_args_list[1][0][0][-1]
        assert retry.startswith("nice -n 19 rm -f ")

    def test_chunk_failure_marks_all_in_chunk_failed(self, hamma_scrub):
        m = MagicMock()
        m.returncode = 255
        m.stderr = b'Connection closed by remote host'
        with patch("subprocess.run", return_value=m):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin", "b.bin"], dry_run=False)
        assert all(r["status"] == "failed" for r in results)
        assert "Connection closed" in results[0]["error"]

    def test_partial_chunk_failure_attributes_per_file(self, hamma_scrub):
        """A batched delete that returns non-zero retries per-file, so files
        that WERE deleted are not mis-reported as failed. `rm -f a b c` can
        delete a and c while erroring on b yet exit non-zero -- the report
        must not claim all three failed (it would lie during a disk-fill)."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            remote = cmd[-1]
            m = MagicMock()
            if calls["n"] == 1:
                # the batched `rm -f a b c` -- one file errored -> non-zero
                m.returncode = 1
                m.stderr = b"rm: /ags/data/b.bin: Permission denied"
            else:
                # per-file retries: only b.bin fails
                fails = "b.bin" in remote
                m.returncode = 1 if fails else 0
                m.stderr = b"rm: Permission denied" if fails else b""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin", "b.bin", "c.bin"],
                dry_run=False)
        by = {r["filename"]: r["status"] for r in results}
        assert by == {"a.bin": "deleted", "b.bin": "failed", "c.bin": "deleted"}

    @pytest.mark.parametrize("nfiles,expected_calls", [
        (0, 0), (1, 1), (99, 1), (100, 1), (101, 2), (200, 2), (250, 3)])
    def test_chunk_boundaries(self, hamma_scrub, nfiles, expected_calls):
        """Exact-multiple boundaries: no spurious empty trailing chunk."""
        files = ["f{}.bin".format(i) for i in range(nfiles)]
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            results = hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", files, dry_run=False)
        assert mock_run.call_count == expected_calls
        assert len(results) == nfiles
        assert all(r["status"] == "deleted" for r in results)

    def test_passes_control_path_through(self, hamma_scrub):
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin"], dry_run=False,
                control_path="/tmp/cm.sock")
        joined = " ".join(mock_run.call_args[0][0])
        assert "ControlPath=/tmp/cm.sock" in joined


class TestSshControlMaster:
    """Test the shared-connection context manager."""

    def _ok(self):
        m = MagicMock()
        m.returncode = 0
        m.stderr = b''
        return m

    def test_yields_socket_path_on_success(self, hamma_scrub):
        with patch("subprocess.run", return_value=self._ok()):
            with hamma_scrub.ssh_control_master("hamma") as cp:
                assert cp is not None
                assert isinstance(cp, str)

    def test_yields_none_when_master_fails(self, hamma_scrub):
        bad = MagicMock()
        bad.returncode = 255
        bad.stderr = b'connect failed'
        with patch("subprocess.run", return_value=bad):
            with hamma_scrub.ssh_control_master("hamma") as cp:
                assert cp is None

    def test_yields_none_on_setup_timeout(self, hamma_scrub):
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
            with hamma_scrub.ssh_control_master("hamma") as cp:
                assert cp is None

    def test_tears_down_master_on_exit(self, hamma_scrub):
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            with hamma_scrub.ssh_control_master("hamma"):
                pass
        last_cmd = mock_run.call_args_list[-1][0][0]
        assert "-O" in last_cmd
        assert "exit" in last_cmd


class TestExtractTriggerControlPath:
    """extract_trigger routes through ssh_cmd and honors control_path."""

    def test_control_path_in_ssh_command(self, hamma_scrub):
        header, body = _make_trigger()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = header + body
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            hamma_scrub.extract_trigger(
                "hamma", "/ags/data", "ags001.bin", 1000, len(header + body),
                control_path="/tmp/cm.sock")
        joined = " ".join(mock_run.call_args[0][0])
        assert "ControlPath=/tmp/cm.sock" in joined


class TestRunControlMasterWiring:
    """run() must open ONE ControlMaster, thread its socket to BOTH recover
    and purge, and close it. This is the integration seam the leaf tests miss;
    without it, dropping control_path (or the open/close) passes silently.
    Deliberately NO autouse control-master stub here."""

    def test_control_path_threaded_to_recover_and_purge_and_closed(
            self, hamma_scrub):
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        ags = {"entries": [{"header": hdr, "filename": "f.bin",
                            "offset": 0, "index": 0}],
               "headers": {hdr}, "duplicate_count": 0, "elapsed": 1.0}
        mj = {"headers": set(), "file_count": 5, "duplicate_count": 0,
              "skipped": 0, "elapsed": 1.0}
        sentinel = "/tmp/sentinel_cm.sock"
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj), \
             patch.object(hamma_scrub, "write_status"), \
             patch.object(hamma_scrub, "write_scan_metrics"), \
             patch.object(hamma_scrub, "open_control_master",
                          return_value=sentinel) as mock_open, \
             patch.object(hamma_scrub, "close_control_master") as mock_close, \
             patch.object(hamma_scrub, "recover_triggers",
                          return_value=[]) as mock_recover, \
             patch.object(hamma_scrub, "identify_purgeable_files",
                          return_value={"purgeable": ["f.bin"],
                                        "retained": []}), \
             patch.object(hamma_scrub, "purge_ags_files",
                          return_value=[]) as mock_purge:
            hamma_scrub.run("hamma", "/ags/data", "/media/pi",
                            recover=True, purge=True)

        mock_open.assert_called_once_with("hamma")
        assert mock_recover.call_args.kwargs.get("control_path") == sentinel
        assert mock_purge.call_args.kwargs.get("control_path") == sentinel
        mock_close.assert_called_once_with("hamma", sentinel)

    def test_refreshes_recovered_dirs_with_ABSOLUTE_paths(self, hamma_scrub):
        """recover_triggers records target_path RELATIVE to mj_path, but the scan
        cache is keyed by ABSOLUTE dirs. run() must convert before refreshing, or
        the refresh silently no-ops (relative dir != cache key)."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        ags = {"entries": [{"header": hdr, "filename": "f.bin",
                            "offset": 0, "index": 0}],
               "headers": {hdr}, "duplicate_count": 0, "elapsed": 1.0}
        mj = {"headers": set(), "file_count": 5, "duplicate_count": 0,
              "skipped": 0, "elapsed": 1.0, "cache_hits": 0, "dirs_total": 5}
        # target_path exactly as recover_triggers emits it: relpath to mj_path
        rec = [{"status": "recovered", "header": hdr,
                "target_path": "DATA01/2026-07-14T12/mj05_x_recovered.bin",
                "source_file": "f.bin", "source_offset": 0}]
        captured = {}
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj), \
             patch.object(hamma_scrub, "write_status"), \
             patch.object(hamma_scrub, "write_scan_metrics"), \
             patch.object(hamma_scrub, "open_control_master", return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "recover_triggers", return_value=rec), \
             patch.object(hamma_scrub, "identify_purgeable_files",
                          return_value={"purgeable": [], "retained": []}), \
             patch.object(hamma_scrub, "purge_ags_files", return_value=[]), \
             patch.object(hamma_scrub, "_refresh_cache_dirs",
                          side_effect=lambda cf, d: captured.setdefault(
                              "dirs", sorted(d))):
            hamma_scrub.run("hamma", "/ags/data", "/media/pi",
                            recover=True, mj_cache="/tmp/c.json",
                            metrics_file=None)
        assert captured["dirs"] == ["/media/pi/DATA01/2026-07-14T12"]

    def test_run_calls_write_scan_metrics_at_completion(self, hamma_scrub):
        """The metrics call must be wired into run() -- guards against the call
        site being deleted (the run-test fixtures otherwise mock it silently)."""
        hdr = b'\xf5\xff\x50\x5d' + b'\x01' + b'\x00' * 123
        ags = {"entries": [{"header": hdr, "filename": "f.bin",
                            "offset": 0, "index": 0}],
               "headers": {hdr}, "duplicate_count": 0, "elapsed": 1.0}
        mj = {"headers": {hdr}, "file_count": 5, "duplicate_count": 0,
              "skipped": 0, "elapsed": 2.5, "cache_hits": 4, "dirs_total": 5}
        with patch.object(hamma_scrub, "scan_ags_files", return_value=ags), \
             patch.object(hamma_scrub, "scan_mj_files", return_value=mj), \
             patch.object(hamma_scrub, "write_status"), \
             patch.object(hamma_scrub, "open_control_master", return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "write_scan_metrics") as mock_metrics:
            hamma_scrub.run("hamma", "/ags/data", "/media/pi",
                            metrics_file="/tmp/m.csv")
        mock_metrics.assert_called_once()
        args = mock_metrics.call_args.args
        assert args[0] == "/tmp/m.csv" and args[1] is mj   # (path, mj, ...)
        assert args[2] == 0 and args[3] == 0               # recovered, purged


class TestWriteStatus:
    """Scrub writes an atomic heartbeat/status file for the monitor to read."""

    def test_writes_json_with_timestamp_phase_pid_counts(self, hamma_scrub,
                                                         tmp_path):
        p = str(tmp_path / "sub" / "status.json")  # dir does not exist yet
        hamma_scrub.write_status(p, "purge", recovered=2, purged=10)
        data = json.loads(pathlib.Path(p).read_text())
        assert data["phase"] == "purge"
        assert data["recovered"] == 2 and data["purged"] == 10
        assert data["pid"] == os.getpid()
        assert isinstance(data["timestamp"], (int, float))

    def test_none_path_is_noop(self, hamma_scrub):
        hamma_scrub.write_status(None, "scan")  # must not raise

    def test_write_failure_is_swallowed(self, hamma_scrub):
        # Unwritable location -> logged at debug, never raised (status is
        # best-effort; a failed heartbeat must not crash the scrub).
        hamma_scrub.write_status("/proc/cannot/write/status.json", "scan")

    def test_atomic_replace_used(self, hamma_scrub, tmp_path):
        # Overwriting an existing status file must not leave a partial file.
        p = str(tmp_path / "status.json")
        hamma_scrub.write_status(p, "scan", purged=0)
        hamma_scrub.write_status(p, "done", purged=5)
        data = json.loads(pathlib.Path(p).read_text())
        assert data["phase"] == "done" and data["purged"] == 5

    def test_writes_via_temp_then_os_replace(self, hamma_scrub, tmp_path):
        # Atomicity MECHANISM: writes go to a temp path then os.replace onto the
        # target (a reader never sees a torn file). A non-atomic direct write
        # would fail this.
        p = str(tmp_path / "status.json")
        with patch("os.replace", wraps=os.replace) as mock_replace:
            hamma_scrub.write_status(p, "scan", purged=0)
        assert mock_replace.call_count == 1
        src, dst = mock_replace.call_args[0]
        assert src == p + ".tmp" and dst == p


class TestIncrementalScan:
    """§3.5: scan_mj_files(cache_file=...) reuses unchanged hourly dirs."""

    def _dir(self, tmp_path, drive="DATA37", hour="2026-04-10T14"):
        d = tmp_path / drive / hour
        d.mkdir(parents=True)
        return d

    def _hdr(self, byte50):
        hdr, rest = _make_trigger()
        hdr = bytearray(hdr)
        hdr[50] = byte50
        return bytes(hdr), rest

    def _two_dirs(self, tmp_path):
        """Older + newest hourly dir, each with one .bin. Returns (older,newer)."""
        older = tmp_path / "DATA37" / "2026-04-10T14"
        newer = tmp_path / "DATA37" / "2026-04-10T15"  # newest -> force-rescanned
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        ho, rest = self._hdr(1)
        hn, _ = self._hdr(2)
        (older / "a.bin").write_bytes(ho + rest)
        (newer / "a.bin").write_bytes(hn + rest)
        return older, newer, ho, hn, rest

    def test_unchanged_older_dir_is_cache_hit_newest_rescanned(
            self, hamma_scrub, tmp_path):
        older, newer, ho, hn, rest = self._two_dirs(tmp_path)
        cache = str(tmp_path / "c.json")
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)  # build
        with patch.object(hamma_scrub, "_read_dir_headers",
                          return_value=(set(), 0, 0)) as mock_read:
            r2 = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert r2["cache_hits"] == 1                    # older reused
        assert mock_read.call_count == 1                # only the newest re-read
        assert mock_read.call_args[0][0].endswith("2026-04-10T15")

    def test_older_dir_mtime_change_invalidates(self, hamma_scrub, tmp_path):
        """In-place content rewrite (same count) with a bumped dir mtime must
        invalidate -- catches an mtime-blind signature."""
        older, newer, ho, hn, rest = self._two_dirs(tmp_path)
        cache = str(tmp_path / "c.json")
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        h2, _ = self._hdr(7)
        (older / "a.bin").write_bytes(h2 + rest)         # same count, new content
        os.utime(str(older), (9e9, 9e9))                 # bump older's mtime
        res = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert h2 in res["headers"] and ho not in res["headers"]

    def test_older_dir_count_change_invalidates(self, hamma_scrub, tmp_path):
        """A new file (count change) invalidates even with mtime pinned --
        catches a count-blind signature."""
        older, newer, ho, hn, rest = self._two_dirs(tmp_path)
        cache = str(tmp_path / "c.json")
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        st = os.stat(str(older))
        h2, _ = self._hdr(8)
        (older / "b.bin").write_bytes(h2 + rest)         # count 1 -> 2
        os.utime(str(older), (st.st_atime, st.st_mtime))  # pin mtime
        res = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert h2 in res["headers"]

    def test_newest_dir_inplace_growth_reread(self, hamma_scrub, tmp_path):
        """A .bin completing IN PLACE (brokkr append-write) in the newest dir --
        same count, maybe same 2s-vfat mtime -- is still caught, because the
        newest dir is always re-read."""
        d = self._dir(tmp_path)  # single dir == newest
        (d / "a.bin").write_bytes(b"\x00" * 8)  # truncated -> skipped
        cache = str(tmp_path / "c.json")
        r1 = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert len(r1["headers"]) == 0 and r1["skipped"] == 1
        hdr, rest = _make_trigger()
        (d / "a.bin").write_bytes(hdr + rest)   # completes in place, count == 1
        r2 = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert hdr in r2["headers"]

    def test_save_failure_does_not_raise(self, hamma_scrub, tmp_path):
        """An unwritable cache location degrades to a full scan, never crashes."""
        d = self._dir(tmp_path)
        hdr, rest = _make_trigger()
        (d / "a.bin").write_bytes(hdr + rest)
        res = hamma_scrub.scan_mj_files(
            str(tmp_path), cache_file="/proc/nonexistent/cache.json")
        assert hdr in res["headers"]

    def test_corrupt_cache_falls_back(self, hamma_scrub, tmp_path):
        d = self._dir(tmp_path)
        hdr, rest = _make_trigger()
        (d / "a.bin").write_bytes(hdr + rest)
        cache = tmp_path / "c.json"
        cache.write_bytes(b"not valid json {{{")
        res = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=str(cache))
        assert hdr in res["headers"] and res["cache_hits"] == 0

    def test_incremental_matches_full_scan(self, hamma_scrub, tmp_path):
        for i, hour in enumerate(["2026-04-10T14", "2026-04-10T15"]):
            d = tmp_path / "DATA37" / hour
            d.mkdir(parents=True)
            hdr, rest = self._hdr(i)
            (d / "a.bin").write_bytes(hdr + rest)
        (tmp_path / "DATA37" / "2026-04-10T16").mkdir(parents=True)
        (tmp_path / "DATA37" / "2026-04-10T16" / "trunc.bin").write_bytes(
            b"\x00" * 8)  # truncated -> skipped in both
        full = hamma_scrub.scan_mj_files(str(tmp_path))
        incr = hamma_scrub.scan_mj_files(
            str(tmp_path), cache_file=str(tmp_path / "c.json"))
        assert incr["headers"] == full["headers"]
        assert incr["file_count"] == full["file_count"]
        assert incr["skipped"] == full["skipped"]
        # No cross-dir duplicate headers here, so the counts agree (they can
        # legitimately diverge for cross-dir dups -- a log stat, never a control).
        assert incr["duplicate_count"] == full["duplicate_count"]

    def test_cache_self_prunes_removed_dir(self, hamma_scrub, tmp_path):
        da = tmp_path / "DATA37" / "2026-04-10T14"
        da.mkdir(parents=True)
        db = tmp_path / "DATA37" / "2026-04-10T15"  # keep a 2nd dir present
        db.mkdir(parents=True)
        hdr, rest = _make_trigger()
        (da / "a.bin").write_bytes(hdr + rest)
        (db / "b.bin").write_bytes(hdr + rest)
        cache = str(tmp_path / "c.json")
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        import shutil as _sh
        _sh.rmtree(str(da))
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        with open(cache) as f:
            saved = json.load(f)
        assert all("2026-04-10T14" not in k for k in saved)  # pruned

    def test_compressed_dir_skipped(self, hamma_scrub, tmp_path):
        comp = tmp_path / "DATA37" / "compressed"
        comp.mkdir(parents=True)
        (comp / "x.bin").write_bytes(b"\x00" * 200)  # must be ignored
        res = hamma_scrub.scan_mj_files(
            str(tmp_path), cache_file=str(tmp_path / "c.json"))
        assert res["file_count"] == 0

    def test_since_filters_dirs(self, hamma_scrub, tmp_path):
        for hour in ["2026-04-10T14", "2026-04-11T09"]:
            d = tmp_path / "DATA37" / hour
            d.mkdir(parents=True)
            hdr, rest = _make_trigger()
            hdr = bytearray(hdr)
            hdr[50] = ord(hour[9])
            (d / "a.bin").write_bytes(bytes(hdr) + rest)
        res = hamma_scrub.scan_mj_files(
            str(tmp_path), since="2026-04-11", cache_file=str(tmp_path / "c.json"))
        assert res["file_count"] == 1 and res["dirs_skipped"] == 1

    def test_empty_cache_file_uses_full_scanner(self, hamma_scrub, tmp_path):
        d = self._dir(tmp_path)
        hdr, rest = _make_trigger()
        (d / "a.bin").write_bytes(hdr + rest)
        res = hamma_scrub.scan_mj_files(str(tmp_path), cache_file="")
        assert "cache_hits" not in res  # dispatched to the full scanner

    def test_refresh_cache_dirs_lets_next_scan_hit_recovered_dir(
            self, hamma_scrub, tmp_path):
        """The cache is saved DURING the scan, before recovery writes new .bin
        files. Refreshing a recovered dir's cache entry lets the NEXT scan
        cache-hit it (with the new header) instead of re-reading it stale."""
        older, newer, ho, hn, rest = self._two_dirs(tmp_path)
        cache = str(tmp_path / "c.json")
        hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)  # older cached
        # simulate recovery writing a recovered trigger into the OLDER dir
        h2, _ = self._hdr(9)
        (older / "r_recovered.bin").write_bytes(h2 + rest)
        hamma_scrub._refresh_cache_dirs(cache, [str(older)])
        # next scan: older is a cache HIT (refreshed sig matches); only newest re-read
        with patch.object(hamma_scrub, "_read_dir_headers",
                          return_value=(set(), 0, 0)) as mock_read:
            r = hamma_scrub.scan_mj_files(str(tmp_path), cache_file=cache)
        assert r["cache_hits"] == 1
        assert mock_read.call_count == 1
        assert mock_read.call_args[0][0].endswith("2026-04-10T15")  # newest only
        assert h2 in r["headers"]  # refresh captured the recovered header

    def test_refresh_cache_dirs_is_safe_noop(self, hamma_scrub, tmp_path):
        """No cache file, empty dir list, or a missing dir must never raise."""
        missing = str(tmp_path / "nope.json")
        hamma_scrub._refresh_cache_dirs(missing, [str(tmp_path / "gone")])
        assert not os.path.exists(missing)          # nothing created from nothing
        hamma_scrub._refresh_cache_dirs(None, [str(tmp_path)])   # None cache
        hamma_scrub._refresh_cache_dirs(str(tmp_path / "c.json"), [])  # no dirs


class TestScanMetrics:
    """A durable per-run CSV of MJ-scan cache performance (hit-rate over time)."""

    HEADER = "utc,dirs_cached,dirs_total,cold,scan_seconds,recovered,purged"

    def test_writes_header_then_row(self, hamma_scrub, tmp_path):
        path = str(tmp_path / "m.csv")
        mj = {"cache_hits": 1191, "dirs_total": 1193, "elapsed": 9.14}
        hamma_scrub.write_scan_metrics(path, mj, recovered=4, purged=78)
        lines = open(path).read().splitlines()
        assert lines[0] == self.HEADER
        # utc is field 0; the rest are the recorded values (warm scan -> cold=0)
        assert lines[1].split(",")[1:] == ["1191", "1193", "0", "9.1", "4", "78"]

    def test_appends_without_duplicating_header_and_flags_cold(
            self, hamma_scrub, tmp_path):
        path = str(tmp_path / "m.csv")
        cold = {"cache_hits": 0, "dirs_total": 1193, "elapsed": 500.0}
        hamma_scrub.write_scan_metrics(path, cold, recovered=0, purged=0)
        hamma_scrub.write_scan_metrics(path, cold, recovered=0, purged=0)
        lines = open(path).read().splitlines()
        assert lines.count(self.HEADER) == 1        # header written once
        assert len(lines) == 3                       # header + 2 rows
        assert lines[1].split(",")[3] == "1"         # 0/1193 -> cold flagged

    def test_full_scanner_has_blank_cache_fields(self, hamma_scrub, tmp_path):
        """When the cache is disabled (full scanner), there are no cache stats;
        the row records blanks rather than a bogus cold flag."""
        path = str(tmp_path / "m.csv")
        hamma_scrub.write_scan_metrics(
            path, {"elapsed": 3.0}, recovered=1, purged=2)
        row = open(path).read().splitlines()[1].split(",")
        assert row[1] == "" and row[2] == "" and row[3] == ""   # no cache stats

    def test_none_path_is_noop(self, hamma_scrub, tmp_path):
        hamma_scrub.write_scan_metrics(
            None, {"cache_hits": 1, "dirs_total": 1, "elapsed": 1.0}, 0, 0)
        assert not (tmp_path / "m.csv").exists()

    def test_size_capped_rotation_bounds_the_file(
            self, hamma_scrub, tmp_path, monkeypatch):
        """Nothing external rotates this file; past the cap it must roll to .1
        and restart so the SD can't fill (HAM-112/113 stance)."""
        path = str(tmp_path / "m.csv")
        monkeypatch.setattr(hamma_scrub, "SCAN_METRICS_MAX_BYTES", 200)
        mj = {"cache_hits": 1, "dirs_total": 2, "elapsed": 1.0}
        for _ in range(30):
            hamma_scrub.write_scan_metrics(path, mj, 0, 0)
        assert os.path.exists(path + ".1")               # old generation kept
        assert os.path.getsize(path) < 400               # live file bounded
        assert open(path).read().splitlines()[0] == self.HEADER  # header restored



# ---------------------------------------------------------------------------
# HAM-164 loss reconciliation (--audit-loss)
# ---------------------------------------------------------------------------

# Records sit around 2026-04-04T01:13:50.808 (see _make_gps_header). Window
# bounds are placed an hour away so the quiescence precondition is satisfied
# by default and only the tests that mean to violate it do.
QUIET_T0 = "2026-04-04T01:00:00"
QUIET_T1 = "2026-04-04T02:00:00"


def _scan(headers, duplicate_count=None, file_count=None,
          duplicate_headers=()):
    """Build a scan-result dict shaped like scan_ags_files/scan_mj_files.

    ``duplicate_headers`` carries the raw header of each duplicate OCCURRENCE,
    exactly as the scanners report it. ``duplicate_count`` defaults to its
    length because that is the invariant both scanners hold -- passing them
    inconsistently is what let the old blocker read a scan-wide tally.
    """
    headers = set(headers)
    duplicate_headers = list(duplicate_headers)
    return {
        "headers": headers,
        "entries": [],
        "duplicate_count": (len(duplicate_headers) if duplicate_count is None
                            else duplicate_count),
        "duplicate_headers": duplicate_headers,
        "file_count": len(headers) if file_count is None else file_count,
        "skipped": 0,
        "elapsed": 0.0,
    }


def _rows(*specs):
    """(stamp, valid, written_gb, sent, dropped) -> telemetry rows."""
    return [
        (t, {"time": t, "valid_packets": str(v), "bytes_written": str(w),
             "packets_sent": str(s), "packets_dropped": str(d)})
        for t, v, w, s, d in specs
    ]


def _simple_rows(valid_delta, written_gb=0.0, dropped_delta=0):
    return _rows(
        (QUIET_T0, 0, 0.0, 0, 0),
        (QUIET_T1, valid_delta, written_gb, 0, dropped_delta),
    )


class TestReconciliationConstants:
    """Pin the constants the derivation depends on (see TestConstants:68)."""

    def test_record_bytes_derived_not_restated(self, hamma_scrub):
        assert hamma_scrub.RECORD_BYTES == 22000132
        assert hamma_scrub.RECORD_BYTES == (
            hamma_scrub.HEADER_SIZE
            + hamma_scrub.EXPECTED_DATASIZE * 2
            + hamma_scrub.PACKET_PAD)

    def test_bytes_written_scale_is_base10_gb(self, hamma_scrub):
        """GB, not GiB. A GiB scale silently inflates derived local drops."""
        assert hamma_scrub.BYTES_WRITTEN_SCALE == 1e9
        assert hamma_scrub.BYTES_WRITTEN_SCALE != 2 ** 30


class TestIsoEpoch:
    """_iso_epoch underpins all boundary arithmetic."""

    def test_naive_treated_as_utc(self, hamma_scrub):
        assert hamma_scrub._iso_epoch("1970-01-01T00:00:10") == 10.0

    def test_explicit_utc_offset(self, hamma_scrub):
        assert hamma_scrub._iso_epoch("1970-01-01T00:00:10+00:00") == 10.0

    def test_zulu_suffix(self, hamma_scrub):
        assert hamma_scrub._iso_epoch("1970-01-01T00:00:10Z") == 10.0

    def test_space_separated_normalized(self, hamma_scrub):
        assert hamma_scrub._iso_epoch("1970-01-01 00:00:10") == 10.0

    def test_nonzero_offset_is_honoured(self, hamma_scrub):
        """A local-time telemetry stamp must not silently shift the window."""
        assert hamma_scrub._iso_epoch("1970-01-01T00:00:10-05:00") == 18010.0

    def test_garbage_returns_none(self, hamma_scrub):
        assert hamma_scrub._iso_epoch("not-a-time") is None
        assert hamma_scrub._iso_epoch("") is None
        assert hamma_scrub._iso_epoch(None) is None


class TestPadIsoFraction:
    """fromisoformat() on 3.7-3.10 (the sensors, Buster) takes ONLY 3 or 6
    fractional digits. This env is 3.12 and parses anything, so these assert
    on the normalized STRING -- the only version-independent evidence.
    """

    @pytest.mark.parametrize("raw,expect", [
        ("2026-09-10T23:59:18.5", "2026-09-10T23:59:18.500000"),
        ("2026-09-10T23:59:18.5+00:00", "2026-09-10T23:59:18.500000+00:00"),
        ("2026-09-10T23:59:18.12", "2026-09-10T23:59:18.120000"),
        ("2026-09-10T23:59:18.12345", "2026-09-10T23:59:18.123450"),
        ("2026-09-10T23:59:18.717", "2026-09-10T23:59:18.717000"),
        ("2026-09-10T23:59:18.717565", "2026-09-10T23:59:18.717565"),
        # >6 digits is sub-microsecond; datetime cannot hold it either way.
        ("2026-09-10T23:59:18.1234567+00:00",
         "2026-09-10T23:59:18.123456+00:00"),
        # No fractional part, and an offset whose ':' must not be touched.
        ("2026-09-10T23:59:18", "2026-09-10T23:59:18"),
        ("2026-09-10T23:59:18+00:00", "2026-09-10T23:59:18+00:00"),
    ])
    def test_normalized_to_six_digits(self, hamma_scrub, raw, expect):
        assert hamma_scrub._pad_iso_fraction(raw) == expect

    @pytest.mark.parametrize("digits", list(range(1, 10)))
    def test_digit_count_is_always_parseable_on_py37(self, hamma_scrub,
                                                     digits):
        """The invariant: whatever the operator types, the string handed to
        fromisoformat has a fractional run of 0 or 6 -- never 1, 2, 5 or 9."""
        raw = "2026-09-10T23:59:18." + ("1" * digits) + "+00:00"
        out = hamma_scrub._pad_iso_fraction(raw)
        run = out.split(".")[1].split("+")[0]
        assert len(run) in (3, 6)

    def test_odd_digit_bound_is_the_same_instant_as_padded(self, hamma_scrub):
        """Same instant whichever form the operator typed. (Passes on 3.12
        with or without the fix -- 3.12 parses '.5' natively. It is the two
        tests above that carry the 3.7 evidence.)"""
        assert (hamma_scrub._iso_epoch("2026-09-10T23:59:18.5+00:00")
                == hamma_scrub._iso_epoch("2026-09-10T23:59:18.500000+00:00"))


class TestNormalizeStamp:
    def test_space_separator_normalized(self, hamma_scrub):
        assert hamma_scrub._normalize_stamp(
            "2026-09-10 23:59:18.717565+00:00"
        ) == "2026-09-10T23:59:18.717565+00:00"

    def test_only_first_space_replaced(self, hamma_scrub):
        assert hamma_scrub._normalize_stamp(
            "2026-09-10 23:59:18 extra") == "2026-09-10T23:59:18 extra"

    def test_already_iso_unchanged(self, hamma_scrub):
        assert hamma_scrub._normalize_stamp(
            "2026-09-10T23:59:18") == "2026-09-10T23:59:18"

    def test_empty_and_none(self, hamma_scrub):
        assert hamma_scrub._normalize_stamp(None) == ""
        assert hamma_scrub._normalize_stamp("   ") == ""


class TestTelemetryFileDate:
    def test_plain_csv(self, hamma_scrub):
        assert hamma_scrub._telemetry_file_date(
            "telemetry_hamma_008_2026-09-10.csv") == "2026-09-10"

    def test_bak(self, hamma_scrub):
        assert hamma_scrub._telemetry_file_date(
            "telemetry_hamma_008_2026-09-10.csv.bak") == "2026-09-10"

    def test_unparseable(self, hamma_scrub):
        assert hamma_scrub._telemetry_file_date("notes.txt") is None


class TestLoadTelemetryRows:
    """Every docstring claim load_telemetry_rows makes, pinned."""

    HEADER = ("time,valid_packets,bytes_written,packets_sent,"
              "packets_dropped,other_column\n")

    def _write(self, tmp_path, name, body):
        (tmp_path / name).write_text(self.HEADER + body)

    def test_reads_and_sorts_across_files(self, tmp_path, hamma_scrub):
        self._write(tmp_path, "telemetry_hamma_008_2026-09-11.csv",
                    "2026-09-11 00:00:00+00:00,300,6.0,291,4,zz\n")
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    "2026-09-10 00:00:00+00:00,100,2.0,95,1,zz\n")
        rows = hamma_scrub.load_telemetry_rows(str(tmp_path))
        assert [s for s, _ in rows] == [
            "2026-09-10T00:00:00+00:00", "2026-09-11T00:00:00+00:00"]

    def test_projects_to_needed_columns_only(self, tmp_path, hamma_scrub):
        """Whole 49-column rows peaked ~3 GB on a 4 GB Pi and OOM'd it."""
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    "2026-09-10 00:00:00+00:00,100,2.0,95,1,zz\n")
        _, row = hamma_scrub.load_telemetry_rows(str(tmp_path))[0]
        assert set(row) == set(hamma_scrub.TELEMETRY_COLUMNS)
        assert "other_column" not in row

    def test_bak_is_read(self, tmp_path, hamma_scrub):
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv.bak",
                    "2026-09-10 00:00:00+00:00,100,2.0,95,1,zz\n")
        assert len(hamma_scrub.load_telemetry_rows(str(tmp_path))) == 1

    def test_live_csv_wins_over_bak_at_equal_stamp(self, tmp_path,
                                                   hamma_scrub):
        """The .bak holds PRE-rotation counters. Letting those win at an equal
        timestamp manufactures a phantom valid_packets reset."""
        stamp = "2026-09-10 00:00:00+00:00"
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv.bak",
                    stamp + ",500000,900.0,499000,9,zz\n")
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    stamp + ",70,2.0,69,0,zz\n")
        rows = hamma_scrub.load_telemetry_rows(str(tmp_path))
        assert len(rows) == 1
        assert rows[0][1]["valid_packets"] == "70"

    def test_nul_bytes_stripped(self, tmp_path, hamma_scrub):
        (tmp_path / "telemetry_hamma_008_2026-09-10.csv").write_text(
            self.HEADER + "2026-09-10 00:00:00+00:00,10\x000,2.0,95,1,zz\n")
        rows = hamma_scrub.load_telemetry_rows(str(tmp_path))
        assert rows[0][1]["valid_packets"] == "100"

    def test_date_filter_skips_out_of_range_files(self, tmp_path,
                                                  hamma_scrub):
        self._write(tmp_path, "telemetry_hamma_008_2023-01-01.csv",
                    "2023-01-01 00:00:00+00:00,1,0.0,1,0,zz\n")
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    "2026-09-10 00:00:00+00:00,100,2.0,95,1,zz\n")
        rows = hamma_scrub.load_telemetry_rows(
            str(tmp_path), since_date="2026-09-09", until_date="2026-09-11")
        assert [s for s, _ in rows] == ["2026-09-10T00:00:00+00:00"]

    def test_non_csv_ignored(self, tmp_path, hamma_scrub):
        (tmp_path / "scrub_metrics.txt").write_text("junk\n")
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    "2026-09-10 00:00:00+00:00,100,2.0,95,1,zz\n")
        assert len(hamma_scrub.load_telemetry_rows(str(tmp_path))) == 1

    def test_blank_time_dropped(self, tmp_path, hamma_scrub):
        self._write(tmp_path, "telemetry_hamma_008_2026-09-10.csv",
                    ",100,2.0,95,1,zz\n"
                    "2026-09-10 00:00:00+00:00,101,2.0,95,1,zz\n")
        assert len(hamma_scrub.load_telemetry_rows(str(tmp_path))) == 1

    def test_missing_dir_raises(self, tmp_path, hamma_scrub):
        with pytest.raises(RuntimeError, match="cannot read telemetry dir"):
            hamma_scrub.load_telemetry_rows(str(tmp_path / "nope"))


class TestCounterField:
    """_counter must reject non-finite values, not merely non-numeric ones."""

    def test_reads_number(self, hamma_scrub):
        assert hamma_scrub._counter({"v": "42"}, "v") == 42.0

    def test_na_and_blank(self, hamma_scrub):
        assert hamma_scrub._counter({"v": "NA"}, "v") is None
        assert hamma_scrub._counter({"v": ""}, "v") is None
        assert hamma_scrub._counter({}, "v") is None

    @pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "1e400"])
    def test_non_finite_rejected(self, hamma_scrub, bad):
        """float() accepts these. A NaN latched into the reset scan makes every
        later `value < previous` False, disabling restart detection."""
        assert hamma_scrub._counter({"v": bad}, "v") is None


class TestCounterWindow:
    def test_delta_between_boundary_rows(self, hamma_scrub):
        rows = _rows(
            ("2026-09-04T23:59:18", 4324, 95.0, 4319, 5),
            ("2026-09-07T12:00:00", 20000, 400.0, 19990, 20),
            ("2026-09-10T23:59:18", 40516, 889.0, 40475, 41),
        )
        out = hamma_scrub.counter_window(
            rows, "2026-09-04T23:59:18", "2026-09-10T23:59:18")
        assert out["delta_valid_packets"] == 36192
        assert out["delta_packets_dropped"] == 36
        assert out["start_row"] == "2026-09-04T23:59:18"
        assert out["end_row"] == "2026-09-10T23:59:18"

    def test_reversed_bounds_rejected(self, hamma_scrub):
        """start/end are chosen INDEPENDENTLY, so a reversed pair otherwise
        yields delta=0, union=0, lost=0 -- a PASS that examined nothing."""
        rows = _rows(
            ("2026-09-09T04:00:00", 100, 2.0, 100, 0),
            ("2026-09-09T20:00:00", 100, 2.0, 100, 0),
        )
        with pytest.raises(RuntimeError, match="not before"):
            hamma_scrub.counter_window(
                rows, "2026-09-09T20:00:00", "2026-09-09T04:00:00")

    def test_equal_bounds_rejected(self, hamma_scrub):
        rows = _rows(("2026-09-09T04:00:00", 100, 2.0, 100, 0))
        with pytest.raises(RuntimeError, match="not before"):
            hamma_scrub.counter_window(
                rows, "2026-09-09T04:00:00", "2026-09-09T04:00:00")

    def test_both_bounds_snapping_to_one_row_rejected(self, hamma_scrub):
        """Otherwise delta=0 and union=0 certify a window nothing happened in."""
        rows = _rows(("2026-09-09T04:00:00", 100, 2.0, 100, 0))
        with pytest.raises(RuntimeError, match="same telemetry row"):
            hamma_scrub.counter_window(
                rows, "2026-09-09T05:00:00", "2026-09-09T06:00:00")

    def test_no_bracketing_rows_rejected(self, hamma_scrub):
        rows = _rows(("2026-09-20T00:00:00", 10, 1.0, 10, 0))
        with pytest.raises(RuntimeError, match="no telemetry rows bracket"):
            hamma_scrub.counter_window(
                rows, "2026-09-04T00:00:00", "2026-09-10T00:00:00")

    def test_bound_far_from_request_rejected(self, hamma_scrub):
        """A telemetry gap would silently measure a much wider window."""
        rows = _rows(
            ("2026-09-04T00:00:00", 100, 2.0, 100, 0),
            ("2026-09-08T00:00:00", 900, 20.0, 900, 0),
        )
        with pytest.raises(RuntimeError, match="from the requested"):
            hamma_scrub.counter_window(
                rows, "2026-09-06T00:00:00", "2026-09-08T00:00:00",
                bound_tolerance_s=120.0)

    def test_reset_inside_window_raises(self, hamma_scrub):
        rows = _rows(
            ("2026-09-04T00:00:00", 4324, 95.0, 4319, 5),
            ("2026-09-06T00:00:00", 12, 0.3, 12, 0),
            ("2026-09-10T00:00:00", 9000, 200.0, 8990, 3),
        )
        with pytest.raises(RuntimeError, match="reset"):
            hamma_scrub.counter_window(
                rows, "2026-09-04T00:00:00", "2026-09-10T00:00:00",
                bound_tolerance_s=1e9)

    def test_reset_at_first_in_window_row_detected(self, hamma_scrub):
        """Seeded from the START row -- a reset landing immediately after the
        start bound must not slip through."""
        rows = _rows(
            ("2026-09-04T00:00:00", 5000, 100.0, 5000, 0),
            ("2026-09-04T00:01:00", 3, 0.1, 3, 0),
            ("2026-09-04T00:02:00", 60, 1.0, 60, 0),
        )
        with pytest.raises(RuntimeError, match="reset"):
            hamma_scrub.counter_window(
                rows, "2026-09-04T00:00:00", "2026-09-04T00:02:00")

    def test_nan_cell_cannot_mask_a_reset(self, hamma_scrub):
        """A NaN latched as `previous` would make every later comparison False
        and silently disable reset detection for the rest of the window."""
        rows = _rows(
            ("2026-09-04T00:00:00", 3600, 79.0, 3600, 0),
            ("2026-09-04T00:30:00", "nan", 79.0, 3600, 0),
            ("2026-09-04T00:45:00", 58, 1.2, 58, 0),
            ("2026-09-04T01:00:00", 1740, 38.0, 1740, 0),
        )
        with pytest.raises(RuntimeError, match="reset"):
            hamma_scrub.counter_window(
                rows, "2026-09-04T00:00:00", "2026-09-04T01:00:00")

    def test_flat_counter_is_not_a_reset(self, hamma_scrub):
        rows = _rows(
            ("2026-09-04T00:00:00", 100, 2.0, 100, 0),
            ("2026-09-04T00:30:00", 100, 2.0, 100, 0),
            ("2026-09-04T01:00:00", 105, 2.2, 105, 0),
        )
        out = hamma_scrub.counter_window(
            rows, "2026-09-04T00:00:00", "2026-09-04T01:00:00")
        assert out["delta_valid_packets"] == 5

    def test_columns_read_by_name_not_position(self, hamma_scrub):
        rows = [
            ("2026-09-04T00:00:00",
             {"time": "2026-09-04T00:00:00", "packets_dropped": "5",
              "valid_packets": "100", "bytes_written": "2.0",
              "packets_sent": "95"}),
            ("2026-09-04T01:00:00",
             {"time": "2026-09-04T01:00:00", "packets_dropped": "9",
              "valid_packets": "300", "bytes_written": "6.0",
              "packets_sent": "291"}),
        ]
        out = hamma_scrub.counter_window(
            rows, "2026-09-04T00:00:00", "2026-09-04T01:00:00")
        assert out["delta_valid_packets"] == 200
        assert out["delta_packets_dropped"] == 4

    def test_bound_on_an_offset_suffixed_row_includes_that_row(
            self, hamma_scrub):
        """Bounds are INSTANTS, not strings.

        brokkr telemetry always carries +00:00; the CLI's own documented
        --window-start example does not. String-compared, the longer string
        loses at an equal prefix ('...01:00:00+00:00' <= '...01:00:00' is
        False), so a bound landing exactly on a row excluded that row and
        snapped a whole telemetry interval early -- inside the 120 s drift
        tolerance, so no guard fired and the audit measured a shifted window.
        """
        rows = _rows(
            ("2026-04-04T00:59:00+00:00", 10, 0.0, 0, 0),
            ("2026-04-04T01:00:00+00:00", 20, 0.0, 0, 0),
            ("2026-04-04T01:59:00+00:00", 30, 0.0, 0, 0),
            ("2026-04-04T02:00:00+00:00", 45, 0.0, 0, 0),
        )
        out = hamma_scrub.counter_window(rows, QUIET_T0, QUIET_T1)
        assert out["start_row"] == "2026-04-04T01:00:00+00:00"
        assert out["end_row"] == "2026-04-04T02:00:00+00:00"
        assert out["delta_valid_packets"] == 25

    def test_mixed_stamp_forms_order_by_instant(self, hamma_scrub):
        """A 'Z' row and a naive row are the same instant an hour apart; the
        lexical order of the two forms must not decide which is first."""
        rows = _rows(
            ("2026-04-04T01:00:00Z", 10, 0.0, 0, 0),
            ("2026-04-04T02:00:00", 40, 0.0, 0, 0),
        )
        out = hamma_scrub.counter_window(rows, QUIET_T0, QUIET_T1)
        assert out["delta_valid_packets"] == 30

    def test_unplaceable_row_inside_the_span_refuses(self, hamma_scrub):
        """A row we cannot order would also drop out of the reset scan, which
        is how a real loss becomes a PASS. Refuse -- but only inside the span
        the reset scan actually examines."""
        rows = _rows(
            (QUIET_T0, 0, 0.0, 0, 0),
            ("2026-04-04T01:3", 5, 0.0, 0, 0),     # torn mid-write row
            (QUIET_T1, 10, 0.0, 0, 0),
        )
        with pytest.raises(RuntimeError, match="unparseable"):
            hamma_scrub.counter_window(rows, QUIET_T0, QUIET_T1)

    @pytest.mark.parametrize("position", ["before", "after"])
    def test_unplaceable_row_outside_the_span_is_tolerated(self, hamma_scrub,
                                                           position):
        """load_telemetry_rows() reads +/-1 day of files, so a NUL-torn row
        from a day away is routine. The reset scan never looks at it, so it
        must not abort a run it could not have influenced."""
        torn = ("2026-04-04T01:3", 5, 0.0, 0, 0)
        span = [(QUIET_T0, 0, 0.0, 0, 0), (QUIET_T1, 10, 0.0, 0, 0)]
        specs = ([torn] + span) if position == "before" else (span + [torn])
        out = hamma_scrub.counter_window(_rows(*specs), QUIET_T0, QUIET_T1)
        assert out["delta_valid_packets"] == 10
        assert out["start_row"] == QUIET_T0
        assert out["end_row"] == QUIET_T1

    def test_reset_still_detected_with_a_torn_row_outside_the_span(
            self, hamma_scrub):
        """Narrowing the refusal must not narrow reset detection."""
        rows = _rows(
            ("2026-04-04T01:3", 5, 0.0, 0, 0),     # torn, outside the span
            (QUIET_T0, 100, 0.0, 0, 0),
            ("2026-04-04T01:30:00", 40, 0.0, 0, 0),   # AGS restarted
            (QUIET_T1, 90, 0.0, 0, 0),
        )
        with pytest.raises(RuntimeError, match="valid_packets reset"):
            hamma_scrub.counter_window(rows, QUIET_T0, QUIET_T1)

    def test_unparseable_bound_refuses(self, hamma_scrub):
        """Previously the drift guard just skipped an unparseable bound."""
        with pytest.raises(RuntimeError, match="ISO timestamps"):
            hamma_scrub.counter_window(
                _simple_rows(5), "not-a-time", QUIET_T1)


class TestHeaderTimeHelpers:
    def test_decode_header_times_splits_undecodable(self, hamma_scrub):
        good = _make_gps_header(tow=522857.0)
        pairs, bad = hamma_scrub.decode_header_times({good, bytes(128)})
        assert len(pairs) == 1 and bad == 1

    def test_headers_in_window_is_start_inclusive(self, hamma_scrub):
        h = _make_gps_header(tow=522857.0)
        pairs, _ = hamma_scrub.decode_header_times({h})
        epoch = pairs[0][0]
        assert hamma_scrub.headers_in_window(pairs, epoch, epoch + 1) == {h}

    def test_headers_in_window_is_end_exclusive(self, hamma_scrub):
        h = _make_gps_header(tow=522857.0)
        pairs, _ = hamma_scrub.decode_header_times({h})
        epoch = pairs[0][0]
        assert hamma_scrub.headers_in_window(pairs, epoch - 1, epoch) == set()

    def test_boundary_activity_counts_within_edge(self, hamma_scrub):
        pairs, _ = hamma_scrub.decode_header_times(
            {_make_gps_header(tow=522857.0 + i) for i in range(3)})
        base = min(e for e, _ in pairs)
        assert hamma_scrub.boundary_activity(pairs, base, 10.0) == 3
        assert hamma_scrub.boundary_activity(pairs, base + 1000, 10.0) == 0

    def test_boundary_activity_edge_is_inclusive(self, hamma_scrub):
        pairs, _ = hamma_scrub.decode_header_times(
            {_make_gps_header(tow=522857.0)})
        epoch = pairs[0][0]
        assert hamma_scrub.boundary_activity(pairs, epoch + 10.0, 10.0) == 1
        assert hamma_scrub.boundary_activity(pairs, epoch + 10.1, 10.0) == 0


class TestBuildLostReport:
    def _headers(self, n):
        return {_make_gps_header(tow=522847.0 + i) for i in range(n)}

    def test_zero_loss_certifies(self, hamma_scrub):
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(3)), _simple_rows(3),
            QUIET_T0, QUIET_T1)
        assert report["union"] == 3
        assert report["lost"] == 0
        assert report["blockers"] == []
        assert report["certified"] is True

    def test_detects_loss_and_refuses_to_certify(self, hamma_scrub):
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(3)), _simple_rows(5),
            QUIET_T0, QUIET_T1)
        assert report["lost"] == 2
        assert report["certified"] is False

    def test_negative_loss_is_not_certified(self, hamma_scrub):
        """More on disk than counted means the model is wrong, not that the
        unit is extra healthy."""
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(5)), _simple_rows(3),
            QUIET_T0, QUIET_T1)
        assert report["lost"] == -2
        assert report["certified"] is False

    def test_union_dedupes_by_value_not_identity(self, hamma_scrub):
        """Separately constructed but byte-equal headers are ONE record."""
        a = _make_gps_header(tow=522857.0)
        b = _make_gps_header(tow=522857.0)
        assert a is not b and a == b
        report = hamma_scrub.build_lost_report(
            _scan([a]), _scan([b]), _simple_rows(1), QUIET_T0, QUIET_T1)
        assert report["union"] == 1
        assert report["ags_only"] == 0
        assert report["lost"] == 0

    def test_ags_only_records_are_not_losses(self, hamma_scrub):
        mj = self._headers(2)
        extra = {_make_gps_header(tow=522900.0)}
        report = hamma_scrub.build_lost_report(
            _scan(extra), _scan(mj), _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["ags_only"] == 1
        assert report["union"] == 3
        assert report["lost"] == 0

    def test_duplicate_headers_block_certification(self, hamma_scrub):
        """Duplicates collapse in the union and OVERSTATE loss; the scanners
        already report this happens in the field with frozen GPS.

        49 copies of ONE header is the real shape of a GPS freeze: many
        distinct records collapsing onto a single set of GPS fields.
        """
        dup = _make_gps_header(tow=522847.0)       # in-window (01:13:50)
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(3), duplicate_headers=[dup] * 49),
            _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["duplicate_headers"] == 49
        assert report["certified"] is False
        assert any("duplicate" in b for b in report["blockers"])

    def test_out_of_window_duplicates_do_not_block(self, hamma_scrub):
        """The other half of the contract: a duplicate the window excludes.

        mj08's GPS-freeze duplicates sit in retained history. Counting the
        whole scan blocked every later audit on that unit permanently, even
        for a window deliberately chosen to avoid the episode -- and --since
        and --recover/--purge are all rejected alongside --audit-loss, so the
        operator had no way out.
        """
        stale = _make_gps_header(tow=522847.0 - 7200)   # ~23:13, day before
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(3), duplicate_headers=[stale] * 49),
            _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["duplicate_headers"] == 0
        assert report["blockers"] == []
        assert report["certified"] is True

    def test_ags_side_duplicates_are_window_scoped_too(self, hamma_scrub):
        """The blocker sums BOTH sides. Scoping only MJ would leave the AGS
        term unscoped, and nothing that avoids SSH would notice."""
        stale = _make_gps_header(tow=522847.0 - 7200)   # ~23:13, day before
        report = hamma_scrub.build_lost_report(
            _scan([], duplicate_headers=[stale] * 12),
            _scan(self._headers(3)), _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["duplicate_headers"] == 0
        assert report["certified"] is True

    def test_ags_side_in_window_duplicate_still_blocks(self, hamma_scrub):
        """...and the AGS term must still be able to block."""
        dup = _make_gps_header(tow=522847.0)
        report = hamma_scrub.build_lost_report(
            _scan([], duplicate_headers=[dup] * 12),
            _scan(self._headers(3)), _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["duplicate_headers"] == 12
        assert report["certified"] is False

    def test_both_sides_sum_independently(self, hamma_scrub):
        """A record on both AGS and MJ is not a duplicate; each scanner counts
        within its own side, so the terms add."""
        dup = _make_gps_header(tow=522847.0)
        report = hamma_scrub.build_lost_report(
            _scan([], duplicate_headers=[dup]),
            _scan(self._headers(3), duplicate_headers=[dup]),
            _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["duplicate_headers"] == 2

    def test_incremental_scan_refuses_rather_than_reading_zero(
            self, hamma_scrub):
        """The cached scanner cannot say WHICH header was duplicated. Reading
        that as "no duplicates" would silently disarm the blocker."""
        mj = _scan(self._headers(3))
        mj["duplicate_headers"] = None
        with pytest.raises(RuntimeError, match="does not retain"):
            hamma_scrub.build_lost_report(
                _scan([]), mj, _simple_rows(3), QUIET_T0, QUIET_T1)

    @pytest.mark.parametrize("side", ["ags", "mj"])
    def test_scan_without_duplicate_identity_refuses(self, hamma_scrub, side):
        """A stale producer -- one still on the pre-fix contract -- must get a
        RuntimeError refusal, not an unhandled KeyError traceback: run()'s
        audit branch catches only (RuntimeError, ValueError), so a bare
        subscript would escape the EXIT_NO_DATA path entirely."""
        scans = {"ags": _scan([]), "mj": _scan(self._headers(3))}
        del scans[side]["duplicate_headers"]
        with pytest.raises(RuntimeError, match="no 'duplicate_headers' key"):
            hamma_scrub.build_lost_report(
                scans["ags"], scans["mj"], _simple_rows(3),
                QUIET_T0, QUIET_T1)

    @pytest.mark.parametrize("side", ["ags", "mj"])
    def test_stale_producer_refusal_is_exit_no_data_not_a_traceback(
            self, hamma_scrub, tmp_path, side):
        """The same fault end-to-end through run(): a refusal exit code."""
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        csv_path = tmp_path / "telemetry_hamma_008_2026-04-04.csv"
        csv_path.write_text(
            "time,valid_packets,bytes_written,packets_sent,packets_dropped\n"
            "{},0,0.0,0,0\n{},3,0.0,0,0\n".format(QUIET_T0, QUIET_T1))

        def stale(headers_arg):
            scan = _scan(headers_arg)
            del scan["duplicate_headers"]
            return scan

        hamma_scrub.scan_ags_files = (
            lambda *a, **k: stale([]) if side == "ags" else _scan([]))
        hamma_scrub.scan_mj_files = (
            lambda *a, **k: stale(headers) if side == "mj"
            else _scan(headers))
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=str(tmp_path), status_file=None, metrics_file=None)
        assert rc == hamma_scrub.EXIT_NO_DATA

    def test_no_mj_files_blocks_certification(self, hamma_scrub):
        """An unreadable drive (e.g. DATA071, HAM-185) empties the union, so a
        delta would read as total loss."""
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan([], file_count=0), _simple_rows(0),
            QUIET_T0, QUIET_T1)
        assert report["certified"] is False
        assert any("no MJ .bin files" in b for b in report["blockers"])

    def test_busy_boundary_blocks_certification(self, hamma_scrub):
        """THE precondition: trigger->valid_packets latency straddling an edge
        means the counter and the header set disagree about membership."""
        headers = self._headers(3)
        pairs, _ = hamma_scrub.decode_header_times(headers)
        first = min(e for e, _ in pairs)
        t0 = datetime.fromtimestamp(first - 2, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S")
        rows = _rows((t0, 0, 0.0, 0, 0), (QUIET_T1, 3, 0.0, 0, 0))
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(headers), rows, t0, QUIET_T1,
            bound_tolerance_s=1e9)
        assert report["edge_records_start"] > 0
        assert report["certified"] is False
        assert any("not quiet" in b for b in report["blockers"])

    def test_extra_blockers_prevent_certification(self, hamma_scrub):
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(3)), _simple_rows(3),
            QUIET_T0, QUIET_T1, extra_blockers=["compressed .hmc present"])
        assert report["lost"] == 0
        assert report["certified"] is False

    def test_derives_local_drops_from_bytes_written(self, hamma_scrub):
        """Literal GB value, NOT re-derived from the constants -- deriving it
        makes the assertion cancel out and pass for any constant."""
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(self._headers(5)),
            _simple_rows(5, written_gb=0.066000396),
            QUIET_T0, QUIET_T1)
        assert report["local_drops_derived"] == 2

    def test_bad_gps_headers_do_not_create_false_loss(self, hamma_scrub):
        mj = self._headers(3) | {bytes(128)}
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan(mj), _simple_rows(3), QUIET_T0, QUIET_T1)
        assert report["undecodable_mj_total"] == 1
        assert report["lost"] == 0
        assert report["certified"] is True

    def test_header_bounds_snap_to_counter_rows(self, hamma_scrub):
        """Counters are measured between telemetry rows; bounding headers by
        anything else reintroduces a start/end mismatch."""
        rows = _rows(("2026-04-04T01:13:00", 10, 0.0, 10, 0),
                     ("2026-04-04T01:14:05", 12, 0.0, 12, 0))
        straddler = _make_gps_header(tow=522847.0)   # 01:13:50.808
        inside = _make_gps_header(tow=522857.0)      # 01:14:00.808
        report = hamma_scrub.build_lost_report(
            _scan([]), _scan({straddler, inside}), rows,
            "2026-04-04T01:13:52", "2026-04-04T01:14:05",
            edge_seconds=0.0)
        assert report["counter_row_start"] == "2026-04-04T01:13:00"
        assert report["mj_in_window"] == 2
        assert report["lost"] == 0


class TestFormatLostReport:
    """The verdict line is the whole output. A wrong one is the catastrophe."""

    def _report(self, **over):
        base = {
            "window_start": QUIET_T0, "window_end": QUIET_T1,
            "counter_row_start": QUIET_T0, "counter_row_end": QUIET_T1,
            "delta_valid_packets": 10, "ags_in_window": 0, "mj_in_window": 10,
            "ags_only": 0, "union": 10, "lost": 0,
            "edge_records_start": 0, "edge_records_end": 0, "edge_seconds": 10,
            "duplicate_headers": 0, "undecodable_ags_total": 0,
            "undecodable_mj_total": 0, "stream_drops": 0,
            "local_drops_derived": 0, "blockers": [], "certified": True,
        }
        base.update(over)
        return base

    def test_pass_only_when_certified(self, hamma_scrub):
        assert "PASS" in hamma_scrub.format_lost_report(self._report())

    def test_loss_never_prints_pass(self, hamma_scrub):
        out = hamma_scrub.format_lost_report(
            self._report(lost=500, union=10, certified=False))
        assert "PASS" not in out
        assert "FAIL" in out and "500" in out

    def test_negative_loss_reported_as_invalid(self, hamma_scrub):
        out = hamma_scrub.format_lost_report(
            self._report(lost=-19, certified=False))
        assert "INVALID" in out
        assert "PASS" not in out

    def test_blockers_are_printed_and_suppress_pass(self, hamma_scrub):
        out = hamma_scrub.format_lost_report(
            self._report(certified=False, blockers=["boundaries not quiet: x"]))
        assert "CANNOT CERTIFY" in out
        assert "PASS" not in out

    def test_none_counters_do_not_raise(self, hamma_scrub):
        out = hamma_scrub.format_lost_report(
            self._report(stream_drops=None, local_drops_derived=None,
                         delta_valid_packets=None, lost=None,
                         certified=False))
        assert "n/a" in out


class TestAuditLossCli:
    """Argument validation and the EXIT CODE -- the gate's actual contract."""

    @staticmethod
    def _patch_scans(hamma_scrub, mj_headers, valid_delta, tmp_path):
        """Stub both scanners so nothing touches ssh or real drives."""
        hamma_scrub.scan_ags_files = lambda *a, **k: _scan([])
        hamma_scrub.scan_mj_files = lambda *a, **k: _scan(mj_headers)
        csv_path = tmp_path / "telemetry_hamma_008_2026-04-04.csv"
        csv_path.write_text(
            "time,valid_packets,bytes_written,packets_sent,packets_dropped\n"
            "{},0,0.0,0,0\n{},{},0.0,0,0\n".format(
                QUIET_T0, QUIET_T1, valid_delta))
        return str(tmp_path)

    def _run(self, hamma_scrub, tmp_path, mj_headers, valid_delta):
        telem = self._patch_scans(
            hamma_scrub, mj_headers, valid_delta, tmp_path)
        return hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=telem, status_file=None, metrics_file=None)

    def test_exit_ok_when_nothing_lost(self, hamma_scrub, tmp_path, capsys):
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        rc = self._run(hamma_scrub, tmp_path, headers, 3)
        assert rc == hamma_scrub.EXIT_OK
        assert "PASS" in capsys.readouterr().out

    def test_exit_missing_when_records_lost(self, hamma_scrub, tmp_path,
                                            capsys):
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        rc = self._run(hamma_scrub, tmp_path, headers, 5)
        assert rc == hamma_scrub.EXIT_MISSING
        assert "FAIL" in capsys.readouterr().out

    def test_exit_missing_when_blocked_even_with_zero_loss(
            self, hamma_scrub, tmp_path, capsys):
        """A blocked audit must never read as a pass."""
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        telem = self._patch_scans(hamma_scrub, headers, 3, tmp_path)
        hamma_scrub.scan_mj_files = lambda *a, **k: _scan(
            headers, duplicate_headers=[_make_gps_header(tow=522847.0)] * 7)
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=telem, status_file=None, metrics_file=None)
        assert rc == hamma_scrub.EXIT_MISSING
        assert "CANNOT CERTIFY" in capsys.readouterr().out

    def test_audit_writes_no_shared_state(self, hamma_scrub, tmp_path):
        """write_status/write_scan_metrics feed state_monitor's hang detector;
        a hand-run audit must not stamp them.

        Asserted against REAL paths and the REAL writers. The previous version
        of this test mocked both, then filtered the status calls out of its own
        assertion -- so it passed while run() stamped three heartbeats -- and
        it passed status_file=None, which makes write_status() a no-op anyway
        (see its `if not path: return`). Either flaw alone made it incapable
        of failing. Files under shared/ so they cannot be mistaken for
        telemetry, which is globbed out of tmp_path itself.
        """
        shared = tmp_path / "shared"
        shared.mkdir()
        status = shared / "hamma_scrub_status.json"
        metrics = shared / "scan_metrics.csv"
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        telem = self._patch_scans(hamma_scrub, headers, 3, tmp_path)
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=telem, status_file=str(status),
            metrics_file=str(metrics))
        assert rc == hamma_scrub.EXIT_OK          # the audit really ran
        assert not status.exists()
        assert not metrics.exists()

    def test_audit_does_not_touch_the_shared_mj_scan_cache(self, hamma_scrub,
                                                           tmp_path):
        """The MJ cache is a world-writable tmpfs file the timer also owns,
        and its cached per-dir header sets cannot say which header was
        duplicated. The audit must not be handed one."""
        seen = []
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        telem = self._patch_scans(hamma_scrub, headers, 3, tmp_path)

        def fake_scan(*args, **kwargs):
            seen.append(kwargs.get("cache_file"))
            return _scan(headers)

        hamma_scrub.scan_mj_files = fake_scan
        hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=telem, status_file=None, metrics_file=None,
            mj_cache=str(tmp_path / "shared_cache.json"))
        assert seen == [None]

    def test_requires_window_bounds(self, hamma_scrub):
        assert hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            status_file=None, metrics_file=None) == hamma_scrub.EXIT_NO_DATA

    @pytest.mark.parametrize("kwargs", [
        {"recover": True}, {"recover": True, "purge": True}, {"since": "auto"},
    ])
    def test_rejects_mutating_and_truncating_combinations(self, hamma_scrub,
                                                          kwargs):
        assert hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            status_file=None, metrics_file=None,
            **kwargs) == hamma_scrub.EXIT_NO_DATA


class TestAuditScanRestriction:
    """The window-derived hourly-dir range is an I/O optimization and MUST NOT
    change the report.

    Over-inclusion is free -- headers_in_window() discards the surplus.
    Under-inclusion silently shrinks the union and manufactures a false FAIL
    for someone hunting data loss that never happened, so every test here is
    about the second direction.
    """

    def test_range_is_derived_from_window_plus_margin(self, hamma_scrub):
        assert hamma_scrub.window_dir_range(
            QUIET_T0, QUIET_T1, 10.0, 120.0) == ("2026-04-03T23",
                                                 "2026-04-04T03")

    def test_margin_tracks_the_named_guards(self, hamma_scrub):
        """Widening a guard must widen the range. A magic-number margin would
        leave the scan under-reading the span the guards now allow."""
        narrow = hamma_scrub.window_dir_range(QUIET_T0, QUIET_T1, 10.0, 120.0)
        wide = hamma_scrub.window_dir_range(QUIET_T0, QUIET_T1, 10.0, 7200.0)
        assert wide[0] < narrow[0]
        assert wide[1] > narrow[1]

    def test_skew_allowance_is_far_looser_than_the_measurement(self,
                                                               hamma_scrub):
        """Measured worst case on mj08 was 3 s; this must not be fitted to it,
        because the skew widens during the episodes that cause drops."""
        assert hamma_scrub.DIR_NAME_SKEW_SECONDS >= 600.0

    def test_unparseable_bound_does_not_restrict(self, hamma_scrub):
        """counter_window() owns bound validation; a full scan reaches the
        same refusal, just slower. Deriving a range here must not pre-empt
        it with a narrower or empty one."""
        assert hamma_scrub.window_dir_range(
            "nonsense", QUIET_T1) == (None, None)
        assert hamma_scrub.window_dir_range(
            QUIET_T0, "nonsense") == (None, None)

    @staticmethod
    def _tree(tmp_path, placements):
        for index, (hour, tow) in enumerate(placements):
            subdir = tmp_path / "DATA37" / hour
            subdir.mkdir(parents=True, exist_ok=True)
            header = _make_gps_header(tow=tow)
            (subdir / "r{}.bin".format(index)).write_bytes(
                header + b"\x00" * 64)
        return str(tmp_path)

    # GPS 01:59:40 filed under the 02 dir is the skew case the margin exists
    # for: in-window, but in a directory the literal bounds would not read.
    _SPREAD = [
        ("2026-04-03T23", 515647.0),   # first dir of range; outside window
        ("2026-04-04T01", 522847.0),   # 01:13:50  in window
        ("2026-04-04T01", 522848.0),   # 01:13:51  in window
        ("2026-04-04T01", 522849.0),   # 01:13:52  in window
        ("2026-04-04T02", 525597.0),   # 01:59:40  in window, FILED LATE
        ("2026-04-04T03", 530047.0),   # last dir of range; outside window
        ("2026-04-01T01", 263647.0),   # far outside the range entirely
    ]

    def _both_reports(self, hamma_scrub, base, t0, t1, rows):
        """The same report built from a restricted scan and from a full one."""
        since, until = hamma_scrub.window_dir_range(t0, t1, 10.0, 120.0)
        assert since is not None             # the restriction really applied
        restricted = hamma_scrub.scan_mj_files(base, since=since, until=until)
        full = hamma_scrub.scan_mj_files(base)
        assert restricted["file_count"] < full["file_count"]
        build = (lambda mj: hamma_scrub.build_lost_report(
            _scan([]), mj, rows, t0, t1))
        return build(restricted), build(full)

    def test_restricted_scan_yields_an_identical_report(self, hamma_scrub,
                                                        tmp_path):
        """The whole property, as a direct comparison of the two reports."""
        base = self._tree(tmp_path, self._SPREAD)
        restricted, full = self._both_reports(
            hamma_scrub, base, QUIET_T0, QUIET_T1, _simple_rows(4))
        assert restricted == full
        assert restricted["union"] == 4      # the late-filed record counted
        assert restricted["lost"] == 0
        assert restricted["certified"] is True

    def test_window_inside_a_single_directory(self, hamma_scrub, tmp_path):
        """A 10-minute window wholly inside one hourly dir still reads the
        neighbours, because a record of that window can be filed in them."""
        base = self._tree(tmp_path, self._SPREAD)
        t0, t1 = "2026-04-04T01:10:00", "2026-04-04T01:20:00"
        rows = _rows((t0, 0, 0.0, 0, 0), (t1, 3, 0.0, 0, 0))
        restricted, full = self._both_reports(hamma_scrub, base, t0, t1, rows)
        assert restricted == full
        assert restricted["union"] == 3      # only the 01:13:5x records
        assert restricted["lost"] == 0
        assert restricted["certified"] is True

    def test_undecodable_total_is_the_one_field_the_restriction_moves(
            self, hamma_scrub, tmp_path):
        """Documented exception to the equivalence property.

        undecodable_mj_total counts bad-GPS headers across whatever the scan
        covered -- format_lost_report() prints it as "whole scan, not
        window-scoped" -- so narrowing the scan narrows it. It is advisory
        only: it never enters the union, never blocks, and cannot move `lost`
        or `certified`. Asserted here so the change is recorded rather than
        discovered.
        """
        bad = [("2026-04-01T01", 0.0), ("2026-04-01T01", 0.0)]
        base = self._tree(tmp_path, self._SPREAD)
        for index, (hour, tow) in enumerate(bad):
            subdir = tmp_path / "DATA37" / hour
            subdir.mkdir(parents=True, exist_ok=True)
            header = _make_gps_header(week=0, tow=tow, subsecond=index)
            (subdir / "bad{}.bin".format(index)).write_bytes(
                header + b"\x00" * 64)

        restricted, full = self._both_reports(
            hamma_scrub, base, QUIET_T0, QUIET_T1, _simple_rows(4))
        assert restricted["undecodable_mj_total"] == 0
        assert full["undecodable_mj_total"] == 2
        # Everything that decides the verdict is still identical.
        for key in ("union", "lost", "certified", "blockers", "ags_in_window",
                    "mj_in_window", "duplicate_headers", "edge_records_start",
                    "edge_records_end", "delta_valid_packets"):
            assert restricted[key] == full[key]

    def test_late_filed_record_is_not_lost_by_the_restriction(self,
                                                              hamma_scrub,
                                                              tmp_path):
        """The under-inclusion failure, isolated: the ONLY in-window record is
        filed in the next hour's dir. A literal-bounds range would miss it and
        report it lost."""
        base = self._tree(tmp_path, [("2026-04-04T02", 525597.0)])
        since, until = hamma_scrub.window_dir_range(
            QUIET_T0, QUIET_T1, 10.0, 120.0)
        mj = hamma_scrub.scan_mj_files(base, since=since, until=until)
        report = hamma_scrub.build_lost_report(
            _scan([]), mj, _simple_rows(1), QUIET_T0, QUIET_T1)
        assert report["union"] == 1
        assert report["lost"] == 0
        assert report["certified"] is True

    def test_run_restricts_the_audit_scan(self, hamma_scrub, tmp_path):
        """End-to-end: run() must pass the derived range to the scanner."""
        seen = {}
        headers = {_make_gps_header(tow=522847.0 + i) for i in range(3)}
        csv_path = tmp_path / "telemetry_hamma_008_2026-04-04.csv"
        csv_path.write_text(
            "time,valid_packets,bytes_written,packets_sent,packets_dropped\n"
            "{},0,0.0,0,0\n{},3,0.0,0,0\n".format(QUIET_T0, QUIET_T1))
        hamma_scrub.scan_ags_files = lambda *a, **k: _scan([])

        def fake_scan(*args, **kwargs):
            seen.update(kwargs)
            return _scan(headers)

        hamma_scrub.scan_mj_files = fake_scan
        rc = hamma_scrub.run(
            "hamma", "/ags/data", "/media/pi", audit_loss=True,
            window_start=QUIET_T0, window_end=QUIET_T1,
            telemetry_dir=str(tmp_path), status_file=None, metrics_file=None)
        assert rc == hamma_scrub.EXIT_OK
        assert seen["since"] == "2026-04-03T23"
        assert seen["until"] == "2026-04-04T03"
        assert seen["cache_file"] is None    # finding 3 still holds

    def test_scheduled_scrub_is_unrestricted_by_default(self, hamma_scrub,
                                                        tmp_path):
        """The range is audit-only: a normal --recover run must not grow an
        upper bound and start skipping the newest dirs."""
        seen = {}
        hamma_scrub.scan_ags_files = lambda *a, **k: _scan([])

        def fake_scan(*args, **kwargs):
            seen.update(kwargs)
            return _scan([])

        hamma_scrub.scan_mj_files = fake_scan
        hamma_scrub.run("hamma", "/ags/data", str(tmp_path),
                        status_file=None, metrics_file=None)
        assert seen["until"] is None


class TestArgparseAbbreviations:
    """Adding flags must not break existing scripted callers."""

    def test_dash_l_still_resolves_to_limit(self, hamma_scrub):
        """--lost-report would have made '--l' ambiguous with --limit; the flag
        is named --audit-loss for exactly this reason."""
        assert hamma_scrub._build_parser().parse_args(["--l", "0"]).limit == 0

    def test_audit_flags_parse(self, hamma_scrub):
        args = hamma_scrub._build_parser().parse_args(
            ["--audit-loss", "--window-start", QUIET_T0,
             "--window-end", QUIET_T1, "--edge-seconds", "5"])
        assert args.audit_loss is True
        assert args.edge_seconds == 5.0
