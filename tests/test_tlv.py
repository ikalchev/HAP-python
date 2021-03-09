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
