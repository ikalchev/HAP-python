"""Tests for pyhap.camera."""
from pyhap import camera


_OPTIONS = {
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
                'type': 'OPUS',
                'samplerate': 24,
            },
            {
                'type': 'AAC-eld',
                'samplerate': 16
            }
        ],
    },
    "srtp": True,
    "address": "192.168.1.226",
}


def test_init(mock_driver):
    """Test that the camera init properly computes TLV values"""
    acc = camera.Camera(_OPTIONS, mock_driver, 'Camera')

    management = acc.get_service('CameraRTPStreamManagement')

    assert management.get_characteristic('SupportedRTPConfiguration').get_value() == \
        'AgEA'
    assert (management.get_characteristic('SupportedVideoStreamConfiguration')
                      .get_value() ==
        'AX4BAQACCQMBAAEBAAIBAAMMAQJAAQIC8AADAg8AAwwBAgAEAgIAAwMCHgADDAECgAICAuA'
        'BAwIeAAMMAQKAAgICaAEDAh4AAwwBAuABAgJoAQMCHgADDAEC4AECAg4BAwIeAAMMAQJAAQ'
        'IC8AADAh4AAwwBAkABAgK0AAMCHgA=')
    assert (management.get_characteristic('SupportedAudioStreamConfiguration')
                      .get_value() ==
        'AQ4BAQMCCQEBAQIBAAMBAgEOAQECAgkBAQECAQADAQECAQA=')
