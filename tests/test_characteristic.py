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
            == char.get_value())

def test_set_value():
    char = get_char(PROPERTIES.copy())
    char.broker = mock.Mock()
    new_value = 3
    char.set_value(new_value, should_notify=False)
    assert char.get_value() == new_value
    new_value = 4
    char.set_value(new_value, should_notify=True)
    assert char.get_value() == new_value
    assert char.broker.publish.called

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

def test_to_HAP():
    pass # TODO