"""WebRTC streaming of a HomeKit Secure Video 17.99 camera.

Developer Preview: HomeKit Secure Video 17.99 is a preview of the guide, so
this API is experimental and may change.

:class:`pyhap.camera.Camera` adds a :class:`WebRTCController` per sensor for
its ``webrtc`` option, next to the multi-tier RTP service. Controllers view
remotely through it. The accessory offers, the controller answers, may
renegotiate and update its SFrame keys, and ends the session. A
:class:`WebRTCDelegate` runs the WebRTC media. It shares
:class:`pyhap.hksv3.WebRTCOffer`, :class:`pyhap.hksv3.WebRTCReofferAnswer`,
:class:`pyhap.hksv3.IceCandidate` and :class:`pyhap.hksv3.SFrameKey` with
the HAP side.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from pyhap import hksv3
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
    from pyhap.camera_multi_tier import MultiTierController

__all__ = [
    "WEBRTC_MIN_SESSIONS",
    "WebRTCConfig",
    "WebRTCController",
    "WebRTCDelegate",
    "WebRTCSession",
]

logger = logging.getLogger(__name__)

# WebRTC sessions per sensor the guide requires at least.
WEBRTC_MIN_SESSIONS = 6

_R = TypeVar("_R")
_STATUS = hksv3.WebRTCStreamingStatus


@dataclass(eq=False, kw_only=True)
class WebRTCSession(StreamSession):
    """A WebRTC session, from the Solicit Offer write until it ends.

    ``video_tier`` and ``audio_tier`` are what the offer advertises.
    ``sframe`` tells whether the media is SFrame encrypted: unless the
    controller explicitly turned it off, as the Scrypted HomeKit plugin found
    remote viewing needs it even when the controller leaves the option out.
    """

    sframe: bool
    video_tier: hksv3.VideoStreamTier
    audio_tier: hksv3.AudioStreamTier


class WebRTCDelegate(Protocol):
    """Runs the WebRTC media of the sessions.

    Each hook may raise :class:`pyhap.camera_stream.StreamError` to answer
    with a status, any other exception answers with an error. A hook that
    takes longer than ``hook_timeout`` is cancelled and its session ends.

    An answered session has no timeout of its own. When the home hub's
    connection closes its sessions end, but remote viewers share that
    connection, so a viewer that leaves closes nothing on the HAP side. The
    delegate must call ``session.end()`` when the media connection fails or
    the viewer leaves, e.g. on ICE or consent failure or a DTLS close, or the
    session keeps one of the sensor's slots.
    """

    async def handle_solicit_offer(
        self,
        session: WebRTCSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier,
    ) -> hksv3.WebRTCOffer:
        """Create the offer of a new session.

        Offer ``session.sensor`` in the given tiers, with the payload types of
        :mod:`pyhap.hksv3`: H.265 video limited by ``a=rid`` to the tier's
        size, frame rate and peak bit rate, and Opus audio. The SDP carries
        all ICE candidates. Return an SFrame key exactly if ``session.sframe``.
        Bind what the offer needs, but start the media only once answered.
        """

    async def handle_answer(
        self,
        session: WebRTCSession,
        sdp: str,
        candidates: list[hksv3.IceCandidate],
    ) -> None:
        """Apply the controller's answer and its ICE candidates."""

    async def handle_reoffer(
        self,
        session: WebRTCSession,
        sdp: str,
        options: hksv3.WebRTCOfferOptions,
    ) -> hksv3.WebRTCReofferAnswer:
        """Answer the controller's new offer for an active session."""

    async def handle_update_session(
        self,
        session: WebRTCSession,
        add_keys: list[hksv3.SFrameKey],
        remove_kids: list[int],
    ) -> None:
        """Change the SFrame keys the controller sends with."""

    async def handle_end(self, session: WebRTCSession) -> None:
        """Release a session that ended, whatever state it reached.

        Called exactly once for every session ``handle_solicit_offer`` saw,
        also after a failed offer or answer and for an offer never answered.
        """


@dataclass(frozen=True, kw_only=True)
class WebRTCConfig:
    """The ``webrtc`` option of :class:`pyhap.camera.Camera`.

    ``delegate`` runs the media, without one every offer fails.
    ``max_sessions`` per sensor is at least six, as the guide requires.
    ``max_sessions_per_connection`` limits one connection, by default to
    ``max_sessions``, since remote viewers share the home hub's connection. A
    delegate hook gets ``hook_timeout`` seconds. A session the controller
    did not answer within ``answer_timeout`` seconds ends. The offer
    advertises the sensor's video tier of ``remote_quality``, the lowest tier
    if it has none. Medium keeps 4K off the remote path. The Scrypted HomeKit
    plugin offers the first tier.
    """

    delegate: WebRTCDelegate | None = None
    max_sessions: int = WEBRTC_MIN_SESSIONS
    max_sessions_per_connection: int | None = None
    hook_timeout: float = DEFAULT_HOOK_TIMEOUT
    answer_timeout: float = 30.0
    remote_quality: hksv3.VideoQuality | int = hksv3.VideoQuality.MEDIUM

    @classmethod
    def from_option(cls, value: WebRTCConfig | Mapping[str, Any]) -> WebRTCConfig:
        """Take the option as it is or build it from a dict of its fields.

        :raises ValueError: For an unknown key.
        """
        return config_from_option(cls, value, "webrtc")

    def __post_init__(self) -> None:
        check_hook_timeout(self.hook_timeout)
        if self.max_sessions < WEBRTC_MIN_SESSIONS:
            raise ValueError(
                f"webrtc max_sessions must be at least {WEBRTC_MIN_SESSIONS}"
            )
        per_connection = self.max_sessions_per_connection
        if per_connection is not None and not 0 < per_connection <= self.max_sessions:
            raise ValueError(
                "webrtc max_sessions_per_connection must be 1 to max_sessions"
            )
        if self.answer_timeout <= 0:
            raise ValueError("webrtc answer_timeout must be positive")
        if self.remote_quality not in set(hksv3.VideoQuality):
            raise ValueError(f"Unknown webrtc remote_quality {self.remote_quality}")


def _remote_tier(
    tiers: list[hksv3.VideoStreamTier], quality: hksv3.VideoQuality | int
) -> hksv3.VideoStreamTier:
    """The smallest tier of ``quality``, else the smallest tier."""
    matching = [tier for tier in tiers if tier.quality == quality] or tiers
    return min(matching, key=lambda tier: (tier.width * tier.height, tier.identifier))


def _decode(name: str, decode: Callable[[bytes], _R], value: str) -> _R | None:
    try:
        request = decode(base64_to_bytes(value))
    except ValueError as ex:
        logger.warning("Ignoring a malformed WebRTC %s write: %s", name, ex)
        return None
    logger.debug("WebRTC %s for %s", name, getattr(request, "session_id", None))
    return request


def _status(session_id: UUID | None, status: hksv3.WebRTCStreamingStatus) -> str:
    return to_base64_str(hksv3.WebRTCSessionStatus(session_id, status).encode())


class WebRTCController(StreamController[WebRTCSession]):
    """The Camera WebRTC Stream Management service of one sensor."""

    kind = "WebRTC"

    def __init__(
        self,
        camera: Camera,
        multi_tier: MultiTierController,
        config: WebRTCConfig,
    ) -> None:
        super().__init__(
            camera,
            multi_tier.sensor,
            config.hook_timeout,
            config.max_sessions,
            config.max_sessions
            if config.max_sessions_per_connection is None
            else config.max_sessions_per_connection,
        )
        self.config = config
        self.delegate = config.delegate
        self.audio_tier = multi_tier.config.audio
        self.video_tier = _remote_tier(multi_tier.tiers, config.remote_quality)
        service = self.service = camera.add_preload_service(
            "CameraWebRTCStreamManagement", unique_id=str(self.sensor.sensor_uuid)
        )
        # The WebRTC service goes with the multi-tier service of its sensor.
        service.add_linked_service(multi_tier.service)
        multi_tier.service.add_linked_service(service)
        # The same tiers as the multi-tier service.
        for name, source in (
            ("SensorUUID", "SensorUUID"),
            ("WebRTCSupportedVideoStreamTiers", "SupportedVideoStreamTiers"),
            ("WebRTCSupportedAudioStreamTiers", "SupportedAudioStreamTiers"),
        ):
            value = multi_tier.service.get_characteristic(source).value
            service.configure_char(name, value=value)
        self.streaming_enabled = service.configure_char("StreamingEnabled", value=True)
        self._session_count = service.get_characteristic("WebRTCNumberOfActiveSessions")
        for name, setter in (
            ("WebRTCSolicitOffer", self._set_solicit_offer),
            ("WebRTCProvideAnswer", self._set_provide_answer),
            ("WebRTCStreamingControl", self._set_streaming_control),
            ("WebRTCReoffer", self._set_reoffer),
            ("WebRTCUpdateSession", self._set_update_session),
        ):
            # The requests carry SDPs with ICE passwords and SFrame keys, so
            # neither they nor the answers are shared with other connections.
            service.configure_char(
                name, setter_callback=setter
            ).read_back_response = True

    def _refusal(self) -> hksv3.WebRTCSolicitOfferStatus | None:
        """Why the sensor may not stream now, None if it may."""
        if not self.camera.homekit_camera_active:
            return hksv3.WebRTCSolicitOfferStatus.PRIVACY_MODE_ACTIVE
        if not (self.camera.streaming_allowed and bool(self.streaming_enabled.value)):
            return hksv3.WebRTCSolicitOfferStatus.ERROR
        return None

    def update_status(self) -> None:
        """End the sessions if the sensor may no longer stream."""
        if self._refusal() is not None:
            self._end_sessions("streaming was turned off")

    def _session_ended(self, session: WebRTCSession, previous: SessionState) -> None:
        if previous is SessionState.ACTIVE:
            self._update_session_count()

    def _update_session_count(self) -> None:
        """Set and notify Number of Active Sessions, answered sessions count."""
        count = sum(
            session.state is SessionState.ACTIVE for session in self._sessions.values()
        )
        self._session_count.set_value(min(count, 255))

    @property
    def _delegate(self) -> WebRTCDelegate:
        # Only a controller with a delegate has sessions.
        return cast("WebRTCDelegate", self.delegate)

    async def _release_hook(self, session: WebRTCSession) -> None:
        await self._delegate.handle_end(session)

    async def _set_solicit_offer(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        deadline = self._deadline()
        request = _decode("Solicit Offer", hksv3.WebRTCSolicitOffer.decode, value)
        if request is None:
            response = hksv3.WebRTCSolicitOfferResponse(
                hksv3.WebRTCSolicitOfferStatus.ERROR
            )
        else:
            response = await self._solicit_offer(
                request.options, sender_client_addr, deadline
            )
        logger.debug(
            "[%s] WebRTC Solicit Offer: %s", response.session_id, response.status
        )
        return to_base64_str(response.encode())

    async def _solicit_offer(
        self,
        options: hksv3.WebRTCOfferOptions,
        client_addr: tuple[str, int] | None,
        deadline: float,
    ) -> hksv3.WebRTCSolicitOfferResponse:
        error = hksv3.WebRTCSolicitOfferStatus.ERROR
        refusal = self._refusal()
        if refusal is not None:
            logger.info("Refusing a WebRTC offer, streaming is off: %s", refusal)
            return hksv3.WebRTCSolicitOfferResponse(refusal)
        # The guide has no Busy for an offer.
        if no_room := self._no_room(client_addr):
            logger.warning("%s, refusing an offer", no_room)
            return hksv3.WebRTCSolicitOfferResponse(error)
        if self.delegate is None:
            logger.warning("No WebRTC delegate, refusing an offer")
            return hksv3.WebRTCSolicitOfferResponse(error)
        session = WebRTCSession(
            id=uuid4(),
            sensor=self.sensor,
            client_addr=client_addr,
            sframe=options.sframe_enabled is not False,
            video_tier=self.video_tier,
            audio_tier=self.audio_tier,
        )
        self._add(session)
        self._set_timer(
            session,
            self.config.answer_timeout,
            "No WebRTC answer in time",
            SessionState.NEW,
            SessionState.SOLICITED,
            SessionState.ANSWERING,
        )
        hook = self.delegate.handle_solicit_offer(
            session, session.video_tier, session.audio_tier
        )
        try:
            offer = await self._run_hook(
                session,
                "solicit offer",
                hook,
                deadline,
                end_on_failure=True,
                first=True,
            )
        except HookFailed as err:
            return hksv3.WebRTCSolicitOfferResponse(
                failure_status(hksv3.WebRTCSolicitOfferStatus, err.status, error)
            )
        if not isinstance(offer, hksv3.WebRTCOffer):
            logger.error("[%s] The offer is not a WebRTCOffer: %r", session.id, offer)
            self._end(session, "the offer is not a WebRTCOffer")
            return hksv3.WebRTCSolicitOfferResponse(error)
        if (offer.sframe is not None) != session.sframe:
            logger.error("[%s] The offer's SFrame key does not fit", session.id)
            self._end(session, "the offer's SFrame key does not fit")
            return hksv3.WebRTCSolicitOfferResponse(error)
        session.state = SessionState.SOLICITED
        return hksv3.WebRTCSolicitOfferResponse(
            hksv3.WebRTCSolicitOfferStatus.SUCCESS,
            session.id,
            offer.sdp,
            [],
            offer.sframe,
        )

    async def _set_provide_answer(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        deadline = self._deadline()
        request = _decode("Provide Answer", hksv3.WebRTCProvideAnswer.decode, value)
        if request is None:
            return _status(None, _STATUS.ERROR)
        session = self._owned(request.session_id, sender_client_addr)
        if session is None:
            return _status(request.session_id, _STATUS.UNKNOWN_SESSION)
        if session.state is SessionState.ANSWERING:
            # Another answer is being applied.
            return _status(session.id, _STATUS.BUSY)
        if session.state is not SessionState.SOLICITED:
            # Answered already.
            return _status(session.id, _STATUS.ERROR)
        session.state = SessionState.ANSWERING
        hook = self._delegate.handle_answer(
            session, request.sdp_answer, request.candidates
        )
        try:
            await self._run_hook(session, "answer", hook, deadline, end_on_failure=True)
        except HookFailed as err:
            return _status(
                session.id, failure_status(_STATUS, err.status, _STATUS.ERROR)
            )
        # Only now, the expiry ends a session whose answer did not apply.
        self._cancel_timer(session)
        session.state = SessionState.ACTIVE
        self._update_session_count()
        return _status(session.id, _STATUS.SUCCESS)

    async def _set_streaming_control(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        deadline = self._deadline()
        request = _decode(
            "Streaming Control", hksv3.WebRTCStreamingControl.decode, value
        )
        if request is None:
            return _status(None, _STATUS.ERROR)
        session = self._owned(request.session_id, sender_client_addr)
        if session is None:
            return _status(request.session_id, _STATUS.UNKNOWN_SESSION)
        if request.command != hksv3.WebRTCStreamingCommand.END:
            return _status(session.id, _STATUS.ERROR)
        ended = await self._end_by_request(session, deadline)
        return _status(session.id, _STATUS.SUCCESS if ended else _STATUS.ERROR)

    def _active_session(
        self, session_id: UUID, client_addr: tuple[str, int] | None
    ) -> WebRTCSession | hksv3.WebRTCStreamingStatus:
        """The session to renegotiate or update, else the status to answer."""
        session = self._owned(session_id, client_addr)
        if session is None:
            return _STATUS.UNKNOWN_SESSION
        if session.state is not SessionState.ACTIVE:
            return _STATUS.ERROR
        if session.busy:
            return _STATUS.BUSY
        return session

    async def _set_reoffer(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        deadline = self._deadline()
        request = _decode("Reoffer", hksv3.WebRTCReoffer.decode, value)
        if request is None:
            response = hksv3.WebRTCReofferResponse(None, _STATUS.ERROR)
        else:
            response = await self._reoffer(request, sender_client_addr, deadline)
        return to_base64_str(response.encode())

    async def _reoffer(
        self,
        request: hksv3.WebRTCReoffer,
        client_addr: tuple[str, int] | None,
        deadline: float,
    ) -> hksv3.WebRTCReofferResponse:
        session = self._active_session(request.session_id, client_addr)
        if not isinstance(session, WebRTCSession):
            return hksv3.WebRTCReofferResponse(request.session_id, session)
        hook = self._delegate.handle_reoffer(
            session, request.sdp_offer, request.options
        )
        try:
            answer = await self._run_hook(session, "reoffer", hook, deadline)
        except HookFailed as err:
            return hksv3.WebRTCReofferResponse(
                session.id, failure_status(_STATUS, err.status, _STATUS.ERROR)
            )
        # Like a failed hook, a bad answer leaves the session as it was.
        if not isinstance(answer, hksv3.WebRTCReofferAnswer):
            logger.error(
                "[%s] The reoffer answer is not a WebRTCReofferAnswer: %r",
                session.id,
                answer,
            )
            return hksv3.WebRTCReofferResponse(session.id, _STATUS.ERROR)
        if answer.sframe is not None and not session.sframe:
            logger.error(
                "[%s] The reoffer answer's SFrame key does not fit", session.id
            )
            return hksv3.WebRTCReofferResponse(session.id, _STATUS.ERROR)
        return hksv3.WebRTCReofferResponse(
            session.id, _STATUS.SUCCESS, answer.sdp, answer.sframe
        )

    async def _set_update_session(
        self, value: str, sender_client_addr: tuple[str, int] | None = None
    ) -> str:
        deadline = self._deadline()
        request = _decode("Update Session", hksv3.WebRTCUpdateSession.decode, value)
        if request is None:
            return _status(None, _STATUS.ERROR)
        session = self._active_session(request.session_id, sender_client_addr)
        if not isinstance(session, WebRTCSession):
            return _status(request.session_id, session)
        hook = self._delegate.handle_update_session(
            session, request.add_keys, request.remove_kids
        )
        try:
            await self._run_hook(session, "update session", hook, deadline)
        except HookFailed as err:
            return _status(
                session.id, failure_status(_STATUS, err.status, _STATUS.ERROR)
            )
        return _status(session.id, _STATUS.SUCCESS)
