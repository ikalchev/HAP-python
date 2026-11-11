"""Tests for pyhap.media.worker: the framing and the worker process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import os
import pathlib
import signal
import time
from typing import Any

import pytest

from pyhap.media import _worker  # noqa: E402
from pyhap.media.worker import (  # noqa: E402
    LOG_FILE_MAX_AGE,
    MAX_MESSAGE,
    MediaWorker,
    encode_message,
    log_file,
    prepare_log_dir,
    read_message,
)

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
async def worker(tmp_path: pathlib.Path) -> AsyncIterator[MediaWorker]:
    """Yield a started worker process and stop it afterwards."""
    started = MediaWorker(separate_process=True, log_dir=tmp_path)
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


async def wait_for_line(path: pathlib.Path, text: str) -> str:
    """Wait until the file at ``path`` contains ``text``; return the file."""
    for _ in range(300):
        if path.exists() and text in (content := path.read_text()):
            return content
        await asyncio.sleep(0.01)
    pytest.fail(f"{text!r} not in {path}")


async def test_process_logs_to_its_own_file(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a worker process logs to its own file, not through the parent."""
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    worker = MediaWorker(separate_process=True, log_dir=tmp_path)
    try:
        await worker.open_source(audio=False)
        assert worker.pid is not None
        path = pathlib.Path(log_file(str(tmp_path), worker.pid))
        content = await wait_for_line(path, "Media input buffer")
        assert "[pyhap.media.source]" in content
        assert not any("Media input buffer" in r.getMessage() for r in caplog.records)
        assert str(path) in caplog.text
    finally:
        await worker.close()


async def test_process_follows_level_changes(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify the worker process logs debug records once the level allows them."""
    caplog.set_level(logging.WARNING, logger="pyhap.media")
    worker = MediaWorker(separate_process=True, log_dir=tmp_path)
    try:
        await worker.open_source(audio=False)
        assert worker.pid is not None
        path = pathlib.Path(log_file(str(tmp_path), worker.pid))
        await asyncio.sleep(0.1)
        assert "Media input buffer" not in path.read_text()
        caplog.set_level(logging.DEBUG, logger="pyhap.media")
        await worker.update_log_levels()
        await worker.open_source(audio=False)
        await wait_for_line(path, "Media input buffer")
    finally:
        await worker.close()


async def test_process_crash_output_lands_in_its_file(tmp_path: pathlib.Path) -> None:
    """Verify what the worker process writes to stderr ends up in its log file."""
    worker = MediaWorker(separate_process=True, log_dir=tmp_path)
    try:
        await worker.start()
        assert worker.pid is not None
        path = pathlib.Path(log_file(str(tmp_path), worker.pid))
        os.kill(worker.pid, signal.SIGSEGV)
        await wait_for_line(path, "Fatal Python error")
    finally:
        await worker.close()


def test_old_log_files_are_removed(tmp_path: pathlib.Path) -> None:
    """Verify starting a worker process removes stale log files only."""
    old = tmp_path / "pyhap-media-1.log"
    old_backup = tmp_path / "pyhap-media-1.log.1"
    fresh = tmp_path / "pyhap-media-2.log"
    other = tmp_path / "other.log"
    for path in (old, old_backup, fresh, other):
        path.write_text("x")
    stale = time.time() - LOG_FILE_MAX_AGE - 60
    for path in (old, old_backup, other):
        os.utime(path, (stale, stale))

    prepare_log_dir(str(tmp_path / "new"))
    prepare_log_dir(str(tmp_path))

    assert (tmp_path / "new").is_dir()
    assert not old.exists()
    assert not old_backup.exists()
    assert fresh.exists()
    assert other.exists()
