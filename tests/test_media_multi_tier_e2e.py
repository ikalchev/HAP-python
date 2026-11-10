"""HAP multi-tier sessions end to end through MultiTierMediaDelegate.

A loopback controller writes Setup Endpoints and RTP Streaming Control and
decrypts the SRTP of a synthetic camera channel; once with the senders in
process, once in the media worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from pyhap import hksv3
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.camera_multi_tier import MultiTierController
from pyhap.camera_stream import StreamError
from pyhap.media.multi_tier_delegate import MultiTierMediaDelegate
from pyhap.media.pool import SourcePool
from pyhap.media.worker import MediaWorker
from pyhap.util import base64_to_bytes, to_base64_str

from .test_media_multi_tier import AUDIO_KEY, VIDEO_KEY, Controller as SrtpController
from .webrtc_hap import (
    SyntheticPull,
    bridged,
)
from .webrtc_peer import KEYFRAME, TRAIL, wait_until

pytestmark = pytest.mark.timeout(30)

SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
SESSION = UUID("acccc6cc-1505-6345-158d-a974b4509006")
CLIENT = ("127.0.0.1", 50000)
CONTROLLER_VIDEO_SSRC = 0x1234


@pytest.fixture(params=["in-process", "worker"])
async def media_worker(
    request: pytest.FixtureRequest,
) -> AsyncIterator[MediaWorker | None]:
    """No worker (senders in process), or a started worker."""
    if request.param == "in-process":
        yield None
        return
    worker = MediaWorker()
    await worker.start()
    yield worker
    await worker.close()


class Provider:
    """Leases one synthetic channel per tier quality from a pool."""

    def __init__(self, pool: SourcePool) -> None:
        """Record the pulls it makes."""
        self.pool = pool
        self.pulls: dict[str, SyntheticPull] = {}

    async def __call__(
        self, _session: Any, tier: hksv3.VideoStreamTier, _audio: Any
    ) -> Any:
        name = hksv3.VideoQuality(tier.quality).name.lower()
        return await self.pool.lease(name, partial_pull(self.pulls, name), name=name)


def partial_pull(
    pulls: dict[str, SyntheticPull], name: str
) -> Callable[[], SyntheticPull]:
    """Make the pull of a channel and remember it."""

    def make() -> SyntheticPull:
        pulls[name] = SyntheticPull()
        return pulls[name]

    return make


@pytest.fixture
async def provider(media_worker: MediaWorker | None) -> AsyncIterator[Provider]:
    """A provider on a pool in the worker or in process."""
    pool = SourcePool(media_worker)
    yield Provider(pool)
    await pool.close()


@pytest.fixture
async def camera(
    stream_driver: AccessoryDriver,
    stream_camera: Callable[..., Camera],
    provider: Provider,
) -> AsyncIterator[Camera]:
    """A camera whose multi-tier sessions lease from the provider."""
    multi_tier = MultiTierMediaDelegate(provider, sender_options={"idle_timeout": 1.0})
    accessory = bridged(
        stream_driver, stream_camera(multi_tier={"delegate": multi_tier})
    )
    yield accessory
    await accessory.stop()
    await multi_tier.close()


def _controller(camera: Camera) -> MultiTierController:
    return camera.multi_tier_controllers[SENSOR]


async def setup(camera: Camera, peer: SrtpController) -> hksv3.SetupEndpointsResponse:
    """Write Setup Endpoints towards the loopback controller."""
    suite = hksv3.SRTPCryptoSuite.AES_CM_128_HMAC_SHA1_80
    request = hksv3.SetupEndpoints(
        SESSION,
        hksv3.EndpointAddress("127.0.0.1", False, peer.video_port, peer.audio_port),
        hksv3.SRTPParameters(suite, VIDEO_KEY[:16], VIDEO_KEY[16:]),
        hksv3.SRTPParameters(suite, AUDIO_KEY[:16], AUDIO_KEY[16:]),
    )
    answer_value = await _controller(camera)._set_setup_endpoints(
        to_base64_str(request.encode()), CLIENT
    )
    return hksv3.SetupEndpointsResponse.decode(base64_to_bytes(answer_value))


async def control(camera: Camera, command: int, tier: int | None = None) -> int:
    """Write RTP Streaming Control, return the status."""
    data = hksv3.encode_item(1, SESSION.bytes) + hksv3.encode_item(2, bytes([command]))
    if tier is not None:
        data += hksv3.encode_item(3, tier.to_bytes(4, "little"))
        data += hksv3.encode_item(4, CONTROLLER_VIDEO_SSRC.to_bytes(4, "little"))
        data += hksv3.encode_item(5, (1).to_bytes(4, "little"))
        data += hksv3.encode_item(6, (0x5678).to_bytes(4, "little"))
    answer_value = await _controller(camera)._set_streaming_control(
        to_base64_str(data), CLIENT
    )
    fields = dict(hksv3.decode_items(base64_to_bytes(answer_value)))
    return fields[2][0]


def tier_of(camera: Camera, quality: hksv3.VideoQuality) -> int:
    """The identifier of the advertised tier of a quality."""
    return next(t.identifier for t in _controller(camera).tiers if t.quality == quality)


async def frames(peer: SrtpController, count: int) -> None:
    """Wait until the controller decrypted ``count`` access units."""

    def check() -> bool:
        peer.poll()
        return len(peer.units) >= count

    await wait_until(check)


async def test_multi_tier_session_end_to_end(
    camera: Camera, provider: Provider
) -> None:
    """Verify setup, start, tier change and end deliver decryptable SRTP."""
    peer = SrtpController()
    response = await setup(camera, peer)
    assert response.status == hksv3.SetupEndpointsStatus.SUCCESS
    assert response.address is not None
    assert response.address.video_port not in (0, peer.video_port)
    assert provider.pool.counts() == {}

    assert await control(camera, 2, tier_of(camera, hksv3.VideoQuality.MEDIUM)) == 0
    await frames(peer, 3)
    assert peer.units[:3] == [KEYFRAME, [TRAIL], [TRAIL]]
    assert {p.ssrc for p in peer.video_packets} == {response.video_ssrc}
    assert provider.pool.counts() == {"medium": (1, 1)}

    assert await control(camera, 2, tier_of(camera, hksv3.VideoQuality.LOW)) == 0
    assert provider.pool.counts() == {"low": (1, 1)}
    assert provider.pulls["medium"].stops == 1
    received = len(peer.units)
    await frames(peer, received + 2)
    assert peer.units[received] == KEYFRAME

    assert await control(camera, 1) == 0
    assert provider.pool.counts() == {}
    assert provider.pulls["low"].stops == 1
    peer.close()


async def test_controller_rtcp_keeps_the_session(
    camera: Camera, provider: Provider
) -> None:
    """Verify the controller's RTCP on the announced port restarts the timer."""
    peer = SrtpController()
    response = await setup(camera, peer)
    assert response.address is not None
    assert await control(camera, 2, tier_of(camera, hksv3.VideoQuality.MEDIUM)) == 0
    session = _controller(camera).sessions[SESSION]
    calls: list[None] = []
    original = session.keep_alive

    def keep_alive() -> None:
        calls.append(None)
        original()

    session.keep_alive = keep_alive  # type: ignore[method-assign]
    delegate = _controller(camera).delegate
    assert isinstance(delegate, MultiTierMediaDelegate)
    sender: Any = delegate.sender(session)
    assert sender is not None
    sender.on_rtcp = keep_alive
    peer.send_pli(response.address.video_port)
    await wait_until(lambda: bool(calls))
    assert await control(camera, 1) == 0
    peer.close()


async def test_stalled_source_ends_the_session(
    camera: Camera, provider: Provider
) -> None:
    """Verify a sender that ends on its own ends its HAP session."""
    peer = SrtpController()
    await setup(camera, peer)
    assert await control(camera, 2, tier_of(camera, hksv3.VideoQuality.MEDIUM)) == 0
    await frames(peer, 1)
    await provider.pulls["medium"].stop()
    controller = _controller(camera)

    async def ended() -> None:
        while controller.sessions:
            await asyncio.sleep(0.05)

    delegate = controller.delegate
    assert isinstance(delegate, MultiTierMediaDelegate)
    await asyncio.wait_for(ended(), 20)
    assert delegate.session_count == 0
    peer.close()


async def test_same_channel_keeps_the_stream(
    camera: Camera, provider: Provider
) -> None:
    """Verify a start with a tier of the same channel keeps the source."""
    peer = SrtpController()
    await setup(camera, peer)
    medium = tier_of(camera, hksv3.VideoQuality.MEDIUM)
    assert await control(camera, 2, medium) == 0
    assert await control(camera, 2, medium) == 0
    assert provider.pool.counts() == {"medium": (1, 1)}
    assert provider.pulls["medium"].stops == 0
    assert await control(camera, 1) == 0
    peer.close()


async def test_provider_failure_refuses_the_start(
    camera: Camera, provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a provider error answers the start with an error."""

    async def fail(*_: Any) -> Any:
        raise RuntimeError("no input")

    monkeypatch.setattr(Provider, "__call__", fail)
    peer = SrtpController()
    await setup(camera, peer)
    status = await control(camera, 2, tier_of(camera, hksv3.VideoQuality.MEDIUM))
    assert status == hksv3.RTPStreamingStatus.ERROR
    assert provider.pool.counts() == {}
    peer.close()


def test_delegate_needs_a_pool() -> None:
    """Verify a provider without a pool needs one passed."""

    async def provider(*_: Any) -> Any:
        return None

    with pytest.raises(TypeError, match="SourcePool"):
        MultiTierMediaDelegate(provider)
    assert MultiTierMediaDelegate(provider, SourcePool()).session_count == 0


class _FakeLease:
    """A lease whose activation can fail."""

    def __init__(self, fail: bool) -> None:
        self.source = object()
        self.fail = fail
        self.released = 0

    async def activate(self) -> None:
        if self.fail:
            raise RuntimeError("pull failed")

    async def release(self) -> None:
        self.released += 1


class _FakeSender:
    """A multi-tier sender that starts once, like the real one; switch can be slow."""

    on_rtcp: Any = None
    on_closed: Any = None
    on_keyframe_request: Any = None

    def __init__(self) -> None:
        self.switching = asyncio.Event()
        self.started = False

    async def bind(self) -> tuple[int, int]:
        return 1, 2

    async def start(self, *_args: Any, **_kwargs: Any) -> None:
        if self.started:
            raise RuntimeError("Multi-tier session already started")
        self.started = True

    async def switch(self, _source: Any) -> None:
        self.switching.set()
        await asyncio.sleep(10)

    async def close(self) -> None:
        return None


class _Session(SimpleNamespace):
    """A hashable stand-in for a MultiTierSession."""

    __hash__ = object.__hash__


def _fake_session() -> Any:
    return _Session(
        id=SESSION,
        address="127.0.0.1",
        is_ipv6=False,
        video_port=1,
        audio_port=2,
        video_srtp_key=bytes(16),
        video_srtp_salt=bytes(14),
        audio_srtp_key=bytes(16),
        audio_srtp_salt=bytes(14),
        video_ssrc=1,
        audio_ssrc=2,
        keep_alive=lambda: None,
        end=lambda _reason: None,
    )


async def test_failed_start_releases_its_lease() -> None:
    """Verify a start that fails, and the repeated start, leak no lease."""
    leases = [_FakeLease(fail=True), _FakeLease(fail=False)]

    async def provider(*_: Any) -> Any:
        return leases.pop(0)

    pool = SourcePool()
    setattr(pool, "create_multi_tier_sender", lambda **_options: _FakeSender())  # noqa: B010
    delegate = MultiTierMediaDelegate(provider, pool)
    session = _fake_session()
    tier = hksv3.VideoStreamTier(2, hksv3.VideoQuality.MEDIUM, 1500, 1280, 720, 30)
    await delegate.handle_prepare(session)
    first, second = leases
    with pytest.raises(StreamError):
        await delegate.handle_start(session, tier, None)
    assert first.released == 1
    await delegate.handle_start(session, tier, None)
    sender: Any = delegate.sender(session)
    assert sender is not None and sender.started
    await delegate.handle_end(session)
    assert (first.released, second.released) == (1, 1)


async def test_cancelled_tier_change_releases_the_new_lease() -> None:
    """Verify a tier change cancelled mid-switch gives the new lease back."""
    leases = [_FakeLease(fail=False), _FakeLease(fail=False)]

    async def provider(*_: Any) -> Any:
        return leases.pop(0)

    pool = SourcePool()
    sender = _FakeSender()
    setattr(pool, "create_multi_tier_sender", lambda **_options: sender)  # noqa: B010
    delegate = MultiTierMediaDelegate(provider, pool)
    session = _fake_session()
    tier = hksv3.VideoStreamTier(2, hksv3.VideoQuality.MEDIUM, 1500, 1280, 720, 30)
    await delegate.handle_prepare(session)
    old, new = leases
    await delegate.handle_start(session, tier, None)
    change = asyncio.ensure_future(delegate.handle_reconfigure(session, tier, None))
    await sender.switching.wait()
    change.cancel()
    with pytest.raises(asyncio.CancelledError):
        await change
    assert new.released == 1
    assert old.released == 0
    await delegate.handle_end(session)
    assert old.released == 1
