"""Unit tests for HAPClient construction helpers."""

import pathlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pyhap.client import ClientIdentity, HAPClient


def _ltpk() -> bytes:
    """A raw accessory long-term public key to construct clients with."""
    key = ed25519.Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def test_new_persistent_creates_and_backs_up_identity(
    tmp_path: pathlib.Path,
) -> None:
    """A missing file leads to a new identity that is saved there."""
    path = tmp_path / "controller.key"
    assert not path.exists()
    client = HAPClient.new_persistent(filepath=path, accessory_ltpk=_ltpk())
    assert path.exists()
    saved = ClientIdentity.load(path)
    assert saved.uuid == client.identity.uuid
    assert saved.public_key_bytes == client.identity.public_key_bytes


def test_new_persistent_loads_existing_identity(tmp_path: pathlib.Path) -> None:
    """An existing file is loaded instead of being overwritten."""
    path = tmp_path / "controller.key"
    identity = ClientIdentity.generate(5)
    identity.save(path)
    client = HAPClient.new_persistent(filepath=path, accessory_ltpk=_ltpk())
    assert client.identity.uuid == identity.uuid
    assert client.identity.public_key_bytes == identity.public_key_bytes


def test_new_persistent_works_without_ltpk(tmp_path: pathlib.Path) -> None:
    """No LTPK needed: the client is created and the session key still set up."""
    path = tmp_path / "controller.key"
    client = HAPClient.new_persistent(filepath=path)
    assert client.accessory_ltpk is None
    assert ClientIdentity.load(path).accessory_ltpk is None


def test_new_persistent_pins_and_reuses_ltpk(tmp_path: pathlib.Path) -> None:
    """A passed LTPK is pinned in the file; later calls pick it up from there."""
    path = tmp_path / "controller.key"
    ltpk = _ltpk()
    first = HAPClient.new_persistent(filepath=path, accessory_ltpk=ltpk)
    assert first.accessory_ltpk == ltpk
    assert ClientIdentity.load(path).accessory_ltpk == ltpk

    second = HAPClient.new_persistent(filepath=path)
    assert second.accessory_ltpk == ltpk
    assert second.identity.uuid == first.identity.uuid
