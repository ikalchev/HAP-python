"""Module for `State` class."""
from cryptography.hazmat.primitives.asymmetric import ed25519

from pyhap import util
from pyhap.const import (
    CLIENT_PROP_PERMS,
    DEFAULT_CONFIG_VERSION,
    DEFAULT_PORT,
    MAX_CONFIG_VERSION,
)

ADMIN_BIT = 0x01


class State:
    """Class to store all (semi-)static information.

    That includes all needed for setup of driver and pairing.
    """

    def __init__(self, *, address=None, mac=None, pincode=None, port=None):
        """Initialize a new object. Create key pair.

        Must be called with keyword arguments.
        """
        self.address = address or util.get_local_address()
        self.mac = mac or util.generate_mac()
        self.pincode = pincode or util.generate_pincode()
        self.port = port or DEFAULT_PORT
        self.setup_id = util.generate_setup_id()

        self.config_version = DEFAULT_CONFIG_VERSION
        self.paired_clients = {}
        self.client_properties = {}

        self.private_key = ed25519.Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.accessories_hash = None

    # ### Pairing ###
    @property
    def paired(self):
        """Return if main accessory is currently paired."""
        return len(self.paired_clients) > 0

    def is_admin(self, client_uuid):
        """Check if a paired client is an admin."""
        if client_uuid not in self.client_properties:
            return False
        return bool(self.client_properties[client_uuid][CLIENT_PROP_PERMS] & ADMIN_BIT)

    def add_paired_client(self, client_uuid, client_public, perms):
        """Add a given client to dictionary of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID

        :param client_public: The client's public key
            (not the session public key).
        :type client_public: bytes
        """
        self.paired_clients[client_uuid] = client_public
        self.client_properties[client_uuid] = {CLIENT_PROP_PERMS: ord(perms)}

    def remove_paired_client(self, client_uuid):
        """Remove a given client from dictionary of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID
        """
        self.paired_clients.pop(client_uuid)
        self.client_properties.pop(client_uuid)

        # All pairings must be removed when the last admin is removed
        if not any(self.is_admin(client_uuid) for client_uuid in self.paired_clients):
            self.paired_clients.clear()
            self.client_properties.clear()

    def set_accessories_hash(self, accessories_hash):
        """Set the accessories hash and increment the config version if needed."""
        if self.accessories_hash == accessories_hash:
            return False
        self.accessories_hash = accessories_hash
        self.increment_config_version()
        return True

    def increment_config_version(self):
        """Increment the config version."""
        self.config_version += 1
        if self.config_version > MAX_CONFIG_VERSION:
            self.config_version = 1
