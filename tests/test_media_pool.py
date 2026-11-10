"""Tests for pyhap.media.pool: sharing one source and pull per channel."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from pyhap.media import MediaError
from pyhap.media.pool import SourcePool

pytestmark = pytest.mark.timeout(5)


class Source:
    """A media source that records its close."""

    def __init__(self, port: int) -> None:
        """Bind nothing, take ``port`` as the video port."""
        self.on_closed: Callable[[str], None] | None = None
        self.closed = False
        self.rtp_input = ("127.0.0.1", port, port + 1)
        self.has_audio = True
        self.close_reasons: list[str] = []

    async def close(self, reason: str = "closed") -> None:
        """Close once."""
        self.closed = True
        self.close_reasons.append(reason)


class Pull:
    """A channel pull that records its starts and stops."""

    def __init__(self) -> None:
        """Not running."""
        self.running = False
        self.starts = 0
        self.stops = 0

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Start pulling."""
        self.running = True
        self.starts += 1

    async def stop(self) -> None:
        """Stop pulling."""
        self.running = False
        self.stops += 1

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Ignore keyframe requests."""


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> list[Source]:
    """The sources the pool opens, in process and without ports."""
    opened: list[Source] = []

    async def _open_source(self: SourcePool, audio: bool) -> Source:
        await asyncio.sleep(0)
        source = Source(5000 + 2 * len(opened))
        opened.append(source)
        return source

    monkeypatch.setattr(SourcePool, "_open_source", _open_source)
    return opened


async def test_leases_share_one_source_and_pull(sources: list[Source]) -> None:
    """Verify the pull runs while a lease is active and the last one closes."""
    pool = SourcePool()
    pull = Pull()
    first = await pool.lease("high", lambda: pull)
    second = await pool.lease("high", Pull)
    assert len(sources) == 1
    assert first.source is second.source

    await first.activate()
    await second.activate()
    assert pull.starts == 1
    await first.release()
    assert pull.running
    await second.release()

    assert not pull.running
    assert sources[0].close_reasons == ["no sessions left"]
    assert pool.counts() == {}


async def test_cancelled_lease_is_given_back(sources: list[Source]) -> None:
    """Verify a lease whose caller gave up is released once it is made."""
    pool = SourcePool()
    lease = asyncio.ensure_future(pool.lease("high", Pull))
    await asyncio.sleep(0)
    lease.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lease
    for _ in range(20):
        if sources and sources[0].closed:
            break
        await asyncio.sleep(0)

    assert sources[0].close_reasons == ["no sessions left"]
    assert pool.counts() == {}


async def test_source_that_ends_is_forgotten(sources: list[Source]) -> None:
    """Verify an ended source stops the pull and the next lease opens anew."""
    pool = SourcePool()
    pull = Pull()
    lease = await pool.lease("high", lambda: pull)
    idle = await pool.lease("high", Pull)
    await lease.activate()

    source = sources[0]
    assert source.on_closed is not None
    source.on_closed("worker exited")
    for _ in range(20):
        if not pull.running:
            break
        await asyncio.sleep(0)

    assert not pull.running
    assert pool.counts() == {}
    with pytest.raises(MediaError, match="closed"):
        await idle.activate()
    other = await pool.lease("high", Pull)
    assert other.source is not source
    for done in (lease, idle, other):
        await done.release()


async def test_close_stops_pulls_and_sources(sources: list[Source]) -> None:
    """Verify closing the pool stops every pull and closes every source."""
    pool = SourcePool()
    pulls = {"high": Pull(), "low": Pull()}
    leases = [await pool.lease(key, lambda key=key: pulls[key]) for key in pulls]
    for lease in leases:
        await lease.activate()

    await pool.close()

    assert not any(pull.running for pull in pulls.values())
    assert [source.close_reasons for source in sources] == [["pool closed"]] * 2
    assert pool.counts() == {}
