"""The media worker, see :mod:`pyhap.media.worker`.

:func:`serve_socket` serves one parent over a socket pair, in a thread of the
parent or in a process of its own. As a process it is started as
``python -m pyhap.media._worker <socket fd> <log level> <log dir>``, logs to
its own file in the log directory and ends when the parent closes the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
from typing import TYPE_CHECKING, Any, NamedTuple

from . import MediaError
from .multi_tier import MultiTierSender
from .recording import Recorder
from .sdp import AudioParams, VideoParams
from .source import MediaSource
from .worker import (
    LOG_FILE_BACKUPS,
    LOG_FILE_MAX_BYTES,
    WORKER_LOGGER,
    encode_message,
    log_file,
    read_message,
)

if TYPE_CHECKING:
    from .sender import WebRTCSender

logger = logging.getLogger(__name__)

# Bytes waiting for the parent above which the worker gives up on a parent
# that stopped reading.
MAX_OUTPUT_BUFFER = 64 * 1024 * 1024


class _Key(NamedTuple):
    key: bytes
    kid: int


class _Candidate(NamedTuple):
    candidate: str
    sdp_mid: str | None
    sdp_mline_index: int | None


class _LogFile(RotatingFileHandler):
    """A rotating log file that also takes the process's stderr and stdout."""

    def _open(self) -> Any:
        stream = super()._open()
        # Tracebacks, faulthandler dumps and prints follow the current file.
        os.dup2(stream.fileno(), 2)
        os.dup2(stream.fileno(), 1)
        return stream


class _Server:
    def __init__(self, output: asyncio.WriteTransport) -> None:
        self._output = output
        self._sessions: dict[int, WebRTCSender | MultiTierSender | Recorder] = {}
        self._sources: dict[int, MediaSource] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.stopping = asyncio.Event()

    def send(self, message: dict[str, Any], blob: bytes = b"") -> None:
        output = self._output
        if output.is_closing():
            return
        if output.get_write_buffer_size() > MAX_OUTPUT_BUFFER:
            # The parent stopped reading; it sees the worker as gone.
            output.close()
            self.stopping.set()
            return
        output.write(encode_message(message, blob))

    def handle(self, message: dict[str, Any]) -> None:
        task = asyncio.get_running_loop().create_task(self._respond(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        try:
            handler = getattr(self, f"_op_{message['op']}")
            result = await handler(message)
        except (
            MediaError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OSError,
        ) as err:
            self.send({"id": request_id, "error": str(err) or type(err).__name__})
            return
        except Exception as err:
            logger.exception("Media worker request failed")
            self.send({"id": request_id, "error": repr(err)})
            return
        try:
            self.send({"id": request_id, "result": result})
        except (ValueError, TypeError) as err:
            # Too large or not serializable: the caller still gets an answer.
            self.send({"id": request_id, "error": f"Answer not sent: {err}"})

    async def _op_open_source(self, message: dict[str, Any]) -> dict[str, Any]:
        source_id = int(message["source"])
        source = MediaSource(str(message["host"]), audio=bool(message["audio"]))
        source.on_closed = lambda reason: self._source_closed(source_id, reason)
        self._sources[source_id] = source
        try:
            rtp_input = await source.start()
        except BaseException:
            self._sources.pop(source_id, None)
            source.on_closed = None
            source.close()
            raise
        return {"rtp_input": list(rtp_input)}

    def _source_closed(self, source_id: int, reason: str) -> None:
        if self._sources.pop(source_id, None) is not None:
            self.send({"event": "source_closed", "source": source_id, "reason": reason})

    async def _op_close_source(self, message: dict[str, Any]) -> None:
        source = self._sources.pop(int(message["source"]), None)
        if source is not None:
            source.close(str(message.get("reason", "closed")))

    async def _op_offer(self, message: dict[str, Any]) -> dict[str, Any]:
        session_id = int(message["session"])
        config = message["config"]
        source = None
        if message.get("source") is not None:
            source = self._sources.get(int(message["source"]))
            if source is None:
                raise MediaError("Unknown media source")
        sframe = config["sframe"]
        audio = config["audio"]
        from .sender import WebRTCSender  # noqa: PLC0415 - only for WebRTC sessions

        sender = WebRTCSender(
            video=VideoParams(**config["video"]),
            audio=AudioParams(**audio) if audio is not None else None,
            sframe=None
            if sframe is None
            else _Key(bytes.fromhex(sframe[0]), int(sframe[1])),
            source=source,
            host_addresses=config["host_addresses"],
            use_ipv6=bool(config["use_ipv6"]),
            rtp_input_host=str(config["rtp_input_host"]),
            **config["options"],
        )
        self._wire(session_id, sender)
        self._sessions[session_id] = sender
        try:
            sdp = await sender.create_offer()
        except BaseException:
            self._sessions.pop(session_id, None)
            raise
        return {"sdp": sdp, "rtp_input": list(sender.rtp_input)}

    def _wire(self, session_id: int, sender: WebRTCSender) -> None:
        def closed(reason: str) -> None:
            self._sessions.pop(session_id, None)
            self.send({"event": "closed", "session": session_id, "reason": reason})

        sender.on_closed = closed
        sender.on_connected = lambda: self.send(
            {"event": "connected", "session": session_id}
        )
        sender.on_keyframe_request = lambda age, gop: self.send(
            {"event": "keyframe_request", "session": session_id, "age": age, "gop": gop}
        )
        sender.on_audio = lambda opus, timestamp: self.send(
            {"event": "audio", "session": session_id, "timestamp": timestamp}, opus
        )

    def _session(self, message: dict[str, Any]) -> WebRTCSender:
        sender = self._sessions.get(int(message["session"]))
        if sender is None or isinstance(sender, (MultiTierSender, Recorder)):
            raise MediaError("Unknown WebRTC session")
        return sender

    def _multi_tier(self, message: dict[str, Any]) -> MultiTierSender:
        sender = self._sessions.get(int(message["session"]))
        if not isinstance(sender, MultiTierSender):
            raise MediaError("Unknown multi-tier session")
        return sender

    def _source(self, message: dict[str, Any]) -> MediaSource:
        source = self._sources.get(int(message["source"]))
        if source is None:
            raise MediaError("Unknown media source")
        return source

    async def _op_mt_bind(self, message: dict[str, Any]) -> dict[str, Any]:
        session_id = int(message["session"])
        config = dict(message["config"])
        config["video_key"] = bytes.fromhex(config["video_key"])
        config["audio_key"] = bytes.fromhex(config["audio_key"])
        sender = MultiTierSender(**config)

        def closed(reason: str) -> None:
            self._sessions.pop(session_id, None)
            self.send({"event": "closed", "session": session_id, "reason": reason})

        sender.on_closed = closed
        sender.on_rtcp = lambda: self.send({"event": "rtcp", "session": session_id})
        sender.on_keyframe_request = lambda age, gop: self.send(
            {"event": "keyframe_request", "session": session_id, "age": age, "gop": gop}
        )
        self._sessions[session_id] = sender
        try:
            ports = await sender.bind()
        except BaseException:
            self._sessions.pop(session_id, None)
            await sender.close()
            raise
        return {"ports": list(ports)}

    async def _op_mt_start(self, message: dict[str, Any]) -> None:
        await self._multi_tier(message).start(
            self._source(message), audio=bool(message["audio"])
        )

    async def _op_mt_switch(self, message: dict[str, Any]) -> None:
        await self._multi_tier(message).switch(self._source(message))

    async def _op_rec_open(self, message: dict[str, Any]) -> None:
        session_id = int(message["session"])
        recorder = Recorder(
            self._source(message),
            prebuffer=float(message["prebuffer"]),
            audio=bool(message["audio"]),
        )
        recorder.on_data = lambda chunk: self.send(
            {"event": "rec_data", "session": session_id}, chunk
        )
        recorder.on_ended = lambda error: self.send(
            {"event": "rec_ended", "session": session_id, "error": error}
        )

        def closed(reason: str) -> None:
            self._sessions.pop(session_id, None)
            self.send({"event": "closed", "session": session_id, "reason": reason})

        recorder.on_closed = closed
        self._sessions[session_id] = recorder

    def _recorder(self, message: dict[str, Any]) -> Recorder:
        recorder = self._sessions.get(int(message["session"]))
        if not isinstance(recorder, Recorder):
            raise MediaError("Unknown recorder")
        return recorder

    async def _op_rec_start(self, message: dict[str, Any]) -> None:
        await self._recorder(message).start(
            [str(arg) for arg in message["command"]], float(message["prebuffer"])
        )

    async def _op_rec_stop(self, message: dict[str, Any]) -> None:
        await self._recorder(message).stop()

    async def _op_answer(self, message: dict[str, Any]) -> None:
        candidates = [
            _Candidate(
                str(c), None if m is None else str(m), None if i is None else int(i)
            )
            for c, m, i in message["candidates"]
        ]
        await self._session(message).apply_answer(str(message["sdp"]), candidates)

    async def _op_reoffer(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"sdp": await self._session(message).reoffer(str(message["sdp"]))}

    async def _op_update_keys(self, message: dict[str, Any]) -> None:
        add = [_Key(bytes.fromhex(key), int(kid)) for key, kid in message["add"]]
        remove = [int(kid) for kid in message["remove"]]
        await self._session(message).update_receive_keys(add, remove)

    async def _op_close(self, message: dict[str, Any]) -> None:
        sender = self._sessions.pop(int(message["session"]), None)
        if sender is not None:
            await sender.close()

    async def _op_log_level(self, message: dict[str, Any]) -> None:
        logging.getLogger(WORKER_LOGGER).setLevel(int(message["level"]))

    async def _op_shutdown(self, message: dict[str, Any]) -> None:
        await self.close_all()
        self.stopping.set()

    async def close_all(self) -> None:
        sessions, self._sessions = self._sessions, {}
        await asyncio.gather(
            *(sender.close() for sender in sessions.values()), return_exceptions=True
        )
        sources, self._sources = self._sources, {}
        for source in sources.values():
            source.on_closed = None
            source.close("media worker stopped")


async def serve_socket(sock: socket.socket) -> None:
    """Serve one parent over a connected socket until it closes."""
    loop = asyncio.get_running_loop()
    reader, writer = await asyncio.open_connection(sock=sock)
    output = writer.transport
    server = _Server(output)
    server.send({"event": "ready"})
    stopping = loop.create_task(server.stopping.wait())
    try:
        while not server.stopping.is_set():
            reading = loop.create_task(read_message(reader))
            await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if not reading.done():
                reading.cancel()
                break
            try:
                message, _blob = reading.result()
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            server.handle(message)
    finally:
        stopping.cancel()
        await server.close_all()
        output.close()


def main() -> None:
    """Run the worker process until the parent closes its socket."""
    sock = socket.socket(fileno=int(sys.argv[1]))
    level = int(sys.argv[2])
    handler = _LogFile(
        log_file(sys.argv[3], os.getpid()),
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    worker_logger = logging.getLogger(WORKER_LOGGER)
    worker_logger.addHandler(handler)
    worker_logger.setLevel(level)
    worker_logger.propagate = False
    faulthandler.enable()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_socket(sock))


if __name__ == "__main__":
    main()
