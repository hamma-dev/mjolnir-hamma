"""Tests for hamma_scrub module."""

import importlib.util
import io
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
