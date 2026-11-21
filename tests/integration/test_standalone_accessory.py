"""Integration tests for a standalone (non-bridged) Accessory over real HAP."""

from __future__ import annotations

import pathlib
from typing import Any, cast

import pytest

from pyhap.client import ClientIdentity, HAPClient
from pyhap.const import HAP_PERMISSIONS, HAP_SERVER_STATUS, STANDALONE_AID

from .helpers import (
    ACCESSORY_INFORMATION_TYPE,
    SWITCH_SERVICE_TYPE,
    SwitchAccessory,
    body_json,
    build_switch,
    characteristic_entries,
    characteristic_value,
    iid_of,
)

# Real socket + crypto round trips; the CI default of --timeout=2 is too
# tight on shared runners.
pytestmark = pytest.mark.timeout(10)


async def test_standalone_accessory_end_to_end(make_hap_server: Any) -> None:
    """A paired client lists, reads and writes a standalone accessory."""
    server = await make_hap_server(build_switch)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    assert accessory.aid == STANDALONE_AID
    on_iid = iid_of(accessory, accessory.char_on)
    info = accessory.get_service("AccessoryInformation")
    assert info is not None
    name_iid = iid_of(accessory, info.get_characteristic("Name"))

    client = await server.connect(0)

    # GET /accessories returns exactly the one standalone accessory, with the
    # AccessoryInformation service first and the Switch service present.
    frame = await client.get_accessories()
    assert frame.status == 200
    accessories = body_json(frame)["accessories"]
    assert [acc["aid"] for acc in accessories] == [STANDALONE_AID]
    services = accessories[0]["services"]
    assert services[0]["type"] == ACCESSORY_INFORMATION_TYPE
    assert SWITCH_SERVICE_TYPE in {service["type"] for service in services}

    # Read several characteristics in one request.
    frame = await client.read_characteristics([(1, name_iid), (1, on_iid)])
    assert frame.status == 200
    assert characteristic_value(frame, 1, name_iid) == "Test Switch"
    assert characteristic_value(frame, 1, on_iid) is False

    # Write On and read it back.
    frame = await client.write_characteristic(1, on_iid, True)
    assert frame.status == 204
    frame = await client.read_characteristic(1, on_iid)
    assert frame.status == 200
    assert characteristic_value(frame, 1, on_iid) is True


async def test_read_of_unknown_characteristic_fails_per_characteristic(
    make_hap_server: Any,
) -> None:
    """A read of an unknown IID answers 207 with a failure status for it."""
    server = await make_hap_server(build_switch)
    client = await server.connect(0)

    frame = await client.read_characteristic(STANDALONE_AID, 9999)
    assert frame.status == 207
    entry = characteristic_entries(frame)[(STANDALONE_AID, 9999)]
    assert entry["status"] == HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    assert "value" not in entry


async def test_unpaired_client_is_rejected_at_pair_verify(make_hap_server: Any) -> None:
    """A client whose identity was never paired cannot open a session."""
    server = await make_hap_server(build_switch)
    stranger = HAPClient(ClientIdentity.generate(99), server.accessory_ltpk)
    server.clients.append(stranger)  # closed by the harness
    with pytest.raises(RuntimeError, match="pair-verify rejected"):
        await stranger.connect("127.0.0.1", server.port)


async def test_client_connects_without_pinned_ltpk(
    make_hap_server: Any, tmp_path: pathlib.Path
) -> None:
    """No LTPK needed: the session is encrypted, the accessory unverified."""
    server = await make_hap_server(build_switch, client_count=0)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)

    client = HAPClient.new_persistent(filepath=tmp_path / "controller.key")
    assert client.accessory_ltpk is None
    server.driver.pair(
        client.identity.uuid_bytes,
        client.identity.public_key_bytes,
        HAP_PERMISSIONS.ADMIN,
    )
    await server.connect_client(client)

    frame = await client.write_characteristic(STANDALONE_AID, on_iid, True)
    assert frame.status == 204
    frame = await client.read_characteristic(STANDALONE_AID, on_iid)
    assert characteristic_value(frame, STANDALONE_AID, on_iid) is True
