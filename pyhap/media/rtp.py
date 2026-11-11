"""Minimal RTP and RTCP handling for the camera media senders (RFC 3550, RFC 4585)."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
import secrets
import struct
import time

# RTP clock rates of H.265 video and Opus audio (RFC 7798, RFC 7587).
VIDEO_CLOCK_RATE = 90000
AUDIO_CLOCK_RATE = 48000
# Dynamic payload types the HomeKit streams use for H.265 and Opus.
DEFAULT_VIDEO_PAYLOAD_TYPE = 99
DEFAULT_AUDIO_PAYLOAD_TYPE = 110

# Shortest interval between keyframe requests to a source.
KEYFRAME_REQUEST_INTERVAL = 1.0
# An audio gap this long starts a new talkspurt (RTP marker bit).
AUDIO_TALKSPURT_GAP = 1.0
# Sent packets kept for NACK retransmission.
RETRANSMIT_HISTORY = 1024


RTP_VERSION = 2
RTP_HEADER = 12
RTCP_SR = 200
RTCP_RR = 201
RTCP_SDES = 202
RTCP_BYE = 203
RTCP_RTPFB = 205
RTCP_PSFB = 206
FMT_NACK = 1
FMT_PLI = 1
FMT_FIR = 4
NTP_EPOCH_OFFSET = 2208988800
# Payload types 64-95 collide with RTCP packet types (RFC 5761 section 4).
RTCP_PAYLOAD_TYPES = range(64, 96)


@dataclass
class RtpPacket:
    """A parsed RTP packet; header extensions and CSRCs are dropped."""

    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes

    @property
    def is_rtcp(self) -> bool:
        """Whether this "RTP" packet is really RTCP sent to an RTP port."""
        return self.payload_type in RTCP_PAYLOAD_TYPES


def parse_rtp(packet: bytes) -> RtpPacket:
    """Parse an RTP packet, skipping CSRCs, extension and padding."""
    if len(packet) < RTP_HEADER or packet[0] >> 6 != RTP_VERSION:
        raise ValueError("Not an RTP packet")
    first, second, sequence, timestamp, ssrc = struct.unpack_from(">BBHII", packet)
    offset = RTP_HEADER + 4 * (first & 0x0F)
    if first & 0x10:
        if offset + 4 > len(packet):
            raise ValueError("Truncated RTP header extension")
        (words,) = struct.unpack_from(">H", packet, offset + 2)
        offset += 4 + 4 * words
    end = len(packet)
    if first & 0x20:
        if end == 0 or packet[-1] == 0:
            raise ValueError("Invalid RTP padding")
        end -= packet[-1]
    if offset > end:
        raise ValueError("Truncated RTP packet")
    return RtpPacket(
        payload_type=second & 0x7F,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        marker=bool(second & 0x80),
        payload=packet[offset:end],
    )


def build_rtp(packet: RtpPacket) -> bytes:
    """Serialize an RTP packet without CSRCs, extension or padding."""
    second = (0x80 if packet.marker else 0) | packet.payload_type
    header = struct.pack(
        ">BBHII",
        RTP_VERSION << 6,
        second,
        packet.sequence & 0xFFFF,
        packet.timestamp & 0xFFFFFFFF,
        packet.ssrc,
    )
    return header + packet.payload


@dataclass
class RtcpMessage:
    """The RTCP fields the sender acts on."""

    packet_type: int
    fmt: int
    sender_ssrc: int
    media_ssrcs: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)

    @property
    def is_keyframe_request(self) -> bool:
        """Whether this is a PLI or FIR."""
        return self.packet_type == RTCP_PSFB and self.fmt in (FMT_PLI, FMT_FIR)

    @property
    def is_nack(self) -> bool:
        """Whether this is a generic NACK."""
        return self.packet_type == RTCP_RTPFB and self.fmt == FMT_NACK


def parse_rtcp(data: bytes) -> list[RtcpMessage]:
    """Parse a compound RTCP packet; stops at the first malformed part.

    Every part must be version 2, fit in ``data`` and carry at least the
    sender SSRC (RFC 3550 section 6.4), except a BYE without sources, whose
    ``sender_ssrc`` is 0.
    """
    messages = []
    offset = 0
    while offset + 4 <= len(data):
        first, packet_type, length = struct.unpack_from(">BBH", data, offset)
        end = offset + 4 * (length + 1)
        if first >> 6 != RTP_VERSION or end > len(data):
            break
        if length == 0:
            # Only a BYE without sources is just a header (RFC 3550 6.6).
            if packet_type != RTCP_BYE or first & 0x1F:
                break
            messages.append(RtcpMessage(packet_type, 0, 0))
        else:
            body = data[offset + 4 : end]
            messages.append(_parse_one(first & 0x1F, packet_type, body))
        offset = end
    return messages


def _parse_one(fmt: int, packet_type: int, body: bytes) -> RtcpMessage:
    (sender,) = struct.unpack_from(">I", body)
    message = RtcpMessage(packet_type, fmt, sender)
    if packet_type == RTCP_BYE:
        message.media_ssrcs = list(
            struct.unpack_from(f">{min(fmt, len(body) // 4)}I", body)
        )
    elif packet_type in (RTCP_RTPFB, RTCP_PSFB) and len(body) >= 8:
        (media,) = struct.unpack_from(">I", body, 4)
        fci = body[8:]
        if packet_type == RTCP_PSFB and fmt == FMT_FIR:
            # FIR names its targets in the FCI (RFC 5104 section 4.3.1).
            message.media_ssrcs = [
                struct.unpack_from(">I", fci, i)[0] for i in range(0, len(fci) - 7, 8)
            ]
        else:
            message.media_ssrcs = [media]
        if message.is_nack:
            message.lost = _nack_sequences(fci)
    return message


def _nack_sequences(fci: bytes) -> list[int]:
    lost: list[int] = []
    for i in range(0, len(fci) - 3, 4):
        pid: int
        blp: int
        pid, blp = struct.unpack_from(">HH", fci, i)
        lost.append(pid)
        lost += [(pid + bit + 1) & 0xFFFF for bit in range(16) if blp >> bit & 1]
    return lost


def ntp_time(now: float | None = None) -> int:
    """Return a 64-bit NTP timestamp for a Unix time."""
    now = time.time() if now is None else now
    seconds = int(now) + NTP_EPOCH_OFFSET
    fraction = int((now % 1) * (1 << 32))
    return (seconds & 0xFFFFFFFF) << 32 | fraction


def build_sender_report(
    ssrc: int, ntp: int, rtp_timestamp: int, packets: int, octets: int
) -> bytes:
    """Build an RTCP SR without report blocks."""
    return struct.pack(
        ">BBHIQIII",
        RTP_VERSION << 6,
        RTCP_SR,
        6,
        ssrc,
        ntp,
        rtp_timestamp & 0xFFFFFFFF,
        packets & 0xFFFFFFFF,
        octets & 0xFFFFFFFF,
    )


def build_receiver_report(ssrc: int) -> bytes:
    """Build an RTCP RR without report blocks."""
    return struct.pack(">BBHI", RTP_VERSION << 6, RTCP_RR, 1, ssrc)


def build_sdes(ssrc: int, cname: str) -> bytes:
    """Build an RTCP SDES with one CNAME chunk."""
    name = cname.encode()[:255]
    chunk = struct.pack(">IBB", ssrc, 1, len(name)) + name + b"\x00"
    chunk += bytes(-len(chunk) % 4)
    header = struct.pack(">BBH", RTP_VERSION << 6 | 1, RTCP_SDES, len(chunk) // 4)
    return header + chunk


def build_bye(ssrcs: Iterable[int]) -> bytes:
    """Build an RTCP BYE for the given sources."""
    sources = list(ssrcs)
    header = struct.pack(
        ">BBH", RTP_VERSION << 6 | len(sources), RTCP_BYE, len(sources)
    )
    return header + b"".join(struct.pack(">I", ssrc) for ssrc in sources)


def build_pli(sender_ssrc: int, media_ssrc: int) -> bytes:
    """Build an RTCP PLI."""
    return struct.pack(
        ">BBHII", RTP_VERSION << 6 | FMT_PLI, RTCP_PSFB, 2, sender_ssrc, media_ssrc
    )


def build_nack(sender_ssrc: int, media_ssrc: int, lost: Iterable[int]) -> bytes:
    """Build an RTCP generic NACK with one FCI per lost packet."""
    fci = b"".join(struct.pack(">HH", seq & 0xFFFF, 0) for seq in lost)
    header = struct.pack(
        ">BBHII",
        RTP_VERSION << 6 | FMT_NACK,
        RTCP_RTPFB,
        2 + len(fci) // 4,
        sender_ssrc,
        media_ssrc,
    )
    return header + fci


class OutboundStream:
    """SSRC, sequence and timestamp rewriting plus SR and NACK state."""

    def __init__(self, payload_type: int, clock_rate: int) -> None:
        self.ssrc = secrets.randbits(32)
        self.payload_type = payload_type
        self.clock_rate = clock_rate
        self.sequence = secrets.randbits(16)
        self.packets = 0
        self.octets = 0
        self._out_base = secrets.randbits(32)
        self._in_base: int | None = None
        self._in_ssrc: int | None = None
        self._last_out: int | None = None
        self._last_wall = 0.0
        self.history: OrderedDict[int, bytes] = OrderedDict()

    def timestamp(self, ssrc: int, timestamp: int) -> int:
        """Map an input timestamp onto this stream's timeline."""
        now = time.monotonic()
        if ssrc != self._in_ssrc or self._in_base is None:
            # A new or restarted source continues where the old one ended.
            if self._last_out is not None:
                elapsed = max(1, round((now - self._last_wall) * self.clock_rate))
                self._out_base = self._last_out + elapsed
            self._in_ssrc = ssrc
            self._in_base = timestamp
        out = (timestamp - self._in_base + self._out_base) & 0xFFFFFFFF
        self._last_out = out
        self._last_wall = now
        return out

    def build(self, timestamp: int, marker: bool, payload: bytes) -> bytes:
        """Build the next outgoing RTP packet and remember it for NACK."""
        packet = build_rtp(
            RtpPacket(
                self.payload_type, self.sequence, timestamp, self.ssrc, marker, payload
            )
        )
        self.history[self.sequence] = packet
        if len(self.history) > RETRANSMIT_HISTORY:
            self.history.popitem(last=False)
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.packets += 1
        self.octets += len(payload)
        return packet

    def sender_report(self) -> bytes | None:
        """Build an SR, extrapolating the RTP time to now."""
        if self._last_out is None:
            return None
        elapsed = time.monotonic() - self._last_wall
        rtp_now = self._last_out + round(elapsed * self.clock_rate)
        return build_sender_report(
            self.ssrc, ntp_time(), rtp_now, self.packets, self.octets
        )
