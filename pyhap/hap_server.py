"""This module implements the communication of HAP.

The HAPServer is the point of contact to and from the world.
"""

import logging

from .util import callback
from .hap_protocol import HAPServerProtocol

logger = logging.getLogger(__name__)


class HAPServer:
    """Point of contact for HAP clients.

    The HAPServer handles all incoming client requests (e.g. pair) and also handles
    communication from Accessories to clients (value changes). The outbound communication
    is something like HTTP push.

    @note: Client requests responses as well as outgoing event notifications happen through
    the same socket for the same client. This introduces a race condition - an Accessory
    decides to push a change in current temperature, while in the same time the HAP client
    decides to query the state of the Accessory. To overcome this the HAPSocket class
    implements exclusive access to the send methods.
    """

    EVENT_MSG_STUB = (
        b"EVENT/1.0 200 OK\r\n"
        b"Content-Type: application/hap+json\r\n"
        b"Content-Length: "
    )

    @classmethod
    def create_hap_event(cls, bytesdata):
        """Creates a HAP HTTP EVENT response for the given data.

        @param data: Payload of the request.
        @type data: bytes
        """
        return (
            cls.EVENT_MSG_STUB
            + str(len(bytesdata)).encode("utf-8")
            + b"\r\n" * 2
            + bytesdata
        )

    def __init__(self, addr_port, accessory_handler):
        """Create a HAP Server."""
        self._addr_port = addr_port
        self.connections = {}  # (address, port): socket
        self.accessory_handler = accessory_handler
        self.server = None
        self._serve_task = None

    async def async_start(self, loop):
        """Start the http-hap server."""
        self.server = await loop.create_server(
            lambda: HAPServerProtocol(loop, self.connections, self.accessory_handler),
            self._addr_port[0],
            self._addr_port[1],
        )

    @callback
    def async_stop(self):
        """Stop the server.

        This method must be run in the event loop.
        """
        self.server.close()
        for hap_server_protocol in list(self.connections.values()):
            if hap_server_protocol:
                hap_server_protocol.close()
        self.connections.clear()

    def push_event(self, bytesdata, client_addr):
        """Send an event to the current connection with the provided data.

        :param bytesdata: The data to send.
        :type bytesdata: bytes

        :param client_addr: A client (address, port) tuple to which to send the data.
        :type client_addr: tuple <str, int>

        :return: True if sending was successful, False otherwise.
        :rtype: bool
        """
        hap_server_protocol = self.connections.get(client_addr)
        if hap_server_protocol is None:
            logger.debug("No socket for %s", client_addr)
            return False
        hap_server_protocol.write(self.create_hap_event(bytesdata))
        return True
