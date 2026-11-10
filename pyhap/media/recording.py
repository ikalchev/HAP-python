"""Classic HomeKit Secure Video recordings from a shared media source.

A :class:`Recorder` attaches a :class:`PrebufferRing` to a
:class:`~pyhap.media.source.MediaSource`: while the hub has recording on,
the ring keeps the last seconds of the channel as compressed access units
and Opus packets, starting at an IRAP picture. Nothing is decoded while no
recording runs.

When the hub opens a recording, the recorder starts ffmpeg with
:func:`recording_ffmpeg_args`, writes the buffered media and then the live
media to its stdin as MPEG-TS (:mod:`pyhap.media.mpegts`) and hands
ffmpeg's fragmented MP4 to ``on_data``. ffmpeg reads the buffered part as
fast as it can encode, so the recording catches up with the live picture
within the first fragment or two. The hub selects H.264 and AAC in an exact
size, frame rate, profile, level and bit rate; the transcode makes exactly
that, with an IRAP picture, and so a fragment, every selected interval.

The recorder runs where the source runs, in the media worker or in process.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import logging
import math
import time
from typing import TYPE_CHECKING

from pyhap import hksv_recording

from . import rtp
from .ffmpeg import FFMPEG_STOP_TIMEOUT, StderrLines, redact_urls
from .hevc import AccessUnit
from .mpegts import MpegTsMuxer
from .rtp import VIDEO_CLOCK_RATE

if TYPE_CHECKING:
    from .source import MediaSource

logger = logging.getLogger(__name__)

AUDIO_CLOCK_RATE = 48000
TS_CLOCK_RATE = 90000
# The ring drops its oldest media beyond this many bytes, whatever the time.
MAX_BUFFER_BYTES = 64 * 1024 * 1024
# A recording ends when ffmpeg falls this far behind its input.
MAX_PENDING_BYTES = 32 * 1024 * 1024
# A timestamp this far from what the arrival times say starts a new anchor,
# e.g. after the pull restarted with a new SSRC.
MAX_TIMESTAMP_DRIFT = 1.0
# The shortest IRAP interval, i.e. fragment length, a recording uses.
MIN_FRAGMENT_SECONDS = 1.0
# The first PTS, so audio that arrived just before the video is not negative.
PTS_START = TS_CLOCK_RATE
OUTPUT_CHUNK = 64 * 1024
# The selected recording values and their ffmpeg names.
H264_PROFILES = {0: "baseline", 1: "main", 2: "high"}
H264_LEVELS = {0: "3.1", 1: "3.2", 2: "4.0"}
SAMPLE_RATES = {
    hksv_recording.AudioSampleRate.KHZ_8: 8000,
    hksv_recording.AudioSampleRate.KHZ_16: 16000,
    hksv_recording.AudioSampleRate.KHZ_24: 24000,
    hksv_recording.AudioSampleRate.KHZ_32: 32000,
    hksv_recording.AudioSampleRate.KHZ_44_1: 44100,
    hksv_recording.AudioSampleRate.KHZ_48: 48000,
}
# Encoders that take ``-level`` and the x264 speed options.
X264_ENCODERS = ("libx264",)


@dataclass(frozen=True)
class BufferedMedia:
    """One access unit or Opus packet and when it arrived."""

    arrival: float
    unit: AccessUnit | None = None
    audio: rtp.RtpPacket | None = None

    @property
    def keyframe(self) -> bool:
        """Whether this is an IRAP picture."""
        return self.unit is not None and self.unit.keyframe

    @property
    def size(self) -> int:
        """Bytes of media."""
        if self.unit is not None:
            return sum(len(nal) for nal in self.unit.nals)
        return len(self.audio.payload) if self.audio is not None else 0


class PrebufferRing:
    """The last seconds of a source, starting at an IRAP picture.

    A :class:`~pyhap.media.source.MediaConsumer` that always wants media.
    It keeps at least ``seconds`` before now, from the latest IRAP picture
    at or before that point; ``listener`` gets each new item once set.
    """

    def __init__(
        self,
        seconds: float,
        max_bytes: int = MAX_BUFFER_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the ring; attach it to a source to fill it."""
        self.seconds = seconds
        self.max_bytes = max_bytes
        self.listener: Callable[[BufferedMedia], None] | None = None
        self.on_closed: Callable[[str], None] | None = None
        self._clock = clock
        self._items: deque[BufferedMedia] = deque()
        self._bytes = 0
        self._ssrc: int | None = None

    @property
    def wants_media(self) -> bool:
        """Always, the ring buffers while it is attached."""
        return True

    @property
    def buffered(self) -> float:
        """Seconds from the first buffered IRAP picture to now, 0 without one."""
        if not self._items or not self._items[0].keyframe:
            return 0.0
        return self._clock() - self._items[0].arrival

    def on_source_video(self, unit: AccessUnit, ssrc: int) -> None:
        """Buffer an access unit; video before the first IRAP is dropped."""
        if ssrc != self._ssrc:
            # A restarted pull: its timestamps start over.
            self._ssrc = ssrc
            self._clear()
        if not self._items and not unit.keyframe:
            return
        self._add(BufferedMedia(self._clock(), unit=unit))

    def on_source_audio(self, packet: rtp.RtpPacket) -> None:
        """Buffer an Opus packet once there is video to go with it."""
        if self._items:
            self._add(BufferedMedia(self._clock(), audio=packet))

    def on_source_closed(self, reason: str) -> None:
        """The source ended."""
        self._clear()
        if self.on_closed is not None:
            self.on_closed(reason)

    def snapshot(self, seconds: float) -> list[BufferedMedia]:
        """The buffered media from the latest IRAP at least ``seconds`` ago.

        Fewer seconds when the ring holds less; empty without an IRAP.
        """
        limit = self._clock() - seconds
        start = 0
        for index, item in enumerate(self._items):
            if item.arrival > limit:
                break
            if item.keyframe:
                start = index
        return list(self._items)[start:]

    def _add(self, item: BufferedMedia) -> None:
        self._items.append(item)
        self._bytes += item.size
        self._trim()
        if self.listener is not None:
            self.listener(item)

    def _trim(self) -> None:
        limit = self._clock() - self.seconds
        while True:
            # The second IRAP from the left: everything before it can go once
            # it is old enough, or the ring is too large.
            second = next(
                (i for i, item in enumerate(self._items) if i and item.keyframe), None
            )
            if second is None:
                if self._bytes > self.max_bytes:
                    # No later IRAP to cut at, e.g. a very long GOP: start over
                    # at the next IRAP rather than grow without bound. A
                    # running recording misses the frames until that IRAP.
                    self._clear()
                return
            if self._items[second].arrival > limit and self._bytes <= self.max_bytes:
                return
            for _ in range(second):
                self._bytes -= self._items.popleft().size

    def _clear(self) -> None:
        self._items.clear()
        self._bytes = 0


class _Timestamps:
    """Maps the RTP timestamps of one stream to a common 90 kHz timeline."""

    def __init__(self, clock_rate: int, origin: float) -> None:
        self._rate = clock_rate
        self._origin = origin
        self._anchor: tuple[int, float] | None = None
        self._last = 0

    def pts(self, timestamp: int, arrival: float) -> int:
        elapsed = 0
        if self._anchor is not None:
            elapsed = (
                (timestamp - self._anchor[0] + 0x80000000) & 0xFFFFFFFF
            ) - 0x80000000
            expected = arrival - self._anchor[1]
            if abs(elapsed / self._rate - expected) > MAX_TIMESTAMP_DRIFT:
                self._anchor = None
        if self._anchor is None:
            self._anchor = (timestamp, arrival)
            elapsed = 0
        seconds = self._anchor[1] - self._origin + elapsed / self._rate
        pts = PTS_START + round(seconds * TS_CLOCK_RATE)
        # Strictly increasing, ffmpeg drops repeated or earlier timestamps.
        self._last = pts if pts > self._last else self._last + 1
        return self._last


def recording_ffmpeg_args(
    selected: hksv_recording.SelectedRecordingConfiguration,
    *,
    audio: bool,
    source_audio: bool = True,
    ffmpeg: str = "ffmpeg",
    video_encoder: str = "libx264",
    encoder_args: tuple[str, ...] = (),
) -> list[str]:
    """The ffmpeg command transcoding MPEG-TS on stdin to the hub's selection.

    ``audio`` is whether the hub wants audio; without it, or without
    ``source_audio``, the recording gets a silent track, the hub expects one.
    """
    video = selected.video
    attributes = video.attributes[0]
    fps = attributes.frame_rate
    width, height = attributes.width, attributes.height
    # Fragments start at each IRAP picture, so the IRAP interval is the
    # fragment length the hub selected.
    containers = selected.recording.media_containers
    fragment_ms = (
        containers[0].fragment_length_ms if containers else video.iframe_interval_ms
    )
    interval = max(fragment_ms / 1000, MIN_FRAGMENT_SECONDS)
    profile = video.profile[0] if isinstance(video.profile, list) else video.profile
    level = video.level[0] if isinstance(video.level, list) else video.level
    sample_rate = SAMPLE_RATES[selected.audio.sample_rate]
    channels = selected.audio.channels
    audio_bitrate = selected.audio.max_audio_bitrate_kbps or 64
    real_audio = audio and source_audio
    command = [
        *(ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats"),
        # Media is ready at once; do not wait seconds to probe it.
        *("-analyzeduration", "1000000", "-f", "mpegts", "-i", "pipe:0"),
    ]
    if not real_audio:
        layout = "mono" if channels == 1 else "stereo"
        command += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={layout}"]
    encoder = []
    if video_encoder in X264_ENCODERS:
        encoder = [
            *("-level:v", H264_LEVELS.get(level, "4.0")),
            *("-preset", "veryfast", "-tune", "zerolatency"),
        ]
    command += [
        *("-map", "0:v:0", "-c:v", video_encoder),
        *("-profile:v", H264_PROFILES.get(profile, "main")),
        *encoder,
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        # The hub expects exactly the selected size, other aspect ratios are padded.
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        *("-r", str(fps), "-b:v", f"{video.bitrate_kbps}k"),
        *(
            "-maxrate",
            f"{video.bitrate_kbps}k",
            "-bufsize",
            f"{video.bitrate_kbps * 2}k",
        ),
        *("-bf", "0", "-g", str(math.ceil(fps * interval))),
        *("-force_key_frames", f"expr:gte(t,n_forced*{interval:g})"),
        # Scene cuts would add IRAP pictures and split fragments.
        *("-sc_threshold", "0"),
        *("-map", "0:a:0" if real_audio else "1:a:0"),
        *("-c:a", "aac", "-profile:a", "aac_low"),
        *("-ac", str(channels), "-ar", str(sample_rate), "-b:a", f"{audio_bitrate}k"),
    ]
    if not real_audio:
        command.append("-shortest")
    command += [
        *(
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof+skip_trailer",
        ),
        "pipe:1",
    ]
    return command


class Recorder:
    """Buffers a source and transcodes it into recordings, one at a time.

    ``on_data(chunk)`` gets ffmpeg's fragmented MP4 output and
    ``on_ended(error)`` the end of a recording, None for a normal end.
    ``on_closed(reason)`` fires when the source ends.
    """

    def __init__(
        self, source: MediaSource, *, prebuffer: float, audio: bool = True
    ) -> None:
        """Attach the ring to ``source``; it buffers from now on."""
        self.on_data: Callable[[bytes], None] | None = None
        self.on_ended: Callable[[str | None], None] | None = None
        self.on_closed: Callable[[str], None] | None = None
        self.audio = audio
        self.ring = PrebufferRing(prebuffer)
        self.ring.on_closed = self._source_closed
        self._source = source
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # Stops started from callbacks, kept so they are not collected.
        self._stops: set[asyncio.Task[None]] = set()
        self._truncated = False
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._pending = 0
        self._closed = False
        source.attach(self.ring)

    @property
    def closed(self) -> bool:
        """Whether the recorder or its source ended."""
        return self._closed

    @property
    def recording(self) -> bool:
        """Whether a recording runs."""
        return self._process is not None

    async def start(self, command: list[str], prebuffer: float) -> None:
        """Run ``command`` on the last ``prebuffer`` seconds and the live media."""
        if self._closed:
            raise RuntimeError("Recorder is closed")
        if self._process is not None:
            raise RuntimeError("A recording runs already")
        items = self.ring.snapshot(prebuffer)
        origin = items[0].arrival if items else time.monotonic()
        video = _Timestamps(VIDEO_CLOCK_RATE, origin)
        audio = _Timestamps(AUDIO_CLOCK_RATE, origin)
        muxer = MpegTsMuxer(audio=self.audio)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queue = queue
        self._pending = 0
        self._truncated = False
        started = bool(items)

        def mux(item: BufferedMedia) -> None:
            nonlocal started
            if item.unit is not None:
                if not started and not item.keyframe:
                    return
                started = True
                data = muxer.video(
                    item.unit.nals,
                    video.pts(item.unit.timestamp, item.arrival),
                    item.keyframe,
                )
            elif item.audio is not None and started:
                data = muxer.opus(
                    item.audio.payload, audio.pts(item.audio.timestamp, item.arrival)
                )
            else:
                return
            self._pending += len(data)
            if self._pending > MAX_PENDING_BYTES:
                self.ring.listener = None
                self._truncated = True
                logger.warning("Recording transcoder fell behind, ending it")
                queue.put_nowait(None)
                return
            queue.put_nowait(data)

        logger.debug(
            "Recording starts with %.1f s of buffered media",
            time.monotonic() - origin if items else 0.0,
        )
        for item in items:
            mux(item)
        self.ring.listener = mux
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            self.ring.listener = None
            self._queue = None
            raise
        logger.info("Recording transcoder started, pid %d", self._process.pid)
        process = self._process
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._write(process, queue)),
            loop.create_task(self._read(process)),
            loop.create_task(_log_stderr(process)),
        ]

    async def _write(
        self, process: asyncio.subprocess.Process, queue: asyncio.Queue[bytes | None]
    ) -> None:
        stdin = process.stdin
        assert stdin is not None  # noqa: S101
        try:
            while (data := await queue.get()) is not None:
                self._pending -= len(data)
                stdin.write(data)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        assert stdout is not None  # noqa: S101
        while chunk := await stdout.read(OUTPUT_CHUNK):
            if self.on_data is not None:
                self.on_data(chunk)
        code = await process.wait()
        if self._process is not process:
            return
        if code != 0:
            self._finish(f"ffmpeg exited with {code}")
        elif self._truncated:
            self._finish("the transcoder fell behind, the recording was cut short")
        else:
            self._finish(None)

    def _finish(self, error: str | None) -> None:
        self.ring.listener = None
        self._process = None
        if self._queue is not None:
            self._queue.put_nowait(None)
            self._queue = None
        if self.on_ended is not None:
            self.on_ended(error)

    async def stop(self) -> None:
        """End the running recording; ``on_ended`` is not called."""
        process, self._process = self._process, None
        self.ring.listener = None
        if self._queue is not None:
            self._queue.put_nowait(None)
            self._queue = None
        tasks, self._tasks = self._tasks, []
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), FFMPEG_STOP_TIMEOUT)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """Stop recording and buffering."""
        if self._closed:
            return
        self._closed = True
        await self.stop()
        self._source.detach(self.ring)

    def _source_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            task = asyncio.get_running_loop().create_task(self.stop())
            self._stops.add(task)
            task.add_done_callback(self._stops.discard)
            if self.on_ended is not None:
                self.on_ended(f"media source ended: {reason}")
        if self.on_closed is not None:
            self.on_closed(reason)


async def _log_stderr(process: asyncio.subprocess.Process) -> None:
    stream = process.stderr
    if stream is None:
        return
    lines = StderrLines()
    while chunk := await stream.read(4096):
        for line in lines.feed(chunk):
            _log_line(line)
    for line in lines.end():
        _log_line(line)


def _log_line(line: bytes) -> None:
    text = line.decode(errors="replace").rstrip()
    if text:
        logger.debug("Recording ffmpeg: %s", redact_urls(text))
