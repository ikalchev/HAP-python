"""The long-term pairing identity of a HAP client (controller)."""

from __future__ import annotations

from dataclasses import dataclass
import uuid as uuid_module

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass(frozen=True)
class ClientIdentity:
    """The long-term pairing identity of a HAP controller.

    The accessory must be paired with this identity first (e.g. via
    :meth:`pyhap.accessory_driver.AccessoryDriver.pair`, bypassing the slow
    SRP pair-setup), so the client only needs a pair-verify to open an
    encrypted session.
    """

    uuid: str
    private_key: ed25519.Ed25519PrivateKey

    @classmethod
    def generate(cls, index: int = 0) -> ClientIdentity:
        """Generate a deterministic identity for the given index."""
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
