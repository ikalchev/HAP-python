"""TLV8 values of the HomeKit Secure Video 17.99 camera services.

Follows the HAP TLV8 list encoding: each item of a list is a TLV of its own,
and items of the same type are separated by an empty TLV of type 0. Values
longer than 255 bytes are sent as 255-byte fragments of the same type; a TLV
continues the value before it only after such a full fragment.

:mod:`pyhap.tlv` maps each type to one value, so it cannot express repeated
items of a type or the separators these lists need; hence the small encoder
and decoder here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypeVar
from uuid import UUID

SEPARATOR = b"\x00\x00"

# Version characteristic of the Camera Capabilities and Motion Zones services.
SERVICE_VERSION = "17.99"
CAPABILITIES_VERSION = 1
ZONES_VERSION = 2

_E = TypeVar("_E", bound=IntEnum)


class SensorType(IntEnum):
    UNKNOWN = 0
    PRIMARY = 1
    GENERIC = 255


class SensorIntent(IntEnum):
    UNKNOWN = 0
    MAIN = 1
    PACKAGE = 2
    GENERIC = 255


class VideoQuality(IntEnum):
    HIGHEST = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


class ZoneMethod(IntEnum):
    NORMAL = 1
    INVERTED = 2


def encode_item(tag: int, value: bytes) -> bytes:
    """Encode one value, fragmented into TLVs of at most 255 bytes."""
    if not value:
        return bytes((tag, 0))
    return b"".join(
        bytes((tag, len(chunk))) + chunk
        for chunk in (value[i : i + 255] for i in range(0, len(value), 255))
    )


def encode_list(tag: int, values: Iterable[bytes]) -> bytes:
    """Encode the values as list items of one type."""
    return SEPARATOR.join(encode_item(tag, value) for value in values)


def decode_items(data: bytes) -> list[tuple[int, bytes]]:
    """Split TLV8 data into its items, joining fragments and dropping separators.

    Raises ``ValueError`` for truncated data.
    """
    items: list[tuple[int, bytearray]] = []
    # Type of the full 255-byte fragment right before, which a TLV continues.
    current: int | None = None
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError(f"Truncated TLV header at offset {offset}")
        tag, length = data[offset], data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            raise ValueError(f"Truncated TLV value at offset {offset}")
        value = data[offset + 2 : end]
        offset = end
        if tag == current:
            items[-1][1].extend(value)
        elif tag or length:
            items.append((tag, bytearray(value)))
        current = tag if length == 255 else None
    return [(tag, bytes(value)) for tag, value in items]


def _fields(data: bytes) -> dict[int, list[bytes]]:
    fields: dict[int, list[bytes]] = {}
    for tag, value in decode_items(data):
        fields.setdefault(tag, []).append(value)
    return fields


def _required(fields: dict[int, list[bytes]], tag: int) -> bytes:
    if tag not in fields:
        raise ValueError(f"Missing TLV type {tag}")
    return fields[tag][0]


def _uint(value: int, size: int) -> bytes:
    return value.to_bytes(size, "little")


def _to_int(data: bytes) -> int:
    if not 0 < len(data) <= 8:
        raise ValueError(f"Invalid integer length {len(data)}")
    return int.from_bytes(data, "little")


def _to_enum(enum_type: type[_E], data: bytes) -> _E | int:
    # Values a newer controller may send are kept as plain ints.
    value = _to_int(data)
    try:
        return enum_type(value)
    except ValueError:
        return value


@dataclass
class VideoStreamCapability:
    """A video encoding the camera can produce from a sensor, bit rates in kbps."""

    identifier: UUID
    quality: VideoQuality | int
    width: int
    height: int
    frame_rate: int
    average_bitrate: int
    peak_bitrate: int

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, self.identifier.bytes),
                encode_item(2, _uint(self.quality, 1)),
                encode_item(3, _uint(self.width, 2)),
                encode_item(4, _uint(self.height, 2)),
                encode_item(5, _uint(self.frame_rate, 1)),
                encode_item(6, _uint(self.average_bitrate, 4)),
                encode_item(7, _uint(self.peak_bitrate, 4)),
            )
        )


@dataclass
class SensorConfiguration:
    """An image sensor of the camera and its video encodings."""

    sensor_uuid: UUID
    width: int
    height: int
    sensor_type: SensorType | int = SensorType.PRIMARY
    intent: SensorIntent | int = SensorIntent.MAIN
    video_streams: list[VideoStreamCapability] = field(default_factory=list)

    def encode(self) -> bytes:
        dimensions = encode_item(1, _uint(self.width, 2)) + encode_item(
            2, _uint(self.height, 2)
        )
        return b"".join(
            (
                encode_item(1, dimensions),
                encode_item(2, self.sensor_uuid.bytes),
                encode_item(3, _uint(self.sensor_type, 1)),
                encode_item(4, _uint(self.intent, 1)),
                encode_list(5, (stream.encode() for stream in self.video_streams)),
            )
        )


@dataclass
class CameraCapabilities:
    """Value of the Camera Capabilities characteristic."""

    sensors: list[SensorConfiguration]
    version: int = CAPABILITIES_VERSION

    def encode(self) -> bytes:
        sensors = encode_list(1, (sensor.encode() for sensor in self.sensors))
        return encode_item(1, _uint(self.version, 1)) + encode_item(2, sensors)


def encode_contributing_sensors(sensor_uuids: Iterable[UUID]) -> bytes:
    """Encode the value of the Contributing Sensors characteristic."""
    return encode_list(
        1, (encode_item(1, sensor_uuid.bytes) for sensor_uuid in sensor_uuids)
    )


@dataclass
class ZonePolygon:
    """A polygon in sensor pixels, the origin is the top left corner.

    A controller may leave out the identifier, it is then None.
    """

    identifier: UUID | None
    vertices: list[tuple[int, int]]

    def encode(self) -> bytes:
        vertices = b"".join(_uint(x, 2) + _uint(y, 2) for x, y in self.vertices)
        identifier = b""
        if self.identifier is not None:
            identifier = encode_item(1, self.identifier.bytes)
        return identifier + encode_item(3, vertices)

    @classmethod
    def decode(cls, data: bytes) -> ZonePolygon:
        fields = _fields(data)
        raw = fields.get(3, [b""])[0]
        if len(raw) % 4:
            raise ValueError(f"Invalid vertices length {len(raw)}")
        vertices = [
            (_to_int(raw[i : i + 2]), _to_int(raw[i + 2 : i + 4]))
            for i in range(0, len(raw), 4)
        ]
        # UUID raises ValueError for anything but 16 bytes.
        identifier = UUID(bytes=fields[1][0]) if 1 in fields else None
        return cls(identifier, vertices)


@dataclass
class ZoneData:
    """Polygons and how they form the region of interest.

    A missing method is :attr:`ZoneMethod.NORMAL`.
    """

    method: ZoneMethod | int
    polygons: list[ZonePolygon] = field(default_factory=list)

    def encode(self) -> bytes:
        return encode_item(1, _uint(self.method, 1)) + encode_list(
            3, (polygon.encode() for polygon in self.polygons)
        )

    @classmethod
    def decode(cls, data: bytes) -> ZoneData:
        fields = _fields(data)
        method: ZoneMethod | int = ZoneMethod.NORMAL
        if 1 in fields:
            method = _to_enum(ZoneMethod, fields[1][0])
        return cls(
            method, [ZonePolygon.decode(polygon) for polygon in fields.get(3, [])]
        )


@dataclass
class CameraZones:
    """Value of the Camera Zones characteristic.

    Only version 2 zone data is decoded, ``zones`` is empty for others.
    """

    zones: list[ZoneData] = field(default_factory=list)
    version: int = ZONES_VERSION

    def encode(self) -> bytes:
        return encode_item(1, _uint(self.version, 1)) + encode_list(
            2, (zone.encode() for zone in self.zones)
        )

    @classmethod
    def decode(cls, data: bytes) -> CameraZones:
        fields = _fields(data)
        version = _to_int(_required(fields, 1))
        if version != ZONES_VERSION:
            return cls([], version)
        return cls([ZoneData.decode(zone) for zone in fields.get(2, [])], version)
