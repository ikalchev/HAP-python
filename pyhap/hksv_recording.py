"""TLV codecs for classic HomeKit Secure Video recording configuration.

The Camera Recording Management service advertises the container, video and
audio configurations the accessory supports and receives the controller's
selection, using the standard HAP TLV8 layout (repeated items separated by a
zero-length type-0 TLV).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import struct
from typing import List, Union

from pyhap import tlv

_SEPARATOR = b"\x00\x00"


class EventTrigger(IntEnum):
    MOTION = 0x01
    DOORBELL = 0x02


class MediaContainerType(IntEnum):
    FRAGMENTED_MP4 = 0x00


class VideoCodecType(IntEnum):
    H264 = 0x00
    H265 = 0x01


class AudioCodecType(IntEnum):
    AAC_LC = 0x00
    AAC_ELD = 0x01


class AudioSampleRate(IntEnum):
    KHZ_8 = 0x00
    KHZ_16 = 0x01
    KHZ_24 = 0x02
    KHZ_32 = 0x03
    KHZ_44_1 = 0x04
    KHZ_48 = 0x05


class BitRateMode(IntEnum):
    VARIABLE = 0x00
    CONSTANT = 0x01


def _u8(value: int) -> bytes:
    return struct.pack("<B", value)


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _join(items: List[bytes]) -> bytes:
    return _SEPARATOR.join(items)


def _iter(data: bytes):
    offset = 0
    while offset < len(data):
        tag = data[offset]
        length = data[offset + 1]
        value = data[offset + 2 : offset + 2 + length]
        offset += 2 + length
        while length == 255 and offset < len(data) and data[offset] == tag:
            length = data[offset + 1]
            value += data[offset + 2 : offset + 2 + length]
            offset += 2 + length
        yield tag, value


def _decode(data: bytes) -> dict:
    out: dict = {}
    for tag, value in _iter(data):
        out.setdefault(tag, value)
    return out


def _split(data: bytes) -> list:
    items = []
    current = b""
    for tag, value in _iter(data):
        if tag == 0 and not value:
            if current:
                items.append(current)
            current = b""
            continue
        current += tlv.encode(_u8(tag), value)
    if current:
        items.append(current)
    return items


@dataclass
class MediaContainerConfiguration:
    fragment_length_ms: int = 4000
    container_type: MediaContainerType = MediaContainerType.FRAGMENTED_MP4

    def encode(self) -> bytes:
        params = tlv.encode(b"\x01", _u32(self.fragment_length_ms))
        return tlv.encode(b"\x01", _u8(self.container_type), b"\x02", params)

    @classmethod
    def decode(cls, data: bytes) -> "MediaContainerConfiguration":
        d = _decode(data)
        params = _decode(d[2])
        return cls(
            container_type=MediaContainerType(_int(d[1])),
            fragment_length_ms=_int(params[1]),
        )


@dataclass
class SupportedRecordingConfiguration:
    prebuffer_length_ms: int = 4000
    event_triggers: int = EventTrigger.MOTION
    media_containers: List[MediaContainerConfiguration] = field(
        default_factory=lambda: [MediaContainerConfiguration()]
    )

    def encode(self) -> bytes:
        return tlv.encode(
            b"\x01",
            _u32(self.prebuffer_length_ms),
            b"\x02",
            struct.pack("<Q", self.event_triggers),
            b"\x03",
            _join([c.encode() for c in self.media_containers]),
        )

    @classmethod
    def decode(cls, data: bytes) -> "SupportedRecordingConfiguration":
        d = _decode(data)
        return cls(
            prebuffer_length_ms=_int(d[1]),
            event_triggers=_int(d[2]),
            media_containers=[
                MediaContainerConfiguration.decode(i) for i in _split(d[3])
            ],
        )


@dataclass
class VideoAttributes:
    width: int
    height: int
    frame_rate: int

    def encode(self) -> bytes:
        return tlv.encode(
            b"\x01",
            _u16(self.width),
            b"\x02",
            _u16(self.height),
            b"\x03",
            _u8(self.frame_rate),
        )

    @classmethod
    def decode(cls, data: bytes) -> "VideoAttributes":
        d = _decode(data)
        return cls(width=_int(d[1]), height=_int(d[2]), frame_rate=_int(d[3]))


@dataclass
class VideoCodecConfiguration:
    codec_type: VideoCodecType
    profile: Union[int, List[int]]
    level: Union[int, List[int]]
    bitrate_kbps: int
    iframe_interval_ms: int
    attributes: List[VideoAttributes]

    def encode(self) -> bytes:
        # Matches HAP-NodeJS' SupportedVideoRecordingConfiguration exactly:
        #   * one ProfileID (0x01) and one Level (0x02) TLV entry per supported
        #     value, with a zero-length 0x00 separator between consecutive
        #     entries of the SAME type (HAP list convention);
        #   * NO bitrate/iframe-interval in the *supported* advertisement (those
        #     only appear in the controller-written *selected* configuration);
        #   * each VideoAttributes as its OWN 0x03 TLV, separated by 0x00 —
        #     not bundled inside a single 0x03.
        profiles = (
            self.profile if isinstance(self.profile, (list, tuple)) else [self.profile]
        )
        levels = self.level if isinstance(self.level, (list, tuple)) else [self.level]
        prof_bytes = _SEPARATOR.join(tlv.encode(b"\x01", _u8(p)) for p in profiles)
        lvl_bytes = _SEPARATOR.join(tlv.encode(b"\x02", _u8(lvl)) for lvl in levels)
        params = prof_bytes + lvl_bytes
        attrs = _SEPARATOR.join(
            tlv.encode(b"\x03", a.encode()) for a in self.attributes
        )
        return (
            tlv.encode(b"\x01", _u8(self.codec_type))
            + tlv.encode(b"\x02", params)
            + attrs
        )

    def encode_selected(self) -> bytes:
        """Encode the single negotiated configuration the controller writes.

        One profile/level plus bitrate + iframe interval and one resolution.
        """
        profile = (
            self.profile[0] if isinstance(self.profile, (list, tuple)) else self.profile
        )
        level = self.level[0] if isinstance(self.level, (list, tuple)) else self.level
        params = tlv.encode(
            b"\x01",
            _u8(profile),
            b"\x02",
            _u8(level),
            b"\x03",
            _u32(self.bitrate_kbps),
            b"\x04",
            _u32(self.iframe_interval_ms),
        )
        return tlv.encode(
            b"\x01",
            _u8(self.codec_type),
            b"\x02",
            params,
            b"\x03",
            self.attributes[0].encode(),
        )

    @classmethod
    def decode(cls, data: bytes) -> "VideoCodecConfiguration":
        d = _decode(data)
        # Collect ALL profile (0x01) and level (0x02) entries: the supported
        # advertisement lists several (0x00-separated), the selected config a
        # single one. A lone value decodes to a scalar, several to a list, so
        # decode is the faithful inverse of encode in both directions.
        profiles = [_int(v) for t, v in _iter(d[2]) if t == 1]
        levels = [_int(v) for t, v in _iter(d[2]) if t == 2]
        params = _decode(d[2])
        # Attributes are separate top-level 0x03 TLVs; gather them all. Bitrate
        # (0x03) and iframe interval (0x04) are only present in the controller's
        # SELECTED config, absent from the supported advertisement.
        attributes = [VideoAttributes.decode(v) for t, v in _iter(data) if t == 3]
        return cls(
            codec_type=VideoCodecType(_int(d[1])),
            profile=profiles[0] if len(profiles) == 1 else profiles,
            level=levels[0] if len(levels) == 1 else levels,
            bitrate_kbps=_int(params[3]) if 3 in params else 0,
            iframe_interval_ms=_int(params[4]) if 4 in params else 0,
            attributes=attributes,
        )


def encode_supported_video(configs: List[VideoCodecConfiguration]) -> bytes:
    # Each configuration is its own 0x01 TLV; multiple configs are separated by
    # a zero-length 0x00 TLV (a config's own body already contains 0x00 list
    # separators, so we must not flatten them under a single 0x01).
    return _SEPARATOR.join(tlv.encode(b"\x01", c.encode()) for c in configs)


def decode_supported_video(data: bytes) -> List[VideoCodecConfiguration]:
    return [VideoCodecConfiguration.decode(v) for t, v in _iter(data) if t == 1]


@dataclass
class AudioCodecConfiguration:
    codec_type: AudioCodecType = AudioCodecType.AAC_LC
    channels: int = 1
    bitrate_mode: BitRateMode = BitRateMode.VARIABLE
    sample_rate: AudioSampleRate = AudioSampleRate.KHZ_32
    max_audio_bitrate_kbps: int = 64

    def encode(self) -> bytes:
        # Matches HAP-NodeJS' SupportedAudioRecordingConfiguration: channels,
        # bit-rate mode and sample rate only — no max-bitrate field.
        params = tlv.encode(
            b"\x01",
            _u8(self.channels),
            b"\x02",
            _u8(self.bitrate_mode),
            b"\x03",
            _u8(self.sample_rate),
        )
        return tlv.encode(b"\x01", _u8(self.codec_type), b"\x02", params)

    def encode_selected(self) -> bytes:
        """Encode the single negotiated audio configuration.

        Unlike the supported advertisement it includes the max-bitrate field.
        """
        params = tlv.encode(
            b"\x01",
            _u8(self.channels),
            b"\x02",
            _u8(self.bitrate_mode),
            b"\x03",
            _u8(self.sample_rate),
            b"\x04",
            _u32(self.max_audio_bitrate_kbps),
        )
        return tlv.encode(b"\x01", _u8(self.codec_type), b"\x02", params)

    @classmethod
    def decode(cls, data: bytes) -> "AudioCodecConfiguration":
        d = _decode(data)
        params = _decode(d[2])
        return cls(
            codec_type=AudioCodecType(_int(d[1])),
            channels=_int(params[1]),
            bitrate_mode=BitRateMode(_int(params[2])),
            sample_rate=AudioSampleRate(_int(params[3])),
            max_audio_bitrate_kbps=_int(params[4]) if 4 in params else 0,
        )


def encode_supported_audio(configs: List[AudioCodecConfiguration]) -> bytes:
    return _SEPARATOR.join(tlv.encode(b"\x01", c.encode()) for c in configs)


def decode_supported_audio(data: bytes) -> List[AudioCodecConfiguration]:
    return [AudioCodecConfiguration.decode(v) for t, v in _iter(data) if t == 1]


@dataclass
class SelectedRecordingConfiguration:
    """The controller's chosen recording configuration."""

    recording: SupportedRecordingConfiguration
    video: VideoCodecConfiguration
    audio: AudioCodecConfiguration

    def encode(self) -> bytes:
        # The selected configuration carries the single negotiated codec params
        # (with bitrate/iframe), not the multi-value supported advertisement.
        return tlv.encode(
            b"\x01",
            self.recording.encode(),
            b"\x02",
            self.video.encode_selected(),
            b"\x03",
            self.audio.encode_selected(),
        )

    @classmethod
    def decode(cls, data: bytes) -> "SelectedRecordingConfiguration":
        d = _decode(data)
        return cls(
            recording=SupportedRecordingConfiguration.decode(d[1]),
            video=VideoCodecConfiguration.decode(d[2]),
            audio=AudioCodecConfiguration.decode(d[3]),
        )
