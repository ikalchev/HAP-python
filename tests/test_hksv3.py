"""Tests for pyhap.hksv3."""

import random
from uuid import UUID

import pytest

from pyhap import hksv3
from pyhap.hksv3 import (
    CameraCapabilities,
    CameraZones,
    SensorConfiguration,
    SensorIntent,
    SensorType,
    VideoQuality,
    VideoStreamCapability,
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
