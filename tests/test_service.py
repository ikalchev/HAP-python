"""
Tests for pyhap.service
"""
import uuid

import pytest

import pyhap.service as service
from pyhap.characteristic import Characteristic, HAP_FORMAT, HAP_PERMISSIONS

CHAR_PROPS = {
    "Format": HAP_FORMAT.INT,
    "Permissions": HAP_PERMISSIONS.READ,
}

def get_chars():
    c1 = Characteristic("Char 1", uuid.uuid1(), CHAR_PROPS)
    c2 = Characteristic("Char 2", uuid.uuid1(), CHAR_PROPS)
    return [c1, c2]

def test_add_characteristic():
    serv = service.Service(uuid.uuid1(), "Test Service")
    chars = get_chars()
    serv.add_characteristic(*chars)
    for c in chars:
        assert serv.get_characteristic(c.display_name, check_optional=False) == c

def test_add_opt_characteristic():
    serv = service.Service(uuid.uuid1(), "Test Service")
    chars = get_chars()
    serv.add_opt_characteristic(*chars)
    for c in chars:
        assert serv.get_characteristic(c.display_name, check_optional=True) == c

def test_to_HAP():
    pass # TODO: