"""Tests for WebRTC writes of pyhap.camera through the accessory driver."""

import asyncio
from collections.abc import Callable
import inspect
from typing import Any
from unittest.mock import AsyncMock

from pyhap import hksv3
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.util import base64_to_bytes, to_base64_str

WEBRTC = "CameraWebRTCStreamManagement"
CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 51000)
SOLICIT = to_base64_str(
    hksv3.WebRTCSolicitOffer(hksv3.WebRTCOfferOptions(True)).encode()
)


def _bridged(driver: AccessoryDriver, camera: Camera) -> Camera:
    bridge = Bridge(driver, "Bridge")
    bridge.add_accessory(camera)
    driver.add_accessory(bridge)
    return camera


def _char_id(camera: Camera, name: str) -> tuple[int, int]:
    service = camera.get_service(WEBRTC)
    assert service is not None
    iid = camera.iid_manager.get_iid(service.get_characteristic(name))
    assert camera.aid is not None
    assert iid is not None
    return camera.aid, iid


async def _write(
    driver: AccessoryDriver,
    camera: Camera,
    name: str,
    value: str,
    timeout: float = 9.0,
) -> dict[str, Any]:
    """Write like a controller that asks for a write response."""
    aid, iid = _char_id(camera, name)
    query = {"characteristics": [{"aid": aid, "iid": iid, "value": value, "r": True}]}
    result = driver._set_characteristics_for_controller(query, CLIENT, timeout)
    assert inspect.isawaitable(result)
    response = await result
    assert response is not None
    return dict(response["characteristics"][0])


def _read(
    driver: AccessoryDriver, camera: Camera, name: str, client: tuple[str, int]
) -> Any:
    aid, iid = _char_id(camera, name)
    response = driver.get_characteristics([f"{aid}.{iid}"], client)
    return response["characteristics"][0]["value"]


async def test_responses_read_back_per_connection(
    stream_driver: AccessoryDriver, stream_camera: Callable[..., Camera]
) -> None:
    """Test only the writing controller reads the offer and the answer status."""
    camera = _bridged(stream_driver, stream_camera())

    result = await _write(stream_driver, camera, "WebRTCSolicitOffer", SOLICIT)

    assert result["status"] == 0
    offer = hksv3.WebRTCSolicitOfferResponse.decode(base64_to_bytes(result["value"]))
    assert offer.sdp_offer == "v=0\r\n"
    assert offer.session_id is not None
    assert (
        _read(stream_driver, camera, "WebRTCSolicitOffer", CLIENT) == (result["value"])
    )
    assert _read(stream_driver, camera, "WebRTCSolicitOffer", OTHER) == ""

    answer = hksv3.WebRTCProvideAnswer(offer.session_id, "v=0\r\n")
    result = await _write(
        stream_driver, camera, "WebRTCProvideAnswer", to_base64_str(answer.encode())
    )
    assert hksv3.WebRTCSessionStatus.decode(
        base64_to_bytes(result["value"])
    ) == hksv3.WebRTCSessionStatus(offer.session_id, hksv3.WebRTCStreamingStatus(0))
    assert _read(stream_driver, camera, "WebRTCProvideAnswer", OTHER) == ""
    assert _read(stream_driver, camera, "WebRTCNumberOfActiveSessions", OTHER) == 1
    await camera.stop()


async def test_slow_hook_answers_error_not_timeout(
    stream_driver: AccessoryDriver,
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
) -> None:
    """Test a slow hook gives the controller Error before the HAP timeout."""
    camera = _bridged(
        stream_driver,
        stream_camera(webrtc={"delegate": webrtc_delegate, "hook_timeout": 0.05}),
    )
    webrtc_delegate.handle_solicit_offer.side_effect = _never

    result = await _write(
        stream_driver, camera, "WebRTCSolicitOffer", SOLICIT, timeout=1.0
    )

    assert result["status"] == 0
    assert base64_to_bytes(result["value"]) == bytes.fromhex("040102")
    await camera.stop()


async def _never(*_: Any) -> None:
    await asyncio.Event().wait()
