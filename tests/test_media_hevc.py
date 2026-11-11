"""Tests for pyhap.media.hevc."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from pyhap.media.hevc import (
    AccessUnit,
    HevcDepacketizer,
    HevcPacketizer,
    is_irap,
    length_prefixed,
    nal_type,
    payload_starts_irap,
    split_aggregation_packet,
    split_length_prefixed,
)

from .media_helpers import blob, scrypted_input


def nal(kind: int, size: int, seed: str = "", layer: int = 0, tid: int = 1) -> bytes:
    """Build a NAL unit of the given type and total size."""
    body = b""
    counter = 0
    while len(body) < size - 2:
        body += hashlib.sha256(f"{seed}{kind}:{counter}".encode()).digest()
        counter += 1
    header = bytes([kind << 1 | layer >> 5, (layer & 0x1F) << 3 | tid])
    return header + body[: size - 2]


VPS, SPS, PPS = nal(32, 24), nal(33, 40), nal(34, 8)
IDR = nal(19, 6000, "idr")
TRAIL = nal(1, 900, "trail")


def feed(
    depacketizer: HevcDepacketizer,
    units: list[list[bytes]],
    sequence: int = 0,
    skip: frozenset[int] = frozenset(),
) -> list[AccessUnit]:
    """Send RTP payload lists (one per access unit) and collect the output."""
    out = []
    for index, payloads in enumerate(units):
        for position, payload in enumerate(payloads):
            if sequence not in skip:
                unit = depacketizer.push(
                    sequence & 0xFFFF,
                    3000 * (index + 1),
                    position == len(payloads) - 1,
                    payload,
                )
                if unit is not None:
                    out.append(unit)
            sequence += 1
    return out


def round_trip(nals: list[bytes], max_payload: int) -> list[list[bytes]]:
    """Packetize and depacketize one access unit."""
    payloads = HevcPacketizer(max_payload).packetize(nals)
    return nals_of(feed(HevcDepacketizer(), [payloads]))


def nals_of(units: list[AccessUnit]) -> list[list[bytes]]:
    """Return the NAL units of each access unit."""
    return [unit.nals for unit in units]


def keyframes_of(units: list[AccessUnit]) -> list[bool]:
    """Return whether each access unit is a keyframe."""
    return [unit.keyframe for unit in units]


def payload_types(nals: list[bytes], max_payload: int) -> list[int]:
    """Return the RTP payload NAL types the packetizer produces."""
    return [nal_type(p) for p in HevcPacketizer(max_payload).packetize(nals)]


def max_payload_size(nals: list[bytes], max_payload: int) -> int:
    """Return the largest RTP payload the packetizer produces."""
    return max(len(p) for p in HevcPacketizer(max_payload).packetize(nals))


def input_description(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe the rebuilt RTP input the way the vector stores it."""
    return [
        {"sequence": seq, "timestamp": ts, "marker": marker, "payload": blob(data)}
        for seq, ts, marker, data in scrypted_input(case)
    ]


@pytest.mark.parametrize(
    "nals",
    [
        [TRAIL],
        [VPS, SPS, PPS, IDR],
        [nal(1, 100, "a"), nal(1, 200, "b"), nal(1, 300, "c")],
        [VPS, SPS, PPS, nal(39, 30), IDR, nal(40, 12)],
        [nal(19, 250_000, "big")],
        [nal(1, 1200, "exact")],
        [nal(1, 1201, "over")],
        [nal(1, 5, "tiny", layer=3, tid=2)],
    ],
    ids=["single", "keyframe", "slices", "sei", "large", "exact", "over", "layer"],
)
def test_round_trip(nals: list[bytes]) -> None:
    """Verify packetize then depacketize restores the access unit."""
    assert round_trip(nals, 1200) == [nals]


def test_packetizer_uses_ap_and_fu() -> None:
    """Verify small NAL units aggregate and large ones fragment."""
    assert payload_types([VPS, SPS, PPS, IDR], 1200) == [48, 49, 49, 49, 49, 49, 49]
    assert max_payload_size([VPS, SPS, PPS, IDR], 1200) == 1200


def test_packetizer_single_nal_not_aggregated() -> None:
    """Verify a lone NAL unit goes out as a single NAL unit packet."""
    assert payload_types([TRAIL], 1200) == [1]


def test_packetizer_ap_header_uses_lowest_layer_and_tid() -> None:
    """Verify the AP header carries the lowest LayerId and TID."""
    payload = HevcPacketizer().packetize(
        [nal(1, 10, layer=2, tid=3), nal(1, 10, layer=1, tid=2)]
    )[0]
    assert payload[:2] == bytes([48 << 1, 1 << 3 | 2])


def test_packetizer_rejects_tiny_budget() -> None:
    """Verify an unusable payload budget is rejected."""
    with pytest.raises(ValueError, match="too small"):
        HevcPacketizer(3)


def test_parameter_sets_inserted_before_irap() -> None:
    """Verify cached VPS/SPS/PPS precede an IRAP that arrived without them."""
    packetizer = HevcPacketizer()
    units = feed(
        HevcDepacketizer(),
        [
            packetizer.packetize([VPS, SPS, PPS, IDR]),
            packetizer.packetize([TRAIL]),
            packetizer.packetize([IDR]),
        ],
    )
    assert nals_of(units) == [[VPS, SPS, PPS, IDR], [TRAIL], [VPS, SPS, PPS, IDR]]
    assert keyframes_of(units) == [True, False, True]


def test_parameter_sets_go_after_an_access_unit_delimiter() -> None:
    """Verify an AUD stays the first NAL unit of an IRAP access unit."""
    packetizer = HevcPacketizer()
    aud = nal(35, 3)
    units = feed(
        HevcDepacketizer(),
        [
            packetizer.packetize([VPS, SPS, PPS, IDR]),
            packetizer.packetize([aud, IDR]),
        ],
    )
    assert units[-1].nals == [aud, VPS, SPS, PPS, IDR]


def test_updated_parameter_sets_are_used() -> None:
    """Verify the latest parameter sets are the ones inserted."""
    packetizer = HevcPacketizer()
    sps2 = nal(33, 44, "new")
    units = feed(
        HevcDepacketizer(),
        [
            packetizer.packetize([VPS, SPS, PPS, IDR]),
            packetizer.packetize([sps2, TRAIL]),
            packetizer.packetize([IDR]),
        ],
    )
    assert units[-1].nals == [VPS, sps2, PPS, IDR]


def test_irap_without_known_parameter_sets_passes() -> None:
    """Verify an IRAP is forwarded even before parameter sets are known."""
    units = feed(HevcDepacketizer(), [HevcPacketizer().packetize([IDR])])
    assert units[0].nals == [IDR]


def test_sequence_gap_drops_only_damaged_unit() -> None:
    """Verify a lost packet drops its access unit and the next one survives."""
    packetizer = HevcPacketizer()
    depacketizer = HevcDepacketizer()
    units = feed(
        depacketizer,
        [packetizer.packetize([IDR]), packetizer.packetize([TRAIL])],
        skip=frozenset({2}),
    )
    assert nals_of(units) == [[TRAIL]]
    assert depacketizer.dropped == 1


def test_sequence_wraps() -> None:
    """Verify sequence numbers wrap at 65535 without a false gap."""
    units = feed(HevcDepacketizer(), [HevcPacketizer().packetize([IDR])], 65533)
    assert units[0].nals == [IDR]


def test_timestamp_change_without_marker_drops_unit() -> None:
    """Verify an access unit cut short by a new timestamp is dropped."""
    depacketizer = HevcDepacketizer()
    assert depacketizer.push(1, 100, False, TRAIL) is None
    unit = depacketizer.push(2, 200, True, TRAIL)
    assert unit == AccessUnit(200, [TRAIL])
    assert depacketizer.dropped == 1


@pytest.mark.parametrize(
    "payloads",
    [
        [bytes([49 << 1, 1, 0x01]) + b"middle", bytes([49 << 1, 1, 0x41]) + b"end"],
        [bytes([49 << 1, 1, 0x81]) + b"start"],
        [bytes([49 << 1, 1, 0x81]) + b"a", bytes([49 << 1, 1, 0x81]) + b"b"],
        [bytes([49 << 1, 1, 0x81]) + b"a", TRAIL],
        [bytes([49 << 1, 1])],
        [bytes([50 << 1, 1]) + b"paci"],
        [b"\x02"],
        [bytes([48 << 1, 1, 0, 9]) + b"short"],
        [bytes([48 << 1, 1, 0])],
    ],
    ids=[
        "fu-no-start",
        "fu-no-end",
        "fu-restart",
        "fu-interrupted",
        "fu-short",
        "paci",
        "short",
        "ap-overrun",
        "ap-truncated-length",
    ],
)
def test_malformed_unit_dropped(payloads: list[bytes]) -> None:
    """Verify malformed RTP payloads drop the access unit."""
    depacketizer = HevcDepacketizer()
    assert feed(depacketizer, [payloads]) == []
    assert depacketizer.dropped == 1


def test_oversized_unit_dropped() -> None:
    """Verify an access unit over the size bound is dropped."""
    huge = bytes([1 << 1, 1]) + bytes(17 * 1024 * 1024)
    depacketizer = HevcDepacketizer()
    assert feed(depacketizer, [HevcPacketizer(60000).packetize([huge])]) == []
    assert depacketizer.dropped == 1


def test_length_prefixed_round_trip() -> None:
    """Verify the hvcC-style layout splits back into NAL units."""
    data = length_prefixed([VPS, SPS, IDR])
    assert data[:4] == len(VPS).to_bytes(4, "big")
    assert split_length_prefixed(data) == [VPS, SPS, IDR]


@pytest.mark.parametrize("data", [b"\x00\x00", b"\x00\x00\x00\x05abc"])
def test_split_length_prefixed_truncated(data: bytes) -> None:
    """Verify truncated length-prefixed data is rejected."""
    with pytest.raises(ValueError, match="Truncated"):
        split_length_prefixed(data)


def test_split_aggregation_packet_empty() -> None:
    """Verify an AP without NAL units is malformed."""
    assert split_aggregation_packet(bytes([48 << 1, 1])) is None


def test_reset_drops_unit_in_progress() -> None:
    """Verify reset forgets a partial unit, the sequence and the sets."""
    packetizer = HevcPacketizer()
    depacketizer = HevcDepacketizer()
    first = packetizer.packetize([VPS, SPS, PPS, IDR])
    feed(depacketizer, [first])
    depacketizer.push(len(first), 6000, False, first[0])
    depacketizer.reset()
    units = feed(depacketizer, [packetizer.packetize([IDR])], sequence=900)
    assert nals_of(units) == [[IDR]]
    assert depacketizer.parameter_sets == {}
    assert depacketizer.dropped == 0


def test_restart_keeps_parameter_sets() -> None:
    """Verify restart forgets a partial unit and the sequence, not the sets."""
    packetizer = HevcPacketizer()
    depacketizer = HevcDepacketizer()
    first = packetizer.packetize([VPS, SPS, PPS, IDR])
    feed(depacketizer, [first])
    depacketizer.push(len(first), 6000, False, first[0])
    depacketizer.restart()
    units = feed(depacketizer, [packetizer.packetize([IDR])], sequence=900)
    assert nals_of(units) == [[VPS, SPS, PPS, IDR]]
    assert depacketizer.dropped == 0


@pytest.mark.parametrize(
    ("payload", "irap"),
    [
        (TRAIL, False),
        (IDR[:50], True),
        (HevcPacketizer().packetize([IDR])[0], True),
        (HevcPacketizer().packetize([IDR])[1], False),
        (HevcPacketizer().packetize([VPS, SPS, PPS, nal(19, 30)])[0], True),
        (HevcPacketizer().packetize([VPS, SPS, PPS])[0], False),
        (b"\x26", False),
    ],
    ids=["trail", "single", "fu-start", "fu-middle", "ap", "ap-no-irap", "short"],
)
def test_payload_starts_irap(payload: bytes, irap: bool) -> None:
    """Verify IRAP detection from the RTP payload header alone."""
    assert payload_starts_irap(payload) is irap


@pytest.mark.parametrize(
    ("kind", "irap"),
    [(1, False), (16, True), (21, True), (23, True), (24, False), (32, False)],
)
def test_is_irap(kind: int, irap: bool) -> None:
    """Verify the IRAP range is BLA_W_LP through RSV_IRAP_VCL23."""
    assert is_irap(nal(kind, 4)) is irap
