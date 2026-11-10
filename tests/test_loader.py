"""Tests for pyhap.loader."""

from unittest.mock import patch
from uuid import UUID

import pytest

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import (
    PROP_ADMIN_ONLY_ACCESS,
    PROP_FORMAT,
    PROP_MAX_VALUE,
    PROP_MIN_VALUE,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.loader import Loader, get_loader
from pyhap.service import Service


def test_loader_char() -> None:
    """Test if method returns a Characteristic object."""
    loader = Loader()

    with pytest.raises(KeyError):
        loader.get_char("Not a char")

    char_name = loader.get_char("Name")
    assert char_name is not None
    assert isinstance(char_name, Characteristic)


def test_loader_get_char_error() -> None:
    """Test if errors are thrown for invalid dictionary entries."""
    loader = Loader.from_dict(char_dict={"Char": None})
    assert loader.char_types == {"Char": None}
    assert loader.serv_types == {}
    json_dicts = (
        {"Format": "int", "Permissions": "read"},
        {"Format": "int", "UUID": "123456"},
        {"Permissions": "read", "UUID": "123456"},
    )

    for case in json_dicts:
        loader.char_types["Char"] = case
        with pytest.raises(KeyError):
            loader.get_char("Char")


def test_loader_service() -> None:
    """Test if method returns a Service object."""
    loader = Loader()

    with pytest.raises(KeyError):
        loader.get_service("Not a service")

    serv_acc_info = loader.get_service("AccessoryInformation")
    assert serv_acc_info is not None
    assert isinstance(serv_acc_info, Service)


def test_loader_service_error() -> None:
    """Test if errors are thrown for invalid dictionary entries."""
    loader = Loader.from_dict(serv_dict={"Service": None})
    assert loader.char_types == {}
    assert loader.serv_types == {"Service": None}
    json_dicts = ({"RequiredCharacteristics": ["Char 1", "Char 2"]}, {"UUID": "123456"})

    for case in json_dicts:
        loader.serv_types["Service"] = case
        with pytest.raises(KeyError):
            loader.get_service("Service")


def test_get_loader() -> None:
    """Test if method returns the preloaded loader object."""
    loader = get_loader()
    assert isinstance(loader, Loader)
    assert loader.char_types is not ({} or None)
    assert loader.serv_types is not ({} or None)

    loader2 = Loader(path_char=CHARACTERISTICS_FILE, path_service=SERVICES_FILE)
    assert loader.char_types == loader2.char_types
    assert loader.serv_types == loader2.serv_types

    assert get_loader() == loader


def test_loader_admin_only_not_advertised() -> None:
    """Test the admin only marker stays out of the HAP representation."""
    char = Loader().get_char("StreamingEnabled")
    assert char.properties[PROP_ADMIN_ONLY_ACCESS] == ["write"]
    with patch.object(char, "broker") as mock_broker:
        mock_broker.iid_manager.get_iid.return_value = 2
        hap_repr = char.to_HAP()
    assert PROP_ADMIN_ONLY_ACCESS not in hap_repr


# Accesses only admin controllers may use, not part of the HAP representation.
ADMIN_ONLY_ACCESS = "AdminOnlyAccess"

# Camera services and characteristics of the HomeKit Secure Video 17.99 guide:
# name -> (short UUID, format, permissions, AdminOnlyAccess).
HKSV3_CHARS = {
    "SensorUUID": ("805B", "data", {"pr"}, None),
    "MotionEnabled": ("8087", "bool", {"pr", "pw", "ev", "tw"}, ["write"]),
    "SupportedVideoStreamTiers": ("8043", "tlv8", {"pr", "ev"}, None),
    "SupportedAudioStreamTiers": ("8044", "tlv8", {"pr", "ev"}, None),
    "CameraCapabilities": ("8011", "tlv8", {"pr"}, None),
    "ContributingSensors": ("8086", "tlv8", {"pr", "ev"}, None),
    "CameraKey": ("8051", "tlv8", {"pw", "tw"}, None),
    "CameraKeyID": ("8052", "tlv8", {"pr", "ev"}, None),
    "BufferUploadCommand": ("8013", "tlv8", {"pr", "pw", "wr"}, None),
    "BufferActivityCommand": ("8017", "tlv8", {"pw"}, None),
    "BufferEventCommand": ("8014", "tlv8", {"pr", "pw", "wr"}, None),
    "BufferEventSequenceNumber": ("8015", "uint32", {"pr", "ev"}, None),
    "CameraRecordingPublishingPoint": ("8016", "tlv8", {"pr", "pw"}, None),
    "CameraZones": ("8022", "tlv8", {"pr", "pw"}, None),
    "StreamingEnabled": ("8041", "bool", {"pr", "pw", "ev", "tw"}, ["write"]),
    "RTPStreamingControl": ("8045", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCSolicitOffer": ("8053", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCProvideAnswer": ("8054", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCStreamingControl": ("8056", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCNumberOfActiveSessions": ("8057", "uint8", {"pr", "ev"}, None),
    "WebRTCReoffer": ("8058", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCUpdateSession": ("805C", "tlv8", {"pr", "pw", "wr"}, None),
    "WebRTCSupportedVideoStreamTiers": ("8059", "tlv8", {"pr", "ev"}, None),
    "WebRTCSupportedAudioStreamTiers": ("805A", "tlv8", {"pr", "ev"}, None),
    "CameraClientCSR": ("8081", "tlv8", {"pr", "pw", "wr"}, None),
    "CameraClientCertificate": ("8082", "tlv8", {"pr", "pw", "tw"}, None),
    "CameraClientCertificateStatus": ("8083", "tlv8", {"pr", "ev"}, None),
}

# name -> (short UUID, required, optional)
HKSV3_SERVICES = {
    "CameraCapabilities": (
        "8010",
        ["Version", "CameraCapabilities"],
        ["ManuallyDisabled"],
    ),
    "CameraGlobalOperatingMode": (
        "8032",
        ["HomeKitCameraActive", "StreamingEnabled", "CameraOperatingModeIndicator"],
        ["ManuallyDisabled", "NightVision", "ThirdPartyCameraActive"],
    ),
    "CameraMotionZones": ("8021", ["Version", "Active", "CameraZones"], []),
    "CameraBufferManagement": (
        "8000",
        [
            "BufferUploadCommand",
            "BufferActivityCommand",
            "BufferEventCommand",
            "BufferEventSequenceNumber",
            "CameraRecordingPublishingPoint",
        ],
        [],
    ),
    "CameraMultiTierRTPStreamManagement": (
        "8031",
        [
            "StreamingEnabled",
            "StatusActive",
            "SupportedVideoStreamTiers",
            "SupportedAudioStreamTiers",
            "SupportedRTPConfiguration",
            "SetupEndpoints",
            "RTPStreamingControl",
            "SensorUUID",
        ],
        [],
    ),
    "CameraWebRTCStreamManagement": (
        "8033",
        [
            "WebRTCSolicitOffer",
            "WebRTCProvideAnswer",
            "WebRTCStreamingControl",
            "WebRTCNumberOfActiveSessions",
            "WebRTCReoffer",
            "WebRTCUpdateSession",
            "WebRTCSupportedVideoStreamTiers",
            "WebRTCSupportedAudioStreamTiers",
            "StreamingEnabled",
            "SensorUUID",
        ],
        [],
    ),
    "CameraKeyManagement": ("8050", ["CameraKey", "CameraKeyID"], []),
    "CameraClientCertificateManagement": (
        "8080",
        [
            "CameraClientCSR",
            "CameraClientCertificate",
            "CameraClientCertificateStatus",
        ],
        [],
    ),
}


def _hap_uuid(short: str) -> UUID:
    return UUID(f"0000{short}-0000-1000-8000-0026BB765291")


@pytest.mark.parametrize("name", HKSV3_CHARS)
def test_loader_hksv3_char(name: str) -> None:
    """Test the HKSV 17.99 characteristics match the guide."""
    short, char_format, permissions, admin_only_access = HKSV3_CHARS[name]
    char = Loader().get_char(name)
    assert char.type_id == _hap_uuid(short)
    assert char.properties[PROP_FORMAT] == char_format
    assert set(char.properties[PROP_PERMISSIONS]) == permissions
    assert char.properties.get(ADMIN_ONLY_ACCESS) == admin_only_access


def test_loader_hksv3_admin_only_not_advertised() -> None:
    """Test the admin only marker stays out of the HAP representation."""
    char = Loader().get_char("StreamingEnabled")
    with patch.object(char, "broker") as mock_broker:
        mock_broker.iid_manager.get_iid.return_value = 2
        hap_repr = char.to_HAP()
    assert hap_repr["perms"] == ["pr", "pw", "ev", "tw"]
    assert ADMIN_ONLY_ACCESS not in hap_repr


def test_loader_hksv3_active_sessions_range() -> None:
    """Test the WebRTC session count carries the guide's value range."""
    char = Loader().get_char("WebRTCNumberOfActiveSessions")
    assert char.properties[PROP_MIN_VALUE] == 0
    assert char.properties[PROP_MAX_VALUE] == 255


@pytest.mark.parametrize("name", HKSV3_SERVICES)
def test_loader_hksv3_service(name: str) -> None:
    """Test the HKSV 17.99 services match the guide."""
    short, required, optional = HKSV3_SERVICES[name]
    loader = Loader()
    service = loader.get_service(name)
    assert service.type_id == _hap_uuid(short)
    assert [char.display_name for char in service.characteristics] == required
    assert loader.serv_types[name]["OptionalCharacteristics"] == optional
    assert set(optional) <= set(loader.char_types)


def test_loader_motion_sensor_hksv3_optional() -> None:
    """Test the motion sensor gains the HKSV 17.99 chars only as optional."""
    loader = Loader()
    optional = loader.serv_types["MotionSensor"]["OptionalCharacteristics"]
    assert {"ContributingSensors", "MotionEnabled"} <= set(optional)
    service = loader.get_service("MotionSensor")
    assert [char.display_name for char in service.characteristics] == ["MotionDetected"]


@pytest.mark.parametrize(
    ("name", "permissions"),
    [
        ("CameraOperatingModeIndicator", {"pr", "pw", "ev", "tw"}),
        ("NightVision", {"pr", "pw", "ev", "tw"}),
        ("ThirdPartyCameraActive", {"pr", "ev"}),
    ],
)
def test_loader_camera_operating_mode_permissions(
    name: str, permissions: set[str]
) -> None:
    """Test the operating mode settings shared with the HKSV 17.99 services."""
    char = Loader().get_char(name)
    assert set(char.properties[PROP_PERMISSIONS]) == permissions
