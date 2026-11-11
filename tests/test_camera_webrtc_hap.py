"""Tests for HKSV 17.99 stream writes of pyhap.camera through the driver."""

import asyncio
from collections.abc import Callable
import inspect
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

from pyhap import hksv3
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.util import base64_to_bytes, to_base64_str

WEBRTC = "CameraWebRTCStreamManagement"
MULTI_TIER = "CameraMultiTierRTPStreamManagement"
SESSION = UUID("acccc6cc-1505-6345-158d-a974b4509006")
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


def _char_id(camera: Camera, name: str, service_name: str = WEBRTC) -> tuple[int, int]:
    service = camera.get_service(service_name)
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
    response: bool = True,
    service_name: str = WEBRTC,
) -> dict[str, Any]:
    """Write like a controller, by default one that asks for a write response.

    Returns the result of the characteristic, empty if the answer has no body.
    """
    aid, iid = _char_id(camera, name, service_name)
    char = {"aid": aid, "iid": iid, "value": value, "r": response}
    query = {"characteristics": [char]}
    result = driver._set_characteristics_for_controller(query, CLIENT, timeout)
    assert inspect.isawaitable(result)
    body = await result
    # Without r a write that succeeds has no body.
    return {} if body is None else dict(body["characteristics"][0])


def _read(
    driver: AccessoryDriver,
    camera: Camera,
    name: str,
    client: tuple[str, int],
    service_name: str = WEBRTC,
) -> Any:
    aid, iid = _char_id(camera, name, service_name)
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


async def test_answer_without_write_response_stays_private(
    stream_driver: AccessoryDriver, stream_camera: Callable[..., Camera]
) -> None:
    """Test an answer written without r is not shared, its writer reads the status."""
    camera = _bridged(stream_driver, stream_camera())
    result = await _write(stream_driver, camera, "WebRTCSolicitOffer", SOLICIT)
    offer = hksv3.WebRTCSolicitOfferResponse.decode(base64_to_bytes(result["value"]))
    assert offer.session_id is not None
    answer = hksv3.WebRTCProvideAnswer(offer.session_id, "v=0\r\na=ice-pwd:secret\r\n")

    result = await _write(
        stream_driver,
        camera,
        "WebRTCProvideAnswer",
        to_base64_str(answer.encode()),
        response=False,
    )

    assert result == {}
    assert _read(stream_driver, camera, "WebRTCProvideAnswer", OTHER) == ""
    assert hksv3.WebRTCSessionStatus.decode(
        base64_to_bytes(_read(stream_driver, camera, "WebRTCProvideAnswer", CLIENT))
    ) == hksv3.WebRTCSessionStatus(offer.session_id, hksv3.WebRTCStreamingStatus(0))
    await camera.stop()


async def test_streaming_control_without_write_response_stays_private(
    stream_driver: AccessoryDriver, stream_camera: Callable[..., Camera]
) -> None:
    """Test an RTP Streaming Control write without r is not shared."""
    camera = _bridged(stream_driver, stream_camera())
    request = hksv3.encode_item(1, SESSION.bytes) + hksv3.encode_item(2, b"\x02")

    result = await _write(
        stream_driver,
        camera,
        "RTPStreamingControl",
        to_base64_str(request),
        response=False,
        service_name=MULTI_TIER,
    )

    assert result == {}
    assert _read(stream_driver, camera, "RTPStreamingControl", OTHER, MULTI_TIER) == ""
    status = _read(stream_driver, camera, "RTPStreamingControl", CLIENT, MULTI_TIER)
    assert base64_to_bytes(status) == hksv3.encode_streaming_status(
        SESSION, hksv3.RTPStreamingStatus.UNKNOWN_SESSION
    )
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
