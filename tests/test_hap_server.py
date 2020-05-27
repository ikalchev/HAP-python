"""Tests for the HAPServer."""
from socket import timeout
from unittest.mock import Mock, MagicMock, patch

import pytest

from pyhap import hap_server
from pyhap.const import __version__


@patch("pyhap.hap_server.HAPServer.server_bind", new=MagicMock())
@patch("pyhap.hap_server.HAPServer.server_activate", new=MagicMock())
def test_finish_request_pops_socket():
    """Test that ``finish_request`` always clears the connection after a request."""
    amock = Mock()
    client_addr = ("192.168.1.1", 55555)
    server_addr = ("", 51826)

    # Positive case: The request is handled
    server = hap_server.HAPServer(
        server_addr, amock, handler_type=lambda *args: MagicMock()
    )

    server.connections[client_addr] = amock
    server.finish_request(amock, client_addr)

    assert len(server.connections) == 0

    # Negative case: The request fails with a timeout
    def raises(*args):
        raise timeout()

    server = hap_server.HAPServer(server_addr, amock, handler_type=raises)
    server.connections[client_addr] = amock
    server.finish_request(amock, client_addr)

    assert len(server.connections) == 0

    # Negative case: The request raises some other exception
    server = hap_server.HAPServer(server_addr, amock, handler_type=lambda *args: 1 / 0)
    server.connections[client_addr] = amock

    with pytest.raises(Exception):
        server.finish_request(amock, client_addr)

    assert len(server.connections) == 0


def test_uses_http11():
    """Test that ``HAPServerHandler`` uses HTTP/1.1."""
    amock = Mock()

    with patch("pyhap.hap_server.HAPServerHandler.setup"), patch(
        "pyhap.hap_server.HAPServerHandler.handle_one_request"
    ), patch("pyhap.hap_server.HAPServerHandler.finish"):
        handler = hap_server.HAPServerHandler(
            "mocksock", "mockclient_addr", "mockserver", amock
        )
        assert handler.protocol_version == "HTTP/1.1"
        assert handler.server_version == "pyhap/" + __version__


def test_end_response_is_one_send():
    """Test that ``HAPServerHandler`` sends the whole response at once."""

    class ConnectionMock:
        sent_bytes = []

        def sendall(self, bytesdata):
            self.sent_bytes.append([bytesdata])
            return 1

        def getsent(self):
            return self.sent_bytes

    amock = Mock()

    with patch("pyhap.hap_server.HAPServerHandler.setup"), patch(
        "pyhap.hap_server.HAPServerHandler.handle_one_request"
    ), patch("pyhap.hap_server.HAPServerHandler.finish"):
        handler = hap_server.HAPServerHandler(
            "mocksock", "mockclient_addr", "mockserver", amock
        )
        handler.request_version = "HTTP/1.1"
        handler.connection = ConnectionMock()
        handler.requestline = "GET / HTTP/1.1"
        handler.send_response(200)
        handler.wfile = MagicMock()
        handler.end_response(b"body")
        assert handler.connection.getsent() == [
            [b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody"]
        ]
        assert handler._headers_buffer == []  # pylint: disable=protected-access
        assert handler.wfile.called_once()


def test_http_204_has_no_content_length():
    """Test that and HTTP 204 has no content length."""

    class ConnectionMock:
        sent_bytes = []

        def sendall(self, bytesdata):
            self.sent_bytes.append([bytesdata])
            return 1

        def getsent(self):
            return self.sent_bytes

    amock = Mock()

    with patch("pyhap.hap_server.HAPServerHandler.setup"), patch(
        "pyhap.hap_server.HAPServerHandler.handle_one_request"
    ), patch("pyhap.hap_server.HAPServerHandler.finish"):
        handler = hap_server.HAPServerHandler(
            "mocksock", "mockclient_addr", "mockserver", amock
        )
        handler.request_version = "HTTP/1.1"
        handler.connection = ConnectionMock()
        handler.requestline = "PUT / HTTP/1.1"
        handler.send_response(204)
        handler.wfile = MagicMock()
        handler.end_response(b"")
        assert handler.connection.getsent() == [[b"HTTP/1.1 204 No Content\r\n\r\n"]]
        assert handler._headers_buffer == []  # pylint: disable=protected-access
        assert handler.wfile.called_once()
