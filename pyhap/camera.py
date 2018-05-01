'''Contains the Camera accessory and related.
'''

import os
import ipaddress
import logging
import struct
import subprocess

from .accessory import Accessory
import loader
import tlv


logger = logging.getLogger(__name__)


SETUP_TYPES = {
    'SESSION_ID': 0x01,
    'STATUS': 0x02,
    'ADDRESS': 0x03,
    'VIDEO_SRTP_PARAM': 0x04,
    'AUDIO_SRTP_PARAM': 0x05,
    'VIDEO_SSRC': 0x06,
    'AUDIO_SSRC': 0x07
}


SETUP_STATUS = {
    'SUCCESS': 0x00,
    'BUSY': 0x01,
    'ERROR': 0x02
}


SETUP_IPV = {
    'IPV4': 0x00,
    'IPV6': 0x01
}


SETUP_ADDR_INFO = {
    'ADDRESS_VER': 0x01,
    'ADDRESS': 0x02,
    'VIDEO_RTP_PORT': 0x03,
    'AUDIO_RTP_PORT': 0x04
}


SETUP_SRTP_PARAM = {
    'CRYPTO': 0x01,
    'MASTER_KEY': 0x02,
    'MASTER_SALT': 0x03
}


STREAMING_STATUS = {
    'AVAILABLE': 0x00,
    'STREAMING': 0x01,
    'BUSY': 0x02
}


RTP_CONFIG_TYPES = {
    'CRYPTO': 0x02
}


SRTP_CRYPTO_SUITES = {
    'AES_CM_128_HMAC_SHA1_80': 0x00,
    'AES_CM_256_HMAC_SHA1_80': 0x01,
    'NONE': 0x02
}


VIDEO_TYPES = {
    'CODEC': 0x01,
    'CODEC_PARAM': 0x02,
    'ATTRIBUTES': 0x03,
    'RTP_PARAM': 0x04
}


VIDEO_CODEC_TYPES = {
    'H264': 0x00
}


VIDEO_CODEC_PARAM_TYPES = {
    'PROFILE_ID': 0x01,
    'LEVEL': 0x02,
    'PACKETIZATION_MODE': 0x03,
    'CVO_ENABLED': 0x04,
    'CVO_ID': 0x05
}


VIDEO_CODEC_PARAM_CVO_TYPES = {
    'UNSUPPORTED': 0x01,
    'SUPPORTED': 0x02
}


VIDEO_CODEC_PARAM_PROFILE_ID_TYPES = {
    'BASELINE': 0x00,
    'MAIN': 0x01,
    'HIGH': 0x02
}


VIDEO_CODEC_PARAM_LEVEL_TYPES = {
    'TYPE3_1': 0x00,
    'TYPE3_2': 0x01,
    'TYPE4_0': 0x02
}


VIDEO_CODEC_PARAM_PACKETIZATION_MODE_TYPES = {
    'NON_INTERLEAVED': 0x00
}


VIDEO_ATTRIBUTES_TYPES = {
    'IMAGE_WIDTH': 0x01,
    'IMAGE_HEIGHT': 0x02,
    'FRAME_RATE': 0x03
}


SELECTED_STREAM_CONFIGURATION_TYPES = {
    'SESSION': 0x01,
    'VIDEO': 0x02,
    'AUDIO': 0x03
}


RTP_PARAM_TYPES = {
    'PAYLOAD_TYPE': 0x01,
    'SYNCHRONIZATION_SOURCE': 0x02,
    'MAX_BIT_RATE': 0x03,
    'RTCP_SEND_INTERVAL': 0x04,
    'MAX_MTU': 0x05,
    'COMFORT_NOISE_PAYLOAD_TYPE': 0x06
}


AUDIO_TYPES = {
    'CODEC': 0x01,
    'CODEC_PARAM': 0x02,
    'RTP_PARAM': 0x03,
    'COMFORT_NOISE': 0x04
}


AUDIO_CODEC_TYPES = {
    'PCMU': 0x00,
    'PCMA': 0x01,
    'AACELD': 0x02,
    'OPUS': 0x03
}


AUDIO_CODEC_PARAM_TYPES = {
    'CHANNEL': 0x01,
    'BIT_RATE': 0x02,
    'SAMPLE_RATE': 0x03,
    'PACKET_TIME': 0x04
}


AUDIO_CODEC_PARAM_BIT_RATE_TYPES = {
    'VARIABLE': 0x00,
    'CONSTANT': 0x01
}


AUDIO_CODEC_PARAM_SAMPLE_RATE_TYPES = {
    'KHZ_8': 0x00,
    'KHZ_16': 0x01,
    'KHZ_24': 0x02
}


SESSION_ID = b'\x01'

class CameraAccessory(Accessory):
    '''An Accessory that can negotiated camera stream settings with iOS and start a
    stream.
    '''

    NO_SRTP = b'\x01\x01\x02\x02\x00\x03\x00'
    '''Configuration value for no SRTP.'''

    FFMPEG_CMD = ('ffmpeg -re -f avfoundation -r 29.970000 -i {camera_source} -threads 0 '
        '-vcodec libx264 -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency '
        '-vf scale={width}:{height} -b:v {bitrate}k -bufsize {bitrate}k '
        '-payload_type 99 -ssrc {video_ssrc} -f rtp '
        '-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {video_srtp_key} '
        'srtp://{address}:{video_port}?rtcpport={video_port}&'
        'localrtcpport={local_video_port}&pkt_size=1378')
    '''Template for the ffmpeg command.'''

    @staticmethod
    def get_supported_rtp_config(support_srtp):
        '''XXX
        :param support_srtp: True if SRTP is supported, False otherwise.
        :type support_srtp: bool
        '''
        if support_srtp:
            crypto = SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80']
        else:
            crypto = SRTP_CRYPTO_SUITES['NONE']
        return tlv.encode(RTP_CONFIG_TYPES['CRYPTO'], crypto)

    @staticmethod
    def get_supported_video_stream_config(video_params):
        '''XXX
        Expected video parameters:
            - codec
            - profiles
            - levels
            - resolutions
        '''
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
                VIDEO_ATTRIBUTES_TYPES['IMAGE_WIDTH'], struct.pack('<B', resolution[0]),
                VIDEO_ATTRIBUTES_TYPES['IMAGE_HEIGHT'], struct.pack('<B', resolution[1]),
                VIDEO_ATTRIBUTES_TYPES['FRAME_RATE'], struct.pack('<B', resolution[2]))
            attr_tlv += tlv.encode(VIDEO_TYPES['ATTRIBUTES'], res_tlv)

        config_tlv = tlv.encode(VIDEO_TYPES['CODEC'], VIDEO_CODEC_TYPES['H264'],
                                VIDEO_TYPES['CODEC_PARAM'], codec_params_tlv)

        return tlv.encode(b'\x01', config_tlv + attr_tlv)

    @staticmethod
    def get_supported_audio_stream_config(audio_params):
        '''XXX
        iOS supports only AACELD and OPUS

        Expected audio parameters:
        - codecs
        - comfort noise
        '''
        has_supported_codec = False
        configs = b''
        for codec_param in audio_params['codecs']:
            param_type = codec_param['type']
            if param_type == 'OPUS':
                has_supported_codec = True
                codec = AUDIO_CODEC_TYPES['OPUS']
                bitrate = AUDIO_CODEC_PARAM_BIT_RATE_TYPES['VARIABLE']
            elif param_type == 'ACC-eld':
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
            configs += tlv.encode(b'\x01', config_tlv)

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

            configs = tlv.encode(b'\x01', config_tlv)

        comfort_noise = b'\x01' if audio_params['comfort_noise'] else b'\x00'
        return configs + tlv.encode(b'\x02', comfort_noise)

    def __init__(self, options, *args, **kwargs):
        '''
        Expected options:
        - video
        - audio
        - srtp
        - address
        '''
        super(CameraAccessory, self).__init__(*args, **kwargs)

        self.streaming_status = STREAMING_STATUS['AVAILABLE']
        self.support_srtp = options.get('srtp', False)
        self.supported_rtp_config = self.get_supported_rtp_config(self.support_srtp)
        self.supported_video_config = \
            self.get_supported_video_stream_config(options['video'])
        self.supported_audio_config = \
            self.get_supported_audio_stream_config(options['audio'])

        self.stream_address = options['address']
        try:
            ipaddress.IPv4Address(self.stream_address)
            self.stream_address_isv6 = 0
        except ValueError:
            self.stream_address_isv6 = 1
        self.pending_sessions = {}
        self.current_sessions = {}
        self.selected_config = None
        self.setup_response = None
        self.session_id = None
        self.management_service = None

    def _set_services(self):
        '''
        '''
        super(CameraAccessory, self)._set_services()

        serv_loader = loader.get_serv_loader()
        self.add_service(
            serv_loader.get('CameraControl'))

        self.management_service = serv_loader.get('CameraRTPStreamManagement')
        self.add_service(self.management_service)

        self.management_service.get_characteristic('StreamingStatus')\
                               .set_value(
                                    tlv.encode(b'\x01', self.streaming_status))  #TODO

        self.management_service.get_characteristic('SupportedRTPConfiguration')\
                               .set_value(self.supported_rtp_config)

        self.management_service.get_characteristic('SupportedVideoStreamConfiguration')\
                               .set_value(self.supported_video_config)

        self.management_service.get_characteristic('SupportedAudioStreamConfiguration')\
                               .set_value(self.supported_audio_config)

        selected_stream = \
            self.management_service.get_characteristic('SelectedStreamConfiguration')
        selected_stream.set_value(self.selected_config)
        selected_stream.setter_callback = self.set_selected_stream_configuration

        endpoints = self.management_service.get_characteristic('SetupEndpoints')
        endpoints.set_value(self.setup_response)
        endpoints.setter_callback = self.set_endpoints

    def _start_stream(self, objs, reconfigure):
        if SELECTED_STREAM_CONFIGURATION_TYPES['VIDEO'] in objs:
            video_objs = tlv.decode(SELECTED_STREAM_CONFIGURATION_TYPES['VIDEO'])

            if VIDEO_TYPES['CODEC_PARAMS'] in video_objs:
                video_codec_param_objs = tlv.decode(VIDEO_TYPES['CODEC_PARAMS'])
                profile_id = \
                    video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['PROFILE_ID']]
                level = video_codec_param_objs[VIDEO_CODEC_PARAM_TYPES['LEVEL']]

            if VIDEO_TYPES['ATTRIBUTES'] in video_objs:
                video_attr_objs = tlv.decode(video_objs[VIDEO_TYPES['ATTRIBUTES']])
                width = video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_WIDTH']]
                height = video_attr_objs[VIDEO_ATTRIBUTES_TYPES['IMAGE_HEIGHT']]
                fps = video_attr_objs[VIDEO_ATTRIBUTES_TYPES['FRAME_RATE']]

            if VIDEO_TYPES['RTP_PARAM'] in video_objs:
                video_rtp_param_objs = tlv.decode(video_objs[VIDEO_TYPES['RTP_PARAM']])
                # Optionals
                video_ssrc = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['SYNCHRONIZATION_SOURCE'])
                video_payload_type = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['PAYLOAD_TYPE'])
                video_max_bitrate = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_BIT_RATE'])
                video_rtcp_interval = \
                    video_rtp_param_objs.get(RTP_PARAM_TYPES['RTCP_SEND_INTERVAL'])
                video_max_mtu = video_rtp_param_objs.get(RTP_PARAM_TYPES['MAX_MTU'])

        if SELECTED_STREAM_CONFIGURATION_TYPES['AUDIO'] in objs:
            audio_objs = tlv.decode(SELECTED_STREAM_CONFIGURATION_TYPES['AUDIO'])
            audio_codec = audio_objs[AUDIO_TYPES['CODEC']]
            audio_codec_param_objs = tlv.decode(AUDIO_TYPES['CODEC_PARAM'])
            audio_rtp_param_objs = tlv.decode(AUDIO_TYPES['RTP_PARAM'])
            audio_comfort_noise = tlv.decode(AUDIO_TYPES['COMFORT_NOISE'])
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
        session_info = self.pending_sessions[session_id]
        width = width or 1280
        height = height or 720
        video_max_bitrate = video_max_bitrate or 300
        fps = min(fps, 30)

        cmd = self.FFMPEG_CMD.format({
            'camera_source': '0:0',
            'address': session_info['address'],
            'video_port': session_info['video_port'],
            'video_srtp_key':
                session_info['video_srtp_key'] + session_info['video_srtp_salt'],
            'fps': fps,
            'width': width,
            'height': height,
            'bitrate': video_max_bitrate,
            'local_video_port': session_info['video_port']
        }).split()

        self.current_sessions[session_id] = subprocess.Popen(cmd)
        del self.pending_sessions[session_id]

    def _stop_stream(self, objs):
        session_objs = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])
        session_id = session_objs[b'\x01']
        ffmpeg_process = self.current_sessions.pop(session_id)
        ffmpeg_process.kill()
        self.session_id = None

    def set_selected_stream_configuration(self, value):
        '''XXX Called from iOS to select a stream configuration.
        '''
        self.selected_config = value
        objs = tlv.decode(value)
        if SELECTED_STREAM_CONFIGURATION_TYPES['SESSION'] not in objs:
            logger.error('Bad request to set selected stream configuration.')
            return

        session = tlv.decode(objs[SELECTED_STREAM_CONFIGURATION_TYPES['SESSION']])

        request_type = session[b'\x02'][0]
        if request_type == 1:
            self._start_stream(objs, reconfigure=False)
        elif request_type == 0:
            self._stop_stream(objs)
        elif request_type == 4:
            self._start_stream(objs, reconfigure=True)
        else:
            logger.error('Unknown request type %d', request_type)

    def set_endpoints(self, value):
        '''Configure streaming endpoints.

        Called when iOS sets the SetupEndpoints Characteristic.
        '''
        objs = tlv.decode(value)
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

        logger.debug('Received endpoint configuration:'
                     '\naddress: ' + address +
                     '\ntarget_video_port: ' + target_video_port +
                     '\ntarget_audio_port: ' + target_audio_port +
                     '\nvideo_crypto_suite: ' + video_crypto_suite +
                     '\nvideo_master_key: ' + video_master_key +
                     '\nvideo_master_salt: ' + video_master_salt +
                     '\naudio_crypto_suite: ' + audio_crypto_suite +
                     '\naudio_master_key: ' + audio_master_key +
                     '\naudio_master_salt: ' + audio_master_salt)

        if(self.support_srtp):
            video_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM['CRYPTO'], SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80'],
                SETUP_SRTP_PARAM['MASTER_KEY'], video_master_key,
                SETUP_SRTP_PARAM['MASTER_SALT'], video_master_salt)

            audio_srtp_tlv = tlv.encode(
                SETUP_SRTP_PARAM['CRYPTO'], SRTP_CRYPTO_SUITES['AES_CM_128_HMAC_SHA1_80'],
                SETUP_SRTP_PARAM['MASTER_KEY'], audio_master_key,
                SETUP_SRTP_PARAM['MASTER_SALT'], audio_master_salt)
        else:
            video_srtp_tlv = self.NO_SRTP
            audio_srtp_tlv = self.NO_SRTP

        video_ssrc = os.urandom(4)
        audio_ssrc = os.urandom(4)

        res_address_tlv = tlv.encode(
            SETUP_ADDR_INFO['ADDRESS_VER'], self.stream_address_isv6,
            SETUP_ADDR_INFO['ADDRESS'], self.stream_address.encode('utf-8'),
            SETUP_ADDR_INFO['VIDEO_RTP_PORT'], target_video_port,
            SETUP_ADDR_INFO['AUDIO_RTP_PORT'], target_audio_port)

        response_tlv = tlv.encode(
            SETUP_TYPES['SESSION_ID'], self.session_id,
            SETUP_TYPES['STATUS'], SETUP_STATUS['SUCCESS'],
            SETUP_TYPES['ADDRESS'], res_address_tlv,
            SETUP_TYPES['VIDEO_SRTP_PARAM'], video_srtp_tlv,
            SETUP_TYPES['AUDIO_SRTP_PARAM'], audio_srtp_tlv,
            SETUP_TYPES['VIDEO_SSRC'], video_ssrc,
            SETUP_TYPES['AUDIO_SSRC'], audio_ssrc)

        self.pending_sessions[session_id] = {
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

        endpoints = self.management_service.get_characteristic('SetupEndpoints')
        endpoints.set_value(response_tlv)
