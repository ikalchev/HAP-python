"""Tests for pyhap.media.multi_tier: SRTP multi-tier sessions from a source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import pathlib
import socket
from typing import Any

import pylibsrtp
import pytest

from pyhap.media import MediaError, rtp
from pyhap.media.hevc import HevcDepacketizer
from pyhap.media.multi_tier import MultiTierSender
from pyhap.media.pool import SourcePool
from pyhap.media.source import MediaSource
from pyhap.media.worker import MediaWorker, MediaWorkerError

from .media_peer import KEYFRAME, TRAIL, Source, wait_until

pytestmark = pytest.mark.timeout(10)

VIDEO_KEY = bytes(range(30))
AUDIO_KEY = bytes(range(30, 60))
VIDEO_SSRC = 0x1111
AUDIO_SSRC = 0x2222
CONTROLLER_SSRC = 0x4242


class Controller:
    """Plays an iOS controller: receives SRTP, sends SRTCP back."""

    def __init__(self) -> None:
        """Open the receive sockets."""
        self.video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for sock in (self.video, self.audio):
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
        self.video_port = self.video.getsockname()[1]
        self.audio_port = self.audio.getsockname()[1]
        self._video_in = _inbound(VIDEO_KEY)
        self._audio_in = _inbound(AUDIO_KEY)
        self._out = pylibsrtp.Session(
            pylibsrtp.Policy(
                key=VIDEO_KEY, ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND
            )
        )
        self._audio_out = pylibsrtp.Session(
            pylibsrtp.Policy(
                key=AUDIO_KEY, ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND
            )
        )
        self.video_packets: list[rtp.RtpPacket] = []
        self.audio_packets: list[rtp.RtpPacket] = []
        self.rtcp = 0
        self.rtcp_types: list[list[int]] = []
        self.units: list[list[bytes]] = []
        self._depacketizer = HevcDepacketizer()

    def poll(self) -> None:
        """Read and decrypt what arrived."""
        for sock, inbound, packets in (
            (self.video, self._video_in, self.video_packets),
            (self.audio, self._audio_in, self.audio_packets),
        ):
            while True:
                try:
                    data = sock.recv(4096)
                except BlockingIOError:
                    break
                if 192 <= data[1] <= 223:
                    compound = rtp.parse_rtcp(inbound.unprotect_rtcp(data))
                    self.rtcp_types.append([m.packet_type for m in compound])
                    self.rtcp += 1
                    continue
                packet = rtp.parse_rtp(inbound.unprotect(data))
                packets.append(packet)
                if sock is self.video:
                    unit = self._depacketizer.push(
                        packet.sequence, packet.timestamp, packet.marker, packet.payload
                    )
                    if unit is not None:
                        self.units.append(unit.nals)

    def send_pli(self, port: int) -> None:
        """Ask the accessory for a keyframe on its video port."""
        pli = self._out.protect_rtcp(rtp.build_pli(CONTROLLER_SSRC, VIDEO_SSRC))
        self.video.sendto(pli, ("127.0.0.1", port))

    def send_audio_report(self, port: int) -> None:
        """Send an SRTCP receiver report on the accessory's audio port."""
        report = rtp.build_pli(CONTROLLER_SSRC + 1, AUDIO_SSRC)
        self.audio.sendto(self._audio_out.protect_rtcp(report), ("127.0.0.1", port))

    def close(self) -> None:
        """Close the sockets."""
        self.video.close()
        self.audio.close()


def _inbound(key: bytes) -> pylibsrtp.Session:
    return pylibsrtp.Session(
        pylibsrtp.Policy(key=key, ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND)
    )


@pytest.fixture
async def controller() -> AsyncIterator[Controller]:
    """Yield a controller and close it afterwards."""
    peer = Controller()
    yield peer
    peer.close()


@pytest.fixture
async def source() -> AsyncIterator[MediaSource]:
    """Yield a started source and close it afterwards."""
    media = MediaSource()
    await media.start()
    yield media
    media.close()


def make_sender(controller: Controller, **kwargs: Any) -> MultiTierSender:
    """A sender towards the controller."""
    options: dict[str, Any] = {
        "address": "127.0.0.1",
        "video_port": controller.video_port,
        "audio_port": controller.audio_port,
        "video_key": VIDEO_KEY,
        "audio_key": AUDIO_KEY,
        "video_ssrc": VIDEO_SSRC,
        "audio_ssrc": AUDIO_SSRC,
        "keyframe_request_interval": 0,
        "rtcp_notify_interval": 0,
    }
    return MultiTierSender(**(options | kwargs))


async def received(controller: Controller, predicate: Any) -> None:
    """Poll the controller until ``predicate()`` holds."""

    def check() -> bool:
        controller.poll()
        return bool(predicate())

    await wait_until(check)


async def test_video_starts_with_a_keyframe(
    controller: Controller, source: MediaSource
) -> None:
    """Verify SRTP video decrypts, carries the session SSRC and starts at IRAP."""
    sender = make_sender(controller)
    requests: list[tuple[float | None, float | None]] = []
    sender.on_keyframe_request = lambda age, gop: requests.append((age, gop))
    video_port, audio_port = await sender.bind()
    assert video_port and audio_port
    await sender.start(source)
    assert requests == [(None, None)]
    camera = Source(source.rtp_input)
    camera.send_video([TRAIL])
    await asyncio.sleep(0.05)
    camera.send_video(KEYFRAME)
    camera.send_video([TRAIL])
    await received(controller, lambda: len(controller.units) == 2)
    assert controller.units == [KEYFRAME, [TRAIL]]
    packets = controller.video_packets
    assert {p.ssrc for p in packets} == {VIDEO_SSRC}
    assert {p.payload_type for p in packets} == {99}
    sequences = [p.sequence for p in packets]
    assert sequences == [(sequences[0] + i) & 0xFFFF for i in range(len(packets))]
    assert packets[-1].timestamp - packets[0].timestamp == 3000
    camera.close()
    await sender.close()
    assert sender.closed


async def test_audio_is_sent_as_srtp(
    controller: Controller, source: MediaSource
) -> None:
    """Verify Opus goes out with the audio SSRC and key, and not without audio."""
    sender = make_sender(controller)
    await sender.start(source)
    camera = Source(source.rtp_input)
    camera.send_audio(b"opus-1")
    await received(controller, lambda: controller.audio_packets)
    packet = controller.audio_packets[0]
    assert (packet.ssrc, packet.payload_type, packet.payload) == (
        AUDIO_SSRC,
        110,
        b"opus-1",
    )
    assert packet.marker
    await sender.close()

    silent = make_sender(controller)
    await silent.start(source, audio=False)
    camera.send_audio(b"opus-2")
    await asyncio.sleep(0.1)
    controller.poll()
    assert len(controller.audio_packets) == 1
    camera.close()
    await silent.close()


async def test_controller_rtcp_keeps_alive_and_asks_for_keyframes(
    controller: Controller, source: MediaSource
) -> None:
    """Verify authentic controller RTCP calls on_rtcp, a PLI asks for a keyframe."""
    sender = make_sender(controller)
    alive: list[None] = []
    requests: list[float | None] = []
    sender.on_rtcp = lambda: alive.append(None)
    sender.on_keyframe_request = lambda age, _gop: requests.append(age)
    video_port, audio_port = await sender.bind()
    await sender.start(source)
    controller.send_pli(video_port)
    await wait_until(lambda: len(requests) == 2 and bool(alive))
    # Unauthenticated datagrams, e.g. with a spoofed address, do not count.
    # Each socket delivers in order: the junk is handled before the next
    # authentic packet, which proves the junk was not counted.
    controller.video.sendto(b"\x81\xc9junk", ("127.0.0.1", video_port))
    controller.send_pli(video_port)
    await wait_until(lambda: len(requests) == 3)
    assert len(alive) == 2
    controller.audio.sendto(b"\x81\xc9junk", ("127.0.0.1", audio_port))
    controller.send_audio_report(audio_port)
    controller.send_audio_report(audio_port)
    await wait_until(lambda: len(alive) >= 4)
    assert len(alive) == 4
    await sender.close()


async def test_rtcp_from_others_is_ignored(
    controller: Controller, source: MediaSource
) -> None:
    """Verify datagrams from another address do not keep the session alive."""
    sender = make_sender(controller, address="127.0.0.2")
    alive: list[None] = []
    sender.on_rtcp = lambda: alive.append(None)
    video_port, _ = await sender.bind()
    await sender.start(source)
    controller.send_pli(video_port)
    await asyncio.sleep(0.1)
    assert not alive
    await sender.close()


async def test_bye_is_a_compound_packet(
    controller: Controller, source: MediaSource
) -> None:
    """Verify the BYE on close comes with a report and a CNAME (RFC 3550 6.1).

    Audio has sent nothing, so it reports as a receiver; video as a sender.
    """
    sender = make_sender(controller)
    await sender.start(source)
    camera = Source(source.rtp_input)
    camera.send_video(KEYFRAME)
    await received(controller, lambda: controller.units)
    await sender.close()
    byes = [
        [rtp.RTCP_SR, rtp.RTCP_SDES, rtp.RTCP_BYE],
        [rtp.RTCP_RR, rtp.RTCP_SDES, rtp.RTCP_BYE],
    ]
    await received(controller, lambda: all(b in controller.rtcp_types for b in byes))
    camera.close()


async def test_close_while_binding_releases_the_ports(
    controller: Controller, source: MediaSource
) -> None:
    """Verify a session closed during bind leaves no port open."""
    sender = make_sender(controller)
    binding = asyncio.ensure_future(sender.bind())
    await asyncio.sleep(0)
    await sender.close()
    with pytest.raises(MediaError):
        await binding
    for port in (sender._video_port, sender._audio_port):
        assert port is None or port.transport is None or port.transport.is_closing()


async def test_close_waits_for_an_ending_session(
    controller: Controller, source: MediaSource
) -> None:
    """Verify close() during an ending of its own returns once ports are released."""
    sender = make_sender(controller)
    closed: list[str] = []
    sender.on_closed = closed.append
    await sender.start(source)
    sender.on_source_closed("gone")
    await sender.close()
    assert closed == ["media source ended: gone"]
    assert sender._video_port is not None
    assert sender._video_port.transport is not None
    assert sender._video_port.transport.is_closing()


async def test_sender_reports(controller: Controller, source: MediaSource) -> None:
    """Verify SRTCP sender reports reach the controller once media flowed."""
    sender = make_sender(controller)
    await sender.start(source)
    camera = Source(source.rtp_input)
    camera.send_video(KEYFRAME)
    await received(controller, lambda: controller.rtcp >= 1)
    camera.close()
    await sender.close()


async def test_switch_continues_the_stream(
    controller: Controller, source: MediaSource
) -> None:
    """Verify a switch keeps SSRC and sequence and waits for the next IRAP."""
    other = MediaSource()
    await other.start()
    sender = make_sender(controller)
    await sender.start(source)
    first = Source(source.rtp_input)
    first.send_video(KEYFRAME)
    await received(controller, lambda: len(controller.units) == 1)
    await sender.switch(other)
    assert sender.source is other
    first.send_video([TRAIL])
    second = Source(other.rtp_input, ssrc=0xCCCC)
    second.send_video([TRAIL])
    await asyncio.sleep(0.05)
    second.send_video(KEYFRAME)
    await received(controller, lambda: len(controller.units) == 2)
    assert controller.units == [KEYFRAME, KEYFRAME]
    sequences = [p.sequence for p in controller.video_packets]
    assert sequences == [(sequences[0] + i) & 0xFFFF for i in range(len(sequences))]
    assert {p.ssrc for p in controller.video_packets} == {VIDEO_SSRC}
    first.close()
    second.close()
    await sender.close()
    other.close()


async def test_switch_keeps_rtp_time_going_with_the_same_input_ssrc(
    controller: Controller, source: MediaSource
) -> None:
    """Verify RTP time continues after a switch even if the input SSRC repeats."""
    other = MediaSource()
    await other.start()
    sender = make_sender(controller, keyframe_request_interval=60)
    requests: list[float | None] = []
    sender.on_keyframe_request = lambda age, _gop: requests.append(age)
    await sender.start(source)
    first = Source(source.rtp_input)
    first.timestamp = 900_000
    first.send_video(KEYFRAME)
    await received(controller, lambda: len(controller.units) == 1)
    before = len(requests)
    await sender.switch(other)
    # The switch asks the new source for a keyframe, whatever the rate limit.
    assert len(requests) == before + 1
    second = Source(other.rtp_input)
    second.timestamp = 1000
    second.send_video(KEYFRAME)
    await received(controller, lambda: len(controller.units) == 2)
    stamps = [p.timestamp for p in controller.video_packets]
    assert stamps[-1] - stamps[0] > 0
    assert stamps[-1] - stamps[0] < 90000
    first.close()
    second.close()
    await sender.close()
    other.close()


async def test_start_waits_for_parameter_sets(
    controller: Controller, source: MediaSource
) -> None:
    """Verify the stream starts at an IRAP picture that carries VPS, SPS, PPS."""
    sender = make_sender(controller)
    await sender.start(source)
    camera = Source(source.rtp_input)
    camera.send_video([KEYFRAME[-1]])
    camera.send_video([TRAIL])
    await asyncio.sleep(0.05)
    camera.send_video(KEYFRAME)
    await received(controller, lambda: len(controller.units) == 1)
    assert controller.units == [KEYFRAME]
    camera.close()
    await sender.close()


async def test_own_packets_sent_back_do_not_keep_alive(
    controller: Controller, source: MediaSource
) -> None:
    """Verify the accessory's own SRTCP, reflected back, is not the controller."""
    sender = make_sender(controller)
    alive: list[None] = []
    sender.on_rtcp = lambda: alive.append(None)
    video_port, _ = await sender.bind()
    await sender.start(source)
    own = controller._out.protect_rtcp(rtp.build_pli(VIDEO_SSRC, VIDEO_SSRC))
    controller.video.sendto(own, ("127.0.0.1", video_port))
    controller.send_pli(video_port)
    await wait_until(lambda: bool(alive))
    await asyncio.sleep(0.05)
    assert len(alive) == 1
    await sender.close()


async def test_idle_input_ends_the_session(
    controller: Controller, source: MediaSource
) -> None:
    """Verify the sender ends itself without video input."""
    sender = make_sender(controller, idle_timeout=0.1)
    closed: list[str] = []
    sender.on_closed = closed.append
    await sender.start(source)
    await wait_until(lambda: bool(closed))
    assert closed == ["media input idle"]
    assert sender.closed


async def test_closed_source_ends_the_session(
    controller: Controller, source: MediaSource
) -> None:
    """Verify the sender ends with its source."""
    sender = make_sender(controller)
    closed: list[str] = []
    sender.on_closed = closed.append
    await sender.start(source)
    source.close("gone")
    await wait_until(lambda: bool(closed))
    assert closed == ["media source ended: gone"]


async def test_close_is_quiet_and_final(
    controller: Controller, source: MediaSource
) -> None:
    """Verify close() calls no callback and a closed sender refuses to start."""
    sender = make_sender(controller)
    closed: list[str] = []
    sender.on_closed = closed.append
    await sender.bind()
    await sender.close()
    await sender.close()
    assert not closed
    with pytest.raises(Exception, match="closed"):
        await sender.start(source)


def test_key_length_is_checked(controller: Controller) -> None:
    """Verify a key without its salt is refused."""
    with pytest.raises(ValueError, match="30 bytes"):
        make_sender(controller, video_key=bytes(16))


@pytest.fixture(params=[False, True], ids=["thread", "process"])
async def worker(
    request: pytest.FixtureRequest, tmp_path: pathlib.Path
) -> AsyncIterator[MediaWorker]:
    """Yield a started media worker thread or process and stop it afterwards."""
    started = MediaWorker(separate_process=request.param, log_dir=tmp_path)
    await started.start()
    yield started
    await started.close()


def sender_config(controller: Controller, **kwargs: Any) -> dict[str, Any]:
    """The sender arguments towards the controller."""
    options: dict[str, Any] = {
        "address": "127.0.0.1",
        "video_port": controller.video_port,
        "audio_port": controller.audio_port,
        "video_key": VIDEO_KEY,
        "audio_key": AUDIO_KEY,
        "video_ssrc": VIDEO_SSRC,
        "audio_ssrc": AUDIO_SSRC,
        "keyframe_request_interval": 0,
        "rtcp_notify_interval": 0,
    }
    return options | kwargs


@pytest.mark.timeout(30)
async def test_sender_in_worker(controller: Controller, worker: MediaWorker) -> None:
    """Verify a multi-tier session runs in the worker with its events."""
    source = await worker.open_source()
    sender = worker.create_multi_tier_sender(**sender_config(controller))
    alive: list[None] = []
    requests: list[float | None] = []
    sender.on_rtcp = lambda: alive.append(None)
    sender.on_keyframe_request = lambda age, _gop: requests.append(age)
    video_port, audio_port = await sender.bind()
    assert video_port and audio_port and sender.ports == (video_port, audio_port)
    await sender.start(source, audio=True)
    assert sender.started
    camera = Source(source.rtp_input)
    camera.send_video(KEYFRAME)
    camera.send_audio(b"opus")
    await received(
        controller, lambda: controller.units == [KEYFRAME] and controller.audio_packets
    )
    controller.send_pli(video_port)
    await wait_until(lambda: len(requests) == 2 and bool(alive))
    other = await worker.open_source()
    await sender.switch(other)
    camera.close()
    await sender.close()
    assert sender.closed
    await source.close()
    await other.close()


@pytest.mark.timeout(30)
async def test_worker_exit_ends_the_sender(
    controller: Controller, worker: MediaWorker
) -> None:
    """Verify a multi-tier session ends with its worker."""
    sender = worker.create_multi_tier_sender(**sender_config(controller))
    closed: list[str] = []
    sender.on_closed = closed.append
    await sender.bind()
    assert worker._runner is not None
    worker._runner.kill()
    await wait_until(lambda: bool(closed))
    assert "media worker exited" in closed[0]
    assert sender.closed


@pytest.mark.timeout(30)
async def test_worker_sender_refuses_bad_use(
    controller: Controller, worker: MediaWorker
) -> None:
    """Verify unknown options, a foreign source and an unbound start are refused."""
    with pytest.raises(TypeError, match="Unknown multi-tier sender options"):
        worker.create_multi_tier_sender(**sender_config(controller), pacing=1)
    source = await worker.open_source()
    sender = worker.create_multi_tier_sender(**sender_config(controller))
    with pytest.raises(MediaWorkerError, match="No multi-tier session"):
        await sender.start(source)
    await sender.bind()
    with pytest.raises(MediaWorkerError, match="already bound"):
        await sender.bind()
    other_worker = MediaWorker()
    try:
        foreign = await other_worker.open_source()
        with pytest.raises(MediaWorkerError, match="another worker"):
            await sender.start(foreign)
    finally:
        await other_worker.close()
    await sender.close()
    await source.close()


async def test_pool_creates_senders_in_process(controller: Controller) -> None:
    """Verify a pool without a worker makes in-process multi-tier senders."""
    pool = SourcePool()
    sender = pool.create_multi_tier_sender(**sender_config(controller))
    assert isinstance(sender, MultiTierSender)
    await sender.close()
