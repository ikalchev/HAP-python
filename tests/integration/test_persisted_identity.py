"""Integration test: a persisted client identity survives a server restart.

The accessory persists the pairing (the client's public key) in the driver
state file; the client persists its own identity via
``HAPClient.new_persistent``. After a "restart" (a new driver on the same
state file), the same identity pair-verifies again.
"""

from __future__ import annotations

import pathlib
from typing import Any, cast

import pytest

from pyhap.client import ClientIdentity, HAPClient
from pyhap.const import HAP_PERMISSIONS, STANDALONE_AID

from .helpers import SwitchAccessory, build_switch, iid_of

# Real socket + crypto round trips plus two server starts; the CI default of
# --timeout=2 is too tight on shared runners.
pytestmark = pytest.mark.timeout(10)


async def test_persisted_identity_reconnects_after_server_restart(
    make_hap_server: Any, tmp_path: pathlib.Path
) -> None:
    """new_persistent() backs up a new identity, then loads it after a restart."""
    state_file = tmp_path / "accessory.state"
    key_file = tmp_path / "controller.key"

    # First run: create the identity (backed up to key_file), pair it, use it.
    server = await make_hap_server(build_switch, client_count=0, state_file=state_file)
    client = HAPClient.new_persistent(
        filepath=key_file, accessory_ltpk=server.accessory_ltpk
    )
    assert key_file.exists()
    server.driver.pair(
        client.identity.uuid_bytes,
        client.identity.public_key_bytes,
        HAP_PERMISSIONS.ADMIN,
    )
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)
    await server.connect_client(client)
    frame = await client.write_characteristic(STANDALONE_AID, on_iid, True)
    assert frame.status == 204
    # Flush the pairing to the state file before the "restart".
    server.driver.persist()
    await server.stop()

    # Restart: a new driver on the same state file, no new pairing. The same
    # key file now yields the existing identity (and the pinned accessory
    # LTPK from the first run), which pair-verifies.
    restarted = await make_hap_server(
        build_switch, client_count=0, state_file=state_file
    )
    client2 = HAPClient.new_persistent(filepath=key_file)
    assert client2.identity.uuid == client.identity.uuid
    assert client2.accessory_ltpk == server.accessory_ltpk
    assert ClientIdentity.load(key_file).public_key_bytes == (
        client.identity.public_key_bytes
    )

    accessory2 = cast("SwitchAccessory", restarted.driver.accessory)
    on_iid2 = iid_of(accessory2, accessory2.char_on)
    await restarted.connect_client(client2)
    frame = await client2.read_characteristic(STANDALONE_AID, on_iid2)
    assert frame.status == 200
    frame = await client2.write_characteristic(STANDALONE_AID, on_iid2, True)
    assert frame.status == 204
