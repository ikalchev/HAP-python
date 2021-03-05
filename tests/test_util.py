"""Test for pyhap.util."""
import functools

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
