"""Media sources shared by the sessions of a camera channel.

:class:`SourcePool` gives each multi-tier session a
:class:`ChannelLease` on the media source of its camera channel. All
sessions of a channel share one source and one media pull
(:class:`ChannelPull`, for example an ffmpeg process), so N sessions cost one
camera connection. Sources live in a
:class:`~pyhap.media.worker.MediaWorker`, or in process without one;
a multi-tier sender is created in the same place.

A multi-tier sender binds its ports before the controller picks a tier, so
it is created by the pool (:meth:`SourcePool.create_multi_tier_sender`) and
started with a lease's source later.

The ports of a source are bound by the first lease; the pull starts with the
first answered session (:meth:`ChannelLease.activate`) and stops with the
last one; the source is closed with the last lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from dataclasses import dataclass, field
from functools import partial
import inspect
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import MediaError

if TYPE_CHECKING:
    from .worker import MediaWorker

logger = logging.getLogger(__name__)


class MediaSourceLike(Protocol):
    """A :class:`~pyhap.media.source.MediaSource` or its worker proxy."""

    on_closed: Callable[[str], None] | None

    @property
    def closed(self) -> bool:
        """Whether the source has ended."""

    @property
    def rtp_input(self) -> tuple[str, int, int]:
        """``(host, video_port, audio_port)`` the pull sends to."""

    @property
    def has_audio(self) -> bool:
        """Whether the source takes audio, i.e. has an audio input port."""

    def close(self, reason: str = "closed") -> Awaitable[None] | None:
        """Release the ports and end every session on the source."""


class MultiTierMediaSender(Protocol):
    """A :class:`~pyhap.media.multi_tier.MultiTierSender` or its worker proxy."""

    on_closed: Callable[[str], None] | None
    on_keyframe_request: Callable[[float | None, float | None], None] | None
    on_rtcp: Callable[[], None] | None

    async def bind(self) -> tuple[int, int]:
        """Bind the accessory's video and audio ports and return them."""

    async def start(self, source: MediaSourceLike, audio: bool = True) -> None:
        """Send ``source``, a source of the same pool, to the controller."""

    async def switch(self, source: MediaSourceLike) -> None:
        """Continue the stream from another source of the same pool."""

    async def close(self) -> None:
        """End the session."""


class ChannelPull(Protocol):
    """The media pull of one camera channel, for example an ffmpeg process."""

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Start sending plain RTP to ``(host, video_port, audio_port)``."""

    async def stop(self) -> None:
        """Stop sending; called once after a successful :meth:`start`."""

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """A session needs an IRAP picture."""


class SourceLease(Protocol):
    """A session's claim on the media source of its camera channel."""

    @property
    def source(self) -> MediaSourceLike:
        """The shared source."""

    async def activate(self) -> None:
        """The session was answered: the channel must be pulled now."""

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Forward a keyframe request of the session's sender."""

    async def release(self) -> None:
        """The session ended; called exactly once."""


@dataclass(eq=False)
class _Channel:
    key: Hashable
    name: str
    source: MediaSourceLike
    pull: ChannelPull
    leases: int = 0
    active: int = 0
    pulling: bool = False
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChannelLease:
    """A :class:`SourceLease` of a :class:`SourcePool`."""

    def __init__(self, pool: SourcePool, channel: _Channel) -> None:
        """Use :meth:`SourcePool.lease`."""
        self._pool = pool
        self._channel = channel
        self.active = False
        self.released = False

    @property
    def source(self) -> MediaSourceLike:
        """The channel's source."""
        return self._channel.source

    @property
    def channel(self) -> str:
        """The channel's name."""
        return self._channel.name

    async def activate(self) -> None:
        """Count the session as answered; the first one starts the pull."""
        await self._pool._activate(self)

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Pass the request on while the channel is pulled."""
        channel = self._channel
        if self.active and channel.pulling and not channel.closed:
            channel.pull.keyframe_requested(age, gop)

    async def release(self) -> None:
        """Give up the claim; the last session stops the pull and the source."""
        await asyncio.shield(self._pool._release(self))


class SourcePool:
    """Shares one media source and one pull per camera channel.

    A channel is any hashable key; leases of equal keys share the source.
    Sources and senders run in ``worker``, or in process without one.
    """

    def __init__(
        self, worker: MediaWorker | None = None, host: str = "127.0.0.1"
    ) -> None:
        """Configure the pool; sources are opened by the first lease."""
        self.worker = worker
        self.host = host
        self._channels: dict[Hashable, _Channel] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    def create_multi_tier_sender(self, **options: Any) -> MultiTierMediaSender:
        """Create a multi-tier sender where the pool's sources run.

        ``options`` are those of
        :class:`~pyhap.media.multi_tier.MultiTierSender`. Start it with the
        source of a lease of this pool. The pool creates it, not a lease: its ports
        are announced in Setup Endpoints, before the controller picks the
        tier, i.e. the lease.
        """
        if self.worker is not None:
            return cast(
                MultiTierMediaSender, self.worker.create_multi_tier_sender(**options)
            )
        from .multi_tier import MultiTierSender  # noqa: PLC0415

        return cast(MultiTierMediaSender, MultiTierSender(**options))

    def counts(self) -> dict[str, tuple[int, int]]:
        """Leases and answered leases per channel name."""
        return {c.name: (c.leases, c.active) for c in self._channels.values()}

    async def lease(
        self,
        channel: Hashable,
        pull: Callable[[], ChannelPull],
        *,
        audio: bool = True,
        name: str | None = None,
    ) -> ChannelLease:
        """Lease the source of ``channel``, opening it with ``pull`` if needed.

        ``pull`` makes the channel's pull when a new source is opened.
        ``name`` is what the log shows instead of the key.
        """
        task = asyncio.ensure_future(self._lease(channel, pull, audio, name))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The lease is made anyway; give it back once it is.
            task.add_done_callback(self._release_orphan)
            raise

    async def _lease(
        self,
        key: Hashable,
        pull: Callable[[], ChannelPull],
        audio: bool,
        name: str | None,
    ) -> ChannelLease:
        async with self._lock:
            channel = self._channels.get(key)
            if channel is None:
                # Made first: a failing factory leaves no open source behind.
                channel_pull = pull()
                source = await self._open_source(audio)
                channel = _Channel(key, name or str(key), source, channel_pull)
                source.on_closed = partial(self._source_closed, channel)
                if source.closed:
                    # It ended before on_closed was set, nothing will report it.
                    raise MediaError(f"Media source {channel.name} ended")
                self._channels[key] = channel
                logger.debug("Source %s opened at %s", channel.name, source.rtp_input)
            channel.leases += 1
            self._log(channel)
            return ChannelLease(self, channel)

    async def _open_source(self, audio: bool) -> MediaSourceLike:
        if self.worker is not None:
            return await self.worker.open_source(self.host, audio)
        from .source import MediaSource  # noqa: PLC0415

        source = MediaSource(self.host, audio)
        try:
            await source.start()
        except BaseException:
            source.close("start failed")
            raise
        return source

    def _release_orphan(self, task: asyncio.Task[ChannelLease]) -> None:
        if not task.cancelled() and task.exception() is None:
            self._spawn(task.result().release())

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _activate(self, lease: ChannelLease) -> None:
        channel = lease._channel
        if lease.released or lease.active:
            return
        if channel.closed:
            raise MediaError(f"Media source {channel.name} is closed")
        lease.active = True
        channel.active += 1
        try:
            await self._sync_pull(channel)
        except BaseException:
            # A release while the pull started has already counted it off.
            if lease.active:
                lease.active = False
                channel.active -= 1
            raise
        self._log(channel)

    async def _release(self, lease: ChannelLease) -> None:
        if lease.released:
            return
        lease.released = True
        channel = lease._channel
        channel.leases -= 1
        if lease.active:
            lease.active = False
            channel.active -= 1
        last = channel.leases == 0 and not channel.closed
        if last:
            # From here on a new lease opens a new source.
            channel.closed = True
            self._forget(channel)
        self._log(channel)
        await self._sync_pull(channel)
        if last:
            await self._close_source(channel, "no sessions left")

    async def _sync_pull(self, channel: _Channel) -> None:
        """Start or stop the pull so it runs exactly while sessions are answered."""
        async with channel.lock:
            wanted = channel.active > 0 and not channel.closed
            if wanted and not channel.pulling:
                logger.debug("Starting the pull of source %s", channel.name)
                await channel.pull.start(channel.source.rtp_input)
                channel.pulling = True
            elif not wanted and channel.pulling:
                channel.pulling = False
                logger.debug("Stopping the pull of source %s", channel.name)
                try:
                    await channel.pull.stop()
                except Exception:
                    logger.exception("Stopping the pull of %s failed", channel.name)

    async def _close_source(self, channel: _Channel, reason: str) -> None:
        source = channel.source
        source.on_closed = None
        try:
            result = source.close(reason)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Closing source %s failed", channel.name)
        logger.debug("Source %s closed: %s", channel.name, reason)

    def _forget(self, channel: _Channel) -> None:
        if self._channels.get(channel.key) is channel:
            del self._channels[channel.key]

    def _source_closed(self, channel: _Channel, reason: str) -> None:
        """The source ended on its own, e.g. its worker exited."""
        if channel.closed:
            return
        logger.warning("Source %s ended: %s", channel.name, reason)
        channel.closed = True
        self._forget(channel)
        self._spawn(self._sync_pull(channel))

    def _log(self, channel: _Channel) -> None:
        logger.debug(
            "Source %s: %d leases, %d answered%s",
            channel.name,
            channel.leases,
            channel.active,
            ", closed" if channel.closed else "",
        )

    async def close(self) -> None:
        """Stop every pull and close every source, e.g. on shutdown."""
        # A lease being opened finishes first, so its channel is closed too.
        async with self._lock:
            channels = list(self._channels.values())
            self._channels.clear()
            for channel in channels:
                channel.closed = True
        for channel in channels:
            await self._sync_pull(channel)
            await self._close_source(channel, "pool closed")
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
