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

import os
import ipaddress
import logging
import struct
import subprocess
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
    # pylint: disable=bad-continuation
    'ffmpeg -re -f avfoundation -i {camera_source} -threads 0 '
    '-vcodec libx264 -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency '
    '-vf scale={width}:{height} -b:v {bitrate}k -bufsize {bitrate}k '
    '-payload_type 99 -ssrc {video_ssrc} -f rtp '
    '-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {video_srtp_key} '
    'srtp://{address}:{video_port}?rtcpport={video_port}&'
    'localrtcpport={local_video_port}&pkt_size=1378'
)
'''Template for the ffmpeg command.'''


class CameraAccessory(Accessory):
    '''An Accessory that can negotiated camera stream settings with iOS and start a
    stream.
    '''

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
                logging.warning('Unsupported codec %s', param_type)
                continue

            param_samplerate = codec_param['samplerate']
            if param_samplerate == 8:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_8']
            elif param_samplerate == 16:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_16']
            elif param_samplerate == 24:
                samplerate = AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES['KHZ_24']
            else:
                logging.warning('Unsupported sample rate %s', param_samplerate)
                continue

            param_tlv = tlv.encode(AUDIO_CODEC_PARAM_TYPES['CHANNEL'], b'\x01',
                                   AUDIO_CODEC_PARAM_TYPES['BIT_RATE'], bitrate,
                                   AUDIO_CODEC_PARAM_TYPES['SAMPLE_RATE'], samplerate)
            config_tlv = tlv.encode(AUDIO_TYPES['CODEC'], codec,
                                    AUDIO_TYPES['CODEC_PARAM'], param_tlv)
            configs += tlv.encode(SUPPORTED_AUDIO_CODECS_TAG, config_tlv)

        if not has_supported_codec:
            logging.warning('Client does not support any audio codec that iOS supports.')

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
                and the client. The keywords will be substituted before the command
                is ran:
                - fps - Frames per second
                - width, height
                - bitrate
                - ssrc - synchronisation source
                - video_srtp_key - Video cipher key
                - target_address - The address to which the camera should stream

        :type options: ``dict``
        """
        self.streaming_status = STREAMING_STATUS['AVAILABLE']
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
        management = self.add_preload_service('CameraRTPStreamManagement')
        management.configure_char('StreamingStatus',
                                  getter_callback=self._get_streaimg_status)
        management.configure_char('SupportedRTPConfiguration',
                                  value=self.get_supported_rtp_config(
                                                options.get('srtp', False)))
        management.configure_char('SupportedVideoStreamConfiguration',
                                  value=self.get_supported_video_stream_config(
                                                options['video']))
        management.configure_char('SupportedAudioStreamConfiguration',
                                  value=self.get_supported_audio_stream_config(
                                                options['audio']))
        management.configure_char('SelectedRTPStreamConfiguration',
                                  setter_callback=self.set_selected_stream_configuration)
        management.configure_char('SetupEndpoints',
                                  setter_callback=self.set_endpoints)

    def _start_stream(self, objs, reconfigure):  # pylint: disable=unused-argument
        """Start or reconfigure video streaming for the given session.

        No support for reconfigure currently.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration
        :type objs: ``dict``

        :param reconfigure: Whether the stream should be reconfigured instead of
            started.
        :type reconfigure: bool
        """
        video_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES['VIDEO'])
        audio_tlv = objs.get(SELECTED_STREAM_CONFIGURATION_TYPES['AUDIO'])

        if video_tlv:
            video_objs = tlv.decode(video_tlv)

            video_codec_params = video_objs.get(VIDEO_TYPES['CODEC_PARAM'])
            if video_codec_params:
                video_codec_param_objs = tlv.decode(video_codec_params)
                profile_id = \
                    video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['PROFILE_ID']]
                level = video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['LEVEL']]

            video_attrs = video_objs.get(VIDEO_TYPES['ATTRIBUTES'])
            if video_attrs:
                video_attr_objs = tlv.decode(video_attrs)
                width = struct.unpack('<H',
                            video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_WIDTH']])[0]
                height = struct.unpack('<H',
                            video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_HEIGHT']])[0]
                fps = struct.unpack('<B',
                                video_attr_objs[VIDEO_ATTRIBUTES_TYPES['FRAME_RATE']])[0]

            video_rtp_param = video_objs.get(VIDEO_TYPES['RTP_PARAM'])
            if video_rtp_param:
                video_rtp_param_objs = tlv.decode(video_rtp_param)
                #TODO: Optionals, handle the case where they are missing
                video_ssrc = 1 or struct.unpack('<I',
                    video_rtp_param_objs.get(
                        RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE']))[0]
                video_payload_type = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['PAYLOAD_TYPE'])
                video_max_bitrate = struct.unpack('<H',
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_BIT_RATE']))[0]
                video_rtcp_interval = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['RTCP_SEND_INTERVAL'])
                video_max_mtu = video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_MTU'])

        if audio_tlv:
            audio_objs = tlv.decode(audio_tlv)
            audio_codec = audio_objs[AUDIO_TYPES['CODEC']]
            audio_codec_param_objs = tlv.decode(
                                        audio_objs[AUDIO_TYPES['CODEC_PARAM']])
            audio_rtp_param_objs = tlv.decode(
                                        audio_objs[AUDIO_TYPES['RTP_PARAM']])
            audio_comfort_noise = audio_objs[AUDIO_TYPES['COMFORT_NOISE']]

            # TODO handle audio codec
            audio_channel = audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['CHANNEL']]
            audio_bitrate = audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['BIT_RATE']]
            audio_sample_rate = \
                audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['SAMPLE_RATE']]
            audio_packet_time = \
                audio_codec_param_objs[AUDIO_CODEC_PARAM_TYPES['PACKET_TIME']]

            audio_ssrc = audio_rtp_param_objs[RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE']]
            audio_payload_type = audio_rtp_param_objs[RTP_PARAM_TYPES['PAYLOAD_TYPE']]
            audio_max_bitrate = audio_rtp_param_objs[RTP_PARAM_TYPES['MAX_BIT_RATE']]
            audio_rtcp_interval = \
                audio_rtp_param_objs[RTP_PARAM_TYPES['RTCP_SEND_INTERVAL']]
            audio_comfort_payload_type = \
                audio_rtp_param_objs[RTP_PARAM_TYPES['COMFORT_NOISE_PAYLOAD_TYPE']]

        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])
        session_id = session_objs[b'\x01']
        session_info = self.sessions[session_id]
        width = width or 1280
        height = height or 720
        video_max_bitrate = video_max_bitrate or 300
        fps = min(fps, 30)

        cmd = self.start_stream_cmd.format(
            camera_source='0:0',
            address=session_info['address'],
            video_port=session_info['video_port'],
            video_srtp_key=to_base64_str(session_info['video_srtp_key']
                                         + session_info['video_srtp_salt']),
            video_ssrc=video_ssrc,  # TODO: this param is optional, check before adding
            fps=fps,
            width=width,
            height=height,
            bitrate=video_max_bitrate,
            local_video_port=session_info['video_port']
        ).split()

        logging.debug('Starting ffmpeg command: %s', cmd)
        print(" ".join(cmd))
        self.sessions[session_id]['process'] = subprocess.Popen(cmd)
        logging.debug('Started ffmpeg')
        self.streaming_status = STREAMING_STATUS['STREAMING']

    def _get_streaimg_status(self):
        """Get the streaming status in TLV format.

        Called when iOS reads the StreaminStatus ``Characteristic``.
        """
        return tlv.encode(b'\x01', self.streaming_status, to_base64=True)

    def _stop_stream(self, objs):
        """Stop the stream for the specified session.

        :param objs: TLV-decoded SelectedRTPStreamConfiguration value.
        :param objs: ``dict``
        """
        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])
        session_id = session_objs[b'\x01']
        ffmpeg_process = self.sessions.pop(session_id).get('process')
        if ffmpeg_process:
            ffmpeg_process.kill()
        self.session_id = None

    def set_selected_stream_configuration(self, value):
        """Set the selected stream configuration.

        Called from iOS to set the SelectedRTPStreamConfiguration ``Characteristic``.

        :param value: base64-encoded selected configuration in TLV format
        :type value: ``str``
        """
        logging.debug('set_selected_stream_config - value - %s', value)
        self.selected_config = value
        objs = tlv.decode(value, from_base64=True)
        if SELECTED_STREAM_CONFIGURATION_TYPES['SESSION'] not in objs:
            logging.error('Bad request to set selected stream configuration.')
            return

        session = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])

        request_type = session[b'\x02'][0]
        logging.debug('Set stream config request: %d', request_type)
        if request_type == 1:
            self._start_stream(objs, reconfigure=False)
        elif request_type == 0:
            self._stop_stream(objs)
        elif request_type == 4:
            self._start_stream(objs, reconfigure=True)
        else:
            logging.error('Unknown request type %d', request_type)

    def set_endpoints(self, value):
        """Configure streaming endpoints.

        Called when iOS sets the SetupEndpoints ``Characteristic``. The endpoint
        information for the camera should be set as the current value of SetupEndpoints.

        :param value: The base64-encoded stream session details in TLV format.
        :param value: ``str``
        """
        objs = tlv.decode(value, from_base64=True)
        session_id = objs[SETUP_TYPES['SESSION_ID']]

        # Extract address info
        address_tlv = objs[SETUP_TYPES['ADDRESS']]
        address_info_objs = tlv.decode(address_tlv)
        is_ipv6 = address_info_objs[SETUP_ADDR_INFO['ADDRESS_VER']][0]  #TODO
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

        logging.debug('Received endpoint configuration:'
                      '\nsession_id: %s\naddress: %s\nis_ipv6: %s'
                      '\ntarget_video_port: %s\ntarget_audio_port: %s'
                      '\nvideo_crypto_suite: %s\nvideo_srtp: %s'
                      '\naudio_crypto_suite: %s\naudio_srtp: %s',
                      session_id, address, is_ipv6, target_video_port, target_audio_port,
                      video_crypto_suite,
                      to_base64_str(video_master_key + video_master_salt),
                      audio_crypto_suite,
                      to_base64_str(audio_master_key + audio_master_salt))

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

        video_ssrc = b'\x01'  #os.urandom(4)
        audio_ssrc = b'\x01'  #os.urandom(4)

        res_address_tlv = tlv.encode(
            SETUP_ADDR_INFO['ADDRESS_VER'], self.stream_address_isv6,
            SETUP_ADDR_INFO['ADDRESS'], self.stream_address.encode('utf-8'),
            SETUP_ADDR_INFO['VIDEO_RTP_PORT'], struct.pack('<H', target_video_port),
            SETUP_ADDR_INFO['AUDIO_RTP_PORT'], struct.pack('<H', target_audio_port))

        response_tlv = tlv.encode(
            SETUP_TYPES['SESSION_ID'], session_id,
            SETUP_TYPES['STATUS'], SETUP_STATUS['SUCCESS'],
            SETUP_TYPES['ADDRESS'], res_address_tlv,
            SETUP_TYPES['VIDEO_SRTP_PARAM'], video_srtp_tlv,
            SETUP_TYPES['AUDIO_SRTP_PARAM'], audio_srtp_tlv,
            SETUP_TYPES['VIDEO_SSRC'], video_ssrc,
            SETUP_TYPES['AUDIO_SSRC'], audio_ssrc,
            to_base64=True)

        self.sessions[session_id] = {
            'address': address,
            'video_port': target_video_port,
            'video_srtp_key': video_master_key,
            'video_srtp_salt': video_master_salt,
            'video_ssrc': video_ssrc,
            'audio_port': target_audio_port,
            'audio_srtp_key': audio_master_key,
            'audio_srtp_salt': audio_master_salt,
            'audio_ssrc': audio_ssrc
        }

        self.get_service('CameraRTPStreamManagement')\
            .get_characteristic('SetupEndpoints')\
            .set_value(response_tlv)

    # ### For client extensions ###

    def get_snapshot(self, image_size):  # pylint: disable=unused-argument, no-self-use
        """Return a jpeg of a snapshot from the camera.

        Overwrite to implement getting snapshots from your camera.

        :param image_size: ``dict`` describing the requested image size. Contains the
            keys "image-width" and "image-height"
        """
        with open(os.path.join(RESOURCE_DIR, 'snapshot.jpg'), 'rb') as fp:
            return fp.read()
