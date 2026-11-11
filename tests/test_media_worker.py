"""Tests for pyhap.media.worker: the framing and the worker process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import Any

import pytest

from pyhap.media import _worker  # noqa: E402
from pyhap.media.worker import (  # noqa: E402
    MAX_MESSAGE,
    MediaWorker,
    _relay_line,
    encode_message,
    read_message,
)

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
async def worker() -> AsyncIterator[MediaWorker]:
    """Yield a started worker process and stop it afterwards."""
    started = MediaWorker(separate_process=True)
    await started.start()
    yield started
    await started.close()


def reader_of(data: bytes) -> asyncio.StreamReader:
    """A stream reader that yields ``data`` and then EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_framing_round_trip() -> None:
    """Verify a message and its binary part survive the framing."""
    data = encode_message({"event": "audio", "timestamp": 960}, b"\x00opus")
    assert await read_message(reader_of(data)) == (
        {"event": "audio", "timestamp": 960},
        b"\x00opus",
    )


async def test_framing_rejects_oversized() -> None:
    """Verify neither side accepts a message over the size limit."""
    with pytest.raises(ValueError, match="too large"):
        encode_message({}, bytes(MAX_MESSAGE))
    header = (MAX_MESSAGE).to_bytes(4, "big") + (1).to_bytes(4, "big")
    with pytest.raises(ValueError, match="too large"):
        await read_message(reader_of(header))


async def test_framing_rejects_non_object() -> None:
    """Verify a frame must hold a JSON object."""
    data = (2).to_bytes(4, "big") + bytes(4) + b"[]"
    with pytest.raises(ValueError, match="object"):
        await read_message(reader_of(data))


async def test_lifecycle(worker: MediaWorker) -> None:
    """Verify start is idempotent, close stops the process and use restarts it."""
    pid = worker.pid
    await worker.start()
    assert worker.pid == pid
    await worker.close()
    assert not worker.running
    await worker.close()
    await worker.start()
    assert worker.running
    assert worker.pid != pid


def kill(worker: MediaWorker) -> None:
    """Crash the worker process."""
    assert worker._runner is not None
    worker._runner.kill()


class Output:
    """Collects what the worker side writes to its output pipe."""

    def __init__(self, buffered: int = 0) -> None:
        """Start empty, with ``buffered`` bytes the parent has not read."""
        self.data = b""
        self.buffered = buffered
        self.closed = False

    def is_closing(self) -> bool:
        """Whether the pipe was closed."""
        return self.closed

    def get_write_buffer_size(self) -> int:
        """The bytes the parent has not read."""
        return self.buffered

    def write(self, data: bytes) -> None:
        """Keep the frame."""
        self.data += data

    def close(self) -> None:
        """Close the pipe."""
        self.closed = True


async def unserializable(message: dict[str, Any]) -> dict[str, Any]:
    """A request handler whose result cannot be encoded."""
    return {"sdp": object()}


async def test_unencodable_answer_becomes_an_error() -> None:
    """Verify the worker answers with an error when its result cannot be sent."""
    output = Output()
    server = _worker._Server(output)  # ty: ignore[invalid-argument-type]
    server._op_open_source = unserializable  # type: ignore[method-assign]
    await server._respond({"id": 7, "op": "open_source"})
    message, _blob = await read_message(reader_of(output.data))
    assert message["id"] == 7
    assert message["error"].startswith("Answer not sent")


async def test_stalled_parent_stops_the_worker() -> None:
    """Verify the worker gives up on a parent that stopped reading."""
    output = Output(buffered=_worker.MAX_OUTPUT_BUFFER + 1)
    server = _worker._Server(output)  # ty: ignore[invalid-argument-type]
    server.send({"event": "source_closed", "source": 1, "reason": "x"})
    assert output.data == b""
    assert output.closed
    assert server.stopping.is_set()


async def test_worker_crash_ends_its_sources(worker: MediaWorker) -> None:
    """Verify a crashed worker closes its sources and the next use restarts it."""
    source = await worker.open_source()
    reasons: list[str] = []
    source.on_closed = reasons.append
    pid = worker.pid

    kill(worker)
    for _ in range(200):
        if reasons:
            break
        await asyncio.sleep(0.01)

    assert source.closed
    assert len(reasons) == 1
    again = await worker.open_source()
    assert worker.pid != pid
    assert not again.closed


async def wait_for_record(caplog: pytest.LogCaptureFixture, start: str) -> None:
    """Wait until a record whose message starts with ``start`` was logged."""
    for _ in range(200):
        if any(record.getMessage().startswith(start) for record in caplog.records):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"no record {start!r}")


async def test_process_records_use_parent_loggers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify a record of the worker process is logged by the parent's logger."""
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    worker = MediaWorker(separate_process=True)
    try:
        await worker.open_source(audio=False)
        await wait_for_record(caplog, "Media input buffer")
        (record,) = (
            record
            for record in caplog.records
            if record.getMessage().startswith("Media input buffer")
        )
        assert record.name == "pyhap.media.source"
        assert record.levelno == logging.DEBUG
        assert record.process == worker.pid
    finally:
        await worker.close()


async def test_process_follows_level_changes(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the worker process logs debug records once the level allows them."""
    caplog.set_level(logging.WARNING, logger="pyhap.media")
    worker = MediaWorker(separate_process=True)
    try:
        await worker.open_source(audio=False)
        caplog.set_level(logging.DEBUG, logger="pyhap.media")
        await asyncio.sleep(0.1)
        assert not any("Media input buffer" in r.getMessage() for r in caplog.records)
        await worker.update_log_levels()
        await worker.open_source(audio=False)
        await wait_for_record(caplog, "Media input buffer")
    finally:
        await worker.close()


def test_relay_line_logs_record(caplog: pytest.LogCaptureFixture) -> None:
    """Verify a JSON record keeps its logger, level and traceback."""
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    _relay_line(
        b'{"name": "pyhap.media.rtp", "level": 40, "message": "broken", '
        b'"exc": "Traceback: boom"}',
        42,
    )
    (record,) = caplog.records
    assert record.name == "pyhap.media.rtp"
    assert record.levelno == logging.ERROR
    assert record.process == 42
    assert "Traceback: boom" in caplog.text


def test_relay_line_respects_parent_level(caplog: pytest.LogCaptureFixture) -> None:
    """Verify a record below the parent's level is dropped."""
    caplog.set_level(logging.INFO, logger="pyhap.media")
    _relay_line(b'{"name": "pyhap.media.rtp", "level": 10, "message": "x"}', 42)
    assert not caplog.records


@pytest.mark.parametrize(
    "line", [b"Traceback (most recent call last):", b"[1, 2]", b'{"name": "x"}']
)
def test_relay_line_logs_other_output_as_warning(
    caplog: pytest.LogCaptureFixture, line: bytes
) -> None:
    """Verify output that is no record, like a crash traceback, is a warning."""
    _relay_line(line, 42)
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert line.decode() in record.getMessage()
