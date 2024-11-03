"""This module implements the communication of HAP.

The HAPServer is the point of contact to and from the world.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from .hap_protocol import HAPServerProtocol
from .util import callback

if TYPE_CHECKING:
    from .accessory_driver import AccessoryDriver

logger = logging.getLogger(__name__)

IDLE_CONNECTION_CHECK_INTERVAL_SECONDS = 300


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

    def __init__(
        self, addr_port: Tuple[str, int], accessory_handler: "AccessoryDriver"
    ) -> None:
        """Create a HAP Server."""
        self._addr_port = addr_port
        self.connections: Dict[Tuple[str, int], HAPServerProtocol] = {}
        self.accessory_handler = accessory_handler
        self.server: Optional[asyncio.Server] = None
        self._connection_cleanup: Optional[asyncio.TimerHandle] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def async_start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the http-hap server."""
        self.loop = loop
        self.server = await loop.create_server(
            lambda: HAPServerProtocol(loop, self.connections, self.accessory_handler),
            self._addr_port[0],
            self._addr_port[1],
        )
        self.async_cleanup_connections()

    @callback
    def async_cleanup_connections(self) -> None:
        """Cleanup stale connections."""
        now = time.time()
        for hap_proto in list(self.connections.values()):
            hap_proto.check_idle(now)
        self._connection_cleanup = self.loop.call_later(
            IDLE_CONNECTION_CHECK_INTERVAL_SECONDS, self.async_cleanup_connections
        )

    @callback
    def async_stop(self) -> None:
        """Stop the server.

        This method must be run in the event loop.
        """
        self._connection_cleanup.cancel()
        for hap_proto in list(self.connections.values()):
            hap_proto.close()
        self.server.close()
        self.connections.clear()

    def push_event(
        self, data: bytes, client_addr: Tuple[str, int], immediate: bool = False
    ) -> bool:
        """Queue an event to the current connection with the provided data.

        :param data: The characteristic changes
        :type data: dict

        :param client_addr: A client (address, port) tuple to which to send the data.
        :type client_addr: tuple <str, int>

        :return: True if sending was successful, False otherwise.
        :rtype: bool
        """
        hap_server_protocol = self.connections.get(client_addr)
        if hap_server_protocol is None:
            logger.debug("No socket for %s", client_addr)
            return False
        hap_server_protocol.queue_event(data, immediate)
        return True
