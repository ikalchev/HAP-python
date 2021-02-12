"""Tests for pyhap.encoder."""
import tempfile
import uuid

import ed25519

import pyhap.encoder as encoder
from pyhap.state import State
from pyhap.util import generate_mac


def test_persist_and_load():
    """Stores an Accessory and then loads the stored state into another
    Accessory. Tests if the two accessories have the same property values.
    """
    mac = generate_mac()
    _pk, sample_client_pk = ed25519.create_keypair()
    state = State(mac=mac)
    state.add_paired_client(uuid.uuid1(), sample_client_pk.to_bytes())

    config_loaded = State()
    config_loaded.config_version += 2  # change the default state.
    enc = encoder.AccessoryEncoder()
    with tempfile.TemporaryFile(mode="r+") as fp:
        enc.persist(fp, state)
        fp.seek(0)
        enc.load_into(fp, config_loaded)

    assert state.mac == config_loaded.mac
    assert state.private_key == config_loaded.private_key
    assert state.public_key == config_loaded.public_key
    assert state.config_version == config_loaded.config_version
    assert state.paired_clients == config_loaded.paired_clients
