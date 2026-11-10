"""Media of HomeKit camera live view: WebRTC (remote) and multi-tier RTP (local).

:class:`WebRTCSender`, :class:`MultiTierSender` and :class:`MediaSource`
import their transport (``pyOpenSSL``, ``pylibsrtp``) on first use;
:class:`MediaWorker` runs them in a worker process.
:class:`WebRTCMediaDelegate` and :class:`MultiTierMediaDelegate` run them for
the WebRTC and multi-tier sessions of :class:`pyhap.camera.Camera`, from one
:class:`SourcePool`; :class:`RecordingMediaDelegate` records classic HomeKit
Secure Video clips with a prebuffer from the same pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sdp import AudioParams, IceCandidateLike, VideoParams


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
        RecorderLike,
        SourceLease,
        SourcePool,
        WebRTCMediaSender,
    )
    from .recording import Recorder
    from .recording_delegate import RecordingError, RecordingMediaDelegate
    from .sender import SFrameKeyLike, WebRTCSender
    from .source import MediaSource
    from .webrtc_delegate import WebRTCMediaDelegate
    from .worker import (
        MediaWorker,
        RemoteMediaSource,
        RemoteMultiTierSender,
        RemoteRecorder,
        RemoteWebRTCSender,
    )

__all__ = [
    "AudioParams",
    "ChannelLease",
    "ChannelPull",
    "FFmpegInput",
    "FFmpegSourceProvider",
    "IceCandidateLike",
    "WebRTCMediaSender",
    "MediaSource",
    "MediaSourceLike",
    "MultiTierMediaDelegate",
    "MultiTierMediaSender",
    "MultiTierSender",
    "Recorder",
    "RecorderLike",
    "RecordingError",
    "RecordingMediaDelegate",
    "RemoteMediaSource",
    "RemoteMultiTierSender",
    "RemoteRecorder",
    "RemoteWebRTCSender",
    "SFrameKeyLike",
    "SourceLease",
    "SourcePool",
    "VideoParams",
    "MediaError",
    "WebRTCMediaDelegate",
    "MediaWorker",
    "WebRTCSender",
]

_LAZY = {
    "ChannelLease": "pool",
    "ChannelPull": "pool",
    "FFmpegInput": "ffmpeg",
    "FFmpegSourceProvider": "ffmpeg",
    "WebRTCMediaSender": "pool",
    "MediaSourceLike": "pool",
    "MultiTierMediaDelegate": "multi_tier_delegate",
    "MultiTierMediaSender": "pool",
    "MultiTierSender": "multi_tier",
    "Recorder": "recording",
    "RecorderLike": "pool",
    "RecordingError": "recording_delegate",
    "RecordingMediaDelegate": "recording_delegate",
    "SourceLease": "pool",
    "SourcePool": "pool",
    "WebRTCMediaDelegate": "webrtc_delegate",
    "MediaSource": "source",
    "SFrameKeyLike": "sender",
    "WebRTCSender": "sender",
    "RemoteMediaSource": "worker",
    "RemoteMultiTierSender": "worker",
    "RemoteRecorder": "worker",
    "RemoteWebRTCSender": "worker",
    "MediaWorker": "worker",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module  # noqa: PLC0415

        return getattr(import_module(f".{_LAZY[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
