"""Integration tests for the pyhap.client CLI (``python -m pyhap.client``).

The full two-machine flow: ``new`` on the client side, ``register`` offline
into the accessory's state file, accessory restart, client connects; then
``deregister`` and the client is rejected at pair-verify.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from pyhap.client import ClientIdentity, HAPClient, cli

from .helpers import build_switch

# Real socket + crypto round trips plus several server starts; the CI default
# of --timeout=2 is too tight on shared runners.
pytestmark = pytest.mark.timeout(10)


async def test_cli_new_register_connect_deregister(
    make_hap_server: Any, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI drives the whole offline pairing flow end to end."""
    state_file = tmp_path / "accessory.state"
    key_file = tmp_path / "client.state"

    # The accessory runs once, creating accessory.state, then stops.
    server = await make_hap_server(build_switch, client_count=0, state_file=state_file)
    await server.stop()

    # Client machine: create the persistent controller identity.
    assert cli.main(["new", "-f", str(key_file)]) == 0
    assert "Creating new HAP controller" in capsys.readouterr().out
    identity = ClientIdentity.load(key_file)

    # Accessory machine, offline: register the printed JSON in accessory.state.
    register_file = tmp_path / "client.json"
    register_file.write_text(
        json.dumps(
            {"uuid": identity.uuid, "public_key": identity.public_key_bytes.hex()}
        ),
        encoding="utf-8",
    )
    assert cli.main(["register", "-f", str(register_file), "-a", str(state_file)]) == 0
    assert cli.main(["list", "-a", str(state_file)]) == 0
    assert f"{identity.uuid}: admin" in capsys.readouterr().out

    # Accessory restarts; the client pair-verifies and controls it.
    restarted = await make_hap_server(
        build_switch, client_count=0, state_file=state_file
    )
    client = HAPClient.new_persistent(filepath=key_file, accessory_state=state_file)
    await restarted.connect_client(client)
    frame = await client.get_accessories()
    assert frame.status == 200
    await restarted.stop()

    # Deregister offline; after another restart the client is rejected.
    assert cli.main(["deregister", "-a", str(state_file), identity.uuid, "--yes"]) == 0
    final = await make_hap_server(build_switch, client_count=0, state_file=state_file)
    rejected = HAPClient.new_persistent(filepath=key_file, accessory_state=state_file)
    with pytest.raises(RuntimeError, match="pair-verify rejected"):
        await final.connect_client(rejected)
