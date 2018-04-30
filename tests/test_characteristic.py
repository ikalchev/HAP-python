"""Tests for pyhap.characteristic."""
import unittest
from unittest.mock import Mock, patch, ANY
from uuid import uuid1

from pyhap.characteristic import (
    Characteristic, HAP_FORMAT_INT, HAP_FORMAT_DEFAULTS, HAP_PERMISSION_READ)


PROPERTIES = {
    'Format': HAP_FORMAT_INT,
    'Permissions': [HAP_PERMISSION_READ]
}


def get_char(props, valid=None, min_value=None, max_value=None):
    if valid:
        props['ValidValues'] = valid
    if min_value:
        props['minValue'] = min_value
    if max_value:
        props['maxValue'] = max_value
    return Characteristic(display_name='Test Char', type_id=uuid1(),
                          properties=props)


def test_from_dict():
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


class TestCharacteristic(unittest.TestCase):

    def test_repr(self):
        char = get_char(PROPERTIES.copy())
        del char.properties['Permissions']
        self.assertEqual(
            '<characteristic display_name=Test Char value=0 ' \
            'properties={\'Format\': \'int\'}>', char.__repr__())

    def test_default_value(self):
        char = get_char(PROPERTIES.copy())
        self.assertEqual(char.value, HAP_FORMAT_DEFAULTS[PROPERTIES['Format']])

    def test_get_default_value(self):
        valid_values = {'foo': 2, 'bar': 3}
        char = get_char(PROPERTIES.copy(), valid=valid_values)
        self.assertEqual(char.value, 2)
        self.assertIn(char.value, valid_values.values())
        char = get_char(PROPERTIES.copy(), min_value=3, max_value=10)
        self.assertEqual(char.value, 3)

    def test_to_valid_value(self):
        char = get_char(PROPERTIES.copy(), valid={'foo': 2, 'bar': 3},
                        min_value=2, max_value=7)
        with self.assertRaises(ValueError):
            char.to_valid_value(1)
        self.assertEqual(char.to_valid_value(2), 2)

        del char.properties['ValidValues']
        for value in ('2', None):
            with self.assertRaises(ValueError):
                char.to_valid_value(value)
        self.assertEqual(char.to_valid_value(1), 2)
        self.assertEqual(char.to_valid_value(5), 5)
        self.assertEqual(char.to_valid_value(8), 7)

        char.properties['Format'] = 'string'
        self.assertEqual(char.to_valid_value(24), '24')

        char.properties['Format'] = 'bool'
        self.assertTrue(char.to_valid_value(1))
        self.assertFalse(char.to_valid_value(0))

        char.properties['Format'] = 'dictionary'
        self.assertEqual(char.to_valid_value({'a': 1}), {'a': 1})

    def test_override_properties_properties(self):
        new_properties = {'minValue': 10, 'maxValue': 20, 'step': 1}
        char = get_char(PROPERTIES.copy(), min_value=0, max_value=1)
        char.override_properties(properties=new_properties)
        self.assertEqual(char.properties['minValue'],
                         new_properties['minValue'])
        self.assertEqual(char.properties['maxValue'],
                         new_properties['maxValue'])
        self.assertEqual(char.properties['step'], new_properties['step'])

    def test_override_properties_valid_values(self):
        new_valid_values = {'foo2': 2, 'bar2': 3}
        char = get_char(PROPERTIES.copy(), valid={'foo': 1, 'bar': 2})
        char.override_properties(valid_values=new_valid_values)
        self.assertEqual(char.properties['ValidValues'], new_valid_values)

    def test_override_properties_error(self):
        char = get_char(PROPERTIES.copy())
        with self.assertRaises(ValueError):
            char.override_properties()

    def test_set_value(self):
        path = 'pyhap.characteristic.Characteristic.notify'
        char = get_char(PROPERTIES.copy(), min_value=3, max_value=7)

        with patch(path) as mock_notify:
            char.set_value(5)
            self.assertEqual(char.value, 5)
            self.assertFalse(mock_notify.called)

            char.broker = Mock()
            char.set_value(8, should_notify=False)
            self.assertEqual(char.value, 7)
            self.assertFalse(mock_notify.called)

            char.set_value(1)
            self.assertEqual(char.value, 3)
            self.assertEqual(mock_notify.call_count, 1)

    def test_client_update_value(self):
        path_notify = 'pyhap.characteristic.Characteristic.notify'
        char = get_char(PROPERTIES.copy())

        with patch(path_notify) as mock_notify:
            char.client_update_value(4)
            self.assertEqual(char.value, 4)
            with patch.object(char, 'setter_callback') as mock_callback:
                char.client_update_value(3)

        self.assertEqual(char.value, 3)
        self.assertEqual(mock_notify.call_count, 2)
        mock_callback.assert_called_with(3)

    def test_notify(self):
        char = get_char(PROPERTIES.copy())

        char.value = 2
        with self.assertRaises(AttributeError):
            char.notify()

        with patch.object(char, 'broker') as mock_broker:
            char.notify()
        mock_broker.publish.assert_called_with(2, char)

    def test_to_HAP_numberic(self):
        char = get_char(PROPERTIES.copy(), min_value=1, max_value=2)
        with patch.object(char, 'broker') as mock_broker:
            mock_iid = mock_broker.iid_manager.get_iid
            mock_iid.return_value = 2
            hap_repr = char.to_HAP()
            mock_iid.assert_called_with(char)

        self.assertEqual(
            hap_repr,
            {
                'iid': 2,
                'type': ANY,
                'description': 'Test Char',
                'perms': ['pr'],
                'format': 'int',
                'maxValue': 2,
                'minValue': 1,
                'value': 1, 
            })

    def test_to_HAP_string(self):
        char = get_char(PROPERTIES.copy())
        char.properties['Format'] = 'string'
        char.value  = 'aaa'
        with patch.object(char, 'broker') as mock_broker:
            hap_repr = char.to_HAP()
        self.assertEqual(hap_repr['format'], 'string')
        self.assertNotIn('maxLen', hap_repr)

        char.value = 'aaaaaaaaaabbbbbbbbbbccccccccccddddddddddeeeeeeeeee' \ 
        'ffffffffffgggggggggg'
        with patch.object(char, 'broker') as mock_broker:
            hap_repr = char.to_HAP()
        self.assertEqual(hap_repr['maxLen'], 70)
        self.assertEqual(hap_repr['value'], char.value)

    def test_to_HAP_bool(self):
        char = get_char(PROPERTIES.copy())
        char.properties['Format'] = 'bool'
        with patch.object(char, 'broker') as mock_broker:
            hap_repr = char.to_HAP()
        self.assertEqual(hap_repr['format'], 'bool')

        char.properties['Permissions'] = []
        with patch.object(char, 'broker') as mock_broker:
            hap_repr = char.to_HAP()
        self.assertNotIn('value', hap_repr)
