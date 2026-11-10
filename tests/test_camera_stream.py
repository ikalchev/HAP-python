"""Tests for the HKSV 17.99 stream session state machine of pyhap.camera_stream."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pyhap import hksv3
from pyhap.camera_stream import (
    MAX_HOOK_TIMEOUT,
    SessionState,
    StreamController,
    StreamError,
    StreamSession,
    _check_hook_timeout,
    _config_from_option,
    _failure_status,
    _HookFailed,
)

CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 50000)


@dataclass(frozen=True)
class _Config:
    max_sessions: int = 5


async def _hook(result: Any = 1) -> Any:
    await asyncio.sleep(0)
    return result


async def _never() -> None:
    await asyncio.Event().wait()


async def _raise(error: BaseException) -> None:
    raise error


async def _stubborn(started: asyncio.Event, finished: asyncio.Event) -> None:
    """A hook that takes 0.2 s to give up after it was cancelled."""
    started.set()
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.2)
        finished.set()


async def test_hook_result(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a hook's result comes back and the session stays."""
    session = add_session(CLIENT)
    deadline = session_controller._deadline()

    assert await session_controller._run_hook(session, "test", _hook(5), deadline) == 5

    assert session.busy is False
    assert session_controller.sessions == {session.id: session}
    session_release.assert_not_called()


async def test_hook_timeout_ends_the_session(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a slow hook is cancelled, then the session is released once."""
    session = add_session(CLIENT)
    deadline = session_controller._deadline()

    with pytest.raises(_HookFailed) as failed:
        await session_controller._run_hook(
            session, "test", _never(), deadline, first=True
        )
    assert failed.value.status is None
    assert session.state is SessionState.ENDED
    assert session_controller.sessions == {}
    await session_controller.close()

    session_release.assert_awaited_once_with(session)
    assert session.busy is False


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (StreamError(hksv3.RTPStreamingStatus.BUSY), 3),
        (NotImplementedError(), None),
        (RuntimeError("boom"), None),
    ],
)
async def test_hook_errors(
    session_controller: StreamController[StreamSession],
    error: Exception,
    status: int | None,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a delegate's status is kept, other errors have none."""
    session = add_session(CLIENT)

    with pytest.raises(_HookFailed) as failed:
        await session_controller._run_hook(
            session, "test", _raise(error), session_controller._deadline()
        )

    assert failed.value.status == status
    assert session.state is SessionState.NEW


async def test_failed_hook_may_end_the_session(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a failed first hook ends the session and still releases it."""
    session = add_session(CLIENT)

    with pytest.raises(_HookFailed):
        await session_controller._run_hook(
            session,
            "test",
            _raise(RuntimeError()),
            session_controller._deadline(),
            end_on_failure=True,
            first=True,
        )
    await session_controller.close()

    assert session.state is SessionState.ENDED
    session_release.assert_awaited_once_with(session)


async def test_end_releases_once(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test ending twice releases a session the delegate saw exactly once."""
    session = add_session(CLIENT)
    await session_controller._run_hook(
        session, "test", _hook(), session_controller._deadline(), first=True
    )

    first = session_controller._end(session, "first")
    second = session_controller._end(session, "second")

    assert first is second
    assert first is not None
    assert await first is True
    session_release.assert_awaited_once_with(session)


async def test_end_without_delegate(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a session the delegate never saw is not released."""
    session = add_session(CLIENT)

    assert session_controller._end(session, "test") is None
    assert await session_controller.end_session(session.id) is False
    session_release.assert_not_called()


async def test_end_waits_for_cancelled_hooks(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test the release starts once a running hook was cancelled."""
    session = add_session(CLIENT)
    session._delegated = True
    hook = asyncio.ensure_future(
        session_controller._run_hook(
            session, "test", _never(), session_controller._deadline()
        )
    )
    await asyncio.sleep(0)

    assert await session_controller.end_session(session.id) is True

    session_release.assert_awaited_once_with(session)
    with pytest.raises(_HookFailed):
        await hook


async def test_release_failure_is_logged(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a failing release still ends the session."""
    session_release.side_effect = RuntimeError
    session = add_session(CLIENT)
    session._delegated = True

    release_task = session_controller._end(session, "test")

    assert release_task is not None
    assert await release_task is False
    assert "Releasing the session failed" in caplog.text


async def test_cancelled_request_ends_the_session(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test the session ends when the request waiting for its hook goes away."""
    session = add_session(CLIENT)
    request = asyncio.ensure_future(
        session_controller._run_hook(
            session, "test", _never(), session_controller._deadline()
        )
    )
    await asyncio.sleep(0)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert session.state is SessionState.ENDED


async def test_hook_of_ended_session(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test no hook runs for a session that already ended."""
    session = add_session(CLIENT)
    session_controller._end(session, "test")
    hook = _hook()

    with pytest.raises(_HookFailed):
        await session_controller._run_hook(
            session, "test", hook, session_controller._deadline()
        )

    assert hook.cr_frame is None


async def test_timer_rechecks_the_state(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a timer only ends a session still in one of its states."""
    waiting = add_session(CLIENT)
    moved_on = add_session(CLIENT)
    session_controller._set_timer(waiting, 0, "too slow", SessionState.NEW)
    session_controller._set_timer(moved_on, 0, "too slow", SessionState.NEW)
    moved_on.state = SessionState.ACTIVE

    await asyncio.sleep(0.01)

    assert waiting.state is SessionState.ENDED
    assert moved_on.state is SessionState.ACTIVE


async def test_end_cancels_the_timer(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test an ended session's timer does not fire."""
    session = add_session(CLIENT)
    session_controller._set_timer(session, 10, "too slow", SessionState.NEW)

    session_controller._end(session, "test")

    assert session._timer is None


async def test_ownership(
    session_controller: StreamController[StreamSession],
    caplog: pytest.LogCaptureFixture,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test only the connection that set a session up finds it."""
    session = add_session(CLIENT)
    caplog.set_level(logging.DEBUG, logger="pyhap.camera_stream")

    assert session_controller._owned(session.id, CLIENT) is session
    assert session_controller._owned(session.id, OTHER) is None
    assert session_controller._owned(uuid4(), CLIENT) is None
    assert "Not the controller of the session" in caplog.text


async def test_add_refuses_a_taken_id(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a session with the ID of another one is not registered."""
    session = add_session(CLIENT)
    twin = StreamSession(id=session.id, sensor=session.sensor, client_addr=CLIENT)

    with pytest.raises(RuntimeError, match="Another session has the ID"):
        session_controller._add(twin)
    assert session_controller.sessions[session.id] is session


async def test_client_disconnected(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a closed connection ends only its own sessions."""
    mine = add_session(CLIENT)
    other = add_session(OTHER)
    mine._delegated = True

    session_controller.client_disconnected(CLIENT)

    assert mine.state is SessionState.ENDED
    assert session_controller.sessions == {other.id: other}
    await session_controller.close()
    session_release.assert_awaited_once_with(mine)


async def test_end_all_sessions(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test ending all sessions waits for each release."""
    first = add_session(CLIENT)
    second = add_session(OTHER)
    first._delegated = second._delegated = True

    await session_controller.end_all_sessions()

    assert session_controller.sessions == {}
    assert session_release.await_count == 2


async def test_end_by_request_waits_until_the_deadline(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a request gives up on a slow release at its deadline."""
    session_release.side_effect = _never
    session = add_session(CLIENT)
    session._delegated = True

    assert (
        await session_controller._end_by_request(
            session, session_controller._deadline()
        )
        is False
    )
    assert session.state is SessionState.ENDED


@pytest.mark.parametrize("hook_timeout", [0, -1, MAX_HOOK_TIMEOUT + 0.1])
def test_hook_timeout_leaves_time_to_answer(hook_timeout: float) -> None:
    """Test hooks must end before the HAP response timeout."""
    with pytest.raises(ValueError, match="hook_timeout"):
        _check_hook_timeout(hook_timeout)


def test_config_from_option() -> None:
    """Test options build a config, a config is taken as it is."""
    config = _Config(max_sessions=6)

    assert _config_from_option(_Config, config, "multi_tier") is config
    assert _config_from_option(_Config, {"max_sessions": 6}, "x") == config
    with pytest.raises(ValueError, match="Unknown multi_tier option: start"):
        _config_from_option(_Config, {"start": 1}, "multi_tier")
    with pytest.raises(ValueError, match="must be a dict"):
        _config_from_option(_Config, [], "multi_tier")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(None, 4), (0, 4), (3, 3), (99, 4)],
)
def test_failure_status(status: int | None, expected: int) -> None:
    """Test a delegate picks a known failure status, else the error."""
    error = hksv3.RTPStreamingStatus.ERROR
    assert _failure_status(hksv3.RTPStreamingStatus, status, error) == expected


async def test_session_end(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
) -> None:
    """Test a session ends itself on the loop and is released once."""
    session = add_session(CLIENT)
    session._delegated = True

    session.end("the media stopped")
    assert session.state is SessionState.NEW
    await asyncio.sleep(0)
    session.end("again")
    await session_controller.close()

    assert session.state is SessionState.ENDED
    assert session_controller.sessions == {}
    session_release.assert_awaited_once_with(session)


def test_session_end_without_controller(
    stream_sensor: hksv3.SensorConfiguration,
) -> None:
    """Test a session no controller registered cannot end itself."""
    session = StreamSession(id=uuid4(), sensor=stream_sensor, client_addr=CLIENT)

    session.end()

    assert session.state is SessionState.NEW


async def test_release_after_a_stuck_hook(
    session_controller: StreamController[StreamSession],
    session_release: AsyncMock,
    add_session: Callable[..., StreamSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the release waits for a cancelled hook only up to hook_timeout."""
    session = add_session(CLIENT)
    session._delegated = True
    started = asyncio.Event()
    finished = asyncio.Event()
    hook = _stubborn(started, finished)
    request = asyncio.ensure_future(
        session_controller._run_hook(
            session, "test", hook, session_controller._deadline()
        )
    )
    await started.wait()

    release = session_controller._end(session, "test")

    assert release is not None
    assert await release is True
    assert not finished.is_set()
    assert "while 1 cancelled hooks still run" in caplog.text
    session_release.assert_awaited_once_with(session)
    with pytest.raises(_HookFailed):
        await request
    await finished.wait()


async def test_no_room(
    session_controller: StreamController[StreamSession],
    add_session: Callable[..., StreamSession],
) -> None:
    """Test the limits of sessions in all and per connection."""
    add_session(CLIENT)
    add_session(CLIENT)
    assert session_controller._no_room(CLIENT) is None

    add_session(CLIENT)
    assert session_controller._no_room(CLIENT) == (
        "Too many stream sessions of the connection"
    )
    assert session_controller._no_room(OTHER) is None

    add_session(OTHER)
    add_session(OTHER)
    assert session_controller._no_room(OTHER) == "Too many stream sessions"


async def test_no_room_once_closed(
    session_controller: StreamController[StreamSession],
) -> None:
    """Test a closed controller takes no new session."""
    await session_controller.close()

    assert session_controller._no_room(CLIENT) == "The stream service is closed"
