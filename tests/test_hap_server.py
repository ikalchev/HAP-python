"""Tests for the HAPServer."""
from socket import timeout
from unittest.mock import Mock, MagicMock, patch

import pytest

from pyhap import hap_server


@patch('pyhap.hap_server.HAPServer.server_bind', new=MagicMock())
@patch('pyhap.hap_server.HAPServer.server_activate', new=MagicMock())
def test_finish_request_pops_socket():
    """Test that ``finish_request`` always clears the connection after a request."""
    amock = Mock()
    client_addr = ('192.168.1.1', 55555)
    server_addr = ('', 51826)

    # Positive case: The request is handled
    server = hap_server.HAPServer(server_addr, amock,
                                  handler_type=lambda *args: MagicMock())

    server.connections[client_addr] = amock
    server.finish_request(amock, client_addr)

    assert len(server.connections) == 0

    # Negative case: The request fails with a timeout
    def raises(*args):
        raise timeout()
    server = hap_server.HAPServer(server_addr, amock,
                                  handler_type=raises)
    server.connections[client_addr] = amock
    server.finish_request(amock, client_addr)

    assert len(server.connections) == 0

    # Negative case: The request raises some other exception
    server = hap_server.HAPServer(server_addr, amock,
                                  handler_type=lambda *args: 1/0)
    server.connections[client_addr] = amock

    with pytest.raises(Exception):
        server.finish_request(amock, client_addr)

    assert len(server.connections) == 0
