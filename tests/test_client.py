"""Unit tests for HAPClient construction helpers."""

import pathlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from pyhap.client import ClientIdentity, HAPClient, load_accessory_ltpk
from pyhap.encoder import AccessoryEncoder
from pyhap.state import State


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


def _write_state_file(path: pathlib.Path) -> bytes:
    """Write a real accessory.state via the encoder; return its raw LTPK."""
    state = State(address="127.0.0.1")
    with open(path, "w", encoding="utf-8") as file_handle:
        AccessoryEncoder().persist(file_handle, state)
    return state.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def test_load_accessory_ltpk(tmp_path: pathlib.Path) -> None:
    """The LTPK is read back from a real accessory.state file."""
    state_file = tmp_path / "accessory.state"
    expected = _write_state_file(state_file)
    assert load_accessory_ltpk(state_file) == expected


def test_load_accessory_ltpk_rejects_bad_file(tmp_path: pathlib.Path) -> None:
    """Files without a valid public_key raise ValueError."""
    bad = tmp_path / "bad.state"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_accessory_ltpk(bad)
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_accessory_ltpk(bad)


def test_new_persistent_pins_ltpk_from_accessory_state(
    tmp_path: pathlib.Path,
) -> None:
    """accessory_state= resolves and pins the LTPK; later calls reuse the pin."""
    state_file = tmp_path / "accessory.state"
    expected = _write_state_file(state_file)
    key_file = tmp_path / "controller.key"

    client = HAPClient.new_persistent(filepath=key_file, accessory_state=state_file)
    assert client.accessory_ltpk == expected
    assert ClientIdentity.load(key_file).accessory_ltpk == expected

    again = HAPClient.new_persistent(filepath=key_file)
    assert again.accessory_ltpk == expected
    assert again.identity.uuid == client.identity.uuid


def test_new_persistent_rejects_conflicting_ltpk(tmp_path: pathlib.Path) -> None:
    """A pinned LTPK is authoritative; a different one raises, file untouched."""
    path = tmp_path / "controller.key"
    first = HAPClient.new_persistent(filepath=path, accessory_ltpk=_ltpk())
    with pytest.raises(ValueError, match="different accessory LTPK"):
        HAPClient.new_persistent(filepath=path, accessory_ltpk=_ltpk())
    assert ClientIdentity.load(path).accessory_ltpk == first.accessory_ltpk
