"""The RTP input of media sessions: one media source shared by many sessions.

A :class:`MediaSource` binds two loopback UDP ports. The integrator points one
media pull of a camera channel at them (for example ffmpeg with ``-c:v copy
-bsf:v dump_extra -f rtp`` for H.265 and ``-c:a libopus -ac 1 -frame_duration
20 -f rtp`` for Opus). Any number of sessions attach to the source: the H.265
access units are reassembled once per source, SFrame, RTP rewriting and SRTP
then run per session.

Input is only reassembled while an attached session is connected; until then
the source only notes when the last IRAP picture started. RTCP that ffmpeg
sends to an RTP port is dropped, and a new input SSRC (a restarted source)
starts a fresh access unit.

The source measures what its input carries: the video bit rate
(:attr:`MediaSource.video_bitrate`, which the sender's pacer follows) and the
interval between IRAP pictures (:attr:`MediaSource.gop`). Each port takes
datagrams from one sender address at a time; another sender is only accepted
once the first one was silent for :data:`INPUT_PEER_TIMEOUT` seconds, as a
restarted media pull is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import socket
import time
from typing import Protocol

from . import rtp
from .hevc import AccessUnit, HevcDepacketizer, payload_starts_irap
from .sdp import VIDEO_CLOCK_RATE

logger = logging.getLogger(__name__)

INPUT_SOCKET_BUFFER = 4 * 1024 * 1024
INPUT_PEER_TIMEOUT = 1.0
# The bit rate is measured over windows of this many seconds and smoothed.
RATE_WINDOW = 1.0
RATE_SMOOTHING = 0.5
GOP_SMOOTHING = 0.25
# IRAP intervals above this are not taken as the GOP of the input.
MAX_GOP = 60.0


class MediaConsumer(Protocol):
    """What a source needs from an attached session."""

    @property
    def wants_media(self) -> bool:
        """Whether the session sends media now, so input must be reassembled."""

    def on_source_video(self, unit: AccessUnit, ssrc: int) -> None:
        """A complete access unit of the input SSRC."""

    def on_source_audio(self, packet: rtp.RtpPacket) -> None:
        """One Opus RTP packet."""

    def on_source_closed(self, reason: str) -> None:
        """The source ended; the session should end too."""


class _InputProtocol(asyncio.DatagramProtocol):
    """Hands datagrams of one sender address at a time to the callback."""

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback
        self._peer: tuple[str | object, ...] | None = None
        self._last = 0.0

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        now = time.monotonic()
        if addr != self._peer:
            if self._peer is not None and now - self._last < INPUT_PEER_TIMEOUT:
                return
            logger.debug("Media input from %s", addr)
            self._peer = addr
        self._last = now
        try:
            self._callback(data)
        except Exception:
            logger.exception("Error handling media input")


def _input_socket(host: str) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        # A keyframe arrives as a burst; the default buffer drops part of it.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, INPUT_SOCKET_BUFFER)
        logger.debug(
            "Media input buffer: %d bytes (asked for %d)",
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            INPUT_SOCKET_BUFFER,
        )
        sock.bind((host, 0))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class MediaSource:
    """Plain RTP input on two UDP ports, fanned out to attached sessions."""

    def __init__(self, host: str = "127.0.0.1", audio: bool = True) -> None:
        """Configure the source; :meth:`start` binds the ports."""
        self.host = host
        self.audio = audio
        self.on_closed: Callable[[str], None] | None = None
        self.last_video_input = 0.0
        self.video_bitrate = 0.0
        self.gop: float | None = None
        self._last_keyframe: float | None = None
        self._keyframe_timestamp: int | None = None
        self._rate_start = 0.0
        self._rate_bytes = 0
        self._consumers: list[MediaConsumer] = []
        self._depacketizer = HevcDepacketizer()
        self._video_ssrc: int | None = None
        self._active = False
        self._sockets: list[asyncio.DatagramTransport] = []
        self._ports = (0, 0)
        self._started = False
        self._closed = False

    @property
    def rtp_input(self) -> tuple[str, int, int]:
        """``(host, video_port, audio_port)``; the audio port is 0 without audio."""
        return self.host, self._ports[0], self._ports[1]

    @property
    def has_audio(self) -> bool:
        """Whether the source takes audio."""
        return self._ports[1] != 0

    @property
    def closed(self) -> bool:
        """Whether the source was closed."""
        return self._closed

    @property
    def session_count(self) -> int:
        """The number of attached sessions."""
        return len(self._consumers)

    def _measure(self, packet: rtp.RtpPacket, now: float) -> None:
        """Update the input bit rate and the IRAP interval."""
        self._rate_bytes += len(packet.payload)
        elapsed = now - self._rate_start
        if elapsed >= RATE_WINDOW:
            rate = self._rate_bytes * 8 / elapsed if self._rate_start else 0.0
            self.video_bitrate = (
                rate
                if not self.video_bitrate
                else self.video_bitrate + RATE_SMOOTHING * (rate - self.video_bitrate)
            )
            self._rate_start, self._rate_bytes = now, 0
        if not payload_starts_irap(packet.payload):
            return
        self._last_keyframe = now
        previous, self._keyframe_timestamp = self._keyframe_timestamp, packet.timestamp
        if previous is None or previous == packet.timestamp:
            return
        interval = ((packet.timestamp - previous) & 0xFFFFFFFF) / VIDEO_CLOCK_RATE
        if interval > MAX_GOP:
            return
        self.gop = (
            interval
            if self.gop is None
            else self.gop + GOP_SMOOTHING * (interval - self.gop)
        )

    def keyframe_age(self) -> float | None:
        """Seconds since the last IRAP picture started, None if none yet."""
        if self._last_keyframe is None:
            return None
        return time.monotonic() - self._last_keyframe

    async def start(self) -> tuple[str, int, int]:
        """Bind the input ports and return :attr:`rtp_input`."""
        if self._closed:
            raise RuntimeError("Media source is closed")
        if self._started:
            return self.rtp_input
        self._started = True
        try:
            video = await self._bind(self._on_video_input)
            audio = await self._bind(self._on_audio_input) if self.audio else 0
        except BaseException:
            self._close_sockets()
            raise
        if self._closed:
            self._close_sockets()
            raise RuntimeError("Media source is closed")
        self._ports = (video, audio)
        return self.rtp_input

    async def _bind(self, callback: Callable[[bytes], None]) -> int:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _InputProtocol(callback), sock=_input_socket(self.host)
        )
        self._sockets.append(transport)
        port: int = transport.get_extra_info("sockname")[1]
        return port

    def attach(self, consumer: MediaConsumer) -> None:
        """Start delivering media to a session."""
        if self._closed:
            raise RuntimeError("Media source is closed")
        if consumer not in self._consumers:
            self._consumers.append(consumer)

    def detach(self, consumer: MediaConsumer) -> None:
        """Stop delivering media to a session."""
        with contextlib.suppress(ValueError):
            self._consumers.remove(consumer)

    def close(self, reason: str = "closed") -> None:
        """Release the ports and end every attached session with ``reason``."""
        if self._closed:
            return
        self._closed = True
        self._close_sockets()
        consumers, self._consumers = self._consumers, []
        for consumer in consumers:
            try:
                consumer.on_source_closed(reason)
            except Exception:
                logger.exception("Error ending a session of a media source")
        if self.on_closed:
            try:
                self.on_closed(reason)
            except Exception:
                logger.exception("Error in media source close callback")

    def _close_sockets(self) -> None:
        for sock in self._sockets:
            sock.close()
        self._sockets.clear()

    def _receivers(self) -> list[MediaConsumer]:
        return [c for c in self._consumers if c.wants_media]

    def _on_video_input(self, data: bytes) -> None:
        try:
            packet = rtp.parse_rtp(data)
        except ValueError:
            return
        if packet.is_rtcp:
            return
        now = time.monotonic()
        self.last_video_input = now
        if packet.ssrc != self._video_ssrc:
            self._video_ssrc = packet.ssrc
            self._keyframe_timestamp = None
            self._depacketizer.reset()
        self._measure(packet, now)
        receivers = self._receivers()
        if not receivers:
            self._active = False
            return
        if not self._active:
            self._active = True
            self._depacketizer.restart()
        unit = self._depacketizer.push(
            packet.sequence, packet.timestamp, packet.marker, packet.payload
        )
        if unit is None:
            return
        for receiver in receivers:
            try:
                receiver.on_source_video(unit, packet.ssrc)
            except Exception:
                logger.exception("Error sending video to a session")

    def _on_audio_input(self, data: bytes) -> None:
        try:
            packet = rtp.parse_rtp(data)
        except ValueError:
            return
        if packet.is_rtcp or not packet.payload:
            return
        for receiver in self._receivers():
            try:
                receiver.on_source_audio(packet)
            except Exception:
                logger.exception("Error sending audio to a session")
