"""This module implements the communication of HAP.

The HAPServerProtocol is a protocol implementation that manages the "TLS" of the connection.
"""
import asyncio
import logging
import time

from cryptography.exceptions import InvalidTag
import h11

from pyhap.accessory import get_topic
from pyhap.const import HAP_REPR_AID, HAP_REPR_IID

from .hap_crypto import HAPCrypto
from .hap_event import create_hap_event
from .hap_handler import HAPResponse, HAPServerHandler

logger = logging.getLogger(__name__)

HIGH_WRITE_BUFFER_SIZE = 2 ** 19
# We timeout idle connections after 90 hours as we must
# clean up unused sockets periodically. 90 hours was choosen
# as its the longest time we expect a user to be away from
# their phone or device before they have to resync when they
# reopen homekit.
IDLE_CONNECTION_TIMEOUT_SECONDS = 90 * 60 * 60

EVENT_COALESCE_TIME_WINDOW = 0.5


class HAPServerProtocol(asyncio.Protocol):
    """A asyncio.Protocol implementing the HAP protocol."""

    def __init__(self, loop, connections, accessory_driver) -> None:
        self.loop = loop
        self.conn = h11.Connection(h11.SERVER)
        self.connections = connections
        self.accessory_driver = accessory_driver
        self.handler = None
        self.peername = None
        self.transport = None

        self.request = None
        self.request_body = None
        self.response = None

        self.last_activity = None
        self.hap_crypto = None
        self._event_timer = None
        self._event_queue = []

        self.start_time = None

    def connection_lost(self, exc: Exception) -> None:
        """Handle connection lost."""
        logger.debug(
            "%s (%s): Connection lost to %s: %s",
            self.peername,
            self.handler.client_uuid,
            self.accessory_driver.accessory.display_name,
            exc,
        )
        self.accessory_driver.connection_lost(self.peername)
        self.close()

    def connection_made(self, transport: asyncio.Transport) -> None:
        """Handle incoming connection."""
        self.last_activity = time.time()
        peername = transport.get_extra_info("peername")
        logger.info(
            "%s: Connection made to %s",
            peername,
            self.accessory_driver.accessory.display_name,
        )
        # Ensure we do not write a partial encrypted response
        # as it can cause the controller to send a RST and drop
        # the connection with large responses.
        transport.set_write_buffer_limits(high=HIGH_WRITE_BUFFER_SIZE)
        self.transport = transport
        self.peername = peername
        self.connections[peername] = self
        self.handler = HAPServerHandler(self.accessory_driver, peername)

    def write(self, data: bytes) -> None:
        """Write data to the client."""
        self.last_activity = time.time()
        if self.hap_crypto:
            result = self.hap_crypto.encrypt(data)
            logger.debug(
                "%s (%s): Send encrypted: %s",
                self.peername,
                self.handler.client_uuid,
                data,
            )
            self.transport.write(result)
        else:
            logger.debug(
                "%s (%s): Send unencrypted: %s",
                self.peername,
                self.handler.client_uuid,
                data,
            )
            self.transport.write(data)

    def close(self) -> None:
        """Remove the connection and close the transport."""
        if self.peername in self.connections:
            del self.connections[self.peername]
        self.transport.write_eof()
        self.transport.close()

    def queue_event(self, data: dict, immediate: bool) -> None:
        """Queue an event for sending."""
        self._event_queue.append(data)
        if immediate:
            self.loop.call_soon(self._send_events)
        elif not self._event_timer:
            self._event_timer = self.loop.call_later(
                EVENT_COALESCE_TIME_WINDOW, self._send_events
            )

    def send_response(self, response: HAPResponse) -> None:
        """Send a HAPResponse object."""
        body_len = len(response.body)
        if body_len:
            # Force Content-Length as iOS can sometimes
            # stall if it gets chunked encoding
            response.headers.append(("Content-Length", str(body_len)))
        self.write(
            self.conn.send(
                h11.Response(
                    status_code=response.status_code,
                    reason=response.reason,
                    headers=response.headers,
                )
            )
            + self.conn.send(h11.Data(data=response.body))
            + self.conn.send(h11.EndOfMessage())
        )

    def finish_and_close(self):
        """Cleanly finish and close the connection."""
        self.conn.send(h11.ConnectionClosed())
        self.close()

    def check_idle(self, now) -> None:
        """Abort when do not get any data within the timeout."""
        if self.last_activity + IDLE_CONNECTION_TIMEOUT_SECONDS >= now:
            return
        logger.info(
            "%s: Idle time out after %s to %s",
            self.peername,
            IDLE_CONNECTION_TIMEOUT_SECONDS,
            self.accessory_driver.accessory.display_name,
        )
        self.close()

    def data_received(self, data: bytes) -> None:
        """Process new data from the socket."""
        self.last_activity = time.time()
        if self.hap_crypto:
            self.hap_crypto.receive_data(data)
            try:
                unencrypted_data = self.hap_crypto.decrypt()
            except InvalidTag as ex:
                logger.debug(
                    "%s (%s): Decrypt failed, closing connection: %s",
                    self.peername,
                    self.handler.client_uuid,
                    ex,
                )
                self.close()
                return
            if unencrypted_data == b"":
                logger.debug("No decryptable data")
                return
            logger.debug(
                "%s (%s): Recv decrypted: %s",
                self.peername,
                self.handler.client_uuid,
                unencrypted_data,
            )
            self.conn.receive_data(unencrypted_data)
        else:
            self.conn.receive_data(data)
            logger.debug(
                "%s (%s): Recv unencrypted: %s",
                self.peername,
                self.handler.client_uuid,
                data,
            )
        self._process_events()

    def _process_events(self):
        """Process pending events."""
        try:
            while self._process_one_event():
                if self.conn.our_state is h11.MUST_CLOSE:
                    self.finish_and_close()
                    return
        except h11.ProtocolError as protocol_ex:
            self._handle_invalid_conn_state(protocol_ex)

    def _send_events(self):
        """Send any pending events."""
        if self._event_timer:
            self._event_timer.cancel()
            self._event_timer = None
        if not self._event_queue:
            return
        subscribed_events = self._event_queue_with_active_subscriptions()
        if subscribed_events:
            self.write(create_hap_event(subscribed_events))
        self._event_queue = []

    def _event_queue_with_active_subscriptions(self):
        """Remove any topics that have been unsubscribed after the event was generated."""
        topics = self.accessory_driver.topics
        return [
            event
            for event in self._event_queue
            if self.peername
            in topics.get(get_topic(event[HAP_REPR_AID], event[HAP_REPR_IID]), [])
        ]

    def _process_one_event(self) -> bool:
        """Process one http event."""
        event = self.conn.next_event()
        logger.debug(
            "%s (%s): h11 Event: %s", self.peername, self.handler.client_uuid, event
        )
        if event in (h11.NEED_DATA, h11.ConnectionClosed):
            return False

        if event is h11.PAUSED:
            self.conn.start_next_cycle()
            return True

        if isinstance(event, h11.Request):
            self.request = event
            self.request_body = b""
            return True

        if isinstance(event, h11.Data):
            self.request_body += event.data
            return True

        if isinstance(event, h11.EndOfMessage):
            response = self.handler.dispatch(self.request, bytes(self.request_body))
            self._process_response(response)
            self.request = None
            self.request_body = None
            return True

        return self._handle_invalid_conn_state("Unexpected event: {}".format(event))

    def _process_response(self, response) -> None:
        """Process a response from the handler."""
        if response.task:
            # If there is a task pending we will schedule
            # the response later
            self.response = response
            response.task.add_done_callback(self._handle_response_ready)
        else:
            self.send_response(response)

        # If we get a shared key, upgrade to encrypted
        if response.shared_key:
            self.hap_crypto = HAPCrypto(response.shared_key)
        # Only update mDNS after sending the response
        if response.pairing_changed:
            asyncio.ensure_future(
                self.loop.run_in_executor(None, self.accessory_driver.finish_pair)
            )

    def _handle_response_ready(self, task: asyncio.Task) -> None:
        """Handle delayed response."""
        response = self.response
        self.response = None
        try:
            response.body = task.result()
        except Exception as ex:  # pylint: disable=broad-except
            logger.debug(
                "%s (%s): exception during delayed response",
                self.peername,
                self.handler.client_uuid,
                exc_info=ex,
            )
            response = self.handler.generic_failure_response()
        if self.transport.is_closing():
            logger.debug(
                "%s (%s): delayed response not sent as the transport as closed.",
                self.peername,
                self.handler.client_uuid,
            )
            return
        self.send_response(response)

    def _handle_invalid_conn_state(self, message):
        """Log invalid state and close."""
        logger.debug(
            "%s (%s): Invalid state: %s: close the client socket",
            self.peername,
            self.handler.client_uuid,
            message,
        )
        self.close()
        return False
