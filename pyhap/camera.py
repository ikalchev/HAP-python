"""Contains the Camera accessory and related.

When a HAP client (e.g. iOS) wants to start a video stream it does the following:
[0. Read supported RTP configuration]
[0. Read supported video configuration]
[0. Read supported audio configuration]
[0. Read the current streaming status]
1. Sets the SetupEndpoints characteristic to notify the camera about its IP address,
selected security parameters, etc.
2. The camera answers the write with its IP address, etc., and the client reads the
answer back from SetupEndpoints.
3. The client sets the SelectedRTPStreamConfiguration characteristic to notify the
camera of its prefered audio and video configuration and to initiate the start of the
streaming.
4. The camera starts the streaming with the above configuration.
[5. At some point the client can reconfigure or stop the stream similarly to step 3.]
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
import contextlib
import functools
import hashlib
import ipaddress
import logging
import os
import struct
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pyhap import (
    RESOURCE_DIR,
    hds,
    hds_recording,
    hds_server,
    hksv3,
    hksv_recording,
    tlv,
)
from pyhap.accessory import Accessory
from pyhap.camera_talkback import TalkbackReceiver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_CAMERA
from pyhap.service import Service
from pyhap.util import base64_to_bytes, byte_bool, callback, to_base64_str

if TYPE_CHECKING:
    from pyhap.camera_multi_tier import MultiTierController
    from pyhap.camera_webrtc import WebRTCController

# Seconds before arming a recording delegate is tried again, doubling up to
# the maximum while it keeps failing.
RECORDING_ARM_RETRY = 1.0
MAX_RECORDING_ARM_RETRY = 60.0

SETUP_TYPES = {
    "SESSION_ID": b"\x01",
    "STATUS": b"\x02",
    "ADDRESS": b"\x03",
    "VIDEO_SRTP_PARAM": b"\x04",
    "AUDIO_SRTP_PARAM": b"\x05",
    "VIDEO_SSRC": b"\x06",
    "AUDIO_SSRC": b"\x07",
}


SETUP_STATUS = {
    "SUCCESS": b"\x00",
    "BUSY": b"\x01",
    "ERROR": b"\x02",
}


SETUP_IPV = {
    "IPV4": b"\x00",
    "IPV6": b"\x01",
}


SETUP_ADDR_INFO = {
    "ADDRESS_VER": b"\x01",
    "ADDRESS": b"\x02",
    "VIDEO_RTP_PORT": b"\x03",
    "AUDIO_RTP_PORT": b"\x04",
}


SETUP_SRTP_PARAM = {
    "CRYPTO": b"\x01",
    "MASTER_KEY": b"\x02",
    "MASTER_SALT": b"\x03",
}


STREAMING_STATUS = {
    "AVAILABLE": b"\x00",
    "STREAMING": b"\x01",
    "BUSY": b"\x02",
}


RTP_CONFIG_TYPES = {
    "CRYPTO": b"\x02",
}


SRTP_CRYPTO_SUITES = {
    "AES_CM_128_HMAC_SHA1_80": b"\x00",
    "AES_CM_256_HMAC_SHA1_80": b"\x01",
    "NONE": b"\x02",
}


VIDEO_TYPES = {
    "CODEC": b"\x01",
    "CODEC_PARAM": b"\x02",
    "ATTRIBUTES": b"\x03",
    "RTP_PARAM": b"\x04",
}


VIDEO_CODEC_TYPES = {
    "H264": b"\x00",
}


VIDEO_CODEC_PARAM_TYPES = {
    "PROFILE_ID": b"\x01",
    "LEVEL": b"\x02",
    "PACKETIZATION_MODE": b"\x03",
    "CVO_ENABLED": b"\x04",
    "CVO_ID": b"\x05",
}


VIDEO_CODEC_PARAM_CVO_TYPES = {
    "UNSUPPORTED": b"\x01",
    "SUPPORTED": b"\x02",
}


VIDEO_CODEC_PARAM_PROFILE_ID_TYPES = {
    "BASELINE": b"\x00",
    "MAIN": b"\x01",
    "HIGH": b"\x02",
}


VIDEO_CODEC_PARAM_LEVEL_TYPES = {
    "TYPE3_1": b"\x00",
    "TYPE3_2": b"\x01",
    "TYPE4_0": b"\x02",
}


VIDEO_CODEC_PARAM_PACKETIZATION_MODE_TYPES = {
    "NON_INTERLEAVED": b"\x00",
}


VIDEO_ATTRIBUTES_TYPES = {
    "IMAGE_WIDTH": b"\x01",
    "IMAGE_HEIGHT": b"\x02",
    "FRAME_RATE": b"\x03",
}


SUPPORTED_VIDEO_CONFIG_TAG = b"\x01"


SELECTED_STREAM_CONFIGURATION_TYPES = {
    "SESSION": b"\x01",
    "VIDEO": b"\x02",
    "AUDIO": b"\x03",
}


RTP_PARAM_TYPES = {
    "PAYLOAD_TYPE": b"\x01",
    "SYNCHRONIZATION_SOURCE": b"\x02",
    "MAX_BIT_RATE": b"\x03",
    "RTCP_SEND_INTERVAL": b"\x04",
    "MAX_MTU": b"\x05",
    "COMFORT_NOISE_PAYLOAD_TYPE": b"\x06",
}


AUDIO_TYPES = {
    "CODEC": b"\x01",
    "CODEC_PARAM": b"\x02",
    "RTP_PARAM": b"\x03",
    "COMFORT_NOISE": b"\x04",
}


AUDIO_CODEC_TYPES = {
    "PCMU": b"\x00",
    "PCMA": b"\x01",
    "AACELD": b"\x02",
    "OPUS": b"\x03",
}


AUDIO_CODEC_PARAM_TYPES = {
    "CHANNEL": b"\x01",
    "BIT_RATE": b"\x02",
    "SAMPLE_RATE": b"\x03",
    "PACKET_TIME": b"\x04",
}


AUDIO_CODEC_PARAM_BIT_RATE_TYPES = {
    "VARIABLE": b"\x00",
    "CONSTANT": b"\x01",
}


AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES = {
    "KHZ_8": b"\x00",
    "KHZ_16": b"\x01",
    "KHZ_24": b"\x02",
}


SUPPORTED_AUDIO_CODECS_TAG = b"\x01"
SUPPORTED_COMFORT_NOISE_TAG = b"\x02"
SUPPORTED_AUDIO_CONFIG_TAG = b"\x02"
SET_CONFIG_REQUEST_TAG = b"\x02"
SESSION_ID = b"\x01"


NO_SRTP = b"\x01\x01\x02\x02\x00\x03\x00"
"""Configuration value for no SRTP."""


FFMPEG_CMD = (
    "ffmpeg -re -f avfoundation -framerate {fps} -i 0:0 -threads 0 "
    "-vcodec libx264 -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency "
    "-vf scale={width}:{height} -b:v {v_max_bitrate}k -bufsize {v_max_bitrate}k "
    "-payload_type 99 -ssrc {v_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} "
    "srtp://{address}:{v_port}?rtcpport={v_port}&"
    "localrtcpport={v_port}&pkt_size=1378"
)
"""Template for the ffmpeg command."""

logger = logging.getLogger(__name__)

SELECTED_RECORDING_CHAR = "SelectedCameraRecordingConfiguration"
PERSISTED_RECORDING_CHARS = (
    ("CameraRecordingManagement", "Active"),
    ("CameraRecordingManagement", "RecordingAudioActive"),
    ("CameraRecordingManagement", SELECTED_RECORDING_CHAR),
    ("CameraOperatingMode", "EventSnapshotsActive"),
    ("CameraOperatingMode", "HomeKitCameraActive"),
    ("CameraOperatingMode", "PeriodicSnapshotsActive"),
)
# accessory_state key of the HKSV 17.99 settings.
HKSV3_STATE = "hksv3"
HKSV3_MOTION_CHARS = ("ContributingSensors", "MotionEnabled")
# Concurrent video encodings each sensor must offer (HKSV 17.99 guide, section 2).
HKSV3_MIN_SENSOR_STREAMS = 3
SUPPORTED_RECORDING_CHARS = (
    "SupportedCameraRecordingConfiguration",
    "SupportedVideoRecordingConfiguration",
    "SupportedAudioRecordingConfiguration",
)
# Global operating mode chars a controller writes, the camera keeps them.
PERSISTED_GLOBAL_OPERATING_MODE_CHARS = (
    "HomeKitCameraActive",
    "StreamingEnabled",
    "CameraOperatingModeIndicator",
    "NightVision",
)


def parse_setup_address(
    value: bytes, is_ipv6: bool
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the controller address of a Setup Endpoints write.

    The address must be an IP address of the announced version, without an
    IPv6 scope ID. ``str()`` gives its canonical form, e.g. ``fd00::1`` for
    ``FD00:0::1``.

    :raises ValueError: If the address is anything else.
    """
    try:
        address = value.decode("utf-8")
        parsed = ipaddress.ip_address(address)
    except ValueError as ex:
        raise ValueError(f"invalid address {value!r}") from ex
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.scope_id is not None:
        raise ValueError(f"address {address!r} has a scope ID")
    if parsed.version != (6 if is_ipv6 else 4):
        raise ValueError(f"address {address!r} does not match its IP version")
    return parsed


def _decode_setup_endpoints(
    session_id: UUID, objs: dict[bytes, Any]
) -> tuple[str, bool, int, int, tuple[bytes, bytes], tuple[bytes, bytes]]:
    """Decode a Setup Endpoints write.

    Returns the controller address, whether it is IPv6, its video and audio
    ports and the video and audio SRTP master key and salt.

    :raises ValueError: If the address is not a valid IP address.
    :raises KeyError: If a field is missing.
    :raises struct.error: If a field is too short.
    :raises IndexError: If a crypto suite is empty.
    """
    address_info = tlv.decode(objs[SETUP_TYPES["ADDRESS"]])
    is_ipv6 = struct.unpack("?", address_info[SETUP_ADDR_INFO["ADDRESS_VER"]])[0]
    address = str(
        parse_setup_address(bytes(address_info[SETUP_ADDR_INFO["ADDRESS"]]), is_ipv6)
    )
    video_port, audio_port = (
        int(struct.unpack("<H", address_info[SETUP_ADDR_INFO[key]])[0])
        for key in ("VIDEO_RTP_PORT", "AUDIO_RTP_PORT")
    )
    video = tlv.decode(objs[SETUP_TYPES["VIDEO_SRTP_PARAM"]])
    audio = tlv.decode(objs[SETUP_TYPES["AUDIO_SRTP_PARAM"]])
    # Do not log the SRTP key material: debug logs may end up in less
    # trusted log collections, and the keys live for the whole session.
    logger.debug(
        "Received endpoint configuration:"
        "\nsession_id: %s\naddress: %s\nis_ipv6: %s"
        "\ntarget_video_port: %s\ntarget_audio_port: %s"
        "\nvideo_crypto_suite: %s\naudio_crypto_suite: %s",
        session_id,
        address,
        is_ipv6,
        video_port,
        audio_port,
        video[SETUP_SRTP_PARAM["CRYPTO"]][0],
        audio[SETUP_SRTP_PARAM["CRYPTO"]][0],
    )
    return (
        address,
        is_ipv6,
        video_port,
        audio_port,
        (
            bytes(video[SETUP_SRTP_PARAM["MASTER_KEY"]]),
            bytes(video[SETUP_SRTP_PARAM["MASTER_SALT"]]),
        ),
        (
            bytes(audio[SETUP_SRTP_PARAM["MASTER_KEY"]]),
            bytes(audio[SETUP_SRTP_PARAM["MASTER_SALT"]]),
        ),
    )


def _setup_endpoints_status(session_id: UUID, status: bytes) -> str:
    """Encode a Setup Endpoints answer that has only a status."""
    return tlv.encode(
        SETUP_TYPES["SESSION_ID"],
        session_id.bytes,
        SETUP_TYPES["STATUS"],
        status,
        to_base64=True,
    )


class Camera(Accessory):
    """An Accessory that can negotiate camera stream settings with a HAP client
    (e.g. iOS) and start a stream.

    The Camera accessory implements the HomeKit Protocol for negotiating stream
    settings, such as the picture width and height, number of audio channels and
    others. Starting a video and/or audio stream, however, is very platform
    specific. Because of this, you need to figure out what video and audio
    settings your camera supports and set them in the ``options`` parameter that
    is passed to the constructor - see the documentation of :meth:`__init__` for
    the settings you need to specify.

    By default, HAP-python will execute the :data:`FFMPEG_CMD` command with the
    negotiated parameters when a stream should be started and will terminate the
    started process when the stream should be stopped. If the default command is
    not supported or correctly formatted for your platform, the streaming can
    fail. For these cases, HAP-python has hooks so that you can insert your own
    command or implement the logic for starting or stopping the stream:

    1. Pass your own command that will be executed when the stream should be
       started. You pass the command as a value to the key ``start_stream_cmd``
       in the ``options`` parameter to the constructor of the ``Camera``
       Accessory. The command is formatted using the negotiated stream
       configuration parameters. For example, if the negotiated width is 640 and
       you pass ``foo start -width {width}``, the command will be formatted as
       ``foo start -width 640``. The full list of negotiated stream configuration
       parameters can be found in the documentation for the
       :meth:`start_stream` method.

    2. Implement your own logic to start, stop and reconfigure the stream. If
       you need more flexibility in managing streams, you can directly override
       the :meth:`start_stream`, :meth:`stop_stream` and
       :meth:`reconfigure_stream` methods. Each will be called when the stream
       should be respectively started, stopped or reconfigured. The start and
       reconfigure methods are given the negotiated stream configuration
       parameters.

    Finally, if you can take snapshots from the camera, you may want to
    implement the :meth:`get_snapshot` method. By default, this serves a stock
    photo.
    """

    category = CATEGORY_CAMERA

    @staticmethod
    def get_supported_rtp_config(support_srtp: bool) -> Any:
        """Return a tlv representation of the RTP configuration we support.

        SRTP support allows only the AES_CM_128_HMAC_SHA1_80 cipher for now.

        :param support_srtp: True if SRTP is supported, False otherwise.
        :type support_srtp: bool
        """
        if support_srtp:
            crypto = SRTP_CRYPTO_SUITES["AES_CM_128_HMAC_SHA1_80"]
        else:
            crypto = SRTP_CRYPTO_SUITES["NONE"]
        return tlv.encode(RTP_CONFIG_TYPES["CRYPTO"], crypto, to_base64=True)

    @staticmethod
    def get_supported_video_stream_config(video_params: dict[str, Any]) -> Any:
        """Return a tlv representation of the supported video stream configuration.

        Expected video parameters:
            - codec
            - resolutions

        :param video_params: Supported video configurations
        :type video_params: dict
        """
        codec_params_tlv = tlv.encode(
            VIDEO_CODEC_PARAM_TYPES["PACKETIZATION_MODE"],
            VIDEO_CODEC_PARAM_PACKETIZATION_MODE_TYPES["NON_INTERLEAVED"],
        )

        codec_params = video_params["codec"]
        for profile in codec_params["profiles"]:
            codec_params_tlv += tlv.encode(
                VIDEO_CODEC_PARAM_TYPES["PROFILE_ID"], profile
            )

        for level in codec_params["levels"]:
            codec_params_tlv += tlv.encode(VIDEO_CODEC_PARAM_TYPES["LEVEL"], level)

        attr_tlv = b""
        for resolution in video_params["resolutions"]:
            res_tlv = tlv.encode(
                VIDEO_ATTRIBUTES_TYPES["IMAGE_WIDTH"],
                struct.pack("<H", resolution[0]),
                VIDEO_ATTRIBUTES_TYPES["IMAGE_HEIGHT"],
                struct.pack("<H", resolution[1]),
                VIDEO_ATTRIBUTES_TYPES["FRAME_RATE"],
                struct.pack("<H", resolution[2]),
            )
            attr_tlv += tlv.encode(VIDEO_TYPES["ATTRIBUTES"], res_tlv)

        config_tlv = tlv.encode(
            VIDEO_TYPES["CODEC"],
            VIDEO_CODEC_TYPES["H264"],
            VIDEO_TYPES["CODEC_PARAM"],
            codec_params_tlv,
        )

        return tlv.encode(
            SUPPORTED_VIDEO_CONFIG_TAG, config_tlv + attr_tlv, to_base64=True
        )

    @staticmethod
    def get_supported_audio_stream_config(audio_params: dict[str, Any]) -> str:
        """Return a tlv representation of the supported audio stream configuration.

        iOS supports only AACELD and OPUS

        Expected audio parameters:
        - codecs
        - comfort_noise

        :param audio_params: Supported audio configurations
        :type audio_params: dict
        """
        has_supported_codec = False
        configs = b""
        for codec_param in audio_params["codecs"]:
            param_type = codec_param["type"]
            if param_type == "OPUS":
                has_supported_codec = True
                codec = AUDIO_CODEC_TYPES["OPUS"]
                bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES["VARIABLE"]
            elif param_type == "AAC-eld":
                has_supported_codec = True
                codec = AUDIO_CODEC_TYPES["AACELD"]
                bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES["VARIABLE"]
            else:
                logger.warning("Unsupported codec %s", param_type)
                continue

            param_samplerate = codec_param["samplerate"]
            if param_samplerate == 8:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES["KHZ_8"]
            elif param_samplerate == 16:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES["KHZ_16"]
            elif param_samplerate == 24:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES["KHZ_24"]
            else:
                logger.warning("Unsupported sample rate %s", param_samplerate)
                continue

            param_tlv = tlv.encode(
                AUDIO_CODEC_PARAM_TYPES["CHANNEL"],
                b"\x01",
                AUDIO_CODEC_PARAM_TYPES["BIT_RATE"],
                bitrate,
                AUDIO_CODEC_PARAM_TYPES["SAMPLE_RATE"],
                samplerate,
            )
            config_tlv = tlv.encode(
                AUDIO_TYPES["CODEC"], codec, AUDIO_TYPES["CODEC_PARAM"], param_tlv
            )
            configs += tlv.encode(SUPPORTED_AUDIO_CODECS_TAG, config_tlv)

        if not has_supported_codec:
            logger.warning("Client does not support any audio codec that iOS supports.")

            codec = AUDIO_CODEC_TYPES["OPUS"]
            bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES["VARIABLE"]
            samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES["KHZ_24"]

            param_tlv = tlv.encode(
                AUDIO_CODEC_PARAM_TYPES["CHANNEL"],
                b"\x01",
                AUDIO_CODEC_PARAM_TYPES["BIT_RATE"],
                bitrate,
                AUDIO_CODEC_PARAM_TYPES["SAMPLE_RATE"],
                samplerate,
            )

            config_tlv = tlv.encode(
                AUDIO_TYPES["CODEC"], codec, AUDIO_TYPES["CODEC_PARAM"], param_tlv
            )

            configs = tlv.encode(SUPPORTED_AUDIO_CODECS_TAG, config_tlv)

        comfort_noise = byte_bool(audio_params.get("comfort_noise", False))
        audio_config = to_base64_str(
            configs + tlv.encode(SUPPORTED_COMFORT_NOISE_TAG, comfort_noise)
        )
        return audio_config

    def __init__(self, options: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        """Initialize a camera accessory with the given options.

        :param options: Describes the supported video and audio configuration
            of this camera. Expected values are video, audio, srtp and address.
            Example configuration:

            .. code-block:: python

            {
                "video": {
                    "codec": {
                        "profiles": [
                            camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
                        ],
                        "levels": [
                            camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE3_1'],
                        ],
                    },
                    "resolutions": [
                        [320, 240, 15],  # Width, Height, framerate
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
                            'type': 'OPUS',
                            'samplerate': 24,
                        },
                        {
                            'type': 'AAC-eld',
                            'samplerate': 16
                        }
                    ],
                },
                "address": "192.168.1.226",  # Address from which the camera will stream
            }

            Additional optional values are:
            - srtp - boolean, defaults to False. Whether the camera supports SRTP.
            - talkback - boolean, defaults to False. Whether the camera supports
                receiving two-way ("talkback") audio from the controller during a
                live view session. When enabled, a "Speaker"
                service is added so that HAP clients know the accessory supports
                it, and :meth:`talkback_audio_received` is called with the
                received audio for each session that negotiates it.
            - global_operating_mode - list of optional characteristics of the
                HomeKit Secure Video 17.99 Camera Global Operating Mode service,
                e.g. ``["ManuallyDisabled", "NightVision"]``. Opts into the service,
                an empty list adds only the required characteristics. With
                ``recording``, the Camera Operating Mode service has a HomeKit
                Camera Active too: the last write to either turns the camera on
                or off, and the other one is set to the same value, so a hub
                that only knows the classic one still works. While streaming is
                not allowed, the camera refuses to set up live streams and ends
                running ones. Unknown names raise ``ValueError``, a value that is
                not a list raises ``TypeError``.
            - recording - ``True``, or a dict whose ``delegate`` is a
                :class:`pyhap.hds_recording.RecordingProducer` such as
                :class:`pyhap.media.recording_delegate.RecordingMediaDelegate`
                that makes the recordings: the camera arms it while the hub has
                recording on and the camera is on, advertises its
                ``prebuffer_ms`` and records with its ``record``. Without a
                delegate, override :meth:`handle_recording_stream`. Observed
                with iOS and tvOS 27: hubs do not upload the HDS recordings of
                a camera that also has ``multi_tier``, they never appear in the
                Home app.
            - start_stream_cmd - string specifying the command to be executed to start
                the stream. The string can contain the keywords, corresponding to the
                video and audio configuration that was negotiated between the camera
                and the client. See the ``start`` method for a full list of parameters.
            - sensors - list of :class:`pyhap.hksv3.SensorConfiguration`, the image
                sensors of the camera. Opts into the HomeKit Secure Video 17.99
                Camera Capabilities and Camera Motion Zones services, and adds
                Contributing Sensors and Motion Enabled to the motion sensor,
                or adds a motion sensor without ``recording``. Each sensor needs
                at least three video encodings. With ``recording``, turning it
                on for a paired camera changes the instance IDs of the recording
                services with the default IID manager.
            - multi_tier - :class:`pyhap.camera_multi_tier.MultiTierConfig`, or a
                dict of its fields. Developer Preview of HomeKit Secure Video
                17.99, experimental. Adds a Camera Multi-Tier RTP Stream
                Management service per sensor, offering the sensor's video
                encodings as H.265 tiers. Needs ``webrtc``, ``sensors``,
                ``global_operating_mode``, ``recording``, ``video`` and
                ``audio``: iOS 27 shows a camera without the classic RTP stream
                management service as not responding. Its services come before
                the RTP stream management services, which then get a new
                ``unique_id``. For a paired camera this changes instance IDs.
                Observed with iOS and tvOS 27: the hub does not upload the
                recordings it gets over HDS from a camera with this service, so
                they do not appear in the Home app. The camera is expected to
                upload its recordings itself.
            - webrtc - :class:`pyhap.camera_webrtc.WebRTCConfig`, or a dict of
                its fields. Developer Preview of HomeKit Secure Video 17.99,
                experimental. Adds a Camera WebRTC Stream Management service per
                sensor, linked to its multi-tier service. Controllers view
                remotely through it: one that streams through the multi-tier
                service does not fall back to the RTP stream management service.
                Needs ``multi_tier`` and the options that it needs.

        :type options: ``dict``
        """
        self.has_srtp = options.get("srtp", False)
        self.support_talkback = options.get("talkback", False)
        self.start_stream_cmd = options.get("start_stream_cmd", FFMPEG_CMD)

        self.stream_address = options["address"]
        try:
            ipaddress.IPv4Address(self.stream_address)
            self.stream_address_isv6 = b"\x00"
        except ValueError:
            self.stream_address_isv6 = b"\x01"
        self.sessions = {}

        super().__init__(*args, **kwargs)

        self.add_preload_service("Microphone")
        if self.support_talkback:
            self.add_preload_service("Speaker").configure_char("Mute", value=False)
        self._streaming_status: list[bytes] = []
        self._management = []
        # All HKSV state defaults are initialized before any setup runs so the
        # setup methods below never follow (and reset) them.
        self._status_active_char = None
        self._recording_service = None
        self._selected_recording_config = None
        # Last selection that decoded, the char may hold a rejected write.
        self._selected_recording_raw = None
        self._recording_defaults = {}
        self._save_pending = False
        self._recording_changed = False
        self._motion_detected_char = None
        # Bumped on stop so a start still binding does not publish its listener.
        self._hds_generation = 0
        self._hds_listener = None
        self._hds_connections = set()
        self._recording_stream = None
        recording = options.get("recording")
        self._recording_media: hds_recording.RecordingProducer | None = (
            recording.get("delegate") if isinstance(recording, dict) else None
        )
        self._recording_media_running = False
        # What the delegate was last armed to; one task reconciles it.
        self._recording_media_armed = False
        self._recording_media_task: asyncio.Task[None] | None = None
        # Set to cut a retry delay short when the wanted state changes.
        self._recording_media_wake = asyncio.Event()
        self._homekit_camera_active_char: Characteristic | None = None
        # The HomeKit Camera Active of the Camera Operating Mode service, kept
        # in step with the global one when both exist.
        self._classic_camera_active_char: Characteristic | None = None
        self._motion_status_active_char = None
        self._global_operating_mode: Service | None = None
        # Its chars by name, they don't change after the setup.
        self._global_operating_mode_chars: dict[str, Characteristic] = {}
        # Persisted HKSV 17.99 chars with their state key, default and the
        # handler a reset calls.
        self._hksv3_defaults: dict[
            Characteristic, tuple[str, Any, Callable[[Any], None]]
        ] = {}
        # The values the handlers last saw, they only hear about changes.
        self._hksv3_seen: dict[Characteristic, Any] = {}
        self._sensors: list[hksv3.SensorConfiguration] = list(
            options.get("sensors") or []
        )
        self._motion_zones_service: Service | None = None
        self._contributing_sensors_char: Characteristic | None = None
        self._motion_enabled_char: Characteristic | None = None
        self._multi_tier: dict[UUID, MultiTierController] = {}
        self._webrtc: dict[UUID, WebRTCController] = {}
        self._stream_status_scheduled = False
        if options.get("multi_tier") is not None or options.get("webrtc") is not None:
            self._setup_stream_controllers(options)
        if options.get("video"):
            self._setup_stream_management(options)
        # Classic HomeKit Secure Video recording (HDS + fragmented MP4).
        if options.get("recording"):
            self._setup_recording_management(options)
            transport = self.get_service("DataStreamTransportManagement")
            if transport is not None:
                for controller in self._stream_controllers():
                    controller.service.add_linked_service(transport)
            if self._multi_tier and self._records_over_hds():
                logger.warning(
                    "%s: hubs with iOS or tvOS 27 do not upload HDS recordings of "
                    "a camera with multi-tier streams, they will not appear in "
                    "the Home app",
                    self.display_name,
                )
        if self._sensors:
            self._setup_camera_capabilities()
        # Last, so the instance IDs of the other services stay the same.
        if options.get("global_operating_mode") is not None:
            self._setup_global_operating_mode(options["global_operating_mode"])
        self._restore_hksv3_state()

    def _records_over_hds(self) -> bool:
        """Whether the camera makes HDS recordings, by delegate or override."""
        return self._recording_media is not None or (
            type(self).handle_recording_stream is not Camera.handle_recording_stream
        )

    @property
    def streaming_status(self) -> bytes | None:
        """For backwards compatibility."""
        # A recording-only camera has no RTP stream management service.
        return self._streaming_status[0] if self._streaming_status else None

    def _setup_stream_management(self, options: dict[str, Any]) -> None:
        """Create stream management."""
        stream_count = options.get("stream_count", 1)
        for stream_idx in range(stream_count):
            self._management.append(self._create_stream_management(stream_idx, options))
            self._streaming_status.append(STREAMING_STATUS["AVAILABLE"])

    def _create_stream_management(
        self, stream_idx: int, options: dict[str, Any]
    ) -> Service:
        """Create a stream management service."""
        # A new unique_id lets a persistent IID manager place the service
        # after the multi-tier ones, which come first.
        unique_id = stream_idx if not self._multi_tier else f"rtp-{stream_idx}"
        management = self.add_preload_service(
            "CameraRTPStreamManagement", unique_id=unique_id, chars=["Active"]
        )
        # HAP-NodeJS exposes an Active (0x0B0) characteristic on every stream
        # management service (value 1 = active); iOS' camera validation expects
        # it, and it is required for Secure Video eligibility.
        management.configure_char("Active", value=1)
        management.configure_char(
            "StreamingStatus",
            getter_callback=lambda: self._get_streaming_status(stream_idx),
        )
        management.configure_char(
            "SupportedRTPConfiguration",
            value=self.get_supported_rtp_config(options.get("srtp", False)),
        )
        management.configure_char(
            "SupportedVideoStreamConfiguration",
            value=self.get_supported_video_stream_config(options["video"]),
        )
        management.configure_char(
            "SupportedAudioStreamConfiguration",
            value=self.get_supported_audio_stream_config(options["audio"]),
        )
        management.configure_char(
            "SelectedRTPStreamConfiguration",
            setter_callback=self.set_selected_stream_configuration,
        )
        setup_endpoints = management.configure_char(
            "SetupEndpoints",
            setter_callback=lambda value: self.set_endpoints(
                value, stream_idx=stream_idx
            ),
        )
        # The answer has the session's SRTP keys, only its controller reads it.
        setup_endpoints.read_back_response = True
        return management

    def _setup_stream_controllers(self, options: dict[str, Any]) -> None:
        """Add the multi-tier and WebRTC controllers of each sensor (17.99)."""
        # Here, not at the top: these modules import this one.
        from pyhap.camera_multi_tier import (  # noqa: PLC0415
            MultiTierConfig,
            MultiTierController,
        )
        from pyhap.camera_webrtc import WebRTCConfig, WebRTCController  # noqa: PLC0415

        if options.get("multi_tier") is None or options.get("webrtc") is None:
            raise ValueError(
                "multi_tier and webrtc need each other, without WebRTC "
                "controllers cannot view remotely"
            )
        if (
            not options.get("video")
            or not options.get("audio")
            or options.get("stream_count", 1) < 1
        ):
            raise ValueError(
                "multi_tier and webrtc need the video and audio options, iOS 27 "
                "shows a camera without the classic RTP stream management "
                "service as not responding"
            )
        if (
            not self._sensors
            or options.get("global_operating_mode") is None
            or not options.get("recording")
        ):
            raise ValueError(
                "multi_tier and webrtc need the sensors, global_operating_mode "
                "and recording options"
            )
        multi_tier = MultiTierConfig.from_option(options["multi_tier"])
        webrtc = WebRTCConfig.from_option(options["webrtc"])
        for sensor in self._sensors:
            for warning in hksv3.check_video_tiers(sensor):
                logger.warning("Sensor %s: %s", sensor.sensor_uuid, warning)
            controller = MultiTierController(self, sensor, multi_tier)
            self._multi_tier[sensor.sensor_uuid] = controller
            self._webrtc[sensor.sensor_uuid] = WebRTCController(
                self, controller, webrtc
            )
        for kind, controllers in (
            ("multi_tier", self._multi_tier),
            ("webrtc", self._webrtc),
        ):
            for sensor_uuid, controller in controllers.items():
                self._persist_hksv3_char(
                    controller.streaming_enabled,
                    f"{kind}.{sensor_uuid}.streaming_enabled",
                    self._handle_stream_enabled,
                )

    def _stream_controllers(
        self,
    ) -> "list[MultiTierController | WebRTCController]":
        return [*self._multi_tier.values(), *self._webrtc.values()]

    @property
    def multi_tier_controllers(self) -> "Mapping[UUID, MultiTierController]":
        """The multi-tier RTP controller of each sensor (17.99), by sensor UUID."""
        return MappingProxyType(self._multi_tier)

    @property
    def webrtc_controllers(self) -> "Mapping[UUID, WebRTCController]":
        """The WebRTC controller of each sensor (17.99), by sensor UUID."""
        return MappingProxyType(self._webrtc)

    def _handle_stream_enabled(self, value: Any) -> None:
        self._schedule_stream_status()

    def _schedule_stream_status(self) -> None:
        """Update the stream controllers once the current write is done.

        A controller writes several of the values they depend on in one
        request, e.g. HomeKit Camera Active and Streaming Enabled.
        """
        if self._stream_status_scheduled or not self._multi_tier:
            return
        self._stream_status_scheduled = True
        self.driver.loop.call_soon(self._update_stream_status)

    def _update_stream_status(self) -> None:
        """Let each stream controller derive whether it may stream now.

        Ends the sessions that may no longer stream.
        """
        self._stream_status_scheduled = False
        for controller in self._stream_controllers():
            controller.update_status()

    def _stream_address_for(
        self, client_addr: tuple[str, int] | None, is_ipv6: bool
    ) -> str | None:
        """The address to stream from to a controller, in the given IP version.

        The accessory's address on the controller's connection, else the
        ``address`` option if it has that version.
        """
        address = self.driver._local_address(client_addr, is_ipv6)
        if address is not None:
            return address
        if (self.stream_address_isv6 == b"\x01") == is_ipv6:
            return str(self.stream_address)
        return None

    def client_disconnected(self, client_addr: tuple[str, int]) -> None:
        """End the multi-tier and WebRTC sessions of the connection."""
        for controller in self._stream_controllers():
            controller.client_disconnected(client_addr)

    def _default_recording_configs(
        self, options: dict[str, Any]
    ) -> tuple[
        hksv_recording.SupportedRecordingConfiguration,
        list[hksv_recording.VideoCodecConfiguration],
        list[hksv_recording.AudioCodecConfiguration],
    ]:
        """Derive supported recording configurations from the streaming options.

        A widely-compatible default: H.264 (Baseline/Main/High, levels 3.1/3.2/
        4.0) at the streaming resolutions, AAC-LC audio. Accessories can override
        this to advertise their own recording capabilities.
        """
        video_opts = options.get("video") or {}
        resolutions = video_opts.get("resolutions") or [[1920, 1080, 30]]
        video = [
            hksv_recording.VideoCodecConfiguration(
                codec_type=hksv_recording.VideoCodecType.H264,
                profile=[
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"][0],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"][0],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["HIGH"][0],
                ],
                level=[
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"][0],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"][0],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"][0],
                ],
                bitrate_kbps=2000,
                iframe_interval_ms=4000,
                attributes=[
                    hksv_recording.VideoAttributes(width, height, fps)
                    for width, height, fps in resolutions
                ],
            )
        ]
        audio = [hksv_recording.AudioCodecConfiguration()]
        general = hksv_recording.SupportedRecordingConfiguration()
        if self._recording_media is not None:
            general.prebuffer_length_ms = int(self._recording_media.prebuffer_ms)
        return general, video, audio

    def _setup_recording_management(self, options: dict[str, Any]) -> None:
        """Create the Camera Recording Management service (classic HKSV).

        Advertises the supported general/video/audio recording configurations
        and accepts the controller's selection. Recording stays inactive
        (Active=0) unless the ``recording`` option enables it; the fragment
        data flow over HDS is wired by the recording transport.
        """
        # HKSV requires the Camera Operating Mode service (0000021A); the
        # controller validates its presence before enabling recording.
        operating_mode = self.add_preload_service(
            "CameraOperatingMode", chars=["PeriodicSnapshotsActive"]
        )
        operating_mode.configure_char("EventSnapshotsActive", value=1)
        homekit_camera_active = operating_mode.configure_char(
            "HomeKitCameraActive", value=1
        )
        homekit_camera_active.setter_callback = self._handle_homekit_camera_active
        self._homekit_camera_active_char = homekit_camera_active
        self._classic_camera_active_char = homekit_camera_active
        operating_mode.configure_char("PeriodicSnapshotsActive", value=1)

        # HKSV records on an event trigger; the camera owns a motion sensor and
        # links it (and the data stream transport) to the recording management.
        motion = self.add_preload_service(
            "MotionSensor",
            chars=["StatusActive", *(HKSV3_MOTION_CHARS if self._sensors else ())],
        )
        self._motion_detected_char = motion.configure_char(
            "MotionDetected", value=False
        )
        # HAP-NodeJS marks the HKSV event-trigger sensor as active; iOS expects it.
        # A restored HomeKitCameraActive bypasses the setter, so reads derive it.
        self._motion_status_active_char = motion.configure_char(
            "StatusActive", value=True, getter_callback=self._homekit_camera_on
        )

        general, video, audio = self._default_recording_configs(options)
        service = self.add_preload_service(
            "CameraRecordingManagement", chars=["RecordingAudioActive"]
        )
        service.add_linked_service(motion)
        service.configure_char("Active", value=0)
        service.configure_char(
            "RecordingAudioActive", value=1 if options.get("recording_audio") else 0
        )
        service.configure_char(
            "SupportedCameraRecordingConfiguration",
            value=to_base64_str(general.encode()),
        )
        service.configure_char(
            "SupportedVideoRecordingConfiguration",
            value=to_base64_str(hksv_recording.encode_supported_video(video)),
        )
        service.configure_char(
            "SupportedAudioRecordingConfiguration",
            value=to_base64_str(hksv_recording.encode_supported_audio(audio)),
        )
        service.configure_char(
            "SelectedCameraRecordingConfiguration",
            setter_callback=self.set_selected_recording_configuration,
        )
        self._recording_service = service
        for service_name, char_name in PERSISTED_RECORDING_CHARS:
            persisted_service = self.get_service(service_name)
            assert persisted_service is not None  # noqa: S101
            char = persisted_service.get_characteristic(char_name)
            self._recording_defaults[char] = char.value
        self._restore_recording_state()
        self._setup_data_stream_transport()

    def _setup_camera_capabilities(self) -> None:
        """Advertise the image sensors and accept motion zones (HKSV 17.99)."""
        for sensor in self._sensors:
            if len(sensor.video_streams) < HKSV3_MIN_SENSOR_STREAMS:
                logger.warning(
                    "%s: sensor %s has %d video encodings, HKSV needs at least %d",
                    self.display_name,
                    sensor.sensor_uuid,
                    len(sensor.video_streams),
                    HKSV3_MIN_SENSOR_STREAMS,
                )
        capabilities = self.add_preload_service("CameraCapabilities")
        capabilities.configure_char("Version", value=hksv3.SERVICE_VERSION)
        capabilities.configure_char(
            "CameraCapabilities",
            value=to_base64_str(hksv3.CameraCapabilities(self._sensors).encode()),
        )
        zones = self.add_preload_service("CameraMotionZones")
        zones.configure_char("Version", value=hksv3.SERVICE_VERSION)
        self._motion_zones_service = zones

        motion = self.get_service("MotionSensor")
        if motion is None:
            motion = self.add_preload_service("MotionSensor", chars=HKSV3_MOTION_CHARS)
            self._motion_detected_char = motion.get_characteristic("MotionDetected")
        self._contributing_sensors_char = motion.get_characteristic(
            "ContributingSensors"
        )
        self._motion_enabled_char = motion.configure_char("MotionEnabled", value=True)
        for char, key, handler in (
            (
                zones.get_characteristic("Active"),
                "motion_zones_active",
                self._handle_motion_zones,
            ),
            (
                zones.get_characteristic("CameraZones"),
                "camera_zones",
                self._handle_motion_zones,
            ),
            (self._motion_enabled_char, "motion_enabled", self._handle_motion_enabled),
        ):
            self._persist_hksv3_char(char, key, handler)
        # Check the zones before they count as written.
        zones.get_characteristic("CameraZones").setter_callback = self._set_camera_zones

    @property
    def camera_zones(self) -> hksv3.CameraZones | None:
        """The motion zones the controller wrote, if any."""
        if self._motion_zones_service is None:
            return None
        value = self._motion_zones_service.get_characteristic("CameraZones").value
        if not value:
            return None
        try:
            return hksv3.CameraZones.decode(base64_to_bytes(value))
        # A damaged state file.
        except ValueError:
            return None

    @property
    def motion_zones_active(self) -> bool:
        """Whether only motion inside :attr:`camera_zones` should count."""
        if self._motion_zones_service is None:
            return False
        return bool(self._motion_zones_service.get_characteristic("Active").value)

    @property
    def motion_enabled(self) -> bool:
        """Whether motion detection is on, cameras without the setting always are."""
        return self._motion_enabled_char is None or bool(
            self._motion_enabled_char.value
        )

    def _set_camera_zones(self, value: str) -> None:
        assert self._motion_zones_service is not None  # noqa: S101
        char = self._motion_zones_service.get_characteristic("CameraZones")
        try:
            if value:
                hksv3.CameraZones.decode(base64_to_bytes(value))
        except ValueError:
            # Fail the write and keep the zones the camera had.
            char.value = self._hksv3_seen[char]
            raise
        self._hksv3_written(char, value)

    def _handle_motion_zones(self, value: Any) -> None:
        self.motion_zones_changed()

    def motion_zones_changed(self) -> None:
        """React to the controller changing :attr:`camera_zones` or turning
        them on or off. Override.

        Only changes call it, not a controller writing the same value again.
        """

    def _handle_motion_enabled(self, value: Any) -> None:
        if not value:
            self.set_motion_detected(False)
        self.motion_enabled_changed(bool(value))

    def motion_enabled_changed(self, enabled: bool) -> None:
        """React to the user turning motion detection on or off. Override.

        While it is off, :meth:`set_motion_detected` reports no motion. Only
        changes call it, not a controller writing the same value again.
        """

    def _setup_data_stream_transport(self) -> None:
        """Create the Data Stream Transport Management service used by HDS.

        SetupDataStreamTransport needs the HAP session shared secret, so its
        setter receives the sender's client address to derive keys from it.
        """
        transport = self.add_preload_service("DataStreamTransportManagement")
        transport.configure_char("Version", value="1.0")
        transport.configure_char(
            "SupportedDataStreamTransportConfiguration",
            value=to_base64_str(self._supported_data_stream_transport()),
        )
        transport.configure_char(
            "SetupDataStreamTransport",
            setter_callback=self.set_data_stream_transport,
        )
        # HKSV requires the recording management service to link the data stream
        # transport it records over; without the link the controller rejects
        # recording during validation, before writing any configuration.
        assert self._recording_service is not None  # noqa: S101
        self._recording_service.add_linked_service(transport)

    @staticmethod
    def _supported_data_stream_transport() -> Any:
        # A single supported transport configuration: TCP.
        transport_configuration = tlv.encode(b"\x01", hds.TRANSPORT_TYPE_TCP)
        return tlv.encode(b"\x01", transport_configuration)

    async def _ensure_hds_listener(self) -> None:
        if self._hds_listener is None:
            generation = self._hds_generation
            listener = hds_server.HDSListener()
            listener.on_connection = self._on_hds_connection
            # Bind where HAP listens: the controller opens HDS to that address.
            await listener.start(self.driver.listen_address())
            # Publish only a bound listener, SetupDataStreamTransport reads its
            # port. A concurrent start or a stop may have come in meanwhile.
            if self._hds_listener is None and generation == self._hds_generation:
                self._hds_listener = listener
            else:
                await listener.stop()

    def _on_hds_connection(self, connection: hds_server.HDSConnection) -> None:
        self._hds_connections.add(connection)
        connection.add_close_callback(self._hds_connections.discard)
        hds_recording.RecordingStreamManager(
            connection, self._recording_delegate, open_gate=self._recording_open_gate
        )

    def set_data_stream_transport(
        self, value: str, sender_client_addr: Any = None
    ) -> str:
        """Handle a write to SetupDataStreamTransport (HDS session setup).

        SetupDataStreamTransport is a write-response ('wr') characteristic: the
        controller writes with ``r: true`` and reads the encoded setup response
        (TCP listening port + accessory key salt) straight back from the write
        response. That means the value MUST be RETURNED from the setter so the
        HAP layer includes it in the write response — setting it on the
        characteristic is not delivered to the controller and leaves it without
        the port, so it can never open the HDS connection.
        """
        request = hds.SetupRequest.decode(base64_to_bytes(value))
        listener = self._hds_listener
        accessory_salt = None
        if listener is not None:
            accessory_salt = listener.register_session_transport(
                functools.partial(self.driver.derive_session_key, sender_client_addr),
                request.controller_key_salt,
            )
        if listener is None or accessory_salt is None:
            response = hds.encode_setup_response(
                0, b"", status=hds.SETUP_STATUS_GENERIC_ERROR
            )
        else:
            response = hds.encode_setup_response(listener.port, accessory_salt)
        return to_base64_str(response)

    def _recording_delegate(
        self, stream_id: int
    ) -> AsyncGenerator[hds_recording.RecordingPacket, None]:
        """Return the recording generator for ``stream_id``.

        Override ``handle_recording_stream`` to produce the fragmented MP4
        packets. Its generator is handed over as is, so the stream's
        ``aclose()`` runs its cleanup inside the tracked recording task.
        """
        return self.handle_recording_stream(stream_id)

    async def handle_recording_stream(
        self, stream_id: int
    ) -> AsyncGenerator[hds_recording.RecordingPacket, None]:
        """Produce the fragmented-MP4 recording for ``stream_id``.

        Override to yield :class:`pyhap.hds_recording.RecordingPacket` objects,
        the first being the MP4 initialization segment. The default records
        with the ``delegate`` of the ``recording`` option, without one it
        yields nothing (no recording).
        """
        delegate = self._recording_media
        selected = self._selected_recording_config
        if delegate is None or selected is None:
            return
        packets = delegate.record(selected, self.recording_audio_active)
        try:
            async for packet in packets:
                yield packet
        finally:
            await packets.aclose()

    @property
    def selected_recording_configuration(
        self,
    ) -> hksv_recording.SelectedRecordingConfiguration | None:
        """The controller's selected recording configuration, if any."""
        return self._selected_recording_config

    @property
    def recording_active(self) -> bool:
        """Whether the controller turned recording on."""
        if self._recording_service is None:
            return False
        return bool(self._recording_service.get_characteristic("Active").value)

    @property
    def recording_audio_active(self) -> bool:
        """Whether recordings include audio."""
        if self._recording_service is None:
            return False
        return bool(
            self._recording_service.get_characteristic("RecordingAudioActive").value
        )

    def _setup_global_operating_mode(self, chars: Sequence[str]) -> None:
        """Create the Camera Global Operating Mode service (HKSV 17.99)."""
        optional = self.driver.loader.serv_types["CameraGlobalOperatingMode"][
            "OptionalCharacteristics"
        ]
        if unknown := [name for name in chars if name not in optional]:
            raise ValueError(
                "Not an optional Camera Global Operating Mode characteristic: "
                f"{', '.join(unknown)}"
            )
        service = self.add_preload_service("CameraGlobalOperatingMode", chars=chars)
        for char in service.characteristics:
            name = char.display_name
            # Everything starts on except Night Vision and Manually Disabled.
            if name not in ("NightVision", "ManuallyDisabled"):
                char.set_value(1, should_notify=False)
            if name not in PERSISTED_GLOBAL_OPERATING_MODE_CHARS:
                continue
            if name == "HomeKitCameraActive":
                handler = self._handle_global_camera_active
                # The last write to either HomeKit Camera Active decides.
                if self._classic_camera_active_char is not None:
                    self._classic_camera_active_char.setter_callback = (
                        self._handle_classic_camera_active
                    )
                self._homekit_camera_active_char = char
            else:
                handler = functools.partial(self._handle_global_operating_mode, name)
            self._persist_hksv3_char(char, f"global_operating_mode.{name}", handler)
        self._global_operating_mode = service
        self._global_operating_mode_chars = {
            char.display_name: char
            for char in service.characteristics
            if char.display_name is not None
        }

    @property
    def streaming_allowed(self) -> bool:
        """Whether the global operating mode allows streaming.

        True without the ``global_operating_mode`` option. While it is False,
        the camera refuses to set up live streams.
        """
        service = self._global_operating_mode
        if service is None:
            return True
        manually_disabled = self._global_operating_mode_char("ManuallyDisabled")
        return (
            self.homekit_camera_active
            and bool(service.get_characteristic("StreamingEnabled").value)
            and not (manually_disabled is not None and manually_disabled.value)
        )

    def set_manually_disabled(self, disabled: bool) -> None:
        """Report the camera being turned off on the device itself.

        Needs ``ManuallyDisabled`` in the ``global_operating_mode`` option.
        """
        char = self._global_operating_mode_char("ManuallyDisabled")
        if char is not None:
            char.set_value(bool(disabled))
            self._end_streams_if_not_allowed()
            self._update_stream_status()

    def set_third_party_camera_active(self, active: bool) -> None:
        """Report whether the camera is on for apps outside HomeKit.

        Needs ``ThirdPartyCameraActive`` in the ``global_operating_mode`` option.
        """
        char = self._global_operating_mode_char("ThirdPartyCameraActive")
        if char is not None:
            char.set_value(bool(active))

    def _global_operating_mode_char(self, name: str) -> Characteristic | None:
        return self._global_operating_mode_chars.get(name)

    def _handle_global_operating_mode(self, name: str, value: Any) -> None:
        if name == "StreamingEnabled":
            self._end_streams_if_not_allowed()
        self._schedule_stream_status()
        self.global_operating_mode_changed(name, value)

    def _end_streams_if_not_allowed(self) -> None:
        """End the live streams once streaming is turned off."""
        if self.streaming_allowed:
            return
        for session_id in list(self.sessions):
            self.driver.add_job(self._end_stream_not_allowed, session_id)

    async def _end_stream_not_allowed(self, session_id: UUID) -> None:
        stream_idx = await self._end_stream(session_id)
        if stream_idx is not None:
            logger.info("[%s] Ending the stream, streaming is turned off", session_id)
            self.set_streaming_available(stream_idx)

    def global_operating_mode_changed(self, name: str, value: Any) -> None:
        """React to the controller changing a global operating mode setting.

        ``name`` is the characteristic, e.g. ``"NightVision"``. HomeKit Camera
        Active changes call :meth:`homekit_camera_active_changed` instead. Only
        changes call it, not a controller writing the same value again.
        Override.
        """

    @property
    def homekit_camera_active(self) -> bool:
        """Whether the camera is on in the Home app.

        Only cameras with recording or the global operating mode have the
        setting, all others are on.
        """
        char = self._homekit_camera_active_char
        return char is None or bool(char.value)

    def set_selected_recording_configuration(self, value: str) -> None:
        """Handle a write to Selected Camera Recording Configuration (spec HKSV)."""
        self._selected_recording_config = (
            hksv_recording.SelectedRecordingConfiguration.decode(base64_to_bytes(value))
        )
        self._selected_recording_raw = value
        self.recording_configuration_selected(self._selected_recording_config)

    def recording_configuration_selected(self, configuration: Any) -> None:
        """React to the controller selecting a recording configuration. Override."""

    def recording_state_changed(self) -> None:
        """React to a change of the recording state, once per burst. Override.

        Persistence does not use the characteristic setters, so subclasses may
        set their own and should override this hook rather than the setters.
        """

    def publish(
        self,
        value: Any,
        sender: Service | Characteristic,
        sender_client_addr: Any = None,
        immediate: bool = False,
    ) -> None:
        """Forward the update and store recording and HKSV 17.99 state changes."""
        super().publish(value, sender, sender_client_addr, immediate)
        if sender in self._hksv3_defaults:
            # Local changes too, so a later write of the same value is no change.
            self._hksv3_seen[sender] = value
        # Without an aid there is no entry to store under.
        if self.aid is None:
            return
        if sender in self._hksv3_defaults:
            key = self._hksv3_defaults[sender][0]
            state = self.driver.accessory_state(self.aid)
            kept = state.get(HKSV3_STATE)
            # A damaged state file must not fail the write.
            if not isinstance(kept, dict):
                kept = state[HKSV3_STATE] = {}
            kept[key] = value
            self._schedule_save("camera")
        if sender not in self._recording_defaults:
            return
        # Store now so a camera rebuilt before the save restores this change.
        stored = {char.display_name: char.value for char in self._recording_defaults}
        stored[SELECTED_RECORDING_CHAR] = self._selected_recording_raw
        stored["supported_hash"] = self._supported_recording_hash()
        self.driver.accessory_state(self.aid)["recording"] = stored
        self._schedule_recording_save()

    def _schedule_recording_save(self) -> None:
        self._recording_changed = True
        self._schedule_save("recording")

    def _schedule_save(self, what: str) -> None:
        if self._save_pending:
            return
        # One disk write per hub burst.
        self._save_pending = True
        try:
            self.driver.add_job(self._save_state)
        except RuntimeError:
            self._save_pending = False
            self._recording_changed = False
            logger.warning("Could not schedule saving the %s state", what)

    def _supported_recording_hash(self) -> str:
        digest = hashlib.sha256()
        assert self._recording_service is not None  # noqa: S101
        for name in SUPPORTED_RECORDING_CHARS:
            char = self._recording_service.get_characteristic(name)
            digest.update(char.value.encode())
        return digest.hexdigest()

    @callback
    def _save_state(self) -> None:
        self._save_pending = False
        recording_changed, self._recording_changed = self._recording_changed, False
        self.driver.async_persist()
        if recording_changed:
            self._arm_recording_media()
            self.recording_state_changed()

    def _persist_hksv3_char(
        self, char: Characteristic, key: str, handler: Callable[[Any], None]
    ) -> None:
        """Keep the value of ``char`` across restarts, reset it for a new home.

        A controller write or a reset that changes the value calls ``handler``
        with the new value, a reset once per handler. The constructor restores
        the stored values afterwards.
        """
        self._hksv3_defaults[char] = (key, char.value, handler)
        char.setter_callback = functools.partial(self._hksv3_written, char)

    def _hksv3_written(self, char: Characteristic, value: Any) -> None:
        """Call the handler of ``char`` if the write changed its value."""
        if self._hksv3_seen.get(char) == value:
            return
        self._hksv3_seen[char] = value
        self._hksv3_defaults[char][2](value)

    def _restore_hksv3_state(self) -> None:
        if self.aid is not None:
            self._load_hksv3_state(self.aid)
        self._hksv3_seen = {char: char.value for char in self._hksv3_defaults}
        # Restored values bypass the handlers.
        self._update_stream_status()

    def _load_hksv3_state(self, aid: int) -> None:
        stored = self.driver.accessory_state(aid).get(HKSV3_STATE)
        if not isinstance(stored, dict):
            return
        for char, (key, _, _) in self._hksv3_defaults.items():
            if key not in stored:
                continue
            try:
                char.set_value(stored[key], should_notify=False)
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring stored %s of aid %s", key, aid)

    def restore_accessory_state(self) -> None:
        """Restore the stored recording and HKSV 17.99 state."""
        self._restore_recording_state()
        self._restore_hksv3_state()

    def _restore_recording_state(self) -> None:
        # Private so construction does not run a subclass override early.
        # No aid yet, e.g. before Bridge.add_accessory. The driver restores later.
        if self.aid is None:
            return
        stored = self.driver.accessory_state(self.aid).get("recording")
        if not self._recording_defaults or not isinstance(stored, dict):
            return
        for char in self._recording_defaults:
            name = char.display_name
            if name == SELECTED_RECORDING_CHAR or name not in stored:
                continue
            try:
                char.set_value(stored[name], should_notify=False)
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring stored %s of aid %s", name, self.aid)
        # The hub does not select again after a restart, so keep the selection
        # unless the supported configuration changed.
        selected = stored.get(SELECTED_RECORDING_CHAR)
        stale = stored.get("supported_hash") != self._supported_recording_hash()
        if not selected or stale:
            return
        try:
            config = hksv_recording.SelectedRecordingConfiguration.decode(
                base64_to_bytes(selected)
            )
        # A damaged state file can fail decoding in many ways, fall back to
        # the defaults instead of failing the accessory.
        except Exception:  # noqa: BLE001
            logger.warning("Ignoring stored recording selection of aid %s", self.aid)
            return
        assert self._recording_service is not None  # noqa: S101
        self._recording_service.get_characteristic(SELECTED_RECORDING_CHAR).set_value(
            selected, should_notify=False
        )
        self._selected_recording_config = config
        self._selected_recording_raw = selected

    def reset_accessory_state(self) -> None:
        """Return the recording and HKSV 17.99 state to its defaults.

        Once all state is reset, the handlers of the HKSV 17.99 values that
        changed are called.
        """
        handlers: list[tuple[Callable[[Any], None], Any]] = []
        for char, (_, value, handler) in self._hksv3_defaults.items():
            changed = char.value != value
            char.set_value(value, should_notify=False)
            self._hksv3_seen[char] = value
            if changed and all(handler != known for known, _ in handlers):
                handlers.append((handler, value))
        if self._hksv3_defaults and self.aid is not None:
            self.driver.accessory_state(self.aid).pop(HKSV3_STATE, None)
            self._schedule_save("camera")
        if self._recording_defaults:
            for char, value in self._recording_defaults.items():
                char.set_value(value, should_notify=False)
            self._selected_recording_config = None
            self._selected_recording_raw = None
            if self.aid is not None:
                self.driver.accessory_state(self.aid).pop("recording", None)
            self._schedule_recording_save()
        for handler, value in handlers:
            handler(value)
        self._update_stream_status()

    def set_motion_detected(
        self, detected: bool, sensors: Iterable[UUID] | None = None
    ) -> None:
        """Fire (``True``) or clear (``False``) the recording motion trigger.

        This is the entry point an integration calls when its own motion
        detector changes state: it drives the HKSV event-trigger MotionSensor,
        which is what makes the Home Hub start (and later finish) a recording.

        With ``sensors`` the camera names the sensors that saw the motion,
        by default or when empty the first one. Ignored without the
        ``sensors`` option.
        """
        detected = bool(detected) and self.motion_enabled
        if self._contributing_sensors_char is not None:
            if not detected:
                sensors = ()
            elif not (sensors := tuple(sensors or ())):
                sensors = (self._sensors[0].sensor_uuid,)
            else:
                configured = {sensor.sensor_uuid for sensor in self._sensors}
                if unknown := [uuid for uuid in sensors if uuid not in configured]:
                    logger.warning(
                        "%s: motion reported for sensors that are not configured: %s",
                        self.display_name,
                        ", ".join(map(str, unknown)),
                    )
            self._contributing_sensors_char.set_value(
                to_base64_str(hksv3.encode_contributing_sensors(sensors))
            )
        if self._motion_detected_char is not None:
            self._motion_detected_char.set_value(detected)

    def _recording_open_gate(
        self, manager: hds_recording.RecordingStreamManager
    ) -> hds_recording.RecordingReason | None:
        # One stream per camera across all HDS connections (hubs), like HAP-NodeJS.
        # A stopping stream frees the slot while its delegate still cleans up.
        reasons = hds_recording.RecordingReason
        if not (self.recording_active and self.homekit_camera_active):
            return reasons.NOT_ALLOWED
        if self._recording_stream is not None and self._recording_stream.streaming:
            return reasons.BUSY
        if self._selected_recording_config is None:
            return reasons.INVALID_CONFIGURATION
        self._recording_stream = manager
        return None

    async def _start_stream(self, objs: dict[bytes, Any], reconfigure: bool) -> None:
        """Start or reconfigure video streaming for the given session.

        Schedules ``self.start_stream`` or ``self.reconfigure``.

        No support for reconfigure currently.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration
        :type objs: ``dict``

        :param reconfigure: Whether the stream should be reconfigured instead of
            started.
        :type reconfigure: bool
        """
        video_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES["VIDEO"])
        audio_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES["AUDIO"])

        opts = {}

        if video_tlv:
            video_objs = tlv.decode(video_tlv)

            video_codec_params = video_objs.get(VIDEO_TYPES["CODEC_PARAM"])
            if video_codec_params:
                video_codec_param_objs = tlv.decode(video_codec_params)
                opts["v_profile_id"] = video_codec_param_objs[
                    VIDEO_CODEC_PARAM_TYPES["PROFILE_ID"]
                ]
                opts["v_level"] = video_codec_param_objs[
                    VIDEO_CODEC_PARAM_TYPES["LEVEL"]
                ]

            video_attrs = video_objs.get(VIDEO_TYPES["ATTRIBUTES"])
            if video_attrs:
                video_attr_objs = tlv.decode(video_attrs)
                opts["width"] = struct.unpack(
                    "<H", video_attr_objs[VIDEO_ATTRIBUTES_TYPES["IMAGE_WIDTH"]]
                )[0]
                opts["height"] = struct.unpack(
                    "<H", video_attr_objs[VIDEO_ATTRIBUTES_TYPES["IMAGE_HEIGHT"]]
                )[0]
                opts["fps"] = struct.unpack(
                    "<B", video_attr_objs[VIDEO_ATTRIBUTES_TYPES["FRAME_RATE"]]
                )[0]

            video_rtp_param = video_objs.get(VIDEO_TYPES["RTP_PARAM"])
            if video_rtp_param:
                video_rtp_param_objs = tlv.decode(video_rtp_param)
                if RTP_PARAM_TYPES["SYNCHRONIZATION_SOURCE"] in video_rtp_param_objs:
                    ssrc = video_rtp_param_objs.get(
                        RTP_PARAM_TYPES["SYNCHRONIZATION_SOURCE"]
                    )
                    assert ssrc is not None  # noqa: S101
                    opts["v_ssrc"] = struct.unpack("<I", ssrc)[0]
                if RTP_PARAM_TYPES["PAYLOAD_TYPE"] in video_rtp_param_objs:
                    opts["v_payload_type"] = video_rtp_param_objs.get(
                        RTP_PARAM_TYPES["PAYLOAD_TYPE"]
                    )
                if RTP_PARAM_TYPES["MAX_BIT_RATE"] in video_rtp_param_objs:
                    max_bit_rate = video_rtp_param_objs.get(
                        RTP_PARAM_TYPES["MAX_BIT_RATE"]
                    )
                    assert max_bit_rate is not None  # noqa: S101
                    opts["v_max_bitrate"] = struct.unpack("<H", max_bit_rate)[0]
                if RTP_PARAM_TYPES["RTCP_SEND_INTERVAL"] in video_rtp_param_objs:
                    rtcp_interval = video_rtp_param_objs.get(
                        RTP_PARAM_TYPES["RTCP_SEND_INTERVAL"]
                    )
                    assert rtcp_interval is not None  # noqa: S101
                    opts["v_rtcp_interval"] = struct.unpack("<f", rtcp_interval)[0]
                if RTP_PARAM_TYPES["MAX_MTU"] in video_rtp_param_objs:
                    opts["v_max_mtu"] = video_rtp_param_objs.get(
                        RTP_PARAM_TYPES["MAX_MTU"]
                    )

        if audio_tlv:
            audio_objs = tlv.decode(audio_tlv)

            opts["a_codec"] = audio_objs[AUDIO_TYPES["CODEC"]]
            audio_codec_param_objs = tlv.decode(audio_objs[AUDIO_TYPES["CODEC_PARAM"]])
            audio_rtp_param_objs = tlv.decode(audio_objs[AUDIO_TYPES["RTP_PARAM"]])
            opts["a_comfort_noise"] = audio_objs[AUDIO_TYPES["COMFORT_NOISE"]]

            opts["a_channel"] = audio_codec_param_objs[
                AUDIO_CODEC_PARAM_TYPES["CHANNEL"]
            ][0]
            opts["a_bitrate"] = struct.unpack(
                "?", audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES["BIT_RATE"]]
            )[0]
            opts["a_sample_rate"] = 8 * (
                1 + audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES["SAMPLE_RATE"]][0]
            )
            opts["a_packet_time"] = struct.unpack(
                "<B", audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES["PACKET_TIME"]]
            )[0]

            opts["a_ssrc"] = struct.unpack(
                "<I", audio_rtp_param_objs[RTP_PARAM_TYPES["SYNCHRONIZATION_SOURCE"]]
            )[0]
            opts["a_payload_type"] = audio_rtp_param_objs[
                RTP_PARAM_TYPES["PAYLOAD_TYPE"]
            ]
            opts["a_max_bitrate"] = struct.unpack(
                "<H", audio_rtp_param_objs[RTP_PARAM_TYPES["MAX_BIT_RATE"]]
            )[0]
            opts["a_rtcp_interval"] = struct.unpack(
                "<f", audio_rtp_param_objs[RTP_PARAM_TYPES["RTCP_SEND_INTERVAL"]]
            )[0]
            opts["a_comfort_payload_type"] = audio_rtp_param_objs[
                RTP_PARAM_TYPES["COMFORT_NOISE_PAYLOAD_TYPE"]
            ]

        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES["SESSION"]])
        session_id = UUID(bytes=session_objs[SETUP_TYPES["SESSION_ID"]])
        session_info = self.sessions[session_id]
        stream_idx = session_info["stream_idx"]

        opts.update(session_info)
        # Turned off after SetupEndpoints.
        refused = not reconfigure and not self.streaming_allowed
        if refused:
            logger.info("[%s] Streaming is turned off, refusing it", session_id)
            success = False
        elif reconfigure:
            success = await self.reconfigure_stream(session_info, opts)
        else:
            success = await self.start_stream(session_info, opts)

        if success:
            self._streaming_status[stream_idx] = STREAMING_STATUS["STREAMING"]
        else:
            if not refused:
                logger.error(
                    "[%s] Failed to start/reconfigure stream, deleting session.",
                    session_id,
                )
            self._close_talkback(session_info)
            del self.sessions[session_id]
            self._streaming_status[stream_idx] = STREAMING_STATUS["AVAILABLE"]

    def _get_streaming_status(self, stream_idx: int) -> Any:
        """Get the streaming status in TLV format.

        Called when iOS reads the StreaminStatus ``Characteristic``.
        """
        return tlv.encode(b"\x01", self._streaming_status[stream_idx], to_base64=True)

    def _homekit_camera_on(self) -> bool:
        assert self._homekit_camera_active_char is not None  # noqa: S101
        return bool(self._homekit_camera_active_char.value)

    def _handle_classic_camera_active(self, value: Any) -> None:
        # A hub that only knows the Camera Operating Mode service.
        char = self._homekit_camera_active_char
        assert char is not None  # noqa: S101
        if char.value != value:
            char.set_value(value)
        self._handle_homekit_camera_active(value)

    def _handle_global_camera_active(self, value: Any) -> None:
        char = self._classic_camera_active_char
        if char is not None and char.value != value:
            char.set_value(value)
        self._handle_homekit_camera_active(value)

    def _handle_homekit_camera_active(self, value: Any) -> None:
        # A camera turned off must not record, and the hub must not treat its
        # motion sensor as an active trigger.
        active = bool(value)
        if self._motion_status_active_char is not None:
            # Always notify: a restored value may have bypassed the stored one,
            # so set_value alone could see no change.
            self._motion_status_active_char.set_value(active, should_notify=False)
            self._motion_status_active_char.notify()
        if not active and self._recording_stream is not None:
            self._recording_stream.close(hds_recording.RecordingReason.NOT_ALLOWED)
        self._arm_recording_media()
        self._end_streams_if_not_allowed()
        self._schedule_stream_status()
        self.homekit_camera_active_changed(active)

    def homekit_camera_active_changed(self, active: bool) -> None:
        """React to the user turning the camera on or off in the Home app.

        Override instead of replacing the characteristic's setter. A running
        recording is already closed when the camera is turned off. Like other
        setters, every controller write calls this, setting the value locally
        does not. With the ``global_operating_mode`` option, writes to its
        HomeKit Camera Active only call it on changes, not when a controller
        writes the same value again.
        """

    async def _stop_stream(self, objs: dict[bytes, Any]) -> None:
        """Stop the stream for the specified session.

        Schedules ``self.stop_stream``.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration value.
        :param objs: ``dict``
        """
        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES["SESSION"]])
        session_id = UUID(bytes=session_objs[SETUP_TYPES["SESSION_ID"]])

        session_info = self.sessions.get(session_id)
        if not session_info:
            logger.error(
                "Requested to stop stream for session %s, but no "
                "such session was found",
                session_id,
            )
            return

        await self._end_stream(session_id)

    async def _end_stream(self, session_id: UUID) -> int | None:
        """Stop the stream of ``session_id`` and return its stream index."""
        session_info = self.sessions.get(session_id)
        if not session_info:
            return None
        stream_idx: int = session_info["stream_idx"]
        await self.stop_stream(session_info)
        self._close_talkback(session_info)
        self.sessions.pop(session_id, None)
        self._streaming_status[stream_idx] = STREAMING_STATUS["AVAILABLE"]
        return stream_idx

    @staticmethod
    def _close_talkback(session_info: dict[str, Any]) -> None:
        """Stop the talkback audio receiver for a session, if any."""
        audio_backchannel = session_info.pop("audio_backchannel", None)
        if audio_backchannel is not None:
            audio_backchannel.stop()

    def _talkback_payload_received(self, session_id: UUID, payload: bytes) -> None:
        """Dispatch a received talkback RTP payload to the accessory implementation.

        Called from the :class:`~pyhap.camera_talkback.TalkbackReceiver`
        background thread, so this hops back onto the accessory driver's event
        loop before calling the (synchronous) public callback, to keep
        :meth:`talkback_audio_received` easy to implement without every caller
        having to worry about thread-safety.
        """
        loop = getattr(self.driver, "loop", None)
        if loop is None:
            logger.warning(
                "Dropping talkback audio packet: driver event loop is not available."
            )
            return
        loop.call_soon_threadsafe(self.talkback_audio_received, session_id, payload)

    def talkback_audio_received(self, session_id: UUID, payload: bytes) -> None:
        """Handle received two-way ("talkback") audio for a streaming session.

        Only called for sessions of accessories constructed with the
        ``talkback`` option enabled (see ``__init__``). Override to consume the
        received audio, e.g. decode it (the codec is whichever was negotiated,
        see ``a_codec`` in the stream configuration passed to
        :meth:`start_stream`) and play it out through a speaker.

        :param session_id: The session ID this audio belongs to.
        :type session_id: :class:`~uuid.UUID`
        :param payload: The RTP payload of one received packet, still encoded
            with the negotiated audio codec (not decoded by pyhap).
        :type payload: ``bytes``
        """

    def set_selected_stream_configuration(self, value: str) -> None:
        """Set the selected stream configuration.

        Called from iOS to set the SelectedRTPStreamConfiguration ``Characteristic``.

        This method schedules a stream for the session in ``value`` to be start, stopped
        or reconfigured, depending on the request.

        :param value: base64-encoded selected configuration in TLV format
        :type value: ``str``
        """
        logger.debug("set_selected_stream_config - value - %s", value)

        objs = tlv.decode(value, from_base64=True)
        if SELECTED_STREAM_CONFIGURATION_TYPES["SESSION"] not in objs:
            logger.error("Bad request to set selected stream configuration.")
            return

        session = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES["SESSION"]])

        request_type = session[b"\x02"][0]
        logger.debug("Set stream config request: %d", request_type)
        if request_type == 1:
            job = functools.partial(self._start_stream, reconfigure=False)
        elif request_type == 0:
            job = self._stop_stream
        elif request_type == 4:
            job = functools.partial(self._start_stream, reconfigure=True)
        else:
            logger.error("Unknown request type %d", request_type)
            return

        self.driver.add_job(job, objs)

    def set_streaming_available(self, stream_idx: int) -> None:
        """Send an update to the controller that streaming is available."""
        self._streaming_status[stream_idx] = STREAMING_STATUS["AVAILABLE"]
        self._management[stream_idx].get_characteristic("StreamingStatus").notify()

    def set_endpoints(self, value: str, stream_idx: int | None = None) -> str:
        """Configure streaming endpoints.

        Called when iOS sets the SetupEndpoints ``Characteristic``. Sets the
        endpoint information of the camera, or a refusal, as the value of
        SetupEndpoints and returns it. A subclass that calls ``super()`` and
        returns nothing still answers. For a controller's write the driver
        keeps the answer for the connection that wrote and puts the former
        value back. An async setter must return the answer and must not set
        the value itself, a value it sets is shared with every controller. A
        direct ``client_update_value`` leaves the answer as the value.

        :param value: The base64-encoded stream session details in TLV format.
        :param value: ``str``
        :return: The base64-encoded answer in TLV format.
        :rtype: ``str``
        """
        if stream_idx is None:
            stream_idx = 0

        objs = tlv.decode(value, from_base64=True)
        session_id = UUID(bytes=objs[SETUP_TYPES["SESSION_ID"]])
        try:
            (
                address,
                is_ipv6,
                target_video_port,
                target_audio_port,
                (video_master_key, video_master_salt),
                (audio_master_key, audio_master_salt),
            ) = _decode_setup_endpoints(session_id, objs)
        except (IndexError, KeyError, ValueError, struct.error) as ex:
            logger.warning("[%s] Refusing the endpoints: %s", session_id, ex)
            # Like an answer, the refusal is only for the writer.
            return self._answer_endpoints(
                stream_idx, _setup_endpoints_status(session_id, SETUP_STATUS["ERROR"])
            )

        if not self.streaming_allowed:
            logger.info("[%s] Streaming is turned off, refusing it", session_id)
            return self._answer_endpoints(
                stream_idx, _setup_endpoints_status(session_id, SETUP_STATUS["ERROR"])
            )

        # Configure the SetupEndpoints response

        if self.has_srtp:
            video_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM["CRYPTO"],
                SRTP_CRYPTO_SUITES["AES_CM_128_HMAC_SHA1_80"],
                SETUP_SRTP_PARAM["MASTER_KEY"],
                video_master_key,
                SETUP_SRTP_PARAM["MASTER_SALT"],
                video_master_salt,
            )

            audio_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM["CRYPTO"],
                SRTP_CRYPTO_SUITES["AES_CM_128_HMAC_SHA1_80"],
                SETUP_SRTP_PARAM["MASTER_KEY"],
                audio_master_key,
                SETUP_SRTP_PARAM["MASTER_SALT"],
                audio_master_salt,
            )
        else:
            video_srtp_tlv = NO_SRTP
            audio_srtp_tlv = NO_SRTP

        video_ssrc = int.from_bytes(os.urandom(3), byteorder="big")
        audio_ssrc = int.from_bytes(os.urandom(3), byteorder="big")

        # If talkback is enabled, open a local receiver for the audio the
        # controller will send back during the session, and report *its* real
        # port below instead of echoing the controller's own port back at it.
        # Per the HAP spec (section 11, "IP Cameras"), the "Accessory Address"
        # TLV in this response carries the accessory's own address/ports (the
        # "Controller Address" received above carries the controller's), so
        # echoing the controller's port here left HAP clients with nowhere
        # valid to send the talkback audio to.
        #
        # Stop any previous receiver for this session first, so a
        # re-sent SetupEndpoints for the same session ID doesn't leak its
        # receiver thread and UDP socket.
        self._close_talkback(self.sessions.get(session_id, {}))
        audio_backchannel = None
        audio_rtp_port = target_audio_port
        if self.support_talkback:
            audio_backchannel = TalkbackReceiver(
                srtp_key_and_salt=audio_master_key + audio_master_salt,
                on_payload=functools.partial(
                    self._talkback_payload_received, session_id
                ),
                is_ipv6=bool(is_ipv6),
                controller_address=address,
            )
            audio_backchannel.start()
            audio_rtp_port = audio_backchannel.local_port

        res_address_tlv = tlv.encode(
            SETUP_ADDR_INFO["ADDRESS_VER"],
            self.stream_address_isv6,
            SETUP_ADDR_INFO["ADDRESS"],
            self.stream_address.encode("utf-8"),
            SETUP_ADDR_INFO["VIDEO_RTP_PORT"],
            struct.pack("<H", target_video_port),
            SETUP_ADDR_INFO["AUDIO_RTP_PORT"],
            struct.pack("<H", audio_rtp_port),
        )

        response_tlv = tlv.encode(
            SETUP_TYPES["SESSION_ID"],
            session_id.bytes,
            SETUP_TYPES["STATUS"],
            SETUP_STATUS["SUCCESS"],
            SETUP_TYPES["ADDRESS"],
            res_address_tlv,
            SETUP_TYPES["VIDEO_SRTP_PARAM"],
            video_srtp_tlv,
            SETUP_TYPES["AUDIO_SRTP_PARAM"],
            audio_srtp_tlv,
            SETUP_TYPES["VIDEO_SSRC"],
            struct.pack("<I", video_ssrc),
            SETUP_TYPES["AUDIO_SSRC"],
            struct.pack("<I", audio_ssrc),
            to_base64=True,
        )

        self.sessions[session_id] = {
            "id": session_id,
            "stream_idx": stream_idx,
            "address": address,
            "v_port": target_video_port,
            "v_srtp_key": to_base64_str(video_master_key + video_master_salt),
            "v_ssrc": video_ssrc,
            "a_port": target_audio_port,
            "a_srtp_key": to_base64_str(audio_master_key + audio_master_salt),
            "a_ssrc": audio_ssrc,
            "audio_backchannel": audio_backchannel,
        }

        return self._answer_endpoints(stream_idx, response_tlv)

    def _answer_endpoints(self, stream_idx: int, answer: str) -> str:
        """Set the Setup Endpoints answer as the value and return it."""
        self._management[stream_idx].get_characteristic("SetupEndpoints").set_value(
            answer, should_notify=False
        )
        return answer

    async def run(self) -> None:
        """Start the HDS listener when recording transport is available."""
        await super().run()
        await self.start_recording_transport()

    def _recording_media_wanted(self) -> bool:
        return (
            self._recording_media_running
            and self.recording_active
            and self.homekit_camera_active
        )

    def _arm_recording_media(self) -> None:
        """Keep the recording delegate armed exactly while recording may run."""
        if self._recording_media is None:
            return
        if self._recording_media_task is not None:
            # A task reconciles already; it rechecks now if it waits to retry.
            self._recording_media_wake.set()
            return
        if self._recording_media_wanted() == self._recording_media_armed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._recording_media_task = loop.create_task(self._reconcile_recording())

    async def _reconcile_recording(self) -> None:
        """Arm or disarm until the delegate matches what the camera wants."""
        delegate = self._recording_media
        assert delegate is not None  # noqa: S101 - only started with a delegate
        failures = 0
        try:
            while (wanted := self._recording_media_wanted()) != (
                self._recording_media_armed
            ):
                self._recording_media_wake.clear()
                try:
                    await delegate.set_armed(wanted)
                except Exception as err:  # noqa: BLE001 - retried with a backoff
                    # Meanwhile a recording still opens the channel on demand.
                    self._recording_media_armed = False
                    delay = min(
                        RECORDING_ARM_RETRY * 2**failures, MAX_RECORDING_ARM_RETRY
                    )
                    failures += 1
                    logger.warning(
                        "Could not arm the recording, retrying in %.0f s: %s",
                        delay,
                        err,
                    )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._recording_media_wake.wait(), delay)
                    continue
                failures = 0
                self._recording_media_armed = wanted
        finally:
            self._recording_media_task = None

    async def start_recording_transport(self) -> None:
        """Start the HDS listener on the HAP server's listen address(es).

        ``run()`` calls this. Call it directly when ``run()`` is not awaited.
        Does nothing without the recording service.
        """
        if self._recording_service is not None:
            await self._ensure_hds_listener()
            self._recording_media_running = True
            self._arm_recording_media()

    @property
    def recording_transport_port(self) -> int | None:
        """The HDS listener port, ``None`` while it is not running."""
        return self._hds_listener.port if self._hds_listener is not None else None

    @property
    def recording_connection_count(self) -> int:
        """The number of open HDS connections, including ones still in setup."""
        return len(self._hds_connections)

    async def stop_recording_transport(self) -> None:
        """Close the HDS listener and its connections, then await recordings."""
        self._hds_generation += 1
        listener, self._hds_listener = self._hds_listener, None
        if listener is not None:
            await listener.stop()
        self._recording_media_running = False
        if self._recording_media is not None:
            if (task := self._recording_media_task) is not None:
                self._recording_media_wake.set()
                await asyncio.gather(task, return_exceptions=True)
            self._recording_media_armed = False
            await self._recording_media.close()

    async def stop(self) -> None:
        """Stop all streaming sessions and the HDS listener.

        The HKSV 17.99 stream services stay closed. Create a new accessory to
        stream on them again.
        """
        await asyncio.gather(
            *(
                self.stop_stream(session_info)
                for session_info in self.sessions.values()
            ),
            *(controller.close() for controller in self._stream_controllers()),
        )
        await self.stop_recording_transport()

    # ### For client extensions ###

    async def start_stream(
        self, session_info: dict[str, Any], stream_config: dict[str, Any]
    ) -> bool:
        """Start a new stream with the given configuration.

        This method can be implemented to start a new stream. Any specific information
        about the started stream can be persisted in the ``session_info`` argument.
        The same will be passed to ``stop_stream`` when the stream for this session
        needs to be stopped.

        The default implementation starts a new process with the command in
        ``self.start_stream_cmd``, formatted with the ``stream_config``.

        :param session_info: Contains information about the current session. Can be used
            for session storage. Available keys:
            - id - The session ID.
        :type session_info: ``dict``
        :param stream_config: Stream configuration, as negotiated with the HAP client.
            Implementations can only use part of these. Available keys:
            General configuration:
                - address - The IP address from which the camera will stream
                - v_port - Remote port to which to stream video
                - v_srtp_key - Base64-encoded key and salt value for the
                    AES_CM_128_HMAC_SHA1_80 cipher to use when streaming video.
                    The key and the salt are concatenated before encoding
                - a_port - Remote audio port to which to stream audio
                - a_srtp_key - As v_srtp_params, but for the audio stream.
            Video configuration:
                - v_profile_id - The profile ID for the H.264 codec, e.g. baseline.
                    Refer to ``VIDEO_CODEC_PARAM_PROFILE_ID_TYPES``.
                - v_level - The level in the profile ID, e.g. 3:1.
                    Refer to ``VIDEO_CODEC_PARAM_LEVEL_TYPES``.
                - width - Video width
                - height - Video height
                - fps - Video frame rate
                - v_ssrc - Video synchronisation source
                - v_payload_type - Type of the video codec
                - v_max_bitrate - Maximum bit rate generated by the codec in kbps
                    and averaged over 1 second
                - v_rtcp_interval - Minimum RTCP interval in seconds
                - v_max_mtu - MTU that the IP camera must use to transmit
                    Video RTP packets.
            Audio configuration:
                - a_bitrate - Whether the bitrate is variable or constant
                - a_codec - Audio codec
                - a_comfort_noise - Wheter to use a comfort noise codec
                - a_channel - Number of audio channels
                - a_sample_rate - Audio sample rate in KHz
                - a_packet_time - Length of time represented by the media in a packet
                - a_ssrc - Audio synchronisation source
                - a_payload_type - Type of the audio codec
                - a_max_bitrate - Maximum bit rate generated by the codec in kbps
                    and averaged over 1 second
                - a_rtcp_interval - Minimum RTCP interval in seconds
                - a_comfort_payload_type - The type of codec for comfort noise

        :return: True if and only if starting the stream command was successful.
        :rtype: ``bool``
        """
        logger.debug(
            "[%s] Starting stream with the following parameters: %s",
            session_info["id"],
            stream_config,
        )

        try:
            self._validate_stream_config(stream_config)
        except ValueError as ex:
            logger.error(
                "[%s] Refusing to start stream with unsafe configuration: %s",
                session_info["id"],
                ex,
            )
            return False

        cmd = [arg.format(**stream_config) for arg in self.start_stream_cmd.split()]
        logger.debug('Executing start stream command: "%s"', " ".join(cmd))
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                limit=1024,
            )
        # The external streaming process can fail in many ways (missing
        # binary, permissions, resource limits); report and fail the
        # start attempt instead of crashing the session.
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to start streaming process because of error: %s", e)
            return False

        session_info["process"] = process

        logger.info(
            "[%s] Started stream process - PID %d", session_info["id"], process.pid
        )

        return True

    @staticmethod
    def _validate_stream_config(stream_config: dict[str, Any]) -> None:
        """Reject controller-supplied values unsafe to format into a command.

        The default start stream command formats each argument of its
        template with the negotiated configuration. The stream address must
        be a valid IP address (per the HAP spec that is all a controller may
        send) and no formatted string value may contain whitespace.

        :raises ValueError: If any value is unsafe.
        """
        address = stream_config["address"]
        try:
            ipaddress.ip_address(address)
        except ValueError as ex:
            raise ValueError(f"invalid stream address {address!r}") from ex

        for key, value in stream_config.items():
            if not isinstance(value, str):
                continue
            if not value or value.split() != [value]:
                raise ValueError(f"value for {key!r} contains whitespace")

    async def stop_stream(self, session_info: dict[str, Any]) -> None:
        """Stop the stream for the given ``session_id``.

        This method can be implemented if custom stop stream commands are needed. The
        default implementation gets the ``process`` value from the ``session_info``
        object and terminates it (assumes it is a ``subprocess.Popen`` object).

        :param session_info: The session info object. Available keys:
            - id - The session ID.
        :type session_info: ``dict``
        """
        session_id = session_info["id"]
        ffmpeg_process = session_info.get("process")
        if ffmpeg_process:
            logger.info("[%s] Stopping stream.", session_id)
            try:
                with contextlib.suppress(ProcessLookupError):
                    ffmpeg_process.terminate()
                async with asyncio.timeout(2.0):
                    _, stderr = await ffmpeg_process.communicate()
                logger.debug("Stream command stderr: %s", stderr)
            except TimeoutError:
                logger.error(
                    "Timeout while waiting for the stream process "
                    "to terminate. Trying with kill."
                )
                with contextlib.suppress(ProcessLookupError):
                    ffmpeg_process.kill()
                await ffmpeg_process.wait()
            logger.debug("Stream process stopped.")
        else:
            logger.warning("No process for session ID %s", session_id)

    async def reconfigure_stream(
        self, session_info: dict[str, Any], stream_config: dict[str, Any]
    ) -> None:
        """Reconfigure the stream so that it uses the given ``stream_config``.

        :param session_info: The session object for the session that needs to
            be reconfigured. Available keys:
            - id - The session id.
        :type session_id: ``dict``

        :return: True if and only if the reconfiguration is successful.
        :rtype: ``bool``
        """
        await self.start_stream(session_info, stream_config)

    def get_snapshot(self, image_size: dict[str, Any]) -> bytes:
        """Return a jpeg of a snapshot from the camera.

        Overwrite to implement getting snapshots from your camera.

        :param image_size: ``dict`` describing the requested image size. Contains the
            keys "image-width" and "image-height"
        """
        with open(os.path.join(RESOURCE_DIR, "snapshot.jpg"), "rb") as fp:
            return fp.read()
