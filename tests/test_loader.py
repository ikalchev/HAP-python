"""Tests for pyhap.loader."""
from unittest.mock import patch, ANY, Mock

import pytest

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import Characteristic
from pyhap.service import Service
from pyhap.loader import get_char_loader, get_serv_loader, TypeLoader


def test_loader_char():
    with open(CHARACTERISTICS_FILE, 'r') as file:
        loader = TypeLoader(file)
    assert loader.types is not None

    assert loader.get('Name') is not None
    with pytest.raises(KeyError):
        loader.get('Not a char')

    assert isinstance(loader.get_char('Name'), Characteristic)


def test_loader_get_char_error():
    loader = TypeLoader.__new__(TypeLoader)
    loader.types = {'Char': None}
    json_dicts = (
        {'Format': 'int', 'Permissions': 'read'},
        {'Format': 'int', 'UUID': '123456'},
        {'Permissions': 'read', 'UUID': '123456'}
    )

    for case in json_dicts:
        loader.types['Char'] = case
        with pytest.raises(KeyError):
            loader.get_char('Char')


def test_loader_service():
    with open(SERVICES_FILE, 'r') as file:
        loader = TypeLoader(file)
    assert loader.types is not None

    assert loader.get('AccessoryInformation') is not None
    with pytest.raises(KeyError):
        loader.get('Not a service')

    with patch('pyhap.service.Service.from_dict') as mock_service_from_dict:
        service = loader.get_service('AccessoryInformation', Mock())
        mock_service_from_dict.assert_called_with(
            'AccessoryInformation', loader.get('AccessoryInformation'), ANY)


def test_loader_service_error():
    loader = TypeLoader.__new__(TypeLoader)
    loader.types = {'Service': None}
    json_dicts = (
        {'RequiredCharacteristics': ['Char 1', 'Char 2']},
        {'UUID': '123456'}
    )

    for case in json_dicts:
        loader.types['Service'] = case
        with pytest.raises(KeyError):
            loader.get_service('Service')


def test_get_char_loader():
    char_loader = get_char_loader()
    assert isinstance(char_loader, TypeLoader)

    with open(CHARACTERISTICS_FILE, 'r') as file:
        loader = TypeLoader(file)
    assert char_loader.types == loader.types

    assert get_char_loader() == char_loader


def test_get_serv_loader():
    serv_loader = get_serv_loader()
    assert isinstance(serv_loader, TypeLoader)

    with open(SERVICES_FILE, 'r') as file:
        loader = TypeLoader(file)
    assert serv_loader.types == loader.types

    assert get_serv_loader() == serv_loader
