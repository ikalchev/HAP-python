"""Tests for pyhap.encoder."""
import json
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pyhap import encoder
from pyhap.const import HAP_PERMISSIONS
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
    admin_client_uuid = uuid.uuid1()
    admin_client_bytes = str(admin_client_uuid).upper().encode("utf-8")
    state.add_paired_client(
        admin_client_bytes,
        sample_client_pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
        HAP_PERMISSIONS.ADMIN,
    )
    assert state.is_admin(admin_client_uuid)
    user_client_uuid = uuid.uuid1()
    user_client_bytes = str(user_client_uuid).upper().encode("utf-8")
    state.add_paired_client(
        user_client_bytes,
        sample_client_pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
        HAP_PERMISSIONS.USER,
    )
    assert not state.is_admin(user_client_uuid)
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
    assert state.client_properties == config_loaded.client_properties


def test_migration_to_include_client_properties():
    """Verify we build client properties if its missing since it was not present in older versions."""
    mac = generate_mac()
    _pk = ed25519.Ed25519PrivateKey.generate()
    sample_client_pk = _pk.public_key()
    state = State(mac=mac)
    admin_client_uuid = uuid.uuid1()
    admin_client_bytes = str(admin_client_uuid).upper().encode("utf-8")
    state.add_paired_client(
        admin_client_bytes,
        sample_client_pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
        HAP_PERMISSIONS.ADMIN,
    )
    assert state.is_admin(admin_client_uuid)
    user_client_uuid = uuid.uuid1()
    user_client_bytes = str(user_client_uuid).upper().encode("utf-8")
    state.add_paired_client(
        user_client_bytes,
        sample_client_pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
        HAP_PERMISSIONS.USER,
    )
    assert not state.is_admin(user_client_uuid)

    config_loaded = State()
    config_loaded.config_version += 2  # change the default state.
    enc = encoder.AccessoryEncoder()
    with tempfile.TemporaryFile(mode="r+") as fp:
        enc.persist(fp, state)
        fp.seek(0)
        loaded = json.load(fp)
        fp.seek(0)
        del loaded["client_properties"]
        json.dump(loaded, fp)
        fp.truncate()
        fp.seek(0)
        enc.load_into(fp, config_loaded)

    # When client_permissions are missing, all clients
    # are imported as admins for backwards compatibility
    assert config_loaded.is_admin(admin_client_uuid)
    assert config_loaded.is_admin(user_client_uuid)
