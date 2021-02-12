"""Tests for pyhap.camera."""
from unittest.mock import Mock, patch
from uuid import UUID

from pyhap import camera

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
    """Test starting a stream request"""
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
