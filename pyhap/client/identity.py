"""The long-term pairing identity of a HAP client (controller)."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
from dataclasses import dataclass
import json
import os
import tempfile
from typing import Any
import uuid as uuid_module
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass(frozen=True)
class ClientIdentity:
    """The long-term pairing identity of a HAP controller.

    The accessory must be paired with this identity first (e.g. via
    :meth:`pyhap.accessory_driver.AccessoryDriver.pair`, bypassing the slow
    SRP pair-setup), so the client only needs a pair-verify to open an
    encrypted session.

    The identity lives in memory only; :meth:`save` and :meth:`load` persist
    it (UUID + private key) to a file, like a real controller keeps its
    long-term key in a keychain. The accessory persists the pairing (the
    public key) in its own state file, so a saved identity reconnects as the
    same controller after a restart.

    ``accessory_ltpk`` optionally pins the accessory's raw long-term public
    key (trust on first use): a client with a pinned LTPK verifies the
    accessory during pair-verify, one without does not.
    """

    uuid: str
    private_key: ed25519.Ed25519PrivateKey
    accessory_ltpk: bytes | None = None

    @classmethod
    def generate(cls, index: int = 0) -> ClientIdentity:
        """Generate an identity for the given index.

        The UUID is deterministic per ``index``, but the private key is
        generated fresh on every call, so persist it with :meth:`save` to
        reuse the identity.
        """
        pair_uuid = str(
            uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"hap-client-{index}")
        )
        return cls(pair_uuid, ed25519.Ed25519PrivateKey.generate())

    @property
    def uuid_bytes(self) -> bytes:
        """The UUID in the wire format the server expects."""
        return self.uuid.encode("utf-8")

    @property
    def public_key_bytes(self) -> bytes:
        """The raw long-term public key bytes."""
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-serializable form of the identity.

        Holds the UUID and the raw private key (hex), plus the raw public
        key (hex), which :meth:`from_dict` checks against the private key.
        The pinned accessory LTPK (hex) is included when set.
        """
        private_key_bytes = self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        data = {
            "uuid": self.uuid,
            "private_key": private_key_bytes.hex(),
            "public_key": self.public_key_bytes.hex(),
        }
        if self.accessory_ltpk is not None:
            data["accessory_ltpk"] = self.accessory_ltpk.hex()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ClientIdentity:
        """Rebuild an identity from :meth:`to_dict` output.

        :raise ValueError: If the data is malformed or the stored public key
            does not match the private key.
        """
        try:
            uuid_str = data["uuid"]
            private_key_hex = data["private_key"]
            public_key_hex = data["public_key"]
        except (KeyError, TypeError) as error:
            raise ValueError(
                "Not a client identity (expected 'uuid', 'private_key' and "
                f"'public_key'): {data!r}"
            ) from error
        if not isinstance(uuid_str, str):
            raise ValueError(f"Invalid client uuid: {uuid_str!r}")
        try:
            UUID(uuid_str)
        except ValueError as error:
            raise ValueError(f"Invalid client uuid: {uuid_str!r}") from error
        try:
            private_key_bytes = bytes.fromhex(private_key_hex)
            public_key_bytes = bytes.fromhex(public_key_hex)
        except (TypeError, ValueError) as error:
            raise ValueError("Client identity keys must be hex encoded") from error
        try:
            private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
                private_key_bytes
            )
        except ValueError as error:
            raise ValueError("Invalid client private key") from error
        identity = cls(uuid_str, private_key)
        if identity.public_key_bytes != public_key_bytes:
            raise ValueError("Client public key does not match the private key")
        ltpk_hex = data.get("accessory_ltpk")
        if ltpk_hex is not None:
            try:
                ltpk_bytes = bytes.fromhex(ltpk_hex)
                ed25519.Ed25519PublicKey.from_public_bytes(ltpk_bytes)
            except (TypeError, ValueError) as error:
                raise ValueError("Invalid pinned accessory_ltpk") from error
            identity = dataclasses.replace(identity, accessory_ltpk=ltpk_bytes)
        return identity

    def save(self, path: str | os.PathLike[str]) -> None:
        """Persist the identity to ``path`` as JSON.

        The write is atomic: a tempfile created with 0600 permissions in the
        same directory is ``os.replace``d into place, so a crash cannot leave
        a half-written key behind and the key file is never world-readable.
        ``~`` in ``path`` is expanded.
        """
        target = os.path.expanduser(os.fspath(path))
        directory = os.path.dirname(target) or "."
        tmp_filename = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=directory, delete=False, encoding="utf-8"
            ) as file_handle:
                tmp_filename = file_handle.name
                json.dump(self.to_dict(), file_handle, indent=2)
                file_handle.write("\n")
            os.replace(tmp_filename, target)
        finally:
            if tmp_filename is not None and os.path.exists(tmp_filename):
                os.remove(tmp_filename)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ClientIdentity:
        """Load an identity saved with :meth:`save`.

        :raise ValueError: If the file does not hold a valid identity.
        :raise OSError: If the file does not exist or cannot be read.
        """
        target = os.path.expanduser(os.fspath(path))
        with open(target, encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        return cls.from_dict(data)
