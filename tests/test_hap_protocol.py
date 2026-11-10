"""Tests for the HAPServerProtocol."""

import asyncio
import contextlib
import time
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock, Mock, patch

from cryptography.exceptions import InvalidTag

from pyhap import hap_handler, hap_protocol
from pyhap.accessory import Accessory, Bridge, get_topic
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import HAP_REPR_AID, HAP_REPR_IID, HAP_REPR_VALUE
from pyhap.hap_crypto import HAPCrypto, hap_hkdf
from pyhap.hap_handler import HAPResponse


class MockTransport(asyncio.Transport):
    """A mock transport."""

    _is_closing: bool = False

    def set_write_buffer_limits(
        self, high: int | None = None, low: int | None = None
    ) -> None:
        """Set the write buffer limits."""

    def write_eof(self) -> None:
        """Write EOF to the stream."""

    def close(self) -> None:
        """Close the stream."""
        self._is_closing = True

    def is_closing(self) -> bool:
        """Return True if the transport is closing or closed."""
        return self._is_closing


class MockHAPCrypto:
    """Mock HAPCrypto that only returns plaintext."""

    def __init__(self) -> None:
        """Create the mock object."""
        self._crypt_in_buffer = bytearray()  # Encrypted buffer

    def receive_data(self, buffer: bytes) -> None:
        """Receive data into the encrypted buffer."""
        self._crypt_in_buffer += buffer

    def decrypt(self) -> bytearray:
        """Mock as plaintext."""
        decrypted = self._crypt_in_buffer
        self._crypt_in_buffer = bytearray()  # Encrypted buffer
        return decrypted

    def encrypt(self, data: bytes) -> bytes:
        """Mock as plaintext."""
        return data


def _enable_encryption(hap_proto: hap_protocol.HAPServerProtocol) -> None:
    """Install the plaintext crypto mock and mark the session encrypted."""
    assert hap_proto.handler is not None
    hap_proto.hap_crypto = cast("HAPCrypto", MockHAPCrypto())
    hap_proto.handler.is_encrypted = True


async def _await_response(hap_proto: hap_protocol.HAPServerProtocol) -> None:
    """Wait for a pending delayed response, ignoring failures."""
    response = hap_proto.response
    assert response is not None
    assert response.task is not None
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await response.task


def test_connection_management(driver: AccessoryDriver) -> None:
    """Verify closing the connection removes it from the pool."""
    loop = MagicMock()
    addr_info = ("1.2.3.4", 5)
    addr_info2 = ("1.2.3.5", 6)

    transport = MagicMock(get_extra_info=Mock(return_value=addr_info))
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))
    driver.async_subscribe_client_topic(addr_info, "1.1", True)
    driver.async_subscribe_client_topic(addr_info, "2.2", True)
    driver.async_subscribe_client_topic(addr_info2, "1.1", True)

    assert "1.1" in driver.topics
    assert "2.2" in driver.topics

    assert addr_info in driver.topics["1.1"]
    assert addr_info in driver.topics["2.2"]
    assert addr_info2 in driver.topics["1.1"]

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)
    assert len(connections) == 1
    assert connections[addr_info] == hap_proto
    hap_proto.connection_lost(None)
    assert len(connections) == 0
    assert "1.1" in driver.topics
    assert "2.2" not in driver.topics
    assert addr_info not in driver.topics["1.1"]
    assert addr_info2 in driver.topics["1.1"]

    hap_proto.connection_made(transport)
    assert len(connections) == 1
    assert connections[addr_info] == hap_proto
    hap_proto.close()
    assert len(connections) == 0

    hap_proto.connection_made(transport)
    assert len(connections) == 1
    assert connections[addr_info] == hap_proto
    hap_proto.connection_lost(None)
    assert len(connections) == 0


def test_pair_setup(driver: AccessoryDriver) -> None:
    """Verify an non-encrypt request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True

    hap_proto.close()


def test_request_body_over_limit_closes_connection(driver: AccessoryDriver) -> None:
    """Verify a request body over the size limit aborts the connection.

    Without the limit a peer can stream an arbitrarily large body (h11 does
    not cap total body size) and exhaust memory before the request is ever
    handled.
    """
    loop = MagicMock()
    transport = MockTransport()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    body = b"x" * (hap_protocol.MAX_REQUEST_BODY_SIZE + 1)
    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n" + body
        )

    # The connection is dropped before the request is dispatched.
    assert transport._is_closing is True
    assert not connections
    writer.assert_not_called()


def test_request_body_at_limit_is_processed(driver: AccessoryDriver) -> None:
    """Verify a body exactly at the limit is still handled normally."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    body = b"x" * hap_protocol.MAX_REQUEST_BODY_SIZE
    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n" + body
        )

    # The (garbage) body is dispatched and answered with a 500, not dropped.
    assert not transport.close.called
    assert connections
    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 500") is True
    hap_proto.close()


def test_http10_close(driver: AccessoryDriver) -> None:
    """Test we handle http/1.0."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\n"
            b"Connection:close\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    assert len(writer.call_args_list) == 1
    assert not connections
    hap_proto.close()


def test_invalid_content_length(driver: AccessoryDriver) -> None:
    """Test we handle invalid content length."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\n"
            b"Connection:close\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\n"
            b"Connection:close\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )

    assert (
        writer.call_args_list[0][0][0].startswith(
            b"HTTP/1.1 500 Internal Server Error\r\n"
        )
        is True
    )
    assert len(writer.call_args_list) == 1
    assert not connections
    hap_proto.close()


def test_invalid_client_closes_connection(driver: AccessoryDriver) -> None:
    """Test we handle client closing the connection."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\n"
            b"Connection:close\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        hap_proto.data_received(b"")

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    assert len(writer.call_args_list) == 1
    assert not connections
    hap_proto.close()


def test_pair_setup_split_between_packets(driver: AccessoryDriver) -> None:
    """Verify an non-encrypt request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\n"
        )
        hap_proto.data_received(b"Content-Length: 6\r\n")
        hap_proto.data_received(
            b"Content-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True

    hap_proto.close()


def test_get_accessories_without_crypto(driver: AccessoryDriver) -> None:
    """Verify an non-encrypt request that expected to be encrypted."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )

    hap_proto.close()
    assert b"-70401" in writer.call_args_list[0][0][0]


def test_get_accessories_with_crypto(driver: AccessoryDriver) -> None:
    """Verify an encrypt accessories request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )

    hap_proto.close()
    assert b"accessories" in b"".join(writelines.call_args_list[0][0])


def test_get_characteristics_with_crypto(driver: AccessoryDriver) -> None:
    """Verify an encrypt characteristics request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("TemperatureSensor")
    acc.add_service(service)
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"GET /characteristics?id=3762173001.7 HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )
        hap_proto.data_received(
            b"GET /characteristics?id=1.5 HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )

    hap_proto.close()
    joined0 = b"".join(writelines.call_args_list[0][0])
    assert b"Content-Length:" in joined0
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in joined0
    assert b"-70402" in joined0

    joined1 = b"".join(writelines.call_args_list[1][0])
    assert b"Content-Length:" in joined1
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in joined1
    assert b"TestAcc" in joined1


def test_set_characteristics_with_crypto(driver: AccessoryDriver) -> None:
    """Verify an encrypt characteristics request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"PUT /characteristics HTTP/1.1\r\n"
            b"Host: HASS12\\032AD1C22._hap._tcp.local\r\n"
            b"Content-Length: 50\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"characteristics":[{"aid":1,"iid":11,"ev":true}]}'
        )

    hap_proto.close()
    assert (
        b"".join(writelines.call_args_list[0][0]) == b"HTTP/1.1 204 No Content\r\n\r\n"
    )


def test_crypto_failure_closes_connection(driver: AccessoryDriver) -> None:
    """Verify a decrypt failure closes the connection."""
    loop = MagicMock()
    addr_info = ("1.2.3.4", 5)
    transport = MagicMock(get_extra_info=Mock(return_value=addr_info))
    connections = {}

    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)
    assert connections[addr_info] == hap_proto
    with patch.object(hap_proto.hap_crypto, "decrypt", side_effect=InvalidTag):
        hap_proto.data_received(b"any")

    assert len(connections) == 0


def test_empty_encrypted_data(driver: AccessoryDriver) -> None:
    """Verify an encrypt request when we start with an empty block."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)
    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(b"")
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )

    hap_proto.close()
    assert b"accessories" in b"".join(writelines.call_args_list[0][0])


def test_http_11_keep_alive(driver: AccessoryDriver) -> None:
    """Verify we can handle multiple requests."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    hap_proto.close()


async def test_camera_snapshot_connection_closed(driver: AccessoryDriver) -> None:
    """Test camera snapshot when the other side closes the connection."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=True)
    connections = {}

    async def _async_get_snapshot(*_: Any) -> bytes:
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    # get_snapshot/async_get_snapshot are camera-only methods, not part
    # of Accessory's interface; attach them dynamically so the handler
    # dispatches to them.
    acc.async_get_snapshot = _async_get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        hap_proto.close()
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert writer.call_args_list == []

    hap_proto.close()


def test_camera_snapshot_without_snapshot_support(driver: AccessoryDriver) -> None:
    """Test camera snapshot fails if there is not support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    acc = Accessory(driver, "TestAcc")
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )

    hap_proto.close()
    assert b"-70402" in b"".join(writelines.call_args_list[0][0])


async def test_camera_snapshot_works_sync(driver: AccessoryDriver) -> None:
    """Test camera snapshot works if there is support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    def _get_snapshot(*_: Any) -> bytes:
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    # Camera-only snapshot method, attached dynamically so the
    # handler dispatches to it.
    acc.get_snapshot = _get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert b"fakesnap" in b"".join(writelines.call_args_list[0][0])

    hap_proto.close()


async def test_camera_snapshot_works_async(driver: AccessoryDriver) -> None:
    """Test camera snapshot works if there is support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_: Any) -> bytes:
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    # get_snapshot/async_get_snapshot are camera-only methods, not part
    # of Accessory's interface; attach them dynamically so the handler
    # dispatches to them.
    acc.async_get_snapshot = _async_get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert b"fakesnap" in b"".join(writelines.call_args_list[0][0])

    hap_proto.close()


def _snapshot_request(*extra_headers: bytes) -> bytes:
    body = b'{"image-height":360,"resource-type":"image","image-width":640,"aid":1}'
    return (
        b"POST /resource HTTP/1.1\r\n"
        b"Host: HASS\\032Bridge._hap._tcp.local\r\n"
        + b"".join(header + b"\r\n" for header in extra_headers)
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\nContent-Type: application/hap+json\r\n\r\n"
        + body
    )


SNAPSHOT_REQUEST = _snapshot_request()


def _delayed_snapshot_protocol(
    driver: AccessoryDriver, release: asyncio.Event
) -> tuple[hap_protocol.HAPServerProtocol, Accessory, MagicMock]:
    """Create an encrypted connection whose snapshots wait for ``release``.

    Each snapshot is numbered, so responses can be told apart.
    """
    transport = MagicMock(get_extra_info=Mock(return_value=("1.2.3.4", 5)))
    transport.is_closing = Mock(return_value=False)
    calls: list[int] = []

    async def _async_get_snapshot(*_: Any) -> bytes:
        number = len(calls) + 1
        calls.append(number)
        await release.wait()
        return b"snap%d" % number

    acc = Accessory(driver, "TestAcc")
    acc.async_get_snapshot = _async_get_snapshot  # ty: ignore[missing-attribute]
    acc.snapshot_calls = calls  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)
    hap_proto = hap_protocol.HAPServerProtocol(MagicMock(), {}, driver)
    hap_proto.connection_made(transport)
    _enable_encryption(hap_proto)
    return hap_proto, acc, transport


async def test_request_waits_for_delayed_response(driver: AccessoryDriver) -> None:
    """A request behind a delayed response is answered after it, in order."""
    release = asyncio.Event()
    hap_proto, acc, transport = _delayed_snapshot_protocol(driver, release)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(SNAPSHOT_REQUEST + SNAPSHOT_REQUEST)
        await asyncio.sleep(0)
        assert writelines.call_args_list == []
        transport.close.assert_not_called()
        # The second request is not dispatched before the first is answered.
        assert acc.snapshot_calls == [1]  # ty: ignore[missing-attribute]

        release.set()
        await _await_response(hap_proto)
        await asyncio.sleep(0)
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    responses = [b"".join(call[0]) for call in writelines.call_args_list]
    assert len(responses) == 2
    assert responses[0].endswith(b"snap1")
    assert responses[1].endswith(b"snap2")
    transport.close.assert_not_called()
    hap_proto.close()


async def test_events_wait_for_delayed_response(driver: AccessoryDriver) -> None:
    """Events are not sent while a delayed response is outstanding."""
    release = asyncio.Event()
    hap_proto, acc, _ = _delayed_snapshot_protocol(driver, release)
    assert acc.aid is not None
    driver.topics[get_topic(acc.aid, 9)] = {hap_proto.peername}

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(SNAPSHOT_REQUEST)
        hap_proto.queue_event(
            {HAP_REPR_AID: acc.aid, HAP_REPR_IID: 9, HAP_REPR_VALUE: 1}, False
        )
        hap_proto._send_events()
        assert writelines.call_args_list == []

        release.set()
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    writes = [b"".join(call[0]) for call in writelines.call_args_list]
    assert len(writes) == 2
    assert writes[0].startswith(b"HTTP/1.1 200 OK")
    assert writes[1].startswith(b"EVENT/1.0 200 OK")
    hap_proto.close()


async def test_cancelled_delayed_response_answers(driver: AccessoryDriver) -> None:
    """A cancelled delayed response is answered with a failure."""
    hap_proto, _, _ = _delayed_snapshot_protocol(driver, asyncio.Event())

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(SNAPSHOT_REQUEST)
        response = hap_proto.response
        assert response is not None
        assert response.task is not None
        await asyncio.sleep(0)
        response.task.cancel()
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert b"".join(writelines.call_args_list[0][0]).startswith(
        b"HTTP/1.1 500 Internal Server Error"
    )
    hap_proto.close()


async def test_close_cancels_delayed_response(driver: AccessoryDriver) -> None:
    """Closing the connection stops the work for a delayed response."""
    hap_proto, _, _ = _delayed_snapshot_protocol(driver, asyncio.Event())
    hap_proto.data_received(SNAPSHOT_REQUEST)
    response = hap_proto.response
    assert response is not None
    assert response.task is not None

    hap_proto.close()
    await _await_response(hap_proto)

    assert response.task.cancelled()


async def test_held_data_is_limited(driver: AccessoryDriver) -> None:
    """A peer cannot make us buffer without limit while a response is pending."""
    hap_proto, _, transport = _delayed_snapshot_protocol(driver, asyncio.Event())
    hap_proto.data_received(SNAPSHOT_REQUEST)
    chunk = b"x" * 65536

    for _ in range(hap_protocol.MAX_REQUEST_BODY_SIZE // len(chunk)):
        hap_proto.data_received(chunk)
    transport.close.assert_not_called()

    hap_proto.data_received(chunk)
    transport.close.assert_called_once()


async def test_held_data_is_limited_across_delayed_responses(
    driver: AccessoryDriver,
) -> None:
    """Data still held when the next response is delayed counts toward the limit."""
    release = asyncio.Event()
    hap_proto, _, transport = _delayed_snapshot_protocol(driver, release)
    padding = b"x" * (hap_protocol.MAX_REQUEST_BODY_SIZE - len(SNAPSHOT_REQUEST))

    with patch.object(hap_proto.transport, "writelines"):
        hap_proto.data_received(SNAPSHOT_REQUEST)
        hap_proto.data_received(SNAPSHOT_REQUEST + padding)
        release.set()
        await _await_response(hap_proto)
        release.clear()
        # The second snapshot is pending now and the padding is still held.
        assert hap_proto.response is not None
        transport.close.assert_not_called()

        hap_proto.data_received(SNAPSHOT_REQUEST + b"x")

    transport.close.assert_called_once()
    await asyncio.sleep(0)


async def test_events_coalesce_after_delayed_response(
    driver: AccessoryDriver,
) -> None:
    """Events whose coalescing window is still open wait for it."""
    release = asyncio.Event()
    hap_proto, acc, _ = _delayed_snapshot_protocol(driver, release)
    assert acc.aid is not None
    driver.topics[get_topic(acc.aid, 9)] = {hap_proto.peername}

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(SNAPSHOT_REQUEST)
        # Schedules the coalescing timer, which has not fired yet.
        hap_proto.queue_event(
            {HAP_REPR_AID: acc.aid, HAP_REPR_IID: 9, HAP_REPR_VALUE: 1}, False
        )
        release.set()
        await _await_response(hap_proto)
        await asyncio.sleep(0)
        assert len(writelines.call_args_list) == 1

        hap_proto._send_events()  # the timer fires

    assert b"".join(writelines.call_args_list[1][0]).startswith(b"EVENT/1.0 200 OK")
    hap_proto.close()


async def test_connection_close_after_delayed_response(
    driver: AccessoryDriver,
) -> None:
    """A delayed response to a request with Connection: close closes it."""
    release = asyncio.Event()
    hap_proto, _, transport = _delayed_snapshot_protocol(driver, release)

    with patch.object(hap_proto.transport, "writelines"):
        hap_proto.data_received(_snapshot_request(b"Connection: close"))
        release.set()
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    transport.close.assert_called_once()


async def test_camera_snapshot_timeout_async(driver: AccessoryDriver) -> None:
    """Test camera snapshot timeout is handled."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_: Any) -> bytes:
        await asyncio.sleep(10)
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    # get_snapshot/async_get_snapshot are camera-only methods, not part
    # of Accessory's interface; attach them dynamically so the handler
    # dispatches to them.
    acc.async_get_snapshot = _async_get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with (
        patch.object(hap_handler, "RESPONSE_TIMEOUT", 0.1),
        patch.object(hap_proto.transport, "writelines") as writelines,
    ):
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await asyncio.sleep(0.3)

    assert b"-70402" in b"".join(writelines.call_args_list[0][0])

    hap_proto.close()


def test_upgrade_to_encrypted(driver: AccessoryDriver) -> None:
    """Test we switch to encrypted wen we get a shared_key."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    acc = Accessory(driver, "TestAcc")
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    assert hap_proto.hap_crypto is None

    def _make_response(*_: Any) -> HAPResponse:
        response = HAPResponse()
        response.shared_key = b"newkey"
        return response

    with (
        patch.object(hap_proto.transport, "writelines"),
        patch.object(hap_proto.handler, "dispatch", _make_response),
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )

    assert hap_proto.hap_crypto is not None
    assert hap_proto.derive_key(b"salt", b"info") == hap_hkdf(
        b"newkey", b"salt", b"info"
    )

    hap_proto.close()
    assert hap_proto.derive_key(b"salt", b"info") is None


def _pair_verify(hap_proto: Any, shared_key: bytes) -> None:
    """Process a handler response that upgrades to ``shared_key``."""
    response = HAPResponse()
    response.shared_key = shared_key
    with patch.object(hap_proto, "send_response"):
        hap_proto._process_response(response)


def test_derive_key_uses_latest_pair_verify(driver: AccessoryDriver) -> None:
    """Test a repeated pair-verify replaces the session key."""
    driver.add_accessory(Accessory(driver, "TestAcc"))
    hap_proto = hap_protocol.HAPServerProtocol(MagicMock(), {}, driver)
    hap_proto.connection_made(MagicMock())
    assert hap_proto.derive_key(b"salt", b"info") is None

    _pair_verify(hap_proto, b"firstkey")
    _pair_verify(hap_proto, b"secondkey")

    assert hap_proto.derive_key(b"salt", b"info") == hap_hkdf(
        b"secondkey", b"salt", b"info"
    )


def test_close_keeps_newer_connection_with_same_peername(
    driver: AccessoryDriver,
) -> None:
    """Test closing a stale connection does not drop a newer one."""
    loop = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))
    old_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    transport = MagicMock()
    old_proto.connection_made(transport)
    _pair_verify(old_proto, b"oldkey")
    new_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    new_proto.connection_made(transport)
    _pair_verify(new_proto, b"newkey")
    assert connections[old_proto.peername] is new_proto

    old_proto.close()

    assert connections[new_proto.peername] is new_proto
    assert old_proto.derive_key(b"salt", b"info") is None
    assert new_proto.derive_key(b"salt", b"info") is not None


async def test_pairing_changed(driver: AccessoryDriver) -> None:
    """Test we update mdns when the pairing changes."""
    loop = MagicMock()

    run_in_executor_called = False

    async def _run_in_executor(*_: Any) -> None:
        nonlocal run_in_executor_called
        run_in_executor_called = True

    loop.run_in_executor = _run_in_executor
    transport = MagicMock()
    connections = {}

    acc = Accessory(driver, "TestAcc")
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    def _make_response(*_: Any) -> HAPResponse:
        response = HAPResponse()
        response.pairing_changed = True
        return response

    with (
        patch.object(hap_proto.transport, "write"),
        patch.object(hap_proto.handler, "dispatch", _make_response),
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        await asyncio.sleep(0)

    assert run_in_executor_called is True
    hap_proto.close()


async def test_camera_snapshot_throws_an_exception(driver: AccessoryDriver) -> None:
    """Test camera snapshot that throws an exception."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_: Any) -> NoReturn:
        raise ValueError("any error")

    acc = Accessory(driver, "TestAcc")
    # get_snapshot/async_get_snapshot are camera-only methods, not part
    # of Accessory's interface; attach them dynamically so the handler
    # dispatches to them.
    acc.async_get_snapshot = _async_get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert b"-70402" in b"".join(writelines.call_args_list[0][0])

    hap_proto.close()


async def test_camera_snapshot_times_out(driver: AccessoryDriver) -> None:
    """Test camera snapshot times out."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    def _get_snapshot(*_: Any) -> NoReturn:
        raise asyncio.TimeoutError("timeout")

    acc = Accessory(driver, "TestAcc")
    # Camera-only snapshot method, attached dynamically so the
    # handler dispatches to it.
    acc.get_snapshot = _get_snapshot  # ty: ignore[missing-attribute]
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await _await_response(hap_proto)
        await asyncio.sleep(0)

    assert b"-70402" in b"".join(writelines.call_args_list[0][0])

    hap_proto.close()


async def test_camera_snapshot_missing_accessory(driver: AccessoryDriver) -> None:
    """Test camera snapshot that throws an exception."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    bridge = Bridge(driver, "Test Bridge")
    driver.add_accessory(bridge)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"POST /resource HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\n"
            b"Content-Length: 79\r\n"
            b"Content-Type: application/hap+json\r\n"
            b"\r\n"
            b'{"image-height":360,"resource-type":"image","image-width":640,"aid":14116'
            b"20844}"
        )
        await asyncio.sleep(0)

    assert hap_proto.response is None
    assert b"-70402" in b"".join(writelines.call_args_list[0][0])
    hap_proto.close()


async def test_idle_timeout(driver: AccessoryDriver) -> None:
    """Test we close the connection once we reach the idle timeout."""
    loop = asyncio.get_event_loop()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with (
        patch.object(hap_protocol, "IDLE_CONNECTION_TIMEOUT_SECONDS", 0),
        patch.object(hap_proto, "close") as hap_proto_close,
        patch.object(hap_proto.transport, "write") as writer,
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
        hap_proto.check_idle(time.time())
        assert hap_proto_close.called is True


async def test_does_not_timeout(driver: AccessoryDriver) -> None:
    """Test we do not timeout the connection if we have not reached the idle."""
    loop = asyncio.get_event_loop()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with (
        patch.object(hap_proto, "close") as hap_proto_close,
        patch.object(hap_proto.transport, "write") as writer,
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\n"
            b"Host: Bridge\\032C77C47._hap._tcp.local\r\n"
            b"Content-Length: 6\r\n"
            b"Content-Type: application/pairing+tlv8\r\n"
            b"\r\n"
            b"\x00\x01\x00\x06\x01\x01"
        )
        assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
        hap_proto.check_idle(time.time())
        assert hap_proto_close.called is False


def test_explicit_close(driver: AccessoryDriver) -> None:
    """Test an explicit connection close."""
    loop = MagicMock()

    transport = MockTransport()
    connections = {}

    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("TemperatureSensor")
    acc.add_service(service)
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    _enable_encryption(hap_proto)
    transport_ = hap_proto.transport
    assert transport_ is not None
    assert transport_.is_closing() is False

    with patch.object(hap_proto.transport, "writelines") as writelines:
        hap_proto.data_received(
            b"GET /characteristics?id=3762173001.7 HTTP/1.1\r\n"
            b"Host: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )
        hap_proto.data_received(
            b"GET /characteristics?id=1.5 HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Host: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n"
            b"\r\n"
            b""
        )

    join0 = b"".join(writelines.call_args_list[0][0])
    assert b"Content-Length:" in join0
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in join0
    assert b"-70402" in join0

    join1 = b"".join(writelines.call_args_list[1][0])
    assert b"Content-Length:" in join1
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in join1
    assert b"TestAcc" in join1

    transport_ = hap_proto.transport
    assert transport_ is not None
    assert transport_.is_closing() is True
