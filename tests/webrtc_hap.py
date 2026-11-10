"""HAP WebRTC sessions against the loopback controller (not a test module)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Any
from uuid import UUID

from pyhap import hksv3
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.util import base64_to_bytes, to_base64_str

from .webrtc_helpers import SFrameKey
from .webrtc_peer import KEY, KEYFRAME, TRAIL, Controller, Source, video_count

WEBRTC = "CameraWebRTCStreamManagement"
CLIENT = ("127.0.0.1", 50000)
# A synthetic camera: an IRAP picture every GOP_FRAMES frames.
GOP_FRAMES = 10
FRAME_INTERVAL = 0.02


class SyntheticPull:
    """Plays a camera channel: synthetic H.265 to the source's ports."""

    def __init__(self) -> None:
        """Not running yet."""
        self.starts = 0
        self.stops = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Send frames until stopped."""
        self.starts += 1
        self._task = asyncio.ensure_future(self._send(Source(rtp_input)))

    async def _send(self, source: Source) -> None:
        try:
            frame = 0
            while True:
                source.send_video(KEYFRAME if frame % GOP_FRAMES == 0 else [TRAIL])
                frame += 1
                await asyncio.sleep(FRAME_INTERVAL)
        finally:
            source.close()

    async def stop(self) -> None:
        """Stop sending."""
        self.stops += 1
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """The next GOP brings one."""


def bridged(driver: AccessoryDriver, camera: Camera) -> Camera:
    """Add the camera to a bridge on the driver."""
    bridge = Bridge(driver, "Bridge")
    bridge.add_accessory(camera)
    driver.add_accessory(bridge)
    return camera


async def write(camera: Camera, name: str, value: Any) -> bytes:
    """Write a TLV like a controller asking for a write response."""
    service = camera.get_service(WEBRTC)
    assert service is not None
    iid = camera.iid_manager.get_iid(service.get_characteristic(name))
    query = {
        "characteristics": [
            {
                "aid": camera.aid,
                "iid": iid,
                "value": to_base64_str(value.encode()),
                "r": True,
            }
        ]
    }
    pending = camera.driver._set_characteristics_for_controller(query, CLIENT, 9.0)
    assert inspect.isawaitable(pending)
    result = await pending
    assert result is not None
    (char,) = result["characteristics"]
    assert char["status"] == 0, char
    return base64_to_bytes(char["value"])


async def solicit(camera: Camera, sframe: bool) -> hksv3.WebRTCSolicitOfferResponse:
    """Solicit an offer."""
    request = hksv3.WebRTCSolicitOffer(hksv3.WebRTCOfferOptions(sframe))
    response = await write(camera, "WebRTCSolicitOffer", request)
    return hksv3.WebRTCSolicitOfferResponse.decode(response)


async def answer(
    camera: Camera, controller: Controller, offer: hksv3.WebRTCSolicitOfferResponse
) -> hksv3.WebRTCSessionStatus:
    """Answer the offer through Provide Answer, candidates next to the SDP."""
    assert offer.session_id is not None
    assert offer.sdp_offer is not None
    key = KEY if offer.sframe is None else SFrameKey(offer.sframe.key, offer.sframe.kid)
    sdp, candidates = await controller.answer(offer.sdp_offer, key)
    request = hksv3.WebRTCProvideAnswer(
        offer.session_id,
        sdp,
        [
            hksv3.IceCandidate(c.candidate, c.sdp_mid, c.sdp_mline_index)
            for c in candidates
        ],
    )
    response = await write(camera, "WebRTCProvideAnswer", request)
    return hksv3.WebRTCSessionStatus.decode(response)


async def end(camera: Camera, session_id: UUID) -> hksv3.WebRTCSessionStatus:
    """End a session through Streaming Control."""
    request = hksv3.WebRTCStreamingControl(session_id, hksv3.WebRTCStreamingCommand.END)
    response = await write(camera, "WebRTCStreamingControl", request)
    return hksv3.WebRTCSessionStatus.decode(response)


async def connect_and_receive(controller: Controller, frames: int) -> None:
    """Connect the controller and wait for decrypted frames."""
    await controller.connect()
    await controller.wait_for(video_count(frames))


async def wait_until_ended(camera: Camera) -> None:
    """Wait until the camera has no WebRTC session left."""
    (controller,) = camera.webrtc_controllers.values()
    while controller.sessions:
        await asyncio.sleep(0.01)
