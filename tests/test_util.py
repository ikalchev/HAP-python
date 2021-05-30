"""Test for pyhap.util."""
import functools
from uuid import UUID

from pyhap import util


@util.callback
def async_is_callback():
    """Test callback."""


def async_not_callback():
    """Test callback."""


async def async_function():
    """Test for iscoro."""


def test_callback():
    """Test is_callback."""
    assert util.is_callback(async_is_callback) is True
    assert util.is_callback(async_not_callback) is False


def test_iscoro():
    """Test iscoro."""
    assert util.iscoro(async_function) is True
    assert util.iscoro(functools.partial(async_function)) is True
    assert util.iscoro(async_is_callback) is False
    assert util.iscoro(async_not_callback) is False


def test_generate_setup_id():
    """Test generate_setup_id."""
    assert len(util.generate_setup_id()) == 4


def test_hap_type_to_uuid():
    """Test we can convert short types to UUIDs."""
    assert util.hap_type_to_uuid("32") == UUID("00000032-0000-1000-8000-0026bb765291")
    assert util.hap_type_to_uuid("00000032") == UUID(
        "00000032-0000-1000-8000-0026bb765291"
    )
    assert util.hap_type_to_uuid("00000032-0000-1000-8000-0026bb765291") == UUID(
        "00000032-0000-1000-8000-0026bb765291"
    )
