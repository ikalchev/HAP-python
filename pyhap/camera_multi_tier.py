"""Multi-tier RTP streaming of a HomeKit Secure Video 17.99 camera.

Developer Preview: HomeKit Secure Video 17.99 is a preview of the guide, so
this API is experimental and may change.

:class:`pyhap.camera.Camera` adds a :class:`MultiTierController` per sensor
for its ``multi_tier`` option. Controllers on the local network stream
through it: they set up a session with Setup Endpoints and start, change or
end its stream with RTP Streaming Control. A :class:`MultiTierDelegate` sends
the media, it gets the sensor and the tiers the controller picked and maps
them to its sources.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import contextlib
from dataclasses import dataclass, field
import logging
import re
import secrets
from typing import TYPE_CHECKING, Any, Protocol, cast

from pyhap import hksv3
from pyhap.camera import parse_setup_address
from pyhap.camera_stream import (
    DEFAULT_HOOK_TIMEOUT,
    HookFailed,
    SessionState,
    StreamController,
    StreamSession,
    check_hook_timeout,
    config_from_option,
    failure_status,
)
from pyhap.util import base64_to_bytes, to_base64_str

if TYPE_CHECKING:
    from pyhap.camera import Camera

__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "FFMPEG_AUDIO_OUTPUT",
    "FFMPEG_VIDEO_OUTPUT",
    "MULTI_TIER_MIN_SESSIONS",
    "FFmpegMultiTierDelegate",
    "MultiTierConfig",
    "MultiTierController",
    "MultiTierDelegate",
    "MultiTierSession",
    "RtcpMonitor",
    "open_rtcp_monitor",
]

logger = logging.getLogger(__name__)

# Multi-tier sessions per sensor the guide requires at least.
MULTI_TIER_MIN_SESSIONS = 5
# Seconds a started session lives without keep_alive().
DEFAULT_IDLE_TIMEOUT = 30.0
_SRTP_SUITE = hksv3.SRTPCryptoSuite.AES_CM_128_HMAC_SHA1_80


@dataclass(eq=False, kw_only=True)
class MultiTierSession(StreamSession):
    """A multi-tier RTP session a controller set up with Setup Endpoints.

    ``address`` and the ports are where the controller receives, with the
    SRTP keys it sent. The accessory sends from ``accessory_address`` and the
    ``accessory_*_port`` values, by default its address on the controller's
    connection and the controller's port numbers; ``handle_prepare`` may
    change them. ``video_ssrc`` and ``audio_ssrc`` are the accessory's SSRCs
    from the Setup Endpoints answer, the controller filters by them. The
    ``controller_*_ssrc`` values are what the controller sent with the last
    start. ``video_tier`` and ``audio_tier`` are what streams once started.
    """

    address: str
    is_ipv6: bool
    video_port: int
    audio_port: int
    video_srtp_key: bytes = field(repr=False)
    video_srtp_salt: bytes = field(repr=False)
    audio_srtp_key: bytes = field(repr=False)
    audio_srtp_salt: bytes = field(repr=False)
    video_ssrc: int
    audio_ssrc: int
    accessory_address: str
    accessory_video_port: int
    accessory_audio_port: int
    controller_video_ssrc: int | None = None
    controller_audio_ssrc: int | None = None
    video_tier: hksv3.VideoStreamTier | None = None
    audio_tier: hksv3.AudioStreamTier | None = None

    def keep_alive(self) -> None:
        """Restart the idle timeout of the started session.

        Call it when the controller is still there, e.g. for each of its RTCP
        packets as :class:`RtcpMonitor` does. Call it in the event loop.
        """
        controller = self._controller
        if isinstance(controller, MultiTierController):
            controller.keep_alive(self)


class RtcpMonitor(asyncio.DatagramProtocol):
    """Keeps a session alive while its controller sends RTCP.

    Listens on the accessory's video port of the session, where the
    controller sends its RTCP. Each datagram from the controller's address
    calls :meth:`MultiTierSession.keep_alive`, without decrypting it. Others
    are ignored. Open it with :func:`open_rtcp_monitor`.
    """

    def __init__(self, session: MultiTierSession) -> None:
        self.session = session
        self.port = 0
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)
        self.port = int(transport.get_extra_info("sockname")[1])

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        # The controller's address has no scope ID, a received one may.
        if str(addr[0]).split("%", 1)[0] == self.session.address:
            self.session.keep_alive()

    @property
    def closed(self) -> bool:
        return self._transport is None or self._transport.is_closing()

    def close(self) -> None:
        """Close the socket, e.g. in ``handle_end``."""
        if self._transport is not None:
            self._transport.close()


async def open_rtcp_monitor(session: MultiTierSession) -> RtcpMonitor:
    """Open an :class:`RtcpMonitor` for ``session``, e.g. in ``handle_prepare``.

    Binds a free UDP port of the controller's IP version and announces it as
    the accessory's video port, ``session.accessory_video_port``. The stream
    is then sent from another port, with its RTCP to the controller's video
    port; for ffmpeg, an ``srtp://`` output without ``localrtpport`` and with
    ``rtcpport`` set to ``session.video_port``. Close the monitor when the
    session ends.

    :raises OSError: If no port can be bound.
    """
    loop = asyncio.get_running_loop()
    # Any interface, as the stream's own ports. Only the controller's
    # datagrams count.
    any_address = "::" if session.is_ipv6 else "0.0.0.0"  # noqa: S104
    _, monitor = await loop.create_datagram_endpoint(
        lambda: RtcpMonitor(session), local_addr=(any_address, 0)
    )
    session.accessory_video_port = monitor.port
    return monitor


class MultiTierDelegate(Protocol):
    """Sends the media of multi-tier RTP sessions.

    Each hook may raise :class:`pyhap.camera_stream.StreamError` to answer
    with a status, any other exception answers with an error. A hook that
    takes longer than ``hook_timeout`` is cancelled and its session ends.
    """

    async def handle_prepare(self, session: MultiTierSession) -> None:
        """Prepare a session before the Setup Endpoints answer.

        May set the ``accessory_*`` values of ``session`` and keep anything in
        ``session.data``. Raise ``StreamError`` with a
        :class:`pyhap.hksv3.SetupEndpointsStatus` to refuse it.
        """

    async def handle_start(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Start streaming ``session.sensor`` in the given tiers.

        Send video with ``session.video_ssrc`` and audio with
        ``session.audio_ssrc``, SRTP protected with the session keys and with
        the payload types of :mod:`pyhap.hksv3`. ``audio_tier`` is None if the
        controller wants no audio: then send none.
        """

    async def handle_reconfigure(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Switch a started session to other tiers.

        Keep the SSRCs, the SRTP keys and the SRTP packet index of the running
        stream: a new sender with the same SSRC and key repeats the keystream.
        A delegate that cannot change the tier of a running stream, e.g. one
        that copies its source, keeps the stream as it is.
        """

    async def handle_end(self, session: MultiTierSession) -> None:
        """Release a session that ended, whatever state it reached.

        Called exactly once for every session ``handle_prepare`` saw, also
        after a failed prepare, start or reconfigure.
        """


@dataclass(frozen=True, kw_only=True)
class MultiTierConfig:
    """The ``multi_tier`` option of :class:`pyhap.camera.Camera`.

    ``delegate`` sends the media. ``audio`` is the one audio tier the guide
    allows. ``max_sessions`` per sensor is at least five, as the guide
    requires, and one connection has at most ``max_sessions_per_connection``
    of them. A delegate hook gets ``hook_timeout`` seconds. A session must
    start within ``setup_timeout`` seconds of its Setup Endpoints answer.
    A started session ends unless :meth:`MultiTierSession.keep_alive` is
    called at least every ``idle_timeout`` seconds, e.g. for each RTCP packet
    of the controller; None turns that off.
    """

    delegate: MultiTierDelegate | None = None
    audio: hksv3.AudioStreamTier = hksv3.DEFAULT_AUDIO_TIER
    max_sessions: int = MULTI_TIER_MIN_SESSIONS
    max_sessions_per_connection: int = 3
    hook_timeout: float = DEFAULT_HOOK_TIMEOUT
    setup_timeout: float = 30.0
    idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT

    @classmethod
    def from_option(cls, value: MultiTierConfig | Mapping[str, Any]) -> MultiTierConfig:
        """Take the option as it is or build it from a dict of its fields.

        :raises ValueError: For an unknown key.
        """
        return config_from_option(cls, value, "multi_tier")

    def __post_init__(self) -> None:
        check_hook_timeout(self.hook_timeout)
        if self.max_sessions < MULTI_TIER_MIN_SESSIONS:
            raise ValueError(
                f"multi_tier max_sessions must be at least {MULTI_TIER_MIN_SESSIONS}"
            )
        if not 0 < self.max_sessions_per_connection <= self.max_sessions:
            raise ValueError(
                "multi_tier max_sessions_per_connection must be 1 to max_sessions"
            )
        if not isinstance(self.audio, hksv3.AudioStreamTier):
            raise ValueError("multi_tier audio must be exactly one audio tier")
        hksv3.check_audio_tier(self.audio)
        if self.setup_timeout <= 0 or (
            self.idle_timeout is not None and self.idle_timeout <= 0
        ):
            raise ValueError("multi_tier timeouts must be positive")


def supported_audio_tiers(audio: hksv3.AudioStreamTier) -> bytes:
    """The Supported Audio Stream Tiers value, exactly one Opus tier."""
    return hksv3.SupportedAudioStreamTiers(
        hksv3.AudioCodec.OPUS, hksv3.AUDIO_PAYLOAD_TYPE, [audio]
    ).encode()


def supported_video_tiers(tiers: list[hksv3.VideoStreamTier]) -> bytes:
    """The Supported Video Stream Tiers value, H.265 only."""
    return hksv3.SupportedVideoStreamTiers(
        hksv3.VideoCodec.H265, hksv3.VIDEO_PAYLOAD_TYPE, tiers
    ).encode()


class MultiTierController(StreamController[MultiTierSession]):
    """The Camera Multi-Tier RTP Stream Management service of one sensor."""

    kind = "multi-tier"

    def __init__(
        self,
        camera: Camera,
        sensor: hksv3.SensorConfiguration,
        config: MultiTierConfig,
    ) -> None:
        super().__init__(
            camera,
            sensor,
            config.hook_timeout,
            config.max_sessions,
            config.max_sessions_per_connection,
        )
        self.config = config
        self.delegate = config.delegate
        self.tiers = hksv3.tiers_from_sensor(sensor)
        service = self.service = camera.add_preload_service(
            "CameraMultiTierRTPStreamManagement", unique_id=str(sensor.sensor_uuid)
        )
        for name, value in (
            ("SensorUUID", sensor.sensor_uuid.bytes),
            ("SupportedVideoStreamTiers", supported_video_tiers(self.tiers)),
            ("SupportedAudioStreamTiers", supported_audio_tiers(config.audio)),
        ):
            service.configure_char(name, value=to_base64_str(value))
        # The guide allows AES_CM_128_HMAC_SHA1_80 only.
        service.configure_char(
            "SupportedRTPConfiguration", value=camera.get_supported_rtp_config(True)
        )
        self.streaming_enabled = service.configure_char("StreamingEnabled", value=True)
        self._status_active = service.get_characteristic("StatusActive")
        setup_endpoints = service.configure_char(
            "SetupEndpoints", setter_callback=self._set_setup_endpoints
        )
        # The answer has the session's SRTP keys, only its controller reads it.
        setup_endpoints.read_back_response = True
        service.configure_char(
            "RTPStreamingControl", setter_callback=self._set_streaming_control
        )

    @property
    def active(self) -> bool:
        """Status Active: whether the sensor may stream now."""
        return bool(self._status_active.value)

    def update_status(self) -> None:
        """Derive Status Active, ending the sessions if it turned off."""
        active = self.camera.streaming_allowed and bool(self.streaming_enabled.value)
        self._status_active.set_value(active)
        if not active:
            self._end_sessions("streaming was turned off")

    def keep_alive(self, session: MultiTierSession) -> None:
        """Restart the ``idle_timeout`` of a started session.

        A former session with the same ID keeps nothing alive. See
        :meth:`MultiTierSession.keep_alive`.
        """
        if (
            self._sessions.get(session.id) is session
            and session.state is SessionState.STARTED
        ):
            self._set_timer(
                session,
                self.config.idle_timeout,
                "No stream activity",
                SessionState.STARTED,
            )

    @property
    def _delegate(self) -> MultiTierDelegate:
        # Only a controller with a delegate has sessions.
        return cast("MultiTierDelegate", self.delegate)

    async def _release_hook(self, session: MultiTierSession) -> None:
        await self._delegate.handle_end(session)

    async def _set_setup_endpoints(
        self, value: str, sender_client_addr: Any = None
    ) -> Any:
        """Set up a session, only the writing connection reads the answer."""
        deadline = self._deadline()
        try:
            request = hksv3.SetupEndpoints.decode(base64_to_bytes(value))
        except ValueError:
            logger.warning("Ignoring a malformed Setup Endpoints write")
            status = hksv3.SetupEndpointsStatus.ERROR
            return to_base64_str(hksv3.SetupEndpointsResponse(None, status).encode())
        result = await self._setup(request, sender_client_addr, deadline)
        if isinstance(result, MultiTierSession):
            return to_base64_str(_answer(result).encode())
        response = hksv3.SetupEndpointsResponse(request.session_id, result)
        return to_base64_str(response.encode())

    async def _setup(
        self,
        request: hksv3.SetupEndpoints,
        client_addr: Any,
        deadline: float,
    ) -> MultiTierSession | hksv3.SetupEndpointsStatus:
        session_id = request.session_id
        endpoint = request.address
        error = hksv3.SetupEndpointsStatus.ERROR
        try:
            address = parse_setup_address(
                endpoint.address.encode("utf-8"), endpoint.is_ipv6
            )
            _check_srtp(request.video_srtp)
            _check_srtp(request.audio_srtp)
        except ValueError as ex:
            logger.warning("[%s] Refusing the endpoints: %s", session_id, ex)
            return error
        logger.debug(
            "[%s] Setup Endpoints: %s, video port %d, audio port %d",
            session_id,
            address,
            endpoint.video_port,
            endpoint.audio_port,
        )
        existing = self._owned(session_id, client_addr)
        if existing is None and session_id in self._sessions:
            return error
        if not self.active:
            logger.warning("[%s] Streaming is turned off, refusing it", session_id)
            return error
        if existing is None and (no_room := self._no_room(client_addr)):
            logger.warning("[%s] %s", session_id, no_room)
            return hksv3.SetupEndpointsStatus.BUSY
        if self.delegate is None:
            logger.warning("[%s] No multi-tier delegate, refusing", session_id)
            return error
        accessory_address = self.camera.stream_address_for(
            client_addr, endpoint.is_ipv6
        )
        if accessory_address is None:
            logger.warning(
                "[%s] No local address of the controller's IP version", session_id
            )
            return error
        if existing is not None:
            # Set up again: the delegate releases the former session first.
            if not await self._end_by_request(existing, deadline):
                logger.warning(
                    "[%s] The former session was not released, refusing",
                    session_id,
                )
                return error
            # Meanwhile another write may have set up the ID or taken the room.
            if session_id in self._sessions:
                logger.warning("[%s] Set up again meanwhile, refusing", session_id)
                return error
            if not self.active:
                logger.warning("[%s] Streaming is turned off, refusing it", session_id)
                return error
            if no_room := self._no_room(client_addr):
                logger.warning("[%s] %s", session_id, no_room)
                return hksv3.SetupEndpointsStatus.BUSY
        session = MultiTierSession(
            id=session_id,
            sensor=self.sensor,
            client_addr=client_addr,
            address=address,
            is_ipv6=endpoint.is_ipv6,
            video_port=endpoint.video_port,
            audio_port=endpoint.audio_port,
            video_srtp_key=request.video_srtp.master_key,
            video_srtp_salt=request.video_srtp.master_salt,
            audio_srtp_key=request.audio_srtp.master_key,
            audio_srtp_salt=request.audio_srtp.master_salt,
            video_ssrc=_ssrc(),
            audio_ssrc=_ssrc(),
            accessory_address=accessory_address,
            accessory_video_port=endpoint.video_port,
            accessory_audio_port=endpoint.audio_port,
        )
        while session.audio_ssrc == session.video_ssrc:
            session.audio_ssrc = _ssrc()
        # Registered before prepare, so a disconnect or the gate ends it too.
        self._add(session)
        try:
            await self._run_hook(
                session,
                "prepare",
                self.delegate.handle_prepare(session),
                deadline,
                end_on_failure=True,
                first=True,
            )
        except HookFailed as err:
            return failure_status(hksv3.SetupEndpointsStatus, err.status, error)
        session.state = SessionState.PREPARED
        self._set_timer(
            session,
            self.config.setup_timeout,
            "Not started in time",
            SessionState.PREPARED,
        )
        return session

    async def _set_streaming_control(
        self, value: str, sender_client_addr: Any = None
    ) -> str:
        """Start, change or end a stream, the write response is the status."""
        deadline = self._deadline()
        try:
            request = hksv3.RTPStreamingControl.decode(base64_to_bytes(value))
        except ValueError:
            logger.warning("Ignoring a malformed RTP Streaming Control write")
            status = hksv3.encode_streaming_status(None, hksv3.RTPStreamingStatus.ERROR)
            return to_base64_str(status)
        logger.debug("RTP Streaming Control: %s", request)
        result = await self._streaming_control(request, sender_client_addr, deadline)
        logger.debug("[%s] RTP Streaming Control: %s", request.session_id, result)
        return to_base64_str(hksv3.encode_streaming_status(request.session_id, result))

    async def _streaming_control(
        self,
        request: hksv3.RTPStreamingControl,
        client_addr: Any,
        deadline: float,
    ) -> hksv3.RTPStreamingStatus:
        status = hksv3.RTPStreamingStatus
        session = self._owned(request.session_id, client_addr)
        if session is None:
            return status.UNKNOWN_SESSION
        if request.command == hksv3.RTPStreamingCommand.END:
            if session.state is not SessionState.STARTED:
                self._end(session, "ended before it started")
                return status.NO_SUCH_STREAM
            ended = await self._end_by_request(session, deadline)
            return status.SUCCESS if ended else status.ERROR
        if request.command != hksv3.RTPStreamingCommand.START:
            return status.ERROR
        if session.state is SessionState.NEW or session.busy:
            return status.BUSY
        if not self.active:
            logger.warning("[%s] Streaming is turned off, refusing it", session.id)
            return status.ERROR
        tiers = self._tiers(request)
        if tiers is None:
            return status.ERROR
        session.controller_video_ssrc = request.video_ssrc
        session.controller_audio_ssrc = request.audio_ssrc
        started = session.state is SessionState.STARTED
        if started:
            hook = self._delegate.handle_reconfigure(session, *tiers)
        else:
            hook = self._delegate.handle_start(session, *tiers)
        try:
            await self._run_hook(
                session, "reconfigure" if started else "start", hook, deadline
            )
        except HookFailed as err:
            return failure_status(status, err.status, status.ERROR)
        session.video_tier, session.audio_tier = tiers
        session.state = SessionState.STARTED
        self.keep_alive(session)
        return status.SUCCESS

    def _tiers(
        self, request: hksv3.RTPStreamingControl
    ) -> tuple[hksv3.VideoStreamTier, hksv3.AudioStreamTier | None] | None:
        """The tiers a start picked, None if they are not advertised."""
        video = next(
            (tier for tier in self.tiers if tier.identifier == request.video_tier),
            None,
        )
        audio = self.config.audio
        if video is None or request.video_ssrc is None:
            return None
        if request.audio_tier is None:
            return video, None
        if request.audio_tier != audio.identifier or request.audio_ssrc is None:
            return None
        return video, audio


def _ssrc() -> int:
    # Up to 2^31 - 1, ffmpeg takes the SSRC as a signed int.
    return secrets.randbelow(2**31 - 1) + 1


def _check_srtp(params: hksv3.SRTPParameters) -> None:
    """Accept only AES_CM_128_HMAC_SHA1_80 keys, the guide allows nothing else."""
    if (
        params.crypto_suite != _SRTP_SUITE
        or len(params.master_key) != 16
        or len(params.master_salt) != 14
    ):
        raise ValueError("only AES_CM_128_HMAC_SHA1_80 is supported")


def _answer(session: MultiTierSession) -> hksv3.SetupEndpointsResponse:
    """The successful Setup Endpoints answer of ``session``."""
    return hksv3.SetupEndpointsResponse(
        session.id,
        hksv3.SetupEndpointsStatus.SUCCESS,
        hksv3.EndpointAddress(
            session.accessory_address,
            session.is_ipv6,
            session.accessory_video_port,
            session.accessory_audio_port,
        ),
        hksv3.SRTPParameters(
            _SRTP_SUITE, session.video_srtp_key, session.video_srtp_salt
        ),
        hksv3.SRTPParameters(
            _SRTP_SUITE, session.audio_srtp_key, session.audio_srtp_salt
        ),
        session.video_ssrc,
        session.audio_ssrc,
    )


# Output arguments of FFmpegMultiTierDelegate, each formatted with the
# session values. Video is copied, audio encoded to 16 kHz mono Opus in
# 20 ms frames, which ffmpeg sends with the 48 kHz RTP clock. Video goes out
# from a free port, its announced port is the session's RtcpMonitor.
FFMPEG_VIDEO_OUTPUT = (
    *("-map", "0:v:0", "-an", "-c:v", "copy", "-bsf:v", "dump_extra"),
    *("-payload_type", "{v_payload_type}", "-ssrc", "{v_ssrc}", "-f", "rtp"),
    *("-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80"),
    *("-srtp_out_params", "{v_srtp_key}"),
    "srtp://{host}:{v_port}?rtcpport={v_port}&pkt_size=1190",
)
FFMPEG_AUDIO_OUTPUT = (
    *("-map", "0:a:0", "-c:a", "libopus", "-application", "lowdelay"),
    *("-frame_duration", "20", "-ar", "16000", "-ac", "1", "-b:a", "{a_bitrate}k"),
    *("-payload_type", "{a_payload_type}", "-ssrc", "{a_ssrc}", "-f", "rtp"),
    *("-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80"),
    *("-srtp_out_params", "{a_srtp_key}"),
    "srtp://{host}:{a_port}?rtcpport={a_port}&localrtpport={a_local_port}&pkt_size=188",
)


class FFmpegMultiTierDelegate:
    """Streams each session with its own ffmpeg process.

    Pass it as the ``delegate`` of the ``multi_tier`` option. The command is
    ``ffmpeg``, the ``ffmpeg_input`` arguments, :data:`FFMPEG_VIDEO_OUTPUT`
    and, if the controller wants audio, :data:`FFMPEG_AUDIO_OUTPUT`. It is an
    argument list that no shell sees.

    The video is copied whatever tier the controller picks. A tier change
    keeps the running stream as it is, the delegate does not adapt it to the
    tier.

    The audio output maps the first audio stream of the input, so ffmpeg
    fails on an input without audio; pass ``audio=False`` for one, and
    sessions stream video only.

    ffmpeg does not report the controller's RTCP, so the delegate listens
    for it with an :class:`RtcpMonitor` on the announced video port, which
    keeps the session alive. When ffmpeg exits on its own, the session ends.
    """

    def __init__(
        self, ffmpeg_input: Sequence[str], ffmpeg: str = "ffmpeg", audio: bool = True
    ) -> None:
        self.argv = (ffmpeg, *ffmpeg_input)
        self.audio = audio

    async def handle_prepare(self, session: MultiTierSession) -> None:
        """Listen for the controller's RTCP on the announced video port."""
        session.data["rtcp_monitor"] = await open_rtcp_monitor(session)

    def command(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> list[str]:
        """The argument list for a session."""
        values = {
            "host": f"[{session.address}]" if session.is_ipv6 else session.address,
            "v_port": session.video_port,
            "a_port": session.audio_port,
            "a_local_port": session.accessory_audio_port,
            "v_srtp_key": to_base64_str(
                session.video_srtp_key + session.video_srtp_salt
            ),
            "a_srtp_key": to_base64_str(
                session.audio_srtp_key + session.audio_srtp_salt
            ),
            "v_ssrc": session.video_ssrc,
            "a_ssrc": session.audio_ssrc,
            "v_payload_type": hksv3.VIDEO_PAYLOAD_TYPE,
            "a_payload_type": hksv3.AUDIO_PAYLOAD_TYPE,
            "a_bitrate": audio_tier.target_bitrate // 1000 if audio_tier else 0,
        }
        with_audio = self.audio and audio_tier is not None
        output = FFMPEG_VIDEO_OUTPUT + (FFMPEG_AUDIO_OUTPUT if with_audio else ())
        return [*self.argv, *(arg.format(**values) for arg in output)]

    async def handle_start(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Start ffmpeg for the session."""
        cmd = self.command(session, video_tier, audio_tier)
        logger.debug("[%s] Executing: %s", session.id, _redact_command(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        session.data["process"] = process
        session.data["watcher"] = asyncio.ensure_future(_watch(session, process))
        logger.info("[%s] Started stream process %d", session.id, process.pid)

    async def handle_reconfigure(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Keep the running stream.

        A copied stream cannot change its tier, and a new ffmpeg with the
        session's SSRC and SRTP key would repeat the keystream.
        """
        logger.debug("[%s] Keeping the stream for tier %s", session.id, video_tier)

    async def handle_end(self, session: MultiTierSession) -> None:
        """Stop the session's ffmpeg, killing it after two seconds."""
        monitor: RtcpMonitor | None = session.data.pop("rtcp_monitor", None)
        if monitor is not None:
            monitor.close()
        process = session.data.pop("process", None)
        if process is None:
            return
        logger.info("[%s] Stopping the stream process", session.id)
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            logger.warning("[%s] Killing the stream process", session.id)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        await session.data.pop("watcher")


def _redact_command(cmd: list[str]) -> str:
    """Join the command for logging, without the SRTP keys and input secrets."""
    return " ".join(
        "<redacted>" if previous == "-srtp_out_params" else _redact_arg(arg)
        for previous, arg in zip(["", *cmd], cmd, strict=False)
    )


# User info runs up to the last "@" of the token, it may hold any character.
_URL = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?:\S*@)?(?P<rest>[^\s'\"]*)", re.IGNORECASE
)
# An argument that is a URL is one token, its user info may hold spaces.
_URL_ARG = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?:.*@)?(?P<rest>.*)", re.IGNORECASE | re.DOTALL
)


def _redact_url(match: re.Match[str]) -> str:
    # Keep the host and port, drop user info, path and query.
    host, *path = re.split(r"[/?#]", match["rest"], maxsplit=1)
    return f"{match['scheme']}{host}{'/<redacted>' if path else ''}"


def _redact_arg(arg: str) -> str:
    """Redact a command argument, a URL argument as a whole."""
    if match := _URL_ARG.fullmatch(arg):
        return _redact_url(match)
    return _redact_urls(arg)


def _redact_urls(text: str) -> str:
    """Drop user info, path and query of each URL in ``text``, e.g. for logs.

    Camera URLs often carry credentials or access tokens there.
    """
    return _URL.sub(_redact_url, text)


async def _watch(
    session: MultiTierSession, process: asyncio.subprocess.Process
) -> None:
    """Log ffmpeg's output and end the session with ffmpeg."""
    output = asyncio.ensure_future(_log_output(session, process))
    code = await process.wait()
    await output
    if session.data.get("process") is process:
        logger.warning("[%s] Stream process exited with %s", session.id, code)
        session.end("the stream process exited")


async def _log_output(
    session: MultiTierSession, process: asyncio.subprocess.Process
) -> None:
    # Reading the output also keeps the pipe from filling up.
    if process.stderr is None:
        return
    async for line in process.stderr:
        # Token based, user info with a raw space is not covered.
        text = _redact_urls(line.decode(errors="replace").rstrip())
        logger.debug("[%s] %s", session.id, text)
