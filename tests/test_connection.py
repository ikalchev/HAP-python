"""Tests for the connection handle."""

from pyhap.connection import HAPConnectionHandle


def test_same_connection_is_equal() -> None:
    """Two handles of one connection, e.g. of a read and a write, are equal."""
    handle = HAPConnectionHandle(("1.2.3.4", 5), 3)
    same = HAPConnectionHandle(("1.2.3.4", 5), 3)

    assert handle == same
    assert hash(handle) == hash(same)


def test_reused_address_is_not_equal() -> None:
    """A later connection reusing the address is a different identity."""
    handle = HAPConnectionHandle(("1.2.3.4", 5), 3)
    reused = HAPConnectionHandle(("1.2.3.4", 5), 4)
    other = HAPConnectionHandle(("1.2.3.4", 6), 3)

    assert handle != reused
    assert handle != other
    assert handle != ("1.2.3.4", 5)
    assert not (handle == ("1.2.3.4", 5))


def test_equal_handles_hash_equal() -> None:
    """Equal handles hash equal, so they key a dict or set together."""
    handles = {HAPConnectionHandle(("1.2.3.4", 5), 3): "connection"}
    assert handles[HAPConnectionHandle(("1.2.3.4", 5), 3)] == "connection"
    assert HAPConnectionHandle(("1.2.3.4", 5), 4) not in handles


def test_exposes_client_addr() -> None:
    """The handle's interface is the connection's address."""
    handle = HAPConnectionHandle(("1.2.3.4", 5), 7)

    assert handle.client_addr == ("1.2.3.4", 5)
    assert not hasattr(handle, "generation")
    assert "1.2.3.4" in repr(handle)


def test_unconnected_handle() -> None:
    """A handle without a connection, e.g. of a direct call, is still a key."""
    handle = HAPConnectionHandle(None, 0)

    assert handle.client_addr is None
    assert handle == HAPConnectionHandle(None, 0)
    assert handle != HAPConnectionHandle(None, 1)
