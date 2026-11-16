"""Tests for the accessory connection lifecycle hooks."""

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable as ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from pyhap import hap_handler, tlv
from pyhap.accessory import STANDALONE_AID, Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import HAP_PERMISSIONS
from pyhap.hap_protocol import HAPServerProtocol

CLIENT_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")
CLIENT_UUID_BYTES = str(CLIENT_UUID).upper().encode("utf-8")
CLIENT2_UUID_BYTES = b"7d0d1ee9-46fe-4a56-a115-69df3f6860c2"
WRITER = ("1.2.3.4", 5)
ASYNC_TIMEOUT = 1.0


async def _run_scheduled_jobs(rounds: int = 8) -> None:
    """Let jobs scheduled by the driver and their tasks run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _ed25519_keypair() -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    """A client identity key pair and its raw public key bytes."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return private_key, public_key


async def _pair_verify(driver: AccessoryDriver, handler: Any) -> None:
    """Complete pair verify on ``handler`` as CLIENT_UUID."""
    client_private_key, client_public_key_bytes = _ed25519_keypair()
    driver.pair(CLIENT_UUID_BYTES, client_public_key_bytes, HAP_PERMISSIONS.ADMIN)

    handler.is_encrypted = False
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M1,
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        client_public_key_bytes,
    )
    handler.handle_pair_verify()

    assert handler.enc_context is not None
    server_public_key: bytes = handler.enc_context["public_key"].public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    material = client_public_key_bytes + CLIENT_UUID_BYTES + server_public_key
    unencrypted_data = tlv.encode(
        hap_handler.HAP_TLV_TAGS.USERNAME,
        CLIENT_UUID_BYTES,
        hap_handler.HAP_TLV_TAGS.PROOF,
        client_private_key.sign(material),
    )
    cipher = ChaCha20Poly1305(handler.enc_context["pre_session_key"])

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.ENCRYPTED_DATA,
        cipher.encrypt(
            hap_handler.HAPServerHandler.PVERIFY_2_NONCE,
            bytes(unencrypted_data),
            b"",
        ),
    )
    handler.handle_pair_verify()


async def test_default_hooks_are_async_noops(driver: AccessoryDriver) -> None:
    """The base hooks are coroutine functions that do nothing."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)
    driver.add_accessory(acc)

    assert inspect.iscoroutinefunction(acc.client_connected)
    assert inspect.iscoroutinefunction(acc.client_verified)
    assert await acc.client_connected(WRITER) is None
    assert await acc.client_verified(WRITER, CLIENT_UUID) is None


async def test_client_connected_of_a_failing_hook_is_isolated(
    driver: AccessoryDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising hook is logged and never reaches the connection."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)
    calls: list[Any] = []

    async def _hook(client_addr: Any) -> None:
        calls.append(client_addr)
        raise RuntimeError("boom")

    acc.client_connected = _hook
    driver.add_accessory(acc)
    driver.loop = asyncio.get_running_loop()

    driver.client_connected(WRITER)
    await _run_scheduled_jobs()

    assert calls == [WRITER]
    assert "failed to run client_connected" in caplog.text
    assert "boom" in caplog.text


async def test_client_verified_of_a_failing_hook_is_isolated(
    driver: AccessoryDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising verified hook is logged and never reaches the response."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)

    async def _hook(client_addr: Any, client_uuid: Any) -> None:
        raise RuntimeError("boom")

    acc.client_verified = _hook
    driver.add_accessory(acc)
    driver.loop = asyncio.get_running_loop()

    driver.client_verified(WRITER, CLIENT_UUID)
    await _run_scheduled_jobs()

    assert "failed to run client_verified" in caplog.text


async def test_bridge_tells_every_accessory(driver: AccessoryDriver) -> None:
    """A bridge forwards both hooks to each bridged accessory."""
    bridge = Bridge(driver, "Test Bridge")
    first = Accessory(driver, "First")
    second = Accessory(driver, "Second")
    bridge.add_accessory(first)
    bridge.add_accessory(second)
    driver.add_accessory(bridge)
    driver.loop = asyncio.get_running_loop()

    with (
        patch.object(first, "client_connected", AsyncMock()) as first_connected,
        patch.object(second, "client_connected", AsyncMock()) as second_connected,
        patch.object(first, "client_verified", AsyncMock()) as first_verified,
        patch.object(second, "client_verified", AsyncMock()) as second_verified,
    ):
        driver.client_connected(WRITER)
        driver.client_verified(WRITER, CLIENT_UUID)
        await _run_scheduled_jobs()

    first_connected.assert_awaited_once_with(WRITER)
    second_connected.assert_awaited_once_with(WRITER)
    first_verified.assert_awaited_once_with(WRITER, CLIENT_UUID)
    second_verified.assert_awaited_once_with(WRITER, CLIENT_UUID)


async def test_bridge_isolates_a_failing_accessory(driver: AccessoryDriver) -> None:
    """One bridged accessory's raising hook does not stop the others."""
    bridge = Bridge(driver, "Test Bridge")
    first = Accessory(driver, "First")
    second = Accessory(driver, "Second")
    bridge.add_accessory(first)
    bridge.add_accessory(second)
    driver.add_accessory(bridge)
    driver.loop = asyncio.get_running_loop()

    with (
        patch.object(
            first, "client_connected", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        patch.object(second, "client_connected", AsyncMock()) as second_connected,
    ):
        driver.client_connected(WRITER)
        await _run_scheduled_jobs()

    second_connected.assert_awaited_once_with(WRITER)


async def test_connection_made_tells_the_accessory(driver: AccessoryDriver) -> None:
    """An accepted connection runs the accessory's client_connected."""
    driver.add_accessory(Accessory(driver, "Test Accessory"))
    driver.loop = asyncio.get_running_loop()

    with (
        patch.object(driver.accessory, "client_connected", AsyncMock()) as connected,
        patch.object(driver.accessory, "client_verified", AsyncMock()) as verified,
    ):
        transport = MagicMock()
        transport.get_extra_info.return_value = WRITER
        proto = HAPServerProtocol(asyncio.get_running_loop(), {}, driver)
        proto.connection_made(transport)
        await _run_scheduled_jobs()

    connected.assert_awaited_once_with(WRITER)
    verified.assert_not_awaited()


async def test_pair_verify_tells_the_accessory(driver: AccessoryDriver) -> None:
    """A finished pair verify runs the accessory's client_verified."""
    driver.add_accessory(Accessory(driver, "Test Accessory"))
    driver.loop = asyncio.get_running_loop()
    handler = hap_handler.HAPServerHandler(driver, WRITER)

    with patch.object(driver.accessory, "client_verified", AsyncMock()) as verified:
        await _pair_verify(driver, handler)
        await _run_scheduled_jobs()

    verified.assert_awaited_once_with(WRITER, CLIENT_UUID)


async def test_pair_verify_failure_tells_the_accessory_nothing(
    driver: AccessoryDriver,
) -> None:
    """A pair verify that fails authentication runs no hook."""
    driver.add_accessory(Accessory(driver, "Test Accessory"))
    driver.loop = asyncio.get_running_loop()
    handler = hap_handler.HAPServerHandler(driver, WRITER)

    with patch.object(driver.accessory, "client_verified", AsyncMock()) as verified:
        # An unknown client cannot finish pair verify: the accessory
        # stays paired, but CLIENT2 never paired with it.
        client_private_key, client_public_key_bytes = _ed25519_keypair()
        driver.pair(CLIENT_UUID_BYTES, client_public_key_bytes, HAP_PERMISSIONS.ADMIN)
        handler.is_encrypted = False
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.request_body = tlv.encode(
            hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
            hap_handler.HAP_TLV_STATES.M1,
            hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
            client_public_key_bytes,
        )
        handler.handle_pair_verify()
        assert handler.enc_context is not None
        cipher = ChaCha20Poly1305(handler.enc_context["pre_session_key"])
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.request_body = tlv.encode(
            hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
            hap_handler.HAP_TLV_STATES.M3,
            hap_handler.HAP_TLV_TAGS.ENCRYPTED_DATA,
            cipher.encrypt(
                hap_handler.HAPServerHandler.PVERIFY_2_NONCE,
                bytes(
                    tlv.encode(hap_handler.HAP_TLV_TAGS.USERNAME, CLIENT2_UUID_BYTES)
                ),
                b"",
            ),
        )
        handler.handle_pair_verify()
        await _run_scheduled_jobs()

    verified.assert_not_awaited()
    assert handler.client_uuid is None
