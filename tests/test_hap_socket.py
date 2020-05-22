"""Tests for the HAPSocket."""
import socket

from pyhap import hap_server


def test_iorefs():
    """Test that the _io_refs are correct when creating/closing a fileio from it."""
    sock = hap_server.HAPSocket(socket.socket(), b'\x00' * 64)
    fileio = sock.makefile('rb')
    assert sock._io_refs == 1  # pylint: disable=protected-access
    fileio.close()
    assert sock._io_refs == 0  # pylint: disable=protected-access
