"""Tests for pyhap.media.sdp."""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
from dataclasses import dataclass
from functools import partial

import pytest

from pyhap.media import sdp
from pyhap.media.sdp import AudioParams, LocalParams, SdpError, VideoParams

from .webrtc_helpers import IceCandidate

FINGERPRINT = ":".join(["AB"] * 32)
LOCAL = LocalParams(
    ice_ufrag="uf01",
    ice_pwd="pw0123456789abcdefghij",
    fingerprint=FINGERPRINT,
    cname="cname01",
    stream_id="stream01",
    video_ssrc=11111,
    audio_ssrc=22222,
    candidates=(
        "candidate:a1 1 udp 2130706431 192.168.1.20 50000 typ host",
        "candidate:b2 1 udp 1694498815 203.0.113.7 61000 typ srflx raddr "
        "192.168.1.20 rport 50000",
    ),
    session_id=42,
)
VIDEO = VideoParams(width=1920, height=1080, fps=30, peak_bitrate_kbps=2000)

GOLDEN_OFFER = """v=0
o=- 42 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0 1
a=msid-semantic: WMS stream01
m=video 50000 UDP/TLS/RTP/SAVPF 99
c=IN IP4 192.168.1.20
a=candidate:a1 1 udp 2130706431 192.168.1.20 50000 typ host
a=candidate:{srflx}
a=end-of-candidates
a=ice-ufrag:uf01
a=ice-pwd:pw0123456789abcdefghij
a=fingerprint:sha-256 {fp}
a=setup:actpass
a=mid:0
a=sendonly
a=rtcp-mux
a=rtcp-rsize
a=rtpmap:99 H265/90000
a=rtcp-fb:99 nack
a=rtcp-fb:99 nack pli
a=rtcp-fb:99 ccm fir
a=rid:1 send max-width=1920;max-height=1080;max-fps=30;max-br=2000000
a=msid:stream01 video
a=ssrc:11111 cname:cname01
a=ssrc:11111 msid:stream01 video
m=audio 50000 UDP/TLS/RTP/SAVPF 110
c=IN IP4 192.168.1.20
a=candidate:a1 1 udp 2130706431 192.168.1.20 50000 typ host
a=candidate:{srflx}
a=end-of-candidates
a=ice-ufrag:uf01
a=ice-pwd:pw0123456789abcdefghij
a=fingerprint:sha-256 {fp}
a=setup:actpass
a=mid:1
a=sendonly
a=rtcp-mux
a=rtcp-rsize
a=rtpmap:110 opus/48000/2
a=fmtp:110 minptime=20;useinbandfec=1;stereo=0;sprop-stereo=0
a=ptime:20
a=msid:stream01 audio
a=ssrc:22222 cname:cname01
a=ssrc:22222 msid:stream01 audio
"""
GOLDEN_OFFER = (
    GOLDEN_OFFER.replace("{fp}", FINGERPRINT)
    .replace("{srflx}", LOCAL.candidates[1].removeprefix("candidate:"))
    .replace("\n", "\r\n")
)

ANSWER = """v=0
o=- 7 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0 1
a=ice-ufrag:remU
a=ice-pwd:remotepassword0123456789
a=fingerprint:sha-256 0a:0b:0c
m=video 9 UDP/TLS/RTP/SAVPF 100
c=IN IP4 0.0.0.0
a=setup:active
a=mid:0
a=recvonly
a=rtcp-mux
a=rtpmap:100 H265/90000
a=rtcp-fb:100 ccm fir
a=candidate:1 1 udp 2122260223 10.0.0.2 52000 typ host
m=audio 9 UDP/TLS/RTP/SAVPF 111
c=IN IP4 0.0.0.0
a=setup:active
a=mid:1
a=sendrecv
a=rtpmap:111 opus/48000/2
a=ssrc:3333 cname:remote
a=ssrc:3333 msid:x y
a=end-of-candidates
"""

REOFFER = """v=0
o=- 9 3 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE v a d
m=video 9 UDP/TLS/RTP/SAVPF 96 98
c=IN IP4 0.0.0.0
a=ice-ufrag:remU
a=ice-pwd:remotepassword0123456789
a=fingerprint:sha-256 0A:0B:0C
a=setup:actpass
a=mid:v
a=recvonly
a=rtpmap:96 H264/90000
a=rtpmap:98 H265/90000
m=audio 9 UDP/TLS/RTP/SAVPF 0 109
c=IN IP4 0.0.0.0
a=mid:a
a=sendrecv
a=rtpmap:0 PCMU/8000
a=rtpmap:109 opus/48000/2
m=application 9 UDP/DTLS/SCTP webrtc-datachannel
c=IN IP4 0.0.0.0
a=mid:d
"""


def offer_with(**changes: object) -> str:
    """Build an offer with modified parameters."""
    local = dataclasses.replace(LOCAL, **changes)  # type: ignore[arg-type]
    return sdp.build_offer(local, VIDEO, AudioParams())


def directions(description: sdp.SessionDescription) -> list[str]:
    """Return each section's direction."""
    return [media.direction for media in description.media]


def ports(description: sdp.SessionDescription) -> list[int]:
    """Return each section's port."""
    return [media.port for media in description.media]


def test_golden_offer() -> None:
    """Verify the full offer text."""
    assert sdp.build_offer(LOCAL, VIDEO, AudioParams()) == GOLDEN_OFFER


def test_offer_without_audio() -> None:
    """Verify a video-only offer bundles one section."""
    parsed = sdp.parse(sdp.build_offer(LOCAL, VIDEO, None))
    assert parsed.bundle == ["0"]
    assert ports(parsed) == [50000]


def test_offer_with_talkback() -> None:
    """Verify talkback makes the audio section sendrecv."""
    offer = sdp.build_offer(LOCAL, VIDEO, AudioParams(talkback=True))
    assert directions(sdp.parse(offer)) == ["sendonly", "sendrecv"]


def test_offer_custom_payload_type() -> None:
    """Verify the payload types are configurable."""
    video = VideoParams(1280, 720, 15, 800, payload_type=100)
    offer = sdp.parse(sdp.build_offer(LOCAL, video, AudioParams(payload_type=111)))
    assert offer.media[0].rtpmap == {100: ("H265", 90000, None)}
    assert offer.media[1].rtpmap == {111: ("opus", 48000, 2)}
    assert offer.media[0].rids == [
        "1 send max-width=1280;max-height=720;max-fps=15;max-br=800000"
    ]


def test_offer_without_candidates_uses_placeholder() -> None:
    """Verify the discard address is used when no candidate is known."""
    assert "m=video 9 UDP/TLS/RTP/SAVPF 99\r\nc=IN IP4 0.0.0.0\r\n" in offer_with(
        candidates=()
    )
    assert "a=end-of-candidates" not in offer_with(candidates=())


def test_offer_ipv6_default_candidate() -> None:
    """Verify an IPv6 default candidate gives an IP6 connection line."""
    offer = offer_with(candidates=("candidate:c 1 udp 1 fd00::5 4000 typ host",))
    assert "m=video 4000 UDP/TLS/RTP/SAVPF 99\r\nc=IN IP6 fd00::5\r\n" in offer


def test_parse_answer() -> None:
    """Verify session-level attributes apply to every section."""
    answer = sdp.parse(ANSWER)
    transport = answer.transport()
    assert (transport.mid, transport.ice_ufrag, transport.ice_pwd) == (
        "0",
        "remU",
        "remotepassword0123456789",
    )
    assert transport.fingerprints == [("sha-256", "0A:0B:0C")]
    assert transport.setup == "active"
    assert answer.candidates == ["1 1 udp 2122260223 10.0.0.2 52000 typ host"]
    assert directions(answer) == ["recvonly", "sendrecv"]
    assert list(answer.media[1].ssrcs) == [3333]
    assert answer.media[1].payload_type_for("opus", 48000, 2) == 111


def test_parse_media_level_overrides_session() -> None:
    """Verify media-level ICE attributes win over session-level ones."""
    text = ANSWER.replace(
        "a=setup:active\na=mid:0", "a=setup:active\na=ice-ufrag:own\na=mid:0"
    )
    assert sdp.parse(text).media[0].ice_ufrag == "own"
    assert sdp.parse(text).media[1].ice_ufrag == "remU"


def test_parse_ice_lite() -> None:
    """Verify a=ice-lite is detected."""
    assert sdp.parse("a=ice-lite\r\n" + ANSWER).ice_lite


def test_transport_skips_rejected_section() -> None:
    """Verify an unbundled answer uses the first accepted section."""
    text = ANSWER.replace("a=group:BUNDLE 0 1\n", "").replace("m=video 9", "m=video 0")
    assert sdp.parse(text).transport().mid == "1"


def test_transport_requires_media() -> None:
    """Verify an SDP without usable media is rejected."""
    with pytest.raises(SdpError, match="no usable"):
        sdp.parse("v=0\r\n").transport()


@pytest.mark.parametrize(
    "text",
    [
        "m=video\r\n",
        "m=video x UDP 96\r\n",
        "m=video 9 UDP 96\r\na=rtpmap:x\r\n",
    ],
    ids=["short", "port", "rtpmap"],
)
def test_parse_rejects_malformed(text: str) -> None:
    """Verify malformed lines raise SdpError."""
    with pytest.raises(SdpError):
        sdp.parse(text)


@pytest.mark.parametrize(
    ("candidate", "section"),
    [
        (IceCandidate("candidate:9 1 udp 1 10.0.0.9 9 typ host", "1", 1), 1),
        (IceCandidate("candidate:9 1 udp 1 10.0.0.9 9 typ host", "0"), 0),
        (IceCandidate("a=candidate:9 1 udp 1 10.0.0.9 9 typ host", None, 1), 1),
    ],
    ids=["mid-and-index", "mid", "index"],
)
def test_merge_candidates(candidate: IceCandidate, section: int) -> None:
    """Verify HAP candidates land in their section before end-of-candidates."""
    merged = sdp.parse(sdp.merge_candidates(ANSWER, [candidate]))
    assert merged.media[section].candidates[-1] == "9 1 udp 1 10.0.0.9 9 typ host"
    assert sdp.merge_candidates(ANSWER, [candidate]).endswith(
        "a=candidate:9 1 udp 1 10.0.0.9 9 typ host\r\na=end-of-candidates\r\n"
    ) is (section == 1)


def test_merge_candidates_single_section_without_mid() -> None:
    """Verify a candidate without mid or index goes to the only section."""
    single = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 100\r\na=mid:0\r\n"
    merged = sdp.merge_candidates(
        single, [IceCandidate("candidate:1 1 udp 1 h 1 typ host")]
    )
    assert sdp.parse(merged).candidates == ["1 1 udp 1 h 1 typ host"]


def test_merge_candidates_deduplicates() -> None:
    """Verify a candidate already in the SDP is not added twice."""
    existing = IceCandidate("candidate:1 1 udp 2122260223 10.0.0.2 52000 typ host", "0")
    assert sdp.merge_candidates(ANSWER, [existing, IceCandidate("")]) == ANSWER.replace(
        "\n", "\r\n"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        IceCandidate("candidate:1 1 udp 1 h 1 typ host", "9"),
        IceCandidate("candidate:1 1 udp 1 h 1 typ host", "0", 1),
        IceCandidate("candidate:1 1 udp 1 h 1 typ host", None, 5),
        IceCandidate("candidate:1 1 udp 1 h 1 typ host"),
        IceCandidate("candidate:1 1 udp 1 h 1 typ host\r\na=x", "0"),
    ],
    ids=["unknown-mid", "mid-index-mismatch", "index-range", "ambiguous", "newline"],
)
def test_merge_candidates_rejects(candidate: IceCandidate) -> None:
    """Verify candidates that match no section are rejected."""
    with pytest.raises(SdpError):
        sdp.merge_candidates(ANSWER, [candidate])


def test_merge_candidates_caps_each_section() -> None:
    """Verify a section takes at most MAX_CANDIDATES candidates."""
    merged = sdp.parse(sdp.merge_candidates(ANSWER, many_candidates(40)))
    assert len(merged.media[0].candidates) == sdp.MAX_CANDIDATES
    assert merged.media[0].candidates[0] == "1 1 udp 2122260223 10.0.0.2 52000 typ host"


def test_merge_candidates_skips_present_before_matching() -> None:
    """Verify a known candidate is skipped before its section is looked up."""
    known = IceCandidate("candidate:1 1 udp 2122260223 10.0.0.2 52000 typ host", "9")
    assert sdp.merge_candidates(ANSWER, [known]) == ANSWER.replace("\n", "\r\n")


def test_merge_candidates_stops_when_full() -> None:
    """Verify candidates after the last free slot are not looked at."""
    unmatched = IceCandidate("candidate:x 1 udp 1 10.3.0.1 9 typ host", "9")
    one_section = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 100\r\na=mid:0\r\n"
    full = [*many_candidates(sdp.MAX_CANDIDATES), unmatched]
    merged = sdp.parse(sdp.merge_candidates(one_section, full))
    assert len(merged.candidates) == sdp.MAX_CANDIDATES


@pytest.mark.parametrize(
    "function",
    [sdp.parse, partial(sdp.merge_candidates, candidates=[])],
    ids=["parse", "merge"],
)
def test_rejects_oversized_sdp(function: Callable[[str], object]) -> None:
    """Verify an SDP over MAX_SDP_SIZE is rejected before it is split."""
    with pytest.raises(SdpError, match="larger than"):
        function(ANSWER + "a=x\n" * sdp.MAX_SDP_SIZE)


def test_parse_accepts_max_media_sections() -> None:
    """Verify an SDP with MAX_MEDIA_SECTIONS media sections is parsed."""
    text = "v=0\r\n" + "m=audio 0 UDP 0\r\n" * sdp.MAX_MEDIA_SECTIONS
    assert len(sdp.parse(text).media) == sdp.MAX_MEDIA_SECTIONS


def test_parse_rejects_too_many_media_sections() -> None:
    """Verify an SDP with more than MAX_MEDIA_SECTIONS sections is rejected."""
    text = "v=0\r\n" + "m=audio 0 UDP 0\r\n" * (sdp.MAX_MEDIA_SECTIONS + 1)
    with pytest.raises(SdpError, match="Too many media sections"):
        sdp.parse(text)


def test_error_shows_a_short_line() -> None:
    """Verify an error message shows at most 80 characters of the line."""
    with pytest.raises(SdpError) as err:
        sdp.parse("m=video 9 UDP 96\r\na=rtpmap:" + "x" * 500 + "\r\n")
    assert str(err.value) == f"Invalid attribute: {('a=rtpmap:' + 'x' * 71)!r}"


def test_parse_caps_candidates() -> None:
    """Verify parsing keeps at most MAX_CANDIDATES per section."""
    text = ANSWER + "".join(candidate_lines(40))
    assert len(sdp.parse(text).media[1].candidates) == sdp.MAX_CANDIDATES
    assert len(sdp.parse(text).candidates) == sdp.MAX_CANDIDATES


def test_video_params_pacing_bitrate() -> None:
    """Verify pacing uses the target bit rate and falls back to the peak."""
    assert VIDEO.pacing_bitrate_kbps == 2000
    assert (
        dataclasses.replace(VIDEO, target_bitrate_kbps=900).pacing_bitrate_kbps == 900
    )


def test_answer_to_reoffer() -> None:
    """Verify a re-offer is answered with its payload types and mids."""
    answer = sdp.build_answer(
        sdp.parse(REOFFER), LOCAL, "passive", VIDEO, AudioParams()
    )
    parsed = sdp.parse(answer.sdp)
    assert (answer.video_payload_type, answer.audio_payload_type) == (98, 109)
    assert (answer.video_mid, answer.audio_mid) == ("v", "a")
    assert answer.audio_direction == "sendonly"
    assert parsed.bundle == ["v", "a"]
    assert ports(parsed) == [50000, 50000, 0]
    assert directions(parsed) == ["sendonly", "sendonly", "inactive"]
    assert parsed.media[0].formats == ["98"]
    assert parsed.media[0].setup == "passive"
    assert parsed.media[0].rids == [sdp.rid_line(VIDEO)[len("a=rid:") :]]


def test_answer_to_reoffer_with_talkback() -> None:
    """Verify talkback accepts the controller's audio."""
    answer = sdp.build_answer(
        sdp.parse(REOFFER), LOCAL, "active", VIDEO, AudioParams(talkback=True)
    )
    assert directions(sdp.parse(answer.sdp))[1] == "sendrecv"
    assert answer.audio_direction == "sendrecv"


def test_answer_to_reoffer_without_audio() -> None:
    """Verify audio is rejected when the sender has none."""
    answer = sdp.build_answer(sdp.parse(REOFFER), LOCAL, "active", VIDEO, None)
    assert answer.audio_payload_type is None
    assert answer.audio_direction is None
    assert ports(sdp.parse(answer.sdp)) == [50000, 0, 0]


def test_answer_requires_h265() -> None:
    """Verify a re-offer without H.265 cannot be answered."""
    with pytest.raises(SdpError, match="H.265"):
        sdp.build_answer(
            sdp.parse(REOFFER.replace("H265", "VP8")), LOCAL, "active", VIDEO, None
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a=candidate:1 1 udp 1 h 1 typ host", "1 1 udp 1 h 1 typ host"),
        ("candidate:1 1 udp 1 h 1 typ host", "1 1 udp 1 h 1 typ host"),
        (" 1 1 udp 1 h 1 typ host ", "1 1 udp 1 h 1 typ host"),
    ],
)
def test_candidate_value(value: str, expected: str) -> None:
    """Verify candidate prefixes are stripped."""
    assert sdp.candidate_value(value) == expected


def many_candidates(count: int) -> list[IceCandidate]:
    """Return ``count`` distinct HAP candidates for mid 0."""
    return [
        IceCandidate(f"candidate:{i} 1 udp 1 10.1.0.{i} 9 typ host", "0")
        for i in range(count)
    ]


def candidate_lines(count: int) -> list[str]:
    """Return ``count`` distinct a=candidate lines."""
    return [f"a=candidate:{i} 1 udp 1 10.2.0.{i} 9 typ host\n" for i in range(count)]


def test_payload_types_ignore_non_ascii_digits() -> None:
    """Verify formats like a superscript two are skipped, not a crash."""
    parsed = sdp.parse("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96 \u00b2\r\n")
    assert parsed.media[0].payload_types == [96]


@dataclass(frozen=True)
class Tier:
    """Stands in for pyhap.hksv3.VideoStreamTier: the fields used."""

    width: int
    height: int
    frame_rate: int
    target_bitrate: int
    peak_bitrate: int | None = None


@pytest.mark.parametrize(
    ("peak", "max_br"), [(3000, 3000), (None, 1200)], ids=["peak", "no-peak"]
)
def test_video_params_from_tier(peak: int | None, max_br: int) -> None:
    """Verify a HAP tier maps to the offer, max-br from the peak bit rate."""
    video = VideoParams.from_tier(Tier(1280, 720, 24, 1200, peak))
    assert video == VideoParams(1280, 720, 24, max_br, 99, 1200)
    assert sdp.rid_line(video).endswith(f"max-fps=24;max-br={max_br * 1000}")
