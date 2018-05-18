"""Module for `State` class."""
import ed25519

from pyhap import util
from pyhap.const import DEFAULT_CONFIG_VERSION, DEFAULT_PORT


class State:
    """Class to store all (semi-)static information.

    That includes all needed for setup of driver and pairing.
    """

    def __init__(self, *, address=None, mac=None,
                 pincode=None, port=None):
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

        sk, vk = ed25519.create_keypair()
        self.private_key = sk
        self.public_key = vk

    # ### Pairing ###
    @property
    def paired(self):
        """Return if main accessory is currently paired."""
        return len(self.paired_clients) > 0

    def add_paired_client(self, client_uuid, client_public):
        """Add a given client to dictionary of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID

        :param client_public: The client's public key
            (not the session public key).
        :type client_public: bytes
        """
        self.paired_clients[client_uuid] = client_public

    def remove_paired_client(self, client_uuid):
        """Remove a given client from dictionary of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID
        """
        self.paired_clients.pop(client_uuid)
