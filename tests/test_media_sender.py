"""Loopback tests for pyhap.media.sender.

A test controller built from the same transport answers the sender's offer,
connects over ICE and DTLS on 127.0.0.1, and decrypts what arrives: SRTP,
then the SFrame descriptor reassembly, then SFrame with the advertised key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
import time
from typing import Any

import pytest

from pyhap.media import (  # noqa: E402
    AudioParams,
    MediaError,
    VideoParams,
    WebRTCSender,
    rtp,
    sdp,
    sender as sender_module,  # noqa: E402
)
from pyhap.media.sender import _Pacer  # noqa: E402
from pyhap.media.transport import DtlsSrtpTransport  # noqa: E402

from .media_peer import (  # noqa: E402
    AUDIO_PT,
    CONTROLLER_SSRC,
    IDR,
    KEY,
    KEYFRAME,
    LEADING_TRAIL,
    PPS,
    SPS,
    TALKBACK_KEY,
    TRAIL,
    VIDEO_PT,
    VPS,
    Controller,
    Received,
    Source,
    audio_count,
    make_sender,
    nal,
    negotiate,
    video_count,
    wait_until,
)
from .webrtc_helpers import SFrameKey  # noqa: E402

pytestmark = pytest.mark.timeout(5)


@dataclass
class Session:
    """A connected sender, controller and source."""

    sender: WebRTCSender
    controller: Controller
    source: Source
    keyframe_requests: list[float | None]
    closed: list[str]


async def open_session(setup: str = "active", **kwargs: Any) -> Session:
    """Connect a sender with talkback to a controller."""
    sender = make_sender(audio=AudioParams(talkback=True), **kwargs)
    controller = Controller(setup=setup, talkback=True)
    session = Session(sender, controller, Source(("127.0.0.1", 0, 0)), [], [])
    sender.on_keyframe_request = lambda age, _gop: session.keyframe_requests.append(age)
    sender.on_closed = session.closed.append
    await negotiate(sender, controller)
    session.source.close()
    session.source = Source(sender.rtp_input)
    return session


async def close_session(session: Session) -> None:
    """Close everything a session opened."""
    session.source.close()
    await session.sender.close()
    await session.controller.close()


@pytest.fixture(params=["active", "passive"])
async def session(request: pytest.FixtureRequest) -> AsyncIterator[Session]:
    """Yield a connected session for both DTLS role assignments."""
    opened = await open_session(request.param)
    yield opened
    await close_session(opened)


def rtcp_type(packet_type: int) -> Callable[[Received], bool]:
    """Predicate: an RTCP packet of the given type arrived."""
    return lambda received: any(m.packet_type == packet_type for m in received.rtcp)


def stream_packets(received: Received, payload_type: int) -> list[rtp.RtpPacket]:
    """Return the received packets of one payload type."""
    return [p for p in received.packets if p.payload_type == payload_type]


def sequences_consecutive(packets: list[rtp.RtpPacket]) -> bool:
    """Whether RTP sequence numbers increase by one, wrapping at 65536."""
    return all(
        b.sequence == (a.sequence + 1) & 0xFFFF
        for a, b in zip(packets, packets[1:], strict=False)
    )


def ssrcs(packets: list[rtp.RtpPacket]) -> set[int]:
    """Return the set of SSRCs."""
    return {p.ssrc for p in packets}


def stream_ssrcs(received: Received) -> list[set[int]]:
    """Return the SSRCs seen for video and audio."""
    return [
        ssrcs(stream_packets(received, VIDEO_PT)),
        ssrcs(stream_packets(received, AUDIO_PT)),
    ]


def announced_ssrcs(controller: Controller) -> list[set[int]]:
    """Return the SSRCs the offer announced for video and audio."""
    assert controller.offer is not None
    return [set(media.ssrcs) for media in controller.offer.media]


def frame_timestamps(packets: list[rtp.RtpPacket]) -> list[int]:
    """Return the timestamps of the packets that end a frame."""
    return [p.timestamp for p in packets if p.marker]


def deltas(values: list[int]) -> list[int]:
    """Return the differences between neighbours, modulo 2**32."""
    return [(b - a) & 0xFFFFFFFF for a, b in zip(values, values[1:], strict=False)]


def largest_payload(packets: list[rtp.RtpPacket]) -> int:
    """Return the largest payload size."""
    return max(len(p.payload) for p in packets)


def packet_received(packet: rtp.RtpPacket) -> Callable[[Received], bool]:
    """Predicate: this exact RTP packet arrived."""
    return lambda received: packet in received.packets


def payload_type_seen(payload_type: int) -> Callable[[Received], bool]:
    """Predicate: an RTP packet with this payload type arrived."""
    return lambda received: bool(stream_packets(received, payload_type))


def send_frames(source: Source, frames: list[list[bytes]]) -> None:
    """Send several access units."""
    for frame in frames:
        source.send_video(frame)


async def test_video_and_audio_end_to_end(session: Session) -> None:
    """Verify frames arrive SRTP- and SFrame-decrypted, unchanged."""
    send_frames(session.source, [[LEADING_TRAIL], KEYFRAME, [TRAIL], [IDR]])
    session.source.send_audio(b"opus-1")
    session.source.send_audio(b"opus-2")
    await session.controller.wait_for(video_count(3))
    await session.controller.wait_for(audio_count(2))
    received = session.controller.received
    # The P-frame before the first keyframe is held back; cached parameter
    # sets are put in front of the IDR that arrived without them.
    assert received.video == [KEYFRAME, [TRAIL], KEYFRAME]
    assert received.audio == [b"opus-1", b"opus-2"]
    video = stream_packets(received, VIDEO_PT)
    assert stream_ssrcs(received) == announced_ssrcs(session.controller)
    assert sequences_consecutive(video)
    assert deltas(frame_timestamps(video)) == [3000, 3000]
    assert largest_payload(video) == 1200
    assert session.keyframe_requests == [None]


async def test_irap_picture_over_64_kib() -> None:
    """Verify an IRAP picture the size of a 4K one arrives whole."""
    session = await open_session(
        video=VideoParams(width=3840, height=2160, fps=30, peak_bitrate_kbps=20000)
    )
    keyframe = [VPS, SPS, PPS, nal(19, 200_000, 4)]
    session.source.send_video(keyframe)
    await session.controller.wait_for(video_count(1))
    assert session.controller.received.video == [keyframe]
    await close_session(session)


async def test_sender_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify SR and SDES flow once media was sent."""
    monkeypatch.setattr(sender_module, "RTCP_INTERVAL", 0.05)
    session = await open_session()
    session.source.send_video(KEYFRAME)
    await session.controller.wait_for(video_count(1))
    await session.controller.wait_for(rtcp_type(rtp.RTCP_SDES))
    assert rtcp_type(rtp.RTCP_SR)(session.controller.received)
    await close_session(session)


async def test_keyframe_request(session: Session) -> None:
    """Verify a PLI reaches the integrator with the keyframe age, rate limited."""
    session.source.send_video(KEYFRAME)
    await session.controller.wait_for(video_count(1))
    ssrc = stream_packets(session.controller.received, VIDEO_PT)[0].ssrc
    session.sender._last_keyframe_request = 0
    await session.controller.transport.send_rtcp(rtp.build_pli(4242, ssrc))
    await session.controller.transport.send_rtcp(rtp.build_pli(4242, ssrc))
    await asyncio.wait_for(wait_until(lambda: len(session.keyframe_requests) > 1), 3)
    await asyncio.sleep(0.1)
    assert len(session.keyframe_requests) == 2
    assert recent(session.keyframe_requests[1])


def recent(age: float | None) -> bool:
    """Whether a keyframe age says a keyframe started in the last seconds."""
    return age is not None and 0 <= age < 3


async def test_nack_retransmits_lost_packet(session: Session) -> None:
    """Verify a NACKed packet is sent again, byte for byte."""
    lost = drop_next_video_packet(session.sender)
    session.source.send_video(KEYFRAME)
    packet = rtp.parse_rtp(await asyncio.wait_for(lost, 3))
    await session.controller.transport.send_rtcp(
        rtp.build_nack(4242, packet.ssrc, [packet.sequence])
    )
    await session.controller.wait_for(packet_received(packet))


def drop_next_video_packet(sender: WebRTCSender) -> asyncio.Future[bytes]:
    """Lose the next video packet on the wire, as a network would."""
    transport = sender._transport
    original = transport.send_rtp
    lost: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def send_rtp(packet: bytes) -> None:
        if not lost.done() and packet[1] & 0x7F == VIDEO_PT:
            lost.set_result(packet)
            return
        await original(packet)

    transport.send_rtp = send_rtp  # type: ignore[method-assign]
    return lost


def test_nack_retransmits_are_capped() -> None:
    """Verify one NACK and one second of NACKs only retransmit so much."""
    sender = make_sender(audio=None)
    fill_history(sender, 300)
    nack = rtp.RtcpMessage(rtp.RTCP_RTPFB, 1, 1, [sender._video.ssrc], all_sent(300))
    sender._retransmit(nack)
    assert len(sender._urgent) == sender_module.MAX_NACK_PER_MESSAGE
    retransmit_times(sender, nack, 5)
    assert len(sender._urgent) == sender_module.MAX_RETRANSMITS_PER_SECOND


def test_urgent_queue_is_bounded() -> None:
    """Verify audio and retransmissions waiting on a blocked path are capped."""
    sender = make_sender(audio=None)
    for index in range(sender_module.MAX_URGENT_PACKETS + 10):
        sender._urgent.append(bytes([index % 256]))
    assert len(sender._urgent) == sender_module.MAX_URGENT_PACKETS
    # The oldest went first.
    assert sender._urgent[0] == bytes([10])


def fill_history(sender: WebRTCSender, count: int) -> None:
    """Pretend ``count`` video packets were sent, from sequence 0."""
    sender._video.sequence = 0
    for _ in range(count):
        sender._video.build(0, False, b"x")


def all_sent(count: int) -> list[int]:
    """Sequence numbers 0 to ``count - 1``."""
    return list(range(count))


def retransmit_times(sender: WebRTCSender, nack: rtp.RtcpMessage, times: int) -> None:
    """Handle the same NACK several times."""
    for _ in range(times):
        sender._retransmit(nack)


async def test_talkback(session: Session) -> None:
    """Verify controller audio is SFrame-decrypted with the receive key."""
    heard: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue()
    session.sender.on_audio = lambda opus, ts: heard.put_nowait((opus, ts))
    await session.sender.update_receive_keys([TALKBACK_KEY], [])
    await session.controller.send_talkback(b"hello", 3)
    await session.controller.send_talkback(b"hello", 3)
    await session.controller.send_talkback(b"world", 4)
    assert await asyncio.wait_for(heard.get(), 3) == (b"hello", 2880)
    assert await asyncio.wait_for(heard.get(), 3) == (b"world", 3840)


async def test_talkback_without_keys_is_dropped(session: Session) -> None:
    """Verify talkback is never passed on in plaintext before a key exists."""
    heard: list[bytes] = []
    session.sender.on_audio = lambda opus, ts: heard.append(opus)
    await session.controller.send_audio_payload(b"\xc0plain", 1)
    await session.sender.update_receive_keys([TALKBACK_KEY], [])
    await session.controller.send_talkback(b"secret", 2)
    await asyncio.wait_for(wait_until(lambda: bool(heard)), 3)
    assert heard == [b"secret"]


async def test_talkback_streams_are_capped(session: Session) -> None:
    """Verify reassembly state is kept for a bounded number of SSRCs."""
    session.sender.on_audio = lambda opus, ts: None
    await session.sender.update_receive_keys([TALKBACK_KEY], [])
    feed_talkback_ssrcs(session.sender, 10)
    assert len(session.sender._talkback) == sender_module.MAX_TALKBACK_STREAMS


def feed_talkback_ssrcs(sender: WebRTCSender, count: int) -> None:
    """Hand the sender one talkback packet from each of ``count`` SSRCs."""
    for ssrc in range(count):
        packet = rtp.RtpPacket(AUDIO_PT, 1, 960, ssrc, False, b"\x80x")
        sender._on_remote_rtp(rtp.build_rtp(packet))


async def test_update_receive_keys_validates() -> None:
    """Verify an invalid receive key is refused and changes nothing."""
    sender = make_sender()
    await sender.update_receive_keys([TALKBACK_KEY])
    with pytest.raises(MediaError, match="32 bytes"):
        await sender.update_receive_keys([SFrameKey(bytes(5), 1)], [TALKBACK_KEY.kid])
    assert sender._receiver.has_keys


async def test_controller_close_ends_session(session: Session) -> None:
    """Verify a DTLS close from the controller ends the session."""
    await session.controller.close()
    await asyncio.wait_for(wait_until(lambda: bool(session.closed)), 3)
    assert session.closed == ["dtls closed by remote"]
    assert not session.sender.connected


async def test_remote_bye_keeps_session(session: Session) -> None:
    """Verify an RTCP BYE (talkback stopping) does not end the session."""
    await session.controller.transport.send_rtcp(rtp.build_bye([CONTROLLER_SSRC]))
    session.source.send_video(KEYFRAME)
    await session.controller.wait_for(video_count(1))
    assert session.sender.connected
    assert session.closed == []


async def test_close_sends_bye(session: Session) -> None:
    """Verify close() says goodbye and does not report its own close."""
    await session.sender.close()
    await session.controller.wait_for(rtcp_type(rtp.RTCP_BYE))
    assert session.closed == []
    with pytest.raises(MediaError, match="closed"):
        await session.sender.wait_connected(1)


async def test_source_end_closes_session(session: Session) -> None:
    """Verify the session ends at once when the media source ends."""
    assert session.sender.source is not None
    session.sender.source.close("ffmpeg exited")
    await asyncio.wait_for(wait_until(lambda: bool(session.closed)), 1)
    assert session.closed == ["media source ended: ffmpeg exited"]


async def test_reoffer(session: Session) -> None:
    """Verify a re-offer is answered on the running transport."""
    answer = sdp.parse(await session.sender.reoffer(reoffer_sdp(session.controller)))
    assert answer.bundle == ["0", "1"]
    assert answer.media[0].setup == session.sender.dtls_role.setup
    assert answer.media[0].formats == ["102"]
    session.source.send_video(KEYFRAME)
    await session.controller.wait_for(payload_type_seen(102))
    session.source.send_audio(b"opus-1")
    await session.controller.wait_for(audio_count(1))


async def test_reoffer_rejects_too_many_sections(session: Session) -> None:
    """Verify a re-offer with too many media sections is refused."""
    rejected = "m=audio 0 UDP/TLS/RTP/SAVPF 0\r\na=inactive\r\n"
    offer = reoffer_sdp(session.controller) + rejected * sdp.MAX_MEDIA_SECTIONS
    with pytest.raises(MediaError, match="Too many media sections"):
        await session.sender.reoffer(offer)


async def test_reoffer_rejects_ice_restart(session: Session) -> None:
    """Verify an ICE restart in a re-offer is refused."""
    with pytest.raises(MediaError, match="ICE restart"):
        await session.sender.reoffer(reoffer_sdp(session.controller, ufrag="new1"))


async def test_reoffer_rejects_new_certificate(session: Session) -> None:
    """Verify a certificate change in a re-offer is refused."""
    offer = reoffer_sdp(session.controller).replace(
        session.controller.transport.certificate.fingerprint, ":".join(["00"] * 32)
    )
    with pytest.raises(MediaError, match="certificate"):
        await session.sender.reoffer(offer)


async def test_media_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the session ends when the source stops sending."""
    monkeypatch.setattr(sender_module, "RTCP_INTERVAL", 0.05)
    sender = make_sender(idle_timeout=0.2)
    closed: asyncio.Queue[str] = asyncio.Queue()
    sender.on_closed = closed.put_nowait
    controller = Controller()
    await negotiate(sender, controller)
    assert await asyncio.wait_for(closed.get(), 5) == "media input idle"
    await controller.close()


async def test_offer_timeout() -> None:
    """Verify an offer that is never answered expires."""
    sender = make_sender(offer_timeout=0.2)
    closed: asyncio.Queue[str] = asyncio.Queue()
    sender.on_closed = closed.put_nowait
    await sender.create_offer()
    assert sender.rtp_input[1] > 0
    assert await asyncio.wait_for(closed.get(), 3) == "offer not answered"
    assert sender.source is not None
    assert sender.source.closed


async def test_negotiation_timeout_counts_from_answer() -> None:
    """Verify ICE and DTLS get their time from the answer, not the offer."""
    sender = make_sender(offer_timeout=5, negotiation_timeout=0.3)
    closed: asyncio.Queue[str] = asyncio.Queue()
    sender.on_closed = closed.put_nowait
    controller = Controller()
    offer = await sender.create_offer()
    await asyncio.sleep(0.4)
    answer, candidates = await controller.answer(offer)
    started = time.monotonic()
    await sender.apply_answer(answer, candidates)
    assert "timed out" in await asyncio.wait_for(closed.get(), 3)
    assert time.monotonic() - started >= 0.25
    await controller.close()


async def test_close_during_create_offer() -> None:
    """Verify closing while the offer is made leaks no socket or task."""
    sender = make_sender()
    ice = sender._transport._ice
    gather = ice.gather
    gathering = asyncio.Event()

    async def slow_gather(addresses: Iterable[str]) -> list[Any]:
        gathering.set()
        await asyncio.sleep(0.2)
        return await gather(addresses)

    ice.gather = slow_gather  # type: ignore[method-assign]
    offering = asyncio.ensure_future(sender.create_offer())
    await gathering.wait()
    await sender.close()
    with pytest.raises(MediaError, match="closed"):
        await offering
    assert sender.source is not None
    assert sender.source.closed
    assert ice._sockets == []
    assert sender._tasks == set()


async def test_offer_contents() -> None:
    """Verify the offer, candidates and input ports of a video-only sender."""
    sender = make_sender(audio=None)
    parsed = sdp.parse(await sender.create_offer())
    assert parsed.bundle == ["0"]
    assert len(parsed.candidates) == 1
    assert parsed.media[0].payload_types == [VIDEO_PT]
    assert sender.rtp_input[0] == "127.0.0.1"
    assert sender.rtp_input[2] == 0
    assert sender.sframe == (KEY.key, KEY.kid)
    with pytest.raises(MediaError, match="already"):
        await sender.create_offer()
    await sender.close()
    await sender.close()


async def test_answer_before_offer() -> None:
    """Verify an answer without an offer is refused."""
    with pytest.raises(MediaError, match="No offer"):
        await make_sender().apply_answer("v=0\r\n")


async def test_reoffer_before_answer() -> None:
    """Verify a re-offer before negotiation is refused."""
    with pytest.raises(MediaError, match="not negotiated"):
        await make_sender().reoffer("v=0\r\n")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("a=fingerprint:sha-256", "a=x-fingerprint:sha-256"), "fingerprint"),
        (("m=video 9", "m=video 0"), "rejected the video"),
        (("H265/90000", "VP8/90000"), "H.265"),
    ],
    ids=["fingerprint", "rejected", "codec"],
)
async def test_answer_rejected(change: tuple[str, str], message: str) -> None:
    """Verify unusable answers are refused."""
    sender = make_sender()
    controller = Controller()
    answer, candidates = await controller.answer(await sender.create_offer())
    with pytest.raises(MediaError, match=message):
        await sender.apply_answer(answer.replace(*change), candidates)
    await sender.close()
    await controller.close()


async def test_shared_source_fans_out(session: Session) -> None:
    """Verify two sessions on one source each get every frame."""
    assert session.sender.source is not None
    second = make_sender(source=session.sender.source)
    controller = Controller()
    await negotiate(second, controller)
    send_frames(session.source, [KEYFRAME, [TRAIL]])
    await session.controller.wait_for(video_count(2))
    await controller.wait_for(video_count(2))
    assert controller.received.video == session.controller.received.video
    assert session.sender.source.session_count == 2
    await second.close()
    await controller.close()
    assert session.sender.source.session_count == 1


async def test_pacing_spreads_a_keyframe(session: Session) -> None:
    """Verify a large keyframe goes out over time, not in one burst."""
    session.source.send_video(KEYFRAME)
    await session.controller.wait_for(video_count(1))
    # 40 kB at 2.5 x 1.5 Mbit/s takes about 85 ms.
    assert arrival_spread(session.controller.received) > 0.04


def arrival_spread(received: Received) -> float:
    """Seconds between the first and the last video packet of the first frame."""
    times = received.arrivals
    return times[-1] - times[0]


async def test_congestion_drops_until_keyframe(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify video waiting too long on a full socket is dropped."""
    monkeypatch.setattr(sender_module, "MAX_WRITE_BUFFER", -1)
    session.sender._keyframe_request_interval = 0
    send_frames(session.source, [KEYFRAME, [TRAIL]])
    await asyncio.wait_for(wait_until(lambda: len(session.keyframe_requests) > 1), 3)
    assert session.sender._waiting_for_keyframe
    assert not session.sender._pacer.video
    monkeypatch.setattr(sender_module, "MAX_WRITE_BUFFER", 1 << 30)
    send_frames(session.source, [[TRAIL], KEYFRAME])
    await session.controller.wait_for(video_count(1))
    assert session.controller.received.video == [KEYFRAME]


@pytest.mark.parametrize(
    ("queued", "age", "budget"),
    [(0, 0.0, 12_500), (10_000, 0.0, 12_500), (1_000_000, 0.3, 100_000)],
    ids=["idle", "below-limit", "catch-up"],
)
def test_pacer_budget(queued: int, age: float, budget: int) -> None:
    """Verify the token bucket and the faster rate when the queue would lag."""
    pacer = _Pacer(10_000_000, 1.0, 0.4)
    pacer._last = 0.0
    pacer.queued_bytes = queued
    stream = rtp.OutboundStream(VIDEO_PT, sdp.VIDEO_CLOCK_RATE)
    pacer.video.append(sender_module._Pending(stream, 0, False, b"", 1.0 - age))
    assert pacer.budget(1.0) == pytest.approx(budget)


@pytest.mark.parametrize(
    ("input_bitrate", "rate"),
    [(0, 2_500_000), (4_000_000, 2_500_000), (16_000_000, 5_000_000)],
    ids=["unknown", "below-tier", "above-tier"],
)
def test_pacer_follows_the_input_rate(input_bitrate: float, rate: float) -> None:
    """Verify the base rate is the larger of the tier and the input rate."""
    pacer = _Pacer(8_000_000, 2.5, 0.4)
    pacer.follow(input_bitrate)
    assert pacer.bytes_per_second == rate


@pytest.mark.parametrize(
    ("spent", "wait"),
    [(0, sender_module.PACING_INTERVAL), (12_500, 0.01)],
    ids=["minimum", "until-tokens"],
)
def test_pacer_waits_until_the_next_packet(spent: int, wait: float) -> None:
    """Verify the pacer sleeps until the bucket is positive, one step at least."""
    pacer = _Pacer(10_000_000, 1.0, 0.4)
    pacer._last = 0.0
    pacer.budget(0.0)
    pacer.spend(spent)
    assert pacer.wait() == pytest.approx(wait)


async def test_stale_video_is_dropped(session: Session) -> None:
    """Verify video queued longer than max_queue_delay goes, even unblocked."""
    sender = session.sender
    sender._keyframe_request_interval = 0
    sender._waiting_for_keyframe = False
    stale = time.monotonic() - 1
    sender._pacer.add(sender_module._Pending(sender._video, 0, True, b"x", stale))
    assert await sender._send_ready() == 0
    assert sender._waiting_for_keyframe
    assert len(session.keyframe_requests) == 2


def reoffer_sdp(controller: Controller, ufrag: str | None = None) -> str:
    """Build a controller re-offer moving H.265 to payload type 102."""
    return (
        "\r\n".join(
            [
                "v=0",
                "o=- 1 3 IN IP4 127.0.0.1",
                "s=-",
                "t=0 0",
                "a=group:BUNDLE 0 1",
                f"a=ice-ufrag:{ufrag or controller.transport.local_username}",
                f"a=ice-pwd:{controller.transport.local_password}",
                f"a=fingerprint:sha-256 {controller.transport.certificate.fingerprint}",
                "a=setup:actpass",
                "m=video 9 UDP/TLS/RTP/SAVPF 102",
                "a=mid:0",
                "a=recvonly",
                "a=rtpmap:102 H265/90000",
                f"m=audio 9 UDP/TLS/RTP/SAVPF {AUDIO_PT}",
                "a=mid:1",
                "a=recvonly",
                f"a=rtpmap:{AUDIO_PT} opus/48000/2",
            ]
        )
        + "\r\n"
    )


async def test_close_waits_for_a_close_under_way(session: Session) -> None:
    """Verify close() returns only once a session ending on its own is done."""
    session.sender.on_source_closed("ffmpeg exited")
    await asyncio.sleep(0)
    await session.sender.close()
    assert session.sender._transport.closed
    assert session.closed == ["media source ended: ffmpeg exited"]


async def test_unexpected_connect_error_ends_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a bug while connecting ends the session at once, with the error."""
    sender = make_sender()
    closed: asyncio.Queue[str] = asyncio.Queue()
    sender.on_closed = closed.put_nowait
    monkeypatch.setattr(DtlsSrtpTransport, "connect", broken_connect)
    controller = Controller()
    answer, candidates = await controller.answer(await sender.create_offer())
    await sender.apply_answer(answer, candidates)
    assert "boom" in await asyncio.wait_for(closed.get(), 2)
    await controller.close()


async def broken_connect(*_args: object, **_kwargs: object) -> None:
    """Fail like an unexpected bug."""
    raise RuntimeError("boom")


async def test_session_without_sframe() -> None:
    """Verify a session without SFrame sends plain H.265 and Opus RTP."""
    sender = make_sender(sframe=None, audio=AudioParams(talkback=True))
    heard: list[tuple[bytes, int]] = []
    sender.on_audio = lambda opus, ts: heard.append((opus, ts))
    controller = Controller(talkback=True, sframe=False)
    await negotiate(sender, controller)
    source = Source(sender.rtp_input)
    send_frames(source, [[TRAIL], KEYFRAME, [TRAIL]])
    source.send_audio(b"opus")
    await controller.wait_for(video_count(2))
    await controller.wait_for(audio_count(1))
    assert controller.received.video == [KEYFRAME, [TRAIL]]
    assert controller.received.audio == [b"opus"]
    assert sender.sframe is None
    await controller.send_audio_payload(b"plain talkback", 2)
    await asyncio.wait_for(wait_until(lambda: bool(heard)), 3)
    assert heard == [(b"plain talkback", 1920)]
    with pytest.raises(MediaError, match="SFrame"):
        await sender.update_receive_keys([TALKBACK_KEY])
    source.close()
    await sender.close()
    await controller.close()
