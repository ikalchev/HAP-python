"""Tests for pyhap.characteristic."""
from unittest.mock import ANY, MagicMock, Mock, patch
from uuid import uuid1

import pytest

from pyhap.characteristic import (
    CHAR_PROGRAMMABLE_SWITCH_EVENT,
    HAP_FORMAT_DEFAULTS,
    HAP_FORMAT_FLOAT,
    HAP_FORMAT_INT,
    HAP_FORMAT_UINT8,
    HAP_FORMAT_UINT16,
    HAP_FORMAT_UINT32,
    HAP_FORMAT_UINT64,
    HAP_PERMISSION_READ,
    Characteristic,
)

PROPERTIES = {"Format": HAP_FORMAT_INT, "Permissions": [HAP_PERMISSION_READ]}
PROPERTIES_FLOAT = {"Format": HAP_FORMAT_FLOAT, "Permissions": [HAP_PERMISSION_READ]}

HAP_FORMAT_INTS = [
    HAP_FORMAT_INT,
    HAP_FORMAT_UINT8,
    HAP_FORMAT_UINT16,
    HAP_FORMAT_UINT32,
    HAP_FORMAT_UINT64,
]


def get_char(props, valid=None, min_value=None, max_value=None):
    """Return a char object with given parameters."""
    if valid:
        props["ValidValues"] = valid
    if min_value:
        props["minValue"] = min_value
    if max_value:
        props["maxValue"] = max_value
    return Characteristic(display_name="Test Char", type_id=uuid1(), properties=props)


def test_repr():
    """Test representation of a characteristic."""
    char = get_char(PROPERTIES.copy())
    del char.properties["Permissions"]
    assert (
        repr(char) == "<characteristic display_name=Test Char unique_id=None value=0 "
        "properties={'Format': 'int'}>"
    )


def test_char_with_unique_id():
    """Test Characteristic with unique_id."""
    service = Characteristic(
        display_name="Test Char",
        type_id=uuid1(),
        properties={"Format": "int"},
        unique_id="123",
    )
    assert service.unique_id == "123"


def test_default_value():
    """Test getting the default value for a specific format."""
    char = get_char(PROPERTIES.copy())
    assert char.value == HAP_FORMAT_DEFAULTS[PROPERTIES["Format"]]


def test_get_default_value():
    """Test getting the default value for valid values."""
    valid_values = {"foo": 2, "bar": 3}
    char = get_char(PROPERTIES.copy(), valid=valid_values)
    assert char.value == 2
    assert char.value in valid_values.values()
    char = get_char(PROPERTIES.copy(), min_value=3, max_value=10)
    assert char.value == 3


def test_to_valid_value():
    """Test function to test if value is valid and saved correctly."""
    char = get_char(
        PROPERTIES.copy(), valid={"foo": 2, "bar": 3}, min_value=2, max_value=7
    )
    with pytest.raises(ValueError):
        char.valid_value_or_raise(1)
    assert char.to_valid_value(2) == 2

    del char.properties["ValidValues"]
    for value in ("2", None):
        with pytest.raises(ValueError):
            char.to_valid_value(value)
    assert char.to_valid_value(1) == 2
    assert char.to_valid_value(5) == 5
    assert char.to_valid_value(8) == 7

    char.properties["Format"] = "string"
    assert char.to_valid_value(24) == "24"

    char.properties["Format"] = "bool"
    assert char.to_valid_value(1) is True
    assert char.to_valid_value(0) is False

    char.properties["Format"] = "dictionary"
    assert char.to_valid_value({"a": 1}) == {"a": 1}


def test_override_properties_properties():
    """Test if overriding the properties works."""
    new_properties = {"minValue": 10, "maxValue": 20, "step": 1}
    char = get_char(PROPERTIES.copy(), min_value=0, max_value=1)
    char.override_properties(properties=new_properties)
    assert char.properties["minValue"] == new_properties["minValue"]
    assert char.properties["maxValue"] == new_properties["maxValue"]
    assert char.properties["step"] == new_properties["step"]


def test_override_properties_exceed_max_length():
    """Test if overriding the properties with invalid values throws."""
    new_properties = {"minValue": 10, "maxValue": 20, "step": 1, "maxLen": 5000}
    char = get_char(PROPERTIES.copy(), min_value=0, max_value=1)
    with pytest.raises(ValueError):
        char.override_properties(properties=new_properties)


def test_override_properties_valid_values():
    """Test if overriding the properties works for valid values."""
    new_valid_values = {"foo2": 2, "bar2": 3}
    char = get_char(PROPERTIES.copy(), valid={"foo": 1, "bar": 2})
    char.override_properties(valid_values=new_valid_values)
    assert char.properties["ValidValues"] == new_valid_values


def test_override_properties_error():
    """Test that method throws an error if no arguments have been passed."""
    char = get_char(PROPERTIES.copy())
    with pytest.raises(ValueError):
        char.override_properties()


@pytest.mark.parametrize("int_format", HAP_FORMAT_INTS)
def test_set_value_invalid_min_step(int_format):
    """Test setting the value of a characteristic that is outside the minStep."""
    path = "pyhap.characteristic.Characteristic.notify"
    props = PROPERTIES.copy()
    props["Format"] = int_format
    props["minStep"] = 2
    char = get_char(props, min_value=4, max_value=8)

    with patch(path) as mock_notify:
        char.set_value(5.55)
        # Ensure floating point is dropped on an int property
        # Ensure value is rounded to match minStep
        assert char.value == 6
        assert mock_notify.called is False

        char.set_value(6.00)
        # Ensure floating point is dropped on an int property
        # Ensure value is rounded to match minStep
        assert char.value == 6
        assert mock_notify.called is False

        char.broker = Mock()
        char.set_value(8, should_notify=False)
        assert char.value == 8
        assert mock_notify.called is False

        char.set_value(1)
        # Ensure value is raised to meet minValue
        assert char.value == 4
        assert mock_notify.call_count == 1

        # No change should not generate another notify
        char.set_value(4)
        assert char.value == 4
        assert mock_notify.call_count == 1


def test_set_value_invalid_min_float():
    """Test setting the value of a characteristic that is outside the minStep."""
    props = PROPERTIES.copy()
    props["Format"] = HAP_FORMAT_FLOAT
    props["minStep"] = 0.1
    char = get_char(props, min_value=0, max_value=26)

    char.set_value(5.55)
    # Ensure value is rounded to match minStep
    assert char.value == 5.5

    char.set_value(22.2)
    # Ensure value is rounded to match minStep
    assert char.value == 22.2

    char.set_value(22.200000)
    # Ensure value is rounded to match minStep
    assert char.value == 22.2

    props = PROPERTIES.copy()
    props["Format"] = HAP_FORMAT_FLOAT
    props["minStep"] = 0.00001
    char = get_char(props, min_value=0, max_value=26)

    char.set_value(5.55)
    # Ensure value is rounded to match minStep
    assert char.value == 5.55

    char.set_value(22.2)
    # Ensure value is rounded to match minStep
    assert char.value == 22.2

    char.set_value(22.200000)
    # Ensure value is rounded to match minStep
    assert char.value == 22.2

    char.set_value(22.12345678)
    # Ensure value is rounded to match minStep
    assert char.value == 22.12346

    char.set_value(0)
    # Ensure value is not modified
    assert char.value == 0

    char.value = 99
    assert char.value == 99
    char.set_value(0)
    assert char.value == 0


@pytest.mark.parametrize("int_format", HAP_FORMAT_INTS)
def test_set_value_int(int_format):
    """Test setting the value of a characteristic."""
    path = "pyhap.characteristic.Characteristic.notify"
    props = PROPERTIES.copy()
    props["Format"] = int_format
    char = get_char(props, min_value=3, max_value=7)

    with patch(path) as mock_notify:
        char.set_value(5.55)
        # Ensure floating point is dropped on an int property
        assert char.value == 5
        assert mock_notify.called is False

        char.broker = Mock()
        char.set_value(8, should_notify=False)
        assert char.value == 7
        assert mock_notify.called is False

        char.set_value(1)
        assert char.value == 3
        assert mock_notify.call_count == 1

        # No change should not generate another notify
        char.set_value(3)
        assert char.value == 3
        assert mock_notify.call_count == 1


def test_set_value_immediate():
    """Test setting the value of a characteristic generates immediate notify."""
    char = Characteristic(
        display_name="Switch Event",
        type_id=CHAR_PROGRAMMABLE_SWITCH_EVENT,
        properties=PROPERTIES.copy(),
    )
    assert char.value is None

    publish_mock = Mock()
    char.broker = Mock(publish=publish_mock)

    char.set_value(0)
    assert char.value is None
    publish_mock.assert_called_with(0, char, None, True)

    char.set_value(1)
    assert char.value is None
    publish_mock.assert_called_with(1, char, None, True)


def test_switch_event_always_serializes_to_null_via_set_value():
    """Test that the switch event char is always null."""
    char = Characteristic(
        display_name="Switch Event",
        type_id=CHAR_PROGRAMMABLE_SWITCH_EVENT,
        properties=PROPERTIES.copy(),
    )
    assert char.value is None
    char.broker = MagicMock()

    assert char.to_HAP()["value"] is None
    char.set_value(1)
    assert char.to_HAP()["value"] is None


def test_switch_event_always_serializes_to_null_via_client_update_value():
    """Test that the switch event char is always null."""
    char = Characteristic(
        display_name="Switch Event",
        type_id=CHAR_PROGRAMMABLE_SWITCH_EVENT,
        properties=PROPERTIES.copy(),
    )
    assert char.value is None
    char.broker = MagicMock()

    assert char.to_HAP()["value"] is None
    char.client_update_value(1)
    assert char.to_HAP()["value"] is None


def test_set_value_float():
    """Test setting the value of a characteristic."""
    path = "pyhap.characteristic.Characteristic.notify"
    char = get_char(PROPERTIES_FLOAT.copy(), min_value=2.55, max_value=7.5)

    with patch(path) as mock_notify:
        char.set_value(5.55)
        # Ensure floating point is preserved on a float property
        assert char.value == 5.55
        assert mock_notify.called is False

        char.broker = Mock()
        char.set_value(8, should_notify=False)
        assert char.value == 7.5
        assert mock_notify.called is False

        char.set_value(1)
        assert char.value == 2.55
        assert mock_notify.call_count == 1


def test_client_update_value():
    """Test updating the characteristic value with call from the driver."""
    path_notify = "pyhap.characteristic.Characteristic.notify"
    char = get_char(PROPERTIES.copy())

    with patch(path_notify) as mock_notify:
        char.client_update_value(4)
        assert char.value == 4
        with patch.object(char, "setter_callback") as mock_callback:
            char.client_update_value(3)

    assert char.value == 3
    assert mock_notify.call_count == 2
    mock_callback.assert_called_with(3)

    with patch(path_notify) as mock_notify:
        char.client_update_value(9, "mock_client_addr")
        assert char.value == 9
        mock_notify.assert_called_once_with("mock_client_addr")
        assert len(mock_notify.mock_calls) == 1

        # Same value, do not call again
        char.client_update_value(9, "mock_client_addr")
        assert char.value == 9
        assert len(mock_notify.mock_calls) == 1

        # New value, should notify
        char.client_update_value(12, "mock_client_addr")
        assert char.value == 12
        assert len(mock_notify.mock_calls) == 2

        # Same value, do not call again
        char.client_update_value(12, "mock_client_addr")
        assert char.value == 12
        assert len(mock_notify.mock_calls) == 2

        # New value, should notify
        char.client_update_value(9, "mock_client_addr")
        assert char.value == 9
        assert len(mock_notify.mock_calls) == 3


def test_client_update_value_with_invalid_value():
    """Test updating the characteristic value with call from the driver with invalid values."""
    char = get_char(PROPERTIES.copy(), valid={"foo": 0, "bar": 2, "baz": 1})

    with patch.object(char, "broker"):
        with pytest.raises(ValueError):
            char.client_update_value(4)

        char.allow_invalid_client_values = True
        char.client_update_value(4)


def test_notify():
    """Test if driver is notified correctly about a changed characteristic."""
    char = get_char(PROPERTIES.copy())

    char.value = 2
    with pytest.raises(AttributeError):
        char.notify()

    with patch.object(char, "broker") as mock_broker:
        char.notify()
    mock_broker.publish.assert_called_with(2, char, None, False)

    with patch.object(char, "broker") as mock_broker:
        char.notify("mock_client_addr")
    mock_broker.publish.assert_called_with(2, char, "mock_client_addr", False)


def test_to_HAP_numberic():
    """Test created HAP representation for numeric formats."""
    char = get_char(PROPERTIES.copy(), min_value=1, max_value=2)
    with patch.object(char, "broker") as mock_broker:
        mock_iid = mock_broker.iid_manager.get_iid
        mock_iid.return_value = 2
        hap_repr = char.to_HAP()
        mock_iid.assert_called_with(char)

    assert hap_repr == {
        "iid": 2,
        "type": ANY,
        "description": "Test Char",
        "perms": ["pr"],
        "format": "int",
        "maxValue": 2,
        "minValue": 1,
        "value": 1,
    }


def test_to_HAP_valid_values():
    """Test created HAP representation for valid values constraint."""
    char = get_char(PROPERTIES.copy(), valid={"foo": 0, "bar": 2, "baz": 1})
    with patch.object(char, "broker") as mock_broker:
        mock_broker.iid_manager.get_iid.return_value = 2

        hap_repr = char.to_HAP()

    assert "valid-values" in hap_repr
    assert hap_repr["valid-values"] == [0, 1, 2]


def test_to_HAP_string():
    """Test created HAP representation for strings."""
    char = get_char(PROPERTIES.copy())
    char.properties["Format"] = "string"
    char.value = "aaa"
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert hap_repr["format"] == "string"
    assert "maxLen" not in hap_repr

    char.set_value(
        "aaaaaaaaaabbbbbbbbbbccccccccccddddddddddeeeeeeeeeeffffffffffgggggggggg"
    )
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert "maxLen" not in hap_repr
    assert hap_repr["value"] == char.value[:64]


def test_to_HAP_string_max_length_override():
    """Test created HAP representation for strings."""
    char = get_char(PROPERTIES.copy())
    char.properties["Format"] = "string"
    char.properties["maxLen"] = 256
    char.value = "aaa"
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert hap_repr["format"] == "string"
    assert "maxLen" in hap_repr
    longer_than_sixty_four = (
        "aaaaaaaaaabbbbbbbbbbccccccccccddddddddddeeeeeeeeeeffffffffffgggggggggg"
    )

    char.set_value(longer_than_sixty_four)
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert hap_repr["maxLen"] == 256
    assert hap_repr["value"] == longer_than_sixty_four


def test_to_HAP_bool():
    """Test created HAP representation for booleans."""
    # pylint: disable=protected-access
    char = get_char(PROPERTIES.copy())
    char.properties["Format"] = "bool"
    char._clear_cache()
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert hap_repr["format"] == "bool"

    char.properties["Permissions"] = []
    char._clear_cache()
    with patch.object(char, "broker"):
        hap_repr = char.to_HAP()
    assert "value" not in hap_repr


def test_from_dict():
    """Test creating a characteristic object from a dictionary."""
    uuid = uuid1()
    json_dict = {
        "UUID": str(uuid),
        "Format": "int",
        "Permissions": "read",
    }

    char = Characteristic.from_dict("Test Char", json_dict)
    assert char.display_name == "Test Char"
    assert char.type_id == uuid
    assert char.properties == {"Format": "int", "Permissions": "read"}


def test_getter_callback():
    """Test getter callback."""
    char = Characteristic(
        display_name="Test Char", type_id="A1", properties=PROPERTIES.copy()
    )
    char.set_value(3)
    char.override_properties({"minValue": 3, "maxValue": 10})
    char.broker = Mock()
    assert char.to_HAP() == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 10,
        "minValue": 3,
        "perms": ["pr"],
        "type": "A1",
        "value": 3,
    }

    assert char.to_HAP(include_value=False) == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 10,
        "minValue": 3,
        "perms": ["pr"],
        "type": "A1",
    }
    char.override_properties({"minValue": 4, "maxValue": 11})
    assert char.to_HAP() == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 11,
        "minValue": 4,
        "perms": ["pr"],
        "type": "A1",
        "value": 4,
    }

    assert char.to_HAP(include_value=False) == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 11,
        "minValue": 4,
        "perms": ["pr"],
        "type": "A1",
    }
    char.getter_callback = lambda: 5
    assert char.to_HAP() == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 11,
        "minValue": 4,
        "perms": ["pr"],
        "type": "A1",
        "value": 5,
    }
    assert char.to_HAP(include_value=False) == {
        "description": "Test Char",
        "format": "int",
        "iid": ANY,
        "maxValue": 11,
        "minValue": 4,
        "perms": ["pr"],
        "type": "A1",
    }
