"""Tests for pyhap.camera."""

import asyncio
import base64
import ipaddress
import logging
import socket
import struct
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from pyhap import camera, tlv

_OPTIONS = {
    "stream_count": 4,
    "video": {
        "codec": {
            "profiles": [
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
            ],
            "levels": [
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
            ],
        },
        "resolutions": [
            [320, 240, 15],
            [1024, 768, 30],
            [640, 480, 30],
            [640, 360, 30],
            [480, 360, 30],
            [480, 270, 30],
            [320, 240, 30],
            [320, 180, 30],
        ],
    },
    "audio": {
        "codecs": [
            {
                "type": "OPUS",
                "samplerate": 24,
            },
            {"type": "AAC-eld", "samplerate": 16},
        ],
    },
    "srtp": True,
    "address": "192.168.1.226",
}


def test_init(mock_driver: Any) -> None:
    """Test that the camera init properly computes TLV values"""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")

    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    assert management.unique_id is not None

    assert (
        management.get_characteristic("SupportedRTPConfiguration").get_value() == "AgEA"
    )
    assert (
        management.get_characteristic("SupportedVideoStreamConfiguration").get_value()
        == "AX4BAQACCQMBAAEBAAIBAAMMAQJAAQIC8AADAg8AAwwBAgAEAgIAAwMCHgADDAECgAICAuA"
        "BAwIeAAMMAQKAAgICaAEDAh4AAwwBAuABAgJoAQMCHgADDAEC4AECAg4BAwIeAAMMAQJAAQ"
        "IC8AADAh4AAwwBAkABAgK0AAMCHgA="
    )
    assert (
        management.get_characteristic("SupportedAudioStreamConfiguration").get_value()
        == "AQ4BAQMCCQEBAQIBAAMBAgEOAQECAgkBAQECAQADAQECAQA="
    )


def test_setup_endpoints(mock_driver: Any) -> None:
    """Test that the SetupEndpoint response is computed correctly"""
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    set_endpoint_res = (
        "ARCszGzBBWNFFY2pdLRQkAaRAgEAAxoBAQACDTE5Mi4xNjguMS4yMjYDAjPFBAK"
        "s1gQlAQEAAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QUlAQEAAh"
        "AKRPSRVaqGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQYBAQcBAQ=="
    )

    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(set_endpoint_req)

    assert setup_endpoints.get_value()[:171] == set_endpoint_res[:171]


def test_setup_endpoints_does_not_log_srtp_keys(
    mock_driver: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify the negotiated SRTP key material is not written to the logs."""
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")

    with caplog.at_level(logging.DEBUG, logger="pyhap.camera"):
        setup_endpoints.client_update_value(set_endpoint_req)

    # The endpoint configuration was logged at DEBUG level, but without any
    # of the SRTP master key/salt material in it.
    assert "Received endpoint configuration:" in caplog.text
    key_and_salt = _audio_srtp_key_and_salt(set_endpoint_req)
    assert base64.b64encode(key_and_salt).decode() not in caplog.text
    assert key_and_salt.hex() not in caplog.text


def test_set_selected_stream_start_stop(mock_driver: Any) -> None:
    """Test starting a stream request."""  # noqa: D202

    # mocks for asyncio.Process
    async def communicate() -> tuple[None, str]:
        return (None, "stderr")

    async def wait() -> None:
        pass

    process_mock = Mock()

    # Mock for asyncio.create_subprocess_exec
    async def subprocess_exec(*args: Any, **kwargs: Any) -> Mock:
        process_mock.id = 42
        process_mock.communicate = communicate
        process_mock.wait = wait
        return process_mock

    selected_config_req = (
        "ARUCAQEBEKzMbMEFY0UVjal0tFCQBpECNAEBAAIJAQEAAgEAAwEAAwsBAoAC"
        "AgJoAQMBHgQXAQFjAgQr66FSAwKEAAQEAAAAPwUCYgUDLAEBAgIMAQEBAgEA"
        "AwEBBAEeAxYBAW4CBMUInmQDAhgABAQAAKBABgENBAEA"
    )

    session_id = UUID("accc6cc1-0563-4515-8da9-74b450900691")

    session_info = {
        "id": session_id,
        "stream_idx": 0,
        "address": "192.168.1.114",
        "v_port": 50483,
        "v_srtp_key": "2JZgpMkwWUH8ahUtzp8VThtBmbk26hCPJqeWpYDR",
        "a_port": 54956,
        "a_srtp_key": "CkT0kVWqhnjZhEkyKI8TYvxaSNMu0a/TBpacuj2B",
        "process": None,
    }

    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")

    acc.sessions[session_id] = session_info

    patcher = patch("asyncio.create_subprocess_exec", new=subprocess_exec)
    patcher.start()

    acc.set_selected_stream_configuration(selected_config_req)

    assert acc.streaming_status == camera.STREAMING_STATUS["STREAMING"]

    selected_config_stop_req = "ARUCAQABEKzMbMEFY0UVjal0tFCQBpE="
    acc.set_selected_stream_configuration(selected_config_stop_req)

    patcher.stop()

    assert session_id not in acc.sessions
    assert process_mock.terminate.called
    assert acc.streaming_status == camera.STREAMING_STATUS["AVAILABLE"]


def test_no_talkback_by_default(mock_driver: Any) -> None:
    """Without the ``talkback`` option, no Speaker service is added and
    SetupEndpoints keeps echoing the controller's port back (unchanged,
    pre-existing behavior for accessories that don't opt in).
    """
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    assert acc.get_service("Speaker") is None

    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    assert acc.sessions[session_id]["audio_backchannel"] is None


def _controller_audio_port(request_b64: str) -> int:
    """Extract the controller's requested audio port from a SetupEndpoints
    write value, for comparison against the accessory's response.
    """
    objs = tlv.decode(base64.b64decode(request_b64))
    address_objs = tlv.decode(objs[camera.SETUP_TYPES["ADDRESS"]])
    return struct.unpack("<H", address_objs[camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[
        0
    ]


def _accessory_audio_port(response_b64: str) -> int:
    """Extract the accessory's reported audio port from a SetupEndpoints
    response value.
    """
    objs = tlv.decode(base64.b64decode(response_b64))
    address_objs = tlv.decode(objs[camera.SETUP_TYPES["ADDRESS"]])
    return struct.unpack("<H", address_objs[camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[
        0
    ]


def _audio_srtp_key_and_salt(request_b64: str) -> bytes:
    """Extract the audio SRTP master key + salt from a SetupEndpoints write
    value, exactly as the accessory itself would when negotiating the
    session.
    """
    objs = tlv.decode(base64.b64decode(request_b64))
    audio_srtp_objs = tlv.decode(objs[camera.SETUP_TYPES["AUDIO_SRTP_PARAM"]])
    return cast(
        bytes,
        audio_srtp_objs[camera.SETUP_SRTP_PARAM["MASTER_KEY"]]
        + audio_srtp_objs[camera.SETUP_SRTP_PARAM["MASTER_SALT"]],
    )


def test_talkback_enabled_adds_speaker_service(mock_driver: Any) -> None:
    """With ``talkback=True``, a muted-by-default Speaker service is added so
    that HAP clients know the accessory supports receiving two-way audio.
    """
    options = dict(_OPTIONS, talkback=True)
    acc = camera.Camera(options, mock_driver, "Camera")

    speaker = acc.get_service("Speaker")
    assert speaker is not None
    assert speaker.get_characteristic("Mute").get_value() is False


def test_setup_endpoints_reports_real_port_when_talkback_enabled(
    mock_driver: Any,
) -> None:
    """The accessory must report a real local listening port for the audio
    talkback channel, not echo the controller's own port back at it (the bug
    this module fixes: HAP clients had nowhere valid to send the talkback
    audio to, because the accessory never actually listened anywhere).
    """
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    options = dict(_OPTIONS, talkback=True)
    acc = camera.Camera(options, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    receiver = acc.sessions[session_id]["audio_backchannel"]
    assert receiver is not None

    reported_port = _accessory_audio_port(setup_endpoints.get_value())
    assert reported_port == receiver.local_port
    assert reported_port != _controller_audio_port(set_endpoint_req)

    receiver.stop()


@pytest.mark.asyncio
async def test_talkback_audio_is_received_and_dispatched(mock_driver: Any) -> None:
    """End-to-end: an SRTP audio packet sent to the reported port (as a HAP
    client would during a live view session) is decrypted, RTP-parsed, and
    dispatched to ``talkback_audio_received`` with the raw codec payload.
    """
    pylibsrtp = pytest.importorskip("pylibsrtp")

    mock_driver.loop = asyncio.get_running_loop()

    received = []

    class TalkbackCamera(camera.Camera):
        def talkback_audio_received(self, session_id: Any, payload: Any) -> None:
            received.append((session_id, payload))

    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    options = dict(_OPTIONS, talkback=True)
    acc = TalkbackCamera(options, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    receiver = acc.sessions[session_id]["audio_backchannel"]
    port = receiver.local_port
    # The test vector advertises 192.168.1.114 as the controller; pretend the
    # controller is local so the loopback send passes the source filter.
    receiver._controller_address = "127.0.0.1"

    key_and_salt = _audio_srtp_key_and_salt(set_endpoint_req)
    rtp_payload = b"opus-frame-payload"
    rtp_packet = struct.pack("!BBHII", 0x80, 110, 1, 0, 0x1234) + rtp_payload

    sender_session = pylibsrtp.Session(
        pylibsrtp.Policy(key=key_and_salt, ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND)
    )
    srtp_packet = sender_session.protect(rtp_packet)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(srtp_packet, ("127.0.0.1", port))

        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.02)

        assert received == [(session_id, rtp_payload)]

        # Packets from any source other than the negotiated controller are
        # dropped, even if they carry valid SRTP authentication. (A fresh RTP
        # sequence number is needed: SRTP rejects replayed sequence numbers.)
        receiver._controller_address = "192.168.1.114"
        received.clear()
        replay_packet = struct.pack("!BBHII", 0x80, 110, 2, 0, 0x1234) + rtp_payload
        sock.sendto(sender_session.protect(replay_packet), ("127.0.0.1", port))
        await asyncio.sleep(0.1)
        assert received == []
    finally:
        sock.close()
        receiver.stop()


@pytest.mark.asyncio
async def test_start_stream_rejects_argument_injection(mock_driver: Any) -> None:
    """Verify controller-supplied values cannot inject stream command args.

    The default stream command is formatted with the negotiated configuration
    and split on whitespace, so a malicious paired controller could otherwise
    smuggle extra command-line arguments into ffmpeg via the address (or any
    other string value) it writes during setup.
    """
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    session_info = {"id": UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")}
    safe_config = {
        "address": "192.168.1.100",
        "v_port": 1234,
        "v_srtp_key": "a2V5",
        "fps": 30,
        "width": 320,
        "height": 240,
        "v_ssrc": 123,
        "v_max_bitrate": 500,
    }

    # An address with whitespace (argument injection attempt) is refused.
    with (
        patch.object(asyncio, "create_subprocess_exec") as exec_mock,
    ):
        assert (
            await acc.start_stream(
                session_info, dict(safe_config, address="192.168.1.100 -i /etc/passwd")
            )
            is False
        )
        exec_mock.assert_not_called()

    # A non-IP address is refused as well.
    with (
        patch.object(asyncio, "create_subprocess_exec") as exec_mock,
    ):
        assert (
            await acc.start_stream(session_info, dict(safe_config, address="host"))
            is False
        )
        exec_mock.assert_not_called()

    # Whitespace in any other formatted string value is refused too.
    with (
        patch.object(asyncio, "create_subprocess_exec") as exec_mock,
    ):
        assert (
            await acc.start_stream(
                session_info, dict(safe_config, v_srtp_key="key with spaces")
            )
            is False
        )
        exec_mock.assert_not_called()

    # A valid configuration still starts the stream.
    async def subprocess_exec(*_args: Any, **_kwargs: Any) -> Mock:
        return Mock(pid=42)

    with (
        patch.object(
            asyncio, "create_subprocess_exec", side_effect=subprocess_exec
        ) as exec_mock,
    ):
        assert await acc.start_stream(session_info, safe_config) is True
        exec_mock.assert_called_once()


_SESSION_ID = UUID("acccc6cc-1505-6345-158d-a974b4509006")


async def test_start_stream_formats_each_argument(mock_driver: Any) -> None:
    """Test the default command formats each template argument on its own.

    A non-string value whose text has a space stays one argument.
    """
    acc = camera.Camera(
        {**_OPTIONS, "start_stream_cmd": "ffmpeg -i x srtp://{address}:{v_port}"},
        mock_driver,
        "Camera",
    )
    with patch.object(
        asyncio, "create_subprocess_exec", return_value=Mock(pid=42)
    ) as exec_mock:
        assert await acc.start_stream(
            {"id": _SESSION_ID}, {"address": "192.168.1.114", "v_port": (1, 2)}
        )
    assert exec_mock.call_args.args == (
        "ffmpeg",
        "-i",
        "x",
        "srtp://192.168.1.114:(1, 2)",
    )


def _address_tlv(address: bytes, is_ipv6: bool) -> bytes:
    """Encode the address info of a Setup Endpoints write."""
    return tlv.encode(
        camera.SETUP_ADDR_INFO["ADDRESS_VER"],
        struct.pack("?", is_ipv6),
        camera.SETUP_ADDR_INFO["ADDRESS"],
        address,
        camera.SETUP_ADDR_INFO["VIDEO_RTP_PORT"],
        struct.pack("<H", 50000),
        camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"],
        struct.pack("<H", 50002),
    )


def _setup_request(address: bytes, is_ipv6: bool) -> str:
    """Build a Setup Endpoints write for the controller address."""
    return _setup_request_with(_address_tlv(address, is_ipv6))


def _setup_request_with(address_tlv: bytes, crypto: bytes = b"\x00") -> str:
    """Build a Setup Endpoints write with the given address info."""
    srtp = tlv.encode(
        camera.SETUP_SRTP_PARAM["CRYPTO"],
        crypto,
        camera.SETUP_SRTP_PARAM["MASTER_KEY"],
        bytes(16),
        camera.SETUP_SRTP_PARAM["MASTER_SALT"],
        bytes(14),
    )
    return tlv.encode(
        camera.SETUP_TYPES["SESSION_ID"],
        _SESSION_ID.bytes,
        camera.SETUP_TYPES["ADDRESS"],
        address_tlv,
        camera.SETUP_TYPES["VIDEO_SRTP_PARAM"],
        srtp,
        camera.SETUP_TYPES["AUDIO_SRTP_PARAM"],
        srtp,
        to_base64=True,
    )


@pytest.mark.parametrize(
    ("address", "is_ipv6", "expected"),
    [
        (b"192.168.1.114", False, "192.168.1.114"),
        (b"fd00::1", True, "fd00::1"),
        (b"FD00::1", True, "fd00::1"),
        (b"fd00:0:0:0:0:0:0:1", True, "fd00::1"),
    ],
)
def test_setup_endpoints_accepts_addresses(
    mock_driver: Any, address: bytes, is_ipv6: bool, expected: str
) -> None:
    """Test addresses of the announced version are accepted in canonical form."""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    management.get_characteristic("SetupEndpoints").client_update_value(
        _setup_request(address, is_ipv6)
    )
    assert acc.sessions[_SESSION_ID]["address"] == expected


@pytest.mark.parametrize(
    ("address", "is_ipv6"),
    [
        (b"192.168.1.114 x", False),
        (b"192.168.1.114\n", False),
        (b"fe80::1%en0", True),
        (b"192.168.1.114", True),
        (b"fd00::1", False),
        (b"::ffff:192.168.1.114", False),
        (b"\xff", False),
        (b"", False),
    ],
)
def test_setup_endpoints_refuses_addresses(
    mock_driver: Any, address: bytes, is_ipv6: bool
) -> None:
    """Test other addresses answer an error and set up no session."""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(_setup_request(address, is_ipv6))
    assert acc.sessions == {}
    assert tlv.decode(setup_endpoints.get_value(), from_base64=True) == {
        camera.SETUP_TYPES["SESSION_ID"]: _SESSION_ID.bytes,
        camera.SETUP_TYPES["STATUS"]: camera.SETUP_STATUS["ERROR"],
    }


@pytest.mark.parametrize(
    "request_value",
    [
        _setup_request_with(
            tlv.encode(
                camera.SETUP_ADDR_INFO["ADDRESS_VER"],
                struct.pack("?", False),
                camera.SETUP_ADDR_INFO["ADDRESS"],
                b"192.168.1.114",
                camera.SETUP_ADDR_INFO["VIDEO_RTP_PORT"],
                struct.pack("<H", 50000),
            )
        ),
        _setup_request_with(
            tlv.encode(
                camera.SETUP_ADDR_INFO["ADDRESS_VER"],
                struct.pack("?", False),
                camera.SETUP_ADDR_INFO["ADDRESS"],
                b"192.168.1.114",
                camera.SETUP_ADDR_INFO["VIDEO_RTP_PORT"],
                b"\x01",
                camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"],
                struct.pack("<H", 50002),
            )
        ),
        _setup_request_with(_address_tlv(b"192.168.1.114", False), crypto=b""),
    ],
    ids=["missing_audio_port", "short_video_port", "empty_crypto"],
)
def test_setup_endpoints_refuses_malformed(
    mock_driver: Any, request_value: str
) -> None:
    """Test a malformed write answers an error and sets up no session."""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    setup_endpoints.client_update_value(request_value)
    assert acc.sessions == {}
    assert tlv.decode(setup_endpoints.get_value(), from_base64=True) == {
        camera.SETUP_TYPES["SESSION_ID"]: _SESSION_ID.bytes,
        camera.SETUP_TYPES["STATUS"]: camera.SETUP_STATUS["ERROR"],
    }


@pytest.mark.parametrize(
    ("address", "message"),
    [
        (b"192.168.1.114 x", "Refusing the endpoints"),
        (b"192.168.1.114", "Streaming is turned off"),
    ],
)
def test_setup_endpoints_refuses_address_before_streaming_off(
    mock_driver: Any,
    caplog: pytest.LogCaptureFixture,
    address: bytes,
    message: str,
) -> None:
    """Test an invalid address is refused first, then streaming turned off."""
    acc = camera.Camera(
        {**_OPTIONS, "global_operating_mode": []}, mock_driver, "Camera"
    )
    operating_mode = acc.get_service("CameraGlobalOperatingMode")
    assert operating_mode is not None
    operating_mode.get_characteristic("HomeKitCameraActive").client_update_value(0)
    management = acc.get_service("CameraRTPStreamManagement")
    assert management is not None
    setup_endpoints = management.get_characteristic("SetupEndpoints")
    with caplog.at_level(logging.INFO):
        setup_endpoints.client_update_value(_setup_request(address, False))
    assert message in caplog.text
    assert acc.sessions == {}
    assert tlv.decode(setup_endpoints.get_value(), from_base64=True) == {
        camera.SETUP_TYPES["SESSION_ID"]: _SESSION_ID.bytes,
        camera.SETUP_TYPES["STATUS"]: camera.SETUP_STATUS["ERROR"],
    }


@pytest.mark.asyncio
async def test_stop_stream_process_already_exited(mock_driver: Any) -> None:
    """Verify stopping a stream whose ffmpeg already exited does not raise."""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    process = Mock()
    process.terminate.side_effect = ProcessLookupError
    process.communicate = AsyncMock(return_value=(b"", b""))
    session_info = {
        "id": UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1"),
        "process": process,
    }

    await acc.stop_stream(session_info)

    process.terminate.assert_called_once_with()
    process.communicate.assert_awaited_once_with()
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_stop_stream_kill_after_process_exited(mock_driver: Any) -> None:
    """Verify the kill fallback tolerates a process that exited meanwhile."""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    process = Mock()
    process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    process.kill.side_effect = ProcessLookupError
    process.wait = AsyncMock(return_value=0)
    session_info = {
        "id": UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1"),
        "process": process,
    }

    await acc.stop_stream(session_info)

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("value", "is_ipv6", "expected"),
    [
        (b"192.168.1.114", False, ipaddress.IPv4Address("192.168.1.114")),
        (b"FD00:0::1", True, ipaddress.IPv6Address("fd00::1")),
    ],
    ids=["ipv4", "ipv6"],
)
def test_parse_setup_address_returns_the_address(
    value: bytes, is_ipv6: bool, expected: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> None:
    """Verify the parsed address comes back as an ip_address object."""
    assert camera.parse_setup_address(value, is_ipv6) == expected
