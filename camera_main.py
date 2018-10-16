"""An example of how to setup and start a Camera."""
import logging
import signal

from pyhap.accessory_driver import AccessoryDriver
from pyhap import camera

logging.basicConfig(level=logging.DEBUG, format='[%(module)s] %(message)s')


# Set up the configurations that your camera supports. Home app will choose among these
# when negotiating a session.
OPTIONS = {
    'video': {
        'codec': {
            'profiles': [
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES['BASELINE'],
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES['MAIN'],
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES['HIGH']
            ],
            'levels': [
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE3_1'],
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE3_2'],
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE4_0'],
            ],
        },
        'resolutions': [
            # Width, Height, framerate
            [1920, 1080, 30],
            [320, 240, 15],
            [1280, 960, 30],
            [1280, 720, 30],
            [1024, 768, 30],
            [640, 480, 30],
            [640, 360, 30],
            [480, 360, 30],
            [480, 270, 30],
            [320, 240, 30],
            [320, 180, 30],
        ],
    },
    'audio': {
        'codecs': [
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

    'srtp': True,
    'address': '192.168.1.226',
}


def main():
    """Start the Camera"""
    # Start the accessory on port 51826
    driver = AccessoryDriver(port=51826)
    acc = camera.Camera(OPTIONS, driver, 'Camera')
    driver.add_accessory(accessory=acc)

    # We want KeyboardInterrupts and SIGTERM (kill) to be handled by the driver itself,
    # so that it can gracefully stop the accessory, server and advertising.
    signal.signal(signal.SIGINT, driver.signal_handler)
    signal.signal(signal.SIGTERM, driver.signal_handler)
    # Start it!
    driver.start()

if __name__ == '__main__':
    main()
