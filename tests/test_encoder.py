"""Tests for pyhap.encoder."""
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pyhap import encoder
from pyhap.state import State
from pyhap.util import generate_mac


def test_persist_and_load():
    """Stores an Accessory and then loads the stored state into another
    Accessory. Tests if the two accessories have the same property values.
    """
    mac = generate_mac()
    _pk = ed25519.Ed25519PrivateKey.generate()
    sample_client_pk = _pk.public_key()
    state = State(mac=mac)
    state.add_paired_client(
        uuid.uuid1(),
        sample_client_pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )

    config_loaded = State()
    config_loaded.config_version += 2  # change the default state.
    enc = encoder.AccessoryEncoder()
    with tempfile.TemporaryFile(mode="r+") as fp:
        enc.persist(fp, state)
        fp.seek(0)
        enc.load_into(fp, config_loaded)

    assert state.mac == config_loaded.mac
    assert state.private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ) == config_loaded.private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert state.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == config_loaded.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert state.config_version == config_loaded.config_version
    assert state.paired_clients == config_loaded.paired_clients
