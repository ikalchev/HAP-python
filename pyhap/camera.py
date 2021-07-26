"""Contains the Camera accessory and related.

When a HAP client (e.g. iOS) wants to start a video stream it does the following:
[0. Read supported RTP configuration]
[0. Read supported video configuration]
[0. Read supported audio configuration]
[0. Read the current streaming status]
1. Sets the SetupEndpoints characteristic to notify the camera about its IP address,
selected security parameters, etc.
2. The camera responds to the above by setting the SetupEndpoints with its IP address,
etc.
3. The client sets the SelectedRTPStreamConfiguration characteristic to notify the
camera of its prefered audio and video configuration and to initiate the start of the
streaming.
4. The camera starts the streaming with the above configuration.
[5. At some point the client can reconfigure or stop the stream similarly to step 3.]
"""

import asyncio
import functools
import os
import ipaddress
import logging
import struct
from uuid import UUID

from pyhap import RESOURCE_DIR
from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_CAMERA
from pyhap.util import to_base64_str, byte_bool
from pyhap import tlv


SETUP_TYPES = {
    'SESSION_ID': b'\x01',
    'STATUS': b'\x02',
    'ADDRESS': b'\x03',
    'VIDEO_SRTP_PARAM': b'\x04',
    'AUDIO_SRTP_PARAM': b'\x05',
    'VIDEO_SSRC': b'\x06',
    'AUDIO_SSRC': b'\x07'
}


SETUP_STATUS = {
    'SUCCESS': b'\x00',
    'BUSY': b'\x01',
    'ERROR': b'\x02'
}


SETUP_IPV = {
    'IPV4': b'\x00',
    'IPV6': b'\x01'
}


SETUP_ADDR_INFO = {
    'ADDRESS_VER': b'\x01',
    'ADDRESS': b'\x02',
    'VIDEO_RTP_PORT': b'\x03',
    'AUDIO_RTP_PORT': b'\x04'
}


SETUP_SRTP_PARAM = {
    'CRYPTO': b'\x01',
    'MASTER_KEY': b'\x02',
    'MASTER_SALT': b'\x03'
}


STREAMING_STATUS = {
    'AVAILABLE': b'\x00',
    'STREAMING': b'\x01',
    'BUSY': b'\x02'
}


RTP_CONFIG_TYPES = {
    'CRYPTO': b'\x02'
}


SRTP_CRYPTO_SUITES = {
    'AES_CM_128_HMAC_SHA1_80': b'\x00',
    'AES_CM_256_HMAC_SHA1_80': b'\x01',
    'NONE': b'\x02'
}


VIDEO_TYPES = {
    'CODEC': b'\x01',
    'CODEC_PARAM': b'\x02',
    'ATTRIBUTES': b'\x03',
    'RTP_PARAM': b'\x04'
}


VIDEO_CODEC_TYPES = {
    'H264': b'\x00'
}


VIDEO_CODEC_PARAM_TYPES = {
    'PROFILE_ID': b'\x01',
    'LEVEL': b'\x02',
    'PACKETIZATION_MODE': b'\x03',
    'CVO_ENABLED': b'\x04',
    'CVO_ID': b'\x05'
}


VIDEO_CODEC_PARAM_CVO_TYPES = {
    'UNSUPPORTED': b'\x01',
    'SUPPORTED': b'\x02'
}


VIDEO_CODEC_PARAM_PROFILE_ID_TYPES = {
    'BASELINE': b'\x00',
    'MAIN': b'\x01',
    'HIGH': b'\x02'
}


VIDEO_CODEC_PARAM_LEVEL_TYPES = {
    'TYPE3_1': b'\x00',
    'TYPE3_2': b'\x01',
    'TYPE4_0': b'\x02'
}


VIDEO_CODEC_PARAM_PACKETIZATION_MODE_TYPES = {
    'NON_INTERLEAVED': b'\x00'
}


VIDEO_ATTRIBUTES_TYPES = {
    'IMAGE_WIDTH': b'\x01',
    'IMAGE_HEIGHT': b'\x02',
    'FRAME_RATE': b'\x03'
}


SUPPORTED_VIDEO_CONFIG_TAG = b'\x01'


SELECTED_STREAM_CONFIGURATION_TYPES = {
    'SESSION': b'\x01',
    'VIDEO': b'\x02',
    'AUDIO': b'\x03'
}


RTP_PARAM_TYPES = {
    'PAYLOAD_TYPE': b'\x01',
    'SYNCHRONIZATION_SOURCE': b'\x02',
    'MAX_BIT_RATE': b'\x03',
    'RTCP_SEND_INTERVAL': b'\x04',
    'MAX_MTU': b'\x05',
    'COMFORT_NOISE_PAYLOAD_TYPE': b'\x06'
}


AUDIO_TYPES = {
    'CODEC': b'\x01',
    'CODEC_PARAM': b'\x02',
    'RTP_PARAM': b'\x03',
    'COMFORT_NOISE': b'\x04'
}


AUDIO_CODEC_TYPES = {
    'PCMU': b'\x00',
    'PCMA': b'\x01',
    'AACELD': b'\x02',
    'OPUS': b'\x03'
}


AUDIO_CODEC_PARAM_TYPES = {
    'CHANNEL': b'\x01',
    'BIT_RATE': b'\x02',
    'SAMPLE_RATE': b'\x03',
    'PACKET_TIME': b'\x04'
}


AUDIO_CODEC_PARAM_BIT_RATE_TYPES = {
    'VARIABLE': b'\x00',
    'CONSTANT': b'\x01'
}


AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES = {
    'KHZ_8': b'\x00',
    'KHZ_16': b'\x01',
    'KHZ_24': b'\x02'
}


SUPPORTED_AUDIO_CODECS_TAG = b'\x01'
SUPPORTED_COMFORT_NOISE_TAG = b'\x02'
SUPPORTED_AUDIO_CONFIG_TAG = b'\x02'
SET_CONFIG_REQUEST_TAG = b'\x02'
SESSION_ID = b'\x01'


NO_SRTP = b'\x01\x01\x02\x02\x00\x03\x00'
'''Configuration value for no SRTP.'''


FFMPEG_CMD = (
    'ffmpeg -re -f avfoundation -framerate {fps} -i 0:0 -threads 0 '
    '-vcodec libx264 -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency '
    '-vf scale={width}:{height} -b:v {v_max_bitrate}k -bufsize {v_max_bitrate}k '
    '-payload_type 99 -ssrc {v_ssrc} -f rtp '
    '-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} '
    'srtp://{address}:{v_port}?rtcpport={v_port}&'
    'localrtcpport={v_port}&pkt_size=1378'
)
'''Template for the ffmpeg command.'''

logger = logging.getLogger(__name__)


class Camera(Accessory):
    """An Accessory that can negotiated camera stream settings with iOS and start a
    stream.
    """

    category = CATEGORY_CAMERA

    @staticmethod
    def get_supported_rtp_config(support_srtp):
        """Return a tlv representation of the RTP configuration we support.

        SRTP support allows only the AES_CM_128_HMAC_SHA1_80 cipher for now.

        :param support_srtp: True if SRTP is supported, False otherwise.
        :type support_srtp: bool
        """
        if support_srtp:
            crypto = SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80']
        else:
            crypto = SRTP_CRYPTO_SUITES['NONE']
        return tlv.encode(RTP_CONFIG_TYPES['CRYPTO'], crypto, to_base64=True)

    @staticmethod
    def get_supported_video_stream_config(video_params):
        """Return a tlv representation of the supported video stream configuration.

        Expected video parameters:
            - codec
            - resolutions

        :param video_params: Supported video configurations
        :type video_params: dict
        """
        codec_params_tlv = tlv.encode(
            VIDEO_CODEC_PARAM_TYPES['PACKETIZATION_MODE'],
            VIDEO_CODEC_PARAM_PACKETIZATION_MODE_TYPES['NON_INTERLEAVED'])

        codec_params = video_params['codec']
        for profile in codec_params['profiles']:
            codec_params_tlv += \
                tlv.encode(VIDEO_CODEC_PARAM_TYPES['PROFILE_ID'], profile)

        for level in codec_params['levels']:
            codec_params_tlv += \
                tlv.encode(VIDEO_CODEC_PARAM_TYPES['LEVEL'], level)

        attr_tlv = b''
        for resolution in video_params['resolutions']:
            res_tlv = tlv.encode(
                VIDEO_ATTRIBUTES_TYPES['IMAGE_WIDTH'], struct.pack('<H', resolution[0]),
                VIDEO_ATTRIBUTES_TYPES['IMAGE_HEIGHT'], struct.pack('<H', resolution[1]),
                VIDEO_ATTRIBUTES_TYPES['FRAME_RATE'], struct.pack('<H', resolution[2]))
            attr_tlv += tlv.encode(VIDEO_TYPES['ATTRIBUTES'], res_tlv)

        config_tlv = tlv.encode(VIDEO_TYPES['CODEC'], VIDEO_CODEC_TYPES['H264'],
                                VIDEO_TYPES['CODEC_PARAM'], codec_params_tlv)

        return tlv.encode(SUPPORTED_VIDEO_CONFIG_TAG, config_tlv + attr_tlv,
                          to_base64=True)

    @staticmethod
    def get_supported_audio_stream_config(audio_params):
        """Return a tlv representation of the supported audio stream configuration.

        iOS supports only AACELD and OPUS

        Expected audio parameters:
        - codecs
        - comfort_noise

        :param audio_params: Supported audio configurations
        :type audio_params: dict
        """
        has_supported_codec = False
        configs = b''
        for codec_param in audio_params['codecs']:
            param_type = codec_param['type']
            if param_type == 'OPUS':
                has_supported_codec = True
                codec = AUDIO_CODEC_TYPES['OPUS']
                bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES['VARIABLE']
            elif param_type == 'AAC-eld':
                has_supported_codec = True
                codec = AUDIO_CODEC_TYPES['AACELD']
                bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES['VARIABLE']
            else:
                logger.warning('Unsupported codec %s', param_type)
                continue

            param_samplerate = codec_param['samplerate']
            if param_samplerate == 8:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_8']
            elif param_samplerate == 16:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_16']
            elif param_samplerate == 24:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_24']
            else:
                logger.warning('Unsupported sample rate %s', param_samplerate)
                continue

            param_tlv = tlv.encode(AUDIO_CODEC_PARAM_TYPES['CHANNEL'], b'\x01',
                                   AUDIO_CODEC_PARAM_TYPES['BIT_RATE'], bitrate,
                                   AUDIO_CODEC_PARAM_TYPES['SAMPLE_RATE'], samplerate)
            config_tlv = tlv.encode(AUDIO_TYPES['CODEC'], codec,
                                    AUDIO_TYPES['CODEC_PARAM'], param_tlv)
            configs += tlv.encode(SUPPORTED_AUDIO_CODECS_TAG, config_tlv)

        if not has_supported_codec:
            logger.warning('Client does not support any audio codec that iOS supports.')

            codec = AUDIO_CODEC_TYPES['OPUS']
            bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES['VARIABLE']
            samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_24']

            param_tlv = tlv.encode(
                AUDIO_CODEC_PARAM_TYPES['CHANNEL'], b'\x01',
                AUDIO_CODEC_PARAM_TYPES['BIT_RATE'], bitrate,
                AUDIO_CODEC_PARAM_TYPES['SAMPLE_RATE'], samplerate)

            config_tlv = tlv.encode(AUDIO_TYPES['CODEC'], codec,
                                    AUDIO_TYPES['CODEC_PARAM'], param_tlv)

            configs = tlv.encode(SUPPORTED_AUDIO_CODECS_TAG, config_tlv)

        comfort_noise = byte_bool(
                            audio_params.get('comfort_noise', False))
        audio_config = to_base64_str(
                        configs + tlv.encode(SUPPORTED_COMFORT_NOISE_TAG, comfort_noise))
        return audio_config

    def __init__(self, options, *args, **kwargs):
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
            - start_stream_cmd - string specifying the command to be executed to start
                the stream. The string can contain the keywords, corresponding to the
                video and audio configuration that was negotiated between the camera
                and the client. See the ``start`` method for a full list of parameters.

        :type options: ``dict``
        """
        self.has_srtp = options.get('srtp', False)
        self.start_stream_cmd = options.get('start_stream_cmd', FFMPEG_CMD)

        self.stream_address = options['address']
        try:
            ipaddress.IPv4Address(self.stream_address)
            self.stream_address_isv6 = b'\x00'
        except ValueError:
            self.stream_address_isv6 = b'\x01'
        self.sessions = {}

        super().__init__(*args, **kwargs)

        self.add_preload_service('Microphone')
        self._streaming_status = []
        self._management = []
        self._setup_stream_management(options)

    @property
    def streaming_status(self):
        """For backwards compatibility."""
        return self._streaming_status[0]

    def _setup_stream_management(self, options):
        """Create stream management."""
        stream_count = options.get("stream_count", 1)
        for stream_idx in range(stream_count):
            self._management.append(self._create_stream_management(stream_idx, options))
            self._streaming_status.append(STREAMING_STATUS["AVAILABLE"])

    def _create_stream_management(self, stream_idx, options):
        """Create a stream management service."""
        management = self.add_preload_service("CameraRTPStreamManagement")
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
        management.configure_char(
            "SetupEndpoints",
            setter_callback=lambda value: self.set_endpoints(
                value, stream_idx=stream_idx
            ),
        )
        return management

    async def _start_stream(self, objs, reconfigure):  # pylint: disable=unused-argument
        """Start or reconfigure video streaming for the given session.

        Schedules ``self.start_stream`` or ``self.reconfigure``.

        No support for reconfigure currently.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration
        :type objs: ``dict``

        :param reconfigure: Whether the stream should be reconfigured instead of
            started.
        :type reconfigure: bool
        """
        video_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES['VIDEO'])
        audio_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES['AUDIO'])

        opts = {}

        if video_tlv:
            video_objs = tlv.decode(video_tlv)

            video_codec_params = video_objs.get(VIDEO_TYPES['CODEC_PARAM'])
            if video_codec_params:
                video_codec_param_objs = tlv.decode(video_codec_params)
                opts['v_profile_id'] = \
                    video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['PROFILE_ID']]
                opts['v_level'] = \
                    video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['LEVEL']]

            video_attrs = video_objs.get(VIDEO_TYPES['ATTRIBUTES'])
            if video_attrs:
                video_attr_objs = tlv.decode(video_attrs)
                opts['width'] = struct.unpack('<H',
                            video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_WIDTH']])[0]
                opts['height'] = struct.unpack('<H',
                            video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_HEIGHT']])[0]
                opts['fps'] = struct.unpack('<B',
                                video_attr_objs[VIDEO_ATTRIBUTES_TYPES['FRAME_RATE']])[0]

            video_rtp_param = video_objs.get(VIDEO_TYPES['RTP_PARAM'])
            if video_rtp_param:
                video_rtp_param_objs = tlv.decode(video_rtp_param)
                if RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE'] in video_rtp_param_objs:
                    opts['v_ssrc'] = struct.unpack('<I',
                        video_rtp_param_objs.get(
                            RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE']))[0]
                if RTP_PARAM_TYPES['PAYLOAD_TYPE'] in video_rtp_param_objs:
                    opts['v_payload_type'] = \
                        video_rtp_param_objs.get(RTP_PARAM_TYPES['PAYLOAD_TYPE'])
                if RTP_PARAM_TYPES['MAX_BIT_RATE'] in video_rtp_param_objs:
                    opts['v_max_bitrate'] = struct.unpack('<H',
                        video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_BIT_RATE']))[0]
                if RTP_PARAM_TYPES['RTCP_SEND_INTERVAL'] in video_rtp_param_objs:
                    opts['v_rtcp_interval'] = struct.unpack('<f',
                        video_rtp_param_objs.get(RTP_PARAM_TYPES['RTCP_SEND_INTERVAL']))[0]
                if RTP_PARAM_TYPES['MAX_MTU'] in video_rtp_param_objs:
                    opts['v_max_mtu'] = video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_MTU'])

        if audio_tlv:
            audio_objs = tlv.decode(audio_tlv)

            opts['a_codec'] = audio_objs[AUDIO_TYPES['CODEC']]
            audio_codec_param_objs = tlv.decode(
                                        audio_objs[AUDIO_TYPES['CODEC_PARAM']])
            audio_rtp_param_objs = tlv.decode(
                                        audio_objs[AUDIO_TYPES['RTP_PARAM']])
            opts['a_comfort_noise'] = audio_objs[AUDIO_TYPES['COMFORT_NOISE']]

            opts['a_channel'] = \
                audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['CHANNEL']][0]
            opts['a_bitrate'] = struct.unpack('?',
                audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['BIT_RATE']])[0]
            opts['a_sample_rate'] = 8 * (
                1 + audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['SAMPLE_RATE']][0])
            opts['a_packet_time'] = struct.unpack('<B',
                audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['PACKET_TIME']])[0]

            opts['a_ssrc'] = struct.unpack('<I',
                audio_rtp_param_objs[RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE']])[0]
            opts['a_payload_type'] = audio_rtp_param_objs[RTP_PARAM_TYPES['PAYLOAD_TYPE']]
            opts['a_max_bitrate'] = struct.unpack('<H',
                audio_rtp_param_objs[RTP_PARAM_TYPES['MAX_BIT_RATE']])[0]
            opts['a_rtcp_interval'] = struct.unpack('<f',
                audio_rtp_param_objs[RTP_PARAM_TYPES['RTCP_SEND_INTERVAL']])[0]
            opts['a_comfort_payload_type'] = \
                audio_rtp_param_objs[RTP_PARAM_TYPES['COMFORT_NOISE_PAYLOAD_TYPE']]

        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])
        session_id = UUID(bytes=session_objs[SETUP_TYPES['SESSION_ID']])
        session_info = self.sessions[session_id]
        stream_idx = session_info['stream_idx']

        opts.update(session_info)
        success = await self.reconfigure_stream(session_info, opts) if reconfigure \
            else await self.start_stream(session_info, opts)

        if success:
            self._streaming_status[stream_idx] = STREAMING_STATUS['STREAMING']
        else:
            logger.error(
                '[%s] Failed to start/reconfigure stream, deleting session.',
                session_id
            )
            del self.sessions[session_id]
            self._streaming_status[stream_idx] = STREAMING_STATUS['AVAILABLE']

    def _get_streaming_status(self, stream_idx):
        """Get the streaming status in TLV format.

        Called when iOS reads the StreaminStatus ``Characteristic``.
        """
        return tlv.encode(b'\x01', self._streaming_status[stream_idx], to_base64=True)

    async def _stop_stream(self, objs):
        """Stop the stream for the specified session.

        Schedules ``self.stop_stream``.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration value.
        :param objs: ``dict``
        """
        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])
        session_id = UUID(bytes=session_objs[SETUP_TYPES['SESSION_ID']])

        session_info = self.sessions.get(session_id)
        if not session_info:
            logger.error(
                'Requested to stop stream for session %s, but no '
                'such session was found',
                session_id
            )
            return

        stream_idx = session_info['stream_idx']
        await self.stop_stream(session_info)
        del self.sessions[session_id]

        self._streaming_status[stream_idx] = STREAMING_STATUS['AVAILABLE']

    def set_selected_stream_configuration(self, value):
        """Set the selected stream configuration.

        Called from iOS to set the SelectedRTPStreamConfiguration ``Characteristic``.

        This method schedules a stream for the session in ``value`` to be start, stopped
        or reconfigured, depending on the request.

        :param value: base64-encoded selected configuration in TLV format
        :type value: ``str``
        """
        logger.debug('set_selected_stream_config - value - %s', value)

        objs = tlv.decode(value, from_base64=True)
        if SELECTED_STREAM_CONFIGURATION_TYPES['SESSION'] not in objs:
            logger.error('Bad request to set selected stream configuration.')
            return

        session = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])

        request_type = session[b'\x02'][0]
        logger.debug('Set stream config request: %d', request_type)
        if request_type == 1:
            job = functools.partial(self._start_stream, reconfigure=False)
        elif request_type == 0:
            job = self._stop_stream
        elif request_type == 4:
            job = functools.partial(self._start_stream, reconfigure=True)
        else:
            logger.error('Unknown request type %d', request_type)
            return

        self.driver.add_job(job, objs)

    def set_streaming_available(self, stream_idx):
        """Send an update to the controller that streaming is available."""
        self._streaming_status[stream_idx] = STREAMING_STATUS["AVAILABLE"]
        self._management[stream_idx].get_characteristic("StreamingStatus").notify()

    def set_endpoints(self, value, stream_idx=None):
        """Configure streaming endpoints.

        Called when iOS sets the SetupEndpoints ``Characteristic``. The endpoint
        information for the camera should be set as the current value of SetupEndpoints.

        :param value: The base64-encoded stream session details in TLV format.
        :param value: ``str``
        """
        if stream_idx is None:
            stream_idx = 0

        objs = tlv.decode(value, from_base64=True)
        session_id = UUID(bytes=objs[SETUP_TYPES['SESSION_ID']])

        # Extract address info
        address_tlv = objs[SETUP_TYPES['ADDRESS']]
        address_info_objs = tlv.decode(address_tlv)
        is_ipv6 = struct.unpack('?',
            address_info_objs[SETUP_ADDR_INFO['ADDRESS_VER']])[0]
        address = address_info_objs[SETUP_ADDR_INFO['ADDRESS']].decode('utf8')
        target_video_port = struct.unpack(
            '<H', address_info_objs[SETUP_ADDR_INFO['VIDEO_RTP_PORT']])[0]
        target_audio_port = struct.unpack(
            '<H', address_info_objs[SETUP_ADDR_INFO['AUDIO_RTP_PORT']])[0]

        # Video SRTP Params
        video_srtp_tlv = objs[SETUP_TYPES['VIDEO_SRTP_PARAM']]
        video_info_objs = tlv.decode(video_srtp_tlv)
        video_crypto_suite = video_info_objs[SETUP_SRTP_PARAM['CRYPTO']][0]
        video_master_key = video_info_objs[SETUP_SRTP_PARAM['MASTER_KEY']]
        video_master_salt = video_info_objs[SETUP_SRTP_PARAM['MASTER_SALT']]

        # Audio SRTP Params
        audio_srtp_tlv = objs[SETUP_TYPES['AUDIO_SRTP_PARAM']]
        audio_info_objs = tlv.decode(audio_srtp_tlv)
        audio_crypto_suite = audio_info_objs[SETUP_SRTP_PARAM['CRYPTO']][0]
        audio_master_key = audio_info_objs[SETUP_SRTP_PARAM['MASTER_KEY']]
        audio_master_salt = audio_info_objs[SETUP_SRTP_PARAM['MASTER_SALT']]

        logger.debug(
            'Received endpoint configuration:'
            '\nsession_id: %s\naddress: %s\nis_ipv6: %s'
            '\ntarget_video_port: %s\ntarget_audio_port: %s'
            '\nvideo_crypto_suite: %s\nvideo_srtp: %s'
            '\naudio_crypto_suite: %s\naudio_srtp: %s',
            session_id, address, is_ipv6, target_video_port, target_audio_port,
            video_crypto_suite,
            to_base64_str(video_master_key + video_master_salt),
            audio_crypto_suite,
            to_base64_str(audio_master_key + audio_master_salt)
        )

        # Configure the SetupEndpoints response

        if self.has_srtp:
            video_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM['CRYPTO'], SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80'],
                SETUP_SRTP_PARAM['MASTER_KEY'], video_master_key,
                SETUP_SRTP_PARAM['MASTER_SALT'], video_master_salt)

            audio_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM['CRYPTO'], SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80'],
                SETUP_SRTP_PARAM['MASTER_KEY'], audio_master_key,
                SETUP_SRTP_PARAM['MASTER_SALT'], audio_master_salt)
        else:
            video_srtp_tlv = NO_SRTP
            audio_srtp_tlv = NO_SRTP

        video_ssrc = int.from_bytes(os.urandom(3), byteorder="big")
        audio_ssrc = int.from_bytes(os.urandom(3), byteorder="big")

        res_address_tlv = tlv.encode(
            SETUP_ADDR_INFO['ADDRESS_VER'], self.stream_address_isv6,
            SETUP_ADDR_INFO['ADDRESS'], self.stream_address.encode('utf-8'),
            SETUP_ADDR_INFO['VIDEO_RTP_PORT'], struct.pack('<H', target_video_port),
            SETUP_ADDR_INFO['AUDIO_RTP_PORT'], struct.pack('<H', target_audio_port))

        response_tlv = tlv.encode(
            SETUP_TYPES['SESSION_ID'], session_id.bytes,
            SETUP_TYPES['STATUS'], SETUP_STATUS['SUCCESS'],
            SETUP_TYPES['ADDRESS'], res_address_tlv,
            SETUP_TYPES['VIDEO_SRTP_PARAM'], video_srtp_tlv,
            SETUP_TYPES['AUDIO_SRTP_PARAM'], audio_srtp_tlv,
            SETUP_TYPES['VIDEO_SSRC'], struct.pack('<I', video_ssrc),
            SETUP_TYPES['AUDIO_SSRC'], struct.pack('<I', audio_ssrc),
            to_base64=True)

        self.sessions[session_id] = {
            'id': session_id,
            'stream_idx': stream_idx,
            'address': address,
            'v_port': target_video_port,
            'v_srtp_key': to_base64_str(video_master_key + video_master_salt),
            'v_ssrc': video_ssrc,
            'a_port': target_audio_port,
            'a_srtp_key': to_base64_str(audio_master_key + audio_master_salt),
            'a_ssrc': audio_ssrc
        }

        self._management[stream_idx].get_characteristic('SetupEndpoints').set_value(response_tlv)

    async def stop(self):
        """Stop all streaming sessions."""
        await asyncio.gather(*(
            self.stop_stream(session_info) for session_info in self.sessions.values()))

    # ### For client extensions ###

    async def start_stream(self, session_info, stream_config):
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
            '[%s] Starting stream with the following parameters: %s',
            session_info['id'],
            stream_config
        )

        cmd = self.start_stream_cmd.format(**stream_config).split()
        logger.debug('Executing start stream command: "%s"', ' '.join(cmd))
        try:
            process = await asyncio.create_subprocess_exec(*cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024)
        except Exception as e:  # pylint: disable=broad-except
            logger.error('Failed to start streaming process because of error: %s', e)
            return False

        session_info['process'] = process

        logger.info(
            '[%s] Started stream process - PID %d',
            session_info['id'],
            process.pid
        )

        return True

    async def stop_stream(self, session_info):  # pylint: disable=no-self-use
        """Stop the stream for the given ``session_id``.

        This method can be implemented if custom stop stream commands are needed. The
        default implementation gets the ``process`` value from the ``session_info``
        object and terminates it (assumes it is a ``subprocess.Popen`` object).

        :param session_info: The session info object. Available keys:
            - id - The session ID.
        :type session_info: ``dict``
        """
        session_id = session_info['id']
        ffmpeg_process = session_info.get('process')
        if ffmpeg_process:
            logger.info('[%s] Stopping stream.', session_id)
            try:
                ffmpeg_process.terminate()
                _, stderr = await asyncio.wait_for(
                    ffmpeg_process.communicate(), timeout=2.0)
                logger.debug('Stream command stderr: %s', stderr)
            except asyncio.TimeoutError:
                logger.error(
                    'Timeout while waiting for the stream process '
                    'to terminate. Trying with kill.'
                )
                ffmpeg_process.kill()
                await ffmpeg_process.wait()
            logger.debug('Stream process stopped.')
        else:
            logger.warning('No process for session ID %s', session_id)

    async def reconfigure_stream(self, session_info, stream_config):
        """Reconfigure the stream so that it uses the given ``stream_config``.

        :param session_info: The session object for the session that needs to
            be reconfigured. Available keys:
            - id - The session id.
        :type session_id: ``dict``

        :return: True if and only if the reconfiguration is successful.
        :rtype: ``bool``
        """
        await self.start_stream(session_info, stream_config)

    def get_snapshot(self, image_size):  # pylint: disable=unused-argument, no-self-use
        """Return a jpeg of a snapshot from the camera.

        Overwrite to implement getting snapshots from your camera.

        :param image_size: ``dict`` describing the requested image size. Contains the
            keys "image-width" and "image-height"
        """
        with open(os.path.join(RESOURCE_DIR, 'snapshot.jpg'), 'rb') as fp:
            return fp.read()
