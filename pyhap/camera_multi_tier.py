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

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import logging
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
    "MULTI_TIER_MIN_SESSIONS",
    "MultiTierConfig",
    "MultiTierController",
    "MultiTierDelegate",
    "MultiTierSession",
]

logger = logging.getLogger(__name__)

# Multi-tier sessions per sensor the guide requires at least.
MULTI_TIER_MIN_SESSIONS = 5
# Seconds a started session lives without keep_alive().
DEFAULT_IDLE_TIMEOUT = 30.0
_SRTP_SUITE = hksv3.SRTPCryptoSuite.AES_CM_128_HMAC_SHA1_80
_STATUS = hksv3.RTPStreamingStatus


@dataclass(eq=False, kw_only=True)
class MultiTierSession(StreamSession):
    """A multi-tier RTP session a controller set up with Setup Endpoints.

    ``address`` and the ports are where the controller receives, with the
    SRTP keys it sent. The accessory sends from ``accessory_address`` and the
    ``accessory_*_port`` values, by default its address on the controller's
    connection and the controller's port numbers. ``handle_prepare`` may
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
        packets. Call it in the event loop.
        """
        controller = self._controller
        if isinstance(controller, MultiTierController):
            controller._keep_alive(self)


class MultiTierDelegate(Protocol):
    """Sends the media of multi-tier RTP sessions.

    Each hook may raise :class:`pyhap.camera_stream.StreamError` to answer
    with a status, any other exception answers with an error. A hook that
    takes longer than ``hook_timeout`` is cancelled and its session ends.

    With the default ``idle_timeout`` a started session ends after 30 s unless
    the delegate calls :meth:`MultiTierSession.keep_alive`, e.g. for each
    RTCP packet of the controller.
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
    requires. One connection has at most ``max_sessions_per_connection`` of
    them, by default three. That is below the guide's "at least five
    simultaneous sessions" per service on purpose: sessions one connection
    leaks cannot take the room of other controllers, and remote viewers,
    who share the home hub's connection, stream over WebRTC. Raise it, at
    most to ``max_sessions``, to let one connection have more.
    A delegate hook gets ``hook_timeout`` seconds. A session must start
    within ``setup_timeout`` seconds of its Setup Endpoints answer.
    A started session ends unless :meth:`MultiTierSession.keep_alive` is
    called at least every ``idle_timeout`` seconds, e.g. for each RTCP packet
    of the controller. None turns that off.
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


def _supported_audio_tiers(audio: hksv3.AudioStreamTier) -> bytes:
    """The Supported Audio Stream Tiers value, exactly one Opus tier."""
    return hksv3.SupportedAudioStreamTiers(
        hksv3.AudioCodec.OPUS, hksv3.AUDIO_PAYLOAD_TYPE, [audio]
    ).encode()


def _supported_video_tiers(tiers: list[hksv3.VideoStreamTier]) -> bytes:
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
            ("SupportedVideoStreamTiers", _supported_video_tiers(self.tiers)),
            ("SupportedAudioStreamTiers", _supported_audio_tiers(config.audio)),
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
        # Neither the request nor the status is shared with other connections.
        service.configure_char(
            "RTPStreamingControl", setter_callback=self._set_streaming_control
        ).read_back_response = True

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

    def _keep_alive(self, session: MultiTierSession) -> None:
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
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
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
        client_addr: tuple[str, int] | None,
        deadline: float,
    ) -> MultiTierSession | hksv3.SetupEndpointsStatus:
        session_id = request.session_id
        endpoint = request.address
        error = hksv3.SetupEndpointsStatus.ERROR
        try:
            address = str(
                parse_setup_address(endpoint.address.encode("utf-8"), endpoint.is_ipv6)
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
            # Busy asks to try again, a closed service never takes one.
            return error if self._closed else hksv3.SetupEndpointsStatus.BUSY
        if self.delegate is None:
            logger.warning("[%s] No multi-tier delegate, refusing", session_id)
            return error
        accessory_address = self.camera._stream_address_for(
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
                return error if self._closed else hksv3.SetupEndpointsStatus.BUSY
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
        ports = session.accessory_video_port, session.accessory_audio_port
        if not all(map(_valid_port, ports)):
            logger.error(
                "[%s] The prepare hook set invalid ports %s", session_id, ports
            )
            self._end(session, "the prepare hook set invalid ports")
            return error
        if not _valid_address(session.accessory_address, session.is_ipv6):
            logger.error(
                "[%s] The accessory address %r is invalid",
                session_id,
                session.accessory_address,
            )
            self._end(session, "the accessory address is invalid")
            return error
        session.state = SessionState.PREPARED
        self._set_timer(
            session,
            self.config.setup_timeout,
            "Not started in time",
            SessionState.PREPARED,
        )
        return session

    async def _set_streaming_control(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        """Start, change or end a stream, the write response is the status."""
        deadline = self._deadline()
        try:
            request = hksv3.RTPStreamingControl.decode(base64_to_bytes(value))
        except ValueError:
            logger.warning("Ignoring a malformed RTP Streaming Control write")
            status = hksv3.encode_streaming_status(None, _STATUS.ERROR)
            return to_base64_str(status)
        logger.debug("RTP Streaming Control: %s", request)
        result = await self._streaming_control(request, sender_client_addr, deadline)
        logger.debug("[%s] RTP Streaming Control: %s", request.session_id, result)
        return to_base64_str(hksv3.encode_streaming_status(request.session_id, result))

    async def _streaming_control(
        self,
        request: hksv3.RTPStreamingControl,
        client_addr: tuple[str, int] | None,
        deadline: float,
    ) -> hksv3.RTPStreamingStatus:
        session = self._owned(request.session_id, client_addr)
        if session is None:
            return _STATUS.UNKNOWN_SESSION
        if request.command == hksv3.RTPStreamingCommand.END:
            if session.state is not SessionState.STARTED:
                self._end(session, "ended before it started")
                return _STATUS.NO_SUCH_STREAM
            ended = await self._end_by_request(session, deadline)
            return _STATUS.SUCCESS if ended else _STATUS.ERROR
        if request.command != hksv3.RTPStreamingCommand.START:
            return _STATUS.ERROR
        if session.state is SessionState.NEW or session.busy:
            return _STATUS.BUSY
        if not self.active:
            logger.warning("[%s] Streaming is turned off, refusing it", session.id)
            return _STATUS.ERROR
        tiers = self._tiers(request)
        if tiers is None:
            return _STATUS.ERROR
        previous_ssrcs = session.controller_video_ssrc, session.controller_audio_ssrc
        session.controller_video_ssrc = request.video_ssrc
        session.controller_audio_ssrc = request.audio_ssrc
        started = session.state is SessionState.STARTED
        if started:
            hook = self._delegate.handle_reconfigure(session, *tiers)
        else:
            # The setup timer must not end the session while it starts.
            self._cancel_timer(session)
            hook = self._delegate.handle_start(session, *tiers)
        try:
            await self._run_hook(
                session, "reconfigure" if started else "start", hook, deadline
            )
        except HookFailed as err:
            session.controller_video_ssrc, session.controller_audio_ssrc = (
                previous_ssrcs
            )
            if not session.ended and session.state is SessionState.PREPARED:
                self._set_timer(
                    session,
                    self.config.setup_timeout,
                    "Not started in time",
                    SessionState.PREPARED,
                )
            return failure_status(_STATUS, err.status, _STATUS.ERROR)
        session.video_tier, session.audio_tier = tiers
        session.state = SessionState.STARTED
        self._keep_alive(session)
        return _STATUS.SUCCESS

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


def _valid_port(port: object) -> bool:
    return type(port) is int and 0 < port <= 0xFFFF


def _valid_address(address: object, is_ipv6: bool) -> bool:
    """Whether ``address`` is a plain IP address string of the session's family.

    Like the controller's address, it must not carry an IPv6 scope ID. An
    IPv4-mapped IPv6 address does not count as IPv6.
    """
    if not isinstance(address, str):
        return False
    try:
        parsed = parse_setup_address(address.encode("utf-8"), is_ipv6)
    except ValueError:
        return False
    return not (isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped)


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
