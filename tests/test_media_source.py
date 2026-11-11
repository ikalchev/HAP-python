"""Tests for pyhap.media.source: the shared RTP input of WebRTC sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import socket
from types import SimpleNamespace

import pytest

from pyhap.media import rtp, source as source_module
from pyhap.media.hevc import AccessUnit, HevcPacketizer
from pyhap.media.source import MediaSource

VPS, SPS, PPS = bytes([64, 1, 1]), bytes([66, 1, 2]), bytes([68, 1, 3])
IDR = bytes([19 << 1, 1]) + bytes(3000)
TRAIL = bytes([1 << 1, 1]) + bytes(500)


class Consumer:
    """Records what a source delivers to one session."""

    def __init__(self, wants_media: bool = True) -> None:
        """Start empty."""
        self.wants_media = wants_media
        self.video: list[tuple[AccessUnit, int]] = []
        self.audio: list[rtp.RtpPacket] = []
        self.closed: list[str] = []

    def on_source_video(self, unit: AccessUnit, ssrc: int) -> None:
        """Record an access unit."""
        self.video.append((unit, ssrc))

    def on_source_audio(self, packet: rtp.RtpPacket) -> None:
        """Record an audio packet."""
        self.audio.append(packet)

    def on_source_closed(self, reason: str) -> None:
        """Record the end of the source."""
        self.closed.append(reason)


class Feeder:
    """Builds RTP for the source's input handlers."""

    def __init__(self, ssrc: int = 1, sequence: int = 100) -> None:
        """Start a stream."""
        self.ssrc = ssrc
        self.sequence = sequence
        self.timestamp = 0

    def unit(self, nals: list[bytes]) -> list[bytes]:
        """Return the RTP packets of one access unit."""
        payloads = HevcPacketizer(1200).packetize(nals)
        packets = []
        for index, payload in enumerate(payloads):
            last = index == len(payloads) - 1
            packet = rtp.RtpPacket(
                96, self.sequence, self.timestamp, self.ssrc, last, payload
            )
            packets.append(rtp.build_rtp(packet))
            self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp += 3000
        return packets


def push(source: MediaSource, packets: list[bytes]) -> None:
    """Hand datagrams to the video input."""
    for packet in packets:
        source._on_video_input(packet)


def units_of(consumer: Consumer) -> list[list[bytes]]:
    """Return the NAL units of each delivered access unit."""
    return [unit.nals for unit, _ssrc in consumer.video]


@pytest.fixture
async def source() -> AsyncIterator[MediaSource]:
    """Yield a started source."""
    started = MediaSource()
    await started.start()
    yield started
    started.close()


async def test_fan_out_reassembles_once(source: MediaSource) -> None:
    """Verify every attached session gets the same access unit object."""
    first, second = Consumer(), Consumer()
    source.attach(first)
    source.attach(second)
    push(source, Feeder().unit([VPS, SPS, PPS, IDR]))
    assert units_of(first) == [[VPS, SPS, PPS, IDR]]
    assert first.video[0][0] is second.video[0][0]
    assert source.session_count == 2


async def test_no_reassembly_without_a_connected_session(source: MediaSource) -> None:
    """Verify input is not reassembled while no session sends, keyframes noted."""
    waiting = Consumer(wants_media=False)
    source.attach(waiting)
    assert source.keyframe_age() is None
    push(source, Feeder().unit([VPS, SPS, PPS, IDR]))
    assert waiting.video == []
    assert source.keyframe_age() is not None
    assert source.last_video_input > 0


async def test_rtcp_on_rtp_port_is_dropped(source: MediaSource) -> None:
    """Verify an RTCP sender report sent to the video port is ignored."""
    consumer = Consumer()
    source.attach(consumer)
    report = rtp.build_sender_report(1, 0, 0, 0, 0)
    source._on_video_input(report)
    source._on_audio_input(report)
    source._on_video_input(b"")
    assert source.last_video_input == 0
    assert consumer.audio == []


async def test_ssrc_change_starts_a_fresh_unit(source: MediaSource) -> None:
    """Verify a restarted input (new SSRC) does not merge into the old unit."""
    consumer = Consumer()
    source.attach(consumer)
    old = Feeder(ssrc=1).unit([IDR])
    push(source, old[:1])
    push(source, Feeder(ssrc=2, sequence=old[0][3] + 1).unit([TRAIL]))
    assert units_of(consumer) == [[TRAIL]]
    assert consumer.video[0][1] == 2


async def test_pause_keeps_parameter_sets(source: MediaSource) -> None:
    """Verify a pause without receivers keeps the cached parameter sets."""
    consumer = Consumer()
    source.attach(consumer)
    feeder = Feeder()
    push(source, feeder.unit([VPS, SPS, PPS, IDR]))
    consumer.wants_media = False
    push(source, feeder.unit([TRAIL]))
    consumer.wants_media = True
    push(source, feeder.unit([IDR]))
    assert units_of(consumer) == [[VPS, SPS, PPS, IDR], [VPS, SPS, PPS, IDR]]


async def test_audio_is_forwarded(source: MediaSource) -> None:
    """Verify Opus packets reach connected sessions only."""
    connected, waiting = Consumer(), Consumer(wants_media=False)
    source.attach(connected)
    source.attach(waiting)
    packet = rtp.RtpPacket(111, 1, 960, 7, False, b"opus")
    source._on_audio_input(rtp.build_rtp(packet))
    assert connected.audio == [packet]
    assert waiting.audio == []


async def test_close_ends_sessions(source: MediaSource) -> None:
    """Verify closing the source ends attached sessions with the reason."""
    consumer = Consumer()
    reasons: list[str] = []
    source.on_closed = reasons.append
    source.attach(consumer)
    source.detach(Consumer())
    source.close("ffmpeg exited")
    source.close("again")
    assert consumer.closed == ["ffmpeg exited"]
    assert reasons == ["ffmpeg exited"]
    assert source.session_count == 0
    with pytest.raises(RuntimeError, match="closed"):
        source.attach(consumer)
    with pytest.raises(RuntimeError, match="closed"):
        await source.start()


async def test_input_ports_and_buffer(source: MediaSource) -> None:
    """Verify both ports are bound with an enlarged receive buffer."""
    host, video, audio = source.rtp_input
    assert host == "127.0.0.1"
    assert video > 0
    assert audio > 0
    assert await source.start() == source.rtp_input
    sock = source._sockets[0].get_extra_info("socket")
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) >= 400_000


async def test_video_only_source() -> None:
    """Verify a source without audio binds one port."""
    source = MediaSource(audio=False)
    assert (await source.start())[2] == 0
    source.close()


async def test_datagrams_reach_the_source(source: MediaSource) -> None:
    """Verify real UDP input arrives through the bound socket."""
    consumer = Consumer()
    source.attach(consumer)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_all(sender, source.rtp_input[1], Feeder().unit([VPS, SPS, PPS, IDR]))
    await asyncio.wait_for(wait_for_video(consumer), 2)
    sender.close()
    assert units_of(consumer) == [[VPS, SPS, PPS, IDR]]


def send_all(sock: socket.socket, port: int, packets: list[bytes]) -> None:
    """Send datagrams to a loopback port."""
    for packet in packets:
        sock.sendto(packet, ("127.0.0.1", port))


async def wait_for_video(consumer: Consumer) -> None:
    """Wait until an access unit was delivered."""
    while not consumer.video:
        await asyncio.sleep(0.01)


def feed_gop(source: MediaSource, feeder: Feeder, frames: int) -> None:
    """Push one IRAP picture and ``frames - 1`` trailing pictures."""
    push(source, feeder.unit([VPS, SPS, PPS, IDR]))
    for _ in range(frames - 1):
        push(source, feeder.unit([TRAIL]))


async def test_measures_the_irap_interval(source: MediaSource) -> None:
    """Verify the GOP comes from the RTP time between IRAP pictures."""
    feeder = Feeder()
    feed_gop(source, feeder, 60)
    assert source.gop is None
    feed_gop(source, feeder, 30)
    assert source.gop == 2.0
    push(source, feeder.unit([IDR]))
    assert source.gop == 1.75


async def test_irap_interval_restarts_with_a_new_ssrc(source: MediaSource) -> None:
    """Verify a restarted input does not count the gap as a GOP."""
    feed_gop(source, Feeder(ssrc=1), 1)
    feed_gop(source, Feeder(ssrc=2), 1)
    assert source.gop is None


def measure(source: MediaSource, size: int, now: float) -> None:
    """Measure one video packet of ``size`` payload bytes at ``now``."""
    source._measure(rtp.RtpPacket(96, 1, 0, 1, False, bytes([2, 1]) + bytes(size)), now)


async def test_measures_the_video_bitrate(source: MediaSource) -> None:
    """Verify the bit rate is measured per window and smoothed."""
    measure(source, 98, 100.0)
    measure(source, 998, 100.5)
    measure(source, 998, 101.0)
    assert source.video_bitrate == 16000
    measure(source, 3998, 102.0)
    assert source.video_bitrate == 24000


def pinned_protocol() -> tuple[source_module._InputProtocol, list[bytes]]:
    """Return an input protocol and the datagrams it passes on."""
    received: list[bytes] = []
    return source_module._InputProtocol(received.append), received


def test_input_is_pinned_to_one_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a second sender is only taken after the first went silent."""
    clock = iter([10.0, 10.5, 10.9, 12.0, 12.1])
    fake_time = SimpleNamespace(monotonic=lambda: next(clock))
    monkeypatch.setattr(source_module, "time", fake_time)
    protocol, received = pinned_protocol()
    protocol.datagram_received(b"a1", ("127.0.0.1", 1000))
    protocol.datagram_received(b"b1", ("127.0.0.1", 2000))
    protocol.datagram_received(b"a2", ("127.0.0.1", 1000))
    protocol.datagram_received(b"b2", ("127.0.0.1", 2000))
    protocol.datagram_received(b"a3", ("127.0.0.1", 1000))
    assert received == [b"a1", b"a2", b"b2"]
