"""Run the media of multi-tier RTP sessions from shared media sources.

:class:`MultiTierMediaDelegate` is a
:class:`pyhap.camera_multi_tier.MultiTierDelegate` with one
:class:`~pyhap.media.multi_tier.MultiTierSender` per session. It leases
sources from the same provider and pool as
:class:`~pyhap.media.delegate.WebRTCMediaDelegate`, so a camera channel is
pulled once for all local and remote sessions::

    worker = MediaWorker()
    provider = FFmpegSourceProvider({"high": [...], "medium": [...]}, worker=worker)
    camera = Camera(
        {
            ...,
            "multi_tier": {"delegate": MultiTierMediaDelegate(provider)},
            "webrtc": {"delegate": WebRTCMediaDelegate(provider)},
        },
        driver,
        "Camera",
    )

Setup Endpoints binds the session's ports in the pool's worker (or in
process) and announces them; the start leases the source of the tier's
channel, starts the sender on it and pulls the channel if no other session
does. A start with another tier moves the session to that tier's channel;
SSRCs, sequence numbers and SRTP state continue. The controller's RTCP keeps
the session alive (:meth:`~pyhap.camera_multi_tier.MultiTierSession.keep_alive`);
a sender that ends on its own ends its HAP session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyhap import hksv3
from pyhap.camera_stream import StreamError

from .pool import MultiTierMediaSender, SourceLease, SourcePool

if TYPE_CHECKING:
    from pyhap.camera_multi_tier import MultiTierSession

logger = logging.getLogger(__name__)

_SETUP_ERROR = hksv3.SetupEndpointsStatus.ERROR
_ERROR = hksv3.RTPStreamingStatus.ERROR

MultiTierSourceProvider = Callable[
    ["MultiTierSession", hksv3.VideoStreamTier, "hksv3.AudioStreamTier | None"],
    Awaitable[SourceLease],
]


@runtime_checkable
class PooledSourceProvider(Protocol):
    """A source provider that names its pool, e.g. an
    :class:`~pyhap.media.ffmpeg.FFmpegSourceProvider`.
    """

    pool: SourcePool


# MultiTierSender options a delegate user may set; the session sets the rest.
_SENDER_OPTIONS = frozenset(
    {
        "local_video_port",
        "local_audio_port",
        "max_payload",
        "idle_timeout",
        "keyframe_request_interval",
        "rtcp_notify_interval",
    }
)


@dataclass(eq=False)
class _Media:
    """What the delegate holds for one session."""

    session: MultiTierSession
    sender: MultiTierMediaSender
    lease: SourceLease | None = None
    finished: bool = False


class MultiTierMediaDelegate:
    """A ``MultiTierDelegate`` that streams from a :class:`SourcePool`.

    ``source_provider`` leases the source of a tier's channel, for example
    an :class:`~pyhap.media.ffmpeg.FFmpegSourceProvider`; ``pool`` is the
    pool its leases come from and defaults to the provider's ``pool``.
    ``sender_options`` go to every
    :class:`~pyhap.media.multi_tier.MultiTierSender`, e.g. ``idle_timeout``
    and ``max_payload``.

    A media worker should be started before the first session: starting it
    takes longer than a HAP hook may.
    """

    def __init__(
        self,
        source_provider: MultiTierSourceProvider,
        pool: SourcePool | None = None,
        *,
        sender_options: Mapping[str, Any] | None = None,
    ) -> None:
        """Configure the delegate."""
        if pool is None and isinstance(source_provider, PooledSourceProvider):
            pool = source_provider.pool
        if not isinstance(pool, SourcePool):
            raise TypeError("MultiTierMediaDelegate needs the provider's SourcePool")
        self.source_provider = source_provider
        self.pool = pool
        self.sender_options = dict(sender_options or {})
        if unknown := sorted(set(self.sender_options) - _SENDER_OPTIONS):
            raise TypeError(f"Unknown sender_options: {', '.join(unknown)}")
        self._media: dict[MultiTierSession, _Media] = {}

    @property
    def session_count(self) -> int:
        """Sessions the delegate holds media for."""
        return len(self._media)

    def sender(self, session: MultiTierSession) -> MultiTierMediaSender | None:
        """The sender of a session, None if there is none."""
        media = self._media.get(session)
        return None if media is None else media.sender

    async def handle_prepare(self, session: MultiTierSession) -> None:
        """Bind the session's ports and announce them."""
        sender = self.pool.create_multi_tier_sender(
            **self.sender_options,
            address=session.address,
            is_ipv6=session.is_ipv6,
            video_port=session.video_port,
            audio_port=session.audio_port,
            video_key=session.video_srtp_key + session.video_srtp_salt,
            audio_key=session.audio_srtp_key + session.audio_srtp_salt,
            video_ssrc=session.video_ssrc,
            audio_ssrc=session.audio_ssrc,
            video_payload_type=hksv3.VIDEO_PAYLOAD_TYPE,
            audio_payload_type=hksv3.AUDIO_PAYLOAD_TYPE,
        )
        media = self._media[session] = _Media(session, sender)
        sender.on_rtcp = session.keep_alive
        sender.on_closed = partial(self._sender_closed, media)
        sender.on_keyframe_request = partial(self._keyframe_requested, media)
        try:
            ports = await sender.bind()
        except Exception as err:
            raise StreamError(_SETUP_ERROR, f"Ports not bound: {err}") from err
        session.accessory_video_port, session.accessory_audio_port = ports

    def _get(self, session: MultiTierSession) -> _Media:
        media = self._media.get(session)
        if media is None or media.finished:
            raise StreamError(_ERROR, "No multi-tier media for the session")
        return media

    async def _lease(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> SourceLease:
        try:
            return await self.source_provider(session, video_tier, audio_tier)
        except Exception as err:
            # A provider's status may be one of another service, e.g. the
            # WebRTC offer's; a start answers with its own error.
            logger.warning("[%s] No multi-tier media source: %s", session.id, err)
            raise StreamError(_ERROR, f"No media source: {err}") from err

    async def handle_start(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Lease the tier's source, pull it and start the sender on it."""
        media = self._get(session)
        if (old := media.lease) is not None:
            # A repeated start after a failed one: give up the earlier claim.
            media.lease = None
            await old.release()
        lease = await self._lease(session, video_tier, audio_tier)
        media.lease = lease
        try:
            # Pull first: a failed pull leaves the sender unstarted, so a
            # repeated start can still start it; its keyframe request also
            # reaches the pull.
            await lease.activate()
            await media.sender.start(lease.source, audio=audio_tier is not None)
        except BaseException as err:
            media.lease = None
            await lease.release()
            if not isinstance(err, Exception):
                raise
            raise StreamError(_ERROR, f"Stream not started: {err}") from err
        logger.debug(
            "[%s] Multi-tier stream of %dx%d@%d started",
            session.id,
            video_tier.width,
            video_tier.height,
            video_tier.frame_rate,
        )

    async def handle_reconfigure(
        self,
        session: MultiTierSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> None:
        """Move the session to the source of the new tier's channel.

        Audio stays as the start set it.
        """
        media = self._get(session)
        old = media.lease
        lease = await self._lease(session, video_tier, audio_tier)
        if old is not None and lease.source is old.source:
            await lease.release()
            logger.debug("[%s] Tier %s uses the same source", session.id, video_tier)
            return
        # The switch asks the new source for a keyframe at once, through the
        # session's lease: it must already be the new one.
        media.lease = lease
        try:
            await lease.activate()
            await media.sender.switch(lease.source)
        except BaseException as err:
            # Also when the hook is cancelled, e.g. on a timeout.
            media.lease = old
            await lease.release()
            if not isinstance(err, Exception):
                raise
            raise StreamError(_ERROR, f"Tier not changed: {err}") from err
        if old is not None:
            await old.release()
        logger.debug("[%s] Multi-tier stream moved to %s", session.id, video_tier)

    async def handle_end(self, session: MultiTierSession) -> None:
        """Close the sender and release the lease."""
        media = self._media.pop(session, None)
        if media is not None:
            await self._finish(media)

    async def _finish(self, media: _Media) -> None:
        if media.finished:
            return
        media.finished = True
        try:
            await media.sender.close()
        finally:
            if media.lease is not None:
                await media.lease.release()
        logger.debug("[%s] Multi-tier media released", media.session.id)

    def _keyframe_requested(
        self, media: _Media, age: float | None, gop: float | None
    ) -> None:
        if media.lease is not None:
            media.lease.keyframe_requested(age, gop)

    def _sender_closed(self, media: _Media, reason: str) -> None:
        logger.info("[%s] Multi-tier stream ended: %s", media.session.id, reason)
        media.session.end(f"multi-tier media ended: {reason}")

    async def close(self) -> None:
        """Release every session left."""
        media, self._media = list(self._media.values()), {}
        await asyncio.gather(*(self._finish(m) for m in media))
