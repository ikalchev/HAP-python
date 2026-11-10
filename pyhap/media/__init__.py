"""Shared media of HomeKit camera streams.

:class:`MediaSource` receives a camera channel as plain RTP and reassembles
its H.265 once for every session of the channel. :class:`SourcePool` shares
one source and one pull (e.g. :class:`FFmpegSourceProvider`) per channel,
and :class:`MediaWorker` runs the sources in a worker process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sdp import AudioParams, IceCandidateLike, VideoParams


class MediaError(Exception):
    """A media session cannot do what was asked."""


if TYPE_CHECKING:
    from .ffmpeg import FFmpegInput, FFmpegSourceProvider
    from .pool import (
        ChannelLease,
        ChannelPull,
        MediaSourceLike,
        SourceLease,
        SourcePool,
    )
    from .source import MediaSource
    from .worker import (
        MediaWorker,
        RemoteMediaSource,
    )

__all__ = [
    "AudioParams",
    "ChannelLease",
    "ChannelPull",
    "FFmpegInput",
    "FFmpegSourceProvider",
    "IceCandidateLike",
    "MediaSource",
    "MediaSourceLike",
    "RemoteMediaSource",
    "SourceLease",
    "SourcePool",
    "VideoParams",
    "MediaError",
    "MediaWorker",
]

_LAZY = {
    "ChannelLease": "pool",
    "ChannelPull": "pool",
    "FFmpegInput": "ffmpeg",
    "FFmpegSourceProvider": "ffmpeg",
    "MediaSourceLike": "pool",
    "SourceLease": "pool",
    "SourcePool": "pool",
    "MediaSource": "source",
    "RemoteMediaSource": "worker",
    "MediaWorker": "worker",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module  # noqa: PLC0415

        return getattr(import_module(f".{_LAZY[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
