"""
Tests for pyhap.loader
"""
import pytest
from unittest import mock

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
import pyhap.loader as loader

# CharLoader

def test_custom_char_class_init():

    mock_char = mock.Mock(side_effect=mock.Mock())
    with open(CHARACTERISTICS_FILE, "r") as fp:
        ldr = loader.CharLoader(fp, char_class=mock_char)
    char = ldr.get("CurrentTemperature")
    assert isinstance(char, mock.Mock)

def test_custom_char_class_get():
    mock_char = mock.Mock(side_effect=mock.Mock())
    with open(CHARACTERISTICS_FILE, "r") as fp:
        ldr = loader.CharLoader(fp)
    char = ldr.get("CurrentTemperature", char_class=mock_char)
    assert isinstance(char, mock.Mock)

# ServiceLoader

def test_custom_service_class_init():
    mock_class = mock.Mock(side_effect=mock.Mock())
    with open(SERVICES_FILE, "r") as fp:
        ldr = loader.ServiceLoader(fp, service_class=mock_class)
    service = ldr.get("TemperatureSensor")
    assert isinstance(service, mock.Mock)

def test_custom_service_class_get():
    mock_class = mock.Mock(side_effect=mock.Mock())
    with open(SERVICES_FILE, "r") as fp:
        ldr = loader.ServiceLoader(fp)
    service = ldr.get("TemperatureSensor", service_class=mock_class)
    assert isinstance(service, mock.Mock)