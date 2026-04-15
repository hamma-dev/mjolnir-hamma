"""Tests for hamma_scrub module."""

import importlib.util
import io
import json
import os
import pathlib
import struct

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

    def test_calls_ssh_with_strider(self, hamma_scrub):
        """scan_ags_files runs ssh with python3 via stdin piping."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b''
        mock_result.stderr = b''

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = hamma_scrub.scan_ags_files("10.10.10.1", "/ags/data")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["ssh", "10.10.10.1", "python3 - /ags/data"]
        assert mock_run.call_args[1]["input"] == hamma_scrub.STRIDER_SCRIPT.encode('utf-8')
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

    def test_ssh_failure_raises(self, hamma_scrub):
        """SSH failure raises RuntimeError."""
        mock_result = MagicMock()
        mock_result.returncode = 255
        mock_result.stdout = b''
        mock_result.stderr = b'Connection refused'

        with patch("subprocess.run", return_value=mock_result):
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
