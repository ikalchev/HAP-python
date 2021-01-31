"""Tests for the HAPServerProtocol."""
import asyncio
from unittest.mock import MagicMock, Mock, patch

from cryptography.exceptions import InvalidTag
import pytest

from pyhap import hap_protocol
from pyhap.accessory import Accessory


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

    def encrypt(self, data):  # pylint: disable=no-self-use
        """Mock as plaintext."""
        return data


def test_connection_management(driver):
    """Verify closing the connection removes it from the pool."""
    loop = MagicMock()
    addr_info = ("1.2.3.4", 5)
    transport = MagicMock(get_extra_info=Mock(return_value=addr_info))
    connections = {}

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)
    assert len(connections) == 1
    assert connections[addr_info] == hap_proto
    hap_proto.connection_lost(None)
    assert len(connections) == 0

    hap_proto.connection_made(transport)
    assert len(connections) == 1
    assert connections[addr_info] == hap_proto
    hap_proto.close()
    assert len(connections) == 0


def test_pair_setup(driver):
    """Verify an non-encrypt request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

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

    hap_proto = hap_protocol.HAPServerProtocol(loop, connections, driver)
    hap_proto.connection_made(transport)

    with patch.object(hap_proto.transport, "write") as writer:
        hap_proto.data_received(
            b"POST /pair-setup HTTP/1.0\r\nConnection:close\r\nHost: Bridge\\032C77C47._hap._tcp.local\r\nContent-Length: 6\r\nContent-Type: application/pairing+tlv8\r\n\r\n\x00\x01\x00\x06\x01\x01"  # pylint: disable=line-too-long
        )

    assert writer.call_args_list[0][0][0].startswith(b"HTTP/1.1 200 OK\r\n") is True
    assert len(writer.call_args_list) == 1
    assert connections == {}
    hap_proto.close()


def test_pair_setup_split_between_packets(driver):
    """Verify an non-encrypt request."""
    loop = MagicMock()
    transport = MagicMock()
    connections = {}

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
    assert b"-70402" in writer.call_args_list[0][0][0]
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
    assert writer.call_args_list[0][0][0] == b"HTTP/1.1 204 OK\r\n\r\n"


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


def test_camera_snapshot_without_snapshot_support(driver):
    """Test camera snapshot fails if there is not support for it."""
    loop = MagicMock()
    transport = MagicMock()
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
