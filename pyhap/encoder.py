"""This module contains various Accessory encoders.

These are used to persist and load the state of the Accessory, so that
it can work properly after a restart.
"""
import json
import uuid

import ed25519

from pyhap.util import fromhex, tohex


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
        """
        paired_clients = {str(client): tohex(key)
                          for client, key in state.paired_clients.items()}
        config_state = {
            'mac': state.mac,
            'config_version': state.config_version,
            'paired_clients': paired_clients,
            'private_key': tohex(state.private_key.to_seed()),
            'public_key': tohex(state.public_key.to_bytes()),
        }
        json.dump(config_state, fp)

    @staticmethod
    def load_into(fp, state):
        """Load the accessory state from the given file object into the given Accessory.

        @see: AccessoryEncoder.persist
        """
        loaded = json.load(fp)
        state.mac = loaded['mac']
        state.config_version = loaded['config_version']
        state.paired_clients = {uuid.UUID(client): fromhex(key)
                                for client, key in
                                loaded['paired_clients'].items()}
        state.private_key = ed25519.SigningKey(fromhex(loaded['private_key']))
        state.public_key = ed25519.VerifyingKey(fromhex(loaded['public_key']))
