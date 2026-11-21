"""A HAP controller (client) connected to a HAP accessory server.

The client deliberately avoids h11 so that interleaved ``EVENT`` frames can
be parsed without fighting a strict HTTP state machine. Requests on a
connection are strictly sequential (like the real protocol on a single
socket) while ``EVENT`` frames may arrive at any time between responses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace
import json
import logging
import os
from typing import Any

from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable as ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .. import tlv
from ..characteristic import CharacteristicWrite
from ..const import RESPONSE_TIMEOUT
from ..hap_crypto import HAPCrypto, hap_hkdf, pad_tls_nonce
from ..hap_handler import HAP_TLV_STATES, HAP_TLV_TAGS
from .identity import ClientIdentity
from .protocol import HAP_CONTENT_TYPE, PAIRING_CONTENT_TYPE, Frame, parse_frame
from .state import load_accessory_ltpk

logger = logging.getLogger(__name__)

# RESPONSE_TIMEOUT is the server's own request timeout: it closes connections
# that take longer than 10 seconds to respond, so never wait longer than that
# on the client side either.


class HAPClient:
    """A HAP controller connected to an accessory server.

    Create one per :class:`ClientIdentity` that the accessory is paired
    with, then :meth:`connect` to run the pair-verify handshake and switch
    to the encrypted transport.
    """

    def __init__(
        self,
        identity: ClientIdentity,
        accessory_ltpk: bytes | None = None,
        *,
        accessory_state: str | os.PathLike[str] | None = None,
    ) -> None:
        """Initialize the controller for a given pairing identity.

        :param identity: The long-term identity the server is paired with.
        :param accessory_ltpk: Raw long-term public key of the accessory,
            used to verify the server during pair-verify.
        :param accessory_state: Path to the accessory's ``accessory.state``
            file (e.g. copied from the accessory machine); its LTPK is read
            from there. Used when ``accessory_ltpk`` is not given. If neither
            is given, the LTPK pinned in ``identity.accessory_ltpk`` is used;
            when none is available at all, the accessory's signature is not
            verified: the session is still encrypted, but the accessory is
            unauthenticated.
        """
        self._identity = identity
        if accessory_ltpk is None and accessory_state is not None:
            accessory_ltpk = load_accessory_ltpk(accessory_state)
        if accessory_ltpk is None:
            accessory_ltpk = identity.accessory_ltpk
        self._accessory_ltpk = accessory_ltpk
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._crypto: HAPCrypto | None = None
        self._buffer = bytearray()
        self._pending: asyncio.Future[Frame] | None = None
        self._request_lock = asyncio.Lock()
        self._events: asyncio.Queue[tuple[int, int, Any, int]] = asyncio.Queue()
        self._closed = False

    @classmethod
    def new_persistent(
        cls,
        *,
        filepath: str | os.PathLike[str],
        accessory_ltpk: bytes | None = None,
        accessory_state: str | os.PathLike[str] | None = None,
    ) -> HAPClient:
        """Create a client whose identity is persisted at ``filepath``.

        If ``filepath`` exists, the identity is loaded from it; otherwise a
        new one is generated and saved there. Pair the identity with the
        accessory once (e.g. :meth:`pyhap.accessory_driver.AccessoryDriver.pair`
        with ``client.identity`` - the pairing itself is persisted in the
        accessory's state file); afterwards the client reconnects as the same
        controller after restarts.

        The accessory LTPK is resolved from, in order: ``accessory_ltpk``,
        ``accessory_state`` (an ``accessory.state`` file, e.g. copied from
        the accessory machine), or the key pinned in ``filepath``. A resolved
        LTPK is pinned into ``filepath`` (trust on first use), so later calls
        verify the accessory against it. Without any LTPK the session is
        encrypted but the accessory is not authenticated.
        """
        path = os.path.expanduser(os.fspath(filepath))
        if os.path.exists(path):
            identity = ClientIdentity.load(path)
        else:
            identity = ClientIdentity.generate()
            identity.save(path)
        ltpk = accessory_ltpk
        if ltpk is None and accessory_state is not None:
            ltpk = load_accessory_ltpk(accessory_state)
        if ltpk is None:
            ltpk = identity.accessory_ltpk
        if ltpk is not None and identity.accessory_ltpk != ltpk:
            identity = replace(identity, accessory_ltpk=ltpk)
            identity.save(path)
        return cls(identity, ltpk)

    @property
    def identity(self) -> ClientIdentity:
        """The long-term identity this client pair-verifies with."""
        return self._identity

    @property
    def accessory_ltpk(self) -> bytes | None:
        """The raw accessory LTPK verified during pair-verify, if pinned."""
        return self._accessory_ltpk

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
        response = await self._request(
            b"POST", b"/pair-verify", body, content_type=PAIRING_CONTENT_TYPE
        )
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
        if self._accessory_ltpk is None:
            logger.warning(
                "%s: no accessory LTPK pinned; the session is encrypted but the "
                "accessory's identity is not verified",
                self.name,
            )
        else:
            ed25519.Ed25519PublicKey.from_public_bytes(self._accessory_ltpk).verify(
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
        response = await self._request(
            b"POST", b"/pair-verify", body, content_type=PAIRING_CONTENT_TYPE
        )
        answer = tlv.decode(response.body)
        if answer.get(HAP_TLV_TAGS.SEQUENCE_NUM) != HAP_TLV_STATES.M4:
            raise RuntimeError(f"Unexpected pair-verify response: {answer!r}")
        if HAP_TLV_TAGS.ERROR_CODE in answer:
            raise RuntimeError(
                f"{self.name}: pair-verify rejected by the accessory: "
                f"{answer[HAP_TLV_TAGS.ERROR_CODE]!r}"
            )

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
                while (frame := parse_frame(self._buffer)) is not None:
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
        content_type: bytes = HAP_CONTENT_TYPE,
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

    async def read_characteristics(self, char_ids: Iterable[tuple[int, int]]) -> Frame:
        """``GET /characteristics?id=aid.iid,...`` for one or more characteristics."""
        ids = ",".join(f"{aid}.{iid}" for aid, iid in char_ids)
        target = b"/characteristics?id=" + ids.encode("ascii")
        return await self._request(b"GET", target, b"")

    async def read_characteristic(self, aid: int, iid: int) -> Frame:
        """``GET /characteristics?id=aid.iid``."""
        return await self.read_characteristics([(aid, iid)])

    async def write_characteristics(
        self, characteristics: Iterable[CharacteristicWrite]
    ) -> Frame:
        """``PUT /characteristics`` with the given entries.

        Each entry is a :class:`pyhap.characteristic.CharacteristicWrite`:
        ``aid``/``iid`` plus any of ``value`` (write), ``ev`` (subscribe or
        unsubscribe) and ``r`` (write response).
        """
        query: dict[str, Any] = {
            "characteristics": [dict(entry) for entry in characteristics]
        }
        response = await self._request(
            b"PUT", b"/characteristics", json.dumps(query).encode("utf-8")
        )
        if response.status not in (200, 204, 207):
            raise RuntimeError(
                f"{self.name}: write failed: {response.status} {response.body!r}"
            )
        return response

    async def write_characteristic(
        self, aid: int, iid: int, value: Any, write_response: bool = False
    ) -> Frame:
        """``PUT /characteristics`` with a single characteristic value."""
        return await self.write_characteristics(
            [{"aid": aid, "iid": iid, "value": value, "r": write_response}]
        )

    async def set_event_subscription(
        self, aid: int, iid: int, subscribed: bool
    ) -> Frame:
        """Subscribe to (``subscribed=True``) or unsubscribe from events."""
        return await self.write_characteristics(
            [{"aid": aid, "iid": iid, "ev": subscribed}]
        )

    async def subscribe(self, aid: int, iid: int) -> Frame:
        """Subscribe to events for a characteristic (``ev: true``)."""
        return await self.set_event_subscription(aid, iid, True)

    async def unsubscribe(self, aid: int, iid: int) -> Frame:
        """Unsubscribe from events for a characteristic (``ev: false``)."""
        return await self.set_event_subscription(aid, iid, False)

    async def next_event(
        self, timeout: float = RESPONSE_TIMEOUT
    ) -> tuple[int, int, Any]:
        """Wait for the next EVENT frame and return ``(aid, iid, value)``."""
        event_aid, event_iid, event_value, _ = await asyncio.wait_for(
            self._events.get(), timeout
        )
        return event_aid, event_iid, event_value

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
