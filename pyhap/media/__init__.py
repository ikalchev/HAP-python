"""Media of HomeKit cameras: multi-tier RTP live view.

:class:`MultiTierSender` and :class:`MediaSource` import their transport
(``pylibsrtp``) on first use; :class:`MediaWorker` runs them in a worker
process. :class:`MultiTierMediaDelegate` runs them for the multi-tier
sessions of :class:`pyhap.camera.Camera` from a :class:`SourcePool`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


class MediaError(Exception):
    """A media session cannot do what was asked."""


if TYPE_CHECKING:
    from .ffmpeg import FFmpegInput, FFmpegSourceProvider
    from .multi_tier import MultiTierSender
    from .multi_tier_delegate import MultiTierMediaDelegate
    from .pool import (
        ChannelLease,
        ChannelPull,
        MediaSourceLike,
        MultiTierMediaSender,
        SourceLease,
        SourcePool,
    )
    from .source import MediaSource
    from .worker import (
        MediaWorker,
        RemoteMediaSource,
        RemoteMultiTierSender,
    )

__all__ = [
    "ChannelLease",
    "ChannelPull",
    "FFmpegInput",
    "FFmpegSourceProvider",
    "MediaSource",
    "MediaSourceLike",
    "MultiTierMediaDelegate",
    "MultiTierMediaSender",
    "MultiTierSender",
    "RemoteMediaSource",
    "RemoteMultiTierSender",
    "SourceLease",
    "SourcePool",
    "MediaError",
    "MediaWorker",
]

_LAZY = {
    "ChannelLease": "pool",
    "ChannelPull": "pool",
    "FFmpegInput": "ffmpeg",
    "FFmpegSourceProvider": "ffmpeg",
    "MediaSourceLike": "pool",
    "MultiTierMediaDelegate": "multi_tier_delegate",
    "MultiTierMediaSender": "pool",
    "MultiTierSender": "multi_tier",
    "SourceLease": "pool",
    "SourcePool": "pool",
    "MediaSource": "source",
    "RemoteMediaSource": "worker",
    "RemoteMultiTierSender": "worker",
    "MediaWorker": "worker",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module  # noqa: PLC0415

        return getattr(import_module(f".{_LAZY[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
