"""WebRTC streaming of a HomeKit Secure Video 17.99 camera.

Developer Preview: HomeKit Secure Video 17.99 is a preview of the guide, so
this API is experimental and may change.

:class:`pyhap.camera.Camera` adds a :class:`WebRTCController` per sensor for
its ``webrtc`` option, next to the multi-tier RTP service. Controllers view
remotely through it. The accessory offers, the controller answers, may
renegotiate and update its SFrame keys, and ends the session. A
:class:`WebRTCDelegate` runs the WebRTC media; it shares
:class:`pyhap.hksv3.WebRTCOffer`, :class:`pyhap.hksv3.WebRTCReofferAnswer`,
:class:`pyhap.hksv3.IceCandidate` and :class:`pyhap.hksv3.SFrameKey` with
the HAP side.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from pyhap import hksv3
from pyhap.camera_stream import (
    DEFAULT_HOOK_TIMEOUT,
    StreamController,
    StreamSession,
    check_hook_timeout,
    config_from_option,
)

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
    delegate hook gets ``hook_timeout`` seconds. The offer
    advertises the sensor's video tier of ``remote_quality``, the lowest tier
    if it has none. Medium keeps 4K off the remote path; the Scrypted HomeKit
    plugin offers the first tier.
    """

    delegate: WebRTCDelegate | None = None
    max_sessions: int = WEBRTC_MIN_SESSIONS
    max_sessions_per_connection: int | None = None
    hook_timeout: float = DEFAULT_HOOK_TIMEOUT
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
        if self.remote_quality not in set(hksv3.VideoQuality):
            raise ValueError(f"Unknown webrtc remote_quality {self.remote_quality}")


def remote_tier(
    tiers: list[hksv3.VideoStreamTier], quality: hksv3.VideoQuality | int
) -> hksv3.VideoStreamTier:
    """The smallest tier of ``quality``, else the smallest tier."""
    matching = [tier for tier in tiers if tier.quality == quality] or tiers
    return min(matching, key=lambda tier: (tier.width * tier.height, tier.identifier))


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
        self.video_tier = remote_tier(multi_tier.tiers, config.remote_quality)
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

    def refusal(self) -> hksv3.WebRTCSolicitOfferStatus | None:
        """Why the sensor may not stream now, None if it may."""
        if not self.camera.homekit_camera_active:
            return hksv3.WebRTCSolicitOfferStatus.PRIVACY_MODE_ACTIVE
        if not (self.camera.streaming_allowed and bool(self.streaming_enabled.value)):
            return hksv3.WebRTCSolicitOfferStatus.ERROR
        return None

    def update_status(self) -> None:
        """End the sessions if the sensor may no longer stream."""
        if self.refusal() is not None:
            self._end_sessions("streaming was turned off")

    @property
    def _delegate(self) -> WebRTCDelegate:
        # Only a controller with a delegate has sessions.
        return cast("WebRTCDelegate", self.delegate)

    async def _release_hook(self, session: WebRTCSession) -> None:
        await self._delegate.handle_end(session)
