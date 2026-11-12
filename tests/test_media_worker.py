"""Tests for pyhap.media.worker: framing, worker processes and WebRTC senders."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import logging
import os
import pathlib
import signal
import time
from typing import Any

import pytest

from pyhap.media import AudioParams, _worker, worker as worker_module  # noqa: E402
from pyhap.media.worker import (  # noqa: E402
    LOG_FILE_MAX_AGE,
    MAX_MESSAGE,
    MediaWorker,
    MediaWorkerError,
    RemoteWebRTCSender,
    encode_message,
    log_file,
    prepare_log_dir,
    read_message,
)

from .media_peer import (  # noqa: E402
    KEYFRAME,
    TALKBACK_KEY,
    TRAIL,
    VIDEO,
    Controller,
    Source,
    negotiate,
    sender_options,
    video_count,
    wait_until,
)
from .webrtc_helpers import SFrameKey  # noqa: E402

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
async def worker(tmp_path: pathlib.Path) -> AsyncIterator[MediaWorker]:
    """Yield a started worker process and stop it afterwards."""
    started = MediaWorker(separate_process=True, log_dir=tmp_path)
    await started.start()
    yield started
    await started.close()


def remote_sender(worker: MediaWorker, **kwargs: Any) -> RemoteWebRTCSender:
    """Create a loopback sender proxy."""
    return worker.create_webrtc_sender(**sender_options(**kwargs))


class Events:
    """Records the callbacks of a sender proxy."""

    def __init__(self, sender: RemoteWebRTCSender) -> None:
        """Hook into every callback."""
        self.closed: list[str] = []
        self.connected = 0
        self.keyframes: list[float | None] = []
        self.audio: list[tuple[bytes, int]] = []
        sender.on_closed = self.closed.append
        sender.on_connected = self._connected
        sender.on_keyframe_request = lambda age, _gop: self.keyframes.append(age)
        sender.on_audio = lambda opus, ts: self.audio.append((opus, ts))

    def _connected(self) -> None:
        self.connected += 1


async def feed(source: Source, frames: list[list[bytes]]) -> None:
    """Send access units with a short pause, as a camera would."""
    for frame in frames:
        source.send_video(frame)
        await asyncio.sleep(0.01)


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


async def test_stop_ends_sessions(worker: MediaWorker) -> None:
    """Verify stopping the worker ends its sessions with a clear reason."""
    sender = remote_sender(worker)
    events = Events(sender)
    exits: list[str] = []
    worker.on_exit = exits.append
    await sender.create_offer()
    await worker.close()
    assert events.closed == ["media worker stopped (code 0)"]
    assert exits == events.closed


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


async def test_end_to_end_through_worker(worker: MediaWorker) -> None:
    """Verify a controller gets decryptable video from a sender in the worker."""
    sender = remote_sender(worker, audio=AudioParams(talkback=True))
    events = Events(sender)
    controller = Controller(talkback=True)
    await negotiate(sender, controller)
    source = Source(sender.rtp_input)
    await feed(source, [[TRAIL], KEYFRAME, [TRAIL]])
    await controller.wait_for(video_count(2))
    assert controller.received.video == [KEYFRAME, [TRAIL]]
    assert events.connected == 1
    assert events.keyframes == [None]
    assert sender.connected
    await sender.close()
    assert events.closed == []
    source.close()
    await controller.close()


async def test_session_without_sframe(worker: MediaWorker) -> None:
    """Verify a session without SFrame runs in the worker."""
    sender = remote_sender(worker, sframe=None)
    controller = Controller(sframe=False)
    await negotiate(sender, controller)
    source = Source(sender.rtp_input)
    await feed(source, [KEYFRAME])
    await controller.wait_for(video_count(1))
    assert controller.received.video == [KEYFRAME]
    source.close()
    await sender.close()
    await controller.close()


async def test_talkback_event(worker: MediaWorker) -> None:
    """Verify talkback audio comes back as an event with its timestamp."""
    sender = remote_sender(worker, audio=AudioParams(talkback=True))
    events = Events(sender)
    controller = Controller(talkback=True)
    await negotiate(sender, controller)
    await sender.update_receive_keys([TALKBACK_KEY])
    await controller.send_talkback(b"hello", 3)
    await asyncio.wait_for(wait_until(lambda: bool(events.audio)), 5)
    assert events.audio == [(b"hello", 2880)]
    await sender.close()
    await controller.close()


async def test_invalid_request_is_refused(worker: MediaWorker) -> None:
    """Verify an error in the worker comes back as an exception."""
    sender = remote_sender(worker)
    await sender.create_offer()
    with pytest.raises(MediaWorkerError, match="32 bytes"):
        await sender.update_receive_keys([SFrameKey(bytes(5), 1)])
    with pytest.raises(MediaWorkerError, match="not negotiated"):
        await sender.reoffer("v=0\r\n")
    await sender.close()


async def test_session_ends_with_reason(worker: MediaWorker) -> None:
    """Verify a session that ends in the worker reports why."""
    sender = remote_sender(worker, idle_timeout=0.3)
    events = Events(sender)
    controller = Controller()
    await negotiate(sender, controller)
    await asyncio.wait_for(wait_until(lambda: bool(events.closed)), 5)
    assert events.closed == ["media input idle"]
    assert sender.closed
    with pytest.raises(MediaWorkerError, match="closed"):
        await sender.wait_connected(1)
    await controller.close()


async def test_fan_out_to_two_sessions(worker: MediaWorker) -> None:
    """Verify one source feeds two sessions in the worker."""
    shared = await worker.open_source()
    first = remote_sender(worker, source=shared)
    second = remote_sender(worker, source=shared)
    controllers = (Controller(), Controller())
    await negotiate(first, controllers[0])
    await negotiate(second, controllers[1])
    assert first.rtp_input == second.rtp_input == shared.rtp_input
    source = Source(shared.rtp_input)
    await feed(source, [KEYFRAME, [TRAIL]])
    await controllers[0].wait_for(video_count(2))
    await controllers[1].wait_for(video_count(2))
    assert controllers[0].received.video == controllers[1].received.video
    events = Events(first)
    await shared.close("ffmpeg exited")
    await asyncio.wait_for(wait_until(lambda: bool(events.closed)), 5)
    assert events.closed == ["media source ended: ffmpeg exited"]
    source.close()
    await second.close()
    await controllers[0].close()
    await controllers[1].close()


async def test_worker_crash_ends_sessions_and_restarts(
    worker: MediaWorker,
) -> None:
    """Verify a crashed worker ends its sessions and sources, then restarts."""
    shared = await worker.open_source()
    sender = remote_sender(worker, source=shared)
    events = Events(sender)
    source_closed: list[str] = []
    shared.on_closed = source_closed.append
    exits: list[str] = []
    worker.on_exit = exits.append
    await sender.create_offer()
    pid = worker.pid
    kill(worker)
    await asyncio.wait_for(wait_until(lambda: bool(events.closed)), 5)
    assert events.closed[0].startswith("media worker exited")
    assert source_closed == events.closed
    assert exits == events.closed
    with pytest.raises(MediaWorkerError, match="No WebRTC session"):
        await sender.reoffer("v=0\r\n")
    with pytest.raises(MediaWorkerError, match="closed"):
        await remote_sender(worker, source=shared).create_offer()
    fresh = remote_sender(worker)
    assert "m=video" in await fresh.create_offer()
    assert worker.pid != pid
    await fresh.close()


def kill(worker: MediaWorker) -> None:
    """Crash the worker process."""
    assert worker._runner is not None
    worker._runner.kill()


async def test_close_during_offer(worker: MediaWorker) -> None:
    """Verify closing while the worker makes the offer ends it in the worker.

    Gathering host candidates is quick, so the offer may also be made first.
    """
    sender = remote_sender(worker)
    offering = asyncio.ensure_future(sender.create_offer())
    await asyncio.sleep(0)
    await sender.close()
    with contextlib.suppress(MediaWorkerError):
        await offering
    assert sender.closed
    assert worker._handles == {}


async def test_worker_logs_to_its_file(
    worker: MediaWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify the worker process writes its WebRTC records to its log file."""
    await worker.close()
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    sender = remote_sender(worker, offer_timeout=0.2)
    await sender.create_offer()
    await asyncio.wait_for(wait_until(lambda: sender.closed), 5)
    assert logged_in_worker(worker, "pyhap.media", "WebRTC session closed: offer")


async def test_worker_follows_changed_log_levels(
    worker: MediaWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a running worker process takes the parent's level when told to."""
    logging.getLogger("pyhap.media").setLevel(logging.WARNING)
    await worker.close()
    await worker.start()
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    await worker.update_log_levels()
    sender = remote_sender(worker, offer_timeout=0.2)
    await sender.create_offer()
    await asyncio.wait_for(wait_until(lambda: sender.closed), 5)
    assert logged_in_worker(worker, "pyhap.media", "WebRTC session closed: offer")


async def test_worker_forwards_ice_logs(
    worker: MediaWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify the worker process logs its ICE records at the parent's level."""
    await worker.close()
    caplog.set_level(logging.DEBUG, logger="pyhap.media")
    sender = remote_sender(worker)
    controller = Controller()
    await negotiate(sender, controller)
    await asyncio.wait_for(wait_until(lambda: ice_selected_in_worker(worker)), 5)
    await sender.close()
    await controller.close()


def ice_selected_in_worker(worker: MediaWorker) -> bool:
    """Whether the worker's ICE agent logged its selected pair."""
    return logged_in_worker(worker, "pyhap.media.ice", "ICE selected")


def logged_in_worker(worker: MediaWorker, name: str, start: str) -> bool:
    """Whether the worker process logged ``start...`` with a ``name`` logger."""
    assert worker.pid is not None
    path = pathlib.Path(log_file(worker._log_dir, worker.pid))
    if not path.exists():
        return False
    return any(
        f"[{name}" in line and f"] {start}" in line
        for line in path.read_text().splitlines()
    )


def test_unknown_option_is_refused() -> None:
    """Verify a typo in the sender options fails in the caller."""
    with pytest.raises(TypeError, match="idle_timout"):
        MediaWorker().create_webrtc_sender(
            video=VIDEO, sframe=TALKBACK_KEY, idle_timout=1
        )


@pytest.mark.parametrize("separate_process", [False, True], ids=["thread", "process"])
async def test_stuck_worker_is_stopped(
    monkeypatch: pytest.MonkeyPatch, separate_process: bool, tmp_path: pathlib.Path
) -> None:
    """Verify a worker that stops answering is ended and replaced."""
    worker = MediaWorker(
        separate_process=separate_process, log_dir=tmp_path, request_timeout=0.5
    )
    sender = remote_sender(worker)
    events = Events(sender)
    await sender.create_offer()
    generation = worker._generation
    monkeypatch.setattr(worker, "_dispatch", ignore_message)
    with pytest.raises(MediaWorkerError, match="did not answer"):
        await sender.update_receive_keys([TALKBACK_KEY])
    await asyncio.wait_for(wait_until(lambda: bool(events.closed)), 5)
    assert events.closed[0].startswith("media worker exited")
    await sender.close()
    monkeypatch.undo()
    assert "m=video" in await remote_sender(worker).create_offer()
    assert worker._generation > generation
    await worker.close()


def ignore_message(message: dict[str, Any], blob: bytes) -> None:
    """Drop a message from the worker, as if it never answered."""


async def test_oversized_request_is_refused(
    worker: MediaWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a request over the size limit fails without reaching the worker."""
    sender = remote_sender(worker)
    await sender.create_offer()
    monkeypatch.setattr(worker_module, "MAX_MESSAGE", 1000)
    with pytest.raises(MediaWorkerError, match="not sent"):
        await sender.reoffer("v=0\r\n" * 400)
    monkeypatch.undo()
    assert worker.running
    await sender.close()


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
    server._op_offer = unserializable  # type: ignore[method-assign]
    await server._respond({"id": 7, "op": "offer"})
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
