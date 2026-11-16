"""Recordings through RecordingMediaDelegate, in process and in the worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import sys
from typing import Any

import pytest

from pyhap import camera as camera_module, hksv3, hksv_recording as hr
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.hds_recording import RecordingPacket
from pyhap.media import mpegts, recording_delegate as delegate_module
from pyhap.media.pool import SourcePool
from pyhap.media.recording_delegate import RecordingError, RecordingMediaDelegate
from pyhap.media.worker import MediaWorker
from pyhap.util import base64_to_bytes, to_base64_str

from .media_hap import FRAME_INTERVAL, GOP_FRAMES, SyntheticPull, bridged
from .media_peer import KEYFRAME, TRAIL, Source, wait_until
from .test_media_recording import selected

pytestmark = pytest.mark.timeout(30)

# Stands in for ffmpeg: like it, nothing before the first input, then an init
# segment and one fragment per input read whose mdat holds the MPEG-TS it read.
FAKE_TRANSCODER = """
import os, struct
def box(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body
init = box(b"ftyp", b"isom") + box(b"moov", b"")
while data := os.read(0, 65536):
    os.write(1, init + box(b"moof", b"") + box(b"mdat", data))
    init = b""
"""
# Stands in for ffmpeg reading a camera that stalls: on the first input the
# init segment and argv[1] fragments, then fragments from 1.5 s later.
PAUSING_TRANSCODER = """
import os, struct, sys, time
def box(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body
fragment = box(b"moof", b"") + box(b"mdat", os.read(0, 65536))
os.write(1, box(b"ftyp", b"isom") + box(b"moov", b"") + fragment * int(sys.argv[1]))
time.sleep(1.5)
while data := os.read(0, 65536):
    os.write(1, box(b"moof", b"") + box(b"mdat", data))
"""
MEDIUM = hksv3.VideoStreamTier(2, hksv3.VideoQuality.MEDIUM, 1500, 1280, 720, 30)
# NAL units of an H.264 camera, which hold no H.265 IRAP picture.
H264_IDR = bytes([0x65, 0x88]) + bytes(200)
H264_SLICE = bytes([0x41, 0x9A]) + bytes(200)
# Frames a camera sends before its first IRAP picture when it is joined late.
LATE_IRAP = 15
NO_VIDEO = (
    "No video to record within 0.2 s: the camera sent no H.265 IRAP picture"
    " or stopped after one"
)


def h265(frame: int) -> list[bytes]:
    """A frame of a camera with an IRAP picture every GOP_FRAMES frames."""
    return KEYFRAME if frame % GOP_FRAMES == 0 else [TRAIL]


def late_irap(frame: int) -> list[bytes]:
    """A frame of a camera joined LATE_IRAP frames before its IRAP picture."""
    return [TRAIL] if frame < LATE_IRAP else h265(frame - LATE_IRAP)


def h264(frame: int) -> list[bytes]:
    """A frame of an H.264 camera."""
    return [H264_IDR] if frame % GOP_FRAMES == 0 else [H264_SLICE]


def no_video(_frame: int) -> list[bytes]:
    """No frame: a camera that sends nothing, or a connection that hangs."""
    return []


class ScriptedPull(SyntheticPull):
    """Plays a camera channel that sends ``script(n)`` as its n-th frame."""

    def __init__(self, script: Callable[[int], list[bytes]]) -> None:
        """Not running yet."""
        super().__init__()
        self._script = script

    async def _send(self, source: Source) -> None:
        try:
            while True:
                if nals := self._script(self.frames):
                    source.send_video(nals)
                self.frames += 1
                await asyncio.sleep(FRAME_INTERVAL)
        finally:
            source.close()


class Provider:
    """Leases one synthetic channel per tier quality, without audio."""

    ffmpeg = "ffmpeg"

    def __init__(
        self, pool: SourcePool, pull: Callable[[], SyntheticPull] = SyntheticPull
    ) -> None:
        """Remember the pulls it makes with ``pull``."""
        self.pool = pool
        self.pull = pull
        self.pulls: dict[str, SyntheticPull] = {}

    async def lease_channel(
        self, tier: hksv3.VideoStreamTier, _audio: Any = None
    ) -> Any:
        """Lease the channel of a tier."""
        name = hksv3.VideoQuality(tier.quality).name.lower()

        def make() -> SyntheticPull:
            self.pulls[name] = self.pull()
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
    # Two GOPs buffered, the recording starts with an IRAP picture.
    await wait_until(lambda: provider.pulls["medium"].frames > 2 * GOP_FRAMES)
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


async def test_camera_retries_a_failed_arm(
    stream_driver: AccessoryDriver,
    stream_camera: Callable[..., Camera],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify an arm that fails, e.g. while the camera is offline, is retried."""
    monkeypatch.setattr(camera_module, "RECORDING_ARM_RETRY", 0.01)
    fake = FakeDelegate()
    failures = [OSError("offline"), OSError("offline")]
    original = fake.set_armed

    async def flaky(armed: bool) -> None:
        if armed and failures:
            fake.calls.append(armed)
            raise failures.pop(0)
        await original(armed)

    fake.set_armed = flaky  # type: ignore[method-assign]
    camera = bridged(stream_driver, stream_camera(recording={"delegate": fake}))
    service = camera.get_service("CameraRecordingManagement")
    assert service is not None
    await camera.start_recording_transport()
    service.get_characteristic("Active").client_update_value(1)
    await wait_until(lambda: fake.armed)
    assert fake.calls == [True, True, True]
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


async def test_buffer_comes_back_after_its_source_ends(
    pool: SourcePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an armed delegate buffers again when its channel ends, e.g. a crash."""
    monkeypatch.setattr(delegate_module, "REOPEN_DELAY", 0.01)
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM, prebuffer_ms=1000)
    await delegate.arm()
    first = delegate._recorder
    assert first is not None and delegate._lease is not None
    source = delegate._lease.source
    result = source.close("worker exited")
    if asyncio.iscoroutine(result):
        await result
    await wait_until(
        lambda: (
            delegate._recorder is not None
            and delegate._recorder is not first
            and not delegate._recorder.closed
        )
    )
    assert delegate.buffering
    await delegate.close()
    assert delegate._recorder is None


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


@pytest.mark.parametrize("prebuffer_ms", [0, 1000])
@pytest.mark.parametrize("script", [no_video, h264], ids=["no-video", "h264"])
async def test_recording_without_video_fails(
    pool: SourcePool,
    fake_transcoder: list[list[str]],
    prebuffer_ms: int,
    script: Callable[[int], list[bytes]],
) -> None:
    """Verify a recording that gets no H.265 video in time ends with an error."""
    provider = Provider(pool, lambda: ScriptedPull(script))
    delegate = RecordingMediaDelegate(
        provider, MEDIUM, prebuffer_ms=prebuffer_ms, first_video_timeout=0.2
    )
    await delegate.arm()
    with pytest.raises(RecordingError) as error:
        await anext(delegate.record(selected(), audio=True))
    assert str(error.value) == NO_VIDEO
    assert not delegate.recording
    await delegate.close()
    assert pool.counts() == {}


@pytest.mark.parametrize(
    ("prebuffer_ms", "held"),
    [(0, {}), (1000, {"medium": (1, 1)})],
    ids=["released", "buffering"],
)
async def test_recording_after_one_without_video(
    pool: SourcePool,
    fake_transcoder: list[list[str]],
    prebuffer_ms: int,
    held: dict[str, tuple[int, int]],
) -> None:
    """Verify a recording that got no video leaves the delegate ready to record."""
    video = False
    provider = Provider(
        pool, lambda: ScriptedPull(lambda frame: h265(frame) if video else [])
    )
    delegate = RecordingMediaDelegate(
        provider, MEDIUM, prebuffer_ms=prebuffer_ms, first_video_timeout=0.2
    )
    await delegate.arm()
    with pytest.raises(RecordingError, match=NO_VIDEO):
        await anext(delegate.record(selected(), audio=True))
    assert not delegate.recording
    # A prebuffering delegate keeps its channel, any other gave it back.
    assert pool.counts() == held
    video = True
    delegate.first_video_timeout = delegate_module.DEFAULT_FIRST_VIDEO_TIMEOUT
    stream = delegate.record(selected(), audio=True)
    init = await anext(stream)
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    first = await anext(stream)
    assert mpegts.START_CODE + KEYFRAME[0] in first.data
    await stream.aclose()
    await delegate.close()
    assert pool.counts() == {}


async def test_late_first_irap_within_the_bound_records(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify a channel whose first IRAP picture comes late, but in time, records."""
    provider = Provider(pool, lambda: ScriptedPull(late_irap))
    delegate = RecordingMediaDelegate(provider, MEDIUM, first_video_timeout=5.0)
    stream = delegate.record(selected(), audio=True)
    init = await anext(stream)
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    # Video came all the while, the recording waited for the IRAP picture.
    assert provider.pulls["medium"].frames > LATE_IRAP
    first = await anext(stream)
    assert mpegts.START_CODE + KEYFRAME[0] in first.data
    await stream.aclose()
    assert pool.counts() == {}


def pausing_transcoder(monkeypatch: pytest.MonkeyPatch, fragments: int) -> None:
    """Replace the ffmpeg command with the pausing transcoder."""
    command = [sys.executable, "-c", PAUSING_TRANSCODER, str(fragments)]
    monkeypatch.setattr(
        delegate_module, "recording_ffmpeg_args", lambda *_a, **_k: command
    )


async def test_init_segment_without_fragment_fails(
    pool: SourcePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a recording whose first fragment does not come in time fails."""
    pausing_transcoder(monkeypatch, fragments=0)
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM, first_video_timeout=1.0)
    stream = delegate.record(selected(), audio=True)
    init = await anext(stream)
    assert init.data.startswith(b"\x00\x00\x00\x0cftyp")
    # Its first fragment would come after the pause, past the bound.
    with pytest.raises(RecordingError, match="No video to record within 1 s"):
        await anext(stream)
    assert not delegate.recording
    assert pool.counts() == {}


async def test_bound_counts_from_the_request(
    pool: SourcePool, fake_transcoder: list[list[str]]
) -> None:
    """Verify the time to open the channel counts toward the bound."""

    class Slow(Provider):
        async def lease_channel(
            self, tier: hksv3.VideoStreamTier, _audio: Any = None
        ) -> Any:
            await asyncio.sleep(1.0)
            return await super().lease_channel(tier, _audio)

    # The IRAP picture comes LATE_IRAP frames, 0.3 s, after the open: in time
    # counted from the open, too late counted from the request.
    provider = Slow(pool, lambda: ScriptedPull(late_irap))
    delegate = RecordingMediaDelegate(provider, MEDIUM, first_video_timeout=1.0)
    with pytest.raises(RecordingError, match="No video to record within 1 s"):
        await anext(delegate.record(selected(), audio=True))
    assert pool.counts() == {}


async def test_bound_ends_with_the_first_fragment(
    pool: SourcePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a recording whose first fragment came in time goes on past the bound."""
    pausing_transcoder(monkeypatch, fragments=1)
    delegate = RecordingMediaDelegate(Provider(pool), MEDIUM, first_video_timeout=1.0)
    stream = delegate.record(selected(), audio=True)
    await anext(stream)
    await anext(stream)
    # The next fragment comes after the pause, past the bound.
    assert b"moof" in (await anext(stream)).data[:16]
    await stream.aclose()
    assert pool.counts() == {}


def test_first_video_timeout_defaults_to_10_s() -> None:
    """Verify a recording gets 10 s to its first fragment by default."""
    delegate = RecordingMediaDelegate(Provider(SourcePool()), MEDIUM)
    assert delegate.first_video_timeout == 10.0
    assert delegate_module.DEFAULT_FIRST_VIDEO_TIMEOUT == 10.0


@pytest.mark.parametrize("seconds", [0, -1.0, float("nan")])
def test_first_video_timeout_must_be_positive(seconds: float) -> None:
    """Verify a bound that is not a positive number is refused."""
    with pytest.raises(ValueError, match="first_video_timeout"):
        RecordingMediaDelegate(
            Provider(SourcePool()), MEDIUM, first_video_timeout=seconds
        )
