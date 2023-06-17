"""Test for pyhap.state."""
from unittest.mock import patch
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from pyhap.const import CLIENT_PROP_PERMS, HAP_PERMISSIONS
from pyhap.state import State

CLIENT_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")
CLIENT_UUID_BYTES = str(CLIENT_UUID).upper().encode("utf-8")
CLIENT2_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c2")
CLIENT2_UUID_BYTES = str(CLIENT2_UUID).upper().encode("utf-8")


def test_setup():
    """Test if State class is setup correctly."""
    with pytest.raises(TypeError):
        State("invalid_argument")

    addr = "172.0.0.1"
    mac = "00:00:00:00:00:00"
    pin = b"123-45-678"
    port = 11111

    private_key = ed25519.Ed25519PrivateKey.generate()

    with patch("pyhap.util.get_local_address") as mock_local_addr, patch(
        "pyhap.util.generate_mac"
    ) as mock_gen_mac, patch("pyhap.util.generate_pincode") as mock_gen_pincode, patch(
        "pyhap.util.generate_setup_id"
    ) as mock_gen_setup_id, patch(
        "cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey.generate",
        return_value=private_key,
    ) as mock_create_keypair:
        state = State(address=addr, mac=mac, pincode=pin, port=port)
        assert not mock_local_addr.called
        assert not mock_gen_mac.called
        assert not mock_gen_pincode.called
        assert mock_gen_setup_id.called
        assert mock_create_keypair.called

        assert state.address == addr
        assert state.addresses == [addr]
        assert state.mac == mac
        assert state.pincode == pin
        assert state.port == port

        state = State()
        assert mock_local_addr.called
        assert mock_gen_mac.called
        assert mock_gen_pincode.called
        assert state.port == 51827
        assert state.config_version == 1


def test_pairing_remove_last_admin():
    """Test if pairing methods work."""
    with patch("pyhap.util.get_local_address"), patch("pyhap.util.generate_mac"), patch(
        "pyhap.util.generate_pincode"
    ), patch("pyhap.util.generate_setup_id"):
        state = State()

    assert not state.paired
    assert not state.paired_clients

    state.add_paired_client(CLIENT_UUID_BYTES, "public", HAP_PERMISSIONS.ADMIN)
    assert state.paired
    assert state.paired_clients == {CLIENT_UUID: "public"}
    assert state.client_properties == {CLIENT_UUID: {CLIENT_PROP_PERMS: 1}}

    state.add_paired_client(CLIENT2_UUID_BYTES, "public", HAP_PERMISSIONS.USER)
    assert state.paired
    assert state.paired_clients == {CLIENT_UUID: "public", CLIENT2_UUID: "public"}
    assert state.client_properties == {
        CLIENT_UUID: {CLIENT_PROP_PERMS: 1},
        CLIENT2_UUID: {CLIENT_PROP_PERMS: 0},
    }
    assert state.uuid_to_bytes == {
        CLIENT_UUID: CLIENT_UUID_BYTES,
        CLIENT2_UUID: CLIENT2_UUID_BYTES,
    }

    # Removing the last admin should remove all non-admins
    state.remove_paired_client(CLIENT_UUID)
    assert not state.paired
    assert not state.paired_clients


def test_pairing_two_admins():
    """Test if pairing methods work."""
    with patch("pyhap.util.get_local_address"), patch("pyhap.util.generate_mac"), patch(
        "pyhap.util.generate_pincode"
    ), patch("pyhap.util.generate_setup_id"):
        state = State()

    assert not state.paired
    assert not state.paired_clients

    state.add_paired_client(CLIENT_UUID_BYTES, "public", HAP_PERMISSIONS.ADMIN)
    assert state.paired
    assert state.paired_clients == {CLIENT_UUID: "public"}
    assert state.client_properties == {CLIENT_UUID: {CLIENT_PROP_PERMS: 1}}

    state.add_paired_client(CLIENT2_UUID_BYTES, "public", HAP_PERMISSIONS.ADMIN)
    assert state.paired
    assert state.paired_clients == {CLIENT_UUID: "public", CLIENT2_UUID: "public"}
    assert state.client_properties == {
        CLIENT_UUID: {CLIENT_PROP_PERMS: 1},
        CLIENT2_UUID: {CLIENT_PROP_PERMS: 1},
    }

    # Removing the admin should leave the other admin
    state.remove_paired_client(CLIENT2_UUID)
    assert state.paired
    assert state.paired_clients == {CLIENT_UUID: "public"}
    assert state.client_properties == {CLIENT_UUID: {CLIENT_PROP_PERMS: 1}}
    assert not state.is_admin(CLIENT2_UUID)
