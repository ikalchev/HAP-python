"""Tests for classic HomeKit Secure Video recording.

Covers the recording configuration TLV codecs, the recording management service
setup (eligibility characteristics), and the HDS SetupDataStreamTransport
write-response handshake. The HDS fragment transfer itself is covered in
test_hds_recording.py.
"""

# pylint: disable=protected-access

import os
from unittest.mock import MagicMock, patch

import pytest

from pyhap import camera as camera_module, hds, hksv_recording as hr, tlv
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.util import base64_to_bytes, to_base64_str


@pytest.fixture(name="recording_camera")
def recording_camera_fixture():
    with (
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=MagicMock(), listen_address="127.0.0.1")
        options = {
            "video": {
                "codec": {
                    "profiles": [
                        camera_module.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"]
                    ],
                    "levels": [camera_module.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"]],
                },
                "resolutions": [[1920, 1080, 30], [1280, 720, 30]],
            },
            "audio": {"codecs": [{"type": "OPUS", "samplerate": 24}]},
            "srtp": True,
            "address": "127.0.0.1",
            "recording": True,
        }
        return Camera(options, driver, "Cam")


# --- Recording configuration TLV codecs ------------------------------------


def test_recording_supported_config_roundtrip():
    config = hr.SupportedRecordingConfiguration(
        prebuffer_length_ms=4000,
        event_triggers=hr.EventTrigger.MOTION | hr.EventTrigger.DOORBELL,
        media_containers=[hr.MediaContainerConfiguration(fragment_length_ms=4000)],
    )
    decoded = hr.SupportedRecordingConfiguration.decode(config.encode())
    assert decoded == config
    assert decoded.event_triggers == 3


def test_recording_supported_video_roundtrip():
    configs = [
        hr.VideoCodecConfiguration(
            codec_type=hr.VideoCodecType.H265,
            profile=2,
            level=2,
            bitrate_kbps=2000,
            iframe_interval_ms=4000,
            attributes=[
                hr.VideoAttributes(3840, 2160, 30),
                hr.VideoAttributes(1920, 1080, 30),
            ],
        ),
        hr.VideoCodecConfiguration(
            codec_type=hr.VideoCodecType.H264,
            profile=0,
            level=0,
            bitrate_kbps=800,
            iframe_interval_ms=4000,
            attributes=[hr.VideoAttributes(1280, 720, 24)],
        ),
    ]
    decoded = hr.decode_supported_video(hr.encode_supported_video(configs))
    assert len(decoded) == 2
    assert decoded[0].codec_type == hr.VideoCodecType.H265
    assert decoded[0].profile == 2 and decoded[0].level == 2
    assert [(a.width, a.height, a.frame_rate) for a in decoded[0].attributes] == [
        (3840, 2160, 30),
        (1920, 1080, 30),
    ]
    assert decoded[1].codec_type == hr.VideoCodecType.H264
    assert [(a.width, a.height, a.frame_rate) for a in decoded[1].attributes] == [
        (1280, 720, 24)
    ]
    # The supported advertisement omits per-config bitrate / iframe interval;
    # those only appear in the controller-selected configuration.
    assert decoded[0].bitrate_kbps == 0 and decoded[0].iframe_interval_ms == 0


def test_recording_supported_audio_roundtrip():
    configs = [
        hr.AudioCodecConfiguration(
            codec_type=hr.AudioCodecType.AAC_LC,
            channels=1,
            bitrate_mode=hr.BitRateMode.VARIABLE,
            sample_rate=hr.AudioSampleRate.KHZ_32,
            max_audio_bitrate_kbps=64,
        ),
        hr.AudioCodecConfiguration(
            codec_type=hr.AudioCodecType.AAC_ELD,
            sample_rate=hr.AudioSampleRate.KHZ_24,
        ),
    ]
    decoded = hr.decode_supported_audio(hr.encode_supported_audio(configs))
    assert len(decoded) == 2
    assert decoded[0].codec_type == hr.AudioCodecType.AAC_LC
    assert decoded[0].sample_rate == hr.AudioSampleRate.KHZ_32
    assert decoded[1].codec_type == hr.AudioCodecType.AAC_ELD
    # max-bitrate is omitted from the supported advertisement.
    assert decoded[0].max_audio_bitrate_kbps == 0


def test_recording_supported_video_multi_value_roundtrip():
    # A supported config advertising several profiles/levels must round-trip
    # faithfully: encode -> decode preserves the full lists (not just the first).
    config = hr.VideoCodecConfiguration(
        codec_type=hr.VideoCodecType.H264,
        profile=[0, 1, 2],
        level=[0, 1, 2],
        bitrate_kbps=2000,
        iframe_interval_ms=4000,
        attributes=[
            hr.VideoAttributes(1920, 1080, 30),
            hr.VideoAttributes(1280, 720, 30),
        ],
    )
    decoded = hr.decode_supported_video(hr.encode_supported_video([config]))[0]
    assert decoded.profile == [0, 1, 2]
    assert decoded.level == [0, 1, 2]
    assert [(a.width, a.height, a.frame_rate) for a in decoded.attributes] == [
        (1920, 1080, 30),
        (1280, 720, 30),
    ]


def test_recording_selected_config_roundtrip():
    selected = hr.SelectedRecordingConfiguration(
        recording=hr.SupportedRecordingConfiguration(
            prebuffer_length_ms=4000, event_triggers=hr.EventTrigger.MOTION
        ),
        video=hr.VideoCodecConfiguration(
            codec_type=hr.VideoCodecType.H265,
            profile=2,
            level=2,
            bitrate_kbps=2000,
            iframe_interval_ms=4000,
            attributes=[hr.VideoAttributes(1920, 1080, 30)],
        ),
        audio=hr.AudioCodecConfiguration(),
    )
    decoded = hr.SelectedRecordingConfiguration.decode(selected.encode())
    assert decoded == selected


# --- Recording management service setup / eligibility ----------------------


def test_recording_management_service_present(recording_camera):
    recording = recording_camera.get_service("CameraRecordingManagement")
    assert recording is not None
    # Recording starts inactive.
    assert recording.get_characteristic("Active").get_value() == 0
    for char in (
        "SupportedCameraRecordingConfiguration",
        "SupportedVideoRecordingConfiguration",
        "SupportedAudioRecordingConfiguration",
    ):
        assert recording.get_characteristic(char).get_value()


def test_recording_eligibility_characteristics(recording_camera):
    # HAP-NodeJS-parity characteristics iOS validates before managing recording.
    stream = recording_camera.get_service("CameraRTPStreamManagement")
    assert stream.get_characteristic("Active").get_value() == 1

    operating_mode = recording_camera.get_service("CameraOperatingMode")
    assert operating_mode.get_characteristic("HomeKitCameraActive").get_value() == 1
    assert operating_mode.get_characteristic("PeriodicSnapshotsActive").get_value() == 1

    motion = recording_camera.get_service("MotionSensor")
    assert motion.get_characteristic("StatusActive").get_value() is True


# --- HDS SetupDataStreamTransport write-response handshake ------------------


def test_data_stream_transport_service_present(recording_camera):
    service = recording_camera.get_service("DataStreamTransportManagement")
    assert service.get_characteristic("Version").get_value() == "1.0"
    assert (
        service.get_characteristic(
            "SupportedDataStreamTransportConfiguration"
        ).get_value()
        is not None
    )


def test_setup_data_stream_transport_without_session_errors(recording_camera):
    camera = recording_camera
    # No session key for this client, and no listener started.
    request = tlv.encode(hds.SETUP_TYPES["CONTROLLER_KEY_SALT"], b"\x11" * 32)
    # SetupDataStreamTransport is a write-response characteristic: the encoded
    # response is RETURNED from the setter (so HAP puts it in the write
    # response), not stored on the characteristic.
    value = camera.set_data_stream_transport(
        to_base64_str(request), sender_client_addr=("10.0.0.9", 5000)
    )
    objs = tlv.decode(base64_to_bytes(value))
    assert objs[hds.SETUP_RESPONSE_TYPES["STATUS"]] == hds.SETUP_STATUS_GENERIC_ERROR


@pytest.mark.asyncio
async def test_setup_data_stream_transport_write_response_plumbing(recording_camera):
    # The HAP write-response path calls client_update_value and forwards the
    # setter's RETURN value. Exercise that path (not the setter directly) and
    # confirm the address opt-in is detected for this two-argument setter.
    camera = recording_camera
    await camera._ensure_hds_listener()
    try:
        client = ("10.0.0.9", 5000)
        camera.driver.session_shared_keys[client] = os.urandom(32)
        char = camera.get_service("DataStreamTransportManagement").get_characteristic(
            "SetupDataStreamTransport"
        )
        assert char._setter_wants_addr() is True
        request = tlv.encode(hds.SETUP_TYPES["CONTROLLER_KEY_SALT"], os.urandom(32))
        value = char.client_update_value(to_base64_str(request), client)
        objs = tlv.decode(base64_to_bytes(value))
        assert objs[hds.SETUP_RESPONSE_TYPES["STATUS"]] == hds.SETUP_STATUS_SUCCESS
    finally:
        await camera.stop()


def test_single_argument_setter_still_called_with_one_argument(recording_camera):
    # Regression guard for the characteristic change: a plain single-argument
    # setter must still be invoked with the value alone even when a client
    # address is available.
    mute = recording_camera.get_service("Microphone").get_characteristic("Mute")
    seen = []
    mute.setter_callback = seen.append
    assert mute._setter_wants_addr() is False
    mute.client_update_value(True, ("10.0.0.9", 5000))
    assert seen == [True]


@pytest.mark.asyncio
async def test_setup_data_stream_transport_with_session(recording_camera):
    camera = recording_camera
    await camera._ensure_hds_listener()
    try:
        client = ("10.0.0.9", 5000)
        camera.driver.session_shared_keys[client] = os.urandom(32)
        request = tlv.encode(hds.SETUP_TYPES["CONTROLLER_KEY_SALT"], os.urandom(32))
        value = camera.set_data_stream_transport(
            to_base64_str(request), sender_client_addr=client
        )
        objs = tlv.decode(base64_to_bytes(value))
        assert objs[hds.SETUP_RESPONSE_TYPES["STATUS"]] == hds.SETUP_STATUS_SUCCESS
        assert objs[hds.SETUP_RESPONSE_TYPES["ACCESSORY_KEY_SALT"]]
        # The write response must carry the accessory's TCP listening port so
        # the controller can open the HDS connection.
        session_params = tlv.decode(
            objs[hds.SETUP_RESPONSE_TYPES["TRANSPORT_TYPE_SESSION_PARAMETERS"]]
        )
        port = int.from_bytes(
            session_params[hds.TRANSPORT_SESSION_PARAM_TCP_LISTENING_PORT], "little"
        )
        assert port == camera._hds_listener.port
    finally:
        await camera.stop()
