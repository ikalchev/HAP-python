"""Low-level HomeKit Data Stream framing and OPACK primitives.

This module does not advertise recording or implement HDS session lifecycle.
"""

from __future__ import annotations

import asyncio
import secrets
import struct
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_FRAME = 0xFFFFF
MAX_SESSIONS = 8
MAX_CONNECTIONS = 8


def derive_keys(
    secret: bytes, controller_salt: bytes, accessory_salt: bytes
) -> tuple[bytes, bytes]:
    """Return accessory-to-controller and controller-to-accessory keys."""
    if len(controller_salt) != 32 or len(accessory_salt) != 32:
        raise ValueError("HDS salts must be 32 bytes")
    salt = controller_salt + accessory_salt

    def key(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt, info=info).derive(
            secret
        )

    return key(b"HDS-Read-Encryption-Key"), key(b"HDS-Write-Encryption-Key")


def opack_encode(value: Any) -> bytes:
    """Encode the OPACK types used by HDS without optional compression."""
    if value is None:
        return b"\x04"
    if isinstance(value, bool):
        return b"\x01" if value else b"\x02"
    if isinstance(value, int):
        if value == -1:
            return b"\x07"
        if 0 <= value <= 39:
            return bytes([8 + value])
        for tag, fmt, lo, hi in (
            (0x30, "b", -128, 127),
            (0x31, "h", -32768, 32767),
            (0x32, "i", -2147483648, 2147483647),
            (0x33, "q", -(2**63), 2**63 - 1),
        ):
            if lo <= value <= hi:
                return bytes([tag]) + struct.pack("<" + fmt, value)
        raise ValueError("integer out of range")
    if isinstance(value, float):
        return b"\x36" + struct.pack("<d", value)
    if isinstance(value, UUID):
        return b"\x05" + value.bytes
    if isinstance(value, (str, bytes)):
        raw = value.encode() if isinstance(value, str) else value
        base = 0x40 if isinstance(value, str) else 0x70
        n = len(raw)
        if n <= 32:
            return bytes([base + n]) + raw
        for offset, size in ((0x21, 1), (0x22, 2), (0x23, 4), (0x24, 8)):
            if n < 1 << (size * 8):
                return bytes([base + offset]) + n.to_bytes(size, "little") + raw
    if isinstance(value, (list, tuple)):
        return (
            (bytes([0xD0 + len(value)]) if len(value) <= 14 else b"\xdf")
            + b"".join(map(opack_encode, value))
            + (b"\x03" if len(value) > 14 else b"")
        )
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("dictionary keys must be strings")
        return (
            (bytes([0xE0 + len(value)]) if len(value) <= 14 else b"\xef")
            + b"".join(opack_encode(k) + opack_encode(v) for k, v in value.items())
            + (b"\x03" if len(value) > 14 else b"")
        )
    raise TypeError(f"unsupported OPACK type: {type(value).__name__}")


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos, self.cache = data, 0, []

    def take(self, n: int) -> bytes:
        if n < 0 or n > len(self.data) - self.pos:
            raise ValueError("truncated OPACK")
        data = self.data[self.pos : self.pos + n]
        self.pos += n
        return data

    def read(self, depth: int = 0) -> Any:
        if depth > 64:
            raise ValueError("OPACK nesting too deep")
        tag = self.take(1)[0]
        if tag == 4:
            return None
        if tag == 3:
            raise ValueError("unexpected OPACK terminator")
        if 0xA0 <= tag <= 0xCF:
            index = tag - 0xA0
            if index >= len(self.cache):
                raise ValueError("invalid OPACK reference")
            return self.cache[index]
        if 0xD0 <= tag <= 0xEF:
            is_dict = tag >= 0xE0
            base = 0xE0 if is_dict else 0xD0
            count = tag - base
            values = []
            if count == 15:
                while True:
                    if self.pos >= len(self.data):
                        raise ValueError("unterminated OPACK container")
                    if self.data[self.pos] == 3:
                        self.pos += 1
                        break
                    values.append(self.read(depth + 1))
            else:
                values = [
                    self.read(depth + 1) for _ in range(count * (2 if is_dict else 1))
                ]
            if is_dict:
                if len(values) % 2 or not all(isinstance(k, str) for k in values[::2]):
                    raise ValueError("invalid OPACK dictionary")
                return dict(zip(values[::2], values[1::2]))
            return values
        if tag in (1, 2):
            value = tag == 1
        elif tag == 5:
            value = UUID(bytes=self.take(16))
        elif tag in (6, 0x36):
            value = struct.unpack("<d", self.take(8))[0]
        elif tag == 7:
            value = -1
        elif 8 <= tag <= 0x2F:
            value = tag - 8
        elif tag in (0x30, 0x31, 0x32, 0x33, 0x35):
            fmt = {0x30: "b", 0x31: "h", 0x32: "i", 0x33: "q", 0x35: "f"}[tag]
            value = struct.unpack("<" + fmt, self.take(struct.calcsize(fmt)))[0]
        elif 0x40 <= tag <= 0x64 or 0x70 <= tag <= 0x94:
            is_string = tag < 0x70
            base = 0x40 if is_string else 0x70
            offset = tag - base
            length = (
                offset
                if offset <= 32
                else int.from_bytes(self.take(1 << (offset - 33)), "little")
            )
            raw = self.take(length)
            value = raw.decode("utf-8") if is_string else raw
        elif tag in (0x6F, 0x9F):
            terminator = 0 if tag == 0x6F else 3
            try:
                end = self.data.index(terminator, self.pos)
            except ValueError as err:
                raise ValueError("unterminated OPACK value") from err
            raw = self.take(end - self.pos)
            self.take(1)
            value = raw.decode("utf-8") if tag == 0x6F else raw
        else:
            raise ValueError(f"unknown OPACK tag {tag:#x}")
        if tag not in (1, 2, 7) and not 8 <= tag <= 0x2F:
            self.cache.append(value)
        return value


def opack_decode(data: bytes) -> Any:
    """Decode one OPACK value and reject trailing bytes."""
    reader = _Reader(data)
    value = reader.read()
    if reader.pos != len(data):
        raise ValueError("trailing OPACK data")
    return value


class HDSCipher:
    """Stateful one-direction frame cipher. Close connection on authentication failure."""

    def __init__(self, key: bytes):
        """Initialize one direction with its own nonce counter."""
        self.cipher = ChaCha20Poly1305(key)
        self.counter = 0

    def _nonce(self) -> bytes:
        return bytes(4) + self.counter.to_bytes(8, "little")

    def encrypt(self, payload: bytes) -> bytes:
        """Encrypt and authenticate one complete HDS frame."""
        if len(payload) > MAX_FRAME:
            raise ValueError("HDS frame too large")
        header = b"\x01" + len(payload).to_bytes(3, "big")
        result = header + self.cipher.encrypt(self._nonce(), payload, header)
        self.counter += 1
        return result

    def decrypt(self, frame: bytes) -> bytes:
        """Authenticate and decrypt one complete HDS frame."""
        if len(frame) < 20 or frame[0] != 1:
            raise ValueError("invalid HDS frame")
        length = int.from_bytes(frame[1:4], "big")
        if length > MAX_FRAME or len(frame) != length + 20:
            raise ValueError("invalid HDS frame length")
        result = self.cipher.decrypt(self._nonce(), frame[4:], frame[:4])
        self.counter += 1
        return result


def encode_message(header: dict, body: dict) -> bytes:
    """Encode the two OPACK dictionaries in an HDS payload."""
    encoded_header = opack_encode(header)
    if len(encoded_header) > 255:
        raise ValueError("HDS message header too large")
    return bytes([len(encoded_header)]) + encoded_header + opack_encode(body)


def decode_message(payload: bytes) -> tuple[dict, dict]:
    """Decode the header and body of an HDS payload."""
    if not payload or payload[0] + 1 >= len(payload):
        raise ValueError("invalid HDS message")
    header = opack_decode(payload[1 : 1 + payload[0]])
    body = opack_decode(payload[1 + payload[0] :])
    if not isinstance(header, dict) or not isinstance(body, dict):
        raise TypeError("HDS header and body must be dictionaries")
    return header, body


# The integration must bind owner to the authenticated HAP connection and call
# close_owner when that connection closes. Never derive keys from pairing data.
class HDSReject(Exception):
    """Reject dataSend/open with a protocol-specific reason."""

    def __init__(self, reason: int):
        """Store the dataSend error reason."""
        self.reason = reason
        super().__init__(f"HDS recording rejected: {reason}")


@dataclass(eq=False)
class _Session:
    owner: object
    outgoing: bytes
    incoming: bytes
    expires: float


class HDSServer:
    """HDS TCP transport; the HAP layer controls authentication and lifecycle.

    recording_factory(owner, stream_id) returns an async iterator of
    (fragment, is_last). It may raise HDSReject before returning the iterator.
    """

    def __init__(
        self,
        recording_factory: Callable[[object, int], AsyncIterator[tuple[bytes, bool]]],
    ):
        """Initialize the server with a recording fragment provider."""
        self.recording_factory = recording_factory
        self._server: asyncio.Server | None = None
        self._sessions: list[_Session] = []
        self._connections: dict[
            asyncio.Task, tuple[object | None, asyncio.StreamWriter]
        ] = {}

    async def start(self, host: str = "0.0.0.0") -> int:
        """Bind a TCP listener and return its port."""
        if self._server is not None:
            raise RuntimeError("HDS server already started")
        self._server = await asyncio.start_server(self._accept, host, 0)
        return self.port

    @property
    def port(self) -> int:
        """Return the bound TCP port."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("HDS server is not started")
        return self._server.sockets[0].getsockname()[1]

    def prepare_session(
        self, secret: bytes, controller_salt: bytes, owner: object
    ) -> tuple[int, bytes]:
        """Reserve one session for 10 seconds; return port and accessory salt."""
        port = self.port
        salt = secrets.token_bytes(32)
        outgoing, incoming = derive_keys(secret, controller_salt, salt)
        now = time.monotonic()
        self._sessions = [
            session for session in self._sessions if session.expires > now
        ]
        if len(self._sessions) >= MAX_SESSIONS:
            raise RuntimeError("too many pending HDS sessions")
        self._sessions.append(_Session(owner, outgoing, incoming, now + 10))
        return port, salt

    def close_owner(self, owner: object) -> None:
        """Invalidate sessions when the authenticated HAP connection closes."""
        self._sessions = [
            session for session in self._sessions if session.owner is not owner
        ]
        for task, (connection_owner, writer) in list(self._connections.items()):
            if connection_owner is owner:
                writer.close()
                task.cancel()

    async def close(self) -> None:
        """Close the listener, sessions, and open connections."""
        self._sessions.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._connections) >= MAX_CONNECTIONS:
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        assert task is not None
        self._connections[task] = (None, writer)
        stream_task: asyncio.Task | None = None
        stream_id: int | None = None
        ack_timer: asyncio.TimerHandle | None = None
        send_lock = asyncio.Lock()
        try:
            frame = await asyncio.wait_for(self._read_frame(reader), 10)
            session = None
            incoming = None
            plaintext = None
            now = time.monotonic()
            self._sessions = [
                candidate for candidate in self._sessions if candidate.expires > now
            ]
            for candidate in self._sessions:
                cipher = HDSCipher(candidate.incoming)
                try:
                    plaintext = cipher.decrypt(frame)
                except InvalidTag:
                    continue
                session, incoming = candidate, cipher
                break
            if session is None or incoming is None or plaintext is None:
                return
            self._sessions.remove(session)
            self._connections[task] = (session.owner, writer)
            outgoing = HDSCipher(session.outgoing)

            async def send(header: dict, body: dict) -> None:
                async with send_lock:
                    writer.write(outgoing.encrypt(encode_message(header, body)))
                    await asyncio.wait_for(writer.drain(), 10)

            async def response(
                protocol: str, topic: str, request_id: int, status: int, body: dict
            ) -> None:
                await send(
                    {
                        "protocol": protocol,
                        "response": topic,
                        "id": request_id,
                        "status": status,
                    },
                    body,
                )

            header, body = decode_message(plaintext)
            if (
                header.get("protocol") != "control"
                or header.get("request") != "hello"
                or not isinstance(header.get("id"), int)
            ):
                return
            await response("control", "hello", header["id"], 0, {})
            while True:
                header, body = decode_message(
                    incoming.decrypt(
                        await asyncio.wait_for(self._read_frame(reader), 90)
                    )
                )
                protocol = header.get("protocol")
                if protocol == "dataSend" and header.get("request") == "open":
                    request_id = header.get("id")
                    requested_id = body.get("streamId")
                    if not isinstance(request_id, int) or not isinstance(
                        requested_id, int
                    ):
                        return
                    if (
                        body.get("target") != "controller"
                        or body.get("type") != "ipcamera.recording"
                    ):
                        await response("dataSend", "open", request_id, 6, {"status": 5})
                        continue
                    if stream_task is not None:
                        await response("dataSend", "open", request_id, 6, {"status": 2})
                        continue
                    try:
                        fragments = self.recording_factory(session.owner, requested_id)
                    except HDSReject as err:
                        await response(
                            "dataSend", "open", request_id, 6, {"status": err.reason}
                        )
                        continue
                    stream_id = requested_id
                    try:
                        await response("dataSend", "open", request_id, 0, {"status": 0})
                    except BaseException:
                        await fragments.aclose()
                        raise
                    stream_task = asyncio.create_task(
                        self._send_fragments(send, stream_id, fragments)
                    )

                    def finished(completed: asyncio.Task) -> None:
                        nonlocal ack_timer
                        if (
                            not completed.cancelled()
                            and completed.exception() is None
                            and completed.result()
                        ):
                            ack_timer = asyncio.get_running_loop().call_later(
                                12, writer.close
                            )
                        elif not completed.cancelled():
                            writer.close()

                    stream_task.add_done_callback(finished)
                elif (
                    protocol == "dataSend"
                    and header.get("event") in ("ack", "close")
                    and body.get("streamId") == stream_id
                ):
                    if stream_task is None:
                        continue
                    if header.get("event") == "ack":
                        if (
                            body.get("endOfStream") is not True
                            or not stream_task.done()
                            or stream_task.cancelled()
                            or stream_task.exception() is not None
                            or not stream_task.result()
                        ):
                            continue
                        await send(
                            {"protocol": "dataSend", "event": "close"},
                            {"streamId": stream_id, "reason": 0},
                        )
                    if ack_timer:
                        ack_timer.cancel()
                        ack_timer = None
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                    stream_task = None
                    stream_id = None
                elif "request" in header and isinstance(header.get("id"), int):
                    await response(
                        str(protocol), str(header["request"]), header["id"], 5, {}
                    )
        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            InvalidTag,
            TypeError,
            ValueError,
            ConnectionError,
        ):
            pass
        finally:
            if ack_timer:
                ack_timer.cancel()
            if stream_task:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            self._connections.pop(task, None)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(4)
        length = int.from_bytes(header[1:], "big")
        if header[0] != 1 or length > MAX_FRAME:
            raise ValueError("invalid HDS frame header")
        return header + await reader.readexactly(length + 16)

    @staticmethod
    async def _send_fragments(
        send: Callable, stream_id: int, fragments: AsyncIterator[tuple[bytes, bool]]
    ) -> bool:
        try:
            sequence = 1
            async for fragment, is_last in fragments:
                if not fragment:
                    raise ValueError("empty recording fragment")
                for offset in range(0, len(fragment), 0x40000):
                    chunk = fragment[offset : offset + 0x40000]
                    last_chunk = offset + len(chunk) == len(fragment)
                    metadata = {
                        "dataType": "mediaInitialization"
                        if sequence == 1
                        else "mediaFragment",
                        "dataSequenceNumber": sequence,
                        "dataChunkSequenceNumber": offset // 0x40000 + 1,
                        "isLastDataChunk": last_chunk,
                    }
                    if offset == 0:
                        metadata["dataTotalSize"] = len(fragment)
                    body = {
                        "streamId": stream_id,
                        "packets": [{"data": chunk, "metadata": metadata}],
                    }
                    if last_chunk:
                        body["endOfStream"] = is_last
                    await send({"protocol": "dataSend", "event": "data"}, body)
                sequence += 1
                if is_last:
                    return True
            raise ValueError("recording ended without final fragment")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - isolate a failed recording producer
            await send(
                {"protocol": "dataSend", "event": "close"},
                {"streamId": stream_id, "reason": 5},
            )
            return False
        finally:
            await fragments.aclose()
