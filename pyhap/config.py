"""Module for `Config` class."""
import logging

import ed25519

from pyhap import util
from pyhap.const import DEFAULT_CONFIG_VERSION, DEFAULT_PORT

logger = logging.getLogger(__name__)


class Config:
    """Class to store all (semi-)static information.

    That includes all needed for setup of driver and pairing.
    """

    def __init__(self, *, loop, address=None, mac=None,
                 pincode=None, port=None):
        """Initialize a new object. Create key pair.

        Must be called with keyword arguments.
        """
        self.loop = loop

        self._address = address
        self._mac = mac
        self._pincode = pincode
        self._port = port
        self._setup_id = None

        self.config_version = DEFAULT_CONFIG_VERSION
        self.paired_clients = {}

        sk, vk = ed25519.create_keypair()
        self.private_key = sk
        self.public_key = vk

    @property
    def address(self):
        """Return `address` or get local one."""
        if self._address is None:
            self._address = util.get_local_address()
        return self._address

    @property
    def mac(self):
        """Return `mac` address or generate new one if not set."""
        if self._mac is None:
            self._mac = util.generate_mac()
        return self._mac

    @property
    def pincode(self):
        """Return `pincode` or generate new one if not set."""
        if self._pincode is None:
            self._pincode = util.generate_pincode()
        return self._pincode

    @property
    def port(self):
        """Return `port` or set to default."""
        if self._port is None:
            self._port = DEFAULT_PORT
        return self._port

    @property
    def setup_id(self):
        """Return `setup_id` or generate new one if not set."""
        if self._setup_id is None:
            self._setup_id = util.generate_setup_id()
        return self._setup_id

    def set_values(self, *, address=None, config_version=None, mac=None,
                   pincode=None, port=None):
        """Set class values. Must be called with keyword arguments."""
        if address:
            self._address = address
        if config_version:
            self.config_version = config_version
        if mac:
            self._mac = mac
        if pincode:
            self._pincode = pincode
        if port:
            self._port = port

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
