"""An RTP source playing a camera for the media tests.

Not a test module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import socket

from pyhap.media import rtp
from pyhap.media.hevc import HevcPacketizer


def nal(kind: int, size: int, fill: int) -> bytes:
    """Build a NAL unit of a type and size."""
    return bytes([kind << 1, 1]) + bytes([fill]) * (size - 2)


VPS, SPS, PPS = nal(32, 20, 1), nal(33, 30, 2), nal(34, 6, 3)
IDR = nal(19, 40000, 4)
TRAIL = nal(1, 2500, 5)
LEADING_TRAIL = nal(1, 300, 6)
KEYFRAME = [VPS, SPS, PPS, IDR]


class Source:
    """Plays the integrator's ffmpeg: plain RTP to the input ports."""

    def __init__(self, ports: tuple[str, int, int], ssrc: int = 0xAAAA) -> None:
        """Open a UDP socket towards the sender's input."""
        self.host, self.video_port, self.audio_port = ports
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssrc = ssrc
        self.sequence = 5000
        self.timestamp = 1000
        self.audio_sequence = 100

    def send_video(self, nals: list[bytes]) -> None:
        """Send one access unit as RFC 7798 RTP."""
        payloads = HevcPacketizer(1400).packetize(nals)
        for index, payload in enumerate(payloads):
            packet = rtp.RtpPacket(
                96,
                self.sequence,
                self.timestamp,
                self.ssrc,
                index == len(payloads) - 1,
                payload,
            )
            self.send_raw(rtp.build_rtp(packet))
            self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp += 3000

    def send_raw(self, data: bytes) -> None:
        """Send a datagram to the video input."""
        self.sock.sendto(data, (self.host, self.video_port))

    def send_audio(self, opus: bytes) -> None:
        """Send one Opus RTP packet."""
        packet = rtp.RtpPacket(
            97, self.audio_sequence, 960 * self.audio_sequence, 0xBBBB, False, opus
        )
        self.sock.sendto(rtp.build_rtp(packet), (self.host, self.audio_port))
        self.audio_sequence += 1

    def close(self) -> None:
        """Close the socket."""
        self.sock.close()


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Poll a predicate on the event loop."""
    while not predicate():
        await asyncio.sleep(0.01)
