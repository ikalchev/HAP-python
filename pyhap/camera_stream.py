"""Stream sessions of the HomeKit Secure Video 17.99 camera services.

Developer Preview: HomeKit Secure Video 17.99 is a preview of the guide, so
this API is experimental and may change.

A :class:`StreamController` keeps the sessions of one stream management
service of one sensor, the multi-tier RTP or the WebRTC one. The media comes
from a delegate, see :class:`pyhap.camera_multi_tier.MultiTierDelegate` and
:class:`pyhap.camera_webrtc.WebRTCDelegate`. Every session runs through one
state machine::

    NEW -> PREPARED (multi-tier) or SOLICITED (WebRTC)
        -> ANSWERING (WebRTC, while the answer is applied)
        -> STARTED (multi-tier) or ACTIVE (WebRTC)
        -> ENDED, from any state

A delegate hook runs as a task the session owns. A HAP request waits for it
until one deadline, ``hook_timeout`` after the write, which leaves time to
answer within the HAP response timeout. A hook that takes longer is
cancelled and its session ends. Once a session ended, the delegate releases
it exactly once, in the background, if it saw the session at all. Until its
release finished or timed out, the session still counts towards the session
limits.

A delegate keeps its state by session object, e.g. in ``session.data``, not
by session ID: a controller may set up a session with the ID of one that is
still being released. The delegate ends a session on its side, e.g. when
its media stopped, with :meth:`StreamSession.end`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field, fields
from enum import Enum, IntEnum
import functools
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast
from uuid import UUID

from pyhap import hksv3
from pyhap.hap_handler import RESPONSE_TIMEOUT

if TYPE_CHECKING:
    from pyhap.camera import Camera

__all__ = [
    "DEFAULT_HOOK_TIMEOUT",
    "MAX_HOOK_TIMEOUT",
    "SessionState",
    "StreamController",
    "StreamError",
    "StreamSession",
]

logger = logging.getLogger(__name__)

# Seconds a request keeps after its hooks to answer within RESPONSE_TIMEOUT.
HOOK_TIMEOUT_MARGIN = 1.0
MAX_HOOK_TIMEOUT = RESPONSE_TIMEOUT - HOOK_TIMEOUT_MARGIN
DEFAULT_HOOK_TIMEOUT = MAX_HOOK_TIMEOUT
# A release waits up to hook_timeout for the cancelled hooks and then up to
# hook_timeout for the delegate. close() gives up on tasks after this many
# hook timeouts.
CLOSE_TIMEOUT_FACTOR = 3

_S = TypeVar("_S", bound="StreamSession")
_T = TypeVar("_T")
_C = TypeVar("_C")
_E = TypeVar("_E", bound=IntEnum)


class StreamError(Exception):
    """Raised by a delegate hook to answer the request with ``status``.

    ``status`` must be a value of the status enum of the request the hook
    answers, e.g. :attr:`pyhap.hksv3.RTPStreamingStatus.BUSY` for RTP
    Streaming Control or :attr:`pyhap.hksv3.SetupEndpointsStatus.BUSY` for
    Setup Endpoints. Another value, other exceptions and hooks that take too
    long answer with the request's error status.
    """

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"status {status}")
        self.status = status


class SessionState(Enum):
    NEW = "new"
    PREPARED = "prepared"
    SOLICITED = "solicited"
    ANSWERING = "answering"
    STARTED = "started"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(eq=False, kw_only=True)
class StreamSession:
    """A stream session of one controller connection and one sensor.

    Only ``client_addr``, the connection that set the session up, may control
    it. ``data`` is free for the delegate.
    """

    id: UUID
    sensor: hksv3.SensorConfiguration
    client_addr: tuple[str, int] | None
    state: SessionState = SessionState.NEW
    data: dict[str, Any] = field(default_factory=dict)
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _timer: asyncio.TimerHandle | None = field(default=None, init=False, repr=False)
    _delegated: bool = field(default=False, init=False, repr=False)
    _release: asyncio.Task[bool] | None = field(default=None, init=False, repr=False)
    _controller: StreamController[Any] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def sensor_uuid(self) -> UUID:
        return self.sensor.sensor_uuid

    @property
    def ended(self) -> bool:
        return self.state is SessionState.ENDED

    @property
    def busy(self) -> bool:
        """Whether a delegate hook of the session is running."""
        return bool(self._tasks)

    def end(self, reason: str = "the camera ended it") -> None:
        """End the session on the camera's side, e.g. when its media stopped.

        Schedules the end on the event loop and returns at once. The
        delegate then releases the session as for any other end. Does
        nothing for a session that ended. Call it in the event loop.
        """
        controller = self._controller
        if controller is not None and not self.ended:
            controller._loop.call_soon(controller._end, self, reason)


class HookFailed(Exception):
    """A hook failed, ``status`` is the one the delegate asked for, if any.

    Raised by :meth:`StreamController._run_hook` to the stream services.
    """

    def __init__(self, status: int | None = None) -> None:
        super().__init__(status)
        self.status = status


def failure_status(status_type: type[_E], status: int | None, error: _E) -> _E:
    """The status a failed request answers, ``error`` unless the delegate chose."""
    try:
        chosen = status_type(status)
    except ValueError:
        return error
    return chosen if chosen else error


def check_hook_timeout(hook_timeout: float) -> None:
    """Make sure a hook leaves time to answer within the HAP response timeout."""
    if not 0 < hook_timeout <= MAX_HOOK_TIMEOUT:
        raise ValueError(f"hook_timeout must be above 0 and at most {MAX_HOOK_TIMEOUT}")


def config_from_option(config_type: type[_C], value: Any, name: str) -> _C:
    """Build a config dataclass from an option dict, or take it as it is.

    :raises ValueError: For an unknown key or a value of another type.
    """
    if isinstance(value, config_type):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a dict or a {config_type.__name__}")
    known = {item.name for item in fields(cast(Any, config_type))}
    if unknown := sorted(set(value) - known):
        raise ValueError(f"Unknown {name} option: {', '.join(unknown)}")
    return config_type(**value)


class StreamController(Generic[_S]):
    """The sessions of one stream management service of one sensor."""

    kind = "stream"

    def __init__(
        self,
        camera: Camera,
        sensor: hksv3.SensorConfiguration,
        hook_timeout: float,
        max_sessions: int,
        max_sessions_per_connection: int,
    ) -> None:
        check_hook_timeout(hook_timeout)
        self.camera = camera
        self.sensor = sensor
        self.hook_timeout = hook_timeout
        self.max_sessions = max_sessions
        self.max_sessions_per_connection = max_sessions_per_connection
        self._sessions: dict[UUID, _S] = {}
        # Ended sessions whose release still runs, they keep their room.
        self._releasing: set[_S] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        # Background tasks nobody waits for any more, their errors are warned.
        self._abandoned: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def sessions(self) -> Mapping[UUID, _S]:
        """The sessions that did not end, by ID."""
        return MappingProxyType(self._sessions)

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        return cast(asyncio.AbstractEventLoop, self.camera.driver.loop)

    def _deadline(self) -> float:
        """The deadline of the hooks of a request that arrives now."""
        return self._loop.time() + self.hook_timeout

    def _add(self, session: _S) -> None:
        """Register a new session, it may end itself from now on.

        :raises RuntimeError: If another session has the same ID.
        """
        if self._sessions.get(session.id, session) is not session:
            raise RuntimeError(f"[{session.id}] Another session has the ID")
        session._controller = self
        self._sessions[session.id] = session

    def _no_room(self, client_addr: tuple[str, int] | None) -> str | None:
        """Why a new session of ``client_addr`` does not fit, None if it does."""
        if self._closed:
            return f"The {self.kind} service is closed"
        sessions = [*self._sessions.values(), *self._releasing]
        if len(sessions) >= self.max_sessions:
            return f"Too many {self.kind} sessions"
        owned = sum(session.client_addr == client_addr for session in sessions)
        if owned >= self.max_sessions_per_connection:
            return f"Too many {self.kind} sessions of the connection"
        return None

    def _owned(
        self, session_id: UUID, client_addr: tuple[str, int] | None
    ) -> _S | None:
        """Return the session if ``client_addr`` owns it.

        A session of another connection is unknown to it.
        """
        session = self._sessions.get(session_id)
        if session is not None and session.client_addr != client_addr:
            logger.debug("[%s] Not the controller of the session", session_id)
            return None
        return session

    async def _run_hook(
        self,
        session: _S,
        name: str,
        hook: Coroutine[Any, Any, _T],
        deadline: float,
        end_on_failure: bool = False,
        first: bool = False,
    ) -> _T:
        """Run a delegate hook as a task of ``session`` until ``deadline``.

        A hook that is still running then is cancelled and its session ends.
        ``first`` marks the hook that introduces the session to the delegate,
        which then releases it when it ends.

        :raises HookFailed: If the hook failed or took too long, or the
            session ended meanwhile.
        """
        if session.ended:
            hook.close()
            raise HookFailed
        if first:
            session._delegated = True
        task = self._loop.create_task(hook)
        session._tasks.add(task)
        task.add_done_callback(session._tasks.discard)
        task.add_done_callback(
            functools.partial(self._retrieve, session, f"The {name} hook")
        )
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=max(deadline - self._loop.time(), 0)
            )
        except asyncio.CancelledError:
            # The request went away, e.g. its connection closed.
            self._end(session, "the request was cancelled")
            raise
        if not done:
            logger.warning("[%s] The %s hook timed out", session.id, name)
            self._end(session, f"the {name} hook timed out")
            raise HookFailed
        if session.ended:
            raise HookFailed
        if task.cancelled():
            # The hook cancelled itself, not the end of its session.
            logger.warning("[%s] The %s hook was cancelled", session.id, name)
            if end_on_failure:
                self._end(session, f"the {name} hook was cancelled")
            raise HookFailed
        error = task.exception()
        if error is None:
            return task.result()
        if isinstance(error, StreamError):
            logger.warning("[%s] The %s hook failed: %s", session.id, name, error)
        elif isinstance(error, NotImplementedError):
            logger.warning("[%s] No %s hook, refusing", session.id, name)
        else:
            logger.error("[%s] The %s hook failed", session.id, name, exc_info=error)
        if end_on_failure:
            self._end(session, f"the {name} hook failed")
        raise HookFailed(error.status if isinstance(error, StreamError) else None)

    def _background(
        self, session: _S, label: str, coro: Coroutine[Any, Any, _T]
    ) -> asyncio.Task[_T]:
        """Run ``coro`` as a task of the controller, ``label`` names it in logs."""
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # A release that timed out is not awaited any more.
        task.add_done_callback(functools.partial(self._retrieve, session, label))
        return task

    def _retrieve(self, session: _S, label: str, task: asyncio.Task[Any]) -> None:
        """Retrieve the exception of a hook or background task, also unawaited.

        Nobody reports the error of an abandoned task, e.g. a release still
        running at its timeout, so it is warned about. Other errors are only
        logged at debug, their waiter reports them or their session ended.
        """
        abandoned = task in self._abandoned
        self._abandoned.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            if abandoned:
                logger.warning(
                    "[%s] %s failed after its timeout: %r", session.id, label, error
                )
            else:
                logger.debug("[%s] %s ended with %r", session.id, label, error)

    def _end(self, session: _S, reason: str) -> asyncio.Task[bool] | None:
        """End ``session`` now and release it in the background.

        Cancels its timer and its running hooks. Returns the release, None if
        the delegate never saw the session. Ending it again returns the same.
        """
        if not session.ended:
            previous = session.state
            session.state = SessionState.ENDED
            if self._sessions.get(session.id) is session:
                del self._sessions[session.id]
            self._cancel_timer(session)
            hooks = list(session._tasks)
            for hook in hooks:
                hook.cancel()
            logger.debug("[%s] %s session ended: %s", session.id, self.kind, reason)
            self._session_ended(session, previous)
            if session._delegated:
                self._releasing.add(session)
                session._release = self._background(
                    session, "The release task", self._release(session, hooks)
                )
        return session._release

    async def _release(self, session: _S, hooks: list[asyncio.Task[Any]]) -> bool:
        """Release ``session``, False if the delegate's release timed out."""
        try:
            return await self._release_after(session, hooks)
        finally:
            self._releasing.discard(session)

    async def _release_after(self, session: _S, hooks: list[asyncio.Task[Any]]) -> bool:
        if hooks:
            # Cancelled hooks finish before the release starts, unless one
            # ignores its cancellation.
            _, pending = await asyncio.wait(hooks, timeout=self.hook_timeout)
            if pending:
                logger.warning(
                    "[%s] Releasing the session while %d cancelled hooks still run",
                    session.id,
                    len(pending),
                )
        release = self._background(session, "The end hook", self._release_hook(session))
        done, _ = await asyncio.wait({release}, timeout=self.hook_timeout)
        if not done:
            # Not awaited: a release that ignores its cancellation must not
            # keep the room or the camera's stop.
            release.cancel()
            self._abandoned.add(release)
            logger.warning("[%s] Releasing the session timed out", session.id)
            return False
        # The release finished, so the session is gone even if it failed.
        if release.cancelled():
            logger.warning("[%s] Releasing the session was cancelled", session.id)
        elif (error := release.exception()) is not None:
            logger.error(
                "[%s] Releasing the session failed", session.id, exc_info=error
            )
        return True

    async def _end_by_request(self, session: _S, deadline: float) -> bool:
        """End a session for a request, waiting for its release until ``deadline``.

        Returns False if the release did not finish by ``deadline``. The
        session then keeps its room until its release finished or timed out.
        """
        release = self._end(session, "the controller ended it")
        if release is None:
            return True
        done, _ = await asyncio.wait(
            {release}, timeout=max(deadline - self._loop.time(), 0)
        )
        return bool(done) and release.result()

    def _set_timer(
        self, session: _S, delay: float | None, reason: str, *states: SessionState
    ) -> None:
        """End ``session`` after ``delay`` seconds if it is still in ``states``."""
        self._cancel_timer(session)
        if delay is not None:
            session._timer = self._loop.call_later(
                delay, self._expire, session, reason, states
            )

    @staticmethod
    def _cancel_timer(session: StreamSession) -> None:
        if session._timer is not None:
            session._timer.cancel()
            session._timer = None

    def _expire(
        self, session: _S, reason: str, states: tuple[SessionState, ...]
    ) -> None:
        session._timer = None
        if session.state in states:
            logger.warning("[%s] %s, ending the session", session.id, reason)
            self._end(session, reason)

    def _session_ended(self, session: _S, previous: SessionState) -> None:
        """Called once when a session ended, before its release starts."""

    async def _release_hook(self, session: _S) -> None:
        """Let the delegate release ``session``."""
        raise NotImplementedError

    def _end_sessions(self, reason: str) -> None:
        """End all sessions now, releasing them in the background."""
        for session in list(self._sessions.values()):
            self._end(session, reason)

    def client_disconnected(self, client_addr: tuple[str, int]) -> None:
        """End the sessions of a connection that closed."""
        for session in list(self._sessions.values()):
            if session.client_addr == client_addr:
                self._end(session, "the controller disconnected")

    async def close(self) -> None:
        """End all sessions and wait for their releases, e.g. when the camera stops.

        Gives up on tasks that still run after three times ``hook_timeout``,
        e.g. a release that ignores its cancellation. No new session fits
        afterwards.
        """
        self._closed = True
        self._end_sessions("the camera stopped")
        deadline = self._loop.time() + CLOSE_TIMEOUT_FACTOR * self.hook_timeout
        while pending := [task for task in self._tasks if not task.done()]:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.warning(
                    "Closing the %s service while %d tasks still run",
                    self.kind,
                    len(pending),
                )
                return
            await asyncio.wait(pending, timeout=remaining)
