"""The media worker process, see :mod:`pyhap.media.worker`.

Started as ``python -m pyhap.media._worker <log levels>``, one level per
logger in ``FORWARDED_LOGGERS``. Requests arrive framed on stdin; responses
and events leave framed on the original stdout, which is moved aside first.
The process ends when stdin closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from typing import Any

from . import MediaError
from .multi_tier import MultiTierSender
from .recording import Recorder
from .source import MediaSource
from .worker import FORWARDED_LOGGERS, encode_message, read_message

logger = logging.getLogger(__name__)

# Bytes waiting for the parent above which log records are dropped, and
# above which the worker gives up on a parent that stopped reading.
LOG_DROP_BUFFER = 1024 * 1024
MAX_OUTPUT_BUFFER = 64 * 1024 * 1024


class _LogForwarder(logging.Handler):
    def __init__(self, server: _Server) -> None:
        super().__init__()
        self._server = server

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self._server.send(
                {
                    "event": "log",
                    "level": record.levelno,
                    "name": record.name,
                    "message": message,
                }
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


class _Server:
    def __init__(self, output: asyncio.WriteTransport) -> None:
        self._output = output
        self._sessions: dict[int, MultiTierSender | Recorder] = {}
        self._sources: dict[int, MediaSource] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.stopping = asyncio.Event()

    def send(self, message: dict[str, Any], blob: bytes = b"") -> None:
        output = self._output
        if output.is_closing():
            return
        buffered = output.get_write_buffer_size()
        if message.get("event") == "log" and buffered > LOG_DROP_BUFFER:
            # A parent that reads slowly loses log records, not media events.
            return
        if buffered > MAX_OUTPUT_BUFFER:
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

    async def _op_close(self, message: dict[str, Any]) -> None:
        sender = self._sessions.pop(int(message["session"]), None)
        if sender is not None:
            await sender.close()

    async def _op_log_levels(self, message: dict[str, Any]) -> None:
        for name, level in zip(FORWARDED_LOGGERS, message["levels"], strict=False):
            logging.getLogger(name).setLevel(int(level))

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


async def _serve(output_fd: int) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(0, "rb", buffering=0)
    )
    output, _ = await loop.connect_write_pipe(
        asyncio.Protocol, os.fdopen(output_fd, "wb", buffering=0)
    )
    server = _Server(output)
    forwarder = _LogForwarder(server)
    for name in FORWARDED_LOGGERS:
        logging.getLogger(name).addHandler(forwarder)
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
            except asyncio.IncompleteReadError:
                break
            server.handle(message)
    finally:
        stopping.cancel()
        await server.close_all()
        output.close()


def main() -> None:
    """Run the worker until its stdin closes or it is told to stop."""
    # Keep the protocol stream for frames; anything printed goes to stderr.
    output_fd = os.dup(1)
    os.dup2(2, 1)
    levels = [int(arg) for arg in sys.argv[1:]]
    levels += [logging.WARNING] * (len(FORWARDED_LOGGERS) - len(levels))
    for name, level in zip(FORWARDED_LOGGERS, levels, strict=False):
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).propagate = False
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(output_fd))


if __name__ == "__main__":
    main()
