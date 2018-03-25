"""
Tests for pyhap.characteristic
"""
import uuid
from unittest import mock

import pytest

import pyhap.characteristic as characteristic
from pyhap.characteristic import Characteristic

PROPERTIES = {
    "Format": characteristic.HAP_FORMAT.INT,
    "Permissions": [characteristic.HAP_PERMISSIONS.READ]
}

def get_char(props, valid=None, min_value=None, max_value=None):
    if valid is not None:
        props["ValidValues"] = valid
    if min_value is not None:
        props["minValue"] = min_value
    if max_value is not None:
        props["maxValue"] = max_value
    c = Characteristic(display_name="Test Char",
                       type_id=uuid.uuid1(),
                       properties=props)
    return c

def test_default_value():
    char = get_char(PROPERTIES.copy())
    assert (characteristic.HAP_FORMAT.DEFAULT[PROPERTIES["Format"]]
            == char.value)

def test_default_valid_value():
    valid_values = {"foo": 2, "bar": 3}
    char = get_char(PROPERTIES.copy(), valid=valid_values)
    assert char.value in valid_values.values()

def test_set_value():
    char = get_char(PROPERTIES.copy())
    new_value = 3
    char.set_value(new_value)
    assert char.value == new_value

def test_set_value_valid_values():
    valid_values = {"foo": 2, "bar": 3, }
    char = get_char(PROPERTIES.copy(), valid=valid_values)
    with pytest.raises(ValueError):
        char.set_value(4)

def test_set_value_callback_toggle():
    char = get_char(PROPERTIES.copy())
    char.setter_callback = mock.Mock()
    char.set_value(3, should_callback=False)
    assert not char.setter_callback.called
    char.set_value(3, should_callback=True)
    assert char.setter_callback.called

def test_override_properties_properties():
    new_properties = {'minValue': 10, 'maxValue': 20, 'step': 1}
    char = get_char(PROPERTIES.copy(), min_value=0, max_value=1)
    char.override_properties(properties=new_properties)
    assert char.properties['minValue'] == new_properties['minValue']
    assert char.properties['maxValue'] == new_properties['maxValue']
    assert char.properties['step'] == new_properties['step']

def test_override_properties_valid_values():
    new_valid_values = {'foo2': 2, 'bar2': 3}
    char = get_char(PROPERTIES.copy(), valid={'foo': 1, 'bar': 2})
    char.override_properties(valid_values=new_valid_values)
    assert char.properties['ValidValues'] == new_valid_values

def test_get_hap_value():
    max_value = 5
    raw_value = 6
    char = get_char(PROPERTIES.copy(), max_value=max_value)
    char.set_value(raw_value, should_notify=False)
    assert char.value == raw_value
    assert char.get_hap_value() == max_value

def test_notify():
    char = get_char(PROPERTIES.copy())
    broker_mock = mock.Mock()
    char.broker = broker_mock
    notify_value = 3
    expected = {
        "type_id": char.type_id,
        "value": notify_value,
    }
    char.value = notify_value
    char.notify()
    assert broker_mock.publish.called
    broker_mock.publish.assert_called_with(expected, char)

def test_notify_except_no_broker():
    char = get_char(PROPERTIES.copy())
    with pytest.raises(characteristic.NotConfiguredError):
        char.notify()
