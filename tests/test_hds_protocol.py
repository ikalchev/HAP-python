"""Tests for the HDS payload encoder and decoder."""

from typing import Any

import pytest

from pyhap.hds_protocol import Int64, decode, encode

VALUES = [
    True,
    False,
    None,
    -1,
    0,
    38,
    39,
    -128,
    127,
    -32768,
    32767,
    -(2**31),
    2**31 - 1,
    2**40,
    Int64(5),
    1.5,
    "",
    "a" * 32,
    "a" * 33,
    "a" * 0x100,
    b"x" * 32,
    b"x" * 0xFF,
    b"x" * 0x10000,
    bytearray(b"ab"),
    [1, "two", b"3"],
    list(range(15)),
    {"a": 1, "b": [True, None]},
    {str(i): i for i in range(15)},
]


@pytest.mark.parametrize("value", VALUES)
def test_round_trip(value: Any) -> None:
    """Encoded values decode back to the same value."""
    result = decode(encode(value))
    if isinstance(value, bytearray):
        value = bytes(value)
    assert result == value


def test_encode_int_size() -> None:
    """Each integer uses the smallest encoding, Int64 always uses 8 bytes."""
    assert len(encode(-1)) == 1
    assert len(encode(38)) == 1
    assert len(encode(-128)) == 2
    assert len(encode(32767)) == 3
    assert len(encode(2**31 - 1)) == 5
    assert len(encode(2**40)) == 9
    assert len(encode(Int64(5))) == 9


def test_encode_unsupported_type() -> None:
    """An unsupported type raises TypeError."""
    with pytest.raises(TypeError):
        encode(object())
