"""Test for pyhap.state."""
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from pyhap.state import State


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
        assert state.mac == mac
        assert state.pincode == pin
        assert state.port == port

        state = State()
        assert mock_local_addr.called
        assert mock_gen_mac.called
        assert mock_gen_pincode.called
        assert state.port == 51827
        assert state.config_version == 2


def test_pairing():
    """Test if pairing methods work."""
    with patch("pyhap.util.get_local_address"), patch("pyhap.util.generate_mac"), patch(
        "pyhap.util.generate_pincode"
    ), patch("pyhap.util.generate_setup_id"):
        state = State()

    assert not state.paired
    assert not state.paired_clients

    state.add_paired_client("uuid", "public")
    assert state.paired
    assert state.paired_clients == {"uuid": "public"}

    state.remove_paired_client("uuid")
    assert not state.paired
    assert not state.paired_clients
