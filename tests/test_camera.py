"""Tests for pyhap.camera."""

import asyncio
import socket
import struct
from unittest.mock import Mock, patch
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


def test_init(mock_driver):
    """Test that the camera init properly computes TLV values"""
    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")

    management = acc.get_service("CameraRTPStreamManagement")
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


def test_setup_endpoints(mock_driver):
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
    setup_endpoints = acc.get_service("CameraRTPStreamManagement").get_characteristic(
        "SetupEndpoints"
    )
    setup_endpoints.client_update_value(set_endpoint_req)

    assert setup_endpoints.get_value()[:171] == set_endpoint_res[:171]


def test_set_selected_stream_start_stop(mock_driver):
    """Test starting a stream request."""  # noqa: D202

    # mocks for asyncio.Process
    async def communicate():
        return (None, "stderr")

    async def wait():
        pass

    process_mock = Mock()

    # Mock for asyncio.create_subprocess_exec
    async def subprocess_exec(*args, **kwargs):  # pylint: disable=unused-argument
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


def test_no_talkback_by_default(mock_driver):
    """Without the ``talkback`` option, no Speaker service is added and
    SetupEndpoints keeps echoing the controller's port back (unchanged,
    pre-existing behavior for accessories that don't opt in)."""
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    acc = camera.Camera(_OPTIONS, mock_driver, "Camera")
    assert acc.get_service("Speaker") is None

    setup_endpoints = acc.get_service("CameraRTPStreamManagement").get_characteristic(
        "SetupEndpoints"
    )
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    assert acc.sessions[session_id]["audio_backchannel"] is None


def _controller_audio_port(request_b64: str) -> int:
    """Extract the controller's requested audio port from a SetupEndpoints
    write value, for comparison against the accessory's response."""
    objs = tlv.decode(request_b64, from_base64=True)
    address_objs = tlv.decode(objs[camera.SETUP_TYPES["ADDRESS"]])
    return struct.unpack("<H", address_objs[camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[
        0
    ]


def _accessory_audio_port(response_b64: str) -> int:
    """Extract the accessory's reported audio port from a SetupEndpoints
    response value."""
    objs = tlv.decode(response_b64, from_base64=True)
    address_objs = tlv.decode(objs[camera.SETUP_TYPES["ADDRESS"]])
    return struct.unpack("<H", address_objs[camera.SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[
        0
    ]


def _audio_srtp_key_and_salt(request_b64: str) -> bytes:
    """Extract the audio SRTP master key + salt from a SetupEndpoints write
    value, exactly as the accessory itself would when negotiating the
    session."""
    objs = tlv.decode(request_b64, from_base64=True)
    audio_srtp_objs = tlv.decode(objs[camera.SETUP_TYPES["AUDIO_SRTP_PARAM"]])
    return (
        audio_srtp_objs[camera.SETUP_SRTP_PARAM["MASTER_KEY"]]
        + audio_srtp_objs[camera.SETUP_SRTP_PARAM["MASTER_SALT"]]
    )


def test_talkback_enabled_adds_speaker_service(mock_driver):
    """With ``talkback=True``, a muted-by-default Speaker service is added so
    that HAP clients know the accessory supports receiving two-way audio."""
    options = dict(_OPTIONS, talkback=True)
    acc = camera.Camera(options, mock_driver, "Camera")

    speaker = acc.get_service("Speaker")
    assert speaker is not None
    assert speaker.get_characteristic("Mute").get_value() is False


def test_setup_endpoints_reports_real_port_when_talkback_enabled(mock_driver):
    """The accessory must report a real local listening port for the audio
    talkback channel, not echo the controller's own port back at it (the bug
    this module fixes: HAP clients had nowhere valid to send the talkback
    audio to, because the accessory never actually listened anywhere)."""
    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    options = dict(_OPTIONS, talkback=True)
    acc = camera.Camera(options, mock_driver, "Camera")
    setup_endpoints = acc.get_service("CameraRTPStreamManagement").get_characteristic(
        "SetupEndpoints"
    )
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    receiver = acc.sessions[session_id]["audio_backchannel"]
    assert receiver is not None

    reported_port = _accessory_audio_port(setup_endpoints.get_value())
    assert reported_port == receiver.local_port
    assert reported_port != _controller_audio_port(set_endpoint_req)

    receiver.stop()


@pytest.mark.asyncio
async def test_talkback_audio_is_received_and_dispatched(mock_driver):
    """End-to-end: an SRTP audio packet sent to the reported port (as a HAP
    client would during a live view session) is decrypted, RTP-parsed, and
    dispatched to ``talkback_audio_received`` with the raw codec payload."""
    pylibsrtp = pytest.importorskip("pylibsrtp")

    mock_driver.loop = asyncio.get_running_loop()

    received = []

    class TalkbackCamera(camera.Camera):
        def talkback_audio_received(self, session_id, payload):
            received.append((session_id, payload))

    set_endpoint_req = (
        "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
        "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
        "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
    )

    options = dict(_OPTIONS, talkback=True)
    acc = TalkbackCamera(options, mock_driver, "Camera")
    setup_endpoints = acc.get_service("CameraRTPStreamManagement").get_characteristic(
        "SetupEndpoints"
    )
    setup_endpoints.client_update_value(set_endpoint_req)

    session_id = list(acc.sessions)[0]
    receiver = acc.sessions[session_id]["audio_backchannel"]
    port = receiver.local_port

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
    finally:
        sock.close()
        receiver.stop()

    assert received == [(session_id, rtp_payload)]
