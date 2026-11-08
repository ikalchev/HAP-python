"""TLV8 values of the HomeKit Secure Video 17.99 camera services.

Developer Preview: HomeKit Secure Video 17.99 is a preview of the guide, so
this API is experimental and may change.

Follows the HAP TLV8 list encoding: each item of a list is a TLV of its own,
and items of the same type are separated by an empty TLV of type 0. Values
longer than 255 bytes are sent as 255-byte fragments of the same type; a TLV
continues the value before it only after such a full fragment.

:mod:`pyhap.tlv` maps each type to one value, so it cannot express repeated
items of a type or the separators these lists need; hence the small encoder
and decoder here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
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
_T = TypeVar("_T")


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


class VideoCodec(IntEnum):
    H264 = 1
    H265 = 2


class AudioCodec(IntEnum):
    OPUS = 3


class AudioSampleRate(IntEnum):
    KHZ_16 = 1
    KHZ_24 = 2
    KHZ_32 = 3
    KHZ_48 = 4


class AudioBitDepth(IntEnum):
    BITS_8 = 1
    BITS_16 = 2
    BITS_24 = 3


class RTPStreamingCommand(IntEnum):
    END = 1
    START = 2


class RTPStreamingStatus(IntEnum):
    SUCCESS = 0
    UNKNOWN_SESSION = 1
    NO_SUCH_STREAM = 2
    BUSY = 3
    ERROR = 4


# The payload types of the stream tier characteristics. WebRTC media and its
# SDP use the same ones.
VIDEO_PAYLOAD_TYPE = 99
AUDIO_PAYLOAD_TYPE = 110


@dataclass(frozen=True)
class VideoStreamTier:
    """A video tier a controller can pick for a stream, bit rates in kbps.

    ``peak_bitrate`` is not part of the TLV. :func:`tiers_from_sensor` takes
    it from the sensor's video encoding, e.g. for the ``max-br`` of a WebRTC
    offer.
    """

    identifier: int
    quality: VideoQuality | int
    target_bitrate: int
    width: int
    height: int
    frame_rate: int
    peak_bitrate: int | None = None

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, _uint(self.identifier, 4)),
                encode_item(2, _uint(self.quality, 1)),
                encode_item(3, _uint(self.target_bitrate, 4)),
                encode_item(4, _uint(self.width, 2)),
                encode_item(5, _uint(self.height, 2)),
                encode_item(6, _uint(self.frame_rate, 1)),
            )
        )


@dataclass(frozen=True)
class AudioStreamTier:
    """An audio tier a controller can pick for a stream, bit rate in bps.

    With Opus the guide has the sample rate of the RTP stream, always 48 kHz,
    whatever the capture rate.
    """

    identifier: int
    target_bitrate: int
    sample_rate: AudioSampleRate | int
    bit_depth: AudioBitDepth | int = AudioBitDepth.BITS_16
    packet_time: int = 20
    channels: int = 1

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, _uint(self.identifier, 4)),
                encode_item(2, _uint(self.target_bitrate, 4)),
                encode_item(3, _uint(self.sample_rate, 1)),
                encode_item(4, _uint(self.bit_depth, 1)),
                encode_item(5, _uint(self.packet_time, 1)),
                encode_item(6, _uint(self.channels, 1)),
            )
        )


@dataclass
class SupportedVideoStreamTiers:
    """Value of the Supported Video Stream Tiers characteristic."""

    codec: VideoCodec | int
    payload_type: int
    tiers: list[VideoStreamTier]

    def encode(self) -> bytes:
        return (
            encode_item(1, _uint(self.codec, 1))
            + encode_item(2, _uint(self.payload_type, 1))
            + encode_list(3, (tier.encode() for tier in self.tiers))
        )


@dataclass
class SupportedAudioStreamTiers:
    """Value of the Supported Audio Stream Tiers characteristic."""

    codec: AudioCodec | int
    payload_type: int
    tiers: list[AudioStreamTier]

    def encode(self) -> bytes:
        return (
            encode_item(1, _uint(self.codec, 1))
            + encode_item(2, _uint(self.payload_type, 1))
            + encode_list(3, (tier.encode() for tier in self.tiers))
        )


def _optional_int(fields: dict[int, list[bytes]], tag: int) -> int | None:
    return _to_int(fields[tag][0]) if tag in fields else None


def _optional(
    fields: dict[int, list[bytes]], tag: int, convert: Callable[[bytes], _T]
) -> _T | None:
    return convert(fields[tag][0]) if tag in fields else None


def _optional_item(tag: int, value: bytes | None) -> bytes:
    return b"" if value is None else encode_item(tag, value)


@dataclass
class RTPStreamingControl:
    """A write to the RTP Streaming Control characteristic.

    Tiers and SSRCs come only with :attr:`RTPStreamingCommand.START`, the
    audio ones only if the controller wants audio.
    """

    session_id: UUID
    command: RTPStreamingCommand | int
    video_tier: int | None = None
    video_ssrc: int | None = None
    audio_tier: int | None = None
    audio_ssrc: int | None = None

    @classmethod
    def decode(cls, data: bytes) -> RTPStreamingControl:
        fields = _fields(data)
        return cls(
            _to_uuid(_required(fields, 1)),
            _to_enum(RTPStreamingCommand, _required(fields, 2)),
            _optional_int(fields, 3),
            _optional_int(fields, 4),
            _optional_int(fields, 5),
            _optional_int(fields, 6),
        )


def encode_streaming_status(
    session_id: UUID | None, status: RTPStreamingStatus | int
) -> bytes:
    """Encode the read back value of RTP Streaming Control.

    Without a session, e.g. for a write that did not decode, only the status
    is sent.
    """
    session = b"" if session_id is None else encode_item(1, session_id.bytes)
    return session + encode_item(2, _uint(status, 1))


class SetupEndpointsStatus(IntEnum):
    SUCCESS = 0
    BUSY = 1
    ERROR = 2


class SRTPCryptoSuite(IntEnum):
    AES_CM_128_HMAC_SHA1_80 = 0
    AES_256_CM_HMAC_SHA1_80 = 1
    DISABLED = 2


def _port(data: bytes) -> int:
    if len(data) != 2:
        raise ValueError(f"Invalid port length {len(data)}")
    return _to_int(data)


@dataclass(frozen=True)
class EndpointAddress:
    """An address of Setup Endpoints and its RTP ports.

    ``address`` is the text the controller sent. Check it with
    :func:`pyhap.camera.parse_setup_address` before using it.
    """

    address: str
    is_ipv6: bool
    video_port: int
    audio_port: int

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, _uint(self.is_ipv6, 1)),
                encode_item(2, self.address.encode("utf-8")),
                encode_item(3, _uint(self.video_port, 2)),
                encode_item(4, _uint(self.audio_port, 2)),
            )
        )

    @classmethod
    def decode(cls, data: bytes) -> EndpointAddress:
        fields = _fields(data)
        return cls(
            _required(fields, 2).decode("utf-8"),
            bool(_to_int(_required(fields, 1))),
            _port(_required(fields, 3)),
            _port(_required(fields, 4)),
        )


@dataclass(frozen=True)
class SRTPParameters:
    """The SRTP crypto suite, master key and master salt of a stream."""

    crypto_suite: SRTPCryptoSuite | int
    master_key: bytes = field(repr=False)
    master_salt: bytes = field(repr=False)

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, _uint(self.crypto_suite, 1)),
                encode_item(2, self.master_key),
                encode_item(3, self.master_salt),
            )
        )

    @classmethod
    def decode(cls, data: bytes) -> SRTPParameters:
        fields = _fields(data)
        return cls(
            _to_enum(SRTPCryptoSuite, _required(fields, 1)),
            _required(fields, 2),
            _required(fields, 3),
        )


@dataclass(frozen=True)
class SetupEndpoints:
    """A write to Setup Endpoints: where the controller receives, and its keys."""

    session_id: UUID
    address: EndpointAddress
    video_srtp: SRTPParameters
    audio_srtp: SRTPParameters

    def encode(self) -> bytes:
        return b"".join(
            (
                encode_item(1, self.session_id.bytes),
                encode_item(3, self.address.encode()),
                encode_item(4, self.video_srtp.encode()),
                encode_item(5, self.audio_srtp.encode()),
            )
        )

    @classmethod
    def decode(cls, data: bytes) -> SetupEndpoints:
        fields = _fields(data)
        return cls(
            _to_uuid(_required(fields, 1)),
            EndpointAddress.decode(_required(fields, 3)),
            SRTPParameters.decode(_required(fields, 4)),
            SRTPParameters.decode(_required(fields, 5)),
        )


@dataclass(frozen=True)
class SetupEndpointsResponse:
    """The read back value of Setup Endpoints.

    Without a session, e.g. for a write that did not decode, only the status
    is sent. Only a successful one has the accessory's address, keys and
    SSRCs.
    """

    session_id: UUID | None
    status: SetupEndpointsStatus | int
    address: EndpointAddress | None = None
    video_srtp: SRTPParameters | None = None
    audio_srtp: SRTPParameters | None = None
    video_ssrc: int | None = None
    audio_ssrc: int | None = None

    def encode(self) -> bytes:
        def encoded(value: EndpointAddress | SRTPParameters | None) -> bytes | None:
            return None if value is None else value.encode()

        def ssrc(value: int | None) -> bytes | None:
            return None if value is None else _uint(value, 4)

        session_id = None if self.session_id is None else self.session_id.bytes
        return b"".join(
            (
                _optional_item(1, session_id),
                encode_item(2, _uint(self.status, 1)),
                _optional_item(3, encoded(self.address)),
                _optional_item(4, encoded(self.video_srtp)),
                _optional_item(5, encoded(self.audio_srtp)),
                _optional_item(6, ssrc(self.video_ssrc)),
                _optional_item(7, ssrc(self.audio_ssrc)),
            )
        )

    @classmethod
    def decode(cls, data: bytes) -> SetupEndpointsResponse:
        fields = _fields(data)
        return cls(
            _optional(fields, 1, _to_uuid),
            _to_enum(SetupEndpointsStatus, _required(fields, 2)),
            _optional(fields, 3, EndpointAddress.decode),
            _optional(fields, 4, SRTPParameters.decode),
            _optional(fields, 5, SRTPParameters.decode),
            _optional_int(fields, 6),
            _optional_int(fields, 7),
        )


def tiers_from_sensor(sensor: SensorConfiguration) -> list[VideoStreamTier]:
    """Offer each video encoding of the sensor as a tier, numbered from 1.

    Tier ``n`` is ``sensor.video_streams[n - 1]``.
    """
    return [
        VideoStreamTier(
            index,
            stream.quality,
            stream.average_bitrate,
            stream.width,
            stream.height,
            stream.frame_rate,
            stream.peak_bitrate,
        )
        for index, stream in enumerate(sensor.video_streams, 1)
    ]


DEFAULT_AUDIO_TIER = AudioStreamTier(1, 24000, AudioSampleRate.KHZ_48)

# Average and maximum kbps of the guide's bit rate table, by the short side
# of the frame; the aspect ratio does not matter. A frame between two rows
# takes the larger one.
_GUIDE_BITRATES = {
    2160: (4500, 5000),
    1440: (2800, 3000),
    1080: (1700, 1800),
    720: (768, 800),
    360: (180, 190),
    240: (180, 190),
}


def check_audio_tier(tier: AudioStreamTier) -> None:
    """Check an audio tier against the guide.

    :raises ValueError: For anything this revision of the guide does not
        allow: a packet time other than 20 ms, more than one channel or an
        unknown sample rate or bit depth.
    """
    if tier.packet_time != 20 or tier.channels != 1:
        raise ValueError("Audio tiers have a 20 ms packet time and one channel")
    if tier.sample_rate not in set(AudioSampleRate) or tier.bit_depth not in set(
        AudioBitDepth
    ):
        raise ValueError("Unknown audio sample rate or bit depth")
    if tier.target_bitrate <= 0:
        raise ValueError("The audio bit rate must be positive")


def check_video_tiers(sensor: SensorConfiguration) -> list[str]:
    """Check the video encodings of a sensor as tiers against the guide.

    Returns warnings for what the guide only recommends: a gap between peak
    and average of more than 10 % of the peak, peaks above its table and
    frames larger than 4K, which the table does not cover.

    :raises ValueError: For a sensor without encodings, an unknown quality,
        Highest without High or a peak below the average.
    """
    if not sensor.video_streams:
        raise ValueError("A sensor needs at least one video encoding")
    warnings = []
    qualities = {stream.quality for stream in sensor.video_streams}
    if not qualities <= set(VideoQuality):
        raise ValueError(f"Unknown video quality in {sorted(qualities)}")
    if VideoQuality.HIGHEST in qualities and VideoQuality.HIGH not in qualities:
        raise ValueError("A Highest video encoding needs a High one")
    for stream in sensor.video_streams:
        name = f"{stream.width}x{stream.height}@{stream.frame_rate}"
        if stream.peak_bitrate < stream.average_bitrate:
            raise ValueError(f"{name}: peak bit rate below the average")
        if (stream.peak_bitrate - stream.average_bitrate) * 10 > stream.peak_bitrate:
            warnings.append(f"{name}: average more than 10 % below the peak")
        short_side = min(stream.width, stream.height)
        rows = [row for row in sorted(_GUIDE_BITRATES) if row >= short_side]
        if not rows:
            warnings.append(f"{name}: larger than the guide's 4K bit rate table")
            continue
        guide = _GUIDE_BITRATES[rows[0]]
        if stream.peak_bitrate > guide[1]:
            warnings.append(f"{name}: peak above the guide's {guide[1]} kbps")
    return warnings
