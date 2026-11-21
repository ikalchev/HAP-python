"""Unit tests for ClientIdentity generation and persistence."""

import dataclasses
import json
import os
import pathlib
import stat
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from pyhap.client import ClientIdentity


def test_generate_deterministic_uuid_but_fresh_key() -> None:
    """The same index yields the same UUID but a new private key each time."""
    first = ClientIdentity.generate(3)
    second = ClientIdentity.generate(3)
    assert first.uuid == second.uuid
    assert first.public_key_bytes != second.public_key_bytes


def test_to_from_dict_round_trip() -> None:
    """from_dict(to_dict()) rebuilds an identity whose key can sign."""
    identity = ClientIdentity.generate(0)
    clone = ClientIdentity.from_dict(identity.to_dict())
    assert clone.uuid == identity.uuid
    assert clone.public_key_bytes == identity.public_key_bytes
    signature = clone.private_key.sign(b"material")
    verifying_key = ed25519.Ed25519PublicKey.from_public_bytes(
        identity.public_key_bytes
    )
    verifying_key.verify(signature, b"material")


def test_save_load_round_trip(tmp_path: pathlib.Path) -> None:
    """save() then load() returns the same identity."""
    identity = ClientIdentity.generate(1)
    path = tmp_path / "controller.key"
    identity.save(path)
    loaded = ClientIdentity.load(path)
    assert loaded.to_dict() == identity.to_dict()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_saved_file_is_private(tmp_path: pathlib.Path) -> None:
    """The key file is written with 0600 permissions."""
    path = tmp_path / "controller.key"
    ClientIdentity.generate(0).save(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_save_overwrites_existing_file(tmp_path: pathlib.Path) -> None:
    """Saving onto an existing file replaces it atomically."""
    path = tmp_path / "controller.key"
    path.write_text("old contents", encoding="utf-8")
    identity = ClientIdentity.generate(2)
    identity.save(path)
    assert ClientIdentity.load(path).to_dict() == identity.to_dict()


def test_load_rejects_malformed_files(tmp_path: pathlib.Path) -> None:
    """Corrupt or incomplete identity files raise ValueError."""
    path = tmp_path / "bad.key"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ClientIdentity.load(path)

    good = ClientIdentity.generate(0).to_dict()
    mutations = [
        lambda data: data.pop("private_key"),
        lambda data: data.update(uuid="not-a-uuid"),
        lambda data: data.update(private_key="zz"),
        lambda data: data.update(private_key="00"),
        lambda data: data.update(public_key="00" * 32),
    ]
    for mutate in mutations:
        data = dict(good)
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            ClientIdentity.load(path)


def test_from_dict_rejects_non_mapping() -> None:
    """from_dict on something that is not a mapping raises ValueError."""
    not_a_mapping: Any = ["not", "a", "mapping"]
    with pytest.raises(ValueError):
        ClientIdentity.from_dict(not_a_mapping)


def test_accessory_ltpk_round_trip() -> None:
    """A pinned accessory LTPK survives to_dict/from_dict and save/load."""
    ltpk = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    identity = ClientIdentity.generate(0)
    pinned = dataclasses.replace(identity, accessory_ltpk=ltpk)
    assert ClientIdentity.from_dict(pinned.to_dict()).accessory_ltpk == ltpk
    assert "accessory_ltpk" in pinned.to_dict()
    assert "accessory_ltpk" not in identity.to_dict()


def test_from_dict_rejects_invalid_ltpk() -> None:
    """A malformed pinned accessory LTPK raises ValueError."""
    data = ClientIdentity.generate(0).to_dict()
    data["accessory_ltpk"] = "zz"
    with pytest.raises(ValueError):
        ClientIdentity.from_dict(data)
    data["accessory_ltpk"] = "00"  # valid hex, not a 32 byte key
    with pytest.raises(ValueError):
        ClientIdentity.from_dict(data)
