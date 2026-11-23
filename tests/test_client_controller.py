"""Unit tests for HAPClient internals the integration tests don't reach.

Covers connection teardown, the frame dispatch and read loop (with fake
streams), the pair-verify failure paths (with a stubbed HTTP layer) and the
write error path.
"""

import asyncio
from collections.abc import Callable
import json
import logging
import pathlib
import time
from typing import Any, cast

from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable as ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
import pytest

from pyhap import tlv
from pyhap.client import ClientIdentity, Frame, HAPClient
from pyhap.client.controller import (
    _PVERIFY_1_INFO,
    _PVERIFY_1_NONCE,
    _PVERIFY_1_SALT,
    _raw_public,
)
from pyhap.client.protocol import HAP_CONTENT_TYPE
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_crypto import HAPCrypto, hap_hkdf
from pyhap.hap_handler import HAP_TLV_STATES, HAP_TLV_TAGS
from pyhap.state import State


class _FakeReader:
    """A stand-in for ``asyncio.StreamReader`` feeding canned chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FailingReader:
    """A reader whose reads always fail, like a socket error mid-stream."""

    async def read(self, size: int) -> bytes:
        raise RuntimeError("socket exploded")


class _BrokenWriter:
    """A writer whose ``wait_closed`` fails with the given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        raise self._error


class _StubbedClient(HAPClient):
    """A HAPClient whose HTTP layer is answered by the given responder."""

    def __init__(
        self, responder: Callable[[bytes, bytes, bytes, bytes], Frame]
    ) -> None:
        super().__init__(ClientIdentity.generate(0))
        self._responder = responder

    async def _request(
        self,
        method: bytes,
        target: bytes,
        body: bytes,
        content_type: bytes = HAP_CONTENT_TYPE,
    ) -> Frame:
        return self._responder(method, target, body, content_type)


def _write_state_file(path: pathlib.Path) -> bytes:
    """Write a real accessory.state via the encoder; return its raw LTPK."""
    state = State(address="127.0.0.1")
    with open(path, "w", encoding="utf-8") as file_handle:
        AccessoryEncoder().persist(file_handle, state)
    return state.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _event_frame(*chars: dict[str, Any]) -> Frame:
    """An EVENT/1.0 frame carrying the given characteristic entries."""
    body = json.dumps({"characteristics": list(chars)}).encode("utf-8")
    return Frame(
        kind="event", status=200, body=body, received_ns=time.perf_counter_ns()
    )


def test_init_resolves_ltpk_from_accessory_state(tmp_path: pathlib.Path) -> None:
    """accessory_state= reads the accessory LTPK from the state file."""
    state_file = tmp_path / "accessory.state"
    expected = _write_state_file(state_file)
    client = HAPClient(ClientIdentity.generate(0), accessory_state=state_file)
    assert client.accessory_ltpk == expected


async def test_close_without_connect_is_a_noop() -> None:
    """close() before connect() has neither a reader task nor a writer."""
    client = HAPClient(ClientIdentity.generate(0))
    await client.close()
    assert client._closed


@pytest.mark.parametrize(
    "error", [ConnectionResetError("reset"), BrokenPipeError("pipe")]
)
async def test_close_ignores_writer_shutdown_errors(error: Exception) -> None:
    """A writer failing wait_closed() does not make close() raise."""
    client = HAPClient(ClientIdentity.generate(0))
    client._writer = cast("asyncio.StreamWriter", _BrokenWriter(error))
    await client.close()


async def test_request_on_closed_connection_raises() -> None:
    """Requests on a closed connection fail immediately."""
    client = HAPClient(ClientIdentity.generate(0))
    client._closed = True
    with pytest.raises(ConnectionError, match="connection is closed"):
        await client._request(b"GET", b"/accessories", b"")


def test_dispatch_response_without_pending_request_raises() -> None:
    """A response frame with no waiter is a protocol error."""
    client = HAPClient(ClientIdentity.generate(0))
    frame = Frame(kind="response", status=200, body=b"{}", received_ns=0)
    with pytest.raises(RuntimeError, match="no pending request"):
        client._dispatch(frame)


async def test_dispatch_event_queues_and_next_event_returns_it() -> None:
    """Event frames land in the queue; next_event() returns (aid, iid, value)."""
    client = HAPClient(ClientIdentity.generate(0))
    client._dispatch(_event_frame({"aid": 1, "iid": 2, "value": True}))
    client._dispatch(_event_frame({"aid": 3, "iid": 4}))  # no "value" key
    assert await client.next_event(timeout=1) == (1, 2, True)
    assert await client.next_event(timeout=1) == (3, 4, None)


async def test_clear_events_drains_the_queue() -> None:
    """clear_events() drops queued events so next_event() times out."""
    client = HAPClient(ClientIdentity.generate(0))
    client._dispatch(_event_frame({"aid": 1, "iid": 2, "value": 1}))
    client.clear_events()
    assert client._events.empty()
    with pytest.raises(TimeoutError):
        await client.next_event(timeout=0.05)


async def test_wait_event_times_out_when_deadline_already_passed() -> None:
    """A zero timeout raises before waiting on the queue."""
    client = HAPClient(ClientIdentity.generate(0))
    with pytest.raises(TimeoutError, match="no event for 1.2"):
        await client.wait_event(1, 2, True, timeout=0)


async def test_wait_event_skips_events_that_do_not_match() -> None:
    """Events for other characteristics or other values are discarded."""
    client = HAPClient(ClientIdentity.generate(0))
    client._dispatch(_event_frame({"aid": 1, "iid": 2, "value": False}))
    client._dispatch(_event_frame({"aid": 9, "iid": 9, "value": True}))
    client._dispatch(_event_frame({"aid": 1, "iid": 2, "value": True}))
    received_ns = await client.wait_event(1, 2, True, timeout=1)
    assert received_ns > 0


async def test_read_loop_eof_fails_pending_request() -> None:
    """Server-side EOF ends the loop and fails the pending request."""
    client = HAPClient(ClientIdentity.generate(0))
    client._reader = cast("asyncio.StreamReader", _FakeReader([]))
    pending = asyncio.get_running_loop().create_future()
    client._pending = pending
    await client._read_loop()
    assert client._closed
    assert pending.done()
    assert isinstance(pending.exception(), ConnectionError)


async def test_read_loop_reader_error_is_logged_and_fails_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A socket error terminates the loop, logs it and fails the waiter."""
    client = HAPClient(ClientIdentity.generate(0))
    client._reader = cast("asyncio.StreamReader", _FailingReader())
    pending = asyncio.get_running_loop().create_future()
    client._pending = pending
    with caplog.at_level(logging.ERROR, logger="pyhap.client.controller"):
        await client._read_loop()
    assert client._closed
    assert isinstance(pending.exception(), ConnectionError)
    assert "reader loop terminated" in caplog.text


async def test_read_loop_buffers_partial_encrypted_block() -> None:
    """A split encrypted record is buffered until the rest of it arrives."""
    shared_key = b"\x01" * 32
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
    encrypted = b"".join(HAPCrypto(shared_key).encrypt(payload))
    client = HAPClient(ClientIdentity.generate(0))
    client._reader = cast(
        "asyncio.StreamReader", _FakeReader([encrypted[:10], encrypted[10:]])
    )
    client._crypto = HAPCrypto(shared_key, client_side=True)
    pending = asyncio.get_running_loop().create_future()
    client._pending = pending
    await client._read_loop()
    frame = pending.result()
    assert frame.kind == "response"
    assert frame.body == b"{}"


def test_raw_public_accepts_private_and_public_keys() -> None:
    """_raw_public handles both key types."""
    private = x25519.X25519PrivateKey.generate()
    assert _raw_public(private) == _raw_public(private.public_key())


async def test_pair_verify_rejects_wrong_m2_sequence() -> None:
    """An M2 answer with the wrong sequence number aborts the handshake."""

    def responder(
        method: bytes, target: bytes, body: bytes, content_type: bytes
    ) -> Frame:
        answer = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M1)
        return Frame(kind="response", status=200, body=answer, received_ns=0)

    client = _StubbedClient(responder)
    client._reader = cast("asyncio.StreamReader", _FakeReader([]))
    with pytest.raises(RuntimeError, match="Unexpected pair-verify response"):
        await client._pair_verify()


def _responder_with_valid_m2(
    second_answer: bytes,
) -> Callable[[bytes, bytes, bytes, bytes], Frame]:
    """Answer M1 with a cryptographically valid M2, then ``second_answer``.

    The client runs without a pinned LTPK, so the accessory signature is not
    verified and any proof bytes do.
    """
    server_ephemeral = x25519.X25519PrivateKey.generate()
    calls = 0

    def responder(
        method: bytes, target: bytes, body: bytes, content_type: bytes
    ) -> Frame:
        nonlocal calls
        calls += 1
        if calls == 1:
            client_public = tlv.decode(body)[HAP_TLV_TAGS.PUBLIC_KEY]
            shared_key = server_ephemeral.exchange(
                x25519.X25519PublicKey.from_public_bytes(client_public)
            )
            output_key = hap_hkdf(shared_key, _PVERIFY_1_SALT, _PVERIFY_1_INFO)
            proof = tlv.encode(
                HAP_TLV_TAGS.USERNAME,
                b"Accessory",
                HAP_TLV_TAGS.PROOF,
                b"\x00" * 64,
            )
            cipher = ChaCha20Poly1305(output_key)
            encrypted = bytes(cipher.encrypt(_PVERIFY_1_NONCE, proof, b""))
            answer = tlv.encode(
                HAP_TLV_TAGS.SEQUENCE_NUM,
                HAP_TLV_STATES.M2,
                HAP_TLV_TAGS.PUBLIC_KEY,
                _raw_public(server_ephemeral),
                HAP_TLV_TAGS.ENCRYPTED_DATA,
                encrypted,
            )
        else:
            answer = second_answer
        return Frame(kind="response", status=200, body=answer, received_ns=0)

    return responder


@pytest.mark.parametrize(
    ("second_answer", "match"),
    [
        (
            tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M2),
            "Unexpected pair-verify response",
        ),
        (
            tlv.encode(
                HAP_TLV_TAGS.SEQUENCE_NUM,
                HAP_TLV_STATES.M4,
                HAP_TLV_TAGS.ERROR_CODE,
                b"\x02",
            ),
            "pair-verify rejected",
        ),
    ],
)
async def test_pair_verify_rejects_bad_m4(second_answer: bytes, match: str) -> None:
    """After a valid M2, a wrong-sequence or error M4 aborts the handshake."""
    client = _StubbedClient(_responder_with_valid_m2(second_answer))
    client._reader = cast("asyncio.StreamReader", _FakeReader([]))
    with pytest.raises(RuntimeError, match=match):
        await client._pair_verify()


async def test_write_characteristics_raises_on_error_status() -> None:
    """A write answered with an error status raises instead of returning."""

    def responder(
        method: bytes, target: bytes, body: bytes, content_type: bytes
    ) -> Frame:
        return Frame(kind="response", status=500, body=b"boom", received_ns=0)

    client = _StubbedClient(responder)
    client._reader = cast("asyncio.StreamReader", _FakeReader([]))
    with pytest.raises(RuntimeError, match="write failed: 500"):
        await client.write_characteristics([{"aid": 1, "iid": 2, "value": True}])
