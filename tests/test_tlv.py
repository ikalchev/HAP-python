"""Tests for pyhap.tlv."""

import pytest

from pyhap import tlv


def test_tlv_round_trip() -> None:
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


def test_tlv_invalid_pairs() -> None:
    """Test we encode fails with an odd amount of args."""
    with pytest.raises(ValueError):
        tlv.encode(b"\x01", b"A", b"\02")


def test_tlv_decode_truncated_record() -> None:
    """Verify a truncated TLV record raises instead of mis-parsing."""
    with pytest.raises(ValueError):
        tlv.decode(b"\x01")


def test_tlv_decode_truncated_value() -> None:
    """Verify a TLV value shorter than its length byte raises."""
    with pytest.raises(ValueError):
        tlv.decode(b"\x01\x05AB")


def test_tlv_decode_full_value() -> None:
    """Verify a value exactly as long as its length byte decodes."""
    assert tlv.decode(b"\x01\x03ABC") == {b"\x01": b"ABC"}


def _fragments(value: bytes, sizes: list[int]) -> bytes:
    """Build the expected TLV fragments of ``value`` with type 1."""
    encoded, offset = b"", 0
    for size in sizes:
        encoded += b"\x01" + bytes([size]) + value[offset : offset + size]
        offset += size
    return encoded


@pytest.mark.parametrize(
    ("length", "fragment_sizes"),
    [
        (0, [0]),
        (1, [1]),
        (254, [254]),
        (255, [255]),
        (256, [255, 1]),
        (509, [255, 254]),
        (510, [255, 255]),
        (511, [255, 255, 1]),
        (765, [255, 255, 255]),
    ],
)
def test_encode_long_values(length: int, fragment_sizes: list[int]) -> None:
    """Test values are split in 255 byte fragments without empty trailing ones."""
    value = (bytes(range(251)) * 4)[:length]

    encoded = tlv.encode(b"\x01", value)

    assert encoded == _fragments(value, fragment_sizes)
    assert tlv.decode(encoded) == {b"\x01": value}
