"""Integration test: a user-persisted client identity survives a server restart.

The accessory persists the pairing (the client's public key) in the driver
state file; the client persists its own identity with
:meth:`pyhap.client.ClientIdentity.save`. After a "restart" (a new driver on
the same state file), the loaded identity still pair-verifies.
"""

from __future__ import annotations

import pathlib
from typing import Any, cast

import pytest

from pyhap.client import ClientIdentity
from pyhap.const import STANDALONE_AID

from .helpers import SwitchAccessory, build_switch, iid_of

# Real socket + crypto round trips plus two server starts; the CI default of
# --timeout=2 is too tight on shared runners.
pytestmark = pytest.mark.timeout(10)


async def test_persisted_identity_reconnects_after_server_restart(
    make_hap_server: Any, tmp_path: pathlib.Path
) -> None:
    """A saved identity pair-verifies against a restarted driver."""
    state_file = tmp_path / "accessory.state"
    key_file = tmp_path / "controller.key"

    # First run: pair a fresh identity, persist it user-side, use it.
    server = await make_hap_server(build_switch, client_count=1, state_file=state_file)
    identity = server.identities[0]
    identity.save(key_file)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)
    client = await server.connect(0)
    frame = await client.write_characteristic(STANDALONE_AID, on_iid, True)
    assert frame.status == 204
    # Flush the pairing to the state file before the "restart".
    server.driver.persist()
    await server.stop()

    # Restart: a new driver on the same state file, no new pairing, and the
    # identity loaded back from the user's key file.
    restarted = await make_hap_server(
        build_switch, client_count=0, state_file=state_file
    )
    loaded = ClientIdentity.load(key_file)
    assert loaded.uuid == identity.uuid
    assert loaded.public_key_bytes == identity.public_key_bytes

    accessory2 = cast("SwitchAccessory", restarted.driver.accessory)
    on_iid2 = iid_of(accessory2, accessory2.char_on)
    client2 = await restarted.connect_identity(loaded)
    frame = await client2.read_characteristic(STANDALONE_AID, on_iid2)
    assert frame.status == 200
    frame = await client2.write_characteristic(STANDALONE_AID, on_iid2, True)
    assert frame.status == 204
