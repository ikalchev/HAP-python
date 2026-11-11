"""Tests for the HKSV 17.99 WebRTC streaming of pyhap.camera."""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from pyhap import hksv3
from pyhap.camera import Camera
from pyhap.camera_stream import SessionState, StreamError
from pyhap.camera_webrtc import WebRTCController, WebRTCSession, _remote_tier
from pyhap.util import base64_to_bytes, to_base64_str

WEBRTC = "CameraWebRTCStreamManagement"
SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 50000)
MEDIUM = hksv3.VideoStreamTier(2, 3, 768, 1280, 720, 30, 800)
LOW = hksv3.VideoStreamTier(3, 4, 180, 640, 360, 15, 190)
ANSWER = "v=0\r\na=setup:active\r\n"
KEY = hksv3.SFrameKey(bytes(32), 7)


def _controller(camera: Camera) -> WebRTCController:
    return camera.webrtc_controllers[SENSOR]


def _count(camera: Camera) -> int:
    service = camera.get_service(WEBRTC)
    assert service is not None
    return int(service.get_characteristic("WebRTCNumberOfActiveSessions").value)


async def _solicit(
    camera: Camera, sframe: bool | None = True, client: Any = CLIENT
) -> hksv3.WebRTCSolicitOfferResponse:
    request = hksv3.WebRTCSolicitOffer(hksv3.WebRTCOfferOptions(sframe))
    response = await _controller(camera)._set_solicit_offer(
        to_base64_str(request.encode()), client
    )
    return hksv3.WebRTCSolicitOfferResponse.decode(base64_to_bytes(response))


async def _session(camera: Camera) -> WebRTCSession:
    """Solicit an offer and return the new session."""
    response = await _solicit(camera)
    assert response.session_id is not None
    return _controller(camera).sessions[response.session_id]


def _status(response: str) -> hksv3.WebRTCSessionStatus:
    return hksv3.WebRTCSessionStatus.decode(base64_to_bytes(response))


async def _answer(
    camera: Camera, session_id: UUID, client: Any = CLIENT
) -> hksv3.WebRTCSessionStatus:
    request = hksv3.WebRTCProvideAnswer(
        session_id,
        ANSWER,
        [hksv3.IceCandidate("candidate:1 1 udp 1 1.2.3.4 5 typ host")],
    )
    return _status(
        await _controller(camera)._set_provide_answer(
            to_base64_str(request.encode()), client
        )
    )


async def _end(
    camera: Camera, session_id: UUID, client: Any = CLIENT, command: int = 1
) -> hksv3.WebRTCSessionStatus:
    request = hksv3.WebRTCStreamingControl(session_id, command)
    return _status(
        await _controller(camera)._set_streaming_control(
            to_base64_str(request.encode()), client
        )
    )


async def _active(camera: Camera) -> WebRTCSession:
    session = await _session(camera)
    assert (await _answer(camera, session.id)).status == 0
    return session


async def _solicit_many(camera: Camera, count: int) -> list[int]:
    return [(await _solicit(camera)).status for _ in range(count)]


# Sessions


async def _hang(*_args: object) -> None:
    """Never return, as a hook stuck beyond its timeout."""
    await asyncio.Event().wait()


def test_remote_tier(stream_sensor: hksv3.SensorConfiguration) -> None:
    """Test the remote tier is the smallest of a quality, else the smallest."""
    tiers = hksv3.tiers_from_sensor(stream_sensor)
    assert _remote_tier(tiers, hksv3.VideoQuality.MEDIUM) == MEDIUM
    assert _remote_tier(tiers, hksv3.VideoQuality.HIGHEST) == LOW


async def test_solicit_offer(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an offer of the remote tier with its SFrame key, ICE in the SDP."""
    camera = stream_camera()

    response = await _solicit(camera, sframe=None)

    assert response.status == hksv3.WebRTCSolicitOfferStatus.SUCCESS
    assert response.sdp_offer == "v=0\r\n"
    assert response.candidates == []
    assert response.sframe == KEY
    assert response.session_id is not None
    session = _controller(camera).sessions[response.session_id]
    assert session.state is SessionState.SOLICITED
    assert session.sframe is True
    assert session.client_addr == CLIENT
    webrtc_delegate.handle_solicit_offer.assert_awaited_once_with(
        session, MEDIUM, hksv3.DEFAULT_AUDIO_TIER
    )
    assert _count(camera) == 0


async def test_solicit_offer_without_sframe(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a controller that turns SFrame off gets an offer without a key."""
    webrtc_delegate.handle_solicit_offer.return_value = hksv3.WebRTCOffer("v=0\r\n")
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "remote_quality": 4})

    response = await _solicit(camera, sframe=False)

    assert response.status == 0
    assert response.sframe is None
    assert response.session_id is not None
    webrtc_delegate.handle_solicit_offer.assert_awaited_once_with(
        _controller(camera).sessions[response.session_id],
        LOW,
        hksv3.DEFAULT_AUDIO_TIER,
    )


@pytest.mark.parametrize(("sframe", "key"), [(True, None), (False, KEY)])
async def test_offer_sframe_must_fit(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    sframe: bool,
    key: hksv3.SFrameKey | None,
) -> None:
    """Test an offer with a key the session does not want, or without one it needs."""
    webrtc_delegate.handle_solicit_offer.return_value = hksv3.WebRTCOffer(
        "v=0\r\n", key
    )
    camera = stream_camera()

    response = await _solicit(camera, sframe=sframe)
    await camera.stop()

    assert response.status == 2
    assert _controller(camera).sessions == {}
    webrtc_delegate.handle_end.assert_awaited_once()


@pytest.mark.parametrize(
    ("change", "status"),
    [
        (lambda camera: camera.set_manually_disabled(True), 2),
        (
            lambda camera: (
                camera.get_service("CameraGlobalOperatingMode")
                .get_characteristic("HomeKitCameraActive")
                .set_value(False)
            ),
            1,
        ),
    ],
    ids=["streaming-off", "privacy-mode"],
)
async def test_solicit_offer_refused(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    change: Callable[[Camera], None],
    status: int,
) -> None:
    """Test no offer while streaming is off or the camera is off."""
    camera = stream_camera()
    change(camera)
    assert (await _solicit(camera)).status == status
    webrtc_delegate.handle_solicit_offer.assert_not_called()


async def test_solicit_offer_after_stop(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a stopped camera answers an offer with an error."""
    camera = stream_camera()
    await camera.stop()

    response = await _solicit(camera)

    assert response.status == hksv3.WebRTCSolicitOfferStatus.ERROR
    assert response.session_id is None
    webrtc_delegate.handle_solicit_offer.assert_not_called()


@pytest.mark.parametrize(
    "offer", [None, "v=0\r\n", hksv3.WebRTCReofferAnswer("v=0\r\n")]
)
async def test_offer_of_another_type(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    offer: Any,
) -> None:
    """Test an offer hook that returns no WebRTCOffer fails, the session ends."""
    webrtc_delegate.handle_solicit_offer.return_value = offer
    camera = stream_camera()

    response = await _solicit(camera)
    sessions = dict(_controller(camera).sessions)
    await camera.stop()

    assert response.status == hksv3.WebRTCSolicitOfferStatus.ERROR
    assert response.session_id is None
    assert sessions == {}
    assert "The offer is not a WebRTCOffer" in caplog.text
    webrtc_delegate.handle_end.assert_awaited_once()


async def test_solicit_offer_without_delegate(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test a camera without a WebRTC delegate refuses offers."""
    assert (await _solicit(stream_camera(webrtc={}))).status == 2


async def test_solicit_offer_limit(stream_camera: Callable[..., Camera]) -> None:
    """Test one connection may solicit max_sessions, beyond fails without busy."""
    camera = stream_camera()
    assert await _solicit_many(camera, 7) == [0, 0, 0, 0, 0, 0, 2]


@pytest.fixture
def end_gate(webrtc_delegate: AsyncMock) -> asyncio.Event:
    """Make ``handle_end`` wait for the returned event."""
    gate = asyncio.Event()

    async def _end(_session: WebRTCSession) -> None:
        await gate.wait()

    webrtc_delegate.handle_end.side_effect = _end
    return gate


async def _solicit_ids(camera: Camera, count: int) -> list[UUID]:
    """Solicit ``count`` offers, return the session IDs."""
    ids: list[UUID] = []
    for _ in range(count):
        response = await _solicit(camera)
        assert response.session_id is not None
        ids.append(response.session_id)
    return ids


async def _end_all(camera: Camera, ids: list[UUID]) -> list[int]:
    """Write End for each session at once, return the statuses."""
    answers = await asyncio.gather(*(_end(camera, session_id) for session_id in ids))
    return [answer.status for answer in answers]


async def test_releasing_sessions_count_towards_max_sessions(
    stream_camera: Callable[..., Camera], end_gate: asyncio.Event
) -> None:
    """Test ended sessions being released count towards max_sessions."""
    camera = stream_camera()
    ends = asyncio.ensure_future(_end_all(camera, await _solicit_ids(camera, 6)))
    # One step starts the gather, the next the End writes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sessions = dict(_controller(camera).sessions)

    refused = await _solicit(camera, client=OTHER)
    end_gate.set()
    statuses = await ends
    accepted = await _solicit(camera, client=OTHER)
    await camera.stop()

    assert sessions == {}
    assert refused.status == hksv3.WebRTCSolicitOfferStatus.ERROR
    assert statuses == [0, 0, 0, 0, 0, 0]
    assert accepted.status == hksv3.WebRTCSolicitOfferStatus.SUCCESS


async def test_solicit_offer_limit_per_connection(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a connection limit of three, another connection still gets its own."""
    camera = stream_camera(
        webrtc={"delegate": webrtc_delegate, "max_sessions_per_connection": 3}
    )

    assert await _solicit_many(camera, 4) == [0, 0, 0, 2]
    assert (await _solicit(camera, client=OTHER)).status == 0


@pytest.mark.parametrize(
    ("error", "status"),
    [(RuntimeError(), 2), (StreamError(1), 1), (NotImplementedError(), 2)],
)
async def test_solicit_offer_fails(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    error: Exception,
    status: int,
) -> None:
    """Test a failed offer answers its status and releases the session once."""
    webrtc_delegate.handle_solicit_offer.side_effect = error
    camera = stream_camera()

    response = await _solicit(camera)
    await camera.stop()

    assert response.status == status
    assert response.session_id is None
    webrtc_delegate.handle_end.assert_awaited_once()


async def test_slow_offer(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test an offer beyond hook_timeout answers an error, the end runs after."""
    webrtc_delegate.handle_solicit_offer.side_effect = _hang
    webrtc_delegate.handle_end.side_effect = _hang
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "hook_timeout": 0.05})

    response = await _solicit(camera)

    assert response.status == 2
    assert "The solicit offer hook timed out" in caplog.text
    webrtc_delegate.handle_end.assert_not_awaited()
    await asyncio.sleep(0.01)
    webrtc_delegate.handle_end.assert_awaited_once()
    await camera.stop()


async def test_answer(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an answer makes the session active and counts it."""
    camera = stream_camera()
    session = await _session(camera)

    status = await _answer(camera, session.id)

    assert status == hksv3.WebRTCSessionStatus(session.id, 0)
    assert session.state is SessionState.ACTIVE
    assert _count(camera) == 1
    webrtc_delegate.handle_answer.assert_awaited_once_with(
        session,
        ANSWER,
        [hksv3.IceCandidate("candidate:1 1 udp 1 1.2.3.4 5 typ host")],
    )
    assert (await _answer(camera, session.id)).status == 3


async def test_answer_of_another_controller(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test only the soliciting connection answers, others see no session."""
    camera = stream_camera()
    session = await _session(camera)

    assert (await _answer(camera, session.id, OTHER)).status == 1
    assert (await _answer(camera, uuid4())).status == 1
    assert (await _end(camera, session.id, OTHER)).status == 1
    webrtc_delegate.handle_answer.assert_not_called()
    assert session.state is SessionState.SOLICITED


async def test_concurrent_answer(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an answer while one is applied answers busy."""
    applied = asyncio.Event()

    async def _apply(*_: Any) -> None:
        await applied.wait()

    webrtc_delegate.handle_answer.side_effect = _apply
    camera = stream_camera()
    session = await _session(camera)
    first = asyncio.ensure_future(_answer(camera, session.id))
    await asyncio.sleep(0)

    assert session.state is SessionState.ANSWERING
    assert (await _answer(camera, session.id)).status == 2

    applied.set()
    assert (await first).status == 0
    webrtc_delegate.handle_answer.assert_awaited_once()


async def test_answer_fails(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a failed answer ends the session and releases it once."""
    webrtc_delegate.handle_answer.side_effect = RuntimeError
    camera = stream_camera()
    session = await _session(camera)

    assert (await _answer(camera, session.id)).status == 3
    await camera.stop()

    assert session.state is SessionState.ENDED
    assert _count(camera) == 0
    webrtc_delegate.handle_end.assert_awaited_once_with(session)


async def test_no_answer_in_time(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a session the controller does not answer in time ends."""
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "answer_timeout": 0.01})
    session = await _session(camera)
    await asyncio.sleep(0.05)
    await camera.stop()
    assert session.state is SessionState.ENDED
    webrtc_delegate.handle_end.assert_awaited_once_with(session)


async def test_answer_ends_the_expiry(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an answered session outlives the answer timeout."""
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "answer_timeout": 0.05})
    session = await _active(camera)
    await asyncio.sleep(0.1)
    assert session.state is SessionState.ACTIVE


async def test_expiry_while_answering(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the expiry still ends a session whose answer is not applied yet."""

    async def _apply(*_: Any) -> None:
        # Only the expiry ends it, long before the hook times out.
        await asyncio.Event().wait()

    webrtc_delegate.handle_answer.side_effect = _apply
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "answer_timeout": 0.05})
    session = await _session(camera)

    assert (await _answer(camera, session.id)).status == 3
    await camera.stop()

    assert "No WebRTC answer in time" in caplog.text
    assert session.state is SessionState.ENDED
    webrtc_delegate.handle_end.assert_awaited_once_with(session)


async def test_end(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test the controller ends a session, the count goes down."""
    camera = stream_camera()
    session = await _active(camera)

    assert (await _end(camera, session.id)).status == 0

    assert _count(camera) == 0
    webrtc_delegate.handle_end.assert_awaited_once_with(session)
    assert (await _end(camera, session.id)).status == 1


async def test_end_fails(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a failed but finished release answers success and frees the room."""
    webrtc_delegate.handle_end.side_effect = RuntimeError
    camera = stream_camera(
        webrtc={"delegate": webrtc_delegate, "max_sessions_per_connection": 1}
    )
    session = await _active(camera)

    assert (await _end(camera, session.id)).status == 0
    assert _controller(camera).sessions == {}
    assert (await _solicit(camera)).status == 0


async def test_end_release_too_slow(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    end_gate: asyncio.Event,
) -> None:
    """Test an end answers an error when the release overruns."""
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "hook_timeout": 0.05})
    session = await _active(camera)

    status = (await _end(camera, session.id)).status
    end_gate.set()
    await camera.stop()

    assert status == 3


async def test_unknown_command(stream_camera: Callable[..., Camera]) -> None:
    """Test a command other than end fails."""
    camera = stream_camera()
    session = await _active(camera)
    assert (await _end(camera, session.id, command=2)).status == 3
    assert session.state is SessionState.ACTIVE


@pytest.mark.parametrize(
    "end",
    [
        lambda camera: camera.set_manually_disabled(True),
        lambda camera: camera.client_disconnected(CLIENT),
    ],
    ids=["streaming-off", "disconnected"],
)
async def test_sessions_end(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    end: Callable[[Camera], None],
) -> None:
    """Test turning streaming off and disconnecting end the sessions."""
    camera = stream_camera()
    session = await _active(camera)

    end(camera)
    await camera.stop()

    assert session.state is SessionState.ENDED
    assert _count(camera) == 0
    webrtc_delegate.handle_end.assert_awaited_once_with(session)


async def test_delegate_ends_a_session(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test the delegate ends sessions, each is released once."""
    camera = stream_camera()
    first = await _active(camera)
    second = await _session(camera)

    first.end()
    first.end()
    second.end()
    await asyncio.sleep(0)
    sessions = dict(_controller(camera).sessions)
    await camera.stop()

    assert sessions == {}
    assert _count(camera) == 0
    assert webrtc_delegate.handle_end.await_count == 2


async def test_malformed_writes(stream_camera: Callable[..., Camera]) -> None:
    """Test writes that do not decode answer an error without a session."""
    controller = _controller(stream_camera())
    value = "AQ=="
    solicit = await controller._set_solicit_offer(value, CLIENT)
    answer = await controller._set_provide_answer(value, CLIENT)
    end = await controller._set_streaming_control(value, CLIENT)
    assert base64_to_bytes(solicit) == bytes.fromhex("040102")
    assert _status(answer) == hksv3.WebRTCSessionStatus(None, 3)
    assert _status(end) == hksv3.WebRTCSessionStatus(None, 3)


@pytest.mark.parametrize(
    ("sdp", "candidates"),
    [
        ("a" * (hksv3.MAX_SDP_SIZE + 1), []),
        ("v=0", [hksv3.IceCandidate("a" * hksv3.MAX_ICE_CANDIDATE_SIZE)]),
    ],
    ids=["sdp", "candidate"],
)
async def test_oversized_answer(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    sdp: str,
    candidates: list[hksv3.IceCandidate],
) -> None:
    """Test an answer above a size limit answers an error before the delegate."""
    camera = stream_camera()
    session = await _session(camera)
    request = hksv3.WebRTCProvideAnswer(session.id, sdp, candidates)

    answer = await _controller(camera)._set_provide_answer(
        to_base64_str(request.encode()), CLIENT
    )

    assert _status(answer) == hksv3.WebRTCSessionStatus(None, 3)
    assert session.state is SessionState.SOLICITED
    webrtc_delegate.handle_answer.assert_not_called()


# Renegotiation


async def _reoffer(
    camera: Camera, session_id: UUID, client: Any = CLIENT
) -> hksv3.WebRTCReofferResponse:
    request = hksv3.WebRTCReoffer(
        session_id, "v=0\r\na=offer\r\n", hksv3.WebRTCOfferOptions(True)
    )
    response = await _controller(camera)._set_reoffer(
        to_base64_str(request.encode()), client
    )
    return hksv3.WebRTCReofferResponse.decode(base64_to_bytes(response))


async def _update(
    camera: Camera, session_id: UUID, client: Any = CLIENT
) -> hksv3.WebRTCSessionStatus:
    request = hksv3.WebRTCUpdateSession(session_id, [KEY], [5])
    return _status(
        await _controller(camera)._set_update_session(
            to_base64_str(request.encode()), client
        )
    )


async def test_reoffer(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a reoffer of an active session is answered."""
    camera = stream_camera()
    session = await _active(camera)

    response = await _reoffer(camera, session.id)

    assert response == hksv3.WebRTCReofferResponse(session.id, 0, "v=0\r\na=answer\r\n")
    webrtc_delegate.handle_reoffer.assert_awaited_once_with(
        session, "v=0\r\na=offer\r\n", hksv3.WebRTCOfferOptions(True)
    )


async def test_reoffer_answer_with_unexpected_sframe_key(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an SFrame key in the answer of a session without SFrame fails."""
    camera = stream_camera()
    session = await _active(camera)
    session.sframe = False
    webrtc_delegate.handle_reoffer.return_value = hksv3.WebRTCReofferAnswer(
        "v=0\r\n", KEY
    )

    response = await _reoffer(camera, session.id)

    assert response == hksv3.WebRTCReofferResponse(session.id, 3)
    assert session.state is SessionState.ACTIVE


async def test_reoffer_answer_of_wrong_type(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a reoffer hook returning no WebRTCReofferAnswer answers an error."""
    camera = stream_camera()
    session = await _active(camera)
    webrtc_delegate.handle_reoffer.return_value = None

    response = await _reoffer(camera, session.id)

    assert response == hksv3.WebRTCReofferResponse(session.id, 3)
    assert session.state is SessionState.ACTIVE
    assert "The reoffer answer is not a WebRTCReofferAnswer: None" in caplog.text


async def test_update_session(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test the controller's SFrame keys reach the delegate."""
    camera = stream_camera()
    session = await _active(camera)

    assert (await _update(camera, session.id)).status == 0

    webrtc_delegate.handle_update_session.assert_awaited_once_with(session, [KEY], [5])


@pytest.mark.parametrize(
    ("add_keys", "remove_kids"),
    [
        ([KEY] * (hksv3.MAX_SFRAME_KEY_CHANGES + 1), []),
        ([], [5] * (hksv3.MAX_SFRAME_KEY_CHANGES + 1)),
    ],
    ids=["add-keys", "remove-kids"],
)
async def test_oversized_update_session(
    stream_camera: Callable[..., Camera],
    webrtc_delegate: AsyncMock,
    add_keys: list[hksv3.SFrameKey],
    remove_kids: list[int],
) -> None:
    """Test an update above the key limit answers an error before the delegate."""
    camera = stream_camera()
    session = await _active(camera)
    request = hksv3.WebRTCUpdateSession(session.id, add_keys, remove_kids)

    response = await _controller(camera)._set_update_session(
        to_base64_str(request.encode()), CLIENT
    )

    assert _status(response) == hksv3.WebRTCSessionStatus(None, 3)
    assert session.state is SessionState.ACTIVE
    webrtc_delegate.handle_update_session.assert_not_called()


async def test_renegotiation_needs_an_active_session(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test unknown, foreign and unanswered sessions are refused."""
    camera = stream_camera()
    session = await _session(camera)

    assert (await _reoffer(camera, session.id)).status == 3
    assert (await _update(camera, session.id)).status == 3
    assert (await _reoffer(camera, uuid4())).status == 1
    assert (await _update(camera, session.id, OTHER)).status == 1
    webrtc_delegate.handle_reoffer.assert_not_called()
    webrtc_delegate.handle_update_session.assert_not_called()


async def test_renegotiation_fails(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a failed reoffer or update answers its status, the session stays."""
    webrtc_delegate.handle_reoffer.side_effect = RuntimeError
    webrtc_delegate.handle_update_session.side_effect = StreamError(2)
    camera = stream_camera()
    session = await _active(camera)

    assert (await _reoffer(camera, session.id)).status == 3
    assert (await _update(camera, session.id)).status == 2
    assert session.state is SessionState.ACTIVE


async def test_renegotiation_while_busy(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test an update while a reoffer runs is busy."""
    answered = asyncio.Event()

    async def _answer_reoffer(*_: Any) -> hksv3.WebRTCReofferAnswer:
        await answered.wait()
        return hksv3.WebRTCReofferAnswer("v=0\r\n")

    webrtc_delegate.handle_reoffer.side_effect = _answer_reoffer
    camera = stream_camera()
    session = await _active(camera)
    reoffer = asyncio.ensure_future(_reoffer(camera, session.id))
    await asyncio.sleep(0)

    assert (await _update(camera, session.id)).status == 2

    answered.set()
    assert (await reoffer).status == 0


async def test_slow_reoffer_ends_the_session(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a reoffer beyond hook_timeout ends the session in the background."""

    async def _never(*_: Any) -> None:
        await asyncio.Event().wait()

    webrtc_delegate.handle_reoffer.side_effect = _never
    camera = stream_camera(webrtc={"delegate": webrtc_delegate, "hook_timeout": 0.05})
    session = await _active(camera)

    assert (await _reoffer(camera, session.id)).status == 3
    await camera.stop()

    assert session.state is SessionState.ENDED
    assert _count(camera) == 0
    webrtc_delegate.handle_end.assert_awaited_once_with(session)


async def test_oversized_reoffer(
    stream_camera: Callable[..., Camera], webrtc_delegate: AsyncMock
) -> None:
    """Test a reoffer above the SDP limit answers an error before the delegate."""
    camera = stream_camera()
    session = await _active(camera)
    request = hksv3.WebRTCReoffer(session.id, "a" * (hksv3.MAX_SDP_SIZE + 1))

    response = await _controller(camera)._set_reoffer(
        to_base64_str(request.encode()), CLIENT
    )

    assert hksv3.WebRTCReofferResponse.decode(base64_to_bytes(response)) == (
        hksv3.WebRTCReofferResponse(None, 3)
    )
    webrtc_delegate.handle_reoffer.assert_not_called()
