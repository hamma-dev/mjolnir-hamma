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

        # SSH runs the deployed script (no stdin piping)
        assert run_call[0][0] == [
            "ssh", "10.10.10.1",
            "python3 /tmp/hamma_strider.py /ags/data; "
            "rm -f /tmp/hamma_strider.py",
        ]

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


class TestDetectSinceAuto:
    """Test --since auto date detection from AGS."""

    def test_returns_date_from_earliest_science_file(self, hamma_scrub):
        """Earliest non-1980 file header decoded to YYYY-MM-DDTHH."""
        # Build a header with known GPS time
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<I', header, 10, 100)  # datasize
        # GPS week 2000, timeOfWeek 259200.0 (3 days), utcOffset 18.0
        struct.pack_into('<f', header, 80, 259200.0)
        struct.pack_into('<h', header, 84, 2000)
        struct.pack_into('<f', header, 86, 18.0)
        struct.pack_into('<I', header, 94, 100)
        struct.pack_into('<I', header, 98, 1000000000)
        header = bytes(header)

        # Expected: decode this header's GPS time, truncate to YYYY-MM-DDTHH
        expected_ts = hamma_scrub.decode_gps_time(header)
        expected_cutoff = expected_ts[:13]  # "YYYY-MM-DDTHH"

        ls_output = "1980-01-06_00.00.00\n2026-03-15_14.30.00\n2026-03-16_10.00.00\n"
        dd_output = header

        with patch("subprocess.run") as mock_run:
            # First call: ssh ls
            ls_result = MagicMock()
            ls_result.returncode = 0
            ls_result.stdout = ls_output.encode()
            # Second call: ssh dd (read first 128 bytes)
            dd_result = MagicMock()
            dd_result.returncode = 0
            dd_result.stdout = dd_output
            mock_run.side_effect = [ls_result, dd_result]

            result = hamma_scrub.detect_since_auto("hamma", "/ags/data")
            assert result == expected_cutoff

    def test_skips_1980_files(self, hamma_scrub):
        """All 1980-* files are skipped; uses first non-1980 file."""
        header = bytearray(128)
        header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<I', header, 10, 100)
        struct.pack_into('<f', header, 80, 259200.0)
        struct.pack_into('<h', header, 84, 2000)
        struct.pack_into('<f', header, 86, 18.0)
        struct.pack_into('<I', header, 94, 100)
        struct.pack_into('<I', header, 98, 1000000000)
        header = bytes(header)

        ls_output = "1980-01-05_00.00.00\n1980-01-06_12.00.00\n2026-04-01_08.00.00\n"

        with patch("subprocess.run") as mock_run:
            ls_result = MagicMock()
            ls_result.returncode = 0
            ls_result.stdout = ls_output.encode()
            dd_result = MagicMock()
            dd_result.returncode = 0
            dd_result.stdout = header
            mock_run.side_effect = [ls_result, dd_result]

            result = hamma_scrub.detect_since_auto("hamma", "/ags/data")

        # dd called on the first non-1980 file
        dd_call = mock_run.call_args_list[1]
        assert "2026-04-01_08.00.00" in dd_call[0][0][-1]

    def test_all_1980_files_returns_none(self, hamma_scrub):
        """If only 1980-* files exist, return None (no science data)."""
        ls_output = "1980-01-05_00.00.00\n1980-01-06_12.00.00\n"

        with patch("subprocess.run") as mock_run:
            ls_result = MagicMock()
            ls_result.returncode = 0
            ls_result.stdout = ls_output.encode()
            mock_run.return_value = ls_result

            result = hamma_scrub.detect_since_auto("hamma", "/ags/data")
            assert result is None

    def test_empty_directory_returns_none(self, hamma_scrub):
        """Empty AGS data directory returns None."""
        with patch("subprocess.run") as mock_run:
            ls_result = MagicMock()
            ls_result.returncode = 0
            ls_result.stdout = b""
            mock_run.return_value = ls_result

            result = hamma_scrub.detect_since_auto("hamma", "/ags/data")
            assert result is None

    def test_ssh_failure_raises(self, hamma_scrub):
        """SSH failure raises RuntimeError."""
        with patch("subprocess.run") as mock_run:
            ls_result = MagicMock()
            ls_result.returncode = 1
            ls_result.stderr = b"Connection refused"
            mock_run.return_value = ls_result

            with pytest.raises(RuntimeError, match="Failed to list AGS"):
                hamma_scrub.detect_since_auto("hamma", "/ags/data")

    def test_bad_header_in_first_file_tries_next(self, hamma_scrub):
        """If first science file header can't be decoded, try next file."""
        bad_header = b'\x00' * 128  # No sync marker, decode_gps_time returns None
        good_header = bytearray(128)
        good_header[0:4] = b'\xf5\xff\x50\x5d'
        struct.pack_into('<I', good_header, 10, 100)
        struct.pack_into('<f', good_header, 80, 259200.0)
        struct.pack_into('<h', good_header, 84, 2000)
        struct.pack_into('<f', good_header, 86, 18.0)
        struct.pack_into('<I', good_header, 94, 100)
        struct.pack_into('<I', good_header, 98, 1000000000)
        good_header = bytes(good_header)

        ls_output = "2026-03-10_08.00.00\n2026-03-11_09.00.00\n"

        with patch("subprocess.run") as mock_run:
            ls_result = MagicMock()
            ls_result.returncode = 0
            ls_result.stdout = ls_output.encode()
            dd_bad = MagicMock()
            dd_bad.returncode = 0
            dd_bad.stdout = bad_header
            dd_good = MagicMock()
            dd_good.returncode = 0
            dd_good.stdout = good_header
            mock_run.side_effect = [ls_result, dd_bad, dd_good]

            result = hamma_scrub.detect_since_auto("hamma", "/ags/data")
            assert result is not None


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
        assert cmd[1] == "hamma"
        # shlex.quote wraps paths with spaces in single quotes
        assert "'/ags/data/ags file.bin'" in cmd[2]


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


class TestMain:
    """Test main() integration."""

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
