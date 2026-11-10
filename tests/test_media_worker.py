"""Tests for pyhap.media.worker: the framing and lifecycle of the worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyhap.media import _worker  # noqa: E402
from pyhap.media.worker import (  # noqa: E402
    MAX_MESSAGE,
    MediaWorker,
    encode_message,
    read_message,
)

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
async def worker() -> AsyncIterator[MediaWorker]:
    """Yield a started worker and stop it afterwards."""
    started = MediaWorker()
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
    assert worker._process is not None
    worker._process.kill()


class Output:
    """Collects what the worker side writes to its output pipe."""

    def __init__(self) -> None:
        """Start empty."""
        self.data = b""

    def is_closing(self) -> bool:
        """The pipe stays open."""
        return False

    def write(self, data: bytes) -> None:
        """Keep the frame."""
        self.data += data


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
