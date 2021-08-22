"""This module contains various Accessory encoders.

These are used to persist and load the state of the Accessory, so that
it can work properly after a restart.
"""
import json
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .const import CLIENT_PROP_PERMS


class AccessoryEncoder:
    """This class defines the Accessory encoder interface.

    The AccessoryEncoder is used by the AccessoryDriver to persist and restore the
    state of an Accessory between restarts. This is needed in order to allow iOS
    clients to see the same MAC, public key, etc. of the Accessory they paired with, thus
    allowing an Accessory to "be remembered".

    The idea is:
        - The Accessory(ies) is created and added to an AccessoryDriver.
        - The AccessoryDriver checks if a given file, containing the Accessory's state
            exists. If so, it loads the state into the Accessory. Otherwise, it
            creates the file and persists the state of the Accessory.
        - On every change of the accessory - config change, new (un)paired clients,
            the state is updated.

    You can implement your own encoding logic, but the minimum set of properties that
    must be persisted are:
        - Public and private keys.
        - UUID and public key of all paired clients.
        - MAC address.
        - Config version - ok, this is debatable, but it retains the consistency.
        - Accessories Hash

    The default implementation persists the above properties.

    Note also that AIDs and IIDs must also survive a restore. However, this is managed
    by the Accessory and Bridge classes.

    @see: AccessoryDriver.persist AccessoryDriver.load AccessoryDriver.__init__
    """

    @staticmethod
    def persist(fp, state):
        """Persist the state of the given Accessory to the given file object.

        Persists:
            - MAC address.
            - Public and private key.
            - UUID and public key of paired clients.
            - Config version.
            - Accessories Hash
        """
        paired_clients = {
            str(client): bytes.hex(key) for client, key in state.paired_clients.items()
        }
        client_properties = {
            str(client): props for client, props in state.client_properties.items()
        }
        config_state = {
            "mac": state.mac,
            "config_version": state.config_version,
            "paired_clients": paired_clients,
            "client_properties": client_properties,
            "accessories_hash": state.accessories_hash,
            "private_key": bytes.hex(
                state.private_key.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            ),
            "public_key": bytes.hex(
                state.public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ),
        }
        json.dump(config_state, fp)

    @staticmethod
    def load_into(fp, state):
        """Load the accessory state from the given file object into the given Accessory.

        @see: AccessoryEncoder.persist
        """
        loaded = json.load(fp)
        state.mac = loaded["mac"]
        state.accessories_hash = loaded.get("accessories_hash")
        state.config_version = loaded["config_version"]
        if "client_properties" in loaded:
            state.client_properties = {
                uuid.UUID(client): props
                for client, props in loaded["client_properties"].items()
            }
        else:
            # If "client_properties" does not exist, everyone
            # before that was paired as an admin
            state.client_properties = {
                uuid.UUID(client): {CLIENT_PROP_PERMS: 1}
                for client in loaded["paired_clients"]
            }
        state.paired_clients = {
            uuid.UUID(client): bytes.fromhex(key)
            for client, key in loaded["paired_clients"].items()
        }
        state.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(loaded["private_key"])
        )
        state.public_key = ed25519.Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(loaded["public_key"])
        )
