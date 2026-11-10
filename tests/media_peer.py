"""A loopback HomeKit controller and RTP source for the WebRTC tests.

Not a test module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import socket
import time
from typing import Any

from pyhap.media import AudioParams, VideoParams, WebRTCSender, rtp, sdp
from pyhap.media.hevc import HevcDepacketizer, HevcPacketizer, split_length_prefixed
from pyhap.media.sframe import (
    SFrameDepacketizer,
    SFrameReceiver,
    SFrameSender,
    SFrameSuite,
    packetize,
)
from pyhap.media.transport import DtlsRole, DtlsSrtpTransport

from .webrtc_helpers import IceCandidate, SFrameKey

KEY = SFrameKey(bytes(range(32)), 0x0123456789ABCDEF)
TALKBACK_KEY = SFrameKey(bytes(range(32, 64)), 77)
VIDEO = VideoParams(width=1280, height=720, fps=30, peak_bitrate_kbps=1500)
VIDEO_PT = sdp.DEFAULT_VIDEO_PAYLOAD_TYPE
AUDIO_PT = sdp.DEFAULT_AUDIO_PAYLOAD_TYPE
CONTROLLER_SSRC = 4242
# Large enough for a 4K IRAP picture; the library's default is for talkback.
CONTROLLER_MAX_FRAME = 8 * 1024 * 1024


def nal(kind: int, size: int, fill: int) -> bytes:
    """Build a NAL unit of a type and size."""
    return bytes([kind << 1, 1]) + bytes([fill]) * (size - 2)


VPS, SPS, PPS = nal(32, 20, 1), nal(33, 30, 2), nal(34, 6, 3)
IDR = nal(19, 40000, 4)
TRAIL = nal(1, 2500, 5)
LEADING_TRAIL = nal(1, 300, 6)
KEYFRAME = [VPS, SPS, PPS, IDR]


@dataclass
class Received:
    """What the controller decrypted, per stream."""

    video: list[list[bytes]] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)
    packets: list[rtp.RtpPacket] = field(default_factory=list)
    arrivals: list[float] = field(default_factory=list)
    rtcp: list[rtp.RtcpMessage] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class Controller:
    """A minimal answering peer standing in for the Home app."""

    def __init__(
        self, setup: str = "active", talkback: bool = False, sframe: bool = True
    ) -> None:
        """Create the controller's transport; ``sframe=False`` reads plain RTP."""
        self.transport = DtlsSrtpTransport(
            ice_controlling=False, host_addresses=["127.0.0.1"], use_ipv6=False
        )
        self.setup = setup
        self.talkback = talkback
        self.received = Received()
        self.offer: sdp.SessionDescription | None = None
        self.audio_pt = AUDIO_PT
        self.sframe = sframe
        self._plain_video = HevcDepacketizer()
        self._depacketizers: dict[int, SFrameDepacketizer] = {}
        self._receivers = {
            SFrameSuite.HOMEKIT_VIDEO: SFrameReceiver(SFrameSuite.HOMEKIT_VIDEO),
            SFrameSuite.HOMEKIT_AUDIO: SFrameReceiver(SFrameSuite.HOMEKIT_AUDIO),
        }
        self.transport.on_rtp = self._on_rtp
        self.transport.on_rtcp = self._on_rtcp

    async def answer(
        self, offer: str, key: SFrameKey = KEY
    ) -> tuple[str, list[IceCandidate]]:
        """Build an answer with the offer's payload types and HAP candidates."""
        self.offer = sdp.parse(offer)
        video_pt = self.offer.media[0].payload_types[0]
        if len(self.offer.media) > 1:
            self.audio_pt = self.offer.media[1].payload_types[0]
        for receiver in self._receivers.values():
            receiver.update_keys([(key.key, key.kid)], [])
        candidates = await self.transport.gather()
        audio_direction = "sendrecv" if self.talkback else "recvonly"
        lines = [
            "v=0",
            "o=- 1 2 IN IP4 127.0.0.1",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE 0 1",
            f"a=ice-ufrag:{self.transport.local_username}",
            f"a=ice-pwd:{self.transport.local_password}",
            f"a=fingerprint:sha-256 {self.transport.certificate.fingerprint}",
            f"a=setup:{self.setup}",
            f"m=video 9 UDP/TLS/RTP/SAVPF {video_pt}",
            "c=IN IP4 0.0.0.0",
            "a=mid:0",
            "a=recvonly",
            "a=rtcp-mux",
            f"a=rtpmap:{video_pt} H265/90000",
            f"m=audio 9 UDP/TLS/RTP/SAVPF {self.audio_pt}",
            "c=IN IP4 0.0.0.0",
            "a=mid:1",
            f"a={audio_direction}",
            f"a=rtpmap:{self.audio_pt} opus/48000/2",
            f"a=ssrc:{CONTROLLER_SSRC} cname:controller",
        ]
        hap_candidates = [IceCandidate(c, "0", 0) for c in candidates]
        return "\r\n".join(lines) + "\r\n", hap_candidates

    async def connect(self) -> None:
        """Connect to the sender described by the offer."""
        assert self.offer is not None
        media = self.offer.transport()
        await self.transport.connect(
            remote_username=media.ice_ufrag or "",
            remote_password=media.ice_pwd or "",
            remote_candidates=self.offer.candidates,
            remote_fingerprints=media.fingerprints,
            role=DtlsRole.CLIENT if self.setup == "active" else DtlsRole.SERVER,
            timeout=5,
        )

    def _on_rtp(self, data: bytes) -> None:
        packet = rtp.parse_rtp(data)
        self.received.packets.append(packet)
        self.received.event.set()
        suite = (
            SFrameSuite.HOMEKIT_AUDIO
            if packet.payload_type == self.audio_pt
            else SFrameSuite.HOMEKIT_VIDEO
        )
        if suite is SFrameSuite.HOMEKIT_VIDEO:
            self.received.arrivals.append(time.monotonic())
        if not self.sframe:
            self._on_plain_rtp(packet, suite)
            return
        depacketizer = self._depacketizers.setdefault(
            packet.ssrc, SFrameDepacketizer(CONTROLLER_MAX_FRAME)
        )
        frame = depacketizer.push(packet.sequence, packet.payload)
        if frame is None:
            return
        plaintext = self._receivers[suite].unprotect(frame, packet.ssrc)
        if suite is SFrameSuite.HOMEKIT_VIDEO:
            self.received.video.append(split_length_prefixed(plaintext))
        else:
            self.received.audio.append(plaintext)
        self.received.event.set()

    def _on_plain_rtp(self, packet: rtp.RtpPacket, suite: SFrameSuite) -> None:
        if suite is SFrameSuite.HOMEKIT_AUDIO:
            self.received.audio.append(packet.payload)
            self.received.event.set()
            return
        unit = self._plain_video.push(
            packet.sequence, packet.timestamp, packet.marker, packet.payload
        )
        if unit is not None:
            self.received.video.append(unit.nals)
            self.received.event.set()

    def _on_rtcp(self, data: bytes) -> None:
        self.received.rtcp += rtp.parse_rtcp(data)
        self.received.event.set()

    async def send_talkback(self, opus: bytes, sequence: int) -> None:
        """Send one SFrame-protected Opus packet to the sender."""
        protected = SFrameSender(
            TALKBACK_KEY.key,
            TALKBACK_KEY.kid,
            CONTROLLER_SSRC,
            SFrameSuite.HOMEKIT_AUDIO,
        )
        protected.counter = sequence
        (payload,) = packetize(protected.protect(opus))
        await self.send_audio_payload(payload, sequence)

    async def send_audio_payload(self, payload: bytes, sequence: int) -> None:
        """Send one raw audio RTP payload to the sender."""
        packet = rtp.RtpPacket(
            self.audio_pt, sequence, 960 * sequence, CONTROLLER_SSRC, False, payload
        )
        await self.transport.send_rtp(rtp.build_rtp(packet))

    async def wait_for(self, predicate: Callable[[Received], bool]) -> None:
        """Wait until the received data satisfies a predicate."""
        while not predicate(self.received):
            self.received.event.clear()
            await asyncio.wait_for(self.received.event.wait(), 3)

    async def close(self) -> None:
        """Close the controller's transport."""
        await self.transport.close()


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


def sender_options(**kwargs: Any) -> dict[str, Any]:
    """Return loopback sender options with overrides."""
    options: dict[str, Any] = {
        "video": VIDEO,
        "audio": AudioParams(),
        "sframe": KEY,
        "host_addresses": ["127.0.0.1"],
        "use_ipv6": False,
    }
    options.update(kwargs)
    return options


def make_sender(**kwargs: Any) -> WebRTCSender:
    """Create a loopback sender."""
    return WebRTCSender(**sender_options(**kwargs))


async def negotiate(sender: Any, controller: Controller) -> None:
    """Run offer, answer and connect on both sides.

    ``sender`` is a :class:`WebRTCSender` or a worker proxy of one.
    """
    offer = await sender.create_offer()
    answer, candidates = await controller.answer(offer)
    await sender.apply_answer(answer, candidates)
    await asyncio.gather(controller.connect(), sender.wait_connected(5))


def video_count(count: int) -> Callable[[Received], bool]:
    """Predicate: at least ``count`` video frames arrived."""
    return lambda received: len(received.video) >= count


def audio_count(count: int) -> Callable[[Received], bool]:
    """Predicate: at least ``count`` audio frames arrived."""
    return lambda received: len(received.audio) >= count


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Poll a predicate on the event loop."""
    while not predicate():
        await asyncio.sleep(0.01)
