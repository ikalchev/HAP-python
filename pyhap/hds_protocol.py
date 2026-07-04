"""HomeKit Data Stream message protocol (payload codec + message framing).

Inside each decrypted HDS frame HomeKit carries a binary-serialized header
dictionary and a message dictionary. The serialization is a compact,
self-describing format (a length byte / tag per value); this module implements
it in pure Python together with the request/response/event message layer that
rides on top of :mod:`pyhap.hds`.

The byte-level format is not published by Apple; the tag values below match the
de-facto reference behaviour so the encoding is wire-compatible with HomeKit
controllers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import struct
from typing import Any, Optional


class HDSStatus(IntEnum):
    """Header status of an HDS response message."""

    SUCCESS = 0
    OUT_OF_MEMORY = 1
    TIMEOUT = 2
    HEADER_ERROR = 3
    PAYLOAD_ERROR = 4
    MISSING_PROTOCOL = 5
    PROTOCOL_SPECIFIC_ERROR = 6


class Int64(int):
    """An integer that always serializes as a 64-bit HDS value.

    The message header ``id`` and ``status`` are wire-encoded as 64-bit integers
    regardless of magnitude; wrapping them keeps that exact-width encoding.
    """


# --- payload format tags ---

_TRUE = 0x01
_FALSE = 0x02
_TERMINATOR = 0x03
_NULL = 0x04
_UUID = 0x05
_DATE = 0x06
_INT_MINUS_ONE = 0x07
_INT_0 = 0x08  # 0x08..0x2E encode 0..38 inline
_INT_38 = 0x2E
_INT8 = 0x30
_INT16 = 0x31
_INT32 = 0x32
_INT64 = 0x33
_FLOAT32 = 0x35
_FLOAT64 = 0x36
_UTF8_0 = 0x40  # 0x40..0x60 encode utf8 of length 0..32 inline
_UTF8_32 = 0x60
_UTF8_LEN8 = 0x61
_UTF8_LEN16 = 0x62
_UTF8_LEN32 = 0x63
_UTF8_LEN64 = 0x64
_DATA_0 = 0x70  # 0x70..0x90 encode data of length 0..32 inline
_DATA_32 = 0x90
_DATA_LEN8 = 0x91
_DATA_LEN16 = 0x92
_DATA_LEN32 = 0x93
_DATA_LEN64 = 0x94
_ARRAY_0 = 0xD0  # 0xD0..0xDE encode 0..14 elements inline
_ARRAY_14 = 0xDE
_ARRAY_TERMINATED = 0xDF
_DICT_0 = 0xE0  # 0xE0..0xEE encode 0..14 pairs inline
_DICT_14 = 0xEE
_DICT_TERMINATED = 0xEF


def _encode_int(value: int) -> bytes:
    if isinstance(value, Int64):
        return bytes([_INT64]) + struct.pack("<q", value)
    if value == -1:
        return bytes([_INT_MINUS_ONE])
    if 0 <= value <= 38:
        return bytes([_INT_0 + value])
    if -128 <= value <= 127:
        return bytes([_INT8]) + struct.pack("<b", value)
    if -32768 <= value <= 32767:
        return bytes([_INT16]) + struct.pack("<h", value)
    if -(2**31) <= value <= 2**31 - 1:
        return bytes([_INT32]) + struct.pack("<i", value)
    return bytes([_INT64]) + struct.pack("<q", value)


def _encode_length_prefixed(
    value: bytes, base: int, len8, len16, len32, len64
) -> bytes:
    length = len(value)
    if length <= 32:
        return bytes([base + length]) + value
    if length <= 0xFF:
        return bytes([len8]) + struct.pack("<B", length) + value
    if length <= 0xFFFF:
        return bytes([len16]) + struct.pack("<H", length) + value
    if length <= 0xFFFFFFFF:
        return bytes([len32]) + struct.pack("<I", length) + value
    return bytes([len64]) + struct.pack("<Q", length) + value


def encode(value: Any) -> bytes:
    """Serialize a python value into the HDS payload format."""
    if value is True:
        return bytes([_TRUE])
    if value is False:
        return bytes([_FALSE])
    if value is None:
        return bytes([_NULL])
    if isinstance(value, int):
        return _encode_int(value)
    if isinstance(value, float):
        return bytes([_FLOAT64]) + struct.pack("<d", value)
    if isinstance(value, str):
        return _encode_length_prefixed(
            value.encode("utf-8"),
            _UTF8_0,
            _UTF8_LEN8,
            _UTF8_LEN16,
            _UTF8_LEN32,
            _UTF8_LEN64,
        )
    if isinstance(value, (bytes, bytearray)):
        return _encode_length_prefixed(
            bytes(value), _DATA_0, _DATA_LEN8, _DATA_LEN16, _DATA_LEN32, _DATA_LEN64
        )
    if isinstance(value, (list, tuple)):
        items = b"".join(encode(item) for item in value)
        if len(value) <= 14:
            return bytes([_ARRAY_0 + len(value)]) + items
        return bytes([_ARRAY_TERMINATED]) + items + bytes([_TERMINATOR])
    if isinstance(value, dict):
        pairs = b"".join(encode(k) + encode(v) for k, v in value.items())
        if len(value) <= 14:
            return bytes([_DICT_0 + len(value)]) + pairs
        return bytes([_DICT_TERMINATED]) + pairs + bytes([_TERMINATOR])
    raise TypeError(f"Cannot encode {type(value).__name__} into an HDS payload")


# Real HDS payloads nest only a handful of levels; bound recursion well above
# that so a hostile peer cannot exhaust the Python stack.
_MAX_NESTING_DEPTH = 32


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def _take(self, length: int) -> bytes:
        chunk = self._data[self._offset : self._offset + length]
        if len(chunk) != length:
            raise ValueError("Truncated HDS payload")
        self._offset += length
        return chunk

    def _read_length(self, size: int) -> int:
        return int.from_bytes(self._take(size), "little")

    def decode(self, depth: int = 0) -> Any:
        if depth > _MAX_NESTING_DEPTH:
            # A hostile peer must not be able to blow the Python stack with
            # deeply nested containers (RecursionError).
            raise ValueError("HDS payload nesting too deep")
        tag = self._take(1)[0]
        if tag == _TRUE:
            return True
        if tag == _FALSE:
            return False
        if tag == _NULL:
            return None
        if tag == _INT_MINUS_ONE:
            return -1
        if _INT_0 <= tag <= _INT_38:
            return tag - _INT_0
        if tag == _INT8:
            return struct.unpack("<b", self._take(1))[0]
        if tag == _INT16:
            return struct.unpack("<h", self._take(2))[0]
        if tag == _INT32:
            return struct.unpack("<i", self._take(4))[0]
        if tag == _INT64:
            return struct.unpack("<q", self._take(8))[0]
        if tag == _FLOAT32:
            return struct.unpack("<f", self._take(4))[0]
        if tag == _FLOAT64:
            return struct.unpack("<d", self._take(8))[0]
        if _UTF8_0 <= tag <= _UTF8_32:
            return self._take(tag - _UTF8_0).decode("utf-8")
        if tag in (_UTF8_LEN8, _UTF8_LEN16, _UTF8_LEN32, _UTF8_LEN64):
            size = {_UTF8_LEN8: 1, _UTF8_LEN16: 2, _UTF8_LEN32: 4, _UTF8_LEN64: 8}[tag]
            return self._take(self._read_length(size)).decode("utf-8")
        if _DATA_0 <= tag <= _DATA_32:
            return self._take(tag - _DATA_0)
        if tag in (_DATA_LEN8, _DATA_LEN16, _DATA_LEN32, _DATA_LEN64):
            size = {_DATA_LEN8: 1, _DATA_LEN16: 2, _DATA_LEN32: 4, _DATA_LEN64: 8}[tag]
            return self._take(self._read_length(size))
        if _ARRAY_0 <= tag <= _ARRAY_14:
            return [self.decode(depth + 1) for _ in range(tag - _ARRAY_0)]
        if tag == _ARRAY_TERMINATED:
            result = []
            while self._data[self._offset] != _TERMINATOR:
                result.append(self.decode(depth + 1))
            self._offset += 1
            return result
        if _DICT_0 <= tag <= _DICT_14:
            return {
                self.decode(depth + 1): self.decode(depth + 1)
                for _ in range(tag - _DICT_0)
            }
        if tag == _DICT_TERMINATED:
            result = {}
            while self._data[self._offset] != _TERMINATOR:
                key = self.decode(depth + 1)
                result[key] = self.decode(depth + 1)
            self._offset += 1
            return result
        raise ValueError(f"Unknown HDS payload tag 0x{tag:02x}")

    @property
    def consumed(self) -> int:
        return self._offset


def decode(data: bytes) -> Any:
    """Deserialize a single value from an HDS payload buffer."""
    return _Decoder(data).decode()


# --- message layer ---

REQUEST = "request"
RESPONSE = "response"
EVENT = "event"


@dataclass
class Message:
    """A decoded HDS protocol message (header + message dictionaries)."""

    protocol: str
    kind: str  # REQUEST / RESPONSE / EVENT
    topic: str
    message: dict = field(default_factory=dict)
    id: Optional[int] = None
    status: Optional[int] = None

    def encode(self) -> bytes:
        """Return the plaintext HDS frame payload for this message.

        The frame payload is a header-length byte, the serialized header
        dictionary and the serialized message dictionary.
        """
        header: dict = {"protocol": self.protocol, self.kind: self.topic}
        if self.id is not None:
            header["id"] = Int64(self.id)
        if self.status is not None:
            header["status"] = Int64(self.status)
        header_bytes = encode(header)
        if len(header_bytes) > 0xFF:
            raise ValueError("HDS message header too large")
        return bytes([len(header_bytes)]) + header_bytes + encode(self.message)

    @classmethod
    def decode(cls, payload: bytes) -> "Message":
        header_length = payload[0]
        header = decode(payload[1 : 1 + header_length])
        message = decode(payload[1 + header_length :])
        for kind in (REQUEST, RESPONSE, EVENT):
            if kind in header:
                topic = header[kind]
                break
        else:
            raise ValueError("HDS message header has no request/response/event")
        return cls(
            protocol=header["protocol"],
            kind=kind,
            topic=topic,
            message=message,
            id=header.get("id"),
            status=header.get("status"),
        )


def request(protocol: str, topic: str, message: dict, request_id: int) -> Message:
    """Build a request message."""
    return Message(protocol, REQUEST, topic, message, id=request_id)


def response(
    protocol: str, topic: str, message: dict, request_id: int, status: int = 0
) -> Message:
    """Build a response message."""
    return Message(protocol, RESPONSE, topic, message, id=request_id, status=status)


def event(protocol: str, topic: str, message: dict) -> Message:
    """Build an event message."""
    return Message(protocol, EVENT, topic, message)
