"""Tests for hamma_scrub module."""

import importlib.util
import io
import os
import pathlib
import struct

import pytest

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
