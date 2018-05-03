"""Tests for pyhap.loader."""
from unittest.mock import patch, ANY, Mock

import pytest

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import Characteristic
from pyhap.service import Service
from pyhap.loader import get_loader, Loader


def test_loader_char():
    loader = Loader()

    assert loader.get_char('Name') is not None
    with pytest.raises(KeyError):
        loader.get_char('Not a char')

    assert isinstance(loader.get_char('Name'), Characteristic)


def test_loader_get_char_error():
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
    loader = Loader()

    assert loader.get_service('AccessoryInformation') is not None
    with pytest.raises(KeyError):
        loader.get_service('Not a service')

    with patch('pyhap.service.Service.from_dict') as mock_service_from_dict:
        service = loader.get_service('AccessoryInformation')
        mock_service_from_dict.assert_called_with(
            'AccessoryInformation', ANY, loader)


def test_loader_service_error():
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


def test_get_loader():
    loader = get_loader()
    assert isinstance(loader, Loader)
    assert loader.char_types is not ({} or None)
    assert loader.serv_types is not ({} or None)

    loader2 = Loader(path_char=CHARACTERISTICS_FILE,
                     path_service=SERVICES_FILE)
    assert loader.char_types == loader2.char_types
    assert loader.serv_types == loader2.serv_types

    assert get_loader() == loader
