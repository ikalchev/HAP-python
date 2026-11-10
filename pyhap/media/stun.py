"""The STUN subset ICE connectivity checks use (RFC 8489, RFC 8445).

Binding requests, success and error responses with the attributes ICE
needs: USERNAME, MESSAGE-INTEGRITY (HMAC-SHA1 with a short-term password),
FINGERPRINT, XOR-MAPPED-ADDRESS, PRIORITY, USE-CANDIDATE, ICE-CONTROLLED,
ICE-CONTROLLING and ERROR-CODE. Attributes are kept as raw values; the
accessors decode the ones ICE reads. Parsing is defensive: a message has a
bounded size, every attribute must fit, padding is skipped whatever its
content, attributes after MESSAGE-INTEGRITY other than FINGERPRINT are
ignored, and nothing may follow FINGERPRINT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
import hashlib
import hmac
import ipaddress
import secrets
import struct
import zlib

MAGIC_COOKIE = 0x2112A442
HEADER_LENGTH = 20
MAX_MESSAGE = 1280
FINGERPRINT_XOR = 0x5354554E
INTEGRITY_LENGTH = 20


class Method(enum.IntEnum):
    """STUN methods."""

    BINDING = 0x001


class Class(enum.IntEnum):
    """STUN message classes, as their bits in the message type."""

    REQUEST = 0x0000
    INDICATION = 0x0010
    SUCCESS = 0x0100
    ERROR = 0x0110


class Attr(enum.IntEnum):
    """STUN attribute types ICE uses."""

    MAPPED_ADDRESS = 0x0001
    USERNAME = 0x0006
    MESSAGE_INTEGRITY = 0x0008
    ERROR_CODE = 0x0009
    UNKNOWN_ATTRIBUTES = 0x000A
    XOR_MAPPED_ADDRESS = 0x0020
    PRIORITY = 0x0024
    USE_CANDIDATE = 0x0025
    SOFTWARE = 0x8022
    FINGERPRINT = 0x8028
    ICE_CONTROLLED = 0x8029
    ICE_CONTROLLING = 0x802A


class StunError(ValueError):
    """A datagram is not a well-formed STUN message."""


def is_stun(data: bytes) -> bool:
    """Whether a datagram looks like STUN (RFC 7983 and the magic cookie)."""
    return (
        len(data) >= HEADER_LENGTH
        and data[0] < 4
        and int.from_bytes(data[4:8], "big") == MAGIC_COOKIE
    )


def transaction_id() -> bytes:
    """A new random 96-bit transaction ID."""
    return secrets.token_bytes(12)


@dataclass
class Message:
    """A STUN message; ``attributes`` maps types to raw values in order."""

    method: Method
    message_class: Class
    transaction_id: bytes = field(default_factory=transaction_id)
    attributes: dict[int, bytes] = field(default_factory=dict)
    # Set by parse(): the raw message and where its integrity and
    # fingerprint attributes start.
    raw: bytes = b""
    integrity_offset: int | None = None
    fingerprint_offset: int | None = None

    def __contains__(self, attr: int) -> bool:
        """Whether the message carries an attribute."""
        return attr in self.attributes

    @property
    def username(self) -> str | None:
        """The USERNAME, if present and valid UTF-8."""
        value = self.attributes.get(Attr.USERNAME)
        if value is None:
            return None
        try:
            return value.decode()
        except UnicodeDecodeError:
            return None

    @property
    def priority(self) -> int | None:
        """The PRIORITY, if present."""
        return _uint(self.attributes.get(Attr.PRIORITY), 4)

    def tie_breaker(self, attr: Attr) -> int | None:
        """The 64-bit tie-breaker of ICE-CONTROLLING or ICE-CONTROLLED."""
        return _uint(self.attributes.get(attr), 8)

    @property
    def error_code(self) -> int | None:
        """The ERROR-CODE number, e.g. 487, if present."""
        value = self.attributes.get(Attr.ERROR_CODE)
        if value is None or len(value) < 4:
            return None
        return (value[2] & 0x07) * 100 + value[3]

    @property
    def xor_mapped_address(self) -> tuple[str, int] | None:
        """The XOR-MAPPED-ADDRESS, if present and valid."""
        value = self.attributes.get(Attr.XOR_MAPPED_ADDRESS)
        if value is None:
            return None
        try:
            return unxor_address(value, self.transaction_id)
        except StunError:
            return None


def _uint(value: bytes | None, size: int) -> int | None:
    if value is None or len(value) != size:
        return None
    return int.from_bytes(value, "big")


def _pad(length: int) -> int:
    return (4 - length % 4) % 4


def xor_address(host: str, port: int, txid: bytes) -> bytes:
    """Encode an address as XOR-MAPPED-ADDRESS (RFC 8489 section 14.2)."""
    address = ipaddress.ip_address(host.split("%", 1)[0])
    xport = port ^ (MAGIC_COOKIE >> 16)
    cookie = struct.pack("!I", MAGIC_COOKIE)
    if address.version == 4:
        key = cookie
        family = 0x01
    else:
        key = cookie + txid
        family = 0x02
    xaddr = bytes(a ^ b for a, b in zip(address.packed, key, strict=True))
    return struct.pack("!BBH", 0, family, xport) + xaddr


def unxor_address(value: bytes, txid: bytes) -> tuple[str, int]:
    """Decode an XOR-MAPPED-ADDRESS value."""
    if len(value) < 4:
        raise StunError("Short address")
    _, family, xport = struct.unpack_from("!BBH", value)
    cookie = struct.pack("!I", MAGIC_COOKIE)
    if family == 0x01 and len(value) == 8:
        key = cookie
    elif family == 0x02 and len(value) == 20:
        key = cookie + txid
    else:
        raise StunError("Bad address family or length")
    packed = bytes(a ^ b for a, b in zip(value[4:], key, strict=True))
    return str(ipaddress.ip_address(packed)), int(xport) ^ (MAGIC_COOKIE >> 16)


def error_value(code: int, reason: str) -> bytes:
    """Encode an ERROR-CODE value."""
    return struct.pack("!HBB", 0, code // 100, code % 100) + reason.encode()


def encode(
    message: Message, integrity_key: bytes | None = None, fingerprint: bool = True
) -> bytes:
    """Serialize a message, adding MESSAGE-INTEGRITY and FINGERPRINT.

    ``integrity_key`` is the short-term password as bytes; without it the
    message has no MESSAGE-INTEGRITY. Attributes of those two types in
    ``message.attributes`` are ignored.
    """
    body = b""
    for attr, value in message.attributes.items():
        if attr in (Attr.MESSAGE_INTEGRITY, Attr.FINGERPRINT):
            continue
        body += struct.pack("!HH", attr, len(value)) + value + bytes(_pad(len(value)))
    msg_type = message.method | message.message_class
    tid = message.transaction_id
    if integrity_key is not None:
        length = len(body) + 4 + INTEGRITY_LENGTH
        header = struct.pack("!HHI", msg_type, length, MAGIC_COOKIE) + tid
        digest = hmac.new(integrity_key, header + body, hashlib.sha1).digest()
        body += struct.pack("!HH", Attr.MESSAGE_INTEGRITY, INTEGRITY_LENGTH) + digest
    if fingerprint:
        length = len(body) + 8
        header = struct.pack("!HHI", msg_type, length, MAGIC_COOKIE) + tid
        crc = zlib.crc32(header + body) ^ FINGERPRINT_XOR
        body += struct.pack("!HHI", Attr.FINGERPRINT, 4, crc)
    return struct.pack("!HHI", msg_type, len(body), MAGIC_COOKIE) + tid + body


def parse(data: bytes) -> Message:
    """Parse a STUN message; raises :class:`StunError` when it is malformed.

    Integrity is not checked here, see :func:`check_integrity`. A present
    FINGERPRINT is checked.
    """
    if not is_stun(data):
        raise StunError("Not STUN")
    if len(data) > MAX_MESSAGE:
        raise StunError("Message too large")
    msg_type, length = struct.unpack_from("!HH", data)
    if length % 4 or HEADER_LENGTH + length != len(data):
        raise StunError("Bad message length")
    try:
        method = Method(msg_type & 0x3EEF)
    except ValueError as err:
        raise StunError(f"Unsupported method {msg_type:#06x}") from err
    message = Message(
        method=method,
        message_class=Class(msg_type & 0x0110),
        transaction_id=data[8:20],
        raw=data,
    )
    offset = HEADER_LENGTH
    while offset < len(data):
        if message.fingerprint_offset is not None:
            raise StunError("Attribute after FINGERPRINT")
        if offset + 4 > len(data):
            raise StunError("Truncated attribute header")
        attr, size = struct.unpack_from("!HH", data, offset)
        start = offset + 4
        if start + size > len(data):
            raise StunError("Truncated attribute")
        value = data[start : start + size]
        if attr == Attr.FINGERPRINT:
            if size != 4:
                raise StunError("Bad FINGERPRINT")
            message.fingerprint_offset = offset
        elif message.integrity_offset is not None:
            # RFC 8489 14.5: attributes after MESSAGE-INTEGRITY are ignored,
            # e.g. a MESSAGE-INTEGRITY-SHA256; only FINGERPRINT counts.
            offset = start + size + _pad(size)
            continue
        elif attr == Attr.MESSAGE_INTEGRITY:
            if size != INTEGRITY_LENGTH:
                raise StunError("Bad MESSAGE-INTEGRITY")
            message.integrity_offset = offset
        # The first instance of an attribute counts (RFC 8489 section 14).
        message.attributes.setdefault(attr, value)
        offset = start + size + _pad(size)
    if offset != len(data):
        raise StunError("Trailing bytes")
    if message.fingerprint_offset is not None and not _fingerprint_ok(message):
        raise StunError("FINGERPRINT mismatch")
    return message


def _fingerprint_ok(message: Message) -> bool:
    offset = message.fingerprint_offset
    assert offset is not None  # noqa: S101 - checked by the caller
    expected = struct.unpack_from("!I", message.raw, offset + 4)[0]
    return zlib.crc32(message.raw[:offset]) ^ FINGERPRINT_XOR == expected


def check_integrity(message: Message, key: bytes) -> bool:
    """Whether a parsed message carries a MESSAGE-INTEGRITY made with ``key``."""
    offset = message.integrity_offset
    if offset is None:
        return False
    raw = message.raw
    # The length field covers the message up to the end of the attribute.
    length = offset + 4 + INTEGRITY_LENGTH - HEADER_LENGTH
    header = raw[:2] + struct.pack("!H", length) + raw[4:HEADER_LENGTH]
    digest = hmac.new(key, header + raw[HEADER_LENGTH:offset], hashlib.sha1).digest()
    return hmac.compare_digest(digest, raw[offset + 4 : offset + 4 + INTEGRITY_LENGTH])
