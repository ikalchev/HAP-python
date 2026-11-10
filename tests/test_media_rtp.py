"""Tests for pyhap.media.rtp."""

from __future__ import annotations

from operator import attrgetter
import struct

import pytest

from pyhap.media import rtp
from pyhap.media.rtp import RtpPacket

PACKET = RtpPacket(
    payload_type=100,
    sequence=65535,
    timestamp=0xFFFFFFFF,
    ssrc=0x01020304,
    marker=True,
    payload=b"data",
)


def test_rtp_round_trip() -> None:
    """Verify an RTP packet survives build and parse."""
    data = rtp.build_rtp(PACKET)
    assert data[:2] == bytes([0x80, 0x80 | 100])
    assert rtp.parse_rtp(data) == PACKET


def test_rtp_parse_skips_csrc_extension_and_padding() -> None:
    """Verify CSRCs, a header extension and padding are skipped."""
    header = struct.pack(">BBHII", 0xB1, 96, 7, 9000, 5)
    csrc = struct.pack(">I", 1)
    extension = struct.pack(">HHI", 0xBEDE, 1, 0)
    data = header + csrc + extension + b"payload" + b"\x00\x00\x03"
    assert rtp.parse_rtp(data) == RtpPacket(96, 7, 9000, 5, False, b"payload")


@pytest.mark.parametrize(
    "data",
    [
        b"\x80\x60",
        bytes([0x40]) + bytes(11),
        bytes([0x90]) + bytes(11),
        bytes([0xA0]) + bytes(11) + b"\x00",
        bytes([0xA0]) + bytes(11) + b"\x20",
        bytes([0x8F]) + bytes(11),
    ],
    ids=["short", "version", "extension", "zero-padding", "padding", "csrc"],
)
def test_rtp_parse_rejects(data: bytes) -> None:
    """Verify malformed RTP raises ValueError."""
    with pytest.raises(ValueError, match="RTP"):
        rtp.parse_rtp(data)


def test_parse_pli() -> None:
    """Verify a PLI names its media SSRC."""
    (message,) = rtp.parse_rtcp(rtp.build_pli(1, 2))
    assert message.is_keyframe_request
    assert (message.sender_ssrc, message.media_ssrcs) == (1, [2])


def test_parse_fir() -> None:
    """Verify a FIR takes its targets from the FCI."""
    fir = struct.pack(">BBHIIIBxxx", 0x84, 206, 4, 1, 0, 77, 3)
    (message,) = rtp.parse_rtcp(fir)
    assert message.is_keyframe_request
    assert message.media_ssrcs == [77]


def test_parse_nack_with_bitmask() -> None:
    """Verify a NACK expands its lost-packet bitmask across the wrap."""
    nack = rtp.build_nack(1, 2, [65534])[:-2] + struct.pack(">H", 0b101)
    (message,) = rtp.parse_rtcp(nack)
    assert message.is_nack
    assert message.lost == [65534, 65535, 1]


def test_parse_compound_sr_sdes_bye() -> None:
    """Verify a compound SR, SDES and BYE parses part by part."""
    compound = (
        rtp.build_sender_report(9, rtp.ntp_time(0), 1, 2, 3)
        + rtp.build_sdes(9, "cname")
        + rtp.build_bye([9, 10])
    )
    messages = rtp.parse_rtcp(compound)
    assert list(map(attrgetter("packet_type"), messages)) == [200, 202, 203]
    assert messages[2].media_ssrcs == [9, 10]


def test_parse_rtcp_bye_without_sources() -> None:
    """Test a BYE without sources is parsed and does not end the compound."""
    bye = bytes([0x80, rtp.RTCP_BYE, 0, 0])
    pli = rtp.build_pli(1, 2)
    messages = rtp.parse_rtcp(bye + pli)
    assert [m.packet_type for m in messages] == [rtp.RTCP_BYE, rtp.RTCP_PSFB]
    assert messages[0].media_ssrcs == []
    # Any other empty part is malformed.
    assert rtp.parse_rtcp(bytes([0x80, rtp.RTCP_SR, 0, 0]) + pli) == []


def test_parse_rtcp_stops_at_garbage() -> None:
    """Verify parsing stops at a part with a bad version or length."""
    assert rtp.parse_rtcp(rtp.build_pli(1, 2) + b"\x00" * 8) == rtp.parse_rtcp(
        rtp.build_pli(1, 2)
    )
    assert rtp.parse_rtcp(rtp.build_pli(1, 2)[:-1]) == []


@pytest.mark.parametrize(
    ("data", "parsed"),
    [
        (bytes([0x81, 206, 0, 0]) + bytes(8), 0),
        (bytes([0x81, 203, 0, 0]), 0),
        (bytes([0x80, 203, 0, 0, 0x80, 200, 0, 0]), 1),
    ],
    ids=["zero-length-pli", "zero-length-bye-with-count", "bye-then-empty-sr"],
)
def test_parse_rtcp_rejects_zero_length(data: bytes, parsed: int) -> None:
    """Verify a part without room for the sender SSRC stops parsing."""
    assert len(rtp.parse_rtcp(data)) == parsed


@pytest.mark.parametrize(
    ("payload_type", "rtcp"), [(96, False), (63, False), (64, True), (72, True)]
)
def test_rtp_packet_is_rtcp(payload_type: int, rtcp: bool) -> None:
    """Verify RTCP sent to an RTP port is recognised by its payload type."""
    packet = rtp.RtpPacket(payload_type, 1, 2, 3, False, b"")
    assert packet.is_rtcp is rtcp


def test_sender_report_layout() -> None:
    """Verify the SR fields and length."""
    report = rtp.build_sender_report(9, 0x0102030405060708, 7, 8, 9)
    assert len(report) == 28
    assert struct.unpack(">BBHIQIII", report) == (
        0x80,
        200,
        6,
        9,
        0x0102030405060708,
        7,
        8,
        9,
    )


def test_sdes_is_word_aligned() -> None:
    """Verify the SDES chunk is padded to 32 bits."""
    sdes = rtp.build_sdes(9, "abc")
    assert len(sdes) % 4 == 0
    assert struct.unpack_from(">H", sdes, 2)[0] == len(sdes) // 4 - 1


def test_ntp_time() -> None:
    """Verify Unix time maps onto the NTP era."""
    assert rtp.ntp_time(1.5) == (rtp.NTP_EPOCH_OFFSET + 1) << 32 | 1 << 31
