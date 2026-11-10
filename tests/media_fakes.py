"""Fakes of the media worker and sessions for the media tests (not a test module).

The fake worker stands in for both the in-process and the worker media.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import pathlib
import sys
from typing import Any
from uuid import UUID, uuid4

from pyhap import hksv3
from pyhap.camera_webrtc import WebRTCSession
from pyhap.media import MediaError
from pyhap.media.pool import ChannelLease, SourcePool

FAKE_OFFER = "v=0\r\na=offer\r\n"
FAKE_REOFFER_ANSWER = "v=0\r\na=reoffer-answer\r\n"
SENSOR = hksv3.SensorConfiguration(
    UUID("00112233-4455-6677-8899-aabbccddeeff"),
    3840,
    2160,
    video_streams=[
        hksv3.VideoStreamCapability(UUID(int=1), 2, 1920, 1080, 30, 1700, 1800),
        hksv3.VideoStreamCapability(UUID(int=2), 3, 1280, 720, 30, 768, 800),
        hksv3.VideoStreamCapability(UUID(int=3), 4, 640, 360, 15, 180, 190),
    ],
)
MEDIUM = hksv3.tiers_from_sensor(SENSOR)[1]
LOW = hksv3.tiers_from_sensor(SENSOR)[2]
AUDIO = hksv3.DEFAULT_AUDIO_TIER


def make_session(sframe: bool = True) -> WebRTCSession:
    """A WebRTC session on the Medium tier, outside any controller."""
    return WebRTCSession(
        id=uuid4(),
        sensor=SENSOR,
        client_addr=None,
        sframe=sframe,
        video_tier=MEDIUM,
        audio_tier=AUDIO,
    )


def write_fake_ffmpeg(directory: pathlib.Path) -> pathlib.Path:
    """Write an ffmpeg stand-in: logs its arguments, prints progress, waits.

    It exits at once with 1 while ``exit-now`` exists next to it, and prints
    a camera URL with credentials and a token.
    """
    script = directory / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, sys, time\n"
        "here = pathlib.Path(__file__).parent\n"
        "with open(here / 'starts.log', 'a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('frame=1 fps=0\\rframe=2 fps=0\\nwarning\\n')\n"
        "sys.stderr.write('rtsps://user:pw@192.168.1.1:7441/T0k3n?x=1: 404\\n')\n"
        "sys.stderr.flush()\n"
        "if (here / 'exit-now').exists():\n"
        "    sys.exit(1)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    return script


class FakeSource:
    """Stands in for a MediaSource or RemoteMediaSource."""

    def __init__(self, port: int, audio: bool) -> None:
        """Pretend to be bound."""
        self.rtp_input = ("127.0.0.1", port, port + 1 if audio else 0)
        self.on_closed: Callable[[str], None] | None = None
        self.close_reasons: list[str] = []

    @property
    def closed(self) -> bool:
        """Whether the source was closed."""
        return bool(self.close_reasons)

    @property
    def has_audio(self) -> bool:
        """Whether the source takes audio."""
        return self.rtp_input[2] != 0

    def keyframe_age(self) -> float | None:
        """No IRAP picture seen yet."""
        return None

    async def close(self, reason: str = "closed") -> None:
        """Record the close."""
        self.close_reasons.append(reason)

    def end(self, reason: str) -> None:
        """End on its own, as when the worker exits."""
        assert self.on_closed is not None
        self.on_closed(reason)


class FakeSender:
    """Stands in for a WebRTCSender, recording every call."""

    def __init__(self, **options: Any) -> None:
        """Keep the options the delegate created it with."""
        self.options = options
        self.on_closed: Callable[[str], None] | None = None
        self.on_connected: Callable[[], None] | None = None
        self.on_keyframe_request: (
            Callable[[float | None, float | None], None] | None
        ) = None
        self.on_audio: Callable[[bytes, int], None] | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.error: MediaError | None = None

    def _call(self, *call: Any) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    async def create_offer(self) -> str:
        """Return a fixed offer."""
        self._call("offer")
        return FAKE_OFFER

    async def apply_answer(self, sdp: str, candidates: Iterable[Any]) -> None:
        """Record the answer."""
        self._call("answer", sdp, list(candidates))

    async def reoffer(self, sdp: str) -> str:
        """Return a fixed answer."""
        self._call("reoffer", sdp)
        return FAKE_REOFFER_ANSWER

    async def update_receive_keys(
        self, add: Iterable[Any] = (), remove: Iterable[int] = ()
    ) -> None:
        """Record the keys."""
        self._call("keys", list(add), list(remove))

    async def close(self) -> None:
        """Record the close; never fails."""
        self.calls.append(("close",))


class FakeWorker:
    """Stands in for a MediaWorker."""

    pid = 4242

    def __init__(self) -> None:
        """Start without sources or senders."""
        self.sources: list[FakeSource] = []
        self.senders: list[FakeSender] = []
        self.end_on_open: str | None = None

    async def open_source(self, host: str = "127.0.0.1", audio: bool = True) -> Any:
        """Open a fake source on new ports, already ended if ``end_on_open``."""
        source = FakeSource(5000 + 2 * len(self.sources), audio)
        if self.end_on_open is not None:
            source.close_reasons.append(self.end_on_open)
        self.sources.append(source)
        return source

    def create_sender(self, **options: Any) -> Any:
        """Create a fake sender."""
        sender = FakeSender(**options)
        self.senders.append(sender)
        return sender


class FakePull:
    """Stands in for an ffmpeg pull."""

    def __init__(self) -> None:
        """Not running yet."""
        self.started: list[tuple[str, int, int]] = []
        self.stopped = 0
        self.keyframes: list[tuple[float | None, float | None]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.waiting = False

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Record the start, or fail; wait for ``gate`` first if set."""
        if self.gate is not None:
            self.waiting = True
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.started.append(rtp_input)

    async def stop(self) -> None:
        """Record the stop."""
        self.stopped += 1

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Record the request."""
        self.keyframes.append((age, gop))


class FakeProvider:
    """A source provider with a channel per tier quality and fake pulls."""

    def __init__(self, pool: SourcePool) -> None:
        """Lease from ``pool``."""
        self.pool = pool
        self.pulls: list[FakePull] = []

    async def __call__(
        self,
        session: WebRTCSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier,
    ) -> ChannelLease:
        """Lease the channel of the tier's quality."""
        name = hksv3.VideoQuality(video_tier.quality).name.lower()
        return await self.pool.lease(video_tier.quality, self._pull, name=name)

    def _pull(self) -> FakePull:
        pull = FakePull()
        self.pulls.append(pull)
        return pull
