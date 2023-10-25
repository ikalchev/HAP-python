"""Tests for the HAPServerProtocol."""
import asyncio
import time
from unittest.mock import MagicMock, Mock, patch

from cryptography.exceptions import InvalidTag
import pytest

from pyhap import hap_handler, hap_protocol
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.hap_handler import HAPResponse


class MockTransport(asyncio.Transport):  # pylint: disable=abstract-method
    """A mock transport."""

    _is_closing: bool = False

    def set_write_buffer_limits(self, high=None, low=None):
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

    def __init__(self):
        """Create the mock object."""
        self._crypt_in_buffer = bytearray()  # Encrypted buffer

    def receive_data(self, buffer):
        """Receive data into the encrypted buffer."""
        self._crypt_in_buffer += buffer

    def decrypt(self):
        """Mock as plaintext."""
        decrypted = self._crypt_in_buffer
        self._crypt_in_buffer = bytearray()  # Encrypted buffer
        return decrypted

    def encrypt(self, data):
        """Mock as plaintext."""
        return data


def test_connection_management(driver):
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


def test_pair_setup(driver):
    """Verify an non-encrypt request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True

    hap_proto.close()


def test_http10_close(driver):
    """Test we handle http/1.0."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\nConnection:close\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    assert len(writer.call_args_list) == 1
    assert not connections
    hap_proto.close()


def test_invalid_content_length(driver):
    """Test we handle invalid content length."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\nConnection:close\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 2\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\nConnection:close\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 2\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
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


def test_invalid_client_closes_connection(driver):
    """Test we handle client closing the connection."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\nConnection:close\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        hap_proto.data_received(b"")

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    assert len(writer.call_args_list) == 1
    assert not connections
    hap_proto.close()


def test_pair_setup_split_between_packets(driver):
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


def test_get_accessories_without_crypto(driver):
    """Verify an non-encrypt request that expected to be encrypted."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert b"-70401" in writer.call_args_list[0][0][0]


def test_get_accessories_with_crypto(driver):
    """Verify an encrypt accessories request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert b"accessories" in writer.call_args_list[0][0][0]


def test_get_characteristics_with_crypto(driver):
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

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"GET /characteristics?id=3762173001.7 HTTP/1.1\r\nHost: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )
        hap_proto.data_received(
            b"GET /characteristics?id=1.5 HTTP/1.1\r\nHost: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert b"Content-Length:" in writer.call_args_list[0][0][0]
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in writer.call_args_list[0][0][0]
    assert b"-70402" in writer.call_args_list[0][0][0]

    assert b"Content-Length:" in writer.call_args_list[1][0][0]
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in writer.call_args_list[1][0][0]
    assert b"TestAcc" in writer.call_args_list[1][0][0]


def test_set_characteristics_with_crypto(driver):
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

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'PUT /characteristics HTTP/1.1\r\nHost: HASS12\\032AD1C22._hap._tcp.local\r\nContent-Length: 49\r\nContent-Type: application/hap+json\r\n\r\n{"characteristics":[{"aid":1,"iid":9,"ev":true}]}'  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert writer.call_args_list[0][0][0] == b"HTTP/1.1 204 No Content\r\n\r\n"


def test_crypto_failure_closes_connection(driver):
    """Verify a decrypt failure closes the connection."""
    loop = MagicMock()
    addr_info = ("1.2.3.4", 5)
    transport = MagicMock(get_extra_info=Mock(return_value=addr_info))
    connections = {}

    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True
    assert connections[addr_info] == hap_proto
    with patch.object(hap_proto.hap_crypto, "decrypt", side_effect=InvalidTag):
        hap_proto.data_received(b"any")  # pylint: disable=line-too-long

    assert len(connections) == 0


def test_empty_encrypted_data(driver):
    """Verify an encrypt request when we start with an empty block."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True
    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(b"")
        hap_proto.data_received(
            b"GET /accessories HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert b"accessories" in writer.call_args_list[0][0][0]


def test_http_11_keep_alive(driver):
    """Verify we can handle multiple requests."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_connection_closed(driver):
    """Test camera snapshot when the other side closes the connection."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=True)
    connections = {}

    async def _async_get_snapshot(*_):
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    acc.async_get_snapshot = _async_get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        hap_proto.close()
        await hap_proto.response.task
        await asyncio.sleep(0)

    assert writer.call_args_list == []

    hap_proto.close()


def test_camera_snapshot_without_snapshot_support(driver):
    """Test camera snapshot fails if there is not support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    acc = Accessory(driver, "TestAcc")
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )

    hap_proto.close()
    assert b"-70402" in writer.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_camera_snapshot_works_sync(driver):
    """Test camera snapshot works if there is support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    def _get_snapshot(*_):
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    acc.get_snapshot = _get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        await hap_proto.response.task
        await asyncio.sleep(0)

    assert b"fakesnap" in writer.call_args_list[0][0][0]

    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_works_async(driver):
    """Test camera snapshot works if there is support for it."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_):
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    acc.async_get_snapshot = _async_get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        await hap_proto.response.task
        await asyncio.sleep(0)

    assert b"fakesnap" in writer.call_args_list[0][0][0]

    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_timeout_async(driver):
    """Test camera snapshot timeout is handled."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_):
        await asyncio.sleep(10)
        return b"fakesnap"

    acc = Accessory(driver, "TestAcc")
    acc.async_get_snapshot = _async_get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_handler, "RESPONSE_TIMEOUT", 0.1), patch.object(
        hap_proto.transport, "write"
    ) as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        await asyncio.sleep(0.3)

    assert b"-70402" in writer.call_args_list[0][0][0]

    hap_proto.close()


def test_upgrade_to_encrypted(driver):
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

    def _make_response(*_):
        response = HAPResponse()
        response.shared_key = b"newkey"
        return response

    with patch.object(hap_proto.transport, "write"), patch.object(
        hap_proto.handler, "dispatch", _make_response
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )

    assert hap_proto.hap_crypto is not None

    hap_proto.close()


@pytest.mark.asyncio
async def test_pairing_changed(driver):
    """Test we update mdns when the pairing changes."""
    loop = MagicMock()

    run_in_executor_called = False

    async def _run_in_executor(*_):
        nonlocal run_in_executor_called
        run_in_executor_called = True

    loop.run_in_executor = _run_in_executor
    transport = MagicMock()
    connections = {}

    acc = Accessory(driver, "TestAcc")
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    def _make_response(*_):
        response = HAPResponse()
        response.pairing_changed = True
        return response

    with patch.object(hap_proto.transport, "write"), patch.object(
        hap_proto.handler, "dispatch", _make_response
    ):
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        await asyncio.sleep(0)

    assert run_in_executor_called is True
    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_throws_an_exception(driver):
    """Test camera snapshot that throws an exception."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    async def _async_get_snapshot(*_):
        raise ValueError("any error")

    acc = Accessory(driver, "TestAcc")
    acc.async_get_snapshot = _async_get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        try:
            await hap_proto.response.task
        except Exception:  # pylint: disable=broad-except
            pass
        await asyncio.sleep(0)

    assert b"-70402" in writer.call_args_list[0][0][0]

    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_times_out(driver):
    """Test camera snapshot times out."""
    loop = MagicMock()
    transport = MagicMock()
    transport.is_closing = Mock(return_value=False)
    connections = {}

    def _get_snapshot(*_):
        raise asyncio.TimeoutError("timeout")

    acc = Accessory(driver, "TestAcc")
    acc.get_snapshot = _get_snapshot
    driver.add_accessory(acc)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        try:
            await hap_proto.response.task
        except Exception:  # pylint: disable=broad-except
            pass
        await asyncio.sleep(0)

    assert b"-70402" in writer.call_args_list[0][0][0]

    hap_proto.close()


@pytest.mark.asyncio
async def test_camera_snapshot_missing_accessory(driver):
    """Test camera snapshot that throws an exception."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

    bridge = Bridge(driver, "Test Bridge")
    driver.add_accessory(bridge)

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b'POST /resource HTTP/1.1\r\nHost: HASS\\032Bridge\\032BROZ\\0323BF435._hap._tcp.local\r\nContent-Length: 79\r\nContent-Type: application/hap+json\r\n\r\n{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'  # pylint: disable=line-too-long
        )
        await asyncio.sleep(0)

    assert hap_proto.response is None
    assert b"-70402" in writer.call_args_list[0][0][0]
    hap_proto.close()


@pytest.mark.asyncio
async def test_idle_timeout(driver):
    """Test we close the connection once we reach the idle timeout."""
    loop = asyncio.get_event_loop()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_protocol, "IDLE_CONNECTION_TIMEOUT_SECONDS", 0), patch.object(
        hap_proto, "close"
    ) as hap_proto_close, patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
        hap_proto.check_idle(time.time())
        assert hap_proto_close.called is True


@pytest.mark.asyncio
async def test_does_not_timeout(driver):
    """Test we do not timeout the connection if we have not reached the idle."""
    loop = asyncio.get_event_loop()
    transport = MagicMock()
    connections = {}
    driver.add_accessory(Accessory(driver, "TestAcc"))

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto, "close") as hap_proto_close, patch.object(
        hap_proto.transport, "write"
    ) as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.1\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )
        assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
        hap_proto.check_idle(time.time())
        assert hap_proto_close.called is False


def test_explicit_close(driver: AccessoryDriver):
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

    hap_proto.hap_crypto = MockHAPCrypto()
    hap_proto.handler.is_encrypted = True
    assert hap_proto.transport.is_closing() is False

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"GET /characteristics?id=3762173001.7 HTTP/1.1\r\nHost: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )
        hap_proto.data_received(
            b"GET /characteristics?id=1.5 HTTP/1.1\r\nConnection: close\r\nHost: HASS\\032Bridge\\032YPHW\\032B223AD._hap._tcp.local\r\n\r\n"  # pylint: disable=line-too-long
        )

    assert b"Content-Length:" in writer.call_args_list[0][0][0]
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in writer.call_args_list[0][0][0]
    assert b"-70402" in writer.call_args_list[0][0][0]

    assert b"Content-Length:" in writer.call_args_list[1][0][0]
    assert b"Transfer-Encoding: chunked\r\n\r\n" not in writer.call_args_list[1][0][0]
    assert b"TestAcc" in writer.call_args_list[1][0][0]

    assert hap_proto.transport.is_closing() is True
