"""Run media sources, senders and recorders in a worker thread or process.

SFrame, SRTP and pacing cost CPU for every packet of every session. In an
application that also runs other work on its event loop (Home Assistant runs
HAP signalling on its main loop) the media path therefore runs on a loop of
its own. Only the signalling stays on the caller's loop: the proxies here
have the async API of :class:`~pyhap.media.source.MediaSource`, the
WebRTC and multi-tier senders and :class:`~pyhap.media.recording.Recorder`,
and their events are called on the caller's loop.

One :class:`MediaWorker` is one asyncio loop, shared by all cameras of the
application; one loop easily carries several sessions. It starts on first
use (or with :meth:`MediaWorker.start`). When it ends unexpectedly, every
session and source of it ends with a reason, and the next use starts a new
one.

By default the worker is a thread of the caller's process. It shares the
loggers and costs no second interpreter, but shares the GIL with the
application. With ``MediaWorker(separate_process=True)`` it runs in a child
process instead, which needs a POSIX system. That process logs to a file of
its own, ``pyhap-media-<pid>.log`` in ``log_dir``, rotated by size; its
stderr goes there too, so a crash leaves its traceback. Starting a worker
process removes such files that were not written for a week.

Either way the worker talks over a socket pair in length-prefixed frames:
a 4-byte JSON length, a 4-byte binary length, a JSON object and raw bytes
(recording data, talkback audio). Nothing is pickled.

Fan-out: register a source per camera channel with
:meth:`MediaWorker.open_source`, point the media pull at its
``rtp_input`` and pass it to each :meth:`MediaWorker.create_webrtc_sender` of
that channel. The source reassembles H.265 once; SFrame and SRTP run per
session. A sender without a source gets its own, closed with the session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
import contextlib
import itertools
import logging
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Any

import orjson

from . import MediaError
from .sdp import AudioParams, IceCandidateLike, VideoParams

logger = logging.getLogger(__name__)

HEADER = struct.Struct(">II")
MAX_MESSAGE = 4 * 1024 * 1024
DEFAULT_START_TIMEOUT = 15.0
# Requests run inside HAP writes, whose hooks get at most 9 s; one second less
# than the shortest sensible hook timeout leaves time to answer the write.
DEFAULT_REQUEST_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
WORKER_MODULE = "pyhap.media._worker"
# A worker process logs with this logger, at the parent's effective level of
# LEVEL_LOGGER.
WORKER_LOGGER = "pyhap"
LEVEL_LOGGER = "pyhap.media"
# The log files of worker processes: name, size limit and backups per file,
# and the age after which the next worker start removes them.
LOG_FILE_PREFIX = "pyhap-media-"
LOG_FILE_MAX_BYTES = 1024 * 1024
LOG_FILE_BACKUPS = 2
LOG_FILE_MAX_AGE = 7 * 24 * 3600
THREAD_NAME = "pyhap-media-worker"


class MediaWorkerError(MediaError):
    """The media worker failed, exited or refused a request."""


def encode_message(message: dict[str, Any], blob: bytes = b"") -> bytes:
    """Frame one message and its binary attachment."""
    data = orjson.dumps(message)
    if len(data) + len(blob) > MAX_MESSAGE:
        raise ValueError("Media worker message too large")
    return HEADER.pack(len(data), len(blob)) + data + blob


async def read_message(reader: asyncio.StreamReader) -> tuple[dict[str, Any], bytes]:
    """Read one framed message; ``asyncio.IncompleteReadError`` at EOF."""
    size, blob_size = HEADER.unpack(await reader.readexactly(HEADER.size))
    if size + blob_size > MAX_MESSAGE:
        raise ValueError("Media worker message too large")
    message = orjson.loads(await reader.readexactly(size))
    if not isinstance(message, dict):
        raise ValueError("Media worker message is not an object")
    blob = await reader.readexactly(blob_size) if blob_size else b""
    return message, blob


def _call(callback: Callable[..., None] | None, *args: Any) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        logger.exception("Error in media worker callback")


class RemoteMediaSource:
    """A :class:`~pyhap.media.source.MediaSource` in the worker.

    ``on_closed(reason)`` fires when the worker exits.
    """

    def __init__(self, worker: MediaWorker, source_id: int, generation: int) -> None:
        """Use :meth:`MediaWorker.open_source`."""
        self.on_closed: Callable[[str], None] | None = None
        self.id = source_id
        self.rtp_input: tuple[str, int, int] = ("", 0, 0)
        self._worker = worker
        self._generation = generation
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the source was closed or its worker exited."""
        return self._closed

    @property
    def has_audio(self) -> bool:
        """Whether the source takes audio."""
        return self.rtp_input[2] != 0

    async def close(self, reason: str = "closed") -> None:
        """Release the ports and end every session on this source."""
        if self._closed:
            return
        self._closed = True
        self._worker._sources.pop(self.id, None)
        with contextlib.suppress(MediaWorkerError):
            await self._worker._request(
                "close_source", self._generation, source=self.id, reason=reason
            )

    def _ended(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        _call(self.on_closed, reason)


class RemoteWebRTCSender:
    """A :class:`~pyhap.media.sender.WebRTCSender` in the worker.

    Same methods and callbacks; ``on_closed(reason)`` also fires when the
    worker exits.
    """

    def __init__(
        self,
        worker: MediaWorker,
        session_id: int,
        source: RemoteMediaSource | None,
        config: dict[str, Any],
    ) -> None:
        """Use :meth:`MediaWorker.create_webrtc_sender`."""
        self.on_closed: Callable[[str], None] | None = None
        self.on_connected: Callable[[], None] | None = None
        self.on_keyframe_request: (
            Callable[[float | None, float | None], None] | None
        ) = None
        self.on_audio: Callable[[bytes, int], None] | None = None
        self.id = session_id
        self.rtp_input: tuple[str, int, int] = ("", 0, 0)
        self._worker = worker
        self._source = source
        self._config = config
        self._generation: int | None = None
        self._state = "new"
        self._settled = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Whether media flows."""
        return self._state == "connected"

    @property
    def closed(self) -> bool:
        """Whether the session ended."""
        return self._state == "closed"

    async def create_offer(self) -> str:
        """Create the session in the worker and return the offer SDP."""
        if self._state != "new":
            raise MediaWorkerError("Offer already created")
        self._state = "offering"
        source = self._source
        try:
            if source is not None:
                generation = source._generation
                if source.closed:
                    raise MediaWorkerError("Media source is closed")
            else:
                generation = await self._worker.start()
            if self._state == "closed":
                raise MediaWorkerError("WebRTC session closed")
            self._generation = generation
            result = await self._worker._request(
                "offer",
                generation,
                session=self.id,
                source=source.id if source else None,
                config=self._config,
            )
        except BaseException:
            await self.close()
            raise
        if self._state == "closed":
            # Closed while the worker made the offer: end it there too.
            with contextlib.suppress(MediaWorkerError):
                await self._worker._request("close", generation, session=self.id)
            raise MediaWorkerError("WebRTC session closed")
        self._worker._handles[self.id] = self
        self._state = "offered"
        self.rtp_input = tuple(result["rtp_input"])  # type: ignore[assignment]
        return str(result["sdp"])

    async def apply_answer(
        self, answer_sdp: str, candidates: Iterable[IceCandidateLike] = ()
    ) -> None:
        """Accept the controller's answer; the worker connects in the background."""
        await self._session_request(
            "answer",
            sdp=answer_sdp,
            candidates=[
                [c.candidate, c.sdp_mid, c.sdp_mline_index] for c in candidates
            ],
        )

    async def reoffer(self, offer_sdp: str) -> str:
        """Answer a controller re-offer."""
        result = await self._session_request("reoffer", sdp=offer_sdp)
        return str(result["sdp"])

    async def update_receive_keys(
        self, add: Iterable[Any] = (), remove: Iterable[int] = ()
    ) -> None:
        """Install and remove the controller's SFrame receive keys."""
        await self._session_request(
            "update_keys",
            add=[[bytes(k.key).hex(), str(k.kid)] for k in add],
            remove=[str(kid) for kid in remove],
        )

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until ICE and DTLS are up; raises if the session ends first."""
        await asyncio.wait_for(self._settled.wait(), timeout)
        if not self.connected:
            raise MediaWorkerError("WebRTC session closed")

    async def close(self) -> None:
        """End the session without calling ``on_closed``."""
        if self._state == "closed":
            return
        generation = self._generation
        self._ended(None)
        if generation is not None:
            with contextlib.suppress(MediaWorkerError):
                await self._worker._request("close", generation, session=self.id)

    async def _session_request(self, op: str, **fields: Any) -> Any:
        if self._state in ("new", "offering", "closed") or self._generation is None:
            raise MediaWorkerError("No WebRTC session in the worker")
        return await self._worker._request(
            op, self._generation, session=self.id, **fields
        )

    def _event(self, event: str, message: dict[str, Any], blob: bytes) -> None:
        if self._state == "closed":
            return
        if event == "connected":
            self._state = "connected"
            self._settled.set()
            _call(self.on_connected)
        elif event == "keyframe_request":
            _call(self.on_keyframe_request, message.get("age"), message.get("gop"))
        elif event == "audio":
            _call(self.on_audio, blob, int(message["timestamp"]))
        elif event == "closed":
            self._ended(str(message.get("reason", "closed")))

    def _ended(self, reason: str | None) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        self._settled.set()
        self._worker._handles.pop(self.id, None)
        if reason is not None:
            _call(self.on_closed, reason)


class RemoteMultiTierSender:
    """A :class:`~pyhap.media.multi_tier.MultiTierSender` in the worker.

    Same methods and callbacks; ``on_closed(reason)`` also fires when the
    worker exits.
    """

    def __init__(
        self, worker: MediaWorker, session_id: int, config: dict[str, Any]
    ) -> None:
        """Use :meth:`MediaWorker.create_multi_tier_sender`."""
        self.on_closed: Callable[[str], None] | None = None
        self.on_keyframe_request: (
            Callable[[float | None, float | None], None] | None
        ) = None
        self.on_rtcp: Callable[[], None] | None = None
        self.id = session_id
        self.ports: tuple[int, int] = (0, 0)
        self._worker = worker
        self._config = config
        self._generation: int | None = None
        self._state = "new"

    @property
    def started(self) -> bool:
        """Whether media flows."""
        return self._state == "started"

    @property
    def closed(self) -> bool:
        """Whether the session ended."""
        return self._state == "closed"

    async def bind(self) -> tuple[int, int]:
        """Create the session in the worker and bind the accessory's ports."""
        if self._state != "new":
            raise MediaWorkerError("Multi-tier session already bound")
        self._state = "binding"
        try:
            generation = await self._worker.start()
            if self._state == "closed":
                raise MediaWorkerError("Multi-tier session closed")
            self._generation = generation
            result = await self._worker._request(
                "mt_bind", generation, session=self.id, config=self._config
            )
        except BaseException:
            await self.close()
            raise
        if self._state == "closed":
            with contextlib.suppress(MediaWorkerError):
                await self._worker._request("close", generation, session=self.id)
            raise MediaWorkerError("Multi-tier session closed")
        self._worker._handles[self.id] = self
        self._state = "bound"
        self.ports = (int(result["ports"][0]), int(result["ports"][1]))
        return self.ports

    async def start(self, source: RemoteMediaSource, audio: bool = True) -> None:
        """Send ``source`` to the controller, with its audio if ``audio``."""
        await self._session_request("mt_start", source, audio=audio)
        if self._state == "bound":
            self._state = "started"

    async def switch(self, source: RemoteMediaSource) -> None:
        """Continue the stream from another source of the same worker."""
        await self._session_request("mt_switch", source)

    async def close(self) -> None:
        """End the session without calling ``on_closed``."""
        if self._state == "closed":
            return
        generation = self._generation
        self._ended(None)
        if generation is not None:
            with contextlib.suppress(MediaWorkerError):
                await self._worker._request("close", generation, session=self.id)

    async def _session_request(
        self, op: str, source: RemoteMediaSource, **fields: Any
    ) -> Any:
        if self._state not in ("bound", "started") or self._generation is None:
            raise MediaWorkerError("No multi-tier session in the worker")
        if source._worker is not self._worker or source._generation != self._generation:
            raise MediaWorkerError("The source belongs to another worker")
        if source.closed:
            raise MediaWorkerError("Media source is closed")
        return await self._worker._request(
            op, self._generation, session=self.id, source=source.id, **fields
        )

    def _event(self, event: str, message: dict[str, Any], blob: bytes) -> None:
        if self._state == "closed":
            return
        if event == "rtcp":
            _call(self.on_rtcp)
        elif event == "keyframe_request":
            _call(self.on_keyframe_request, message.get("age"), message.get("gop"))
        elif event == "closed":
            self._ended(str(message.get("reason", "closed")))

    def _ended(self, reason: str | None) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        self._worker._handles.pop(self.id, None)
        if reason is not None:
            _call(self.on_closed, reason)


class RemoteRecorder:
    """A :class:`~pyhap.media.recording.Recorder` in the worker.

    Same methods and callbacks; ``on_closed(reason)`` also fires when the
    worker exits, after ``on_ended`` for a running recording.
    """

    def __init__(
        self, worker: MediaWorker, recorder_id: int, source: RemoteMediaSource
    ) -> None:
        """Use :meth:`MediaWorker.open_recorder`."""
        self.on_data: Callable[[bytes], None] | None = None
        self.on_ended: Callable[[str | None], None] | None = None
        self.on_closed: Callable[[str], None] | None = None
        self.id = recorder_id
        self._worker = worker
        self._source = source
        self._generation = source._generation
        self._recording = False
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the recorder, its source or its worker ended."""
        return self._closed

    @property
    def recording(self) -> bool:
        """Whether a recording runs."""
        return self._recording

    async def open(self, prebuffer: float, audio: bool) -> None:
        """Attach the recorder to its source in the worker."""
        self._worker._handles[self.id] = self
        try:
            await self._worker._request(
                "rec_open",
                self._generation,
                session=self.id,
                source=self._source.id,
                prebuffer=prebuffer,
                audio=audio,
            )
        except BaseException:
            self._worker._handles.pop(self.id, None)
            self._closed = True
            raise

    async def start(self, command: list[str], prebuffer: float) -> None:
        """Run the transcoder on the buffer and the live media."""
        if self._closed:
            raise MediaWorkerError("Recorder is closed")
        # Set first: the end of a short recording may arrive with the answer.
        self._recording = True
        try:
            await self._worker._request(
                "rec_start",
                self._generation,
                session=self.id,
                command=command,
                prebuffer=prebuffer,
            )
        except BaseException:
            self._recording = False
            raise

    async def stop(self) -> None:
        """End the running recording without calling ``on_ended``."""
        if not self._recording or self._closed:
            self._recording = False
            return
        self._recording = False
        with contextlib.suppress(MediaWorkerError):
            await self._worker._request("rec_stop", self._generation, session=self.id)

    async def close(self) -> None:
        """Stop recording and buffering."""
        if self._closed:
            return
        self._closed = True
        self._recording = False
        self._worker._handles.pop(self.id, None)
        with contextlib.suppress(MediaWorkerError):
            await self._worker._request("close", self._generation, session=self.id)

    def _event(self, event: str, message: dict[str, Any], blob: bytes) -> None:
        if self._closed:
            return
        if event == "rec_data":
            _call(self.on_data, blob)
        elif event == "rec_ended":
            if self._recording:
                self._recording = False
                error = message.get("error")
                _call(self.on_ended, None if error is None else str(error))
        elif event == "closed":
            self._ended(str(message.get("reason", "closed")))

    def _ended(self, reason: str | None) -> None:
        if self._closed:
            return
        if self._recording:
            self._recording = False
            _call(self.on_ended, f"media source ended: {reason}")
        self._closed = True
        self._worker._handles.pop(self.id, None)
        if reason is not None:
            _call(self.on_closed, reason)


class _ProcessRunner:
    """The worker as a child process, talking over a socket pair."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._process = process

    @property
    def running(self) -> bool:
        return self._process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()

    async def wait(self) -> int:
        return await self._process.wait()


class _ThreadRunner:
    """The worker as a thread with its own loop, talking over a socket pair."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        done: asyncio.Future[int],
        thread: threading.Thread,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.thread = thread
        self._done = done

    @property
    def running(self) -> bool:
        return not self._done.done()

    @property
    def pid(self) -> int | None:
        return None

    def kill(self) -> None:
        # A thread cannot be killed: drop the connection, which ends a worker
        # that still runs its loop, and count the worker as gone either way.
        self.writer.close()
        if not self._done.done():
            self._done.set_result(-1)

    async def wait(self) -> int:
        return await asyncio.shield(self._done)


async def _start_thread() -> _ThreadRunner:
    from . import _worker  # noqa: PLC0415 - the worker module imports this one

    loop = asyncio.get_running_loop()
    done: asyncio.Future[int] = loop.create_future()
    parent, child = socket.socketpair()

    def finished(code: int) -> None:
        if not done.done():
            done.set_result(code)

    def run() -> None:
        code = 0
        try:
            asyncio.run(_worker.serve_socket(child))
        except Exception:
            logger.exception("Media worker thread failed")
            code = 1
        finally:
            child.close()
            # The parent loop may be gone when the application shuts down.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(finished, code)

    try:
        reader, writer = await asyncio.open_connection(sock=parent)
    except BaseException:
        parent.close()
        child.close()
        raise
    thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
    thread.start()
    return _ThreadRunner(reader, writer, done, thread)


class MediaWorker:
    """A worker thread, or with ``separate_process`` a process, running media."""

    def __init__(
        self,
        *,
        separate_process: bool = False,
        python: str | None = None,
        log_dir: str | os.PathLike[str] | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        """Configure the worker; it starts on first use.

        ``separate_process`` runs it in a child process with the interpreter
        ``python`` (default: this one) instead of a thread of this process.
        That process logs to a file in ``log_dir`` (default: ``pyhap-media``
        in the temporary directory).
        """
        self.on_exit: Callable[[str], None] | None = None
        self._separate_process = separate_process
        self._python = python or sys.executable
        self._log_dir = os.fspath(log_dir) if log_dir else _default_log_dir()
        self._start_timeout = start_timeout
        self._request_timeout = request_timeout
        self._runner: _ProcessRunner | _ThreadRunner | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._stopping = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handles: dict[
            int, RemoteWebRTCSender | RemoteMultiTierSender | RemoteRecorder
        ] = {}
        self._sources: dict[int, RemoteMediaSource] = {}

    @property
    def separate_process(self) -> bool:
        """Whether the worker runs in a child process instead of a thread."""
        return self._separate_process

    @property
    def running(self) -> bool:
        """Whether the worker is up."""
        return self._runner is not None and self._runner.running

    @property
    def pid(self) -> int | None:
        """The worker's process ID while it runs as a child process."""
        return self._runner.pid if self.running and self._runner else None

    async def start(self) -> int:
        """Start the worker unless it runs; return its generation."""
        async with self._lock:
            if self.running and self._ready is not None and self._ready.done():
                return self._generation
            await self._spawn()
            return self._generation

    async def _spawn(self) -> None:
        runner = await (
            self._start_process() if self._separate_process else _start_thread()
        )
        if self._handles or self._sources or self._pending:
            # The previous worker exited and its reader did not get to it yet.
            self._exited(self._generation, "media worker exited")
        self._generation += 1
        self._stopping = False
        self._runner = runner
        self._ready = asyncio.get_running_loop().create_future()
        self._reader = asyncio.get_running_loop().create_task(
            self._read_loop(runner, self._generation)
        )
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self._start_timeout)
        except BaseException:
            await self._kill(runner)
            raise
        if runner.pid is None:
            logger.debug("Media worker started in a thread")
        else:
            logger.debug(
                "Media worker %s started, logging to %s",
                runner.pid,
                log_file(self._log_dir, runner.pid),
            )

    async def _start_process(self) -> _ProcessRunner:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            os.path.abspath(path) for path in sys.path if path
        )
        await asyncio.get_running_loop().run_in_executor(
            None, prepare_log_dir, self._log_dir
        )
        parent, child = socket.socketpair()
        try:
            with child:
                # -P and a fixed working directory: the worker uses exactly
                # the parent's import path.
                process = await asyncio.create_subprocess_exec(
                    self._python,
                    "-P",
                    "-m",
                    WORKER_MODULE,
                    str(child.fileno()),
                    str(_log_level()),
                    self._log_dir,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    pass_fds=(child.fileno(),),
                    env=env,
                    cwd=os.path.abspath(os.sep),
                )
            reader, writer = await asyncio.open_connection(sock=parent)
        except BaseException:
            parent.close()
            raise
        return _ProcessRunner(process, reader, writer)

    async def update_log_levels(self) -> None:
        """Apply the parent's current log levels in the worker.

        A worker process starts with the level of that moment; call this
        after changing it, e.g. when an integration turns on debug logging. A
        worker thread shares the loggers, so there is nothing to apply.
        """
        if not self._separate_process:
            return
        await self._request("log_level", self._generation, level=_log_level())

    async def open_source(
        self, host: str = "127.0.0.1", audio: bool = True
    ) -> RemoteMediaSource:
        """Bind a media source in the worker; see ``rtp_input``."""
        generation = await self.start()
        source = RemoteMediaSource(self, next(self._ids), generation)
        self._sources[source.id] = source
        try:
            result = await self._request(
                "open_source", generation, source=source.id, host=host, audio=audio
            )
        except BaseException:
            self._sources.pop(source.id, None)
            source._closed = True
            raise
        source.rtp_input = tuple(result["rtp_input"])  # type: ignore[assignment]
        return source

    def create_webrtc_sender(
        self,
        *,
        video: VideoParams,
        audio: AudioParams | None = None,
        sframe: Any | None,
        source: RemoteMediaSource | None = None,
        host_addresses: Sequence[str] | None = None,
        use_ipv6: bool = True,
        rtp_input_host: str = "127.0.0.1",
        **options: float,
    ) -> RemoteWebRTCSender:
        """Create a sender proxy; the session starts with ``create_offer``.

        The arguments are those of :class:`~pyhap.media.sender.WebRTCSender`;
        ``options`` are its numeric ones (timeouts, ``max_payload``,
        ``pacing_factor``, ``max_queue_delay``, ``keyframe_request_interval``).
        """
        if source is not None and source._worker is not self:
            raise ValueError("The source belongs to another worker")
        unknown = set(options) - _NUMERIC_OPTIONS
        if unknown:
            raise TypeError(f"Unknown sender options: {sorted(unknown)}")
        config = {
            "video": _fields(video),
            "audio": _fields(audio) if audio else None,
            "sframe": None
            if sframe is None
            else [bytes(sframe.key).hex(), str(sframe.kid)],
            "host_addresses": None if host_addresses is None else list(host_addresses),
            "use_ipv6": use_ipv6,
            "rtp_input_host": rtp_input_host,
            "options": options,
        }
        return RemoteWebRTCSender(self, next(self._ids), source, config)

    def create_multi_tier_sender(
        self,
        *,
        address: str,
        video_port: int,
        audio_port: int,
        video_key: bytes,
        audio_key: bytes,
        video_ssrc: int,
        audio_ssrc: int,
        **options: Any,
    ) -> RemoteMultiTierSender:
        """Create a multi-tier sender proxy; :meth:`RemoteMultiTierSender.bind`.

        The arguments are those of
        :class:`~pyhap.media.multi_tier.MultiTierSender`.
        """
        unknown = set(options) - _MULTI_TIER_OPTIONS
        if unknown:
            raise TypeError(f"Unknown multi-tier sender options: {sorted(unknown)}")
        config = {
            "address": address,
            "video_port": video_port,
            "audio_port": audio_port,
            "video_key": bytes(video_key).hex(),
            "audio_key": bytes(audio_key).hex(),
            "video_ssrc": video_ssrc,
            "audio_ssrc": audio_ssrc,
            **options,
        }
        return RemoteMultiTierSender(self, next(self._ids), config)

    async def open_recorder(
        self, source: RemoteMediaSource, *, prebuffer: float, audio: bool = True
    ) -> RemoteRecorder:
        """Buffer ``source`` in the worker for recordings, see ``Recorder``."""
        if source._worker is not self:
            raise ValueError("The source belongs to another worker")
        if source.closed:
            raise MediaWorkerError("Media source is closed")
        recorder = RemoteRecorder(self, next(self._ids), source)
        await recorder.open(prebuffer, audio)
        return recorder

    async def close(self) -> None:
        """End every session and stop the worker."""
        async with self._lock:
            runner = self._runner
            if runner is None or not runner.running:
                return
            self._stopping = True
            with contextlib.suppress(MediaWorkerError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._request("shutdown", self._generation), STOP_TIMEOUT
                )
            runner.writer.close()
            try:
                await asyncio.wait_for(runner.wait(), STOP_TIMEOUT)
            except TimeoutError:
                await self._kill(runner)
            if self._reader is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reader

    async def _kill(self, runner: _ProcessRunner | _ThreadRunner) -> None:
        runner.kill()
        await runner.wait()

    async def _request(self, op: str, generation: int, **fields: Any) -> Any:
        runner = self._runner
        if generation != self._generation or runner is None or not runner.running:
            raise MediaWorkerError("Media worker is not running")
        request_id = next(self._ids)
        try:
            data = encode_message({"id": request_id, "op": op, **fields})
        except (ValueError, TypeError) as err:
            raise MediaWorkerError(f"Request {op} not sent: {err}") from err
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            return await asyncio.wait_for(
                self._exchange(runner.writer, data, future), self._request_timeout
            )
        except (ConnectionError, BrokenPipeError) as err:
            raise MediaWorkerError("Media worker is not running") from err
        except TimeoutError as err:
            # A worker that stops answering is stuck: end it, so its sessions
            # end and the next use starts a new one.
            logger.warning("Media worker did not answer %s, stopping it", op)
            runner.kill()
            raise MediaWorkerError(f"Media worker did not answer {op}") from err
        finally:
            self._pending.pop(request_id, None)

    @staticmethod
    async def _exchange(
        writer: asyncio.StreamWriter, data: bytes, future: asyncio.Future[Any]
    ) -> Any:
        """Send a request and wait for its answer, under one deadline."""
        writer.write(data)
        await writer.drain()
        return await future

    async def _read_loop(
        self, runner: _ProcessRunner | _ThreadRunner, generation: int
    ) -> None:
        reason = "media worker exited"
        try:
            while True:
                message, blob = await read_message(runner.reader)
                self._dispatch(message, blob)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a broken stream ends the worker
            logger.warning("Media worker sent invalid data: %r", err)
            reason = "media worker protocol error"
            runner.kill()
        code = await runner.wait()
        if self._stopping:
            reason = "media worker stopped"
        self._exited(generation, f"{reason} (code {code})")

    def _dispatch(self, message: dict[str, Any], blob: bytes) -> None:
        if "id" in message:
            future = self._pending.get(int(message["id"]))
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(MediaWorkerError(str(message["error"])))
            else:
                future.set_result(message.get("result"))
            return
        event = message.get("event")
        if event == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
        elif event == "source_closed":
            source = self._sources.pop(int(message["source"]), None)
            if source is not None:
                source._ended(str(message.get("reason", "closed")))
        elif "session" in message:
            sender = self._handles.get(int(message["session"]))
            if sender is not None:
                sender._event(str(event), message, blob)

    def _exited(self, generation: int, reason: str) -> None:
        if generation != self._generation:
            return
        logger.debug("Media worker ended: %s", reason)
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(MediaWorkerError(reason))
            self._ready.exception()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MediaWorkerError(reason))
                # Retrieved here so a caller that gave up logs no warning.
                future.exception()
        senders, self._handles = self._handles, {}
        sources, self._sources = self._sources, {}
        for sender in senders.values():
            sender._ended(reason)
        for source in sources.values():
            source._ended(reason)
        _call(self.on_exit, reason)


_NUMERIC_OPTIONS = {
    "offer_timeout",
    "negotiation_timeout",
    "idle_timeout",
    "max_payload",
    "pacing_factor",
    "max_queue_delay",
    "keyframe_request_interval",
}

_MULTI_TIER_OPTIONS = {
    "video_payload_type",
    "audio_payload_type",
    "local_video_port",
    "local_audio_port",
    "is_ipv6",
    "max_payload",
    "idle_timeout",
    "keyframe_request_interval",
    "rtcp_notify_interval",
}


def _log_level() -> int:
    """The level a worker process logs at."""
    return logging.getLogger(LEVEL_LOGGER).getEffectiveLevel()


def _default_log_dir() -> str:
    name = "pyhap-media"
    if hasattr(os, "getuid"):
        # The temporary directory may be shared between users.
        name += f"-{os.getuid()}"
    return os.path.join(tempfile.gettempdir(), name)


def log_file(log_dir: str, pid: int) -> str:
    """The log file of the worker process ``pid``."""
    return os.path.join(log_dir, f"{LOG_FILE_PREFIX}{pid}.log")


def prepare_log_dir(log_dir: str) -> None:
    """Create ``log_dir`` and remove worker log files not written for a week."""
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    oldest = time.time() - LOG_FILE_MAX_AGE
    with os.scandir(log_dir) as entries:
        for entry in entries:
            if not entry.name.startswith(LOG_FILE_PREFIX) or ".log" not in entry.name:
                continue
            with contextlib.suppress(OSError):
                if entry.is_file() and entry.stat().st_mtime < oldest:
                    os.unlink(entry.path)


def _fields(params: VideoParams | AudioParams) -> dict[str, Any]:
    return dict(vars(params))
