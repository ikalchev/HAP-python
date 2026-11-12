"""A minimal MPEG-TS muxer for H.265 access units and Opus packets.

It feeds a recording transcoder: ffmpeg reads the stream from a pipe
(``-f mpegts -i pipe:0``) with the timestamps of the media source, so the
buffered seconds before a recording go through ffmpeg faster than real time.
A pipe also gives back-pressure and cannot drop data, unlike RTP over UDP.

One program carries H.265 (stream type 0x24) and, optionally, Opus as
private data with the ``Opus`` registration descriptor and the channel
configuration extension descriptor (ETSI TS 102 366 annex, as ffmpeg muxes
it). Each Opus packet gets the Opus control header. Timestamps are 90 kHz
PTS; video PES packets carry no DTS, the camera streams have no B-frames.
"""

from __future__ import annotations

from collections.abc import Iterable

PACKET_SIZE = 188
PAYLOAD_SIZE = PACKET_SIZE - 4
PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
AUDIO_PID = 0x0101
PROGRAM_NUMBER = 1
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_PRIVATE = 0x06
STREAM_ID_VIDEO = 0xE0
STREAM_ID_PRIVATE_1 = 0xBD
START_CODE = b"\x00\x00\x00\x01"
NAL_AUD = 35
# NAL unit types below this are VCL NAL units.
NAL_FIRST_NON_VCL = 32
# The payload of an HEVC access unit delimiter: pic_type 2 (any), stop bit.
AUD_PAYLOAD = 0x50
# PCR runs this far behind the PTS of the video it is sent with.
PCR_DELAY = 9000
PTS_MASK = (1 << 33) - 1


def _aud(nals: list[bytes]) -> bytes:
    """An access unit delimiter with the TemporalId of the unit's pictures."""
    tid_plus1 = next(
        (
            nal[1] & 0x07
            for nal in nals
            if len(nal) > 1 and (nal[0] >> 1) & 0x3F < NAL_FIRST_NON_VCL
        ),
        1,
    )
    return bytes([NAL_AUD << 1, tid_plus1 or 1, AUD_PAYLOAD])


def _crc_table() -> list[int]:
    table: list[int] = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC_TABLE: list[int] = _crc_table()


def crc32_mpeg2(data: bytes) -> int:
    """The CRC-32/MPEG-2 of a PSI section."""
    crc = 0xFFFFFFFF
    for byte in data:
        entry: int = _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
        crc = ((crc << 8) & 0xFFFFFFFF) ^ entry
    return crc


def encode_pts(pts: int, marker: int = 0x2) -> bytes:
    """The 5-byte PTS field of a PES header."""
    pts &= PTS_MASK
    return bytes(
        [
            (marker << 4) | ((pts >> 29) & 0x0E) | 0x01,
            (pts >> 22) & 0xFF,
            ((pts >> 14) & 0xFE) | 0x01,
            (pts >> 7) & 0xFF,
            ((pts << 1) & 0xFE) | 0x01,
        ]
    )


def decode_pts(field: bytes) -> int:
    """The PTS of a 5-byte PES header field."""
    return (
        ((field[0] >> 1) & 0x07) << 30
        | field[1] << 22
        | (field[2] >> 1) << 15
        | field[3] << 7
        | field[4] >> 1
    )


def opus_control_header(size: int) -> bytes:
    """The Opus control header that precedes each packet in MPEG-TS."""
    return b"\x7f\xe0" + b"\xff" * (size // 255) + bytes([size % 255])


class MpegTsMuxer:
    """Turns access units and Opus packets into 188-byte TS packets."""

    def __init__(self, audio: bool = True, channels: int = 1) -> None:
        """Configure the program; :meth:`video` sends the tables first."""
        self.audio = audio
        self.channels = channels
        self._continuity: dict[int, int] = {}

    def tables(self) -> bytes:
        """The PAT and PMT; sent before every IRAP picture."""
        return self._section(PAT_PID, self._pat()) + self._section(PMT_PID, self._pmt())

    def video(self, nals: Iterable[bytes], pts: int, keyframe: bool) -> bytes:
        """One access unit as a PES packet in Annex B, with tables at IRAPs.

        Starts with an access unit delimiter, which HEVC in MPEG-TS requires
        (ISO/IEC 13818-1 2.17): the unit's own one moved to the front, else a
        new one.
        """
        nals = list(nals)
        auds = [nal for nal in nals if (nal[0] >> 1) & 0x3F == NAL_AUD]
        if auds:
            nals = [auds[0], *(nal for nal in nals if nal not in auds)]
        else:
            nals.insert(0, _aud(nals))
        payload = b"".join(START_CODE + nal for nal in nals)
        pes = self._pes(STREAM_ID_VIDEO, payload, pts, bounded=False, aligned=True)
        pcr = max(pts - PCR_DELAY, 0)
        packets = self._packets(VIDEO_PID, pes, pcr=pcr, random_access=keyframe)
        return self.tables() + packets if keyframe else packets

    def opus(self, packet: bytes, pts: int) -> bytes:
        """One Opus packet as a PES packet; empty without audio."""
        if not self.audio:
            return b""
        payload = opus_control_header(len(packet)) + packet
        pes = self._pes(STREAM_ID_PRIVATE_1, payload, pts, bounded=True)
        return self._packets(AUDIO_PID, pes)

    def _pat(self) -> bytes:
        body = PROGRAM_NUMBER.to_bytes(2, "big") + (0xE000 | PMT_PID).to_bytes(2, "big")
        return self._psi(0x00, 1, body)

    def _pmt(self) -> bytes:
        streams = bytes([STREAM_TYPE_HEVC]) + self._es(VIDEO_PID, b"")
        if self.audio:
            descriptors = (
                b"\x05\x04Opus"  # registration descriptor
                + bytes([0x7F, 0x02, 0x80, self.channels])  # Opus channel config
            )
            streams += bytes([STREAM_TYPE_PRIVATE]) + self._es(AUDIO_PID, descriptors)
        body = (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00" + streams
        return self._psi(0x02, PROGRAM_NUMBER, body)

    @staticmethod
    def _es(pid: int, descriptors: bytes) -> bytes:
        return (
            (0xE000 | pid).to_bytes(2, "big")
            + (0xF000 | len(descriptors)).to_bytes(2, "big")
            + descriptors
        )

    @staticmethod
    def _psi(table_id: int, table_id_extension: int, body: bytes) -> bytes:
        # Section syntax, version 0, current, section 0 of 0, then the CRC.
        length = 5 + len(body) + 4
        section = (
            bytes([table_id])
            + (0xB000 | length).to_bytes(2, "big")
            + table_id_extension.to_bytes(2, "big")
            + b"\xc1\x00\x00"
            + body
        )
        return section + crc32_mpeg2(section).to_bytes(4, "big")

    def _section(self, pid: int, section: bytes) -> bytes:
        # A pointer field, then the section; one packet holds our tables.
        payload = b"\x00" + section
        payload += b"\xff" * (PAYLOAD_SIZE - len(payload))
        return self._header(pid, start=True, adaptation=False) + payload

    @staticmethod
    def _pes(
        stream_id: int,
        payload: bytes,
        pts: int,
        *,
        bounded: bool,
        aligned: bool = False,
    ) -> bytes:
        # '10', no scrambling or priority, data_alignment_indicator, PTS only.
        flags = b"\x84" if aligned else b"\x80"
        header = flags + b"\x80\x05" + encode_pts(pts)
        length = len(header) + len(payload)
        # Video may exceed 16 bits, so its length is left unbounded (0).
        size = length if bounded and length <= 0xFFFF else 0
        return (
            b"\x00\x00\x01"
            + bytes([stream_id])
            + size.to_bytes(2, "big")
            + (header + payload)
        )

    def _packets(
        self,
        pid: int,
        pes: bytes,
        *,
        pcr: int | None = None,
        random_access: bool = False,
    ) -> bytes:
        out = bytearray()
        offset = 0
        first = True
        while offset < len(pes) or first:
            adaptation = b""
            if first and (pcr is not None or random_access):
                flags = (0x40 if random_access else 0) | (
                    0x10 if pcr is not None else 0
                )
                field = bytes([flags])
                if pcr is not None:
                    field += self._pcr(pcr)
                adaptation = field
            room = PAYLOAD_SIZE - (len(adaptation) + 1 if adaptation else 0)
            chunk = pes[offset : offset + room]
            offset += len(chunk)
            if len(chunk) < room:
                # Stuff the last packet through its adaptation field.
                if adaptation:
                    adaptation += b"\xff" * (room - len(chunk))
                else:
                    # The length byte alone fills one byte, a flags byte and
                    # 0xFF stuffing the rest.
                    stuffing = PAYLOAD_SIZE - len(chunk) - 1
                    adaptation = b"\x00" + b"\xff" * (stuffing - 1) if stuffing else b""
                out += self._header(pid, start=first, adaptation=True)
                out += bytes([len(adaptation)]) + adaptation + chunk
            elif adaptation:
                out += self._header(pid, start=first, adaptation=True)
                out += bytes([len(adaptation)]) + adaptation + chunk
            else:
                out += self._header(pid, start=first, adaptation=False) + chunk
            first = False
        return bytes(out)

    @staticmethod
    def _pcr(pcr: int) -> bytes:
        base = pcr & PTS_MASK
        return (base << 15 | 0x3F << 9).to_bytes(6, "big")

    def _header(self, pid: int, *, start: bool, adaptation: bool) -> bytes:
        counter = self._continuity.get(pid, -1) + 1 & 0x0F
        self._continuity[pid] = counter
        control = (0x30 if adaptation else 0x10) | counter
        return bytes(
            [0x47, (0x40 if start else 0) | (pid >> 8) & 0x1F, pid & 0xFF, control]
        )
