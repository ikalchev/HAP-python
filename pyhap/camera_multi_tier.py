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
from pyhap.util import to_base64_str

if TYPE_CHECKING:
    from pyhap.camera import Camera

__all__ = [
    "MULTI_TIER_MIN_SESSIONS",
    "MultiTierConfig",
    "MultiTierController",
    "MultiTierDelegate",
    "MultiTierSession",
]

logger = logging.getLogger(__name__)

# Multi-tier sessions per sensor the guide requires at least.
MULTI_TIER_MIN_SESSIONS = 5


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
    of them. A delegate hook gets ``hook_timeout`` seconds.
    """

    delegate: MultiTierDelegate | None = None
    audio: hksv3.AudioStreamTier = hksv3.DEFAULT_AUDIO_TIER
    max_sessions: int = MULTI_TIER_MIN_SESSIONS
    max_sessions_per_connection: int = 3
    hook_timeout: float = DEFAULT_HOOK_TIMEOUT

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

    @property
    def _delegate(self) -> MultiTierDelegate:
        # Only a controller with a delegate has sessions.
        return cast("MultiTierDelegate", self.delegate)

    async def _release_hook(self, session: MultiTierSession) -> None:
        await self._delegate.handle_end(session)
