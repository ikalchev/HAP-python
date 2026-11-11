"""Tests for the thread mode of pyhap.media.worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import socket
import threading

import pytest

from pyhap.media import _worker
from pyhap.media.worker import (
    THREAD_NAME,
    MediaWorker,
    MediaWorkerError,
    _ThreadRunner,
)

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
async def worker() -> AsyncIterator[MediaWorker]:
    """Yield a started worker thread and stop it afterwards."""
    started = MediaWorker()
    await started.start()
    yield started
    await started.close()


def thread_of(worker: MediaWorker) -> threading.Thread:
    """The thread the worker runs in."""
    runner = worker._runner
    assert isinstance(runner, _ThreadRunner)
    assert runner.thread.name == THREAD_NAME
    return runner.thread


async def wait_until_gone(thread: threading.Thread) -> None:
    """Wait until the worker thread ended."""
    for _ in range(200):
        if not thread.is_alive():
            return
        await asyncio.sleep(0.01)
    pytest.fail("worker thread still runs")


async def test_lifecycle(worker: MediaWorker) -> None:
    """Verify the thread starts once, stops on close and starts again on use."""
    assert not worker.separate_process
    assert worker.running
    assert worker.pid is None
    thread = thread_of(worker)
    assert thread.is_alive()
    generation = await worker.start()
    assert thread_of(worker) is thread

    await worker.close()
    assert not worker.running
    await wait_until_gone(thread)
    await worker.close()

    assert await worker.start() == generation + 1
    assert worker.running
    assert thread_of(worker) is not thread


async def test_source_round_trip(worker: MediaWorker) -> None:
    """Verify a source opens and closes in the thread like in a process."""
    source = await worker.open_source(audio=False)
    host, video_port, audio_port = source.rtp_input
    assert host == "127.0.0.1"
    assert video_port > 0
    assert audio_port == 0
    assert not source.has_audio
    await source.close()
    assert source.closed


async def test_close_ends_sources() -> None:
    """Verify closing the worker ends its sources with a reason."""
    worker = MediaWorker()
    source = await worker.open_source()
    thread = thread_of(worker)
    reasons: list[str] = []
    source.on_closed = reasons.append
    exits: list[str] = []
    worker.on_exit = exits.append

    await worker.close()

    assert source.closed
    assert reasons == [exits[0]]
    assert "stopped" in exits[0]
    await wait_until_gone(thread)


async def test_kill_ends_sources_and_restarts(worker: MediaWorker) -> None:
    """Verify a dropped thread ends its sources and the next use starts anew."""
    source = await worker.open_source()
    reasons: list[str] = []
    source.on_closed = reasons.append
    assert worker._runner is not None

    worker._runner.kill()
    for _ in range(200):
        if reasons:
            break
        await asyncio.sleep(0.01)

    assert source.closed
    assert len(reasons) == 1
    again = await worker.open_source()
    assert not again.closed
    assert worker.running


async def test_loggers_are_shared(worker: MediaWorker) -> None:
    """Verify the thread neither forwards records nor touches logger levels."""
    media = logging.getLogger("pyhap.media")
    handlers = list(logging.getLogger("pyhap").handlers)
    level = media.level
    propagate = logging.getLogger("pyhap").propagate

    await worker.update_log_levels()
    await worker.open_source()

    assert logging.getLogger("pyhap").handlers == handlers
    assert media.level == level
    assert logging.getLogger("pyhap").propagate == propagate


async def test_failed_thread_fails_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a worker thread that dies before it is ready fails the start."""

    async def broken(sock: socket.socket) -> None:
        sock.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(_worker, "serve_socket", broken)
    worker = MediaWorker(start_timeout=5)

    with pytest.raises(MediaWorkerError):
        await worker.start()
    assert not worker.running
