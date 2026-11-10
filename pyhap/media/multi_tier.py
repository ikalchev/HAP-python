"""SRTP media of HomeKit multi-tier RTP sessions from a shared media source.

A :class:`MultiTierSender` sends one camera stream to one controller on the
local network, as the Camera Multi-Tier RTP Stream Management service of a
HomeKit Secure Video 17.99 camera negotiates it. It reads the same
:class:`~pyhap.media.source.MediaSource` as the camera's other sessions, so a
camera channel is pulled once for all local and remote sessions; see
:class:`~pyhap.media.pool.SourcePool`.

Video goes out as RFC 7798 H.265 (single NAL units, aggregation and
fragmentation units, no DONL) with the 90 kHz clock, the parameter sets
in-band before each IRAP picture as the source delivers them. Audio is the
source's Opus with the 48 kHz clock. Each packet gets the session's SSRC and
payload type and is protected with SRTP (AES_CM_128_HMAC_SHA1_80) with the
key of its stream; RTCP sender reports go out once a second as SRTCP.

Session flow, mapped to the HAP operations:

* Setup Endpoints: create the sender and :meth:`MultiTierSender.bind` it.
  It binds the accessory's video and audio ports, which the answer
  announces, and sends from them.
* Start: :meth:`MultiTierSender.start` with the source of the tier's
  channel. Video starts with the next IRAP picture; ``on_keyframe_request``
  asks the channel's pull for one.
* Start again with another tier: :meth:`MultiTierSender.switch` to the
  other channel's source. SSRCs, sequence numbers, RTP time and the SRTP
  packet index continue.
* End: :meth:`MultiTierSender.close`.

The controller sends its RTCP to the accessory's ports. Each datagram from
the controller's address calls ``on_rtcp`` (at most once per
``rtcp_notify_interval``), which keeps the HAP session alive; a picture loss
indication or full intra request that decrypts with the session key asks
for a keyframe. The sender ends on its own, calling ``on_closed(reason)``,
when no video arrives for ``idle_timeout``, sending fails or the source is
closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
import logging
import time
from typing import Any, cast

import pylibsrtp

from . import MediaError, rtp
from .hevc import AccessUnit, HevcPacketizer
from .source import MediaSource

logger = logging.getLogger(__name__)

# Keeps every RTP packet below a typical path MTU.
DEFAULT_MAX_PAYLOAD = 1200

DEFAULT_IDLE_TIMEOUT = 10.0
RTCP_INTERVAL = 1.0
RTCP_NOTIFY_INTERVAL = 1.0
# Length of an AES_CM_128_HMAC_SHA1_80 master key and salt together.
SRTP_KEY_LENGTH = 30


class _Port(asyncio.DatagramProtocol):
    """One of the accessory's ports: sends media, receives the controller's RTCP."""

    def __init__(self, on_datagram: Callable[[bytes, tuple[Any, ...]], None]) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self._on_datagram = on_datagram

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        try:
            self._on_datagram(data, addr)
        except Exception:
            logger.exception("Error handling a multi-tier RTCP packet")

    @property
    def port(self) -> int:
        if self.transport is None:
            return 0
        return int(self.transport.get_extra_info("sockname")[1])


class _SrtpStream:
    """An outbound RTP stream and its SRTP context."""

    def __init__(self, ssrc: int, payload_type: int, clock: int, key: bytes) -> None:
        if len(key) != SRTP_KEY_LENGTH:
            raise ValueError("SRTP key and salt must be 30 bytes")
        self.rtp = rtp.OutboundStream(payload_type, clock)
        self.rtp.ssrc = ssrc
        self._outbound = pylibsrtp.Session(
            pylibsrtp.Policy(key=key, ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND)
        )
        self._inbound = pylibsrtp.Session(
            pylibsrtp.Policy(key=key, ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND)
        )

    def packet(self, timestamp: int, marker: bool, payload: bytes) -> bytes:
        packet = self.rtp.build(timestamp, marker, payload)
        # Nothing is retransmitted, no history is kept.
        self.rtp.history.clear()
        return self._outbound.protect(packet)

    def report(self, cname: str) -> bytes | None:
        report = self.rtp.sender_report()
        if report is None:
            return None
        return self._outbound.protect_rtcp(
            report + rtp.build_sdes(self.rtp.ssrc, cname)
        )

    def bye(self, cname: str) -> bytes:
        # RFC 3550 6.1: a compound packet starts with a report and has a CNAME;
        # a source that has sent nothing reports as a receiver.
        report = self.rtp.sender_report() or rtp.build_receiver_report(self.rtp.ssrc)
        return self._outbound.protect_rtcp(
            report
            + rtp.build_sdes(self.rtp.ssrc, cname)
            + rtp.build_bye([self.rtp.ssrc])
        )

    def unprotect_rtcp(self, data: bytes) -> bytes | None:
        try:
            return self._inbound.unprotect_rtcp(data)
        except pylibsrtp.Error:
            return None

    def authentic(self, data: bytes) -> bool:
        """Whether ``data`` is SRTCP or SRTP under the session's key."""
        if self.unprotect_rtcp(data) is not None:
            return True
        try:
            self._inbound.unprotect(data)
        except pylibsrtp.Error:
            return False
        return True


class MultiTierSender:
    """Sends one camera stream to one controller as SRTP, see the module docs.

    Callbacks, all optional and called on the event loop:

    * ``on_rtcp()``: the controller is still there; keep its session alive.
    * ``on_keyframe_request(age, gop)``: the session needs an IRAP picture;
      ``age`` is the time since the source last started one and ``gop`` the
      interval between them the source measured (each None until known).
    * ``on_closed(reason)``: the sender ended on its own. Not called for
      :meth:`close`.
    """

    def __init__(
        self,
        *,
        address: str,
        video_port: int,
        audio_port: int,
        video_key: bytes,
        audio_key: bytes,
        video_ssrc: int,
        audio_ssrc: int,
        video_payload_type: int = rtp.DEFAULT_VIDEO_PAYLOAD_TYPE,
        audio_payload_type: int = rtp.DEFAULT_AUDIO_PAYLOAD_TYPE,
        local_video_port: int = 0,
        local_audio_port: int = 0,
        is_ipv6: bool | None = None,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        keyframe_request_interval: float = rtp.KEYFRAME_REQUEST_INTERVAL,
        rtcp_notify_interval: float = RTCP_NOTIFY_INTERVAL,
    ) -> None:
        """Configure the session; :meth:`bind` opens the ports.

        ``address`` and the ports are where the controller receives. The
        keys are the SRTP master key and salt (30 bytes) of each stream, as
        Setup Endpoints carries them. Local ports of 0 bind free ones.
        """
        self.address = address.split("%", 1)[0]
        self.is_ipv6 = ":" in self.address if is_ipv6 is None else is_ipv6
        self.on_rtcp: Callable[[], None] | None = None
        self.on_keyframe_request: (
            Callable[[float | None, float | None], None] | None
        ) = None
        self.on_closed: Callable[[str], None] | None = None
        self._destination = (video_port, audio_port)
        self._local = (local_video_port, local_audio_port)
        self._video = _SrtpStream(
            video_ssrc, video_payload_type, rtp.VIDEO_CLOCK_RATE, video_key
        )
        self._audio = _SrtpStream(
            audio_ssrc, audio_payload_type, rtp.AUDIO_CLOCK_RATE, audio_key
        )
        self._cname = f"pyhap-{video_ssrc:08x}"
        self._packetizer = HevcPacketizer(max_payload)
        self._closing: asyncio.Task[None] | None = None
        self._idle_timeout = idle_timeout
        self._keyframe_request_interval = keyframe_request_interval
        self._rtcp_notify_interval = rtcp_notify_interval
        self._video_port: _Port | None = None
        self._audio_port: _Port | None = None
        self._source: MediaSource | None = None
        self._send_audio = False
        self._waiting_for_keyframe = True
        self._last_keyframe_request = -keyframe_request_interval
        self._last_rtcp_notify = -rtcp_notify_interval
        self._last_audio_input = 0.0
        self._started_at = 0.0
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._closed = False

    @property
    def ports(self) -> tuple[int, int]:
        """The accessory's ``(video_port, audio_port)`` once bound."""
        if self._video_port is None or self._audio_port is None:
            return self._local
        return self._video_port.port, self._audio_port.port

    @property
    def source(self) -> MediaSource | None:
        """The source the sender reads, once started."""
        return self._source

    @property
    def started(self) -> bool:
        """Whether media flows to the controller."""
        return self._started and not self._closed

    @property
    def closed(self) -> bool:
        """Whether the session ended."""
        return self._closed

    @property
    def wants_media(self) -> bool:
        """Whether the source should reassemble media for this session."""
        return self.started

    async def bind(self) -> tuple[int, int]:
        """Bind the accessory's ports on every interface and return them.

        :raises OSError: If a port cannot be bound.
        """
        if self._closed:
            raise MediaError("Multi-tier session closed")
        if self._video_port is not None:
            return self.ports
        loop = asyncio.get_running_loop()
        # Any interface: the controller reaches the accessory on its LAN
        # address; only the controller's datagrams are read.
        any_address = "::" if self.is_ipv6 else "0.0.0.0"  # noqa: S104
        try:
            _, self._video_port = await loop.create_datagram_endpoint(
                lambda: _Port(self._on_video_port),
                local_addr=(any_address, self._local[0]),
            )
            if not self._closed:
                _, self._audio_port = await loop.create_datagram_endpoint(
                    lambda: _Port(self._on_audio_port),
                    local_addr=(any_address, self._local[1]),
                )
        except BaseException:
            self._close_ports()
            raise
        if self._closed:
            # Closed while binding: _close found no ports to release.
            self._close_ports()
            raise MediaError("Multi-tier session closed")
        return self.ports

    async def start(self, source: MediaSource, audio: bool = True) -> None:
        """Send ``source`` to the controller, with its audio if ``audio``."""
        if self._closed:
            raise MediaError("Multi-tier session closed")
        if self._video_port is None:
            await self.bind()
        if self._started:
            raise MediaError("Multi-tier session already started")
        self._send_audio = audio
        self._attach(source)
        self._started = True
        self._started_at = time.monotonic()
        self._spawn(self._rtcp_loop())
        self._request_keyframe()

    async def switch(self, source: MediaSource) -> None:
        """Continue the stream from another source, e.g. for another tier."""
        if not self.started:
            raise MediaError("Multi-tier session not started")
        if source is self._source:
            return
        self._attach(source)
        self._started_at = time.monotonic()
        self._request_keyframe()

    def _attach(self, source: MediaSource) -> None:
        if source.closed:
            raise MediaError("Media source is closed")
        if self._source is not None:
            self._source.detach(self)
        self._source = source
        self._waiting_for_keyframe = True
        source.attach(self)

    async def close(self) -> None:
        """Stop sending and release the ports, without calling ``on_closed``."""
        if self._closing is not None:
            # Ending on its own already; wait until the ports are released.
            await asyncio.shield(self._closing)
            return
        await self._close(None)

    async def _close(self, reason: str | None) -> None:
        if self._closed:
            return
        self._closed = True
        if reason is not None:
            logger.debug("Multi-tier session closed: %s", reason)
        if self._started:
            self._send(self._video_port, self._video.bye(self._cname), 0)
            if self._send_audio:
                self._send(self._audio_port, self._audio.bye(self._cname), 1)
        if self._source is not None:
            self._source.detach(self)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._close_ports()
        if reason is not None and self.on_closed:
            try:
                self.on_closed(reason)
            except Exception:
                logger.exception("Error in multi-tier close callback")

    def _close_ports(self) -> None:
        for port in (self._video_port, self._audio_port):
            if port is not None and port.transport is not None:
                port.transport.close()

    def _close_soon(self, reason: str) -> None:
        if not self._closed and self._closing is None:
            self._closing = asyncio.get_running_loop().create_task(self._close(reason))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _notify(self, callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Error in multi-tier callback")

    def _send(self, port: _Port | None, packet: bytes, index: int) -> None:
        if port is None or port.transport is None or port.transport.is_closing():
            return
        port.transport.sendto(packet, (self.address, self._destination[index]))

    def on_source_closed(self, reason: str) -> None:
        """The media source ended: end the session."""
        self._close_soon(f"media source ended: {reason}")

    def on_source_video(self, unit: AccessUnit, ssrc: int) -> None:
        """Send one access unit, starting with an IRAP picture."""
        if not self.started:
            return
        if self._waiting_for_keyframe and not unit.keyframe:
            return
        self._waiting_for_keyframe = False
        timestamp = self._video.rtp.timestamp(ssrc, unit.timestamp)
        payloads = self._packetizer.packetize(unit.nals)
        last = len(payloads) - 1
        try:
            for index, payload in enumerate(payloads):
                packet = self._video.packet(timestamp, index == last, payload)
                self._send(self._video_port, packet, 0)
        except (OSError, pylibsrtp.Error) as err:
            self._close_soon(f"send failed: {err!r}")

    def on_source_audio(self, packet: rtp.RtpPacket) -> None:
        """Send one Opus packet."""
        now = time.monotonic()
        marker = now - self._last_audio_input > rtp.AUDIO_TALKSPURT_GAP
        self._last_audio_input = now
        if not self.started or not self._send_audio:
            return
        timestamp = self._audio.rtp.timestamp(packet.ssrc, packet.timestamp)
        try:
            data = self._audio.packet(timestamp, marker, packet.payload)
            self._send(self._audio_port, data, 1)
        except (OSError, pylibsrtp.Error) as err:
            self._close_soon(f"send failed: {err!r}")

    def _from_controller(self, addr: tuple[Any, ...]) -> bool:
        # The controller's address has no scope ID, a received one may.
        return str(addr[0]).split("%", 1)[0] == self.address

    def _on_video_port(self, data: bytes, addr: tuple[Any, ...]) -> None:
        if not self._from_controller(addr):
            return
        rtcp = self._video.unprotect_rtcp(data)
        if rtcp is None:
            # A spoofed source address alone must not keep the session alive.
            return
        self._controller_alive()
        for message in rtp.parse_rtcp(rtcp):
            if (
                message.is_keyframe_request
                and self._video.rtp.ssrc in message.media_ssrcs
            ):
                self._request_keyframe()

    def _on_audio_port(self, data: bytes, addr: tuple[Any, ...]) -> None:
        if self._from_controller(addr) and self._audio.authentic(data):
            self._controller_alive()

    def _controller_alive(self) -> None:
        now = time.monotonic()
        if now - self._last_rtcp_notify < self._rtcp_notify_interval:
            return
        self._last_rtcp_notify = now
        self._notify(self.on_rtcp)

    def _request_keyframe(self) -> None:
        now = time.monotonic()
        if now - self._last_keyframe_request < self._keyframe_request_interval:
            return
        self._last_keyframe_request = now
        source = self._source
        age = source.keyframe_age() if source else None
        self._notify(self.on_keyframe_request, age, source.gop if source else None)

    async def _rtcp_loop(self) -> None:
        cname = self._cname
        try:
            while True:
                await asyncio.sleep(RTCP_INTERVAL)
                source = self._source
                last_input = max(
                    self._started_at, source.last_video_input if source else 0.0
                )
                if time.monotonic() - last_input > self._idle_timeout:
                    await self._close("media input idle")
                    return
                if self._waiting_for_keyframe:
                    self._request_keyframe()
                if report := self._video.report(cname):
                    self._send(self._video_port, report, 0)
                if self._send_audio and (report := self._audio.report(cname)):
                    self._send(self._audio_port, report, 1)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a broken path ends the session
            logger.debug("Multi-tier RTCP send failed", exc_info=True)
            with contextlib.suppress(Exception):
                await self._close(f"rtcp send failed: {err!r}")
