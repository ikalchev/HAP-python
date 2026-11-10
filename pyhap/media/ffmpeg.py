"""A reference source provider that pulls each camera channel with ffmpeg.

:class:`FFmpegSourceProvider` is a ``source_provider`` for
:class:`~pyhap.media.multi_tier_delegate.MultiTierMediaDelegate`: it
resolves the tier of a session to ffmpeg input arguments and leases that
channel's source from its :class:`~pyhap.media.pool.SourcePool`, with an
:class:`FFmpegPull` as the channel's pull. All sessions and recordings of a
channel share one ffmpeg.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import contextlib
from dataclasses import dataclass, replace
from functools import partial
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from pyhap import hksv3
from pyhap.camera_stream import StreamError

from .pool import ChannelLease, SourcePool

if TYPE_CHECKING:
    from pyhap.camera_stream import StreamSession

    from .worker import MediaWorker

logger = logging.getLogger(__name__)

_OFFER_ERROR = hksv3.WebRTCSolicitOfferStatus.ERROR
# Seconds ffmpeg gets to exit after SIGTERM before it is killed.
FFMPEG_STOP_TIMEOUT = 3.0
# Userinfo may hold spaces and quotes, so it is matched up to its "@" first.
_URL = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?:[^/?#@\n]*@)?(?P<rest>[^\s'\"]+)"
)
# Bytes of one stderr line kept before it is logged anyway.
MAX_LOG_LINE = 4096
# Restarts after failures back off up to this many seconds.
MAX_RESTART_DELAY = 30.0
# An ffmpeg that ran this long counts as healthy again.
HEALTHY_RUN = 30.0


def redact_urls(text: str) -> str:
    """Keep only scheme, host and port of the URLs in ``text``.

    Credentials, path and query of a camera URL may carry secrets, such as
    an access token in the path.
    """
    return _URL.sub(_redact_url, text)


def _redact_url(match: re.Match[str]) -> str:
    rest = match["rest"]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = authority.rpartition("@")[2]
    hidden = "/<redacted>" if len(authority) < len(rest) else ""
    return f"{match['scheme']}{host}{hidden}"


@dataclass(frozen=True)
class FFmpegInput:
    """The ffmpeg input of a camera channel.

    ``args`` are ffmpeg's input arguments, such as ``["-rtsp_transport",
    "tcp", "-i", url]``. The other fields override the provider's defaults
    when set: ``video_map`` and ``audio_map`` select the streams, ``audio``
    turns audio off for inputs without it, and ``gop`` is the expected
    interval between IRAP pictures until the source has measured it.
    """

    args: tuple[str, ...]
    video_map: str | None = None
    audio_map: str | None = None
    audio: bool | None = None
    gop: float | None = None


InputResolver = Callable[
    [hksv3.VideoStreamTier], Awaitable["Sequence[str] | FFmpegInput | None"]
]


class FFmpegSourceProvider:
    """A ``source_provider`` that pulls each channel with ffmpeg.

    ``inputs`` maps a tier identifier, or a :class:`pyhap.hksv3.VideoQuality`
    (also by name, e.g. ``"medium"``), to ffmpeg input arguments or an
    :class:`FFmpegInput`; an identifier wins over a quality. It can instead
    be an async callable that resolves a tier to the same, or None when
    nothing fits. Tiers with the same input share one ffmpeg.

    Video ``video_map`` is copied with ``dump_extra`` so every IRAP picture
    carries its parameter sets (an Annex B input such as RTSP; MP4 needs
    ``hevc_mp4toannexb``); ``audio_map``, unless ``audio`` is false, is
    encoded to mono Opus. ffmpeg fails when a mapped stream is missing, so
    set ``audio=False`` for inputs without audio. The Opus bit rate and frame
    duration follow the audio tier of the session that opens the channel; a
    camera has one audio tier for its multi-tier and WebRTC sessions, so all
    sessions of a channel get the same audio. The command is an argument
    list that no shell sees; the URLs in ffmpeg's log are reduced to scheme,
    host and port.

    ffmpeg is restarted when it exits while sessions need it (after
    ``restart_delay``), and for a keyframe request only when no IRAP picture
    came for twice the GOP and ffmpeg runs at least that long: a copied
    stream gets its next IRAP picture with the next GOP anyway. The GOP is
    the one the source measures, until then ``gop``.
    """

    def __init__(
        self,
        inputs: Mapping[int | hksv3.VideoQuality | str, Sequence[str] | FFmpegInput]
        | InputResolver,
        *,
        worker: MediaWorker | None = None,
        ffmpeg: str = "ffmpeg",
        video_map: str = "0:v:0",
        audio_map: str = "0:a:0",
        audio: bool = True,
        gop: float = 2.0,
        restart_delay: float = 1.0,
        stats_period: float | None = None,
        host: str = "127.0.0.1",
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        """Configure the inputs; nothing runs before the first answer.

        ``on_error(channel, message)`` hears each unexpected ffmpeg exit or
        failed start, e.g. to mark the camera unavailable.
        """
        self._resolver: InputResolver | None = None
        self._by_id: dict[int, FFmpegInput] = {}
        self._by_quality: dict[int, FFmpegInput] = {}
        if not isinstance(inputs, Mapping):
            self._resolver = inputs
        else:
            for key, value in inputs.items():
                if isinstance(key, str):
                    self._by_quality[hksv3.VideoQuality[key.upper()]] = _input(value)
                elif isinstance(key, hksv3.VideoQuality):
                    self._by_quality[key] = _input(value)
                else:
                    self._by_id[key] = _input(value)
        self.pool = SourcePool(worker, host)
        self.ffmpeg = ffmpeg
        self.video_map = video_map
        self.audio_map = audio_map
        self.audio = audio
        self.gop = gop
        self.restart_delay = restart_delay
        self.stats_period = stats_period
        self.on_error = on_error
        self._pulls: dict[str, FFmpegPull] = {}

    async def input_for(self, tier: hksv3.VideoStreamTier) -> FFmpegInput:
        """The input of a tier, with the provider's defaults filled in.

        :raises StreamError: If no input fits the tier.
        """
        if self._resolver is not None:
            value = await self._resolver(tier)
            found = None if value is None else _input(value)
        else:
            found = self._by_id.get(tier.identifier) or self._by_quality.get(
                tier.quality
            )
        if found is None:
            raise StreamError(_OFFER_ERROR, f"No ffmpeg input for tier {tier}")
        return FFmpegInput(
            found.args,
            found.video_map or self.video_map,
            found.audio_map or self.audio_map,
            self.audio if found.audio is None else found.audio,
            found.gop or self.gop,
        )

    def channel_name(self, tier: hksv3.VideoStreamTier) -> str:
        """How the log names the channel of a tier, without its input."""
        if tier.identifier in self._by_id:
            return f"tier {tier.identifier}"
        with contextlib.suppress(ValueError):
            return hksv3.VideoQuality(tier.quality).name.lower()
        return f"tier {tier.identifier}"

    def command(
        self,
        ffmpeg_input: FFmpegInput,
        rtp_input: tuple[str, int, int],
        audio_bitrate: int,
        packet_time: int = 20,
    ) -> list[str]:
        """The ffmpeg arguments sending one input to a source's ports."""
        host, video_port, audio_port = rtp_input
        host = f"[{host}]" if ":" in host else host
        stats = (
            ["-stats", "-stats_period", str(self.stats_period)]
            if self.stats_period
            else ["-nostats"]
        )
        video_map = ffmpeg_input.video_map or self.video_map
        command = [
            *(self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"),
            *stats,
            *ffmpeg_input.args,
            *("-map", video_map, "-c:v", "copy", "-bsf:v", "dump_extra"),
            *("-payload_type", str(hksv3.VIDEO_PAYLOAD_TYPE)),
            # The source drops RTCP, and port + 1 may be the audio input.
            *("-rtpflags", "skip_rtcp", "-f", "rtp", f"rtp://{host}:{video_port}"),
        ]
        if audio_port:
            audio_map = ffmpeg_input.audio_map or self.audio_map
            command += [
                *("-map", audio_map, "-c:a", "libopus", "-ac", "1", "-ar", "48000"),
                *("-b:a", str(audio_bitrate), "-application", "lowdelay"),
                *("-frame_duration", str(packet_time)),
                *("-payload_type", str(hksv3.AUDIO_PAYLOAD_TYPE)),
                *("-rtpflags", "skip_rtcp", "-f", "rtp", f"rtp://{host}:{audio_port}"),
            ]
        return command

    async def __call__(
        self,
        session: StreamSession,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None,
    ) -> ChannelLease:
        """Lease the source of the session's channel.

        ``audio_tier`` is None for a multi-tier session without audio; the
        channel's audio then follows :data:`pyhap.hksv3.DEFAULT_AUDIO_TIER`.
        """
        return await self.lease_channel(video_tier, audio_tier)

    async def lease_channel(
        self,
        video_tier: hksv3.VideoStreamTier,
        audio_tier: hksv3.AudioStreamTier | None = None,
    ) -> ChannelLease:
        """Lease the source of a tier's channel without a session.

        For example to keep a recording channel buffered; see
        :class:`~pyhap.media.recording_delegate.RecordingMediaDelegate`.
        """
        found = await self.input_for(video_tier)
        name = self.channel_name(video_tier)
        audio = audio_tier or hksv3.DEFAULT_AUDIO_TIER
        command = partial(
            self.command,
            found,
            audio_bitrate=audio.target_bitrate,
            packet_time=audio.packet_time,
        )
        gop = found.gop or self.gop

        def make_pull() -> FFmpegPull:
            pull = FFmpegPull(
                command, name, gop, self.restart_delay, on_error=self._pull_error
            )
            self._pulls[name] = pull
            return pull

        return await self.pool.lease(
            replace(found, gop=None),
            make_pull,
            audio=bool(found.audio),
            name=name,
        )

    def _pull_error(self, channel: str, message: str) -> None:
        if self.on_error is not None:
            try:
                self.on_error(channel, message)
            except Exception:
                logger.exception("Error in the ffmpeg error callback")

    def diagnostics(self) -> dict[str, Any]:
        """Leases and the ffmpeg of each channel, for an integration's diagnostics."""
        counts = self.pool.counts()
        worker = self.pool.worker
        return {
            "worker_pid": worker.pid if worker is not None else None,
            "channels": {
                name: {
                    "leases": leases,
                    "active": active,
                    **(self._pulls[name].diagnostics() if name in self._pulls else {}),
                }
                for name, (leases, active) in counts.items()
            },
        }

    async def close(self) -> None:
        """Stop every ffmpeg and close every source."""
        await self.pool.close()


def _input(value: Sequence[str] | FFmpegInput) -> FFmpegInput:
    if isinstance(value, FFmpegInput):
        return replace(value, args=tuple(value.args))
    return FFmpegInput(tuple(value))


class FFmpegPull:
    """One ffmpeg process feeding a channel's source, see ``ChannelPull``."""

    def __init__(
        self,
        command: Callable[[tuple[str, int, int]], list[str]],
        name: str,
        gop: float,
        restart_delay: float,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        """Configure the pull; :meth:`start` runs ffmpeg.

        ``gop`` is the expected IRAP interval until the source measured one.
        ``on_error(name, message)`` hears each unexpected exit or failed start.
        """
        self.name = name
        self.gop = gop
        self.restart_delay = restart_delay
        self.on_error = on_error
        self.restarts = 0
        self.last_error: str | None = None
        self._failures = 0
        self._command = command
        self._argv: list[str] = []
        self._process: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._started = 0.0
        self._running = False
        self._restart_now = asyncio.Event()

    @property
    def pid(self) -> int | None:
        """The process ID of the running ffmpeg."""
        process = self._process
        return process.pid if process and process.returncode is None else None

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Run ffmpeg and keep it running until :meth:`stop`."""
        self._argv = self._command(rtp_input)
        self._running = True
        try:
            await self._spawn()
        except BaseException:
            self._running = False
            raise
        self._watcher = asyncio.ensure_future(self._watch())

    async def _spawn(self) -> None:
        self._restart_now.clear()
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._started = time.monotonic()
        logger.info("ffmpeg for %s started, pid %d", self.name, self._process.pid)

    def diagnostics(self) -> dict[str, Any]:
        """The pid, restarts and last error, for diagnostics."""
        return {
            "pid": self.pid,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }

    async def _watch(self) -> None:
        while self._running and self._process is not None:
            process = self._process
            last_line = await _log_output(self.name, process)
            code = await process.wait()
            if not self._running:
                return
            if not self._restart_now.is_set():
                if time.monotonic() - self._started >= HEALTHY_RUN:
                    self._failures = 0
                await self._back_off(
                    f"exited with {code}" + (f": {last_line}" if last_line else "")
                )
            while self._running:
                try:
                    await self._spawn()
                except OSError as err:
                    # No new process: back off further before the next try.
                    await self._back_off(f"could not start: {err}")
                    continue
                self.restarts += 1
                break

    async def _back_off(self, message: str) -> None:
        """Report ``message`` and wait the next backoff delay or a restart."""
        delay = min(self.restart_delay * 2**self._failures, MAX_RESTART_DELAY)
        self._failures += 1
        self._error(message, delay)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._restart_now.wait(), delay)

    def _error(self, message: str, delay: float) -> None:
        self.last_error = message
        logger.warning(
            "ffmpeg for %s %s, restarting in %.1f s", self.name, message, delay
        )
        if self.on_error is not None:
            try:
                self.on_error(self.name, message)
            except Exception:
                logger.exception("Error in the ffmpeg error callback")

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Restart ffmpeg if its stream has gone without IRAP pictures.

        ``gop`` is the IRAP interval the source measured, if it did.
        """
        limit = 2 * (gop or self.gop)
        if not self._running or self._restart_now.is_set():
            return
        if age is not None and age <= limit:
            return
        running = time.monotonic() - self._started
        if running < limit:
            # Started recently, its first IRAP picture is on the way.
            return
        logger.info(
            "Restarting ffmpeg for %s for an IRAP picture (last %s s ago)",
            self.name,
            "-" if age is None else f"{age:.1f}",
        )
        self._restart_now.set()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    async def stop(self) -> None:
        """Stop ffmpeg, killing it if it does not exit in time."""
        self._running = False
        self._restart_now.set()
        await self._terminate()
        if self._watcher is not None:
            await self._watcher
        # The watcher may have started one more meanwhile.
        await self._terminate()
        process = self._process
        if process is not None:
            logger.info(
                "ffmpeg for %s stopped (exit %s, %d restarts)",
                self.name,
                process.returncode,
                self.restarts,
            )

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), FFMPEG_STOP_TIMEOUT)
        except TimeoutError:
            logger.warning("Killing ffmpeg for %s", self.name)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def _log_output(name: str, process: asyncio.subprocess.Process) -> str | None:
    """Log ffmpeg's output line by line, URLs redacted; progress ends with CR.

    Returns the last line, redacted, to explain an exit.
    """
    stream = process.stderr
    if stream is None:
        return None
    lines = StderrLines()
    last: bytes | None = None
    while chunk := await stream.read(4096):
        for line in lines.feed(chunk):
            _log_line(name, line)
            last = line
    for line in lines.end():
        _log_line(name, line)
        last = line
    if last is None:
        return None
    return redact_urls(last.decode(errors="replace")).strip() or None


class StderrLines:
    """Splits ffmpeg's stderr into lines of at most about MAX_LOG_LINE bytes.

    An overlong line is returned once, cut, and its rest skipped up to the
    next line end. Logging it in parts could split a URL so that its
    credentials escape redaction.
    """

    def __init__(self) -> None:
        self.pending = b""
        self._skipping = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add output, return the lines it completes."""
        lines = (self.pending + chunk).replace(b"\r", b"\n").split(b"\n")
        self.pending = lines.pop()
        if self._skipping and lines:
            lines.pop(0)
            self._skipping = False
        done = [_cut(line) for line in lines if line]
        if self._skipping:
            self.pending = b""
        elif len(self.pending) > MAX_LOG_LINE:
            done.append(_cut(self.pending))
            self.pending = b""
            self._skipping = True
        return done

    def end(self) -> list[bytes]:
        """Return the last line, which had no line end."""
        return [self.pending] if self.pending else []


def _cut(line: bytes) -> bytes:
    """Cut an overlong line before its last URL and its last partial word.

    A URL may hold spaces in its userinfo, so a cut URL is dropped whole.
    """
    if len(line) <= MAX_LOG_LINE:
        return line
    head = line[:MAX_LOG_LINE]
    if (url := head.rfind(b"://")) != -1:
        head = head[:url]
    return head.rpartition(b" ")[0] + b" [truncated]"


def _log_line(name: str, line: bytes) -> None:
    logger.debug("ffmpeg %s: %s", name, redact_urls(line.decode(errors="replace")))
