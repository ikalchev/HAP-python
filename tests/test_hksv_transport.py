"""Tests for the HDS transport lifecycle: binding, start/stop and timeouts."""

import asyncio
import errno
import logging
import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from pyhap import camera as camera_module, hds, hds_recording, hds_server
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.hds_protocol import REQUEST, Message


async def _until(condition):
    """Wait until ``condition()`` holds, failing after two seconds."""

    async def poll():
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 2)


def _ipv6_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


class _SlowCleanupCamera(Camera):
    """A camera whose recording cleanup takes a while, like reaping ffmpeg."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.init_is_last = False
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def handle_recording_stream(self, stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init", is_last=self.init_is_last)
            self.started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.1)
            self.cleaned.set()


def _make_camera(camera_class=Camera):
    with (
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=MagicMock(), listen_address="127.0.0.1")
        options = {
            "video": {
                "codec": {
                    "profiles": [
                        camera_module.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"]
                    ],
                    "levels": [camera_module.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"]],
                },
                "resolutions": [[1280, 720, 30]],
            },
            "audio": {"codecs": [{"type": "OPUS", "samplerate": 24}]},
            "srtp": True,
            "address": "127.0.0.1",
            "recording": True,
        }
        return camera_class(options, driver, "Cam")


@pytest.fixture(name="camera")
def camera_fixture():
    return _make_camera()


async def _start_without_gate(camera):
    """Start the camera transport with a plain recording manager per connection.

    This exercises the camera's delegate without depending on open gating.
    """
    await camera.start_recording_transport()
    managers = []

    def on_connection(connection):
        managers.append(
            hds_recording.RecordingStreamManager(connection, camera._recording_delegate)
        )

    camera._hds_listener.on_connection = on_connection
    return managers


async def _open_recording(listener):
    """Connect as a controller, bind with hello and open a recording stream."""
    secret, salt = os.urandom(32), os.urandom(32)
    accessory_salt = listener.register_transport(secret, salt)
    acc_read, acc_write = hds.derive_keys(secret, salt, accessory_salt)
    controller = hds.HDSCrypto(acc_write, acc_read)
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    open_body = {"streamId": 1, "type": "ipcamera.recording", "target": "controller"}
    for message in (
        Message("control", REQUEST, "hello", {}, id=1),
        Message("dataSend", REQUEST, "open", open_body, id=2),
    ):
        writer.write(controller.encrypt_frame(message.encode()))
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_listener_binds_all_hosts_to_one_port():
    hosts = ["127.0.0.1", "::1"] if _ipv6_available() else ["127.0.0.1"]
    listener = hds_server.HDSListener()
    port = await listener.start(hosts)
    try:
        assert port == listener.port
        bound = {
            sock.getsockname()[:2]
            for server in listener._servers
            for sock in server.sockets
        }
        assert bound == {(host, port) for host in hosts}
        for host in hosts:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_listener_wildcard_uses_one_port_for_all_families():
    listener = hds_server.HDSListener()
    port = await listener.start("")
    try:
        ports = {sock.getsockname()[1] for sock in listener._servers[0].sockets}
        assert ports == {port}
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_listener_start_failure_releases_bound_hosts():
    listener = hds_server.HDSListener()
    with pytest.raises(OSError):
        await listener.start(["127.0.0.1", "256.0.0.1"])
    assert not listener._servers


@pytest.mark.asyncio
async def test_start_recording_transport_binds_hap_listen_address(camera):
    await camera.start_recording_transport()
    try:
        assert camera.driver.listen_address() == "127.0.0.1"
        sockets = camera._hds_listener._servers[0].sockets
        assert sockets[0].getsockname()[0] == "127.0.0.1"
        assert camera.recording_transport_port == sockets[0].getsockname()[1]
        # Already started: a second call keeps the listener.
        listener = camera._hds_listener
        await camera.start_recording_transport()
        assert camera._hds_listener is listener
    finally:
        await camera.stop_recording_transport()
    assert camera._hds_listener is None
    assert camera.recording_transport_port is None


@pytest.mark.asyncio
async def test_concurrent_starts_publish_one_bound_listener(camera):
    release = asyncio.Event()
    original_start = hds_server.HDSListener.start
    seen_while_binding = []

    async def slow_start(self, host):
        seen_while_binding.append(camera._hds_listener)
        await release.wait()
        return await original_start(self, host)

    with (
        patch.object(hds_server.HDSListener, "start", slow_start),
        patch.object(hds_server.HDSListener, "stop", autospec=True) as stop,
    ):
        first = asyncio.ensure_future(camera.start_recording_transport())
        second = asyncio.ensure_future(camera.start_recording_transport())
        await asyncio.sleep(0)
        # SetupDataStreamTransport must not see a listener without a port.
        assert seen_while_binding == [None, None]
        assert camera._hds_listener is None
        release.set()
        await asyncio.gather(first, second)
        # The concurrent loser is stopped, the winner is bound.
        assert stop.call_count == 1
        assert stop.call_args.args[0] is not camera._hds_listener
    assert camera._hds_listener.port > 0
    await camera.stop_recording_transport()
    await stop.call_args.args[0].stop()


@pytest.mark.asyncio
async def test_stop_gives_up_on_stuck_task(caplog):
    listener = hds_server.HDSListener()
    await listener.start("127.0.0.1")
    connection = hds_server.HDSConnection(listener)

    # Nothing cancels this task, so stop() must give up after the timeout.
    task = asyncio.ensure_future(asyncio.sleep(10))
    connection.track_task(task)
    await listener.stop(timeout=0.05)
    assert "1 HDS recording tasks did not stop" in caplog.text
    task.cancel()


@pytest.mark.asyncio
async def test_drain_timeout_aborts_stalled_peer():
    listener = hds_server.HDSListener()
    connection = hds_server.HDSConnection(listener)
    transport = MagicMock()
    # Like asyncio, abort() reports the connection lost on the next iteration.
    transport.abort.side_effect = lambda: asyncio.get_running_loop().call_soon(
        connection.connection_lost, None
    )
    connection.connection_made(transport)
    connection.pause_writing()
    with (
        patch.object(hds_server, "_DRAIN_TIMEOUT", 0.01),
        pytest.raises(ConnectionResetError),
    ):
        await asyncio.wait_for(connection.drain(), 1)
    transport.abort.assert_called_once()


@pytest.mark.asyncio
async def test_stalled_hub_stops_stream_without_failure_warning(caplog):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    connections = []

    async def delegate(stream_id):
        try:
            started.set()
            while True:
                yield hds_recording.RecordingPacket(b"x" * 1000)
                await asyncio.sleep(0.01)
        finally:
            cleaned.set()

    def on_connection(connection):
        connections.append(connection)
        hds_recording.RecordingStreamManager(connection, delegate)

    listener = hds_server.HDSListener()
    listener.on_connection = on_connection
    await listener.start("127.0.0.1")
    _, writer = await _open_recording(listener)
    await asyncio.wait_for(started.wait(), 2)
    caplog.set_level(logging.INFO)
    with patch.object(hds_server, "_DRAIN_TIMEOUT", 0.05):
        connections[0].pause_writing()  # the hub stops reading
        await asyncio.wait_for(cleaned.wait(), 2)
    assert "peer stopped reading" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    writer.close()
    await listener.stop()


@pytest.mark.asyncio
async def test_stop_drops_connection_before_handshake():
    listener = hds_server.HDSListener()
    port = await listener.start("127.0.0.1")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _until(lambda: listener._connections)
    # Must not wait for the unbound connection's handshake timeout.
    await asyncio.wait_for(listener.stop(), 1)
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()


@pytest.mark.asyncio
async def test_stop_aborts_connection_with_unsent_data():
    connections = []
    listener = hds_server.HDSListener()
    listener.on_connection = connections.append
    port = await listener.start("127.0.0.1")
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    await _until(lambda: connections and connections[0]._transport)
    # The peer does not read, so this stays queued. close() would wait for it.
    connections[0]._transport.write(b"x" * 32 * 1024 * 1024)
    await asyncio.wait_for(listener.stop(), 1)
    await _until(lambda: not listener._connections)
    writer.close()


@pytest.mark.asyncio
async def test_listener_needs_an_address():
    with pytest.raises(ValueError):
        await hds_server.HDSListener().start([])


def _fail_first_bind_of_other(loop, error, calls):
    """Fake create_server: binding "other" (another family) fails once."""
    real_create_server = loop.create_server

    async def create_server(factory, host, port):
        calls.append((host, port))
        if host == "other":
            if len(calls) == 2:
                raise OSError(error, os.strerror(error))
            host, port = "127.0.0.1", 0
        return await real_create_server(factory, host, port)

    return patch.object(loop, "create_server", side_effect=create_server)


@pytest.mark.asyncio
async def test_listener_retries_port_taken_in_other_family():
    calls = []
    listener = hds_server.HDSListener()
    with _fail_first_bind_of_other(asyncio.get_running_loop(), errno.EADDRINUSE, calls):
        port = await listener.start(["127.0.0.1", "other"])
    # The first address is rebound to a fresh port after the conflict.
    assert [host_port[1] for host_port in calls[::2]] == [0, 0]
    assert calls[3] == ("other", port)
    assert len(listener._servers) == 2
    await listener.stop()


@pytest.mark.asyncio
async def test_listener_does_not_retry_other_bind_errors():
    calls = []
    listener = hds_server.HDSListener()
    with (
        _fail_first_bind_of_other(asyncio.get_running_loop(), errno.EACCES, calls),
        pytest.raises(PermissionError),
    ):
        await listener.start(["127.0.0.1", "other"])
    assert len(calls) == 2
    assert not listener._servers


@pytest.mark.asyncio
async def test_run_and_stop_drive_recording_transport(camera):
    await camera.run()
    assert camera._hds_listener.port > 0
    await camera.stop()
    assert camera._hds_listener is None


@pytest.mark.asyncio
async def test_stop_during_start_does_not_publish_listener(camera):
    release = asyncio.Event()
    original_start = hds_server.HDSListener.start
    binding = []

    async def slow_start(self, host):
        binding.append(self)
        await release.wait()
        return await original_start(self, host)

    with patch.object(hds_server.HDSListener, "start", slow_start):
        start = asyncio.ensure_future(camera.start_recording_transport())
        await asyncio.sleep(0)
        await camera.stop_recording_transport()
        release.set()
        await start
    assert camera._hds_listener is None
    assert not binding[0]._servers


@pytest.mark.asyncio
async def test_stop_recording_transport_awaits_camera_cleanup():
    camera = _make_camera(_SlowCleanupCamera)
    await _start_without_gate(camera)
    _, writer = await _open_recording(camera._hds_listener)
    await asyncio.wait_for(camera.started.wait(), 2)
    listener = camera._hds_listener
    await asyncio.wait_for(camera.stop_recording_transport(), 2)
    assert camera.cleaned.is_set()
    # Finished tasks are dropped from the listener.
    assert not listener._tasks
    writer.close()


@pytest.mark.asyncio
async def test_finished_stream_runs_camera_cleanup_in_its_task():
    camera = _make_camera(_SlowCleanupCamera)
    camera.init_is_last = True
    managers = await _start_without_gate(camera)
    _, writer = await _open_recording(camera._hds_listener)
    await _until(lambda: managers and managers[0]._task is not None)
    # The stream ends after the last packet, its cleanup must be part of it.
    await asyncio.wait_for(managers[0]._task, 2)
    assert camera.cleaned.is_set()
    writer.close()
    await camera.stop_recording_transport()


async def _connection_count(camera, count):
    while camera.recording_connection_count != count:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_recording_connection_count(camera):
    await camera.start_recording_transport()
    try:
        assert camera.recording_connection_count == 0
        _, writer = await asyncio.open_connection(
            "127.0.0.1", camera.recording_transport_port
        )
        await asyncio.wait_for(_connection_count(camera, 1), 1)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(_connection_count(camera, 0), 1)
    finally:
        await camera.stop_recording_transport()


def test_abort_before_connection_made():
    """A connection aborted before its transport exists does nothing."""
    hds_server.HDSConnection(hds_server.HDSListener()).abort()
