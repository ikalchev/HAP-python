"""Tests for HKSV dataSend/open gating and the delegate failure close."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from pyhap import hds, hds_recording, hds_server
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.hds_protocol import EVENT, REQUEST, RESPONSE, HDSStatus, Message

Reason = hds_recording.RecordingReason


class _CaptureTransport(asyncio.Transport):
    def __init__(self):
        super().__init__()
        self.buffer = bytearray()

    def write(self, data):
        self.buffer += data

    def close(self):
        pass


class _Controller:
    """One hub with its own bound HDS connection."""

    def __init__(self, listener, on_connection):
        secret, salt = os.urandom(32), os.urandom(32)
        accessory_salt = listener.register_transport(secret, salt)
        acc_read, acc_write = hds.derive_keys(secret, salt, accessory_salt)
        self.crypto = hds.HDSCrypto(acc_write, acc_read)
        self.connection = hds_server.HDSConnection(listener)
        self.transport = _CaptureTransport()
        self.connection.connection_made(self.transport)
        on_connection(self.connection)
        self.send(Message("control", REQUEST, "hello", {}, id=1))
        self.messages()

    def send(self, message):
        self.connection.data_received(self.crypto.encrypt_frame(message.encode()))

    def open(self, stream_id):
        body = {
            "streamId": stream_id,
            "type": "ipcamera.recording",
            "target": "controller",
        }
        self.send(Message("dataSend", REQUEST, "open", body, id=stream_id + 1))
        return next(m for m in self.messages() if m.kind == RESPONSE)

    def messages(self):
        buffer = bytearray(self.transport.buffer)
        self.transport.buffer = bytearray()
        messages = []
        while (payload := self.crypto.decrypt_frame(buffer)) is not None:
            messages.append(Message.decode(payload))
        return messages


class _GatedCamera(Camera):
    mode = "record"
    recording: asyncio.Event | None = None
    cleaning: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def handle_recording_stream(self, stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init")
            if self.mode == "fail":
                raise RuntimeError("ffmpeg died")
            if self.mode == "complete":
                yield hds_recording.RecordingPacket(b"last", is_last=True)
            assert self.recording is not None
            self.recording.set()
            await asyncio.Event().wait()  # record until the hub stops us
        finally:
            assert self.cleaning is not None
            self.cleaning.set()
            # e.g. waiting for ffmpeg to exit; bounded so a failing test ends
            assert self.release is not None
            await asyncio.wait_for(self.release.wait(), 1)


@pytest.fixture(name="camera")
def camera_fixture():
    with (
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=MagicMock(), listen_address="127.0.0.1")
        camera = _GatedCamera(
            {"address": "127.0.0.1", "recording": True}, driver, "Cam"
        )
    recording_service = camera._recording_service
    assert recording_service is not None
    recording_service.get_characteristic("Active").set_value(1)
    # Gating only needs a non-None "configured" marker; the tests
    # never read the actual recording configuration.
    camera._selected_recording_config = object()  # ty: ignore[invalid-assignment]
    camera.recording = asyncio.Event()
    camera.cleaning = asyncio.Event()
    camera.release = asyncio.Event()
    return camera


def _hub(camera):
    return _Controller(hds_server.HDSListener(), camera._on_hds_connection)


def _assert_rejected(response, reason):
    assert response.status == HDSStatus.PROTOCOL_SPECIFIC_ERROR
    assert response.message["status"] == reason


@pytest.mark.parametrize(
    ("service", "char", "value", "reason"),
    [
        ("CameraRecordingManagement", "Active", 0, Reason.NOT_ALLOWED),
        ("CameraOperatingMode", "HomeKitCameraActive", 0, Reason.NOT_ALLOWED),
    ],
)
@pytest.mark.asyncio
async def test_open_rejected_when_recording_off(camera, service, char, value, reason):
    camera.get_service(service).get_characteristic(char).set_value(value)
    _assert_rejected(_hub(camera).open(1), reason)
    assert camera._recording_stream is None


@pytest.mark.asyncio
async def test_open_rejected_without_selected_configuration(camera):
    camera._selected_recording_config = None
    _assert_rejected(_hub(camera).open(1), Reason.INVALID_CONFIGURATION)
    assert camera._recording_stream is None


@pytest.mark.asyncio
async def test_one_stream_per_camera_and_slot_freed_on_stop(camera):
    """A second hub gets BUSY until the first stream stops, not until cleanup ends."""
    first, second = _hub(camera), _hub(camera)
    assert first.open(1).status == HDSStatus.SUCCESS
    await camera.recording.wait()
    assert camera._recording_stream.streaming
    _assert_rejected(second.open(2), Reason.BUSY)

    first.connection.connection_lost(None)  # first hub gone, cleanup pending
    await camera.cleaning.wait()
    old_task = camera._recording_stream._task
    assert not old_task.done()
    assert second.open(3).status == HDSStatus.SUCCESS

    camera.release.set()
    await asyncio.wait([old_task])
    second.connection.connection_lost(None)
    await asyncio.wait([camera._recording_stream._task])


@pytest.mark.asyncio
async def test_camera_off_closes_running_recording(camera):
    """Turning the camera off in the Home app ends the recording, like HAP-NodeJS."""
    hub = _hub(camera)
    assert hub.open(1).status == HDSStatus.SUCCESS
    await camera.recording.wait()

    camera.get_service("CameraOperatingMode").get_characteristic(
        "HomeKitCameraActive"
    ).client_update_value(0)
    await asyncio.wait_for(camera.cleaning.wait(), 1)
    camera.release.set()
    await asyncio.wait([camera._recording_stream._task])

    closes = [m for m in hub.messages() if m.kind == EVENT and m.topic == "close"]
    assert [c.message for c in closes] == [
        {"streamId": 1, "reason": Reason.NOT_ALLOWED}
    ]


@pytest.mark.asyncio
async def test_camera_turned_on_keeps_running_recording(camera):
    hub = _hub(camera)
    assert hub.open(1).status == HDSStatus.SUCCESS
    await camera.recording.wait()

    camera.get_service("CameraOperatingMode").get_characteristic(
        "HomeKitCameraActive"
    ).client_update_value(1)
    assert camera._recording_stream.streaming
    assert not [m for m in hub.messages() if m.kind == EVENT and m.topic == "close"]

    camera.release.set()
    hub.connection.connection_lost(None)
    await asyncio.wait([camera._recording_stream._task])


@pytest.mark.parametrize("mode", ["complete", "fail"])
@pytest.mark.asyncio
async def test_slot_freed_after_stream_ended(camera, mode):
    """A stream that ended on its own or failed frees the camera's slot."""
    camera.mode = mode
    camera.release.set()
    first, second = _hub(camera), _hub(camera)
    assert first.open(1).status == HDSStatus.SUCCESS
    manager = camera._recording_stream
    await manager._task
    assert not manager.streaming
    assert second.open(2).status == HDSStatus.SUCCESS
    await camera._recording_stream._task


def test_new_manager_is_not_streaming():
    connection = hds_server.HDSConnection(hds_server.HDSListener())

    async def _no_stream(stream_id: int):
        """Yield nothing; the manager is never started in this test."""
        return
        yield  # pragma: no cover

    assert not hds_recording.RecordingStreamManager(connection, _no_stream).streaming


def _open_stream(delegate):
    managers = []
    controller = _Controller(
        hds_server.HDSListener(),
        lambda connection: managers.append(
            hds_recording.RecordingStreamManager(connection, delegate)
        ),
    )
    assert controller.open(7).status == HDSStatus.SUCCESS
    return controller, managers[0]


async def _stream_events(delegate):
    controller, manager = _open_stream(delegate)
    await manager._task
    return [m for m in controller.messages() if m.kind == EVENT]


async def _raises(_stream_id):
    yield hds_recording.RecordingPacket(b"init")
    raise RuntimeError("ffmpeg died")


async def _ends_early(_stream_id):
    yield hds_recording.RecordingPacket(b"init")


async def _empty(_stream_id):
    return
    yield


@pytest.mark.parametrize("delegate", [_raises, _ends_early, _empty])
@pytest.mark.asyncio
async def test_delegate_failure_sends_close(delegate):
    events = await _stream_events(delegate)
    assert events[-1].topic == "close"
    assert events[-1].message == {"streamId": 7, "reason": Reason.UNEXPECTED_FAILURE}
    assert len([e for e in events if e.topic == "close"]) == 1


@pytest.mark.asyncio
async def test_complete_stream_sends_no_close_and_cleans_up():
    cleaned = []

    async def delegate(_stream_id):
        try:
            yield hds_recording.RecordingPacket(b"init")
            yield hds_recording.RecordingPacket(b"last", is_last=True)
            yield hds_recording.RecordingPacket(b"never")
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    controller, manager = _open_stream(delegate)
    await manager._task
    # The delegate's cleanup ran inside the stream, not later via GC.
    assert cleaned
    events = [m for m in controller.messages() if m.kind == EVENT]
    assert [e.topic for e in events] == ["data", "data"]
    assert events[-1].message["endOfStream"] is True


@pytest.mark.asyncio
async def test_failure_after_stop_sends_no_close():
    """A delegate failing after the hub closed the stream sends no close."""
    recording, stopped = asyncio.Event(), asyncio.Event()

    async def delegate(_stream_id):
        yield hds_recording.RecordingPacket(b"init")
        try:
            recording.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()
            raise RuntimeError("cleanup failed")

    controller, manager = _open_stream(delegate)
    await recording.wait()
    manager._stop()
    await stopped.wait()
    await asyncio.wait([manager._task])
    assert not [m for m in controller.messages() if m.topic == "close"]


@pytest.mark.asyncio
async def test_close_only_closes_a_running_stream():
    started = asyncio.Event()

    async def delegate(_stream_id):
        yield hds_recording.RecordingPacket(b"init")
        started.set()
        await asyncio.Event().wait()

    controller, manager = _open_stream(delegate)
    await started.wait()
    manager.close(Reason.NOT_ALLOWED)
    manager.close(Reason.NOT_ALLOWED)
    await asyncio.wait([manager._task])
    manager.close(Reason.NOT_ALLOWED)

    closes = [
        m for m in controller.messages() if m.kind == EVENT and m.topic == "close"
    ]
    assert [c.message for c in closes] == [
        {"streamId": 7, "reason": Reason.NOT_ALLOWED}
    ]
