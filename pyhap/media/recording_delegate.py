"""Classic HomeKit Secure Video recordings from a source pool.

:class:`RecordingMediaDelegate` turns one camera channel into the hub's
recordings. Without a prebuffer (the default) it pulls the channel only for
a recording, which starts at the next IRAP picture. With ``prebuffer_ms``
it keeps the channel buffered while the hub has recording on, so a
recording starts that many milliseconds before the hub asked::

    worker = MediaWorker()
    provider = FFmpegSourceProvider({"high": [...], "medium": [...]}, worker=worker)
    recording = RecordingMediaDelegate(provider, medium_tier, prebuffer_ms=4000)
    camera = Camera(
        {
            ...,
            "recording": {"delegate": recording},
            "multi_tier": {"delegate": MultiTierMediaDelegate(provider)},
        },
        driver,
        "Camera",
    )

The channel comes from the same provider and pool as the live sessions, so
a recording channel that is also watched live is pulled once. While armed
with a prebuffer the channel is pulled and buffered as compressed media;
only a recording transcodes, see :class:`~pyhap.media.recording.Recorder`.
The camera arms the delegate while the hub has recording on and the camera
is on, and advertises :attr:`RecordingMediaDelegate.prebuffer_ms`.

Observed with iOS and tvOS 27: the hub does not upload recordings it gets
over HDS from a camera that also offers the Multi-Tier RTP service, so they
never appear in the Home app. Use this delegate for cameras without
Multi-Tier.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
import logging
from typing import Protocol

from pyhap import hksv3, hksv_recording
from pyhap.hds_recording import RecordingPacket, fmp4_recording_packets

from .pool import ChannelLease, RecorderLike
from .recording import recording_ffmpeg_args

logger = logging.getLogger(__name__)

DEFAULT_PREBUFFER_MS = 0
# The fragmented MP4 a recording may hold while the hub reads it.
MAX_PENDING_OUTPUT = 16 * 1024 * 1024


class ChannelProvider(Protocol):
    """Leases camera channels, e.g. an
    :class:`~pyhap.media.ffmpeg.FFmpegSourceProvider`.
    """

    ffmpeg: str

    async def lease_channel(self, tier: hksv3.VideoStreamTier) -> ChannelLease:
        """Lease the channel of ``tier`` without activating it."""


class _BoundedReader:
    """Holds a recording's MP4 output until the hub reads it, up to a limit.

    Has the ``readexactly`` that :func:`fmp4_recording_packets` needs;
    ``on_overflow`` runs once when unread output exceeds ``limit`` bytes.
    """

    def __init__(self, limit: int, on_overflow: Callable[[], None]) -> None:
        self._limit = limit
        self._on_overflow = on_overflow
        self._buffer = bytearray()
        self._eof = False
        self._changed = asyncio.Event()

    def feed_data(self, data: bytes) -> None:
        if self._eof:
            return
        self._buffer += data
        self._changed.set()
        if len(self._buffer) > self._limit:
            self._on_overflow()

    def feed_eof(self) -> None:
        self._eof = True
        self._changed.set()

    def at_eof(self) -> bool:
        return self._eof and not self._buffer

    async def readexactly(self, n: int) -> bytes:
        size = n
        while len(self._buffer) < size:
            if self._eof:
                partial = bytes(self._buffer)
                self._buffer.clear()
                raise asyncio.IncompleteReadError(partial, size)
            self._changed.clear()
            await self._changed.wait()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


class RecordingError(Exception):
    """A recording could not be made or ended early."""


class RecordingMediaDelegate:
    """Transcodes a channel into recordings, buffering it while armed.

    With ``prebuffer_ms`` 0 nothing is pulled while idle.

    ``source_provider`` is a :class:`ChannelProvider`, ``tier`` selects its
    recording channel. ``prebuffer_ms`` 0 is valid: hubs accept it (observed
    with iOS and tvOS 26 and 27) and record from the trigger on. ``ffmpeg``
    defaults to the provider's; ``video_encoder`` and ``encoder_args`` pick
    the H.264 encoder, e.g. a hardware one.
    """

    def __init__(
        self,
        source_provider: ChannelProvider,
        tier: hksv3.VideoStreamTier,
        *,
        prebuffer_ms: int = DEFAULT_PREBUFFER_MS,
        ffmpeg: str | None = None,
        video_encoder: str = "libx264",
        encoder_args: Sequence[str] = (),
    ) -> None:
        """Configure the delegate; :meth:`arm` starts buffering."""
        self._provider = source_provider
        self.tier = tier
        self.prebuffer_ms = prebuffer_ms
        self.ffmpeg = ffmpeg or source_provider.ffmpeg
        self.video_encoder = video_encoder
        self.encoder_args = tuple(encoder_args)
        self._lease: ChannelLease | None = None
        self._recorder: RecorderLike | None = None
        self._armed = False
        self._lock = asyncio.Lock()

    @property
    def armed(self) -> bool:
        """Whether the hub has recording on (see :meth:`arm`)."""
        return self._armed

    @property
    def buffering(self) -> bool:
        """Whether the channel is kept pulled and buffered between recordings."""
        return self._armed and self.prebuffer_ms > 0

    @property
    def recording(self) -> bool:
        """Whether a recording runs."""
        return self._recorder is not None and self._recorder.recording

    async def set_armed(self, armed: bool) -> None:
        """Arm or disarm, see :meth:`arm` and :meth:`disarm`."""
        if armed:
            await self.arm()
        else:
            await self.disarm()

    async def arm(self) -> None:
        """Allow recordings; with a prebuffer, buffer until :meth:`disarm`."""
        async with self._lock:
            self._armed = True
            if self.buffering:
                try:
                    await self._open()
                except BaseException:
                    self._armed = False
                    raise

    async def disarm(self) -> None:
        """Stop buffering; a running recording ends."""
        async with self._lock:
            self._armed = False
            await self._release()

    async def close(self) -> None:
        """Disarm for good."""
        await self.disarm()

    async def _open(self) -> RecorderLike:
        recorder = self._recorder
        if recorder is not None and not recorder.closed:
            return recorder
        await self._release()
        lease = await self._provider.lease_channel(self.tier)
        try:
            await lease.activate()
            audio = lease.source.has_audio
            recorder = await lease.open_recorder(
                prebuffer=self.prebuffer_ms / 1000, audio=audio
            )
        except BaseException:
            await lease.release()
            raise
        self._lease, self._recorder = lease, recorder
        logger.debug("Recording channel %s buffered", lease.channel)
        return recorder

    async def _release(self) -> None:
        recorder, self._recorder = self._recorder, None
        lease, self._lease = self._lease, None
        if recorder is not None:
            ended = recorder.on_ended if recorder.recording else None
            await recorder.close()
            if ended is not None:
                ended("recording channel released")
        if lease is not None:
            await lease.release()

    async def record(
        self,
        selected: hksv_recording.SelectedRecordingConfiguration,
        audio: bool,
    ) -> AsyncGenerator[RecordingPacket, None]:
        """Yield the recording the hub selected, starting with the buffer.

        Without a buffered channel the channel is pulled for this recording
        only, which then starts at the next IRAP picture. Raises
        :class:`RecordingError` when the transcode fails.
        """
        async with self._lock:
            recorder = await self._open()
            if recorder.recording:
                raise RecordingError("A recording runs already")
            errors: list[str] = []

            def overflow() -> None:
                errors.append("the hub reads the recording too slowly")
                recorder.on_data = None
                reader.feed_eof()

            reader = _BoundedReader(MAX_PENDING_OUTPUT, overflow)

            def ended(error: str | None) -> None:
                if error is not None:
                    errors.append(error)
                reader.feed_eof()

            try:
                command = recording_ffmpeg_args(
                    selected,
                    audio=audio,
                    source_audio=self._lease is not None
                    and self._lease.source.has_audio,
                    ffmpeg=self.ffmpeg,
                    video_encoder=self.video_encoder,
                    encoder_args=self.encoder_args,
                )
                recorder.on_data = reader.feed_data
                recorder.on_ended = ended
                await recorder.start(
                    command, selected.recording.prebuffer_length_ms / 1000
                )
            except BaseException:
                recorder.on_data = None
                recorder.on_ended = None
                if not self.buffering:
                    await self._release()
                raise
        try:
            async for packet in fmp4_recording_packets(reader):
                if packet.is_last and errors:
                    break
                yield packet
            if errors:
                raise RecordingError(errors[0])
        finally:
            recorder.on_data = None
            recorder.on_ended = None
            await recorder.stop()
            if not self.buffering:
                async with self._lock:
                    # A recording started meanwhile keeps the channel.
                    if not self.buffering and not self.recording:
                        await self._release()
