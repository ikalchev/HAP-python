"""Tests for the HKSV 17.99 multi-tier RTP streaming of pyhap.camera."""

import asyncio
from collections.abc import Callable
import struct
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from pyhap import hksv3, tlv
from pyhap.camera import SETUP_ADDR_INFO, SETUP_SRTP_PARAM, SETUP_TYPES, Camera
from pyhap.camera_multi_tier import (
    MultiTierController,
    MultiTierSession,
    RtcpMonitor,
    open_rtcp_monitor,
)
from pyhap.camera_stream import SessionState, StreamError
from pyhap.camera_webrtc import WebRTCController
from pyhap.util import base64_to_bytes, to_base64_str

MULTI_TIER = "CameraMultiTierRTPStreamManagement"
WEBRTC = "CameraWebRTCStreamManagement"
SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
SESSION = UUID("acccc6cc-1505-6345-158d-a974b4509006")
CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 50000)
KEY = bytes(range(16))
SALT = bytes(range(14))
HIGH = hksv3.VideoStreamTier(1, 2, 1700, 1920, 1080, 30, 1800)
MEDIUM = hksv3.VideoStreamTier(2, 3, 768, 1280, 720, 30, 800)


def _controller(camera: Camera) -> MultiTierController:
    return camera.multi_tier_controllers[SENSOR]


def _char(camera: Camera, service: str, name: str) -> Any:
    found = camera.get_service(service)
    assert found is not None
    return found.get_characteristic(name)


# Services and options


async def _hang(*_args: object) -> None:
    """Never return, as a hook stuck beyond its timeout."""
    await asyncio.Event().wait()


async def test_services(stream_camera: Callable[..., Camera]) -> None:
    """Test the advertised tiers, payload types and linked services."""
    camera = stream_camera()
    multi_tier = camera.get_service(MULTI_TIER)
    webrtc = camera.get_service(WEBRTC)
    transport = camera.get_service("DataStreamTransportManagement")
    assert multi_tier is not None
    assert webrtc is not None
    video = base64_to_bytes(
        _char(camera, MULTI_TIER, "SupportedVideoStreamTiers").value
    )
    assert video[:6] == bytes.fromhex("010102 020163")
    assert (
        video
        == hksv3.SupportedVideoStreamTiers(
            hksv3.VideoCodec.H265, 99, _controller(camera).tiers
        ).encode()
    )
    audio = base64_to_bytes(
        _char(camera, MULTI_TIER, "SupportedAudioStreamTiers").value
    )
    assert audio == bytes.fromhex(
        "010103 02016e 0318 010401000000 0204c05d0000 030104 040102 050114 060101"
    )
    assert _char(camera, WEBRTC, "WebRTCSupportedVideoStreamTiers").value == (
        to_base64_str(video)
    )
    assert _char(camera, WEBRTC, "WebRTCSupportedAudioStreamTiers").value == (
        to_base64_str(audio)
    )
    assert base64_to_bytes(_char(camera, WEBRTC, "SensorUUID").value) == SENSOR.bytes
    assert _char(camera, MULTI_TIER, "StatusActive").value is True
    assert multi_tier.linked_services == [webrtc, transport]
    assert webrtc.linked_services == [multi_tier, transport]
    assert camera.services.index(multi_tier) < camera.services.index(webrtc)


async def test_rtp_services_come_after(stream_camera: Callable[..., Camera]) -> None:
    """Test the RTP stream management services follow with a new unique_id."""
    camera = stream_camera()
    rtp = camera.get_service("CameraRTPStreamManagement")
    multi_tier = camera.get_service(MULTI_TIER)
    assert rtp is not None
    assert rtp.unique_id == "rtp-0"
    assert camera.services.index(cast(Any, multi_tier)) < camera.services.index(rtp)


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"webrtc": None}, "need each other"),
        ({"multi_tier": None}, "need each other"),
        ({"video": None}, "need the video and audio"),
        ({"video": {}}, "need the video and audio"),
        ({"audio": None}, "need the video and audio"),
        ({"audio": {}}, "need the video and audio"),
        ({"stream_count": 0}, "need the video and audio"),
        ({"sensors": []}, "need the sensors"),
        ({"global_operating_mode": None}, "need the sensors"),
        ({"recording": False}, "need the sensors"),
        ({"multi_tier": {"start_stream_cmd": "ffmpeg"}}, "Unknown multi_tier"),
        ({"webrtc": {"offer_timeout": 8}}, "Unknown webrtc option"),
        ({"multi_tier": {"max_sessions": 4}}, "at least 5"),
        ({"webrtc": {"max_sessions": 5}}, "at least 6"),
        ({"multi_tier": {"max_sessions_per_connection": 0}}, "must be 1 to"),
        ({"webrtc": {"max_sessions_per_connection": 7}}, "must be 1 to"),
        ({"multi_tier": {"hook_timeout": 9}}, "hook_timeout"),
        ({"webrtc": {"remote_quality": 5}}, "remote_quality"),
        ({"multi_tier": {"audio": None}}, "exactly one audio tier"),
    ],
)
async def test_invalid_options(
    stream_camera: Callable[..., Camera], options: dict[str, Any], match: str
) -> None:
    """Test invalid combinations and values raise."""
    with pytest.raises(ValueError, match=match):
        stream_camera(**options)


async def test_tier_warnings(
    stream_camera: Callable[..., Camera], caplog: pytest.LogCaptureFixture
) -> None:
    """Test bit rates beyond the guide are logged, not refused."""
    sensor = hksv3.SensorConfiguration(
        SENSOR,
        1920,
        1080,
        video_streams=[
            hksv3.VideoStreamCapability(UUID(int=1), 2, 1920, 1080, 30, 1700, 2500),
            hksv3.VideoStreamCapability(UUID(int=2), 3, 1280, 720, 30, 768, 800),
            hksv3.VideoStreamCapability(UUID(int=3), 4, 640, 360, 15, 180, 190),
        ],
    )
    stream_camera(sensors=[sensor])
    assert "1920x1080@30: peak above the guide's 1800 kbps" in caplog.text


async def test_status_active(stream_camera: Callable[..., Camera]) -> None:
    """Test Status Active follows the global operating mode and the service."""
    camera = stream_camera()
    camera.set_manually_disabled(True)
    assert _char(camera, MULTI_TIER, "StatusActive").value is False
    camera.set_manually_disabled(False)
    _char(camera, MULTI_TIER, "StreamingEnabled").client_update_value(False)
    assert _char(camera, MULTI_TIER, "StatusActive").value is True
    await asyncio.sleep(0)
    assert _char(camera, MULTI_TIER, "StatusActive").value is False


async def test_streaming_enabled_persisted(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test the services' Streaming Enabled survive a restart."""
    camera = stream_camera()
    _char(camera, WEBRTC, "StreamingEnabled").client_update_value(False)
    restored = stream_camera()
    assert _char(restored, WEBRTC, "StreamingEnabled").value is False
    assert _char(restored, MULTI_TIER, "StreamingEnabled").value is True


async def test_camera_forwards_to_controllers(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test the camera's session actions reach each controller."""
    camera = stream_camera()
    controllers = [
        *camera.multi_tier_controllers.values(),
        *camera.webrtc_controllers.values(),
    ]
    with (
        patch.object(MultiTierController, "close") as close,
        patch.object(MultiTierController, "client_disconnected") as disconnected,
        patch.object(MultiTierController, "end_session", return_value=True) as end,
        patch.object(MultiTierController, "end_all_sessions") as end_all,
    ):
        await camera.stop()
        camera.client_disconnected(CLIENT)
        assert await camera.end_session(SESSION) is True
        await camera.end_all_sessions()
    close.assert_awaited_once()
    disconnected.assert_called_once_with(CLIENT)
    end.assert_awaited_once_with(SESSION)
    end_all.assert_awaited_once()
    assert len(controllers) == 2


@pytest.mark.parametrize(
    ("multi_tier_ended", "webrtc_ended", "expected"),
    [(True, True, True), (False, True, True), (False, False, False)],
)
async def test_camera_ends_the_id_in_each_controller(
    stream_camera: Callable[..., Camera],
    multi_tier_ended: bool,
    webrtc_ended: bool,
    expected: bool,
) -> None:
    """Test the camera ends a session ID in every controller, not the first only."""
    camera = stream_camera()
    with (
        patch.object(
            MultiTierController, "end_session", return_value=multi_tier_ended
        ) as multi_tier_end,
        patch.object(
            WebRTCController, "end_session", return_value=webrtc_ended
        ) as webrtc_end,
    ):
        assert await camera.end_session(SESSION) is expected
    await camera.stop()

    multi_tier_end.assert_awaited_once_with(SESSION)
    webrtc_end.assert_awaited_once_with(SESSION)


# Sessions


def _setup_request(
    address: bytes = b"192.168.1.114",
    is_ipv6: bool = False,
    suite: bytes = b"\x00",
    key: bytes = KEY,
) -> str:
    """A Setup Endpoints write of SESSION from the controller at ``address``."""
    address_tlv = tlv.encode(
        SETUP_ADDR_INFO["ADDRESS_VER"],
        struct.pack("?", is_ipv6),
        SETUP_ADDR_INFO["ADDRESS"],
        address,
        SETUP_ADDR_INFO["VIDEO_RTP_PORT"],
        struct.pack("<H", 50000),
        SETUP_ADDR_INFO["AUDIO_RTP_PORT"],
        struct.pack("<H", 50002),
    )
    srtp = tlv.encode(
        SETUP_SRTP_PARAM["CRYPTO"],
        suite,
        SETUP_SRTP_PARAM["MASTER_KEY"],
        key,
        SETUP_SRTP_PARAM["MASTER_SALT"],
        SALT,
    )
    return tlv.encode(
        SETUP_TYPES["SESSION_ID"],
        SESSION.bytes,
        SETUP_TYPES["ADDRESS"],
        address_tlv,
        SETUP_TYPES["VIDEO_SRTP_PARAM"],
        srtp,
        SETUP_TYPES["AUDIO_SRTP_PARAM"],
        srtp,
        to_base64=True,
    )


async def _setup(
    camera: Camera, request: str | None = None, client: Any = CLIENT
) -> dict[bytes, Any]:
    """Write Setup Endpoints and decode the answer the writer reads back."""
    controller = _controller(camera)
    answer = await controller._set_setup_endpoints(request or _setup_request(), client)
    return tlv.decode(answer, from_base64=True)


def _session(camera: Camera) -> MultiTierSession:
    return _controller(camera).sessions[SESSION]


def _control(
    command: int, video_tier: int | None = 1, audio_tier: int | None = 1
) -> str:
    """An RTP Streaming Control write for SESSION."""
    data = hksv3.encode_item(1, SESSION.bytes) + hksv3.encode_item(2, bytes([command]))
    if video_tier is not None:
        data += hksv3.encode_item(3, video_tier.to_bytes(4, "little"))
        data += hksv3.encode_item(4, (0x1234).to_bytes(4, "little"))
    if audio_tier is not None:
        data += hksv3.encode_item(5, audio_tier.to_bytes(4, "little"))
        data += hksv3.encode_item(6, (0x5678).to_bytes(4, "little"))
    return to_base64_str(data)


async def _streaming_control(
    camera: Camera, command: int = 2, client: Any = CLIENT, **tiers: Any
) -> int:
    """Write RTP Streaming Control and return the status of the answer."""
    controller = _controller(camera)
    answer = await controller._set_streaming_control(_control(command, **tiers), client)
    status = tlv.decode(base64_to_bytes(answer))[b"\x02"]
    return int(status[0])


async def _starts(camera: Camera, count: int) -> list[int]:
    """Start SESSION ``count`` times, return the statuses."""
    return [await _streaming_control(camera) for _ in range(count)]


def _setup_request_of(session_id: UUID) -> str:
    """A Setup Endpoints write like ``_setup_request()`` for ``session_id``."""
    request = base64_to_bytes(_setup_request())
    return to_base64_str(request[:2] + session_id.bytes + request[18:])


async def _setup_many(camera: Camera, count: int) -> list[int]:
    """Set up ``count`` sessions with other IDs, return the answer statuses."""
    statuses: list[int] = []
    for index in range(count):
        answer = await _setup(camera, _setup_request_of(UUID(int=index + 1)))
        statuses.append(answer[SETUP_TYPES["STATUS"]][0])
    return statuses


async def test_setup(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a session is prepared and answered from the stream address."""
    camera = stream_camera()

    answer = await _setup(camera)

    session = _session(camera)
    assert answer[SETUP_TYPES["STATUS"]] == b"\x00"
    assert tlv.decode(answer[SETUP_TYPES["ADDRESS"]]) == {
        SETUP_ADDR_INFO["ADDRESS_VER"]: b"\x00",
        SETUP_ADDR_INFO["ADDRESS"]: b"127.0.0.1",
        SETUP_ADDR_INFO["VIDEO_RTP_PORT"]: struct.pack("<H", 50000),
        SETUP_ADDR_INFO["AUDIO_RTP_PORT"]: struct.pack("<H", 50002),
    }
    assert answer[SETUP_TYPES["VIDEO_SSRC"]] == struct.pack("<I", session.video_ssrc)
    assert answer[SETUP_TYPES["AUDIO_SSRC"]] == struct.pack("<I", session.audio_ssrc)
    assert session.video_ssrc != session.audio_ssrc
    assert session.state is SessionState.PREPARED
    assert session.client_addr == CLIENT
    assert session.address == "192.168.1.114"
    assert session.video_srtp_key == KEY
    multi_tier_delegate.handle_prepare.assert_awaited_once_with(session)


async def test_setup_answers_from_the_connection(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test the answer has the accessory's address on the connection."""
    camera = stream_camera()
    with patch.object(camera.driver, "local_address", return_value="fd00::10"):
        answer = await _setup(camera, _setup_request(b"fd00::1", True))
    address = tlv.decode(answer[SETUP_TYPES["ADDRESS"]])
    assert address[SETUP_ADDR_INFO["ADDRESS_VER"]] == b"\x01"
    assert address[SETUP_ADDR_INFO["ADDRESS"]] == b"fd00::10"


async def test_prepare_may_change_the_answer(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test the delegate may answer with other addresses and ports."""

    async def _prepare(session: MultiTierSession) -> None:
        session.accessory_address = "192.168.1.20"
        session.accessory_video_port = 40000

    multi_tier_delegate.handle_prepare.side_effect = _prepare
    answer = await _setup(stream_camera())
    address = tlv.decode(answer[SETUP_TYPES["ADDRESS"]])
    assert address[SETUP_ADDR_INFO["ADDRESS"]] == b"192.168.1.20"
    assert address[SETUP_ADDR_INFO["VIDEO_RTP_PORT"]] == struct.pack("<H", 40000)


@pytest.mark.parametrize(
    "request_value",
    [
        _setup_request(b"192.168.1.114 x"),
        _setup_request(b"fe80::1%en0", True),
        _setup_request(b"fd00::1", True),
        _setup_request(suite=b"\x01"),
        _setup_request(key=bytes(32)),
    ],
    ids=["not-canonical", "scope-id", "no-ipv6-address", "suite", "key-length"],
)
async def test_setup_refused(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    request_value: str,
) -> None:
    """Test invalid endpoints answer an error before the delegate sees them."""
    camera = stream_camera()
    answer = await _setup(camera, request_value)
    assert answer == {
        SETUP_TYPES["SESSION_ID"]: SESSION.bytes,
        SETUP_TYPES["STATUS"]: b"\x02",
    }
    assert _controller(camera).sessions == {}
    multi_tier_delegate.handle_prepare.assert_not_called()


async def test_setup_refused_while_streaming_is_off(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test no session is set up while streaming is not allowed."""
    camera = stream_camera()
    camera.set_manually_disabled(True)
    answer = await _setup(camera)
    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"


async def test_setup_without_delegate(stream_camera: Callable[..., Camera]) -> None:
    """Test a camera without a delegate refuses sessions."""
    camera = stream_camera(multi_tier={})
    answer = await _setup(camera)
    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"


async def test_setup_busy(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test sessions beyond max_sessions answer busy."""
    camera = stream_camera(
        multi_tier={"delegate": multi_tier_delegate, "max_sessions_per_connection": 5}
    )
    assert await _setup_many(camera, 6) == [0, 0, 0, 0, 0, 1]


async def test_setup_busy_per_connection(stream_camera: Callable[..., Camera]) -> None:
    """Test a connection gets three sessions, another one still gets its own."""
    camera = stream_camera()

    assert await _setup_many(camera, 4) == [0, 0, 0, 1]
    assert (await _setup(camera, client=OTHER))[SETUP_TYPES["STATUS"]] == b"\x00"


async def test_malformed_setup(stream_camera: Callable[..., Camera]) -> None:
    """Test a Setup Endpoints write that does not decode answers an error."""
    answer = await _controller(stream_camera())._set_setup_endpoints("AQ==", CLIENT)
    assert base64_to_bytes(answer) == bytes.fromhex("020102")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RuntimeError(), b"\x02"),
        (StreamError(hksv3.SetupEndpointsStatus.BUSY), b"\x01"),
    ],
)
async def test_prepare_fails(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    error: Exception,
    status: bytes,
) -> None:
    """Test a failed prepare answers its status and still releases the session."""
    multi_tier_delegate.handle_prepare.side_effect = error
    camera = stream_camera()

    answer = await _setup(camera)
    await camera.stop()

    assert answer[SETUP_TYPES["STATUS"]] == status
    assert _controller(camera).sessions == {}
    multi_tier_delegate.handle_end.assert_awaited_once()


async def test_slow_prepare(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a prepare beyond hook_timeout answers an error in time."""
    multi_tier_delegate.handle_prepare.side_effect = _hang
    camera = stream_camera(
        multi_tier={"delegate": multi_tier_delegate, "hook_timeout": 0.05}
    )

    answer = await _setup(camera)
    await camera.stop()

    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"
    assert "The prepare hook timed out" in caplog.text
    multi_tier_delegate.handle_end.assert_awaited_once()


async def test_setup_again(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test setting a session up again releases the former one first."""
    camera = stream_camera()
    await _setup(camera)
    first = _session(camera)

    answer = await _setup(camera)

    assert answer[SETUP_TYPES["STATUS"]] == b"\x00"
    multi_tier_delegate.handle_end.assert_awaited_once_with(first)
    assert _session(camera) is not first


@pytest.mark.parametrize("client", [CLIENT, OTHER], ids=["same", "other"])
async def test_setup_again_concurrently(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    client: Any,
) -> None:
    """Test a write that sets the ID up during a set up again wins.

    The set up again releases the former session and then finds the ID
    taken, so no prepared session is lost: each one is released.
    """
    camera = stream_camera()
    await _setup(camera)

    again, other = await asyncio.gather(_setup(camera), _setup(camera, client=client))
    session = _session(camera)
    await camera.stop()

    assert again[SETUP_TYPES["STATUS"]] == b"\x02"
    assert other[SETUP_TYPES["STATUS"]] == b"\x00"
    assert session.client_addr == client
    assert multi_tier_delegate.handle_prepare.await_count == 2
    assert (
        multi_tier_delegate.handle_end.await_args_list
        == multi_tier_delegate.handle_prepare.await_args_list
    )


async def test_setup_again_finds_no_room(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a set up again is busy when a new session took its room meanwhile."""
    camera = stream_camera()
    await _setup(camera)
    await _setup_many(camera, 2)

    again, new = await asyncio.gather(
        _setup(camera), _setup(camera, _setup_request_of(UUID(int=3)))
    )
    sessions = dict(_controller(camera).sessions)
    await camera.stop()

    assert again[SETUP_TYPES["STATUS"]] == b"\x01"
    assert new[SETUP_TYPES["STATUS"]] == b"\x00"
    assert sorted(sessions) == [UUID(int=1), UUID(int=2), UUID(int=3)]
    assert multi_tier_delegate.handle_end.await_count == 4


@pytest.fixture
def end_gate(multi_tier_delegate: AsyncMock) -> asyncio.Event:
    """Make ``handle_end`` wait for the returned event.

    ``handle_end.waiting`` is set once it waits.
    """
    gate = asyncio.Event()
    waiting = asyncio.Event()

    async def _end(_session: MultiTierSession) -> None:
        waiting.set()
        await gate.wait()

    multi_tier_delegate.handle_end.side_effect = _end
    multi_tier_delegate.handle_end.waiting = waiting
    return gate


async def test_setup_again_turned_off_meanwhile(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    end_gate: asyncio.Event,
) -> None:
    """Test a set up again is refused when streaming turned off during the release."""
    camera = stream_camera()
    await _setup(camera)
    again = asyncio.ensure_future(_setup(camera))
    await multi_tier_delegate.handle_end.waiting.wait()

    camera.set_manually_disabled(True)
    end_gate.set()
    answer = await again
    await camera.stop()

    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"
    multi_tier_delegate.handle_prepare.assert_awaited_once()


async def test_setup_again_release_too_slow(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    end_gate: asyncio.Event,
) -> None:
    """Test a set up again answers an error when the former release overruns."""
    camera = stream_camera(
        multi_tier={"delegate": multi_tier_delegate, "hook_timeout": 0.05}
    )
    await _setup(camera)

    answer = await _setup(camera)
    # The former session ended and no new one took its place.
    assert _controller(camera).sessions == {}
    end_gate.set()
    await camera.stop()

    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"
    multi_tier_delegate.handle_prepare.assert_awaited_once()


async def test_setup_of_another_controller(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test another connection cannot take a session over."""
    camera = stream_camera()
    await _setup(camera)
    session = _session(camera)

    answer = await _setup(camera, client=OTHER)

    assert answer[SETUP_TYPES["STATUS"]] == b"\x02"
    assert _session(camera) is session
    assert await _streaming_control(camera, client=OTHER) == 1


async def test_start(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a start streams the picked tiers with the session's SSRCs."""
    camera = stream_camera()
    await _setup(camera)
    session = _session(camera)

    assert await _streaming_control(camera) == 0

    multi_tier_delegate.handle_start.assert_awaited_once_with(
        session, HIGH, hksv3.DEFAULT_AUDIO_TIER
    )
    assert session.state is SessionState.STARTED
    assert session.video_tier == HIGH
    assert session.controller_video_ssrc == 0x1234
    assert session.controller_audio_ssrc == 0x5678


async def test_start_video_only(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a start without an audio tier sends no audio."""
    camera = stream_camera()
    await _setup(camera)
    assert await _streaming_control(camera, video_tier=2, audio_tier=None) == 0
    multi_tier_delegate.handle_start.assert_awaited_once_with(
        _session(camera), MEDIUM, None
    )


@pytest.mark.parametrize(
    "tiers",
    [{"video_tier": 9}, {"video_tier": None}, {"audio_tier": 2}],
)
async def test_start_unknown_tiers(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    tiers: dict[str, Any],
) -> None:
    """Test a start of tiers that are not advertised fails."""
    camera = stream_camera()
    await _setup(camera)
    assert await _streaming_control(camera, **tiers) == 4
    multi_tier_delegate.handle_start.assert_not_called()


async def test_reconfigure(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a start of a started session changes its tiers, not its SSRCs."""
    camera = stream_camera()
    await _setup(camera)
    await _streaming_control(camera)
    session = _session(camera)
    ssrcs = (session.video_ssrc, session.audio_ssrc)

    assert await _streaming_control(camera, video_tier=2) == 0

    multi_tier_delegate.handle_reconfigure.assert_awaited_once_with(
        session, MEDIUM, hksv3.DEFAULT_AUDIO_TIER
    )
    assert (session.video_ssrc, session.audio_ssrc) == ssrcs
    assert session.video_tier == MEDIUM


@pytest.mark.parametrize(
    ("starts", "hook", "state"),
    [
        (1, "handle_start", SessionState.PREPARED),
        (2, "handle_reconfigure", SessionState.STARTED),
    ],
)
async def test_start_fails(
    stream_camera: Callable[..., Camera],
    multi_tier_delegate: AsyncMock,
    starts: int,
    hook: str,
    state: SessionState,
) -> None:
    """Test a failed start or change keeps the session as it was."""
    getattr(multi_tier_delegate, hook).side_effect = StreamError(3)
    camera = stream_camera()
    await _setup(camera)
    statuses = await _starts(camera, starts)
    assert statuses[-1] == 3
    assert _session(camera).state is state


async def test_start_while_preparing(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a start while the delegate still prepares is busy."""
    prepared = asyncio.Event()

    async def _prepare(session: MultiTierSession) -> None:
        await prepared.wait()

    multi_tier_delegate.handle_prepare.side_effect = _prepare
    camera = stream_camera()
    setup = asyncio.ensure_future(_setup(camera))
    await asyncio.sleep(0)

    assert await _streaming_control(camera) == 3

    prepared.set()
    assert (await setup)[SETUP_TYPES["STATUS"]] == b"\x00"


async def test_end(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test an end stops and releases the session once."""
    camera = stream_camera()
    await _setup(camera)
    await _streaming_control(camera)
    session = _session(camera)

    assert await _streaming_control(camera, command=1) == 0
    await camera.stop()

    multi_tier_delegate.handle_end.assert_awaited_once_with(session)
    assert session.state is SessionState.ENDED
    assert await _streaming_control(camera, command=1) == 1


async def test_end_before_start(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test ending a session that never started still releases it."""
    camera = stream_camera()
    await _setup(camera)
    assert await _streaming_control(camera, command=1) == 2
    await camera.stop()
    multi_tier_delegate.handle_end.assert_awaited_once()


async def test_malformed_control(stream_camera: Callable[..., Camera]) -> None:
    """Test a write that does not decode answers an error."""
    answer = await _controller(stream_camera())._set_streaming_control("AQ==", CLIENT)
    assert base64_to_bytes(answer) == bytes.fromhex("020104")


async def test_not_started_in_time(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a session that does not start within setup_timeout ends."""
    camera = stream_camera(
        multi_tier={"delegate": multi_tier_delegate, "setup_timeout": 0.01}
    )
    await _setup(camera)
    await asyncio.sleep(0.05)
    await camera.stop()
    assert _controller(camera).sessions == {}
    multi_tier_delegate.handle_end.assert_awaited_once()


async def test_idle_timeout(
    stream_camera: Callable[..., Camera], multi_tier_delegate: AsyncMock
) -> None:
    """Test a started session ends unless kept alive."""
    camera = stream_camera(
        multi_tier={"delegate": multi_tier_delegate, "idle_timeout": 0.01}
    )
    await _setup(camera)
    await _streaming_control(camera)
    await asyncio.sleep(0.1)
    assert _controller(camera).sessions == {}


async def test_keep_alive(stream_camera: Callable[..., Camera]) -> None:
    """Test keeping a started session alive restarts its 30 s idle timer."""
    camera = stream_camera()
    await _setup(camera)
    await _streaming_control(camera)
    session = _session(camera)
    timer = session._timer

    session.keep_alive()

    assert timer is not None
    assert timer.cancelled()
    assert session._timer is not None
    assert session._timer.when() > asyncio.get_running_loop().time() + 29
    await camera.stop()


async def test_former_session_keeps_nothing_alive(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test keep_alive() of a former session leaves a new one with its ID alone."""
    camera = stream_camera()
    await _setup(camera)
    await _streaming_control(camera)
    former = _session(camera)
    await _setup(camera)
    await _streaming_control(camera)
    timer = _session(camera)._timer

    former.keep_alive()

    assert timer is not None
    assert not timer.cancelled()
    assert _session(camera)._timer is timer
    await camera.stop()


async def _send_rtcp(monitor: RtcpMonitor, host: str) -> None:
    """Send a datagram from ``host`` to ``monitor`` and wait until it handled it."""
    handled = asyncio.Event()
    received = monitor.datagram_received

    def _received(data: bytes, addr: tuple[Any, ...]) -> None:
        received(data, addr)
        handled.set()

    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=(host, monitor.port)
    )
    with patch.object(monitor, "datagram_received", _received):
        transport.sendto(b"\x81\xc9\x00\x01")
        await asyncio.wait_for(handled.wait(), 2)
    transport.close()


@pytest.mark.parametrize(
    ("address", "kept_alive"),
    [(b"127.0.0.1", True), (b"192.168.1.114", False)],
    ids=["controller", "other-host"],
)
async def test_rtcp_monitor(
    stream_camera: Callable[..., Camera], address: bytes, kept_alive: bool
) -> None:
    """Test a datagram from the controller keeps the session alive, others not."""
    camera = stream_camera()
    await _setup(camera, _setup_request(address))
    session = _session(camera)
    monitor = await open_rtcp_monitor(session)
    await _streaming_control(camera)
    timer = session._timer

    await _send_rtcp(monitor, "127.0.0.1")

    assert timer is not None
    assert timer.cancelled() is kept_alive
    assert session.accessory_video_port == monitor.port
    assert monitor.port != 0
    monitor.close()
    assert monitor.closed
    await camera.stop()


async def test_rtcp_monitor_ipv6(stream_sensor: hksv3.SensorConfiguration) -> None:
    """Test the monitor of an IPv6 controller listens on IPv6."""
    session = MultiTierSession(
        id=SESSION,
        sensor=stream_sensor,
        client_addr=CLIENT,
        address="::1",
        is_ipv6=True,
        video_port=50000,
        audio_port=50002,
        video_srtp_key=KEY,
        video_srtp_salt=SALT,
        audio_srtp_key=KEY,
        audio_srtp_salt=SALT,
        video_ssrc=1,
        audio_ssrc=2,
        accessory_address="::1",
        accessory_video_port=50000,
        accessory_audio_port=50002,
    )
    monitor = await open_rtcp_monitor(session)

    with patch.object(session, "keep_alive") as keep_alive:
        await _send_rtcp(monitor, "::1")
    monitor.close()

    keep_alive.assert_called_once_with()
    assert session.accessory_video_port == monitor.port


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
    multi_tier_delegate: AsyncMock,
    end: Callable[[Camera], None],
) -> None:
    """Test turning streaming off and disconnecting end the sessions."""
    camera = stream_camera()
    await _setup(camera)
    await _streaming_control(camera)

    end(camera)
    await camera.stop()

    assert _controller(camera).sessions == {}
    multi_tier_delegate.handle_end.assert_awaited_once()


@pytest.mark.parametrize(
    ("sockname", "ipv6", "expected"),
    [
        (("192.168.1.10", 51826), False, "192.168.1.10"),
        (("192.168.1.10", 51826), True, None),
        (("::ffff:192.168.1.10", 51826, 0, 0), False, "192.168.1.10"),
        (("fe80::10%en0", 51826, 0, 4), True, "fe80::10"),
        (None, False, None),
    ],
)
async def test_driver_local_address(
    stream_driver: Any, sockname: Any, ipv6: bool, expected: str | None
) -> None:
    """Test the accessory's address on a connection, in an IP version."""
    connection = MagicMock()
    connection.transport.get_extra_info.return_value = sockname
    stream_driver.http_server.connections[CLIENT] = connection
    assert stream_driver.local_address(CLIENT, ipv6) == expected
    assert stream_driver.local_address(OTHER, ipv6) is None
