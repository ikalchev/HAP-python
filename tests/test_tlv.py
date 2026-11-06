"""Tests for pyhap.tlv."""

import pytest

from pyhap import tlv


def test_tlv_round_trip():
    """Test tlv can round trip TLV8 data."""
    message = tlv.encode(
        b"\x01",
        b"A",
        b"\x01",
        b"B",
        b"\x02",
        b"C",
    )

    decoded = tlv.decode(message)
    assert decoded == {
        b"\x01": b"AB",
        b"\x02": b"C",
    }


def test_tlv_invalid_pairs():
    """Test we encode fails with an odd amount of args."""
    with pytest.raises(ValueError):
        tlv.encode(b"\x01", b"A", b"\02")


def test_tlv_decode_truncated_record():
    """Verify a truncated TLV record raises instead of mis-parsing."""
    with pytest.raises(ValueError):
        tlv.decode(b"\x01")


def test_tlv_decode_truncated_value():
    """Verify a TLV value shorter than its length byte raises."""
    with pytest.raises(ValueError):
        tlv.decode(b"\x01\x05AB")


def test_tlv_decode_full_value():
    """Verify a value exactly as long as its length byte decodes."""
    assert tlv.decode(b"\x01\x03ABC") == {b"\x01": b"ABC"}
