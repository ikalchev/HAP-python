"""Tests for pyhap.media.stun, with the RFC 5769 test vectors."""

from __future__ import annotations

import struct

import pytest

from pyhap.media import stun
from pyhap.media.stun import Attr, Class, Message, Method

PASSWORD = b"VOkJxbRl1RmTxUk/WvJxBt"
TXID = bytes.fromhex("b7e7a701bc34d686fa87dfae")

# RFC 5769 section 2.1, 2.2 and 2.3.
REQUEST = bytes.fromhex(
    "000100582112a442b7e7a701bc34d686fa87dfae"
    "802200105354554e207465737420636c69656e74"
    "002400046e0001ff"
    "80290008932ff9b151263b36"
    "000600096576746a3a68367659202020"
    "000800149aeaa70cbfd8cb56781ef2b5b2d3f249c1b571a2"
    "80280004e57a3bcf"
)
RESPONSE_V4 = bytes.fromhex(
    "0101003c2112a442b7e7a701bc34d686fa87dfae"
    "8022000b7465737420766563746f7220"
    "002000080001a147e112a643"
    "000800142b91f599fd9e90c38c7489f92af9ba53f06be7d7"
    "80280004c07d4c96"
)
RESPONSE_V6 = bytes.fromhex(
    "010100482112a442b7e7a701bc34d686fa87dfae"
    "8022000b7465737420766563746f7220"
    "002000140002a1470113a9faa5d3f179bc25f4b5bed2b9d9"
    "00080014a382954e4be67bf11784c97c8292c275bfe3ed41"
    "80280004c8fb0b4c"
)


def test_rfc5769_request() -> None:
    """Verify the sample request parses and authenticates."""
    message = stun.parse(REQUEST)
    assert message.method is Method.BINDING
    assert message.message_class is Class.REQUEST
    assert message.transaction_id == TXID
    assert message.username == "evtj:h6vY"
    assert message.priority == 0x6E0001FF
    assert message.tie_breaker(Attr.ICE_CONTROLLED) == 0x932FF9B151263B36
    assert message.tie_breaker(Attr.ICE_CONTROLLING) is None
    assert stun.check_integrity(message, PASSWORD)
    assert not stun.check_integrity(message, b"wrong")


@pytest.mark.parametrize(
    ("data", "address"),
    [
        (RESPONSE_V4, ("192.0.2.1", 32853)),
        (RESPONSE_V6, ("2001:db8:1234:5678:11:2233:4455:6677", 32853)),
    ],
    ids=["ipv4", "ipv6"],
)
def test_rfc5769_responses(data: bytes, address: tuple[str, int]) -> None:
    """Verify the sample responses parse, authenticate and map the address."""
    message = stun.parse(data)
    assert message.message_class is Class.SUCCESS
    assert message.xor_mapped_address == address
    assert stun.check_integrity(message, PASSWORD)


@pytest.mark.parametrize(
    "address", [("192.0.2.1", 32853), ("2001:db8:1234:5678:11:2233:4455:6677", 1)]
)
def test_encode_roundtrip(address: tuple[str, int]) -> None:
    """Verify an encoded message parses back and authenticates."""
    message = Message(
        Method.BINDING,
        Class.SUCCESS,
        attributes={Attr.XOR_MAPPED_ADDRESS: stun.xor_address(*address, TXID)},
        transaction_id=TXID,
    )
    parsed = stun.parse(stun.encode(message, PASSWORD))
    assert parsed.xor_mapped_address == address
    assert stun.check_integrity(parsed, PASSWORD)
    assert parsed.fingerprint_offset is not None


def test_encode_error_code() -> None:
    """Verify ERROR-CODE round-trips."""
    message = Message(
        Method.BINDING,
        Class.ERROR,
        attributes={Attr.ERROR_CODE: stun.error_value(487, "Role Conflict")},
    )
    assert stun.parse(stun.encode(message)).error_code == 487


def test_without_integrity() -> None:
    """Verify a message without MESSAGE-INTEGRITY never authenticates."""
    message = stun.parse(stun.encode(Message(Method.BINDING, Class.REQUEST)))
    assert message.integrity_offset is None
    assert not stun.check_integrity(message, PASSWORD)


def test_bad_fingerprint_is_rejected() -> None:
    """Verify a changed FINGERPRINT fails parsing."""
    with pytest.raises(stun.StunError, match="FINGERPRINT"):
        stun.parse(REQUEST[:-1] + bytes([REQUEST[-1] ^ 1]))


def test_changed_body_fails_integrity() -> None:
    """Verify a changed attribute fails MESSAGE-INTEGRITY (without FP)."""
    tampered = bytearray(REQUEST[:-8])
    tampered[2:4] = struct.pack("!H", len(tampered) - 20)
    tampered[-30] ^= 1  # inside USERNAME
    assert not stun.check_integrity(stun.parse(bytes(tampered)), PASSWORD)


def header(length: int, msg_type: int = 0x0001) -> bytes:
    """A STUN header with a length field."""
    return struct.pack("!HHI", msg_type, length, stun.MAGIC_COOKIE) + TXID


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"\x00\x01" + b"\x00" * 18, "Not STUN"),
        (header(4) + b"\x00\x06", "length"),
        (header(4) + b"\x00\x06\x00\x08", "Truncated attribute"),
        (header(2) + b"\x00\x06", "length"),
        (header(0, 0x0002), "method"),
        (header(8) + b"\x80\x28\x00\x02\x00\x00\x00\x00", "FINGERPRINT"),
        (header(8) + b"\x00\x08\x00\x04\x00\x00\x00\x00", "MESSAGE-INTEGRITY"),
        (header(1300) + bytes(1300), "too large"),
    ],
    ids=[
        "no-cookie",
        "unaligned",
        "truncated",
        "short-length",
        "unknown-method",
        "short-fingerprint",
        "short-integrity",
        "too-large",
    ],
)
def test_malformed(data: bytes, error: str) -> None:
    """Verify malformed messages raise StunError."""
    with pytest.raises(stun.StunError, match=error):
        stun.parse(data)


def test_attribute_after_integrity_is_ignored() -> None:
    """Verify attributes after MESSAGE-INTEGRITY are ignored (RFC 8489 14.5).

    A peer may add MESSAGE-INTEGRITY-SHA256 after it; the message still
    authenticates, and the ignored value does not count.
    """
    without_fp = REQUEST[:-8]
    sha256 = b"\x00\x1c\x00\x20" + bytes(32)
    extra = b"\x00\x06\x00\x04evil"
    data = bytearray(without_fp + sha256 + extra)
    data[2:4] = struct.pack("!H", len(data) - 20)
    message = stun.parse(bytes(data))
    assert stun.check_integrity(message, PASSWORD)
    assert message.attributes.get(stun.Attr.USERNAME) != b"evil"
    assert 0x001C not in message.attributes


def test_attribute_after_fingerprint_is_rejected() -> None:
    """Verify nothing may follow FINGERPRINT."""
    data = bytearray(REQUEST + b"\x00\x24\x00\x04\x00\x00\x00\x01")
    data[2:4] = struct.pack("!H", len(data) - 20)
    with pytest.raises(stun.StunError, match="after FINGERPRINT"):
        stun.parse(bytes(data))


def test_unknown_attributes_are_kept_raw() -> None:
    """Verify unknown comprehension-optional attributes do not fail parsing."""
    message = Message(Method.BINDING, Class.REQUEST, attributes={0x8055: b"abc"})
    assert stun.parse(stun.encode(message)).attributes[0x8055] == b"abc"


def test_is_stun() -> None:
    """Verify STUN is told apart from DTLS and RTP."""
    assert stun.is_stun(REQUEST)
    assert not stun.is_stun(b"\x16" + REQUEST[1:])
    assert not stun.is_stun(b"\x80" + REQUEST[1:])
    assert not stun.is_stun(REQUEST[:19])


def test_bad_xor_mapped_address_is_none() -> None:
    """Verify an invalid XOR-MAPPED-ADDRESS reads as None."""
    message = Message(
        Method.BINDING, Class.SUCCESS, attributes={Attr.XOR_MAPPED_ADDRESS: b"\x00"}
    )
    assert message.xor_mapped_address is None
    message.attributes[Attr.XOR_MAPPED_ADDRESS] = b"\x00\x03\x00\x00" + bytes(4)
    assert message.xor_mapped_address is None


def test_invalid_username_is_none() -> None:
    """Verify a USERNAME that is not UTF-8 reads as None."""
    message = Message(
        Method.BINDING, Class.REQUEST, attributes={Attr.USERNAME: b"\xff"}
    )
    assert message.username is None
