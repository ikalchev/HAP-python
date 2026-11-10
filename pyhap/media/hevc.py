"""H.265 RTP payload handling (RFC 7798) for the camera media senders.

The integrator's ffmpeg sends HEVC as RTP (``-c:v copy -bsf:v dump_extra -f
rtp``). That suits Annex B inputs such as RTSP; an MP4 or other hvcC input
needs ``-bsf:v hevc_mp4toannexb`` instead, which also puts the parameter sets
in front of every IRAP picture. SFrame protects whole frames, so the sender
turns that RTP back into access units, writes each one as 4-byte
length-prefixed NAL units (the hvcC sample layout) and encrypts the result.

Only the RFC 7798 packet types without DONL are handled: single NAL unit
packets, aggregation packets (type 48) and fragmentation units (type 49). The
receiver needs VPS, SPS and PPS in-band in front of every IRAP picture, since
the SDP carries no sprop parameter sets; parameter sets seen earlier are
inserted when an IRAP access unit arrives without them.

The SFrame plaintext matches what Scrypted's HomeKit WebRTC plugin sends for
the same RTP input (https://github.com/koush/scrypted, ``plugins/webrtc``).

:class:`HevcPacketizer` is the reverse direction, for sessions without
SFrame, which send plain RFC 7798 RTP.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
import struct

logger = logging.getLogger(__name__)

NAL_HEADER_SIZE = 2
NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34
NAL_AUD = 35
NAL_AP = 48
NAL_FU = 49
IRAP_FIRST = 16
IRAP_LAST = 23
PARAMETER_SETS = (NAL_VPS, NAL_SPS, NAL_PPS)
MAX_ACCESS_UNIT = 16 * 1024 * 1024


def nal_type(nal: bytes) -> int:
    """Return the NAL unit type of an H.265 NAL unit or RTP payload."""
    return (nal[0] >> 1) & 0x3F


def is_irap(nal: bytes) -> bool:
    """Whether a NAL unit is an IRAP (BLA, IDR or CRA) picture slice."""
    return IRAP_FIRST <= nal_type(nal) <= IRAP_LAST


def payload_starts_irap(payload: bytes) -> bool:
    """Whether an RTP payload starts or carries an IRAP slice.

    A cheap look at the payload header (and the FU header or AP units),
    without reassembling anything.
    """
    if len(payload) < NAL_HEADER_SIZE + 1:
        return False
    kind = nal_type(payload)
    if kind == NAL_FU:
        return bool(payload[2] & 0x80) and IRAP_FIRST <= payload[2] & 0x3F <= IRAP_LAST
    if kind == NAL_AP:
        return any(is_irap(nal) for nal in split_aggregation_packet(payload) or ())
    return is_irap(payload)


def length_prefixed(nals: Iterable[bytes]) -> bytes:
    """Join NAL units with 4-byte big-endian length prefixes."""
    return b"".join(struct.pack(">I", len(nal)) + nal for nal in nals)


def split_length_prefixed(data: bytes) -> list[bytes]:
    """Split 4-byte length-prefixed NAL units."""
    nals = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("Truncated NAL unit length")
        (length,) = struct.unpack_from(">I", data, offset)
        offset += 4
        if offset + length > len(data):
            raise ValueError("Truncated NAL unit")
        nals.append(data[offset : offset + length])
        offset += length
    return nals


def split_aggregation_packet(payload: bytes) -> list[bytes] | None:
    """Return the NAL units of an AP, or ``None`` when it is malformed."""
    nals = []
    offset = NAL_HEADER_SIZE
    while offset < len(payload):
        if offset + 2 > len(payload):
            return None
        (length,) = struct.unpack_from(">H", payload, offset)
        offset += 2
        if length < NAL_HEADER_SIZE or offset + length > len(payload):
            return None
        nals.append(payload[offset : offset + length])
        offset += length
    return nals or None


@dataclass
class AccessUnit:
    """All NAL units of one picture, sharing one RTP timestamp."""

    timestamp: int
    nals: list[bytes] = field(default_factory=list)

    @property
    def keyframe(self) -> bool:
        """Whether the access unit contains an IRAP picture."""
        return any(is_irap(nal) for nal in self.nals)

    @property
    def self_contained(self) -> bool:
        """Whether a decoder can start here: IRAP with VPS, SPS and PPS."""
        kinds = {nal_type(nal) for nal in self.nals}
        return self.keyframe and all(kind in kinds for kind in PARAMETER_SETS)

    def to_bytes(self) -> bytes:
        """Return the 4-byte length-prefixed SFrame plaintext."""
        return length_prefixed(self.nals)


class HevcDepacketizer:
    """Reassembles access units from RFC 7798 RTP payloads.

    An access unit ends with the RTP marker bit. A sequence gap, a broken
    fragmentation unit or an unsupported packet type damages the access unit,
    which is then dropped; the next one starts clean.
    """

    def __init__(self) -> None:
        """Start without cached parameter sets."""
        self.parameter_sets: dict[int, bytes] = {}
        self.dropped = 0
        self._timestamp: int | None = None
        self._last_sequence: int | None = None
        self._nals: list[bytes] = []
        self._size = 0
        self._fragment: list[bytes] | None = None
        self._damaged = False

    def reset(self) -> None:
        """Drop the access unit in progress, for example on a new input SSRC.

        Cached parameter sets go too: a new source sends its own in-band, and
        an old set must not be put in front of its pictures.
        """
        self.parameter_sets = {}
        self.restart()

    def restart(self) -> None:
        """Drop the access unit in progress and forget the last sequence.

        Cached parameter sets stay, for a pause in the same input stream.
        """
        self._last_sequence = None
        self._reset()

    def push(
        self, sequence: int, timestamp: int, marker: bool, payload: bytes
    ) -> AccessUnit | None:
        """Add one RTP payload; return the access unit completed by it."""
        if self._timestamp is not None and timestamp != self._timestamp:
            # A new timestamp without a marker: the previous unit is incomplete.
            self._drop()
        if self._timestamp is None:
            self._timestamp = timestamp
        if (
            self._last_sequence is not None
            and sequence != (self._last_sequence + 1) & 0xFFFF
        ):
            self._damaged = True
        self._last_sequence = sequence
        if not self._damaged:
            self._add_payload(payload)
        if not marker:
            return None
        if self._fragment is not None:
            self._damaged = True
        if self._damaged or not self._nals:
            self._drop()
            return None
        unit = AccessUnit(timestamp, self._complete(self._nals))
        self._reset()
        return unit

    def _add_payload(self, payload: bytes) -> None:
        if len(payload) < NAL_HEADER_SIZE:
            self._damaged = True
            return
        kind = nal_type(payload)
        if kind == NAL_FU:
            self._add_fragment(payload)
            return
        if self._fragment is not None:
            self._damaged = True
            return
        if kind == NAL_AP:
            nals = split_aggregation_packet(payload)
            if nals is None:
                self._damaged = True
                return
            for nal in nals:
                self._add_nal(nal)
            return
        if kind > NAL_AP:
            self._damaged = True
            return
        self._add_nal(payload)

    def _add_fragment(self, payload: bytes) -> None:
        if len(payload) < 3:
            self._damaged = True
            return
        fu_header = payload[2]
        start, end = fu_header & 0x80, fu_header & 0x40
        if start:
            if self._fragment is not None:
                self._damaged = True
                return
            header = bytes([(payload[0] & 0x81) | (fu_header & 0x3F) << 1, payload[1]])
            self._fragment = [header]
        elif self._fragment is None:
            self._damaged = True
            return
        self._fragment.append(payload[3:])
        self._size += len(payload) - 3
        if self._size > MAX_ACCESS_UNIT:
            self._damaged = True
            return
        if end:
            nal = b"".join(self._fragment)
            self._fragment = None
            self._size -= len(nal) - NAL_HEADER_SIZE
            self._add_nal(nal)

    def _add_nal(self, nal: bytes) -> None:
        self._size += 4 + len(nal)
        if self._size > MAX_ACCESS_UNIT:
            # Bound memory when a source never sets the marker bit.
            self._damaged = True
            return
        kind = nal_type(nal)
        if kind in PARAMETER_SETS:
            self.parameter_sets[kind] = nal
        self._nals.append(nal)

    def _complete(self, nals: list[bytes]) -> list[bytes]:
        """Put cached parameter sets in front of an IRAP that lacks them."""
        if not any(is_irap(nal) for nal in nals):
            return nals
        present = {nal_type(nal) for nal in nals}
        missing = [
            self.parameter_sets[kind]
            for kind in PARAMETER_SETS
            if kind not in present and kind in self.parameter_sets
        ]
        # An access unit delimiter stays the first NAL unit (H.265 7.4.2.4.4).
        lead = 1 if nals and nal_type(nals[0]) == NAL_AUD else 0
        return nals[:lead] + missing + nals[lead:]

    def _drop(self) -> None:
        if self._nals or self._fragment is not None or self._damaged:
            self.dropped += 1
        self._reset()

    def _reset(self) -> None:
        self._timestamp = None
        self._nals = []
        self._size = 0
        self._fragment = None
        self._damaged = False


def fragment(nal: bytes, max_payload: int) -> list[bytes]:
    """Split one NAL unit into RFC 7798 fragmentation units."""
    kind = nal_type(nal)
    header = bytes([(nal[0] & 0x81) | NAL_FU << 1, nal[1]])
    chunk = max_payload - 3
    body = nal[NAL_HEADER_SIZE:]
    payloads = []
    for offset in range(0, len(body), chunk):
        flags = (0x80 if offset == 0 else 0) | (
            0x40 if offset + chunk >= len(body) else 0
        )
        payloads.append(header + bytes([flags | kind]) + body[offset : offset + chunk])
    return payloads


class HevcPacketizer:
    """Packetizes access units into RFC 7798 RTP payloads (no DONL)."""

    def __init__(self, max_payload: int = 1200) -> None:
        """Set the RTP payload budget."""
        if max_payload < 4:
            raise ValueError("Payload budget too small for H.265")
        self.max_payload = max_payload

    def packetize(self, nals: Iterable[bytes]) -> list[bytes]:
        """Return the RTP payloads of one access unit, in order."""
        payloads: list[bytes] = []
        pending: list[bytes] = []
        for nal in nals:
            if len(nal) > self.max_payload:
                payloads += self._aggregate(pending)
                pending = []
                payloads += self._fragment(nal)
                continue
            if self._ap_size([*pending, nal]) > self.max_payload:
                payloads += self._aggregate(pending)
                pending = []
            pending.append(nal)
        payloads += self._aggregate(pending)
        return payloads

    @staticmethod
    def _ap_size(nals: list[bytes]) -> int:
        return NAL_HEADER_SIZE + sum(2 + len(nal) for nal in nals)

    @staticmethod
    def _aggregate(nals: list[bytes]) -> list[bytes]:
        if len(nals) <= 1:
            return list(nals)
        # The AP header carries the lowest LayerId and TID (RFC 7798 4.4.2).
        layer = min(((nal[0] & 1) << 5) | (nal[1] >> 3) for nal in nals)
        tid = min(nal[1] & 7 for nal in nals)
        header = bytes([NAL_AP << 1 | layer >> 5, (layer & 0x1F) << 3 | tid])
        return [header + b"".join(struct.pack(">H", len(n)) + n for n in nals)]

    def _fragment(self, nal: bytes) -> list[bytes]:
        return fragment(nal, self.max_payload)
