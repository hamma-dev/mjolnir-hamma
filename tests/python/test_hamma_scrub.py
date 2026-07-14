"""Tests for hamma_scrub module."""

import importlib.util
import io
import json
import os
import pathlib
import struct
import subprocess
import time

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
        assert run_argv[-1] == (
            "python3 /tmp/hamma_strider.py /ags/data; "
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
        assert "dd" in cmd[-1]
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
        assert cmd[-1].startswith("rm -f ")


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
        """run() opens a real SSH ControlMaster + writes a status file to the
        real home; stub both out in unit tests."""
        with patch.object(hamma_scrub, "open_control_master",
                          return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "write_status"):
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
        """run() opens a real SSH ControlMaster + writes a status file to the
        real home; stub both out in unit tests."""
        with patch.object(hamma_scrub, "open_control_master",
                          return_value=None), \
             patch.object(hamma_scrub, "close_control_master"), \
             patch.object(hamma_scrub, "write_status"):
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
        assert len(purge_beats) == 3
        assert all(c.args[0] == "/tmp/s.json" for c in purge_beats)

    def test_chunk_command_deletes_multiple_files(self, hamma_scrub):
        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            hamma_scrub.purge_ags_files(
                "hamma", "/ags/data", ["a.bin", "b.bin"], dry_run=False)
        remote = mock_run.call_args[0][0][-1]
        assert "/ags/data/a.bin" in remote
        assert "/ags/data/b.bin" in remote

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
