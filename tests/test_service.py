"""Tests for pyhap.service."""

from unittest.mock import Mock, call, patch
from uuid import uuid1

import pytest

from pyhap.characteristic import (
    HAP_FORMAT_INT,
    HAP_PERMISSION_READ,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.delegates import CharacteristicDelegate, ServiceDelegate
from pyhap.service import Service

CHAR_PROPS = {
    PROP_FORMAT: HAP_FORMAT_INT,
    PROP_PERMISSIONS: HAP_PERMISSION_READ,
}


def get_chars() -> list[Characteristic]:
    """Return example char objects."""
    c1 = Characteristic("Char 1", uuid1(), CHAR_PROPS)
    c2 = Characteristic("Char 2", uuid1(), CHAR_PROPS)
    return [c1, c2]


def test_repr() -> None:
    """Test service representation."""
    service = Service(uuid1(), "TestService", unique_id="my_service_unique_id")
    service.characteristics = [get_chars()[0]]
    assert (
        repr(service)
        == "<service display_name=TestService unique_id=my_service_unique_id"
        " chars={'Char 1': 0}>"
    )


def test_service_with_unique_id() -> None:
    """Test service with unique_id."""
    service = Service(uuid1(), "TestService", unique_id="service_unique_id")
    assert service.unique_id == "service_unique_id"


def test_add_characteristic() -> None:
    """Test adding characteristics to a service."""
    service = Service(uuid1(), "Test Service")
    chars = get_chars()
    service.add_characteristic(*chars)
    for char_service, char_original in zip(service.characteristics, chars, strict=True):
        assert char_service == char_original

    service.add_characteristic(chars[0])
    assert len(service.characteristics) == 2


def test_get_characteristic() -> None:
    """Test getting a characteristic from a service."""
    service = Service(uuid1(), "Test Service")
    chars = get_chars()
    service.characteristics = chars
    assert service.get_characteristic("Char 1") == chars[0]
    with pytest.raises(ValueError):
        service.get_characteristic("Not found")


def test_configure_char() -> None:
    """Test preconfiguring a characteristic from a service."""
    pyhap_char = "pyhap.characteristic.Characteristic"

    service = Service(uuid1(), "Test Service")
    chars = get_chars()
    service.characteristics = chars

    with pytest.raises(ValueError):
        service.configure_char("Char not found")
    assert service.configure_char("Char 1") == chars[0]

    with (
        patch(pyhap_char + ".override_properties") as mock_override_prop,
        patch(pyhap_char + ".set_value") as mock_set_value,
    ):
        service.configure_char("Char 1")
        mock_override_prop.assert_not_called()
        mock_set_value.assert_not_called()
        assert service.get_characteristic("Char 1").setter_callback is None

    with patch(pyhap_char + ".override_properties") as mock_override_prop:
        new_properties = {"Format": "string"}
        new_valid_values = {0: "on", 1: "off"}
        service.configure_char("Char 1", properties=new_properties)
        mock_override_prop.assert_called_with(new_properties, None)
        service.configure_char("Char 1", valid_values=new_valid_values)
        mock_override_prop.assert_called_with(None, new_valid_values)
        service.configure_char(
            "Char 1", properties=new_properties, valid_values=new_valid_values
        )
        mock_override_prop.assert_called_with(new_properties, new_valid_values)

    with patch(pyhap_char + ".set_value") as mock_set_value:
        new_value = 1
        service.configure_char("Char 1", value=new_value)
        mock_set_value.assert_called_with(1, should_notify=False)

    new_setter_callback = "Test callback"
    service.configure_char("Char 1", setter_callback=new_setter_callback)
    assert service.get_characteristic("Char 1").setter_callback == new_setter_callback


def test_is_primary_service() -> None:
    """Test setting is_primary_service on a service."""
    service = Service(uuid1(), "Test Service")

    assert service.is_primary_service is None

    service.is_primary_service = True
    assert service.is_primary_service is True

    service.is_primary_service = False
    assert service.is_primary_service is False


def test_add_linked_service() -> None:
    """Test adding linked service to a service."""
    service = Service(uuid1(), "Test Service")
    assert len(service.linked_services) == 0

    linked_service = Service(uuid1(), "Test Linked Service")
    service.broker = Mock()
    service.add_linked_service(linked_service)

    assert len(service.linked_services) == 1
    assert service.linked_services[0] == linked_service


def test_to_HAP() -> None:
    """Test created HAP representation of a service."""
    uuid = uuid1()
    pyhap_char_to_HAP = "pyhap.characteristic.Characteristic.to_HAP"

    service = Service(uuid, "Test Service")
    service.characteristics = get_chars()
    with (
        patch(pyhap_char_to_HAP) as mock_char_HAP,
        patch.object(service, "broker") as mock_broker,
    ):
        mock_iid = mock_broker.iid_manager.get_iid
        mock_iid.return_value = 2
        mock_char_HAP.side_effect = ("Char 1", "Char 2")
        hap_repr = service.to_HAP()
        mock_iid.assert_called_with(service)

    assert hap_repr == {
        "iid": 2,
        "type": str(uuid).upper(),
        "characteristics": ["Char 1", "Char 2"],
    }


def test_linked_service_to_HAP() -> None:
    """Test created HAP representation of a service."""
    uuid = uuid1()
    pyhap_char_to_HAP = "pyhap.characteristic.Characteristic.to_HAP"

    service = Service(uuid, "Test Service")
    linked_service = Service(uuid1(), "Test Linked Service")
    service.broker = Mock()
    service.add_linked_service(linked_service)
    service.characteristics = get_chars()
    with (
        patch(pyhap_char_to_HAP) as mock_char_HAP,
        patch.object(service, "broker") as mock_broker,
        patch.object(linked_service, "broker") as mock_linked_broker,
    ):
        mock_iid = mock_broker.iid_manager.get_iid
        mock_iid.return_value = 2
        mock_linked_iid = mock_linked_broker.iid_manager.get_iid
        mock_linked_iid.return_value = 3
        mock_char_HAP.side_effect = ("Char 1", "Char 2")
        hap_repr = service.to_HAP()
        mock_iid.assert_called_with(service)
        assert hap_repr == {
            "iid": 2,
            "type": str(uuid).upper(),
            "characteristics": ["Char 1", "Char 2"],
            "linked": [mock_linked_iid()],
        }
        # Verify we can readd it without dupes
        service.add_linked_service(linked_service)
        assert hap_repr == {
            "iid": 2,
            "type": str(uuid).upper(),
            "characteristics": ["Char 1", "Char 2"],
            "linked": [mock_linked_iid()],
        }


def test_is_primary_service_to_HAP() -> None:
    """Test created HAP representation of primary service."""
    uuid = uuid1()
    pyhap_char_to_HAP = "pyhap.characteristic.Characteristic.to_HAP"

    service = Service(uuid, "Test Service")
    service.characteristics = get_chars()
    service.is_primary_service = True
    with (
        patch(pyhap_char_to_HAP) as mock_char_HAP,
        patch.object(service, "broker") as mock_broker,
    ):
        mock_iid = mock_broker.iid_manager.get_iid
        mock_iid.return_value = 2
        mock_char_HAP.side_effect = ("Char 1", "Char 2")
        hap_repr = service.to_HAP()
        mock_iid.assert_called_with(service)

    assert hap_repr == {
        "iid": 2,
        "type": str(uuid).upper(),
        "characteristics": ["Char 1", "Char 2"],
        "primary": True,
    }


def test_from_dict() -> None:
    """Test creating a service from a dictionary."""
    uuid = uuid1()
    chars = get_chars()
    mock_char_loader = Mock()
    mock_char_loader.get_char.side_effect = chars

    json_dict = {
        "UUID": str(uuid),
        "RequiredCharacteristics": {
            "Char 1",
            "Char 2",
        },
    }

    service = Service.from_dict("Test Service", json_dict, mock_char_loader)
    assert service.display_name == "Test Service"
    assert service.type_id == uuid
    assert service.characteristics == chars

    mock_char_loader.get_char.assert_has_calls(
        [call("Char 1"), call("Char 2")], any_order=True
    )


class _AsyncGate(ServiceDelegate):
    """A service delegate overriding the grouped gate."""


class _AsyncCharDelegate(CharacteristicDelegate):
    """A characteristic delegate."""


def test_service_delegate_instance() -> None:
    """An assigned service delegate is the service's delegate."""
    service = Service(uuid1())
    delegate = _AsyncGate()

    service.delegate = delegate

    assert service.delegate is delegate


def test_service_delegate_sync_override_fails() -> None:
    """A grouped on_set that is not async fails the assignment."""
    service = Service(uuid1())

    class _SyncGate(ServiceDelegate):
        def on_set(  # ty: ignore[invalid-method-override]
            self, values: object, context: object
        ) -> None:
            pass

    with pytest.raises(TypeError, match="on_set of a service is not a coroutine"):
        service.delegate = _SyncGate()


def test_configure_char_wires_delegate() -> None:
    """configure_char wires a delegate instance or class onto the char."""
    service = Service(uuid1())
    service.add_characteristic(*get_chars())
    delegate = _AsyncCharDelegate()

    configured = service.configure_char("Char 1", delegate=delegate)

    assert configured.delegate is delegate

    configured_by_class = service.configure_char("Char 2", delegate=_AsyncCharDelegate)

    assert isinstance(configured_by_class.delegate, _AsyncCharDelegate)


def test_configure_char_without_delegate() -> None:
    """configure_char without a delegate leaves the char without one."""
    service = Service(uuid1())
    service.add_characteristic(*get_chars())

    configured = service.configure_char("Char 1", value=5)

    assert configured.delegate is None
    assert configured.value == 5
