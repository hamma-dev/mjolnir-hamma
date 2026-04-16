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
        }]
        j = hamma_scrub.format_json_report(results, "hamma", recovery=recovery)
        parsed = json.loads(j)
        assert "recovery" in parsed
        assert len(parsed["recovery"]) == 1
        assert parsed["recovery"][0]["status"] == "recovered"

    def test_json_report_no_recovery(self, hamma_scrub):
        """No recovery parameter -> no recovery key in JSON."""
        results = self._make_results_with_recovery()
        j = hamma_scrub.format_json_report(results, "hamma")
        parsed = json.loads(j)
        assert "recovery" not in parsed


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
        assert rc == 1  # Still EXIT_MISSING even after recovery
        mock_recover.assert_called_once()
        mock_filter.assert_called_once()

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
