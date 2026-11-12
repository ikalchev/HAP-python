"""A minimal WebRTC sender for HomeKit camera remote live view.

The integrator runs a media pull per camera channel and sends plain RTP to
the loopback ports of a :class:`~pyhap.media.source.MediaSource`, for
example::

    ffmpeg -i <camera>
        -map 0:v -c:v copy -bsf:v dump_extra -f rtp rtp://127.0.0.1:<video>
        -map 0:a -c:a libopus -ac 1 -ar 48000 -frame_duration 20
        -f rtp rtp://127.0.0.1:<audio>

Several sessions can share one source. Without a ``source`` a sender makes
its own, bound by :meth:`WebRTCSender.create_offer`.

Each session protects every H.265 access unit with SFrame (RFC 9605; suite 6
for video, suite 8 for audio, as Scrypted's HomeKit WebRTC plugin uses them),
splits it into RTP payloads behind the SFrame descriptor, gives the packets
its own SSRC, payload type, sequence numbers and timestamps, and sends them
as SRTP over ICE/DTLS. A session without SFrame (the controller turned it
off) sends plain RFC 7798 H.265 and Opus instead.

Session flow, mapped to the HAP WebRTC stream management operations:

* Solicit Offer: create a sender with ``VideoParams.from_tier(video_tier)``
  and the SFrame key (or None), then :meth:`WebRTCSender.create_offer`
  returns the SDP with every ICE candidate in it.
* Provide Answer: :meth:`WebRTCSender.apply_answer`. It returns once the
  answer is accepted; ICE and DTLS finish in the background.
* Reoffer: :meth:`WebRTCSender.reoffer`, answered with the same key.
* Update Session: :meth:`WebRTCSender.update_receive_keys`.
* End: :meth:`WebRTCSender.close`.

Sending is paced with a token bucket at ``pacing_factor`` times the larger
of the tier's target bit rate and the bit rate the source measures, and
faster when needed to keep the queue under ``max_queue_delay``. The writer
sleeps until the bucket allows the next packet, at least 5 ms at a time.
When video still waits longer than ``max_queue_delay`` (the loop or the
socket cannot keep up), queued video is dropped, the session waits for the
next IRAP picture and asks for one.

Keyframes: video starts with the first IRAP access unit after the
connection is up. ``on_keyframe_request(age, gop)`` fires then and for a PLI
or FIR from the controller, at most once per ``keyframe_request_interval``;
``age`` is the time since the source last started an IRAP picture and
``gop`` the interval between IRAP pictures the source measured (each None
until known). Do not restart the source on every request: the next GOP
brings a keyframe. A good policy is to restart (or otherwise ask) the
source only when ``age`` is None or above about twice the GOP.

The session ends on its own, calling ``on_closed(reason)``: an offer is not
answered within ``offer_timeout``, ICE and DTLS do not finish within
``negotiation_timeout`` of the answer, the connection fails, the controller
closes DTLS, ICE consent is lost, sending fails, no video arrives for
``idle_timeout`` or the source is closed. ``offer_timeout`` is a backstop for
callers without their own answer deadline; HAP signalling usually ends an
unanswered session earlier, and then closes the sender.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Coroutine, Iterable, Sequence
import contextlib
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any, Protocol

from . import MediaError, rtp, sdp
from .hevc import AccessUnit, HevcPacketizer
from .sdp import AudioParams, IceCandidateLike, LocalParams, VideoParams
from .sframe import (
    DEFAULT_MAX_PAYLOAD,
    SFrameDepacketizer,
    SFrameError,
    SFrameReceiver,
    SFrameSender,
    SFrameSuite,
    packetize,
)
from .source import MediaSource
from .transport import DtlsRole, DtlsSrtpTransport, TransportError

logger = logging.getLogger(__name__)

DEFAULT_OFFER_TIMEOUT = 60.0
DEFAULT_NEGOTIATION_TIMEOUT = 20.0
DEFAULT_IDLE_TIMEOUT = 10.0
DEFAULT_PACING_FACTOR = 2.5
DEFAULT_MAX_QUEUE_DELAY = 0.4
# Audio and retransmitted packets waiting to go out: a second of the
# retransmission cap plus some audio.
MAX_URGENT_PACKETS = 128
RTCP_INTERVAL = 1.0
KEYFRAME_REQUEST_INTERVAL = rtp.KEYFRAME_REQUEST_INTERVAL
AUDIO_TALKSPURT_GAP = rtp.AUDIO_TALKSPURT_GAP
# The shortest pause between two sends of a session's pacer.
PACING_INTERVAL = 0.005
# Socket bytes not sent yet beyond which the path counts as congested.
MAX_WRITE_BUFFER = 64 * 1024
MAX_NACK_PER_MESSAGE = 32
MAX_RETRANSMITS_PER_SECOND = 100
MAX_TALKBACK_STREAMS = 4


class SFrameKeyLike(Protocol):
    """An SFrame key and its KID, as HAP carries them."""

    @property
    def key(self) -> bytes:
        """The 32-byte key."""

    @property
    def kid(self) -> int:
        """The 64-bit key ID."""


@dataclass
class _Pending:
    """A payload waiting for the pacer; numbered when it is sent."""

    stream: rtp.OutboundStream
    timestamp: int
    marker: bool
    payload: bytes
    queued: float


class _Pacer:
    """Token bucket over the queued video, with a time-based queue limit."""

    def __init__(self, bitrate: float, factor: float, max_delay: float) -> None:
        self.factor = factor
        self.max_delay = max_delay
        self.video: deque[_Pending] = deque()
        self.queued_bytes = 0
        self._min_bytes_per_second = bitrate / 8
        self.bytes_per_second = self._min_bytes_per_second * factor
        self._rate = self.bytes_per_second
        self._tokens = 0.0
        self._last = time.monotonic()

    def follow(self, input_bitrate: float) -> None:
        """Base the rate on the input's bit rate when it is above the tier's."""
        base = max(self._min_bytes_per_second, input_bitrate / 8)
        self.bytes_per_second = base * self.factor

    def add(self, item: _Pending) -> None:
        self.video.append(item)
        self.queued_bytes += len(item.payload)

    def pop(self) -> _Pending:
        item = self.video.popleft()
        self.queued_bytes -= len(item.payload)
        return item

    def clear(self) -> None:
        self.video.clear()
        self.queued_bytes = 0

    def oldest_age(self, now: float) -> float:
        return now - self.video[0].queued if self.video else 0.0

    def budget(self, now: float) -> float:
        """Bytes that may go out now.

        The rate rises above the base rate when the queue would otherwise
        not drain within ``max_delay``.
        """
        remaining = max(self.max_delay - self.oldest_age(now), PACING_INTERVAL)
        rate = self._rate = max(self.bytes_per_second, self.queued_bytes / remaining)
        self._tokens = min(
            self._tokens + (now - self._last) * rate, 2 * PACING_INTERVAL * rate
        )
        self._last = now
        return self._tokens

    def spend(self, size: int) -> None:
        self._tokens -= size

    def wait(self) -> float:
        """Seconds until the bucket allows the next packet, at least one step."""
        return max(-self._tokens / self._rate, PACING_INTERVAL)


class WebRTCSender:
    """Sends one camera stream to one HomeKit controller over WebRTC.

    Callbacks, all optional and called on the event loop:

    * ``on_connected()``: ICE and DTLS are up, media is forwarded.
    * ``on_keyframe_request(age, gop)``: the session needs an IRAP picture,
      see the module docs for the policy.
    * ``on_audio(opus, rtp_timestamp)``: SFrame-decrypted talkback audio from
      the controller (only with ``AudioParams(talkback=True)``, and only once
      a receive key is installed).
    * ``on_closed(reason)``: the session ended on its own. Not called for
      :meth:`close`.
    """

    def __init__(
        self,
        *,
        video: VideoParams,
        audio: AudioParams | None = None,
        sframe: SFrameKeyLike | None,
        source: MediaSource | None = None,
        host_addresses: Sequence[str] | None = None,
        use_ipv6: bool = True,
        rtp_input_host: str = "127.0.0.1",
        offer_timeout: float = DEFAULT_OFFER_TIMEOUT,
        negotiation_timeout: float = DEFAULT_NEGOTIATION_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        pacing_factor: float = DEFAULT_PACING_FACTOR,
        max_queue_delay: float = DEFAULT_MAX_QUEUE_DELAY,
        keyframe_request_interval: float = KEYFRAME_REQUEST_INTERVAL,
    ) -> None:
        """Configure the session; sockets are bound by :meth:`create_offer`.

        ``sframe`` is the send key returned to the controller (32 random
        bytes and a KID), or None for a session without SFrame: video then
        goes out as plain RFC 7798 RTP and Opus as it is. ``host_addresses``
        default to the LAN interface, see
        :func:`pyhap.media.transport.default_host_addresses`.
        """
        self.video = video
        self.audio = audio
        self.on_closed: Callable[[str], None] | None = None
        self.on_connected: Callable[[], None] | None = None
        self.on_keyframe_request: (
            Callable[[float | None, float | None], None] | None
        ) = None
        self.on_audio: Callable[[bytes, int], None] | None = None
        self._sframe = None if sframe is None else (bytes(sframe.key), sframe.kid)
        self._source = source
        self._own_source = source is None
        self._rtp_input_host = rtp_input_host
        self._offer_timeout = offer_timeout
        self._negotiation_timeout = negotiation_timeout
        self._idle_timeout = idle_timeout
        self._max_payload = max_payload
        self._keyframe_request_interval = keyframe_request_interval
        self._transport = DtlsSrtpTransport(
            ice_controlling=True,
            host_addresses=host_addresses,
            use_ipv6=use_ipv6,
        )
        self._transport.on_rtp = self._on_remote_rtp
        self._transport.on_rtcp = self._on_remote_rtcp
        self._transport.on_closed = self._on_transport_closed
        self._video = rtp.OutboundStream(video.payload_type, rtp.VIDEO_CLOCK_RATE)
        self._audio = (
            rtp.OutboundStream(audio.payload_type, rtp.AUDIO_CLOCK_RATE)
            if audio
            else None
        )
        self._video_sframe: SFrameSender | None = None
        self._audio_sframe: SFrameSender | None = None
        self._packetizer = HevcPacketizer(max_payload)
        if self._sframe is not None:
            key, kid = self._sframe
            suite = SFrameSuite.HOMEKIT_VIDEO
            self._video_sframe = SFrameSender(key, kid, self._video.ssrc, suite)
            if self._audio:
                suite = SFrameSuite.HOMEKIT_AUDIO
                self._audio_sframe = SFrameSender(key, kid, self._audio.ssrc, suite)
        self._receiver = SFrameReceiver(SFrameSuite.HOMEKIT_AUDIO)
        self._talkback: OrderedDict[int, SFrameDepacketizer] = OrderedDict()
        self._pacer = _Pacer(
            video.pacing_bitrate_kbps * 1000, pacing_factor, max_queue_delay
        )
        # Audio and retransmissions; bounded, the oldest go first when the
        # path is blocked, so no burst of stale audio follows.
        self._urgent: deque[_Pending | bytes] = deque(maxlen=MAX_URGENT_PACKETS)
        self._wakeup = asyncio.Event()
        self._pacing = False
        self._local: LocalParams | None = None
        self._remote_ice: tuple[str, str] | None = None
        self._remote_fingerprints: list[tuple[str, str]] = []
        self._dtls_role = DtlsRole.CLIENT
        self._audio_active = audio is not None
        self._audio_payload_types: set[int] = set()
        self._waiting_for_keyframe = True
        self._last_keyframe_request = -keyframe_request_interval
        self._last_audio_input = 0.0
        self._connected_at = 0.0
        self._retransmit_window = (0.0, 0)
        self._tasks: set[asyncio.Task[None]] = set()
        self._state = "new"
        self._closed = False
        self._connected = False
        self._settled = asyncio.Event()
        self._close_done = asyncio.Event()

    @property
    def sframe(self) -> tuple[bytes, int] | None:
        """The SFrame send key and KID, None without SFrame."""
        return self._sframe

    @property
    def rtp_input(self) -> tuple[str, int, int]:
        """``(host, video_port, audio_port)`` of the source; 0 for no audio."""
        if self._source is None:
            return self._rtp_input_host, 0, 0
        return self._source.rtp_input

    @property
    def source(self) -> MediaSource | None:
        """The media source, once there is one."""
        return self._source

    @property
    def connected(self) -> bool:
        """Whether media flows."""
        return self._connected and not self._closed

    @property
    def closed(self) -> bool:
        """Whether the session ended."""
        return self._closed

    @property
    def wants_media(self) -> bool:
        """Whether the source should reassemble media for this session."""
        return self.connected

    @property
    def dtls_role(self) -> DtlsRole:
        """The local DTLS role; meaningful once the answer was applied."""
        return self._dtls_role

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until ICE and DTLS are up.

        Raises :class:`MediaError` when the session ends first, and
        ``asyncio.TimeoutError`` after ``timeout``.
        """
        await asyncio.wait_for(self._settled.wait(), timeout)
        if not self.connected:
            raise MediaError("WebRTC session closed")

    async def create_offer(self) -> str:
        """Bind the RTP input if needed, gather candidates and build the offer."""
        if self._state != "new":
            raise MediaError("Offer already created")
        self._state = "offered"
        try:
            if self._source is None:
                self._source = MediaSource(
                    self._rtp_input_host, audio=self.audio is not None
                )
            if self._own_source:
                await self._source.start()
            self._check_open()
            self._source.attach(self)
            candidates = await self._transport.gather()
            self._check_open()
        except (TransportError, RuntimeError) as err:
            await self._close(None)
            raise MediaError(str(err)) from err
        except BaseException:
            await self._close(None)
            raise
        self._local = LocalParams(
            ice_ufrag=self._transport.local_username,
            ice_pwd=self._transport.local_password,
            fingerprint=self._transport.certificate.fingerprint,
            cname=secrets.token_hex(8),
            stream_id=secrets.token_hex(8),
            video_ssrc=self._video.ssrc,
            audio_ssrc=self._audio.ssrc if self._audio else None,
            candidates=candidates,
            session_id=secrets.randbits(62),
        )
        self._start(self._expire("offered", self._offer_timeout, "offer not answered"))
        return sdp.build_offer(self._local, self.video, self.audio)

    def _check_open(self) -> None:
        if self._closed:
            raise MediaError("WebRTC session closed")

    async def apply_answer(
        self, answer_sdp: str, candidates: Iterable[IceCandidateLike] = ()
    ) -> None:
        """Accept the controller's answer and connect in the background.

        ``candidates`` are answer candidates HAP carried next to the SDP.
        """
        if self._state != "offered":
            raise MediaError("No offer is waiting for an answer")
        try:
            answer = sdp.parse(sdp.merge_candidates(answer_sdp, candidates))
        except sdp.SdpError as err:
            raise MediaError(f"Invalid answer: {err}") from err
        video = next((m for m in answer.media if m.kind == "video" and m.port), None)
        if video is None:
            raise MediaError("Answer rejected the video")
        try:
            media = answer.transport()
        except sdp.SdpError as err:
            raise MediaError(f"Invalid answer: {err}") from err
        if not media.ice_ufrag or not media.ice_pwd or not media.fingerprints:
            raise MediaError("Answer lacks ICE credentials or a fingerprint")
        video_pt = video.payload_type_for("H265", rtp.VIDEO_CLOCK_RATE)
        if video_pt is None:
            raise MediaError("Answer has no H.265 payload type")
        self._video.payload_type = video_pt
        self._apply_audio(answer, ("recvonly", "sendrecv"))
        self._state = "answered"
        self._remote_ice = (media.ice_ufrag, media.ice_pwd)
        self._remote_fingerprints = list(media.fingerprints)
        self._dtls_role = DtlsRole.from_remote_setup(media.setup)
        self._start(
            self._expire("answered", self._negotiation_timeout, "negotiation timed out")
        )
        self._start(
            self._connect(
                media.ice_ufrag,
                media.ice_pwd,
                answer.candidates,
                self._dtls_role,
                answer.ice_lite,
            )
        )

    def _apply_audio(
        self, answer: sdp.SessionDescription, sending: tuple[str, ...]
    ) -> None:
        """Pick up the audio payload type and whether audio is sent.

        ``sending`` lists the directions of the audio section that mean the
        accessory sends: the controller's view for its answer, the
        accessory's own for an answer to a re-offer.
        """
        audio = next((m for m in answer.media if m.kind == "audio" and m.port), None)
        pt = audio.payload_type_for("opus", rtp.AUDIO_CLOCK_RATE, 2) if audio else None
        self._set_audio(pt, audio.direction if audio else None, sending)

    def _set_audio(
        self, pt: int | None, direction: str | None, sending: tuple[str, ...]
    ) -> None:
        if pt is None or self._audio is None:
            self._audio_active = False
            return
        self._audio.payload_type = pt
        self._audio_active = direction in sending
        self._audio_payload_types = {pt}

    async def _connect(
        self,
        username: str,
        password: str,
        candidates: list[str],
        role: DtlsRole,
        ice_lite: bool,
    ) -> None:
        try:
            await self._transport.connect(
                remote_username=username,
                remote_password=password,
                remote_candidates=candidates,
                remote_fingerprints=self._remote_fingerprints,
                role=role,
                ice_lite=ice_lite,
                timeout=self._negotiation_timeout,
            )
        except Exception as err:  # noqa: BLE001 - TransportError and bugs alike
            logger.debug("WebRTC connection failed: %s", err)
            await self._close(str(err))
            return
        if self._closed:
            return
        self._state = "connected"
        self._connected = True
        self._connected_at = time.monotonic()
        self._settled.set()
        self._start(self._writer())
        self._start(self._rtcp_loop())
        self._notify(self.on_connected)
        self._request_keyframe()

    async def reoffer(self, offer_sdp: str) -> str:
        """Answer a controller re-offer on the established transport.

        ICE restarts and certificate changes are not supported.
        """
        if self._state not in ("answered", "connected") or self._local is None:
            raise MediaError("Session is not negotiated")
        try:
            offer = sdp.parse(offer_sdp)
            media = offer.transport()
        except sdp.SdpError as err:
            raise MediaError(f"Invalid offer: {err}") from err
        if (media.ice_ufrag, media.ice_pwd) != self._remote_ice:
            raise MediaError("ICE restart is not supported")
        if media.fingerprints and media.fingerprints != self._remote_fingerprints:
            raise MediaError("DTLS certificate change is not supported")
        role = self._dtls_role
        try:
            answer = sdp.build_answer(
                offer, self._local, role.setup, self.video, self.audio
            )
        except sdp.SdpError as err:
            raise MediaError(str(err)) from err
        if answer.video_payload_type is not None:
            self._video.payload_type = answer.video_payload_type
        self._set_audio(
            answer.audio_payload_type,
            answer.audio_direction,
            ("sendonly", "sendrecv"),
        )
        return answer.sdp

    async def update_receive_keys(
        self, add: Iterable[SFrameKeyLike] = (), remove: Iterable[int] = ()
    ) -> None:
        """Install and remove the controller's SFrame receive keys.

        Nothing changes when a key is invalid.
        """
        if self._sframe is None:
            raise MediaError("The session does not use SFrame")
        try:
            self._receiver.update_keys([(k.key, k.kid) for k in add], remove)
        except SFrameError as err:
            raise MediaError(str(err)) from err

    async def close(self) -> None:
        """Stop sending and release all sockets and tasks.

        Returns once that is done, also when the session is already ending
        on its own.
        """
        await self._close(None)
        await self._close_done.wait()

    async def _close(self, reason: str | None) -> None:
        """End the session; never raises, ``on_closed`` only with a reason."""
        if self._closed:
            return
        self._closed = True
        self._state = "closed"
        self._settled.set()
        if reason is not None:
            logger.debug("WebRTC session closed: %s", reason)
        if self._connected:
            try:
                await self._transport.send_rtcp(self._bye())
            except Exception:  # noqa: BLE001
                logger.debug("Sending RTCP BYE failed", exc_info=True)
        self._connected = False
        if self._source is not None:
            self._source.detach(self)
            if self._own_source:
                self._source.close("session closed")
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._transport.close()
        self._receiver.clear()
        self._pacer.clear()
        self._urgent.clear()
        self._close_done.set()
        if reason is not None and self.on_closed:
            try:
                self.on_closed(reason)
            except Exception:
                logger.exception("Error in WebRTC close callback")

    def _bye(self) -> bytes:
        ssrcs = [self._video.ssrc] + ([self._audio.ssrc] if self._audio else [])
        return rtp.build_bye(ssrcs)

    def _start(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _close_soon(self, reason: str) -> None:
        if not self._closed:
            self._start(self._close(reason))

    def _notify(self, callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Error in WebRTC callback")

    def _on_transport_closed(self, reason: str) -> None:
        self._close_soon(reason)

    def on_source_closed(self, reason: str) -> None:
        """The media source ended: end the session."""
        self._close_soon(f"media source ended: {reason}")

    async def _expire(self, state: str, timeout: float, reason: str) -> None:
        await asyncio.sleep(timeout)
        if self._state == state:
            await self._close(reason)

    async def _writer(self) -> None:
        try:
            while True:
                self._wakeup.clear()
                if not self._urgent and not self._pacer.video:
                    await self._wakeup.wait()
                delay = await self._send_ready()
                if delay:
                    await self._pace(delay)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a broken path ends the session
            logger.debug("WebRTC send failed", exc_info=True)
            await self._close(f"send failed: {err!r}")

    async def _pace(self, delay: float) -> None:
        """Wait for the pacer; audio and retransmissions end the wait early."""
        self._pacing = True
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), delay)
        finally:
            self._pacing = False

    async def _send_ready(self) -> float:
        """Send what the pacer allows now; drop video that waited too long.

        Returns the seconds until the pacer allows more, 0 when nothing waits.
        """
        now = time.monotonic()
        if self._pacer.oldest_age(now) > self._pacer.max_delay:
            logger.debug("WebRTC send queue over its limit, waiting for a keyframe")
            self._pacer.clear()
            self._waiting_for_keyframe = True
            self._request_keyframe()
        if self._transport.write_buffer_size > MAX_WRITE_BUFFER:
            return PACING_INTERVAL
        while self._urgent:
            item = self._urgent.popleft()
            packet = item if isinstance(item, bytes) else self._build(item)
            self._pacer.spend(len(packet))
            await self._transport.send_rtp(packet)
        if self._source is not None:
            self._pacer.follow(self._source.video_bitrate)
        budget = self._pacer.budget(now)
        while self._pacer.video and budget > 0:
            packet = self._build(self._pacer.pop())
            self._pacer.spend(len(packet))
            budget -= len(packet)
            await self._transport.send_rtp(packet)
        return self._pacer.wait() if self._pacer.video else 0.0

    @staticmethod
    def _build(item: _Pending) -> bytes:
        return item.stream.build(item.timestamp, item.marker, item.payload)

    async def _rtcp_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(RTCP_INTERVAL)
                last_input = max(self._connected_at, self._last_video_input())
                if time.monotonic() - last_input > self._idle_timeout:
                    await self._close("media input idle")
                    return
                await self._send_reports()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.debug("WebRTC RTCP send failed", exc_info=True)
            await self._close(f"rtcp send failed: {err!r}")

    def _last_video_input(self) -> float:
        return self._source.last_video_input if self._source else 0.0

    async def _send_reports(self) -> None:
        streams = [self._video]
        if self._audio and self._audio_active:
            streams.append(self._audio)
        for stream in streams:
            report = stream.sender_report()
            if report:
                sdes = rtp.build_sdes(stream.ssrc, self._cname)
                await self._transport.send_rtcp(report + sdes)

    @property
    def _cname(self) -> str:
        return self._local.cname if self._local else ""

    def on_source_video(self, unit: AccessUnit, ssrc: int) -> None:
        """Protect and queue one access unit from the source."""
        if not self.connected:
            return
        if self._waiting_for_keyframe and not unit.keyframe:
            return
        self._waiting_for_keyframe = False
        timestamp = self._video.timestamp(ssrc, unit.timestamp)
        if self._video_sframe is None:
            payloads = self._packetizer.packetize(unit.nals)
        else:
            frame = self._video_sframe.protect(unit.to_bytes())
            payloads = packetize(frame, self._max_payload)
        now = time.monotonic()
        last = len(payloads) - 1
        for index, payload in enumerate(payloads):
            self._pacer.add(
                _Pending(self._video, timestamp, index == last, payload, now)
            )
        if not self._pacing:
            self._wakeup.set()

    def on_source_audio(self, packet: rtp.RtpPacket) -> None:
        """Protect and queue one Opus packet from the source."""
        if not self._audio:
            return
        now = time.monotonic()
        marker = now - self._last_audio_input > AUDIO_TALKSPURT_GAP
        self._last_audio_input = now
        if not self.connected or not self._audio_active:
            return
        timestamp = self._audio.timestamp(packet.ssrc, packet.timestamp)
        payloads = [packet.payload]
        if self._audio_sframe is not None:
            frame = self._audio_sframe.protect(packet.payload)
            payloads = packetize(frame, self._max_payload)
        for payload in payloads:
            self._urgent.append(_Pending(self._audio, timestamp, marker, payload, now))
            marker = False
        self._wakeup.set()

    def _on_remote_rtcp(self, data: bytes) -> None:
        for message in rtp.parse_rtcp(data):
            if message.is_keyframe_request and self._video.ssrc in message.media_ssrcs:
                self._request_keyframe()
            elif message.is_nack:
                self._retransmit(message)

    def _retransmit(self, message: rtp.RtcpMessage) -> None:
        now = time.monotonic()
        started, count = self._retransmit_window
        if now - started >= 1.0:
            started, count = now, 0
        for stream in (self._video, self._audio):
            if stream is None or stream.ssrc not in message.media_ssrcs:
                continue
            for sequence in message.lost[:MAX_NACK_PER_MESSAGE]:
                packet = stream.history.get(sequence)
                if packet is None or count >= MAX_RETRANSMITS_PER_SECOND:
                    continue
                count += 1
                self._urgent.append(packet)
        self._retransmit_window = (started, count)
        self._wakeup.set()

    def _request_keyframe(self) -> None:
        now = time.monotonic()
        if now - self._last_keyframe_request < self._keyframe_request_interval:
            return
        self._last_keyframe_request = now
        source = self._source
        age = source.keyframe_age() if source else None
        self._notify(self.on_keyframe_request, age, source.gop if source else None)

    def _on_remote_rtp(self, data: bytes) -> None:
        """Deliver talkback audio.

        With SFrame only decrypted, once a receive key exists; never as it
        arrived.
        """
        if not self.on_audio:
            return
        if self._sframe is not None and not self._receiver.has_keys:
            return
        try:
            packet = rtp.parse_rtp(data)
        except ValueError:
            return
        if packet.payload_type not in self._audio_payload_types:
            return
        if self._sframe is None:
            self._notify(self.on_audio, packet.payload, packet.timestamp)
            return
        depacketizer = self._talkback.get(packet.ssrc)
        if depacketizer is None:
            depacketizer = self._talkback[packet.ssrc] = SFrameDepacketizer()
            while len(self._talkback) > MAX_TALKBACK_STREAMS:
                self._talkback.popitem(last=False)
        frame = depacketizer.push(packet.sequence, packet.payload)
        if frame is None:
            return
        try:
            opus = self._receiver.unprotect(frame, packet.ssrc)
        except SFrameError as err:
            logger.debug("Dropping talkback frame: %s", err)
            return
        self._notify(self.on_audio, opus, packet.timestamp)
