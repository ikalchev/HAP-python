"""Recordings through RecordingMediaDelegate, in process and in the worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import sys
from typing import Any

import pytest

from pyhap import hksv3, hksv_recording as hr
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.hds_recording import RecordingPacket
from pyhap.media import mpegts, recording_delegate as delegate_module
from pyhap.media.pool import SourcePool
from pyhap.media.recording_delegate import RecordingError, RecordingMediaDelegate
from pyhap.media.worker import MediaWorker
from pyhap.util import base64_to_bytes, to_base64_str

from .test_media_recording import selected
from .webrtc_hap import SyntheticPull, bridged
from .webrtc_peer import KEYFRAME, wait_until

pytestmark = pytest.mark.timeout(30)

# Stands in for ffmpeg: an init segment, then one fragment per input read
# whose mdat holds the MPEG-TS it read.
FAKE_TRANSCODER = """
import os, struct
def box(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body
os.write(1, box(b"ftyp", b"isom") + box(b"moov", b""))
while data := os.read(0, 65536):
    os.write(1, box(b"moof", b"") + box(b"mdat", data))
"""
MEDIUM = hksv3.VideoStreamTier(2, hksv3.VideoQuality.MEDIUM, 1500, 1280, 720, 30)


class Provider:
    """Leases one synthetic channel per tier quality, without audio."""

    ffmpeg = "ffmpeg"

    def __init__(self, pool: SourcePool) -> None:
        """Remember the pulls it makes."""
        self.pool = pool
        self.pulls: dict[str, SyntheticPull] = {}

    async def lease_channel(
        self, tier: hksv3.VideoStreamTier, _audio: Any = None
    ) -> Any:
        """Lease the channel of a tier."""
        name = hksv3.VideoQuality(tier.quality).name.lower()

        def make() -> SyntheticPull:
            self.pulls[name] = SyntheticPull()
            return self.pulls[name]

        return await self.pool.lease(name, make, audio=False, name=name)


@pytest.fixture(params=["in-process", "worker"])
async def pool(request: pytest.FixtureRequest) -> AsyncIterator[SourcePool]:
    """A pool in process or in a started worker."""
    worker = None
    if request.param == "worker":
        worker = MediaWorker()
        await worker.start()
    pool = SourcePool(worker)
    yield pool
    await pool.close()
    if worker is not None:
        await worker.close()


@pytest.fixture
def fake_transcoder(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the ffmpeg command with the fake transcoder; record the args."""
    commands: list[list[str]] = []

    def args(*_args: Any, **kwargs: Any) -> list[str]:
        commands.append([str(kwargs.get("audio")), str(kwargs.get("source_audio"))])
        return [sys.executable, "-c", FAKE_TRANSCODER]

    monkeypatch.setattr(delegate_module, "recording_ffmpeg_args", args)
    return commands


async def test_recording_starts_with_the_buffer(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify an armed channel records its buffered seconds first."""
    provider = Provider(pool)
    delegate = RecordingMediaDelegate(provider, MEDIUM, prebuffer_ms=1000)
    await delegate.arm()
    assert delegate.armed
    assert provider.pulls["medium"].starts == 1
    await asyncio.sleep(1.2)
    packets: list[RecordingPacket] = []
    stream = delegate.record(selected(), audio=True)
    async for packet in stream:
        packets.append(packet)
        if len(packets) == 2:
            break
    await stream.aclose()
    init, first = packets
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    assert b"moof" in first.data[:16]
    # The first fragment starts with the tables and a buffered IRAP picture.
    mdat = first.data[first.data.index(b"mdat") + 4 :]
    assert mdat[0] == 0x47
    assert mpegts.START_CODE + KEYFRAME[0] in mdat
    assert not delegate.recording
    assert fake_transcoder == [["True", "False"]]
    # Still armed and buffering after the recording.
    assert delegate.armed
    assert pool.counts() == {"medium": (1, 1)}
    await delegate.disarm()
    assert pool.counts() == {}
    assert provider.pulls["medium"].stops == 1


async def test_recording_channel_is_shared(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify a live lease and the recording share one pull."""
    provider = Provider(pool)
    delegate = RecordingMediaDelegate(provider, MEDIUM, prebuffer_ms=1000)
    await delegate.arm()
    live = await provider.lease_channel(MEDIUM)
    await live.activate()
    assert pool.counts() == {"medium": (2, 2)}
    assert provider.pulls["medium"].starts == 1
    await delegate.disarm()
    assert pool.counts() == {"medium": (1, 1)}
    assert provider.pulls["medium"].stops == 0
    await live.release()
    assert provider.pulls["medium"].stops == 1


async def test_no_pull_while_idle_without_prebuffer(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify the default pulls the channel only for a recording."""
    provider = Provider(pool)
    delegate = RecordingMediaDelegate(provider, MEDIUM)
    assert delegate.prebuffer_ms == 0
    await delegate.arm()
    assert delegate.armed
    assert not delegate.buffering
    assert provider.pulls == {}
    assert pool.counts() == {}
    stream = delegate.record(selected(), audio=False)
    init = await anext(stream)
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    assert pool.counts() == {"medium": (1, 1)}
    first = await anext(stream)
    assert mpegts.START_CODE + KEYFRAME[0] in first.data
    await stream.aclose()
    # Released after the recording although the hub still has recording on.
    assert delegate.armed
    assert pool.counts() == {}
    assert provider.pulls["medium"].starts == 1
    assert provider.pulls["medium"].stops == 1
    await delegate.disarm()


async def test_record_without_arming(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify a recording buffers on demand and releases the channel after."""
    provider = Provider(pool)
    delegate = RecordingMediaDelegate(provider, MEDIUM)
    stream = delegate.record(selected(), audio=False)
    init = await anext(stream)
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    first = await anext(stream)
    assert mpegts.START_CODE + KEYFRAME[0] in first.data
    await stream.aclose()
    assert pool.counts() == {}


async def test_failing_transcoder_raises(
    pool: SourcePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a transcoder that fails ends the recording with an error."""
    monkeypatch.setattr(
        delegate_module,
        "recording_ffmpeg_args",
        lambda *_a, **_k: [sys.executable, "-c", "raise SystemExit(2)"],
    )
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM)
    await delegate.arm()
    with pytest.raises(RecordingError, match="exited with 2"):
        async for _packet in delegate.record(selected(), audio=True):
            pass
    await delegate.close()


async def test_disarm_ends_a_running_recording(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify disarming ends the stream instead of leaving it hanging."""
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM)
    await delegate.arm()
    stream = delegate.record(selected(), audio=True)
    await anext(stream)
    await delegate.disarm()
    with pytest.raises(RecordingError, match="released"):
        async for _packet in stream:
            pass


class FakeDelegate:
    """Records what the camera asks of a recording delegate."""

    prebuffer_ms = 3000

    def __init__(self) -> None:
        self.armed = False
        self.calls: list[bool] = []
        self.closed = False
        self.records: list[tuple[hr.SelectedRecordingConfiguration, bool]] = []

    async def set_armed(self, armed: bool) -> None:
        self.calls.append(armed)
        self.armed = armed

    async def close(self) -> None:
        self.closed = True
        self.armed = False

    async def record(
        self, chosen: hr.SelectedRecordingConfiguration, audio: bool
    ) -> AsyncIterator[RecordingPacket]:
        self.records.append((chosen, audio))
        yield RecordingPacket(b"init")
        yield RecordingPacket(b"", is_last=True)


async def test_camera_arms_and_records_with_the_delegate(
    stream_driver: AccessoryDriver, stream_camera: Callable[..., Camera]
) -> None:
    """Verify the camera advertises, arms, records and closes the delegate."""
    fake = FakeDelegate()
    camera = bridged(stream_driver, stream_camera(recording={"delegate": fake}))
    service = camera.get_service("CameraRecordingManagement")
    assert service is not None
    supported = hr.SupportedRecordingConfiguration.decode(
        base64_to_bytes(
            service.get_characteristic("SupportedCameraRecordingConfiguration").value
        )
    )
    assert supported.prebuffer_length_ms == 3000
    await camera.start_recording_transport()
    assert fake.calls == []
    service.get_characteristic("Active").client_update_value(1)
    await wait_until(lambda: fake.calls == [True])
    camera.set_selected_recording_configuration(to_base64_str(selected().encode()))
    packets = [packet async for packet in camera.handle_recording_stream(1)]
    assert [p.data for p in packets] == [b"init", b""]
    assert fake.records[0][1] is False
    service.get_characteristic("Active").client_update_value(0)
    await wait_until(lambda: fake.calls == [True, False])
    service.get_characteristic("Active").client_update_value(1)
    await wait_until(lambda: fake.calls == [True, False, True])
    await camera.stop()
    assert fake.closed


async def test_camera_without_delegate_records_nothing(
    stream_driver: AccessoryDriver, stream_camera: Callable[..., Camera]
) -> None:
    """Verify the default recording stream stays empty without a delegate."""
    camera = bridged(stream_driver, stream_camera())
    camera.set_selected_recording_configuration(to_base64_str(selected().encode()))
    assert [p async for p in camera.handle_recording_stream(1)] == []


@pytest.mark.parametrize("delegate", [True, False])
def test_warns_about_hds_recordings_with_multi_tier(
    stream_camera: Callable[..., Camera],
    caplog: pytest.LogCaptureFixture,
    delegate: bool,
) -> None:
    """Verify a multi-tier camera that records over HDS warns once."""
    options = {"recording": {"delegate": FakeDelegate()}} if delegate else {}
    stream_camera(**options)
    warnings = [r for r in caplog.records if "do not upload HDS" in r.getMessage()]
    assert len(warnings) == (1 if delegate else 0)


async def test_failed_arm_disarms(pool: SourcePool) -> None:
    """Verify an arm whose channel cannot open leaves the delegate disarmed."""

    class Failing(Provider):
        async def lease_channel(
            self, tier: hksv3.VideoStreamTier, _audio: Any = None
        ) -> Any:
            raise OSError("no camera")

    delegate = RecordingMediaDelegate(Failing(pool), MEDIUM, prebuffer_ms=1000)
    with pytest.raises(OSError):
        await delegate.arm()
    assert not delegate.armed
    assert not delegate.buffering


async def test_failed_start_releases_the_channel(
    pool: SourcePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a recording that cannot start gives the channel back."""

    def broken(*_args: Any, **_kwargs: Any) -> list[str]:
        raise KeyError("sample rate")

    monkeypatch.setattr(delegate_module, "recording_ffmpeg_args", broken)
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM)
    await delegate.arm()
    stream = delegate.record(selected(), audio=True)
    with pytest.raises(KeyError):
        await stream.__anext__()
    assert pool.counts() == {}


async def test_ending_recording_keeps_the_next_one(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify an ending recording does not release a channel the next one uses."""
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM)
    await delegate.arm()
    first = delegate.record(selected(), audio=True)
    await first.__anext__()
    recorder: Any = delegate._recorder
    assert recorder is not None
    stop = recorder.stop

    async def stop_then_start_next() -> None:
        await stop()
        # The next recording claims the recorder before the first releases.
        second = delegate.record(selected(), audio=True)
        nonlocal next_stream
        next_stream = second
        await second.__anext__()

    next_stream: Any = None
    recorder.stop = stop_then_start_next
    await first.aclose()
    recorder.stop = stop
    assert delegate.recording
    assert pool.counts() == {"medium": (1, 1)}
    await next_stream.aclose()
    assert pool.counts() == {}


async def test_slow_hub_ends_the_recording(
    pool: SourcePool,
    fake_transcoder: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify output the hub does not read in time ends the recording."""
    monkeypatch.setattr(delegate_module, "MAX_PENDING_OUTPUT", 16)
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM, prebuffer_ms=1000)
    await delegate.arm()
    await wait_until(lambda: delegate._recorder is not None)
    stream = delegate.record(selected(), audio=True)
    with pytest.raises(RecordingError, match="too slowly"):
        async for _packet in stream:
            await asyncio.sleep(0.2)
    await delegate.disarm()
