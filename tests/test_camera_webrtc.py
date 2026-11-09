"""Tests for the HKSV 17.99 WebRTC streaming of pyhap.camera."""

from uuid import UUID

from pyhap import hksv3
from pyhap.camera_webrtc import remote_tier

WEBRTC = "CameraWebRTCStreamManagement"
SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 50000)
MEDIUM = hksv3.VideoStreamTier(2, 3, 768, 1280, 720, 30, 800)
LOW = hksv3.VideoStreamTier(3, 4, 180, 640, 360, 15, 190)
ANSWER = "v=0\r\na=setup:active\r\n"
KEY = hksv3.SFrameKey(bytes(32), 7)


def test_remote_tier(stream_sensor: hksv3.SensorConfiguration) -> None:
    """Test the remote tier is the smallest of a quality, else the smallest."""
    tiers = hksv3.tiers_from_sensor(stream_sensor)
    assert remote_tier(tiers, hksv3.VideoQuality.MEDIUM) == MEDIUM
    assert remote_tier(tiers, hksv3.VideoQuality.HIGHEST) == LOW
