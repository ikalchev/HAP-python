"""Tests for pyhap.hksv3."""

from collections.abc import Callable
import random
from uuid import UUID

import pytest

from pyhap import hksv3
from pyhap.hksv3 import (
    AudioBitDepth,
    AudioCodec,
    AudioSampleRate,
    AudioStreamTier,
    CameraCapabilities,
    CameraZones,
    EndpointAddress,
    IceCandidate,
    RTPStreamingCommand,
    RTPStreamingControl,
    RTPStreamingStatus,
    SensorConfiguration,
    SensorIntent,
    SensorType,
    SetupEndpoints,
    SetupEndpointsResponse,
    SetupEndpointsStatus,
    SFrameKey,
    SRTPCryptoSuite,
    SRTPParameters,
    SupportedAudioStreamTiers,
    SupportedVideoStreamTiers,
    VideoCodec,
    VideoQuality,
    VideoStreamCapability,
    VideoStreamTier,
    WebRTCOfferOptions,
    WebRTCProvideAnswer,
    WebRTCReoffer,
    WebRTCReofferResponse,
    WebRTCSessionStatus,
    WebRTCSolicitOffer,
    WebRTCSolicitOfferResponse,
    WebRTCSolicitOfferStatus,
    WebRTCStreamingCommand,
    WebRTCStreamingControl,
    WebRTCStreamingStatus,
    WebRTCUpdateSession,
    ZoneData,
    ZoneMethod,
    ZonePolygon,
)

SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
STREAM = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
ZONE = UUID("0123456789abcdef0123456789abcdef")


def reference_parse(data: bytes) -> list[tuple[int, bytes]]:
    """Parse TLV8 the way the HAP TLV8 list encoding defines it.

    A TLV of the same type continues the value only after a full 255-byte
    fragment, a zero-length type 0 TLV is a separator and no item.
    """
    items: list[tuple[int, bytes]] = []
    previous: tuple[int, int] | None = None
    rest = memoryview(data)
    while rest:
        tag, length, value = rest[0], rest[1], bytes(rest[2 : 2 + rest[1]])
        assert len(value) == length
        rest = rest[2 + length :]
        if previous == (tag, 255):
            items[-1] = (tag, items[-1][1] + value)
        elif (tag, length) != (0, 0):
            items.append((tag, value))
        previous = (tag, length)
    return items


def test_encode_item_fragments() -> None:
    """Test values over 255 bytes are split into TLVs of the same type."""
    value = bytes(range(256)) * 2 + b"\x01" * 88
    encoded = hksv3.encode_item(3, value)
    assert (
        encoded
        == (b"\x03\xff" + value[:255] + b"\x03\xff" + value[255:510] + b"\x03\x5a")
        + value[510:]
    )
    assert reference_parse(encoded) == [(3, value)]


@pytest.mark.parametrize("length", [0, 1, 255, 510])
def test_encode_item_boundaries(length: int) -> None:
    """Test empty values and multiples of 255 bytes have no extra TLV."""
    value = b"\xaa" * length
    encoded = hksv3.encode_item(7, value)
    assert len(encoded) == length + 2 * max(1, -(-length // 255))
    assert reference_parse(encoded) == [(7, value)]


def test_encode_list() -> None:
    """Test list items are separated by an empty type 0 TLV."""
    assert hksv3.encode_list(3, [b"\xaa", b"\xbb\xcc", b""]) == bytes.fromhex(
        "0301aa 0000 0302bbcc 0000 0300"
    )
    assert hksv3.encode_list(3, []) == b""


@pytest.mark.parametrize(
    ("data", "items"),
    [
        (
            "010105 0302aabb 0000 0301cc",
            [(1, "05"), (3, "aabb"), (3, "cc")],
        ),
        ("0000 0301aa 0000", [(3, "aa")]),
        ("0301aa 0302bbcc 0401dd", [(3, "aa"), (3, "bbcc"), (4, "dd")]),
        ("0300 0000 0300", [(3, ""), (3, "")]),
        ("0301aa 0401bb 0301cc", [(3, "aa"), (4, "bb"), (3, "cc")]),
    ],
)
def test_decode_items(data: str, items: list[tuple[int, str]]) -> None:
    """Test decoding hand written TLV8 lists."""
    raw = bytes.fromhex(data)
    expected = [(tag, bytes.fromhex(value)) for tag, value in items]
    assert hksv3.decode_items(raw) == expected
    assert reference_parse(raw) == expected


def test_decode_items_fragments() -> None:
    """Test a fragmented value ends at a separator."""
    first = bytes(range(255)) + b"\x01\x02"
    raw = b"\x05\xff" + first[:255] + b"\x05\x02\x01\x02\x00\x00\x05\x01\x03"
    assert hksv3.decode_items(raw) == [(5, first), (5, b"\x03")]


@pytest.mark.parametrize("data", ["03", "0302aa", "0301aa 04"])
def test_decode_items_truncated(data: str) -> None:
    """Test truncated data raises."""
    with pytest.raises(ValueError, match="Truncated"):
        hksv3.decode_items(bytes.fromhex(data))


def _random_tlv8(seed: int) -> bytes:
    """Build a random TLV8 sequence with separators and full fragments."""
    rng = random.Random(seed)
    tlvs = [
        bytes((tag, length)) + rng.randbytes(length)
        for tag, length in (
            (rng.randint(0, 4), rng.choice([0, 0, 1, 2, 254, 255, 255]))
            for _ in range(rng.randint(0, 12))
        )
    ]
    return b"".join(tlvs)


@pytest.mark.parametrize("seed", range(50))
def test_decode_items_matches_reference(seed: int) -> None:
    """Test random TLV8 sequences parse like the reference parser."""
    raw = _random_tlv8(seed)
    assert hksv3.decode_items(raw) == reference_parse(raw)


def _capabilities() -> CameraCapabilities:
    streams = [
        VideoStreamCapability(STREAM, VideoQuality.HIGH, 1920, 1080, 30, 1700, 1800),
        VideoStreamCapability(STREAM, VideoQuality.LOW, 640, 360, 15, 180, 190),
    ]
    return CameraCapabilities(
        [
            SensorConfiguration(SENSOR, 2688, 1520, video_streams=streams),
            SensorConfiguration(
                STREAM, 1600, 1200, SensorType.GENERIC, SensorIntent.PACKAGE
            ),
        ]
    )


def test_camera_capabilities_golden() -> None:
    """Test a single sensor capabilities value byte for byte."""
    stream = VideoStreamCapability(STREAM, VideoQuality.LOW, 640, 360, 15, 180, 190)
    value = CameraCapabilities(
        [SensorConfiguration(SENSOR, 1920, 1080, video_streams=[stream])]
    )
    stream_tlv = (
        "0110"
        + STREAM.hex
        + "020104"
        + "03028002"
        + "04026801"
        + "05010f"
        + "0604b4000000"
        + "0704be000000"
    )
    sensor_tlv = (
        "0108"
        + "01028007"
        + "02023804"
        + "0210"
        + SENSOR.hex
        + "030101"
        + "040101"
        + "05"
        + f"{len(stream_tlv) // 2:02x}"
        + stream_tlv
    )
    expected = "010101" + "02" + f"{len(sensor_tlv) // 2 + 2:02x}"
    expected += "01" + f"{len(sensor_tlv) // 2:02x}" + sensor_tlv
    assert value.encode() == bytes.fromhex(expected)


def test_camera_capabilities_lists() -> None:
    """Test sensors and video streams are separate list items."""
    top = reference_parse(_capabilities().encode())
    assert [tag for tag, _ in top] == [1, 2]
    assert top[0][1] == b"\x01"
    sensors = reference_parse(top[1][1])
    assert [tag for tag, _ in sensors] == [1, 1]

    first = dict(reference_parse(sensors[0][1]))
    assert reference_parse(first[1]) == [(1, b"\x80\x0a"), (2, b"\xf0\x05")]
    assert first[2] == SENSOR.bytes
    assert first[3] == bytes([SensorType.PRIMARY])
    assert first[4] == bytes([SensorIntent.MAIN])
    streams = [value for tag, value in reference_parse(sensors[0][1]) if tag == 5]
    assert len(streams) == 2
    assert dict(reference_parse(streams[1]))[2] == bytes([VideoQuality.LOW])

    second = reference_parse(sensors[1][1])
    assert (3, b"\xff") in second
    assert (4, bytes([SensorIntent.PACKAGE])) in second
    assert 5 not in dict(second)


def test_camera_capabilities_fragmented() -> None:
    """Test a value over 255 bytes, two sensors with four encodings each."""
    qualities = list(VideoQuality)
    sensors = [
        SensorConfiguration(
            sensor_uuid,
            3840,
            2160,
            video_streams=[
                VideoStreamCapability(UUID(int=i), quality, 640, 360, 15, 180, 190)
                for i, quality in enumerate(qualities)
            ],
        )
        for sensor_uuid in (SENSOR, STREAM)
    ]
    encoded = CameraCapabilities(sensors).encode()
    # Version, then the sensors list in a full and a partial type 2 fragment.
    assert encoded[:5] == bytes.fromhex("010101 02ff")
    assert encoded[260] == 2
    assert len(encoded) == 3 + 2 * 2 + 454

    top = reference_parse(encoded)
    assert [(tag, len(value)) for tag, value in top] == [(1, 1), (2, 454)]
    items = reference_parse(top[1][1])
    assert [tag for tag, _ in items] == [1, 1]
    sensor_fields = [reference_parse(item) for _, item in items]
    streams = [
        [dict(reference_parse(value)) for tag, value in fields if tag == 5]
        for fields in sensor_fields
    ]
    assert [dict(fields)[2] for fields in sensor_fields] == [
        SENSOR.bytes,
        STREAM.bytes,
    ]
    expected = [(UUID(int=i).bytes, bytes([q])) for i, q in enumerate(qualities)]
    assert [[(s[1], s[2]) for s in sensor] for sensor in streams] == [expected] * 2


def test_contributing_sensors() -> None:
    """Test each contributing sensor is its own list item."""
    encoded = hksv3.encode_contributing_sensors([SENSOR, STREAM])
    assert encoded == bytes.fromhex(
        "0112 0110" + SENSOR.hex + "0000 0112 0110" + STREAM.hex
    )
    assert hksv3.encode_contributing_sensors([]) == b""


def _zones_hex(method: str = "01", vertices: str = "0000000080070000") -> str:
    polygon_a = "0110" + ZONE.hex + "03" + f"{len(vertices) // 2:02x}" + vertices
    polygon_b = "0110" + STREAM.hex + "030c" + "0a000a00" + "14000a00" + "0a001400"
    zone = (
        "0101"
        + method
        + "03"
        + f"{len(polygon_a) // 2:02x}"
        + polygon_a
        + "0000"
        + "03"
        + f"{len(polygon_b) // 2:02x}"
        + polygon_b
    )
    return "010102" + "02" + f"{len(zone) // 2:02x}" + zone


def test_camera_zones_decode() -> None:
    """Test decoding hand written zones with two polygons."""
    raw = bytes.fromhex(_zones_hex())
    zones = CameraZones.decode(raw)
    assert zones == CameraZones(
        [
            ZoneData(
                ZoneMethod.NORMAL,
                [
                    ZonePolygon(ZONE, [(0, 0), (1920, 0)]),
                    ZonePolygon(STREAM, [(10, 10), (20, 10), (10, 20)]),
                ],
            )
        ]
    )
    assert zones.encode() == raw


def test_camera_zones_several() -> None:
    """Test several zone data items survive a round trip."""
    zones = CameraZones(
        [
            ZoneData(ZoneMethod.INVERTED, [ZonePolygon(ZONE, [(1, 2)])]),
            ZoneData(ZoneMethod.NORMAL),
        ]
    )
    encoded = zones.encode()
    assert [tag for tag, _ in reference_parse(encoded)] == [1, 2, 2]
    assert CameraZones.decode(encoded) == zones


def test_camera_zones_fragmented_polygon() -> None:
    """Test a polygon with more than 255 bytes of vertices."""
    vertices = [(i, 2 * i) for i in range(100)]
    zones = CameraZones([ZoneData(ZoneMethod.NORMAL, [ZonePolygon(ZONE, vertices)])])
    encoded = zones.encode()
    zone = reference_parse(reference_parse(encoded)[1][1])
    assert [tag for tag, _ in zone] == [1, 3]
    polygon = dict(reference_parse(zone[1][1]))
    assert polygon[1] == ZONE.bytes
    assert polygon[3] == b"".join(
        x.to_bytes(2, "little") + y.to_bytes(2, "little") for x, y in vertices
    )
    assert CameraZones.decode(encoded) == zones


def test_camera_zones_unknown_method() -> None:
    """Test an unknown zone method is kept as int."""
    zones = CameraZones.decode(bytes.fromhex(_zones_hex(method="09")))
    assert zones.zones[0].method == 9
    assert not isinstance(zones.zones[0].method, ZoneMethod)


def test_camera_zones_unknown_types() -> None:
    """Test unknown TLV types are ignored at every level."""
    polygon = "0110" + ZONE.hex + "0304" + "01000200" + "0902aabb"
    zone = "010101" + "0901cc" + "03" + f"{len(polygon) // 2:02x}" + polygon
    raw = bytes.fromhex("010102" + "0a01dd" + "02" + f"{len(zone) // 2:02x}" + zone)
    assert reference_parse(raw)[1] == (10, b"\xdd")
    assert CameraZones.decode(raw) == CameraZones(
        [ZoneData(ZoneMethod.NORMAL, [ZonePolygon(ZONE, [(1, 2)])])]
    )


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        # No method.
        (
            "0306" + "0304" + "01000200",
            ZoneData(ZoneMethod.NORMAL, [ZonePolygon(None, [(1, 2)])]),
        ),
        # No polygon identifier and no vertices.
        ("010102" + "0300", ZoneData(ZoneMethod.INVERTED, [ZonePolygon(None, [])])),
    ],
)
def test_camera_zones_missing_fields(zone: str, expected: ZoneData) -> None:
    """Test missing fields get defaults, like HAP-NodeJS reads them."""
    raw = bytes.fromhex("010102" + "02" + f"{len(zone) // 2:02x}" + zone)
    zones = CameraZones.decode(raw)
    assert zones == CameraZones([expected])
    assert CameraZones.decode(zones.encode()) == zones


def test_camera_zones_other_version() -> None:
    """Test zone data of another version is not interpreted."""
    zones = CameraZones.decode(bytes.fromhex("010103 0203aabbcc"))
    assert zones == CameraZones([], 3)


@pytest.mark.parametrize(
    "data",
    [
        "",
        "0100",
        "010902000000000000000000",
        _zones_hex(vertices="000000"),
        "010102 0203 030100",
        "010102 020b 010101 0306 0102aabb 0300",
    ],
)
def test_camera_zones_invalid(data: str) -> None:
    """Test malformed zones raise ValueError."""
    with pytest.raises(ValueError):
        CameraZones.decode(bytes.fromhex(data))


# 1920x1080@30 High at 1700 kbps as tier 1.
TIER_HEX = "010401000000 020102 0304a4060000 04028007 05023804 06011e"
# 640x360@15 Low at 180 kbps as tier 3.
LOW_TIER_HEX = "010403000000 020104 0304b4000000 04028002 05026801 06010f"
# Opus at 24 kbps, 48 kHz, 16 bit, 20 ms, mono as tier 1.
AUDIO_TIER_HEX = "010401000000 0204c05d0000 030104 040102 050114 060101"
SESSION = UUID("7f3c2a10-4b5d-4e6f-8a9b-0c1d2e3f4a5b")


def test_video_stream_tier_golden() -> None:
    """Test a video tier byte for byte."""
    tier = VideoStreamTier(1, VideoQuality.HIGH, 1700, 1920, 1080, 30)
    assert tier.encode() == bytes.fromhex(TIER_HEX)


def test_supported_video_stream_tiers_golden() -> None:
    """Test the tiers are list items after the codec and payload type."""
    value = SupportedVideoStreamTiers(
        VideoCodec.H265,
        99,
        [
            VideoStreamTier(1, VideoQuality.HIGH, 1700, 1920, 1080, 30),
            VideoStreamTier(3, VideoQuality.LOW, 180, 640, 360, 15),
        ],
    )
    encoded = value.encode()
    assert encoded == bytes.fromhex(
        "010102 020163 031a" + TIER_HEX + "0000 031a" + LOW_TIER_HEX
    )
    assert reference_parse(encoded) == [
        (1, b"\x02"),
        (2, b"\x63"),
        (3, bytes.fromhex(TIER_HEX)),
        (3, bytes.fromhex(LOW_TIER_HEX)),
    ]
    assert reference_parse(bytes.fromhex(LOW_TIER_HEX))[3] == (4, b"\x80\x02")


def test_supported_audio_stream_tiers_golden() -> None:
    """Test an Opus tier with the defaults the guide allows."""
    value = SupportedAudioStreamTiers(
        AudioCodec.OPUS, 110, [AudioStreamTier(1, 24000, AudioSampleRate.KHZ_48)]
    )
    encoded = value.encode()
    assert encoded == bytes.fromhex("010103 02016e 0318" + AUDIO_TIER_HEX)
    assert reference_parse(encoded)[2] == (3, bytes.fromhex(AUDIO_TIER_HEX))
    assert AudioStreamTier(1, 24000, 1, AudioBitDepth.BITS_8).encode()[12:18] == (
        bytes.fromhex("030101 040101")
    )


def test_supported_stream_tiers_empty() -> None:
    """Test a codec without tiers has no tier item."""
    assert SupportedAudioStreamTiers(AudioCodec.OPUS, 110, []).encode() == (
        bytes.fromhex("010103 02016e")
    )


def test_rtp_streaming_control_start() -> None:
    """Test decoding a start with video and audio."""
    data = bytes.fromhex(
        "0110" + SESSION.hex + "020102"
        "040478563412 030402000000 0504 01000000 0604efbeadde"
    )
    assert RTPStreamingControl.decode(data) == RTPStreamingControl(
        SESSION, RTPStreamingCommand.START, 2, 0x12345678, 1, 0xDEADBEEF
    )


def test_rtp_streaming_control_end() -> None:
    """Test an end has no tiers, short integers are accepted."""
    request = RTPStreamingControl.decode(
        bytes.fromhex("0110" + SESSION.hex + "020101 030103")
    )
    assert request == RTPStreamingControl(SESSION, RTPStreamingCommand.END, 3)


def test_rtp_streaming_control_largest_ssrc() -> None:
    """Test the largest 32-bit SSRC still decodes for video and audio."""
    request = RTPStreamingControl.decode(
        bytes.fromhex("0110" + SESSION.hex + "020102 0404ffffffff 0604ffffffff")
    )
    assert request.video_ssrc == 0xFFFFFFFF
    assert request.audio_ssrc == 0xFFFFFFFF


def test_rtp_streaming_control_unknown_values() -> None:
    """Test unknown commands and types are kept and skipped."""
    request = RTPStreamingControl.decode(
        bytes.fromhex("0110" + SESSION.hex + "020109 0902aabb")
    )
    assert request.command == 9
    assert request.video_tier is None


@pytest.mark.parametrize(
    "data",
    [
        "020102",
        "0110" + SESSION.hex,
        "0104aabbccdd 020102",
        "0110" + SESSION.hex + "020102 0300",
        "0110" + SESSION.hex + "0201",
        "0110" + SESSION.hex + "020102 04050000000001",
        "0110" + SESSION.hex + "020102 06050000000001",
    ],
)
def test_rtp_streaming_control_invalid(data: str) -> None:
    """Test missing fields, bad lengths, truncated data and wide SSRCs raise."""
    with pytest.raises(ValueError):
        RTPStreamingControl.decode(bytes.fromhex(data))


def test_streaming_status() -> None:
    """Test the read back value with and without a session."""
    encoded = hksv3.encode_streaming_status(SESSION, RTPStreamingStatus.BUSY)
    assert encoded == bytes.fromhex("0110" + SESSION.hex + "020103")
    assert reference_parse(encoded) == [(1, SESSION.bytes), (2, b"\x03")]
    assert hksv3.encode_streaming_status(None, RTPStreamingStatus.ERROR) == (
        b"\x02\x01\x04"
    )


def test_tiers_from_sensor() -> None:
    """Test each encoding becomes a tier numbered in order, with its peak."""
    sensor = _capabilities().sensors[0]
    assert hksv3.tiers_from_sensor(sensor) == [
        VideoStreamTier(1, VideoQuality.HIGH, 1700, 1920, 1080, 30, 1800),
        VideoStreamTier(2, VideoQuality.LOW, 180, 640, 360, 15, 190),
    ]
    assert hksv3.tiers_from_sensor(SensorConfiguration(SENSOR, 640, 360)) == []
    # The peak is not part of the TLV.
    assert hksv3.tiers_from_sensor(sensor)[0].encode() == bytes.fromhex(TIER_HEX)


def test_tiers_are_frozen() -> None:
    """Test a tier cannot change after it was advertised."""
    with pytest.raises(AttributeError):
        hksv3.DEFAULT_AUDIO_TIER.target_bitrate = 1  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "tier",
    [
        AudioStreamTier(1, 24000, AudioSampleRate.KHZ_48, packet_time=40),
        AudioStreamTier(1, 24000, AudioSampleRate.KHZ_48, channels=2),
        AudioStreamTier(1, 24000, 5),
        AudioStreamTier(1, 24000, AudioSampleRate.KHZ_48, bit_depth=4),
        AudioStreamTier(1, 0, AudioSampleRate.KHZ_48),
    ],
)
def test_check_audio_tier_invalid(tier: AudioStreamTier) -> None:
    """Test audio tiers the guide does not allow raise."""
    with pytest.raises(ValueError):
        hksv3.check_audio_tier(tier)


def test_check_audio_tier_default() -> None:
    """Test the default audio tier is Opus at 48 kHz as the guide reports it."""
    hksv3.check_audio_tier(hksv3.DEFAULT_AUDIO_TIER)
    assert hksv3.DEFAULT_AUDIO_TIER.sample_rate == AudioSampleRate.KHZ_48


def _sensor(*streams: tuple[int, int, int, int, int]) -> SensorConfiguration:
    """A sensor with (quality, width, height, average, peak) encodings."""
    return SensorConfiguration(
        UUID(int=0),
        3840,
        2160,
        video_streams=[
            VideoStreamCapability(
                UUID(int=index), quality, width, height, 30, avg, peak
            )
            for index, (quality, width, height, avg, peak) in enumerate(streams)
        ],
    )


GUIDE_4K = (2, 3840, 2160, 4500, 5000)
GUIDE_1080 = (3, 1920, 1080, 1700, 1800)
GUIDE_360 = (4, 640, 360, 180, 190)


def test_check_video_tiers_guide_table() -> None:
    """Test the guide's own bit rates give no warning."""
    assert hksv3.check_video_tiers(_sensor(GUIDE_4K, GUIDE_1080, GUIDE_360)) == []


@pytest.mark.parametrize(
    "streams",
    [
        ((2, 2880, 2160, 4500, 5000), (3, 1600, 1200, 1700, 1800), GUIDE_360),
        ((2, 2400, 3200, 4500, 5000), (3, 1200, 1600, 1700, 1800), GUIDE_360),
        ((2, 2880, 2880, 4500, 5000), (3, 1440, 1440, 1700, 1800), GUIDE_360),
        ((2, 2560, 1440, 2800, 3000), GUIDE_1080, (4, 640, 480, 180, 190)),
        ((2, 2048, 1536, 2800, 3000), (3, 1600, 1200, 1700, 1800), GUIDE_360),
        ((2, 1536, 2048, 2800, 3000), (3, 1200, 1600, 1700, 1800), GUIDE_360),
        ((2, 1920, 1920, 2800, 3000), (3, 1440, 1440, 1700, 1800), GUIDE_360),
        (GUIDE_1080, (3, 1280, 720, 768, 800), (4, 426, 240, 180, 190)),
        (GUIDE_1080, (3, 1440, 1080, 768, 800), (4, 640, 480, 180, 190)),
        (
            (2, 1200, 1600, 1700, 1800),
            (3, 960, 1280, 768, 800),
            (4, 480, 640, 180, 190),
        ),
        (
            (2, 1440, 1440, 1700, 1800),
            (3, 1080, 1080, 768, 800),
            (4, 480, 480, 180, 190),
        ),
    ],
)
def test_check_video_tiers_guide_resolutions(
    streams: tuple[tuple[int, int, int, int, int], ...],
) -> None:
    """Test every resolution of the guide at its class's bit rates gives no warning."""
    assert hksv3.check_video_tiers(_sensor(*streams)) == []


def test_check_video_tiers_above_4k() -> None:
    """Test a frame above 4K, here DCI 4K, is held to the guide's 4K row."""
    dci_4k = (2, 4096, 2160, 4500, 5000)
    assert hksv3.check_video_tiers(_sensor(dci_4k, GUIDE_1080, GUIDE_360)) == []


@pytest.mark.parametrize(
    ("stream", "warning"),
    [
        ((3, 2048, 1536, 2800, 3100), "peak above the guide's 3000 kbps"),
        ((3, 1920, 1920, 2800, 3100), "peak above the guide's 3000 kbps"),
        ((3, 1440, 1440, 1750, 1900), "peak above the guide's 1800 kbps"),
        ((3, 1440, 1080, 768, 850), "peak above the guide's 800 kbps"),
        ((3, 640, 480, 180, 200), "peak above the guide's 190 kbps"),
    ],
)
def test_check_video_tiers_class_by_pixels(
    stream: tuple[int, int, int, int, int], warning: str
) -> None:
    """Test a frame takes the row of its class in the guide, not of its short side."""
    (found,) = hksv3.check_video_tiers(_sensor(GUIDE_4K, stream, GUIDE_360))
    assert warning in found


@pytest.mark.parametrize(
    ("stream", "warning"),
    [
        ((3, 1920, 1080, 1500, 1800), "average more than 10 % below the peak"),
        ((3, 1920, 1080, 1800, 1900), "peak above the guide's 1800 kbps"),
        ((3, 1080, 1920, 1800, 1900), "1080x1920@30: peak above the guide's 1800"),
        ((3, 1280, 600, 900, 950), "1280x600@30: peak above the guide's 800 kbps"),
        ((3, 7680, 4320, 9000, 9500), "7680x4320@30: peak above the guide's 5000"),
    ],
)
def test_check_video_tiers_warnings(
    stream: tuple[int, int, int, int, int], warning: str
) -> None:
    """Test what the guide recommends only warns."""
    (found,) = hksv3.check_video_tiers(_sensor(GUIDE_4K, stream, GUIDE_360))
    assert warning in found


@pytest.mark.parametrize(
    ("streams", "match"),
    [
        ((GUIDE_1080, GUIDE_360, (5, 640, 360, 180, 190)), "Unknown video quality"),
        (((1, 3840, 2160, 4500, 5000), GUIDE_1080, GUIDE_360), "needs a High"),
        ((GUIDE_4K, (3, 1920, 1080, 1700, 1600), GUIDE_360), "below the average"),
        ((), "at least one video encoding"),
    ],
)
def test_check_video_tiers_invalid(
    streams: tuple[tuple[int, int, int, int, int], ...], match: str
) -> None:
    """Test qualities and bit rates the guide does not allow raise."""
    with pytest.raises(ValueError, match=match):
        hksv3.check_video_tiers(_sensor(*streams))


ADDRESS = EndpointAddress("192.168.1.114", False, 50000, 50002)
# 192.168.1.114, IPv4, video port 50000 and audio port 50002.
ADDRESS_HEX = "010100 020d3139322e3136382e312e313134 030250c3 040252c3"
SRTP = SRTPParameters(SRTPCryptoSuite.AES_CM_128_HMAC_SHA1_80, bytes(16), bytes(14))


def test_setup_endpoints_golden() -> None:
    """Test a Setup Endpoints write byte for byte and back."""
    request = SetupEndpoints(SESSION, ADDRESS, SRTP, SRTP)
    srtp_hex = "010100 0210" + "00" * 16 + "030e" + "00" * 14
    encoded = request.encode()

    srtp_items = "0425" + srtp_hex + "0525" + srtp_hex
    assert encoded == bytes.fromhex(
        "0110" + SESSION.hex + "031a" + ADDRESS_HEX + srtp_items
    )
    assert SetupEndpoints.decode(encoded) == request
    assert "master_key" not in repr(request)


def test_setup_endpoints_response() -> None:
    """Test a full answer and a status-only one encode and decode."""
    answer = SetupEndpointsResponse(
        SESSION, SetupEndpointsStatus.SUCCESS, ADDRESS, SRTP, SRTP, 1, 0xDEADBEEF
    )
    encoded = answer.encode()

    assert reference_parse(encoded)[5:] == [
        (6, b"\x01\x00\x00\x00"),
        (7, b"\xef\xbe\xad\xde"),
    ]
    assert SetupEndpointsResponse.decode(encoded) == answer
    assert SetupEndpointsResponse(None, SetupEndpointsStatus.ERROR).encode() == (
        b"\x02\x01\x02"
    )


@pytest.mark.parametrize(
    "data",
    [
        "0110" + SESSION.hex,
        "0110" + SESSION.hex + "0300 0400 0500",
        "0110" + SESSION.hex + "030e 010100 0201ff 030250c3 040252c3 0400 0500",
        "0110" + SESSION.hex + "030d 010100 020131 030150 040252c3 0400 0500",
    ],
    ids=["missing", "empty", "not-utf-8", "short-port"],
)
def test_setup_endpoints_invalid(data: str) -> None:
    """Test missing fields, bad text and bad ports raise."""
    with pytest.raises(ValueError):
        SetupEndpoints.decode(bytes.fromhex(data))


def _item(tag: int, value: bytes) -> str:
    """Hand encode one TLV of up to 255 bytes as hex."""
    return f"{tag:02x}{len(value):02x}{value.hex()}"


HOST = b"candidate:1 1 udp 2122260223 192.0.2.10 50000 typ host"
SRFLX = (
    b"candidate:2 1 udp 1686052607 198.51.100.7 50001 typ srflx"
    b" raddr 192.0.2.10 rport 50000"
)
# The host candidate for the first m-line, mid "0", and one without either.
HOST_HEX = _item(1, HOST) + "020130 03020000"
SRFLX_HEX = _item(1, SRFLX)
CANDIDATES = [
    IceCandidate(HOST.decode(), "0", 0),
    IceCandidate(SRFLX.decode()),
]
SFRAME = SFrameKey(bytes(range(32)), 0x0102030405060708)
SFRAME_HEX = _item(1, bytes(range(32))) + "02080807060504030201"


@pytest.mark.parametrize(
    ("data", "enabled"),
    [("0103 010101", True), ("0103 010100", False), ("0100", None), ("", None)],
)
def test_solicit_offer_decode(data: str, enabled: bool | None) -> None:
    """Test SFrame Enabled, an empty and a missing Options TLV."""
    request = WebRTCSolicitOffer.decode(bytes.fromhex(data))
    assert request == WebRTCSolicitOffer(WebRTCOfferOptions(enabled))


def test_solicit_offer_round_trip() -> None:
    """Test the encoding a controller sends, unknown types are skipped."""
    request = WebRTCSolicitOffer(WebRTCOfferOptions(True))
    assert request.encode() == bytes.fromhex("0103 010101")
    raw = bytes.fromhex("0106 010101 0901aa 0a00")
    assert WebRTCSolicitOffer.decode(raw) == request


def test_solicit_offer_response_golden() -> None:
    """Test a successful response with two candidates and an SFrame key."""
    response = WebRTCSolicitOfferResponse(
        WebRTCSolicitOfferStatus.SUCCESS, SESSION, "v=0\r\n", CANDIDATES, SFRAME
    )
    encoded = response.encode()
    assert encoded == bytes.fromhex(
        "0110"
        + SESSION.hex
        + "0205 763d300d0a"
        + _item(3, bytes.fromhex(HOST_HEX))
        + "0000"
        + _item(3, bytes.fromhex(SRFLX_HEX))
        + "040100"
        + _item(5, bytes.fromhex(SFRAME_HEX))
    )
    top = reference_parse(encoded)
    assert top[2:4] == [
        (3, bytes.fromhex(HOST_HEX)),
        (3, bytes.fromhex(SRFLX_HEX)),
    ]
    assert reference_parse(top[2][1]) == [(1, HOST), (2, b"0"), (3, b"\x00\x00")]
    assert reference_parse(top[5][1]) == [
        (1, bytes(range(32))),
        (2, bytes.fromhex("0807060504030201")),
    ]
    assert WebRTCSolicitOfferResponse.decode(encoded) == response


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (WebRTCSolicitOfferStatus.PRIVACY_MODE_ACTIVE, "040101"),
        (WebRTCSolicitOfferStatus.ERROR, "040102"),
    ],
)
def test_solicit_offer_response_failed(
    status: WebRTCSolicitOfferStatus, data: str
) -> None:
    """Test a failed response has only the status."""
    assert WebRTCSolicitOfferResponse(status).encode() == bytes.fromhex(data)


@pytest.mark.parametrize("length", [255, 300, 600])
def test_solicit_offer_response_long_sdp(length: int) -> None:
    """Test an SDP offer over 255 bytes is fragmented, then the candidates."""
    sdp = ("v=0\r\n" + "a=x" * length)[:length]
    response = WebRTCSolicitOfferResponse(0, SESSION, sdp, CANDIDATES)
    encoded = response.encode()
    assert encoded[18:20] == b"\x02\xff"
    top = reference_parse(encoded)
    assert top[1] == (2, sdp.encode())
    assert top[2] == (3, bytes.fromhex(HOST_HEX))
    assert WebRTCSolicitOfferResponse.decode(encoded) == response


def test_provide_answer_golden() -> None:
    """Test decoding an answer with two candidates."""
    raw = bytes.fromhex(
        "0110"
        + SESSION.hex
        + "0205 763d300d0a"
        + _item(3, bytes.fromhex(SRFLX_HEX))
        + "0000"
        + _item(3, bytes.fromhex(HOST_HEX))
    )
    request = WebRTCProvideAnswer.decode(raw)
    assert request == WebRTCProvideAnswer(SESSION, "v=0\r\n", CANDIDATES[::-1])
    assert request.encode() == raw
    assert reference_parse(raw)[2:] == [
        (3, bytes.fromhex(SRFLX_HEX)),
        (3, bytes.fromhex(HOST_HEX)),
    ]


def test_session_status() -> None:
    """Test the read back status with and without a session."""
    encoded = WebRTCSessionStatus(SESSION, WebRTCStreamingStatus.BUSY).encode()
    assert encoded == bytes.fromhex("0110" + SESSION.hex + "020102")
    assert reference_parse(encoded) == [(1, SESSION.bytes), (2, b"\x02")]
    error = WebRTCSessionStatus(None, WebRTCStreamingStatus.ERROR)
    assert error.encode() == b"\x02\x01\x03"
    assert WebRTCSessionStatus.decode(b"\x02\x01\x03") == error


def test_streaming_control() -> None:
    """Test an End, and an unknown command kept as int."""
    raw = bytes.fromhex("0110" + SESSION.hex + "020101")
    request = WebRTCStreamingControl.decode(raw)
    assert request == WebRTCStreamingControl(SESSION, WebRTCStreamingCommand.END)
    assert request.encode() == raw
    unknown = WebRTCStreamingControl.decode(
        bytes.fromhex("0110" + SESSION.hex + "020107 0901aa")
    )
    assert unknown.command == 7
    assert not isinstance(unknown.command, WebRTCStreamingCommand)


def test_reoffer_golden() -> None:
    """Test a reoffer with and without options."""
    raw = bytes.fromhex("0110" + SESSION.hex + "0205 763d300d0a 0303 010100")
    request = WebRTCReoffer.decode(raw)
    assert request == WebRTCReoffer(SESSION, "v=0\r\n", WebRTCOfferOptions(False))
    assert request.encode() == raw
    plain = WebRTCReoffer.decode(bytes.fromhex("0110" + SESSION.hex + "0200"))
    assert plain == WebRTCReoffer(SESSION, "")
    assert plain.options.sframe_enabled is None


def test_reoffer_response_golden() -> None:
    """Test a successful reoffer response, and a failed one."""
    response = WebRTCReofferResponse(SESSION, 0, "v=0\r\n", SFRAME)
    encoded = response.encode()
    assert encoded == bytes.fromhex(
        "0110"
        + SESSION.hex
        + "0205 763d300d0a 030100"
        + _item(4, bytes.fromhex(SFRAME_HEX))
    )
    assert WebRTCReofferResponse.decode(encoded) == response
    failed = WebRTCReofferResponse(SESSION, WebRTCStreamingStatus.UNKNOWN_SESSION)
    assert failed.encode() == bytes.fromhex("0110" + SESSION.hex + "030101")


def test_update_session_golden() -> None:
    """Test keys to add and key IDs to remove are list items."""
    other = SFrameKey(b"\xaa" * 16, 9)
    other_hex = _item(1, b"\xaa" * 16) + "02080900000000000000"
    raw = bytes.fromhex(
        "0110"
        + SESSION.hex
        + _item(2, bytes.fromhex(SFRAME_HEX))
        + "0000"
        + _item(2, bytes.fromhex(other_hex))
        + "030a 0108 0807060504030201 0000 030a 0108 0200000000000000"
    )
    request = WebRTCUpdateSession.decode(raw)
    assert request == WebRTCUpdateSession(
        SESSION, [SFRAME, other], [0x0102030405060708, 2]
    )
    assert request.encode() == raw
    top = reference_parse(raw)
    assert len(top) == 5
    assert top[1] == (2, bytes.fromhex(SFRAME_HEX))
    assert reference_parse(top[2][1]) == [(1, b"\xaa" * 16), (2, b"\x09" + bytes(7))]
    assert top[3][0] == top[4][0] == 3
    assert reference_parse(top[4][1]) == [(1, b"\x02" + bytes(7))]


def test_update_session_short_and_empty() -> None:
    """Test short key IDs are accepted and lists may be empty."""
    request = WebRTCUpdateSession.decode(
        bytes.fromhex("0110" + SESSION.hex + "0303 010105")
    )
    assert request == WebRTCUpdateSession(SESSION, [], [5])
    empty = WebRTCUpdateSession(SESSION)
    assert empty.encode() == bytes.fromhex("0110" + SESSION.hex)
    assert WebRTCUpdateSession.decode(empty.encode()) == empty


def test_webrtc_unknown_values() -> None:
    """Test unknown statuses are kept as int and unknown types are skipped."""
    response = WebRTCSolicitOfferResponse.decode(
        bytes.fromhex("040109 0901aa 0306 010161 0a01ff")
    )
    assert response == WebRTCSolicitOfferResponse(9, candidates=[IceCandidate("a")])
    status = WebRTCSessionStatus.decode(bytes.fromhex("020107"))
    assert status.status == 7
    assert not isinstance(status.status, WebRTCStreamingStatus)
    assert repr(SFRAME) == f"SFrameKey(kid={SFRAME.kid})"


@pytest.mark.parametrize(
    ("decode", "data"),
    [
        # Missing session, bad session length, missing SDP.
        (WebRTCProvideAnswer.decode, "0205 763d300d0a"),
        (WebRTCProvideAnswer.decode, "0104aabbccdd 0200"),
        (WebRTCReoffer.decode, "0110" + SESSION.hex),
        # A candidate without its string, an SDP that is not UTF-8.
        (WebRTCProvideAnswer.decode, "0110" + SESSION.hex + "0200 0303 020130"),
        (WebRTCProvideAnswer.decode, "0110" + SESSION.hex + "0201ff"),
        # A key ID over 64 bits, a key without its ID.
        (WebRTCUpdateSession.decode, "0110" + SESSION.hex + "030b 0109" + "00" * 9),
        (WebRTCUpdateSession.decode, "0110" + SESSION.hex + "0203 0101aa"),
        (WebRTCStreamingControl.decode, "0110" + SESSION.hex),
        (WebRTCSolicitOffer.decode, "0103 0101"),
        (WebRTCSolicitOfferResponse.decode, "0110" + SESSION.hex),
    ],
)
def test_webrtc_invalid(decode: Callable[[bytes], object], data: str) -> None:
    """Test malformed writes raise ValueError."""
    with pytest.raises(ValueError):
        decode(bytes.fromhex(data))


@pytest.mark.parametrize("size", [0, 15, 65])
def test_sframe_key_size(size: int) -> None:
    """Test a key outside 16 to 64 bytes is refused."""
    with pytest.raises(ValueError, match="SFrame key"):
        SFrameKey.decode(SFrameKey(b"\xaa" * size, 1).encode())


def test_ice_candidate_mline_index_range() -> None:
    """Test the m-line index decodes only what encodes, up to 0xFFFF."""
    candidate = IceCandidate("a", "0", 0xFFFF)
    assert IceCandidate.decode(candidate.encode()) == candidate
    with pytest.raises(ValueError, match="m-line"):
        IceCandidate.decode(bytes.fromhex("010161 0303 000001"))


def test_generate_sframe_key() -> None:
    """Test generated keys have 32 random bytes and a 64-bit key ID."""
    first = hksv3.generate_sframe_key()
    second = hksv3.generate_sframe_key()
    assert len(first.key) == 32
    assert first.key != second.key
    assert 0 <= first.kid < 2**64
    assert SFrameKey.decode(first.encode()) == first
    assert first.key.hex() not in repr(first)


def test_shared_webrtc_types_are_frozen() -> None:
    """Test the offer and answer types the media side gets cannot change."""
    offer = hksv3.WebRTCOffer("v=0\r\n", hksv3.generate_sframe_key())
    with pytest.raises(AttributeError):
        offer.sdp = ""  # ty: ignore[invalid-assignment]
    assert hksv3.WebRTCReofferAnswer("v=0\r\n").sframe is None


LIMIT_CANDIDATE = IceCandidate("candidate:1 1 udp 1 192.168.1.2 50000 typ host")
MAX_SDP = "a" * hksv3.MAX_SDP_SIZE
LONG_CANDIDATE = IceCandidate("a" * (hksv3.MAX_ICE_CANDIDATE_SIZE - 32), "0", 0)
OVERSIZED_CANDIDATE = IceCandidate("a" * hksv3.MAX_ICE_CANDIDATE_SIZE, "0", 0)
LIMIT_KEYS = [SFRAME] * hksv3.MAX_SFRAME_KEY_CHANGES
LIMIT_KIDS = [5] * hksv3.MAX_SFRAME_KEY_CHANGES


def test_webrtc_size_limits_accepted() -> None:
    """Test writes at the SDP, candidate and key limits decode."""
    answer = WebRTCProvideAnswer(
        SESSION, MAX_SDP, [LIMIT_CANDIDATE] * hksv3.MAX_ICE_CANDIDATES
    )
    assert WebRTCProvideAnswer.decode(answer.encode()) == answer
    assert WebRTCReoffer.decode(WebRTCReoffer(SESSION, MAX_SDP).encode()).sdp_offer
    assert len(LONG_CANDIDATE.encode()) <= hksv3.MAX_ICE_CANDIDATE_SIZE
    assert IceCandidate.decode(LONG_CANDIDATE.encode()) == LONG_CANDIDATE
    update = WebRTCUpdateSession(SESSION, LIMIT_KEYS, LIMIT_KIDS)
    assert WebRTCUpdateSession.decode(update.encode()) == update


@pytest.mark.parametrize(
    ("decode", "request_value"),
    [
        (WebRTCProvideAnswer.decode, WebRTCProvideAnswer(SESSION, MAX_SDP + "a")),
        (
            WebRTCProvideAnswer.decode,
            WebRTCProvideAnswer(
                SESSION, "v=0", [LIMIT_CANDIDATE] * (hksv3.MAX_ICE_CANDIDATES + 1)
            ),
        ),
        (WebRTCReoffer.decode, WebRTCReoffer(SESSION, MAX_SDP + "a")),
        (
            WebRTCProvideAnswer.decode,
            WebRTCProvideAnswer(SESSION, "v=0", [OVERSIZED_CANDIDATE]),
        ),
        (IceCandidate.decode, OVERSIZED_CANDIDATE),
        (
            WebRTCUpdateSession.decode,
            WebRTCUpdateSession(SESSION, [*LIMIT_KEYS, SFRAME]),
        ),
        (
            WebRTCUpdateSession.decode,
            WebRTCUpdateSession(SESSION, [], [*LIMIT_KIDS, 5]),
        ),
    ],
    ids=[
        "answer-sdp",
        "answer-candidates",
        "reoffer-sdp",
        "answer-candidate-size",
        "candidate-size",
        "update-add-keys",
        "update-remove-kids",
    ],
)
def test_webrtc_size_limits_refused(
    decode: Callable[[bytes], object],
    request_value: WebRTCProvideAnswer
    | WebRTCReoffer
    | IceCandidate
    | WebRTCUpdateSession,
) -> None:
    """Test writes above the SDP, candidate or key limits do not decode."""
    with pytest.raises(ValueError, match="at most"):
        decode(request_value.encode())
