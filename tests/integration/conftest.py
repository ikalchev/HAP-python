"""Fixtures that run a real AccessoryDriver in-process with real HAP clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import pathlib
import socket

from cryptography.hazmat.primitives import serialization
import pytest

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.client import ClientIdentity, HAPClient
from pyhap.const import HAP_PERMISSIONS

AccessoryFactory = Callable[[AccessoryDriver], Accessory]


def reserve_port() -> int:
    """Reserve a loopback port for the HAP server.

    ``AccessoryDriver`` cannot express "pick an ephemeral port" (``State``
    falls back to the default port for port 0), so a port is reserved and
    immediately released here for the driver to bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HAPServerHarness:
    """A running ``AccessoryDriver`` with pre-paired HAP clients.

    The identities are paired directly via ``AccessoryDriver.pair`` (which
    bypasses the slow SRP pair-setup), so each :meth:`connect` only needs
    the pair-verify handshake before it can speak the encrypted transport.
    """

    def __init__(
        self,
        driver: AccessoryDriver,
        identities: list[ClientIdentity],
        accessory_ltpk: bytes,
    ) -> None:
        """Store the driver, the paired identities and the accessory LTPK."""
        self.driver = driver
        self.identities = identities
        self.accessory_ltpk = accessory_ltpk
        self.clients: list[HAPClient] = []
        self._stopped = False

    @classmethod
    async def start(
        cls,
        accessory_factory: AccessoryFactory,
        persist_file: pathlib.Path,
        client_count: int = 1,
    ) -> HAPServerHarness:
        """Build the accessory, pair ``client_count`` clients and start the driver."""
        driver = AccessoryDriver(
            loop=asyncio.get_running_loop(),
            address="127.0.0.1",
            listen_address="127.0.0.1",
            port=reserve_port(),
            persist_file=str(persist_file),
            advertise=False,
        )
        driver.add_accessory(accessory_factory(driver))
        identities = [ClientIdentity.generate(index) for index in range(client_count)]
        for identity in identities:
            driver.pair(
                identity.uuid_bytes, identity.public_key_bytes, HAP_PERMISSIONS.ADMIN
            )
        await driver.async_start()
        ltpk = driver.state.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return cls(driver, identities, ltpk)

    @property
    def port(self) -> int:
        """The loopback port the HAP server listens on."""
        return self.driver.state.port

    async def connect(self, index: int = 0) -> HAPClient:
        """Connect pre-paired client ``index`` (pair-verify included)."""
        return await self.connect_identity(self.identities[index])

    async def connect_identity(self, identity: ClientIdentity) -> HAPClient:
        """Connect an arbitrary identity, e.g. one loaded from disk."""
        return await self.connect_client(HAPClient(identity, self.accessory_ltpk))

    async def connect_client(self, client: HAPClient) -> HAPClient:
        """Connect an already-built client, e.g. from ``HAPClient.new_persistent``."""
        self.clients.append(client)
        await client.connect("127.0.0.1", self.port)
        return client

    async def stop(self) -> None:
        """Close all clients and stop the driver. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        for client in self.clients:
            await client.close()
        self.clients.clear()
        await self.driver.async_stop()


@pytest.fixture
async def make_hap_server(
    tmp_path: pathlib.Path,
) -> AsyncIterator[Callable[..., Awaitable[HAPServerHarness]]]:
    """Start a driver for the accessory built by the given factory.

    Usage: ``server = await make_hap_server(lambda driver: MyAccessory(driver))``.
    Pass ``client_count=N`` to pre-pair N clients, and ``state_file`` to run
    on a specific driver state file (e.g. to restart on the same one). Every
    started server is stopped when the test ends.
    """
    harnesses: list[HAPServerHarness] = []

    async def _make(
        accessory_factory: AccessoryFactory,
        client_count: int = 1,
        state_file: pathlib.Path | None = None,
    ) -> HAPServerHarness:
        if state_file is None:
            state_file = tmp_path / f"accessory-{len(harnesses)}.state"
        harness = await HAPServerHarness.start(
            accessory_factory, state_file, client_count
        )
        harnesses.append(harness)
        return harness

    yield _make

    for harness in harnesses:
        await harness.stop()
