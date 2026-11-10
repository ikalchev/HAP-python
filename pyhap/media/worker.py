"""Run media sources and senders in a worker process.

SRTP and pacing cost CPU for every packet of every session. In an
application that also runs other work on its event loop (Home Assistant runs
HAP signalling on its main loop) the media path therefore runs in a separate
process. Only the signalling stays on the caller's loop: the proxies here
have the async API of :class:`~pyhap.media.multi_tier.MultiTierSender` and
:class:`~pyhap.media.source.MediaSource`, and their events are called on the
caller's loop.

One :class:`MediaWorker` is one process with one asyncio loop, shared by
all cameras of the application. A process per camera would cost a Python
interpreter and the crypto imports for each camera, while one loop easily
carries several sessions; an integrator that wants isolation or more cores
can still create several workers. The process starts on first use (or with
:meth:`MediaWorker.start`). When it exits unexpectedly, every session
and source of it ends with a reason, and the next use starts a new one.

The worker talks over its stdin and stdout pipes in length-prefixed frames:
a 4-byte JSON length, a 4-byte binary length, a JSON object and raw bytes.
Nothing is pickled. The worker moves its own stdout aside
so stray prints cannot corrupt the stream, and forwards the records of its
``pyhap`` loggers to the parent's loggers, at the parent's
levels.

Fan-out: register a source per camera channel with
:meth:`MediaWorker.open_source`, point the media pull at its
``rtp_input`` and start each sender of that channel with it.
The source reassembles H.265 once; SRTP runs per session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import itertools
import logging
import os
import struct
import sys
from typing import Any

import orjson

from . import MediaError
from .sdp import AudioParams, VideoParams

logger = logging.getLogger(__name__)

HEADER = struct.Struct(">II")
MAX_MESSAGE = 4 * 1024 * 1024
DEFAULT_START_TIMEOUT = 15.0
# Requests run inside HAP writes, whose hooks get at most 9 s; one second less
# than the shortest sensible hook timeout leaves time to answer the write.
DEFAULT_REQUEST_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
WORKER_MODULE = "pyhap.media._worker"
# The loggers the worker forwards, each at the parent's effective level of
# the logger named here.
FORWARDED_LOGGERS = {"pyhap": "pyhap.media"}


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
    """A :class:`~pyhap.media.source.MediaSource` in the worker process.

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


class RemoteMultiTierSender:
    """A :class:`~pyhap.media.multi_tier.MultiTierSender` in the worker process.

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


class MediaWorker:
    """A worker process running media sources and senders."""

    def __init__(
        self,
        *,
        python: str | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        """Configure the worker; it starts on first use."""
        self.on_exit: Callable[[str], None] | None = None
        self._python = python or sys.executable
        self._start_timeout = start_timeout
        self._request_timeout = request_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._stopping = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handles: dict[int, RemoteMultiTierSender] = {}
        self._sources: dict[int, RemoteMediaSource] = {}

    @property
    def running(self) -> bool:
        """Whether the worker process is up."""
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        """The worker's process ID while it runs."""
        return self._process.pid if self.running and self._process else None

    async def start(self) -> int:
        """Start the worker unless it runs; return its generation."""
        async with self._lock:
            if self.running and self._ready is not None and self._ready.done():
                return self._generation
            await self._spawn()
            return self._generation

    async def _spawn(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            os.path.abspath(path) for path in sys.path if path
        )
        levels = [str(level) for level in _log_levels()]
        # -P and a fixed working directory: the worker uses exactly the
        # parent's import path.
        process = await asyncio.create_subprocess_exec(
            self._python,
            "-P",
            "-m",
            WORKER_MODULE,
            *levels,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
            cwd=os.path.abspath(os.sep),
        )
        if self._handles or self._sources or self._pending:
            # The previous process exited and its reader did not get to it yet.
            self._exited(self._generation, "media worker exited")
        self._generation += 1
        self._stopping = False
        self._process = process
        self._ready = asyncio.get_running_loop().create_future()
        self._reader = asyncio.get_running_loop().create_task(
            self._read_loop(process, self._generation)
        )
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self._start_timeout)
        except BaseException:
            await self._kill(process)
            raise
        logger.debug("Media worker %s started", process.pid)

    async def update_log_levels(self) -> None:
        """Apply the parent's current log levels in the worker.

        The worker starts with the levels of that moment; call this after
        changing them, e.g. when an integration turns on debug logging.
        """
        await self._request("log_levels", self._generation, levels=_log_levels())

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

    async def close(self) -> None:
        """End every session and stop the worker."""
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None:
                return
            self._stopping = True
            with contextlib.suppress(MediaWorkerError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._request("shutdown", self._generation), STOP_TIMEOUT
                )
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            except TimeoutError:
                await self._kill(process)
            if self._reader is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reader

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()

    async def _request(self, op: str, generation: int, **fields: Any) -> Any:
        process = self._process
        if (
            generation != self._generation
            or process is None
            or process.stdin is None
            or not self.running
        ):
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
                self._exchange(process.stdin, data, future), self._request_timeout
            )
        except (ConnectionError, BrokenPipeError) as err:
            raise MediaWorkerError("Media worker is not running") from err
        except TimeoutError as err:
            # A worker that stops answering is stuck: end it, so its sessions
            # end and the next use starts a new one.
            logger.warning("Media worker did not answer %s, stopping it", op)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise MediaWorkerError(f"Media worker did not answer {op}") from err
        finally:
            self._pending.pop(request_id, None)

    @staticmethod
    async def _exchange(
        stdin: asyncio.StreamWriter, data: bytes, future: asyncio.Future[Any]
    ) -> Any:
        """Send a request and wait for its answer, under one deadline."""
        stdin.write(data)
        await stdin.drain()
        return await future

    async def _read_loop(
        self, process: asyncio.subprocess.Process, generation: int
    ) -> None:
        stdout = process.stdout
        reason = "media worker exited"
        try:
            while stdout is not None:
                message, blob = await read_message(stdout)
                self._dispatch(message, blob)
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a broken stream ends the worker
            logger.warning("Media worker sent invalid data: %r", err)
            reason = "media worker protocol error"
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        code = await process.wait()
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
        elif event == "log":
            logging.getLogger(str(message.get("name", __name__))).log(
                int(message.get("level", logging.INFO)),
                "[media worker] %s",
                message.get("message", ""),
            )
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


def _log_levels() -> list[int]:
    """The parent's effective level of each forwarded logger."""
    return [
        logging.getLogger(name).getEffectiveLevel()
        for name in FORWARDED_LOGGERS.values()
    ]


def _fields(params: VideoParams | AudioParams) -> dict[str, Any]:
    return dict(vars(params))
