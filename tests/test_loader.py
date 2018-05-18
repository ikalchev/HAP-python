"""Tests for pyhap.loader."""
import pytest

from pyhap.characteristic import Characteristic
from pyhap.service import Service
from pyhap.loader import Loader


def test_loader_char():
    """Test if method returns a Characteristic object."""
    loader = Loader()

    with pytest.raises(KeyError):
        loader.get_char('Not a char')

    char_name = loader.get_char('Name')
    assert char_name is not None
    assert isinstance(char_name, Characteristic)


def test_loader_get_char_error():
    """Test if errors are thrown for invalid dictionary entries."""
    loader = Loader.from_dict(char_dict={'Char': None})
    assert loader.char_types == {'Char': None}
    assert loader.serv_types == {}
    json_dicts = (
        {'Format': 'int', 'Permissions': 'read'},
        {'Format': 'int', 'UUID': '123456'},
        {'Permissions': 'read', 'UUID': '123456'}
    )

    for case in json_dicts:
        loader.char_types['Char'] = case
        with pytest.raises(KeyError):
            loader.get_char('Char')


def test_loader_service():
    """Test if method returns a Service object."""
    loader = Loader()

    with pytest.raises(KeyError):
        loader.get_service('Not a service')

    serv_acc_info = loader.get_service('AccessoryInformation')
    assert serv_acc_info is not None
    assert isinstance(serv_acc_info, Service)


def test_loader_service_error():
    """Test if errors are thrown for invalid dictionary entries."""
    loader = Loader.from_dict(serv_dict={'Service': None})
    assert loader.char_types == {}
    assert loader.serv_types == {'Service': None}
    json_dicts = (
        {'RequiredCharacteristics': ['Char 1', 'Char 2']},
        {'UUID': '123456'}
    )

    for case in json_dicts:
        loader.serv_types['Service'] = case
        with pytest.raises(KeyError):
            loader.get_service('Service')
