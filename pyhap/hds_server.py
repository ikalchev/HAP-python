"""HomeKit Data Stream TCP listener and connection dispatch.

Ties the HDS transport crypto (:mod:`pyhap.hds`) and message codec
(:mod:`pyhap.hds_protocol`) into an asyncio TCP server. A controller sets up a
transport over HAP (SetupDataStreamTransport), connects to the advertised port
and completes a ``control``/``hello`` handshake; from there both sides exchange
request/response/event messages on named protocols (e.g. ``dataSend`` for
recording fragment transfer).

The listener does not know the HAP session shared secret on its own; the camera
accessory registers a transport with the secret when it answers
SetupDataStreamTransport, and the first frame from the controller is matched
against the registered transports by trial decryption.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Callable, Dict, List, Optional, Tuple

from pyhap import hds
from pyhap.hds_protocol import EVENT, REQUEST, RESPONSE, HDSStatus, Message

logger = logging.getLogger(__name__)

RequestHandler = Callable[[Message], Tuple[int, dict]]  # returns (HDSStatus, body)
EventHandler = Callable[[Message], None]

# A connection that has not completed the control handshake buffers only the
# tiny hello frame; cap it hard and time it out so an unauthenticated peer
# cannot make us buffer indefinitely.
_MAX_UNBOUND_BYTES = 4096
_BIND_TIMEOUT = 10.0
# Controllers re-run SetupDataStreamTransport and may abandon transports; bound
# the registrations we keep (and trial-decrypt against) so they cannot pile up.
_MAX_PENDING_TRANSPORTS = 16


@dataclass
class _PendingTransport:
    encrypt_key: bytes
    decrypt_key: bytes


class HDSConnection(asyncio.Protocol):
    """One HDS TCP connection: key binding, framing and message dispatch."""

    def __init__(self, listener: "HDSListener") -> None:
        self._listener = listener
        self._buffer = bytearray()
        self._crypto: Optional[hds.HDSCrypto] = None
        self._transport: Optional[asyncio.Transport] = None
        self._request_handlers: Dict[Tuple[str, str], RequestHandler] = {}
        self._event_handlers: Dict[Tuple[str, str], EventHandler] = {}
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._next_request_id = 1
        self._bind_timeout_handle: Optional[asyncio.TimerHandle] = None
        self._close_callbacks: List[Callable[["HDSConnection"], None]] = []
        # Writable unless the transport has asked us to pause (back-pressure).
        self._can_write = asyncio.Event()
        self._can_write.set()
        # The mandatory control handshake is answered by default.
        self.add_request_handler(
            "control", "hello", lambda message: (HDSStatus.SUCCESS, {})
        )

    # --- registration API ---

    def add_request_handler(
        self, protocol: str, topic: str, handler: RequestHandler
    ) -> None:
        """Register a handler returning ``(status, response_message)``."""
        self._request_handlers[(protocol, topic)] = handler

    def add_event_handler(
        self, protocol: str, topic: str, handler: EventHandler
    ) -> None:
        """Register a handler for a fire-and-forget event."""
        self._event_handlers[(protocol, topic)] = handler

    def add_close_callback(self, callback: Callable[["HDSConnection"], None]) -> None:
        """Register a callback invoked once when the connection is lost.

        Several owners (the listener's connection set, any recording stream
        manager) need to react to connection loss, so this is a list rather than
        a single slot.
        """
        self._close_callbacks.append(callback)

    # --- send API ---

    def send_event(self, protocol: str, topic: str, message: dict) -> None:
        """Send an event message (no response expected)."""
        self._send(Message(protocol, EVENT, topic, message))

    def send_request(
        self, protocol: str, topic: str, message: dict
    ) -> "asyncio.Future[Message]":
        """Send a request; the future resolves with the controller's response."""
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        self._send(Message(protocol, REQUEST, topic, message, id=request_id))
        return future

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    # --- asyncio.Protocol ---

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        self._bind_timeout_handle = asyncio.get_running_loop().call_later(
            _BIND_TIMEOUT, self._on_bind_timeout
        )

    def _on_bind_timeout(self) -> None:
        if self._crypto is None:
            logger.warning("Closing HDS connection: control handshake timed out")
            self.close()

    def data_received(self, data: bytes) -> None:
        self._buffer += data
        if self._crypto is None:
            if len(self._buffer) > _MAX_UNBOUND_BYTES:
                logger.warning(
                    "Closing HDS connection: no valid handshake within %d bytes",
                    _MAX_UNBOUND_BYTES,
                )
                self.close()
                return
            if not self._bind():
                return
        self._drain_frames()

    def pause_writing(self) -> None:
        # Transport buffer above the high-water mark: stop producing.
        self._can_write.clear()

    def resume_writing(self) -> None:
        self._can_write.set()

    async def drain(self) -> None:
        """Wait until the transport can accept more data (write back-pressure)."""
        await self._can_write.wait()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if self._bind_timeout_handle is not None:
            self._bind_timeout_handle.cancel()
            self._bind_timeout_handle = None
        # Unblock anyone waiting on write back-pressure so their task can unwind.
        self._can_write.set()
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        self._listener._connections.discard(self)  # pylint: disable=protected-access
        for callback in self._close_callbacks:
            callback(self)
        self._close_callbacks.clear()

    # --- internals ---

    def _bind(self) -> bool:
        """Identify the transport by trial-decrypting the first frame."""
        if len(self._buffer) < 4:
            return False
        # The listener and its connections cooperate within this module.
        # pylint: disable=protected-access
        for pending in list(self._listener._pending):
            trial = hds.HDSCrypto(pending.encrypt_key, pending.decrypt_key)
            probe = bytearray(self._buffer)
            try:
                payload = trial.decrypt_frame(probe)
            # Wrong keys fail the auth tag; try the next pending transport.
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            if payload is None:
                # Right keys but the frame is not complete yet; wait for more.
                return False
            self._crypto = trial
            self._buffer = probe
            if self._bind_timeout_handle is not None:
                self._bind_timeout_handle.cancel()
                self._bind_timeout_handle = None
            self._listener._pending.remove(pending)
            self._listener._connections.add(self)
            self._dispatch(payload)
            return True
        return False

    def _drain_frames(self) -> None:
        while True:
            try:
                payload = self._crypto.decrypt_frame(self._buffer)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("Dropping HDS connection on frame decrypt failure")
                self.close()
                return
            if payload is None:
                return
            self._dispatch(payload)

    def _dispatch(self, payload: bytes) -> None:
        try:
            message = Message.decode(payload)
        except (ValueError, IndexError, KeyError, TypeError):
            logger.warning("Ignoring malformed HDS message")
            return
        if message.kind == REQUEST:
            self._handle_request(message)
        elif message.kind == RESPONSE:
            future = self._pending_requests.pop(message.id, None)
            if future is not None and not future.done():
                future.set_result(message)
        elif message.kind == EVENT:
            handler = self._event_handlers.get((message.protocol, message.topic))
            if handler is not None:
                handler(message)

    def _handle_request(self, message: Message) -> None:
        handler = self._request_handlers.get((message.protocol, message.topic))
        if handler is None:
            status, response_message = HDSStatus.PROTOCOL_SPECIFIC_ERROR, {}
        else:
            try:
                status, response_message = handler(message)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "HDS request handler for %s/%s failed",
                    message.protocol,
                    message.topic,
                    exc_info=True,
                )
                status, response_message = HDSStatus.PROTOCOL_SPECIFIC_ERROR, {}
        self._send(
            Message(
                message.protocol,
                RESPONSE,
                message.topic,
                response_message,
                id=message.id,
                status=status,
            )
        )

    def _send(self, message: Message) -> None:
        if self._transport is None or self._crypto is None:
            raise RuntimeError("HDS connection is not ready to send")
        self._transport.write(self._crypto.encrypt_frame(message.encode()))


class HDSListener:
    """An asyncio TCP server accepting HDS connections."""

    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self._pending = []
        self._connections = set()
        self.on_connection: Optional[Callable[[HDSConnection], None]] = None

    async def start(self, host: str = "0.0.0.0") -> int:
        """Bind the server to an ephemeral port and return it."""
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(self._make_connection, host, 0)
        return self.port

    def _make_connection(self) -> HDSConnection:
        connection = HDSConnection(self)
        if self.on_connection is not None:
            self.on_connection(connection)
        return connection

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    def register_transport(
        self, shared_secret: bytes, controller_key_salt: bytes
    ) -> bytes:
        """Register an expected transport, returning the accessory key salt.

        The camera calls this when it answers SetupDataStreamTransport, passing
        the HAP session shared secret and the controller's key salt. The
        returned accessory key salt goes into the setup response.
        """
        accessory_key_salt = hds.new_key_salt()
        encrypt_key, decrypt_key = hds.derive_keys(
            shared_secret, controller_key_salt, accessory_key_salt
        )
        self._pending.append(_PendingTransport(encrypt_key, decrypt_key))
        # Evict the oldest stale registration if the controller keeps setting up
        # transports it never connects to.
        while len(self._pending) > _MAX_PENDING_TRANSPORTS:
            self._pending.pop(0)
        return accessory_key_salt

    async def stop(self) -> None:
        for connection in list(self._connections):
            connection.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
