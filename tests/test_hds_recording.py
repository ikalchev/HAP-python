"""Tests for the HDS dataSend recording transfer."""

import asyncio
import functools
import os

import pytest

from pyhap import hds, hds_recording, hds_server
from pyhap.hap_crypto import hap_hkdf
from pyhap.hds_protocol import EVENT, REQUEST, RESPONSE, HDSStatus, Message


def _paired(listener):
    secret, salt = os.urandom(32), os.urandom(32)
    accessory_salt = listener.register_transport(secret, salt)
    acc_read, acc_write = hds.derive_keys(secret, salt, accessory_salt)
    return hds.HDSCrypto(acc_write, acc_read)


def test_derive_session_keys():
    secret, controller_key_salt, accessory_key_salt = (os.urandom(32) for _ in "abc")
    salt = controller_key_salt + accessory_key_salt
    keys = hds.derive_session_keys(
        functools.partial(hap_hkdf, secret), controller_key_salt, accessory_key_salt
    )
    assert keys == (
        hap_hkdf(secret, salt, b"HDS-Read-Encryption-Key"),
        hap_hkdf(secret, salt, b"HDS-Write-Encryption-Key"),
    )
    assert keys == hds.derive_keys(secret, controller_key_salt, accessory_key_salt)
    assert hds.derive_session_keys(lambda salt, info: None, b"", b"") is None
    # A session that ends between the two derivations yields no keys either.
    for missing in (b"HDS-Read-Encryption-Key", b"HDS-Write-Encryption-Key"):
        assert (
            hds.derive_session_keys(
                lambda salt, info, missing=missing: None if info == missing else b"k",
                b"",
                b"",
            )
            is None
        )


class _CaptureTransport(asyncio.Transport):
    def __init__(self):
        super().__init__()
        self.buffer = bytearray()
        self.closed = False

    def write(self, data):
        self.buffer += data

    def close(self):
        self.closed = True


def _drain_events(controller, transport):
    """Decrypt and return every buffered accessory->controller message."""
    buffer = bytearray(transport.buffer)
    transport.buffer = bytearray()
    messages = []
    while True:
        payload = controller.decrypt_frame(buffer)
        if payload is None:
            break
        messages.append(Message.decode(payload))
    return messages


async def _run_recording(delegate, chunk_size=hds_recording.DEFAULT_CHUNK_SIZE):
    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    hds_recording.RecordingStreamManager(connection, delegate, chunk_size=chunk_size)

    # Bind with the control handshake.
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    _drain_events(controller, transport)

    # Open the recording data stream.
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 99, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    # Let the streaming task run.
    await asyncio.sleep(0.05)
    return controller, transport


@pytest.mark.asyncio
async def test_recording_stream_open_and_fragments():
    async def delegate(stream_id):
        assert stream_id == 99
        yield hds_recording.RecordingPacket(b"moov-init-segment")
        yield hds_recording.RecordingPacket(b"fragment-1")
        yield hds_recording.RecordingPacket(b"fragment-2", is_last=True)

    controller, transport = await _run_recording(delegate)
    messages = _drain_events(controller, transport)

    # First message is the open response, then three data events.
    assert messages[0].kind == RESPONSE
    assert messages[0].topic == "open"
    assert messages[0].status == HDSStatus.SUCCESS

    data_events = [m for m in messages if m.kind == EVENT and m.topic == "data"]
    assert len(data_events) == 3
    metas = [e.message["packets"][0]["metadata"] for e in data_events]
    assert metas[0]["dataType"] == hds_recording.MEDIA_INITIALIZATION
    assert metas[1]["dataType"] == hds_recording.MEDIA_FRAGMENT
    assert [m["dataSequenceNumber"] for m in metas] == [1, 2, 3]
    assert all(m["isLastDataChunk"] for m in metas)
    assert data_events[-1].message["endOfStream"] is True
    assert data_events[0].message["packets"][0]["data"] == b"moov-init-segment"


@pytest.mark.asyncio
async def test_recording_fragment_chunking():
    fragment = os.urandom(1000)

    async def delegate(stream_id):
        yield hds_recording.RecordingPacket(b"init")
        yield hds_recording.RecordingPacket(fragment, is_last=True)

    controller, transport = await _run_recording(delegate, chunk_size=256)
    messages = _drain_events(controller, transport)
    data_events = [m for m in messages if m.kind == EVENT and m.topic == "data"]

    # init (1 chunk) + fragment split into ceil(1000/256)=4 chunks.
    fragment_events = [
        e
        for e in data_events
        if e.message["packets"][0]["metadata"]["dataSequenceNumber"] == 2
    ]
    assert len(fragment_events) == 4
    metas = [e.message["packets"][0]["metadata"] for e in fragment_events]
    assert [m["dataChunkSequenceNumber"] for m in metas] == [1, 2, 3, 4]
    # dataTotalSize only on the first chunk.
    assert metas[0]["dataTotalSize"] == 1000
    assert "dataTotalSize" not in metas[1]
    assert [m["isLastDataChunk"] for m in metas] == [False, False, False, True]
    # Reassembling the chunks reproduces the fragment.
    reassembled = b"".join(e.message["packets"][0]["data"] for e in fragment_events)
    assert reassembled == fragment
    # endOfStream only on the very last chunk.
    assert fragment_events[-1].message["endOfStream"] is True
    assert "endOfStream" not in fragment_events[0].message


@pytest.mark.asyncio
async def test_recording_open_rejects_wrong_type():
    async def delegate(stream_id):
        yield hds_recording.RecordingPacket(b"x", is_last=True)

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    hds_recording.RecordingStreamManager(connection, delegate)

    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    _drain_events(controller, transport)

    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 1, "type": "wrong", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await asyncio.sleep(0.02)
    response = _drain_events(controller, transport)[0]
    assert response.status == HDSStatus.PROTOCOL_SPECIFIC_ERROR
    assert (
        response.message["status"] == hds_recording.RecordingReason.UNEXPECTED_FAILURE
    )


@pytest.mark.parametrize(
    ("topic", "stream_id", "stops"),
    [
        ("close", 7, True),
        ("close", 8, False),
        ("ack", 7, True),
        ("ack", 8, False),
    ],
)
@pytest.mark.asyncio
async def test_recording_close_event_stops_stream(topic, stream_id, stops):
    """A dataSend close or ack EVENT for the current stream stops it."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def delegate(_stream_id):
        yield hds_recording.RecordingPacket(b"init")
        started.set()
        await release.wait()  # block until the controller closes
        yield hds_recording.RecordingPacket(b"never", is_last=True)

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    manager = hds_recording.RecordingStreamManager(connection, delegate)

    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    _drain_events(controller, transport)
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 7, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await started.wait()
    _drain_events(controller, transport)

    connection.data_received(
        controller.encrypt_frame(
            Message("dataSend", EVENT, topic, {"streamId": stream_id}).encode()
        )
    )
    await asyncio.sleep(0.02)
    # Events get no response, and the blocked "never" fragment was not sent.
    assert not _drain_events(controller, transport)
    task = manager._task
    assert task is not None
    assert task.done() is stops
    release.set()
    await asyncio.sleep(0.02)


@pytest.mark.parametrize("topic", ["ack", "close"])
@pytest.mark.asyncio
async def test_ack_after_end_of_stream_keeps_delegate_cleanup(topic):
    """The controller acks endOfStream while the delegate is still cleaning up."""
    in_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def delegate(_stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init")
            yield hds_recording.RecordingPacket(b"last", is_last=True)
        finally:
            in_cleanup.set()
            await release_cleanup.wait()  # e.g. waiting for ffmpeg to exit
            cleanup_done.set()

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    manager = hds_recording.RecordingStreamManager(connection, delegate)

    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 5, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await in_cleanup.wait()
    assert _drain_events(controller, transport)[-1].message["endOfStream"] is True

    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend", EVENT, topic, {"streamId": 5, "endOfStream": True}
            ).encode()
        )
    )
    await asyncio.sleep(0.01)
    release_cleanup.set()
    await asyncio.sleep(0.01)
    assert cleanup_done.is_set()
    task = manager._task
    assert task is not None
    assert not task.cancelled()
    assert manager._closed


def _open(connection, controller, stream_id, request_id):
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {
                    "streamId": stream_id,
                    "type": "ipcamera.recording",
                    "target": "controller",
                },
                id=request_id,
            ).encode()
        )
    )


def _event(connection, controller, topic, body):
    connection.data_received(
        controller.encrypt_frame(Message("dataSend", EVENT, topic, body).encode())
    )


@pytest.mark.asyncio
async def test_close_stops_a_second_stream_on_the_same_connection():
    """A stream that ended on its own must not block stopping the next one."""
    second_started = asyncio.Event()

    async def delegate(stream_id):
        yield hds_recording.RecordingPacket(b"init")
        if stream_id == 1:
            yield hds_recording.RecordingPacket(b"last", is_last=True)
            return
        second_started.set()
        await asyncio.Event().wait()  # block until the controller closes

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    connection.connection_made(_CaptureTransport())
    manager = hds_recording.RecordingStreamManager(connection, delegate)
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )

    _open(connection, controller, 1, 2)
    await asyncio.sleep(0.01)
    task = manager._task
    assert task is not None
    assert task.done()
    _open(connection, controller, 2, 3)
    await second_started.wait()

    _event(connection, controller, "close", {"streamId": 2})
    await asyncio.sleep(0.01)
    task = manager._task
    assert task is not None
    assert task.cancelled()


@pytest.mark.parametrize("body", [[7], None, 7, {"streamId": "7"}])
@pytest.mark.asyncio
async def test_malformed_close_event_is_ignored(body):
    started = asyncio.Event()

    async def delegate(_stream_id):
        yield hds_recording.RecordingPacket(b"init")
        started.set()
        await asyncio.Event().wait()

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    manager = hds_recording.RecordingStreamManager(connection, delegate)
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    _open(connection, controller, 7, 2)
    await started.wait()

    _event(connection, controller, "close", body)
    await asyncio.sleep(0.01)
    task = manager._task
    assert task is not None
    assert not task.done()
    assert not transport.closed
    manager._stop()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_unbound_connection_is_capped():
    """A connection that never completes the control handshake must not buffer
    unboundedly: once it exceeds the pre-bind cap it is closed.
    """
    listener = hds_server.HDSListener()
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    connection.data_received(b"\x00" * (hds_server._MAX_UNBOUND_BYTES + 1))
    assert transport.closed


def test_pending_transports_are_bounded():
    """Abandoned SetupDataStreamTransport registrations must not accumulate."""
    listener = hds_server.HDSListener()
    for _ in range(hds_server._MAX_PENDING_TRANSPORTS + 5):
        listener.register_transport(os.urandom(32), os.urandom(32))
    assert len(listener._pending) == hds_server._MAX_PENDING_TRANSPORTS


@pytest.mark.asyncio
async def test_connection_loss_cancels_stream_and_runs_delegate_cleanup():
    """Dropping the TCP connection mid-recording must cancel the stream task and
    run the delegate's cleanup (e.g. terminate its ffmpeg subprocess).
    """
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def delegate(stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init")
            started.set()
            await asyncio.Event().wait()  # block until cancelled
            yield hds_recording.RecordingPacket(b"never", is_last=True)
        finally:
            cleaned.set()

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    manager = hds_recording.RecordingStreamManager(connection, delegate)
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 5, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await started.wait()

    connection.connection_lost(None)
    await asyncio.sleep(0.02)
    assert cleaned.is_set()
    task = manager._task
    assert task is not None
    assert task.done()


@pytest.mark.asyncio
async def test_write_backpressure_gates_fragments():
    """While the transport is paused, no data events are produced; resuming lets
    them flow.
    """

    async def delegate(stream_id):
        yield hds_recording.RecordingPacket(b"init")
        yield hds_recording.RecordingPacket(b"fragment", is_last=True)

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    hds_recording.RecordingStreamManager(connection, delegate)
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    connection.pause_writing()
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 8, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await asyncio.sleep(0.02)
    paused = [
        m
        for m in _drain_events(controller, transport)
        if m.kind == EVENT and m.topic == "data"
    ]
    assert paused == []  # nothing sent while paused

    connection.resume_writing()
    await asyncio.sleep(0.02)
    resumed = [
        m
        for m in _drain_events(controller, transport)
        if m.kind == EVENT and m.topic == "data"
    ]
    assert len(resumed) == 2  # init + fragment now delivered


@pytest.mark.asyncio
async def test_double_stop_does_not_abort_delegate_cleanup():
    """A dataSend/close followed by a TCP drop (two _stop calls) must not cancel
    the stream twice and abort the delegate's in-flight async cleanup.
    """
    started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def delegate(stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init")
            started.set()
            await asyncio.Event().wait()  # block until cancelled
        finally:
            await release_cleanup.wait()  # async teardown that must complete
            cleanup_done.set()

    listener = hds_server.HDSListener()
    controller = _paired(listener)
    connection = hds_server.HDSConnection(listener)
    transport = _CaptureTransport()
    connection.connection_made(transport)
    manager = hds_recording.RecordingStreamManager(connection, delegate)
    connection.data_received(
        controller.encrypt_frame(
            Message("control", REQUEST, "hello", {}, id=1).encode()
        )
    )
    connection.data_received(
        controller.encrypt_frame(
            Message(
                "dataSend",
                REQUEST,
                "open",
                {"streamId": 9, "type": "ipcamera.recording", "target": "controller"},
                id=2,
            ).encode()
        )
    )
    await started.wait()

    manager._stop()  # cancel #1 (dataSend/close) -> enters aclose cleanup
    await asyncio.sleep(0.01)
    connection.connection_lost(None)  # cancel #2 (TCP drop) must be suppressed
    await asyncio.sleep(0.01)
    assert not cleanup_done.is_set()  # still blocked in the delegate's cleanup
    release_cleanup.set()
    await asyncio.sleep(0.01)
    assert cleanup_done.is_set()  # cleanup ran to completion despite double stop


@pytest.mark.asyncio
async def test_fmp4_recording_packets_splits_init_and_fragments():
    """Yield ftyp+moov as init and each moof+mdat as a fragment, then end.

    Exercises the source-agnostic framer end to end over a synthetic stream.
    """

    def box(box_type, body=b"\x00" * 8):
        return (8 + len(body)).to_bytes(4, "big") + box_type + body

    stream = (
        box(b"ftyp")
        + box(b"moov")  # initialization
        + box(b"moof")
        + box(b"mdat")  # fragment 1
        + box(b"moof")
        + box(b"mdat")  # fragment 2
    )
    reader = asyncio.StreamReader()
    reader.feed_data(stream)
    reader.feed_eof()

    packets = [p async for p in hds_recording.fmp4_recording_packets(reader)]
    assert len(packets) == 4
    # init packet carries both ftyp and moov, not flagged last
    assert packets[0].data[4:8] == b"ftyp"
    assert b"moov" in packets[0].data
    assert not packets[0].is_last
    assert packets[1].data[4:8] == b"moof" and not packets[1].is_last
    assert packets[2].data[4:8] == b"moof" and not packets[2].is_last
    # EOF is signalled with an empty last packet
    assert packets[3].data == b"" and packets[3].is_last


@pytest.mark.asyncio
async def test_fmp4_recording_packets_handles_64bit_box_size():
    """A box using the 64-bit extended-size form is read whole."""
    body = b"\x11" * 8
    ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"\x00" * 8
    moov = (16).to_bytes(4, "big") + b"moov" + b"\x00" * 8
    # moof with 64-bit size: size32 == 1, then 64-bit size = 16 + 8 + len(body)
    big_moof = (
        (1).to_bytes(4, "big") + b"moof" + (16 + len(body)).to_bytes(8, "big") + body
    )
    reader = asyncio.StreamReader()
    mdat = (8).to_bytes(4, "big") + b"mdat"
    reader.feed_data(ftyp + moov + big_moof + mdat)
    reader.feed_eof()
    packets = [p async for p in hds_recording.fmp4_recording_packets(reader)]
    assert len(packets) == 3
    assert packets[1].data == big_moof + mdat
    assert packets[2].is_last
