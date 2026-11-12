"""Run the media of HomeKit WebRTC sessions with :mod:`pyhap.media` senders.

:class:`WebRTCMediaDelegate` is a :class:`pyhap.camera_webrtc.WebRTCDelegate`
with one :class:`~pyhap.media.sender.WebRTCSender` per session::

    worker = MediaWorker()
    provider = FFmpegSourceProvider({"medium": ["-i", url]}, worker=worker)
    delegate = WebRTCMediaDelegate(provider)
    camera = Camera({..., "webrtc": {"delegate": delegate}}, driver, "Camera")

Media sources: the integrator's ``source_provider`` maps the tier of a
session to a camera channel and returns a
:class:`~pyhap.media.pool.SourceLease` on that channel's source, usually
from a :class:`~pyhap.media.pool.SourcePool`: all sessions of a channel
share one source and one media pull. The lease creates the session's
sender where its source runs, in the pool's media worker or in process.
:class:`~pyhap.media.ffmpeg.FFmpegSourceProvider` is a reference provider
that pulls with ffmpeg.

When the camera is pulled: the offer binds the source's ports and the ICE
sockets but starts no pull, so an offer that is never answered never
touches the camera. The answer starts the pull of the channel if no other
answered session runs it; ICE and DTLS connect meanwhile. The pull stops
when the last answered session of the channel ends, and the ports are
released with the last session.

A sender that ends on its own (connection lost, source gone, no answer)
ends its HAP session with :meth:`pyhap.camera_stream.StreamSession.end`.
The delegate keeps its state per session object.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
import logging
import time
from typing import TYPE_CHECKING, Any

from pyhap import hksv3
from pyhap.camera_stream import StreamError

from . import MediaError
from .pool import SourceLease, WebRTCMediaSender
from .sdp import AudioParams, VideoParams

if TYPE_CHECKING:
    from pyhap.camera_webrtc import WebRTCSession

logger = logging.getLogger(__name__)

_ERROR = hksv3.WebRTCStreamingStatus.ERROR
_OFFER_ERROR = hksv3.WebRTCSolicitOfferStatus.ERROR

SourceProvider = Callable[
    ["WebRTCSession", hksv3.VideoStreamTier, hksv3.AudioStreamTier],
    Awaitable[SourceLease],
]


@dataclass(eq=False)
class _Media:
    """What the delegate holds for one session."""

    session: WebRTCSession
    sframe: hksv3.SFrameKey | None
    lease: SourceLease | None = None
    sender: WebRTCMediaSender | None = None
    created: float = field(default_factory=time.monotonic)
    answered: float | None = None
    finished: bool = False


class WebRTCMediaDelegate:
    """A ``WebRTCDelegate`` that streams with :mod:`pyhap.media` senders.

    ``source_provider`` leases the source of a session's channel, see
    :class:`~pyhap.media.pool.SourcePool`; the lease decides whether the
    sender runs in a media worker. ``on_audio(session, opus, rtp_timestamp)``
    gets the controller's talkback, which the offer then asks for.
    ``sender_options`` go to every sender, e.g. ``host_addresses`` and
    timeouts.

    The SFrame key is new for each session. A media worker should be started
    before the first offer: starting it takes longer than a HAP hook may.
    """

    def __init__(
        self,
        source_provider: SourceProvider,
        *,
        on_audio: Callable[[WebRTCSession, bytes, int], None] | None = None,
        sender_options: Mapping[str, Any] | None = None,
    ) -> None:
        """Configure the delegate."""
        # Fail here, not on the first session, without the WebRTC extra.
        from . import transport  # noqa: F401, PLC0415

        self.source_provider = source_provider
        self.on_audio = on_audio
        self.sender_options = dict(sender_options or {})
        self._media: dict[WebRTCSession, _Media] = {}

    @property
    def session_count(self) -> int:
        """Sessions the delegate holds media for."""
        return len(self._media)

    def sender(self, session: WebRTCSession) -> WebRTCMediaSender | None:
        """The sender of a session, None if there is none."""
        media = self._media.get(session)
        return None if media is None else media.sender

    async def handle_solicit_offer(
        self,
        session: WebRTCSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier,
    ) -> hksv3.WebRTCOffer:
        """Lease the channel's source, create a sender and its offer."""
        key = hksv3.generate_sframe_key() if session.sframe else None
        media = self._media[session] = _Media(session, key)
        try:
            media.lease = await self.source_provider(session, video_tier, audio_tier)
        except StreamError:
            raise
        except Exception as err:
            logger.warning("[%s] No WebRTC media source: %s", session.id, err)
            raise StreamError(_OFFER_ERROR, f"No media source: {err}") from err
        sender = media.sender = media.lease.create_webrtc_sender(
            **self.sender_options,
            video=VideoParams.from_tier(video_tier, hksv3.VIDEO_PAYLOAD_TYPE),
            audio=AudioParams(
                hksv3.AUDIO_PAYLOAD_TYPE, talkback=self.on_audio is not None
            ),
            sframe=key,
        )
        sender.on_closed = partial(self._sender_closed, media)
        sender.on_connected = partial(self._connected, media)
        sender.on_keyframe_request = partial(self._keyframe_requested, media)
        if self.on_audio is not None:
            sender.on_audio = partial(self.on_audio, session)
        try:
            sdp = await sender.create_offer()
        except MediaError as err:
            raise StreamError(_OFFER_ERROR, f"No offer: {err}") from err
        logger.debug(
            "[%s] WebRTC offer for %dx%d@%d, SFrame %s, in %.0f ms",
            session.id,
            video_tier.width,
            video_tier.height,
            video_tier.frame_rate,
            key is not None,
            (time.monotonic() - media.created) * 1000,
        )
        return hksv3.WebRTCOffer(sdp, key)

    def _get(
        self, session: WebRTCSession
    ) -> tuple[WebRTCMediaSender, SourceLease, _Media]:
        media = self._media.get(session)
        if media is None or media.sender is None or media.lease is None:
            raise StreamError(_ERROR, "No WebRTC media for the session")
        return media.sender, media.lease, media

    async def handle_answer(
        self,
        session: WebRTCSession,
        sdp: str,
        candidates: list[hksv3.IceCandidate],
    ) -> None:
        """Apply the answer and pull the channel while ICE and DTLS connect."""
        sender, lease, media = self._get(session)
        try:
            await sender.apply_answer(sdp, candidates)
        except MediaError as err:
            raise StreamError(_ERROR, f"Answer not applied: {err}") from err
        media.answered = time.monotonic()
        try:
            await lease.activate()
        except Exception as err:
            raise StreamError(_ERROR, f"Media pull not started: {err}") from err

    async def handle_reoffer(
        self,
        session: WebRTCSession,
        sdp: str,
        options: hksv3.WebRTCOfferOptions,
    ) -> hksv3.WebRTCReofferAnswer:
        """Answer the controller's new offer; SFrame cannot be switched."""
        sender, _lease, media = self._get(session)
        if options.sframe_enabled not in (None, session.sframe):
            raise StreamError(_ERROR, "SFrame cannot be switched in a session")
        try:
            answer = await sender.reoffer(sdp)
        except MediaError as err:
            raise StreamError(_ERROR, f"Reoffer not answered: {err}") from err
        return hksv3.WebRTCReofferAnswer(answer, media.sframe)

    async def handle_update_session(
        self,
        session: WebRTCSession,
        add_keys: list[hksv3.SFrameKey],
        remove_kids: list[int],
    ) -> None:
        """Install and remove the controller's SFrame keys."""
        sender, _lease, _media = self._get(session)
        try:
            await sender.update_receive_keys(add_keys, remove_kids)
        except MediaError as err:
            raise StreamError(_ERROR, f"Keys not updated: {err}") from err

    async def handle_end(self, session: WebRTCSession) -> None:
        """Close the sender and release the lease."""
        media = self._media.pop(session, None)
        if media is not None:
            await self._finish(media)

    async def _finish(self, media: _Media) -> None:
        if media.finished:
            return
        media.finished = True
        try:
            if media.sender is not None:
                await media.sender.close()
        finally:
            if media.lease is not None:
                await media.lease.release()
        logger.debug("[%s] WebRTC media released", media.session.id)

    def _connected(self, media: _Media) -> None:
        now = time.monotonic()
        logger.debug(
            "[%s] WebRTC connected %.0f ms after the offer, %.0f ms after the answer",
            media.session.id,
            (now - media.created) * 1000,
            (now - (media.answered or now)) * 1000,
        )

    def _keyframe_requested(
        self, media: _Media, age: float | None, gop: float | None
    ) -> None:
        logger.debug(
            "[%s] WebRTC keyframe request, last IRAP %s s ago, GOP %s s",
            media.session.id,
            "-" if age is None else f"{age:.1f}",
            "-" if gop is None else f"{gop:.1f}",
        )
        if media.lease is not None:
            media.lease.keyframe_requested(age, gop)

    def _sender_closed(self, media: _Media, reason: str) -> None:
        logger.info("[%s] WebRTC session ended: %s", media.session.id, reason)
        media.session.end(f"WebRTC media ended: {reason}")

    async def close(self) -> None:
        """Release every session left and wait for background work."""
        media, self._media = list(self._media.values()), {}
        await asyncio.gather(*(self._finish(m) for m in media))
