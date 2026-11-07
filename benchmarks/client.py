"""A mock HAP controller (client) used to drive the benchmark server.

This module speaks the real HAP protocol against the server:

* ``POST /pair-verify`` with X25519/Ed25519/HKDF and TLV payloads, exactly
  as a real iOS controller would, based on a pre-paired long-term identity.
* The encrypted transport (ChaCha20-Poly1305 in 1024 byte blocks) via
  :class:`pyhap.hap_crypto.HAPCrypto` in ``client_side`` mode.
* Plain HTTP/1.1 requests for characteristic reads/writes, subscriptions
  and ``GET /accessories``, plus parsing of interleaved ``EVENT/1.0`` push
  frames on the same connection.

The client deliberately avoids h11 so that interleaved ``EVENT`` frames can
be parsed without fighting a strict HTTP state machine, and to keep the
flamegraph of the *server* process clean of client machinery.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any
import uuid as uuid_module

from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable as ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from pyhap import tlv
from pyhap.hap_crypto import HAPCrypto, hap_hkdf, pad_tls_nonce
from pyhap.hap_handler import HAP_TLV_STATES, HAP_TLV_TAGS

logger = logging.getLogger(__name__)

# The server closes connections that take longer than 10 seconds to
# respond, so never wait longer than that on the client side either.
RESPONSE_TIMEOUT = 9.0

_PAIRING_CONTENT_TYPE = b"application/pairing+tlv8"
_HAP_CONTENT_TYPE = b"application/hap+json"

_HEADER_SEPARATOR = b"\r\n\r\n"


@dataclass(frozen=True)
class ClientIdentity:
    """The long-term pairing identity of a mock controller.

    The server pre-pairs these identities at startup (bypassing the slow
    SRP pair-setup), so each controller only needs a pair-verify to open
    an encrypted session.
    """

    uuid: str
    private_key: ed25519.Ed25519PrivateKey

    @classmethod
    def generate(cls, index: int) -> ClientIdentity:
        """Generate a deterministic identity for the given index."""
        pair_uuid = str(
            uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"hap-bench-client-{index}")
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


@dataclass(frozen=True)
class Frame:
    """A parsed HTTP response or EVENT frame received from the server."""

    kind: str  # "response" or "event"
    status: int
    body: bytes
    received_ns: int


def _parse_frame(buffer: bytearray) -> Frame | None:
    """Parse one frame from ``buffer``.

    Returns ``None`` if the buffer does not hold a complete frame yet, in
    which case the buffer is left untouched. Otherwise the frame is
    returned and its bytes are consumed from the buffer.
    """
    header_end = buffer.find(_HEADER_SEPARATOR)
    if header_end < 0:
        return None
    lines = bytes(buffer[:header_end]).split(b"\r\n")
    start = lines[0]
    if start.startswith(b"EVENT/"):
        kind = "event"
    elif start.startswith(b"HTTP/"):
        kind = "response"
    else:
        raise RuntimeError(f"Client received unexpected data: {start!r}")
    status = int(start.split(b" ", 2)[1])
    content_length = 0
    for header in lines[1:]:
        name, _, value = header.partition(b":")
        if name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    body_start = header_end + len(_HEADER_SEPARATOR)
    total = body_start + content_length
    if len(buffer) < total:
        return None
    body = bytes(buffer[body_start:total])
    del buffer[:total]
    return Frame(kind, status, body, time.perf_counter_ns())


class BenchController:
    """A mock HAP controller connected to the benchmark server.

    Requests on a connection are strictly sequential (like the real
    protocol on a single socket) while ``EVENT`` frames may arrive at any
    time between responses.
    """

    def __init__(self, identity: ClientIdentity, accessory_ltpk: bytes) -> None:
        """Initialize the controller for a given pairing identity.

        :param identity: The long-term identity the server pre-paired.
        :param accessory_ltpk: Raw long-term public key of the accessory,
            used to verify the server during pair-verify.
        """
        self._identity = identity
        self._accessory_ltpk = ed25519.Ed25519PublicKey.from_public_bytes(
            accessory_ltpk
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._crypto: HAPCrypto | None = None
        self._buffer = bytearray()
        self._pending: asyncio.Future[Frame] | None = None
        self._request_lock = asyncio.Lock()
        self._events: asyncio.Queue[tuple[int, int, Any, int]] = asyncio.Queue()
        self._closed = False

    @property
    def name(self) -> str:
        """A human readable name for logging."""
        return f"client-{self._identity.uuid[:8]}"

    async def connect(self, host: str, port: int) -> None:
        """Connect to the server and negotiate an encrypted session.

        Performs the full two-step pair-verify: X25519 key exchange,
        Ed25519 signature verification in both directions, HKDF key
        derivation and the switch to the encrypted transport.
        """
        self._reader, self._writer = await asyncio.open_connection(host, port)
        self._read_task = asyncio.create_task(
            self._read_loop(), name=f"{self.name}-reader"
        )
        await self._pair_verify()

    async def close(self) -> None:
        """Close the connection."""
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    # ### Protocol: pair verify ###

    async def _pair_verify(self) -> None:
        """Run the pair-verify handshake and upgrade to encryption."""
        assert self._reader is not None  # noqa: S101
        ephemeral_private = x25519.X25519PrivateKey.generate()
        ephemeral_public = _raw_public(ephemeral_private)

        # Step 1: send our ephemeral public key.
        body = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            HAP_TLV_STATES.M1,
            HAP_TLV_TAGS.PUBLIC_KEY,
            ephemeral_public,
        )
        response = await self._request(b"POST", b"/pair-verify", body)
        answer = tlv.decode(response.body)
        if answer.get(HAP_TLV_TAGS.SEQUENCE_NUM) != HAP_TLV_STATES.M2:
            raise RuntimeError(f"Unexpected pair-verify response: {answer!r}")

        server_ephemeral_public = answer[HAP_TLV_TAGS.PUBLIC_KEY]
        shared_key = ephemeral_private.exchange(
            x25519.X25519PublicKey.from_public_bytes(server_ephemeral_public)
        )
        output_key = hap_hkdf(shared_key, _PVERIFY_1_SALT, _PVERIFY_1_INFO)

        # Step 2: verify the accessory's signature over the exchange.
        cipher = ChaCha20Poly1305(output_key)
        decrypted = cipher.decrypt(
            _PVERIFY_1_NONCE, answer[HAP_TLV_TAGS.ENCRYPTED_DATA], b""
        )
        proof = tlv.decode(decrypted)
        accessory_mac = proof[HAP_TLV_TAGS.USERNAME]
        accessory_proof = proof[HAP_TLV_TAGS.PROOF]
        self._accessory_ltpk.verify(
            accessory_proof,
            server_ephemeral_public + accessory_mac + ephemeral_public,
        )

        # Step 3: prove our own identity with our long-term key.
        material = (
            ephemeral_public + self._identity.uuid_bytes + server_ephemeral_public
        )
        message = tlv.encode(
            HAP_TLV_TAGS.USERNAME,
            self._identity.uuid_bytes,
            HAP_TLV_TAGS.PROOF,
            self._identity.private_key.sign(material),
        )
        aead_message = bytes(cipher.encrypt(_PVERIFY_2_NONCE, message, b""))
        body = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            HAP_TLV_STATES.M3,
            HAP_TLV_TAGS.ENCRYPTED_DATA,
            aead_message,
        )
        response = await self._request(b"POST", b"/pair-verify", body)
        answer = tlv.decode(response.body)
        if answer.get(HAP_TLV_TAGS.SEQUENCE_NUM) != HAP_TLV_STATES.M4:
            raise RuntimeError(f"Unexpected pair-verify response: {answer!r}")

        # The M4 response arrives in plaintext; everything after it is
        # encrypted with the session key.
        self._crypto = HAPCrypto(shared_key, client_side=True)

    # ### HTTP plumbing ###

    async def _read_loop(self) -> None:
        """Read from the socket, decrypt and dispatch complete frames."""
        assert self._reader is not None  # noqa: S101
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    break
                if self._crypto is not None:
                    self._crypto.receive_data(data)
                    plaintext = self._crypto.decrypt()
                    if not plaintext:
                        continue
                else:
                    plaintext = data
                self._buffer += plaintext
                while (frame := _parse_frame(self._buffer)) is not None:
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("%s: reader loop terminated", self.name)
        finally:
            self._closed = True
            pending = self._pending
            if pending is not None and not pending.done():
                pending.set_exception(ConnectionError("Connection closed by server"))

    def _dispatch(self, frame: Frame) -> None:
        """Deliver a parsed frame to the waiter or the event queue."""
        if frame.kind == "event":
            chars = json.loads(frame.body)["characteristics"]
            for char in chars:
                self._events.put_nowait(
                    (char["aid"], char["iid"], char.get("value"), frame.received_ns)
                )
            return
        pending = self._pending
        if pending is None or pending.done():
            raise RuntimeError(f"Client got response with no pending request: {frame}")
        pending.set_result(frame)

    async def _request(
        self,
        method: bytes,
        target: bytes,
        body: bytes,
        content_type: bytes = _HAP_CONTENT_TYPE,
    ) -> Frame:
        """Send one HTTP request and await its response frame."""
        if self._closed:
            raise ConnectionError(f"{self.name}: connection is closed")
        async with self._request_lock:
            assert self._writer is not None  # noqa: S101
            payload = b"".join(
                (
                    method,
                    b" ",
                    target,
                    b" HTTP/1.1\r\nHost: hap\r\nContent-Type: ",
                    content_type,
                    b"\r\nContent-Length: ",
                    str(len(body)).encode("ascii"),
                    b"\r\n\r\n",
                    body,
                )
            )
            if self._crypto is not None:
                self._writer.write(b"".join(self._crypto.encrypt(payload)))
            else:
                self._writer.write(payload)
            pending: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
            self._pending = pending
            try:
                return await asyncio.wait_for(pending, timeout=RESPONSE_TIMEOUT)
            finally:
                self._pending = None

    # ### HAP operations ###

    async def get_accessories(self) -> Frame:
        """``GET /accessories`` - the full accessory list."""
        return await self._request(b"GET", b"/accessories", b"")

    async def read_characteristic(self, aid: int, iid: int) -> Frame:
        """``GET /characteristics?id=aid.iid``."""
        target = b"/characteristics?id=" + f"{aid}.{iid}".encode("ascii")
        return await self._request(b"GET", target, b"")

    async def write_characteristic(
        self, aid: int, iid: int, value: Any, write_response: bool = False
    ) -> Frame:
        """``PUT /characteristics`` with a single characteristic value."""
        query: dict[str, Any] = {
            "characteristics": [
                {"aid": aid, "iid": iid, "value": value, "r": write_response}
            ]
        }
        response = await self._request(
            b"PUT", b"/characteristics", json.dumps(query).encode("utf-8")
        )
        if response.status not in (200, 204, 207):
            raise RuntimeError(
                f"{self.name}: write of {aid}.{iid} failed: "
                f"{response.status} {response.body!r}"
            )
        return response

    async def subscribe(self, aid: int, iid: int) -> Frame:
        """Subscribe to events for a characteristic (``ev: true``)."""
        query: dict[str, Any] = {
            "characteristics": [{"aid": aid, "iid": iid, "ev": True}]
        }
        response = await self._request(
            b"PUT", b"/characteristics", json.dumps(query).encode("utf-8")
        )
        if response.status not in (200, 204, 207):
            raise RuntimeError(
                f"{self.name}: subscribe to {aid}.{iid} failed: "
                f"{response.status} {response.body!r}"
            )
        return response

    async def wait_event(
        self,
        aid: int,
        iid: int,
        value: Any,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> int:
        """Wait for the next event of the given characteristic.

        The expected ``value`` is matched as well, so stale events from
        earlier writes of other characteristics (or of the same one with
        a different value) are discarded. Returns ``received_ns``, a
        ``time.perf_counter_ns()`` timestamp taken when the frame
        finished arriving.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"{self.name}: no event for {aid}.{iid}")
            event_aid, event_iid, event_value, received_ns = await asyncio.wait_for(
                self._events.get(), remaining
            )
            if (event_aid, event_iid) == (aid, iid) and event_value == value:
                return received_ns

    def clear_events(self) -> None:
        """Drop all queued events."""
        while not self._events.empty():
            self._events.get_nowait()


def _raw_public(key: x25519.X25519PrivateKey | x25519.X25519PublicKey) -> bytes:
    """Return the raw 32 byte public key of an X25519 key."""
    if isinstance(key, x25519.X25519PrivateKey):
        key = key.public_key()
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


# The salt/info pairs of the pair-verify handshake. These mirror
# HAPServerHandler so the client does not depend on the server class.
_PVERIFY_1_SALT = b"Pair-Verify-Encrypt-Salt"
_PVERIFY_1_INFO = b"Pair-Verify-Encrypt-Info"
_PVERIFY_1_NONCE = pad_tls_nonce(b"PV-Msg02")
_PVERIFY_2_NONCE = pad_tls_nonce(b"PV-Msg03")
