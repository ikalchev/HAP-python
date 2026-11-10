"""HAP WebRTC sessions end to end through WebRTCMediaDelegate.

The loopback controller writes the WebRTC characteristics through the
accessory driver, connects over ICE and DTLS and decrypts the synthetic
H.265 of a shared source; once with the senders in process, once in the
media worker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from pyhap import hksv3
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.media.delegate import WebRTCMediaDelegate
from pyhap.media.pool import SourcePool
from pyhap.media.worker import MediaWorker

from .webrtc_hap import (
    SyntheticPull,
    answer,
    bridged,
    connect_and_receive,
    end,
    solicit,
)
from .webrtc_peer import KEYFRAME, TRAIL, Controller, wait_until

pytestmark = pytest.mark.timeout(30)

SUCCESS = hksv3.WebRTCStreamingStatus.SUCCESS


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


@pytest.fixture
def synthetic_pulls() -> list[SyntheticPull]:
    """The synthetic camera pulls the e2e provider made."""
    return []


@pytest.fixture
async def e2e_pool(
    media_worker: MediaWorker | None,
) -> AsyncIterator[SourcePool]:
    """The source pool of the e2e delegate."""
    pool = SourcePool(media_worker)
    yield pool
    await pool.close()


@pytest.fixture
async def e2e_delegate(
    e2e_pool: SourcePool, synthetic_pulls: list[SyntheticPull]
) -> AsyncIterator[WebRTCMediaDelegate]:
    """A media delegate with real senders on a synthetic camera channel."""

    def pull() -> SyntheticPull:
        synthetic_pulls.append(SyntheticPull())
        return synthetic_pulls[-1]

    async def provider(*_: Any) -> Any:
        return await e2e_pool.lease("medium", pull)

    delegate = WebRTCMediaDelegate(
        provider,
        sender_options={"host_addresses": ["127.0.0.1"], "use_ipv6": False},
    )
    yield delegate
    await delegate.close()


@pytest.mark.parametrize("sframe", [True, False])
async def test_hap_session_end_to_end(
    stream_driver: AccessoryDriver,
    stream_camera: Callable[..., Camera],
    e2e_delegate: WebRTCMediaDelegate,
    e2e_pool: SourcePool,
    synthetic_pulls: list[SyntheticPull],
    sframe: bool,
) -> None:
    """Verify solicit, answer and end through HAP deliver decryptable frames."""
    camera = bridged(stream_driver, stream_camera(webrtc={"delegate": e2e_delegate}))
    controller = Controller(sframe=sframe)

    offer = await solicit(camera, sframe)
    assert offer.session_id is not None
    assert (offer.status, offer.sframe is not None) == (0, sframe)
    assert e2e_pool.counts() == {"medium": (1, 0)}
    assert synthetic_pulls[0].starts == 0

    status = await answer(camera, controller, offer)
    assert status == hksv3.WebRTCSessionStatus(offer.session_id, SUCCESS)
    await connect_and_receive(controller, 3)
    assert controller.received.video[:3] == [KEYFRAME, [TRAIL], [TRAIL]]
    assert e2e_pool.counts() == {"medium": (1, 1)}

    status = await end(camera, offer.session_id)
    assert status == hksv3.WebRTCSessionStatus(offer.session_id, SUCCESS)
    assert e2e_pool.counts() == {}
    assert (synthetic_pulls[0].starts, synthetic_pulls[0].stops) == (1, 1)
    await controller.close()
    await camera.stop()


async def test_lost_connection_ends_the_hap_session(
    stream_driver: AccessoryDriver,
    stream_camera: Callable[..., Camera],
    e2e_delegate: WebRTCMediaDelegate,
    e2e_pool: SourcePool,
    synthetic_pulls: list[SyntheticPull],
) -> None:
    """Verify a sender that loses its controller ends the HAP session."""
    camera = bridged(stream_driver, stream_camera(webrtc={"delegate": e2e_delegate}))
    controller = Controller()
    offer = await solicit(camera, True)
    await answer(camera, controller, offer)
    await connect_and_receive(controller, 1)
    (webrtc,) = camera.webrtc_controllers.values()

    await controller.close()
    await wait_until(lambda: not e2e_pool.counts())

    assert webrtc.sessions == {}
    assert synthetic_pulls[0].stops == 1
    assert e2e_delegate.session_count == 0
    await camera.stop()
