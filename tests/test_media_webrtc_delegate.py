"""Tests for pyhap.media.webrtc_delegate on fakes of the media."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhap import hksv3
from pyhap.camera_stream import StreamError
from pyhap.camera_webrtc import WebRTCSession
from pyhap.media import AudioParams, MediaError, VideoParams
from pyhap.media.pool import SourcePool
from pyhap.media.webrtc_delegate import WebRTCMediaDelegate

from .media_fakes import (
    AUDIO,
    FAKE_OFFER,
    FAKE_REOFFER_ANSWER,
    LOW,
    MEDIUM,
    FakeProvider,
    FakeSender,
    FakeWorker,
    make_session,
)

pytestmark = pytest.mark.timeout(5)

CANDIDATE = hksv3.IceCandidate("candidate:1 1 udp 1 127.0.0.1 9 typ host", "0", 0)


@pytest.fixture
def fake_worker() -> FakeWorker:
    """A fake media worker that records its sources and senders."""
    return FakeWorker()


@pytest.fixture
def fake_provider(fake_worker: FakeWorker) -> FakeProvider:
    """A source provider with fake pulls, one channel per tier quality."""
    return FakeProvider(SourcePool(cast(Any, fake_worker)))


@pytest.fixture
def media_delegate(fake_provider: FakeProvider) -> WebRTCMediaDelegate:
    """A media delegate on fakes."""
    return WebRTCMediaDelegate(fake_provider)


@pytest.fixture
def webrtc_session() -> Callable[..., WebRTCSession]:
    """Build a WebRTC session on the Medium tier."""
    return make_session


async def offer(
    delegate: WebRTCMediaDelegate,
    session: WebRTCSession,
    tier: hksv3.VideoStreamTier = MEDIUM,
) -> hksv3.WebRTCOffer:
    """Solicit an offer for a session on a tier."""
    return await delegate.handle_solicit_offer(session, tier, AUDIO)


async def answered(
    delegate: WebRTCMediaDelegate, session: WebRTCSession
) -> hksv3.WebRTCOffer:
    """Offer and answer a session."""
    result = await offer(delegate, session)
    await delegate.handle_answer(session, "v=0\r\na=answer\r\n", [CANDIDATE])
    return result


def sender_of(delegate: WebRTCMediaDelegate, session: WebRTCSession) -> FakeSender:
    """The fake sender of a session."""
    sender = delegate.sender(session)
    assert isinstance(sender, FakeSender)
    return sender


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Poll a predicate on the event loop."""
    while not predicate():
        await asyncio.sleep(0.01)


async def test_offer_with_sframe(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_worker: FakeWorker,
    fake_provider: FakeProvider,
) -> None:
    """Verify the offer carries a new key and binds the source but pulls nothing."""
    session = webrtc_session()

    result = await offer(media_delegate, session)

    sender = sender_of(media_delegate, session)
    assert result == hksv3.WebRTCOffer(FAKE_OFFER, result.sframe)
    assert result.sframe is not None
    assert sender.options == {
        "video": VideoParams(1280, 720, 30, 800, 99, 768),
        "audio": AudioParams(110, talkback=False),
        "sframe": result.sframe,
        "source": fake_worker.sources[0],
    }
    assert sender.calls == [("offer",)]
    assert fake_provider.pool.counts() == {"medium": (1, 0)}
    assert fake_provider.pulls[0].started == []


async def test_offer_without_sframe(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify a session without SFrame gets no key."""
    session = webrtc_session(sframe=False)

    result = await offer(media_delegate, session)

    assert result == hksv3.WebRTCOffer(FAKE_OFFER, None)
    assert sender_of(media_delegate, session).options["sframe"] is None


async def test_keys_differ_per_session(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify each session sends with its own SFrame key."""
    first = await offer(media_delegate, webrtc_session())
    second = await offer(media_delegate, webrtc_session())

    assert first.sframe != second.sframe


async def test_answer_starts_the_pull(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify the answer is applied and the channel is pulled to the source."""
    session = webrtc_session()

    await answered(media_delegate, session)

    assert sender_of(media_delegate, session).calls[1] == (
        "answer",
        "v=0\r\na=answer\r\n",
        [CANDIDATE],
    )
    assert fake_provider.pulls[0].started == [("127.0.0.1", 5000, 5001)]
    assert fake_provider.pool.counts() == {"medium": (1, 1)}


async def test_sessions_share_one_pull(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
    fake_worker: FakeWorker,
) -> None:
    """Verify N sessions of a channel share a source and a pull until the last ends."""
    first, second, unanswered = webrtc_session(), webrtc_session(), webrtc_session()
    await answered(media_delegate, first)
    await answered(media_delegate, second)
    await offer(media_delegate, unanswered)
    (pull,) = fake_provider.pulls
    (source,) = fake_worker.sources

    await media_delegate.handle_end(first)
    assert (pull.stopped, fake_provider.pool.counts()) == (0, {"medium": (2, 1)})

    await media_delegate.handle_end(second)
    assert (pull.stopped, fake_provider.pool.counts()) == (1, {"medium": (1, 0)})
    assert source.close_reasons == []

    await media_delegate.handle_end(unanswered)
    assert pull.started == [("127.0.0.1", 5000, 5001)]
    assert source.close_reasons == ["no sessions left"]
    assert fake_provider.pool.counts() == {}


async def test_channels_per_tier(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify tiers of different channels get their own source."""
    await offer(media_delegate, webrtc_session(), MEDIUM)
    await offer(media_delegate, webrtc_session(), LOW)

    assert fake_provider.pool.counts() == {"medium": (1, 0), "low": (1, 0)}


async def test_unanswered_offer_never_pulls(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
    fake_worker: FakeWorker,
) -> None:
    """Verify an offer that ends unanswered never starts the pull."""
    session = webrtc_session()
    await offer(media_delegate, session)

    await media_delegate.handle_end(session)

    assert (fake_provider.pulls[0].started, fake_provider.pulls[0].stopped) == ([], 0)
    assert fake_worker.sources[0].close_reasons == ["no sessions left"]


async def test_new_source_after_the_last_session(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_worker: FakeWorker,
) -> None:
    """Verify a session after the last one ended opens a new source."""
    first = webrtc_session()
    await answered(media_delegate, first)
    await media_delegate.handle_end(first)

    await answered(media_delegate, webrtc_session())

    assert len(fake_worker.sources) == 2


async def test_end_releases_exactly_once(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify ending twice closes the sender and releases the lease once."""
    session = webrtc_session()
    await answered(media_delegate, session)
    sender = sender_of(media_delegate, session)

    await media_delegate.handle_end(session)
    await media_delegate.handle_end(session)

    assert sender.calls[-1] == ("close",)
    assert sender.calls.count(("close",)) == 1
    assert fake_provider.pulls[0].stopped == 1
    assert media_delegate.session_count == 0


async def test_end_after_failed_offer(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify a failing provider answers Error and leaves nothing to release."""
    session = webrtc_session()
    with pytest.raises(StreamError, match="No media source") as err:
        await offer(media_delegate, session, hksv3.VideoStreamTier(9, 9, 1, 1, 1, 1))

    assert err.value.status == hksv3.WebRTCSolicitOfferStatus.ERROR

    await media_delegate.handle_end(session)

    assert fake_provider.pool.counts() == {}
    assert media_delegate.session_count == 0


async def test_sender_closing_ends_the_session(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a sender that ends on its own ends its HAP session."""
    session = webrtc_session()
    end = MagicMock()
    monkeypatch.setattr(session, "end", end)
    await answered(media_delegate, session)
    sender = sender_of(media_delegate, session)

    assert sender.on_closed is not None
    sender.on_closed("ICE consent lost")

    end.assert_called_once_with("WebRTC media ended: ICE consent lost")


@pytest.mark.parametrize(
    ("sframe", "enabled"), [(True, None), (True, True), (False, False)]
)
async def test_reoffer(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    sframe: bool,
    enabled: bool | None,
) -> None:
    """Verify a reoffer is answered with the session's key."""
    session = webrtc_session(sframe=sframe)
    result = await answered(media_delegate, session)

    reoffer = await media_delegate.handle_reoffer(
        session, "v=0\r\na=reoffer\r\n", hksv3.WebRTCOfferOptions(enabled)
    )

    assert reoffer == hksv3.WebRTCReofferAnswer(FAKE_REOFFER_ANSWER, result.sframe)
    assert sender_of(media_delegate, session).calls[-1] == (
        "reoffer",
        "v=0\r\na=reoffer\r\n",
    )


@pytest.mark.parametrize("sframe", [True, False])
async def test_reoffer_cannot_switch_sframe(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    sframe: bool,
) -> None:
    """Verify a reoffer that switches SFrame is refused."""
    session = webrtc_session(sframe=sframe)
    await answered(media_delegate, session)

    with pytest.raises(StreamError, match="SFrame"):
        await media_delegate.handle_reoffer(
            session, "v=0\r\n", hksv3.WebRTCOfferOptions(not sframe)
        )


async def test_update_session(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify the controller's keys go to the sender."""
    session = webrtc_session()
    await answered(media_delegate, session)
    key = hksv3.SFrameKey(bytes(32), 5)

    await media_delegate.handle_update_session(session, [key], [4])

    assert sender_of(media_delegate, session).calls[-1] == ("keys", [key], [4])


@pytest.mark.parametrize(
    ("hook", "args"),
    [
        ("handle_answer", ("v=0\r\n", [])),
        ("handle_reoffer", ("v=0\r\n", hksv3.WebRTCOfferOptions())),
        ("handle_update_session", ([], [1])),
    ],
)
async def test_media_errors_answer_error(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    hook: str,
    args: tuple[Any, ...],
) -> None:
    """Verify a refusal of the media answers the request with Error."""
    session = webrtc_session()
    await offer(media_delegate, session)
    sender_of(media_delegate, session).error = MediaError("refused")

    with pytest.raises(StreamError, match="refused") as err:
        await getattr(media_delegate, hook)(session, *args)

    assert err.value.status == hksv3.WebRTCStreamingStatus.ERROR


async def test_offer_error(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_worker: FakeWorker,
) -> None:
    """Verify a failed offer answers Error and keeps the session to release."""
    session = webrtc_session()
    cast(Any, fake_worker).create_webrtc_sender = _failing_sender

    with pytest.raises(StreamError, match="No offer") as err:
        await offer(media_delegate, session)

    assert err.value.status == hksv3.WebRTCSolicitOfferStatus.ERROR
    assert media_delegate.session_count == 1


def _failing_sender(**options: Any) -> FakeSender:
    sender = FakeSender(**options)
    sender.error = MediaError("no candidates")
    return sender


async def test_unknown_session(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify a hook for a session the delegate does not hold fails."""
    with pytest.raises(StreamError, match="No WebRTC media"):
        await media_delegate.handle_answer(webrtc_session(), "v=0\r\n", [])


async def test_failed_pull_fails_the_answer(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify a pull that cannot start fails the answer and counts nothing."""
    session = webrtc_session()
    await offer(media_delegate, session)
    fake_provider.pulls[0].error = FileNotFoundError("ffmpeg")

    with pytest.raises(StreamError, match="Media pull not started: ffmpeg"):
        await media_delegate.handle_answer(session, "v=0\r\n", [])

    assert fake_provider.pool.counts() == {"medium": (1, 0)}


async def test_talkback(
    fake_provider: FakeProvider,
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify talkback is offered and its audio reaches the callback."""
    on_audio = MagicMock()
    delegate = WebRTCMediaDelegate(fake_provider, on_audio=on_audio)
    session = webrtc_session()
    await answered(delegate, session)
    sender = sender_of(delegate, session)

    assert sender.on_audio is not None
    sender.on_audio(b"opus", 960)

    assert sender.options["audio"] == AudioParams(110, talkback=True)
    on_audio.assert_called_once_with(session, b"opus", 960)


async def test_keyframe_requests_reach_the_pull(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify keyframe requests go to the pull once the session is answered."""
    session = webrtc_session()
    await offer(media_delegate, session)
    sender = sender_of(media_delegate, session)
    assert sender.on_keyframe_request is not None
    sender.on_keyframe_request(None, None)

    await media_delegate.handle_answer(session, "v=0\r\n", [])
    sender.on_keyframe_request(3.5, 2.0)

    assert fake_provider.pulls[0].keyframes == [(3.5, 2.0)]


async def test_source_ending_on_its_own(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
    fake_worker: FakeWorker,
) -> None:
    """Verify a source that ended (worker exit) stops its pull and is replaced."""
    first = webrtc_session()
    await answered(media_delegate, first)
    fake_worker.sources[0].end("media worker exited")
    await wait_until(lambda: fake_provider.pulls[0].stopped == 1)

    second = webrtc_session()
    await answered(media_delegate, second)
    await media_delegate.handle_end(first)
    await media_delegate.handle_end(second)

    assert fake_provider.pulls[0].stopped == 1
    assert fake_provider.pulls[1].stopped == 1
    assert fake_worker.sources[0].close_reasons == []
    assert fake_worker.sources[1].close_reasons == ["no sessions left"]


async def test_cancelled_lease_is_given_back(fake_worker: FakeWorker) -> None:
    """Verify a lease whose caller went away is released once it is made."""
    pool = SourcePool(cast(Any, fake_worker))
    lease = asyncio.ensure_future(pool.lease("medium", AsyncMock))
    await asyncio.sleep(0)
    lease.cancel()

    await asyncio.gather(lease, return_exceptions=True)
    await wait_until(lambda: fake_worker.sources[0].closed)

    assert fake_worker.sources[0].close_reasons == ["no sessions left"]
    assert pool.counts() == {}
    await pool.close()


async def test_failing_pull_factory_opens_no_source(fake_worker: FakeWorker) -> None:
    """Verify a pull factory that raises leaves no source open."""
    pool = SourcePool(cast(Any, fake_worker))

    with pytest.raises(RuntimeError, match="no pull"):
        await pool.lease("medium", MagicMock(side_effect=RuntimeError("no pull")))

    assert fake_worker.sources == []
    assert pool.counts() == {}


async def test_source_ended_on_open_is_not_used(fake_worker: FakeWorker) -> None:
    """Verify a source that ended before it was wired up is not registered."""
    pool = SourcePool(cast(Any, fake_worker))
    fake_worker.end_on_open = "worker exited"

    with pytest.raises(MediaError, match="medium ended"):
        await pool.lease("medium", AsyncMock)

    assert pool.counts() == {}


async def test_close_releases_everything(
    media_delegate: WebRTCMediaDelegate,
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify closing the delegate releases the sessions left."""
    await answered(media_delegate, webrtc_session())

    await media_delegate.close()

    assert fake_provider.pool.counts() == {}
    assert fake_provider.pulls[0].stopped == 1


async def test_release_during_activate_counts_once(
    webrtc_session: Callable[..., WebRTCSession],
    fake_provider: FakeProvider,
) -> None:
    """Verify a lease released while its pull fails to start counts off once."""
    lease = await fake_provider(webrtc_session(), MEDIUM, AUDIO)
    other = await fake_provider(webrtc_session(), MEDIUM, AUDIO)
    pull = fake_provider.pulls[0]
    pull.error = OSError("no camera")
    pull.gate = asyncio.Event()
    activating = asyncio.ensure_future(lease.activate())
    await wait_until(lambda: pull.waiting)

    releasing = asyncio.ensure_future(lease.release())
    await asyncio.sleep(0)
    pull.gate.set()

    with pytest.raises(OSError, match="no camera"):
        await activating
    await releasing
    assert fake_provider.pool.counts() == {"medium": (1, 0)}
    await other.release()


async def test_provider_stream_error_passes(
    webrtc_session: Callable[..., WebRTCSession],
) -> None:
    """Verify a provider's own StreamError answers with its status."""
    refused = StreamError(hksv3.WebRTCSolicitOfferStatus.PRIVACY_MODE_ACTIVE, "off")
    delegate = WebRTCMediaDelegate(AsyncMock(side_effect=refused))

    with pytest.raises(StreamError) as err:
        await offer(delegate, webrtc_session())

    assert err.value is refused
