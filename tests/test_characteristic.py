"""Tests for pyhap.characteristic."""
from unittest.mock import Mock, patch, ANY
from uuid import uuid1

import pytest

from pyhap.characteristic import (
    Characteristic, HAP_FORMAT_INT, HAP_FORMAT_DEFAULTS, HAP_PERMISSION_READ)

PROPERTIES = {
    'Format': HAP_FORMAT_INT,
    'Permissions': [HAP_PERMISSION_READ]
}


def get_char(props, valid=None, min_value=None, max_value=None):
    """Return a char object with given parameters."""
    if valid:
        props['ValidValues'] = valid
    if min_value:
        props['minValue'] = min_value
    if max_value:
        props['maxValue'] = max_value
    return Characteristic(display_name='Test Char', type_id=uuid1(),
                          properties=props)


def test_repr():
    """Test representation of a characteristic."""
    char = get_char(PROPERTIES.copy())
    del char.properties['Permissions']
    assert char.__repr__() == \
        '<characteristic display_name=Test Char value=0 ' \
        'properties={\'Format\': \'int\'}>'


def test_default_value():
    """Test getting the default value for a specific format."""
    char = get_char(PROPERTIES.copy())
    assert char.value == HAP_FORMAT_DEFAULTS[PROPERTIES['Format']]


def test_get_default_value():
    """Test getting the default value for valid values."""
    valid_values = {'foo': 2, 'bar': 3}
    char = get_char(PROPERTIES.copy(), valid=valid_values)
    assert char.value == 2
    assert char.value in valid_values.values()
    char = get_char(PROPERTIES.copy(), min_value=3, max_value=10)
    assert char.value == 3


def test_to_valid_value():
    """Test function to test if value is valid and saved correctly."""
    char = get_char(PROPERTIES.copy(), valid={'foo': 2, 'bar': 3},
                    min_value=2, max_value=7)
    with pytest.raises(ValueError):
        char.to_valid_value(1)
    assert char.to_valid_value(2) == 2

    del char.properties['ValidValues']
    for value in ('2', None):
        with pytest.raises(ValueError):
            char.to_valid_value(value)
    assert char.to_valid_value(1) == 2
    assert char.to_valid_value(5) == 5
    assert char.to_valid_value(8) == 7

    char.properties['Format'] = 'string'
    assert char.to_valid_value(24) == '24'

    char.properties['Format'] = 'bool'
    assert char.to_valid_value(1) is True
    assert char.to_valid_value(0) is False

    char.properties['Format'] = 'dictionary'
    assert char.to_valid_value({'a': 1}) == {'a': 1}


def test_override_properties_properties():
    """Test if overriding the properties works."""
    new_properties = {'minValue': 10, 'maxValue': 20, 'step': 1}
    char = get_char(PROPERTIES.copy(), min_value=0, max_value=1)
    char.override_properties(properties=new_properties)
    assert char.properties['minValue'] == new_properties['minValue']
    assert char.properties['maxValue'] == new_properties['maxValue']
    assert char.properties['step'] == new_properties['step']


def test_override_properties_valid_values():
    """Test if overriding the properties works for valid values."""
    new_valid_values = {'foo2': 2, 'bar2': 3}
    char = get_char(PROPERTIES.copy(), valid={'foo': 1, 'bar': 2})
    char.override_properties(valid_values=new_valid_values)
    assert char.properties['ValidValues'] == new_valid_values


def test_override_properties_error():
    """Test that method throws an error if no arguments have been passed."""
    char = get_char(PROPERTIES.copy())
    with pytest.raises(ValueError):
        char.override_properties()


def test_set_value():
    """Test setting the value of a characteristic."""
    path = 'pyhap.characteristic.Characteristic.notify'
    char = get_char(PROPERTIES.copy(), min_value=3, max_value=7)

    with patch(path) as mock_notify:
        char.set_value(5)
        assert char.value == 5
        assert mock_notify.called is False

        char.broker = Mock()
        char.set_value(8, should_notify=False)
        assert char.value == 7
        assert mock_notify.called is False

        char.set_value(1)
        assert char.value == 3
        assert mock_notify.call_count == 1


def test_client_update_value():
    """Test updating the characteristic value with call from the driver."""
    path_notify = 'pyhap.characteristic.Characteristic.notify'
    char = get_char(PROPERTIES.copy())

    with patch(path_notify) as mock_notify:
        char.client_update_value(4)
        assert char.value == 4
        with patch.object(char, 'setter_callback') as mock_callback:
            char.client_update_value(3)

    assert char.value == 3
    assert mock_notify.call_count == 2
    mock_callback.assert_called_with(3)


def test_notify():
    """Test if driver is notified correctly about a changed characteristic."""
    char = get_char(PROPERTIES.copy())

    char.value = 2
    with pytest.raises(AttributeError):
        char.notify()

    with patch.object(char, 'broker') as mock_broker:
        char.notify()
    mock_broker.publish.assert_called_with(2, char)


def test_to_HAP_numberic():
    """Test created HAP representation for numeric formats."""
    char = get_char(PROPERTIES.copy(), min_value=1, max_value=2)
    with patch.object(char, 'broker') as mock_broker:
        mock_iid = mock_broker.iid_manager.get_iid
        mock_iid.return_value = 2
        hap_repr = char.to_HAP()
        mock_iid.assert_called_with(char)

    assert hap_repr == {
        'iid': 2,
        'type': ANY,
        'description': 'Test Char',
        'perms': ['pr'],
        'format': 'int',
        'maxValue': 2,
        'minValue': 1,
        'value': 1,
    }


def test_to_HAP_string():
    """Test created HAP representation for strings."""
    char = get_char(PROPERTIES.copy())
    char.properties['Format'] = 'string'
    char.value = 'aaa'
    with patch.object(char, 'broker'):
        hap_repr = char.to_HAP()
    assert hap_repr['format'] == 'string'
    assert 'maxLen' not in hap_repr

    char.value = 'aaaaaaaaaabbbbbbbbbbccccccccccddddddddddeeeeeeeeee' \
        'ffffffffffgggggggggg'
    with patch.object(char, 'broker'):
        hap_repr = char.to_HAP()
    assert hap_repr['maxLen'] == 70
    assert hap_repr['value'] == char.value


def test_to_HAP_bool():
    """Test created HAP representation for booleans."""
    char = get_char(PROPERTIES.copy())
    char.properties['Format'] = 'bool'
    with patch.object(char, 'broker'):
        hap_repr = char.to_HAP()
    assert hap_repr['format'] == 'bool'

    char.properties['Permissions'] = []
    with patch.object(char, 'broker'):
        hap_repr = char.to_HAP()
    assert 'value' not in hap_repr


def test_from_dict():
    """Test creating a characteristic object from a dictionary."""
    uuid = uuid1()
    json_dict = {
        'UUID': str(uuid),
        'Format': 'int',
        'Permissions': 'read',
    }

    char = Characteristic.from_dict('Test Char', json_dict)
    assert char.display_name == 'Test Char'
    assert char.type_id == uuid
    assert char.properties == {'Format': 'int', 'Permissions': 'read'}
