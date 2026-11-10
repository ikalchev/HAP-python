"""SDP for HomeKit WebRTC remote live view.

The accessory is the offerer. Its offer is sendonly, BUNDLEs one H.265 video
and one Opus audio section, carries all ICE candidates (no trickle) and keeps
``a=setup:actpass`` so the controller picks the DTLS role.

The video section offers H.265 as the only video codec and carries
``a=rid:1 send`` with ``max-width``, ``max-height``, ``max-fps`` and
``max-br`` but no RID RTP header extension. That matches the offer of
Scrypted's HomeKit WebRTC plugin (https://github.com/koush/scrypted,
``plugins/webrtc``), which is known to play remotely.

The offer carries every ICE candidate. A controller may still send its
answer's candidates next to the SDP in HAP (RFC 8839 candidate strings with an
optional mid and m-line index, see :class:`IceCandidateLike`); they are merged
into the answer before it is parsed. At most :data:`MAX_CANDIDATES` candidates
are kept per media section.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol

from .rtp import (
    AUDIO_CLOCK_RATE,
    DEFAULT_AUDIO_PAYLOAD_TYPE,
    DEFAULT_VIDEO_PAYLOAD_TYPE,
    VIDEO_CLOCK_RATE,
)

OPUS_PTIME = 20
VIDEO_MID = "0"
AUDIO_MID = "1"
RID = "1"
DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")
MAX_CANDIDATES = 32
MAX_SDP_SIZE = 32 * 1024
MAX_MEDIA_SECTIONS = 16


class SdpError(ValueError):
    """An SDP could not be parsed or does not fit the session."""


class IceCandidateLike(Protocol):
    """An ICE candidate as HAP carries it next to the SDP.

    ``candidate`` is the RFC 8839 candidate string, with or without the
    ``candidate:`` prefix.
    """

    @property
    def candidate(self) -> str:
        """The candidate string."""

    @property
    def sdp_mid(self) -> str | None:
        """The mid of the media section it belongs to."""

    @property
    def sdp_mline_index(self) -> int | None:
        """The index of the media section it belongs to."""


class VideoTierLike(Protocol):
    """A HAP video stream tier; bit rates in kbps."""

    @property
    def width(self) -> int:
        """Pixels."""

    @property
    def height(self) -> int:
        """Pixels."""

    @property
    def frame_rate(self) -> int:
        """Frames per second."""

    @property
    def target_bitrate(self) -> int:
        """The average bit rate."""

    @property
    def peak_bitrate(self) -> int | None:
        """The peak bit rate, if known."""


@dataclass(frozen=True)
class VideoParams:
    """The video stream tier the offer advertises.

    ``peak_bitrate_kbps`` goes into ``max-br``; ``target_bitrate_kbps`` (the
    average) paces the sender and defaults to the peak.
    """

    width: int
    height: int
    fps: int
    peak_bitrate_kbps: int
    payload_type: int = DEFAULT_VIDEO_PAYLOAD_TYPE
    target_bitrate_kbps: int | None = None

    @classmethod
    def from_tier(
        cls, tier: VideoTierLike, payload_type: int = DEFAULT_VIDEO_PAYLOAD_TYPE
    ) -> VideoParams:
        """Describe a HAP video tier; the peak falls back to the target."""
        return cls(
            width=tier.width,
            height=tier.height,
            fps=tier.frame_rate,
            peak_bitrate_kbps=tier.peak_bitrate or tier.target_bitrate,
            payload_type=payload_type,
            target_bitrate_kbps=tier.target_bitrate,
        )

    @property
    def pacing_bitrate_kbps(self) -> int:
        """The bit rate the pacer is based on."""
        return self.target_bitrate_kbps or self.peak_bitrate_kbps


@dataclass(frozen=True)
class AudioParams:
    """The mono Opus audio stream; ``talkback`` makes the section sendrecv."""

    payload_type: int = DEFAULT_AUDIO_PAYLOAD_TYPE
    talkback: bool = False


@dataclass(frozen=True)
class LocalParams:
    """Transport and stream identifiers of the local side."""

    ice_ufrag: str
    ice_pwd: str
    fingerprint: str
    cname: str
    stream_id: str
    video_ssrc: int
    audio_ssrc: int | None = None
    candidates: Sequence[str] = ()
    session_id: int = 0
    session_version: int = 2


@dataclass
class MediaDescription:
    """One parsed m-section."""

    kind: str
    port: int
    protocol: str
    formats: list[str]
    mid: str | None = None
    direction: str = "sendrecv"
    rtpmap: dict[int, tuple[str, int, int | None]] = field(default_factory=dict)
    fmtp: dict[int, str] = field(default_factory=dict)
    ssrcs: dict[int, None] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    rids: list[str] = field(default_factory=list)
    ice_ufrag: str | None = None
    ice_pwd: str | None = None
    fingerprints: list[tuple[str, str]] = field(default_factory=list)
    setup: str | None = None

    @property
    def payload_types(self) -> list[int]:
        """The numeric formats of an RTP section."""
        return [int(fmt) for fmt in self.formats if fmt.isascii() and fmt.isdigit()]

    def payload_type_for(
        self, codec: str, clock_rate: int, channels: int | None = None
    ) -> int | None:
        """Return the first payload type mapped to a codec, if any."""
        for payload_type in self.payload_types:
            mapping = self.rtpmap.get(payload_type)
            if (
                mapping
                and mapping[0].lower() == codec.lower()
                and mapping[1] == clock_rate
                and (channels is None or (mapping[2] or 1) == channels)
            ):
                return payload_type
        return None


@dataclass
class SessionDescription:
    """A parsed SDP with transport attributes resolved per section."""

    media: list[MediaDescription]
    bundle: list[str] = field(default_factory=list)
    ice_lite: bool = False

    def transport(self) -> MediaDescription:
        """Return the section whose transport the session uses.

        With BUNDLE that is the first section of the group, otherwise the
        first section with a non-zero port.
        """
        for media in self.media:
            if media.port and (not self.bundle or media.mid == self.bundle[0]):
                return media
        raise SdpError("SDP has no usable media section")

    @property
    def candidates(self) -> list[str]:
        """Candidates of the transport sections, without duplicates."""
        seen: dict[str, None] = {}
        bundle = set(self.bundle)
        for media in self.media:
            if media.port and (not bundle or media.mid in bundle):
                seen.update(dict.fromkeys(media.candidates))
        return list(seen)[:MAX_CANDIDATES]


def _checked_size(sdp: str) -> str:
    if len(sdp) > MAX_SDP_SIZE:
        raise SdpError(f"SDP larger than {MAX_SDP_SIZE} characters")
    return sdp


def _split_lines(sdp: str) -> list[str]:
    return [line for line in sdp.replace("\r\n", "\n").split("\n") if line]


@dataclass
class _SessionAttributes:
    ice_ufrag: str | None = None
    ice_pwd: str | None = None
    setup: str | None = None
    fingerprints: list[tuple[str, str]] = field(default_factory=list)


def parse(sdp: str) -> SessionDescription:
    """Parse an SDP; ICE and DTLS attributes fall back to session level.

    An SDP over :data:`MAX_SDP_SIZE` characters or with more than
    :data:`MAX_MEDIA_SECTIONS` media sections is rejected.
    """
    session = _SessionAttributes()
    desc = SessionDescription(media=[])
    current: MediaDescription | None = None
    for line in _split_lines(_checked_size(sdp)):
        if line.startswith("m="):
            if len(desc.media) == MAX_MEDIA_SECTIONS:
                raise SdpError("Too many media sections")
            current = _media_line(line)
            desc.media.append(current)
            continue
        if not line.startswith("a="):
            continue
        name, _, value = line[2:].partition(":")
        try:
            if current is None:
                _session_attribute(desc, session, name, value)
            else:
                _media_attribute(current, name, value)
        except (ValueError, IndexError) as err:
            raise SdpError(f"Invalid attribute: {line[:80]!r}") from err
    for media in desc.media:
        media.ice_ufrag = media.ice_ufrag or session.ice_ufrag
        media.ice_pwd = media.ice_pwd or session.ice_pwd
        media.setup = media.setup or session.setup
        media.fingerprints = media.fingerprints or list(session.fingerprints)
    return desc


def _media_line(line: str) -> MediaDescription:
    parts = line[2:].split()
    try:
        return MediaDescription(
            kind=parts[0],
            port=int(parts[1].split("/")[0]),
            protocol=parts[2],
            formats=parts[3:],
        )
    except (ValueError, IndexError) as err:
        raise SdpError(f"Invalid m-line: {line[:80]!r}") from err


def _fingerprint(value: str) -> tuple[str, str]:
    algorithm, _, digest = value.partition(" ")
    return algorithm.lower(), digest.strip().upper()


def _session_attribute(
    desc: SessionDescription, session: _SessionAttributes, name: str, value: str
) -> None:
    if name == "group" and value.startswith("BUNDLE"):
        desc.bundle = value.split()[1:]
    elif name == "ice-lite":
        desc.ice_lite = True
    elif name == "ice-ufrag":
        session.ice_ufrag = value.strip()
    elif name == "ice-pwd":
        session.ice_pwd = value.strip()
    elif name == "setup":
        session.setup = value.strip()
    elif name == "fingerprint":
        session.fingerprints.append(_fingerprint(value))


def _media_attribute(media: MediaDescription, name: str, value: str) -> None:
    if name == "mid":
        media.mid = value.strip()
    elif name in DIRECTIONS and not value:
        media.direction = name
    elif name == "rtpmap":
        pt, _, codec = value.partition(" ")
        codec_parts = codec.strip().split("/")
        media.rtpmap[int(pt)] = (
            codec_parts[0],
            int(codec_parts[1]) if len(codec_parts) > 1 else 0,
            int(codec_parts[2]) if len(codec_parts) > 2 else None,
        )
    elif name == "fmtp":
        pt, _, params = value.partition(" ")
        media.fmtp[int(pt)] = params.strip()
    elif name == "ssrc":
        media.ssrcs[int(value.split(maxsplit=1)[0])] = None
    elif name == "candidate" and len(media.candidates) < MAX_CANDIDATES:
        media.candidates.append(value.strip())
    elif name == "rid":
        media.rids.append(value.strip())
    elif name == "ice-ufrag":
        media.ice_ufrag = value.strip()
    elif name == "ice-pwd":
        media.ice_pwd = value.strip()
    elif name == "setup":
        media.setup = value.strip()
    elif name == "fingerprint":
        media.fingerprints.append(_fingerprint(value))


def candidate_value(candidate: str) -> str:
    """Strip ``a=`` and ``candidate:`` from a candidate string."""
    value = candidate.strip()
    value = value.removeprefix("a=")
    return value.removeprefix("candidate:")


def merge_candidates(sdp: str, candidates: Iterable[IceCandidateLike]) -> str:
    """Add HAP-signaled candidates to the m-sections they belong to.

    A candidate goes to the section with its mid, else to its m-line index,
    else to the only section. Candidates already present are not repeated,
    and a section takes at most :data:`MAX_CANDIDATES`; once every section is
    full the rest is not looked at.
    """
    lines = _split_lines(_checked_size(sdp))
    starts = [i for i, line in enumerate(lines) if line.startswith("m=")]
    bounds = [*starts, len(lines)]
    media = [lines[start:end] for start, end in pairwise(bounds)]
    mids: dict[str, int] = {}
    counts = [0] * len(media)
    present: set[str] = set()
    for index, section in enumerate(media):
        for line in section:
            if line.startswith("a=mid:"):
                mids.setdefault(line[6:], index)
            elif line.startswith("a=candidate:"):
                present.add(line)
                counts[index] += 1
    room = sum(max(0, MAX_CANDIDATES - count) for count in counts)
    added: list[list[str]] = [[] for _ in media]
    for candidate in candidates:
        if not room:
            break
        if not candidate.candidate:
            continue
        if "\r" in candidate.candidate or "\n" in candidate.candidate:
            raise SdpError("Invalid ICE candidate")
        line = "a=candidate:" + candidate_value(candidate.candidate)
        if line in present:
            continue
        index = _candidate_section(mids, len(media), candidate)
        if counts[index] >= MAX_CANDIDATES:
            continue
        present.add(line)
        counts[index] += 1
        room -= 1
        added[index].append(line)
    for section, lines_added in zip(media, added, strict=True):
        end = (
            section.index("a=end-of-candidates")
            if "a=end-of-candidates" in section
            else len(section)
        )
        section[end:end] = lines_added
    out = lines[: bounds[0]] + [line for section in media for line in section]
    return "\r\n".join(out) + "\r\n"


def _candidate_section(
    mids: dict[str, int], count: int, candidate: IceCandidateLike
) -> int:
    index: int | None
    if candidate.sdp_mid is not None:
        index = mids.get(candidate.sdp_mid)
        if index is None or candidate.sdp_mline_index not in (None, index):
            raise SdpError("ICE candidate does not match a media section")
        return index
    index = candidate.sdp_mline_index
    if index is None and count == 1:
        index = 0
    if index is None or not 0 <= index < count:
        raise SdpError("ICE candidate does not match a media section")
    return index


def _default_address(candidates: Sequence[str]) -> tuple[str, int]:
    """Return the connection address of the first candidate (RFC 8839 4.2.1.2)."""
    for candidate in candidates:
        parts = candidate_value(candidate).split()
        if len(parts) >= 6:
            return parts[4], int(parts[5])
    return "0.0.0.0", 9  # noqa: S104 - SDP placeholder address, not a bind


def _connection_line(address: str) -> str:
    family = "IP6" if ":" in address else "IP4"
    return f"c=IN {family} {address}"


def _transport_lines(local: LocalParams, setup: str) -> list[str]:
    lines = [f"a=candidate:{candidate_value(c)}" for c in local.candidates]
    if local.candidates:
        lines.append("a=end-of-candidates")
    return [
        *lines,
        f"a=ice-ufrag:{local.ice_ufrag}",
        f"a=ice-pwd:{local.ice_pwd}",
        f"a=fingerprint:sha-256 {local.fingerprint}",
        f"a=setup:{setup}",
    ]


def _ssrc_lines(local: LocalParams, ssrc: int, track: str) -> list[str]:
    return [
        f"a=msid:{local.stream_id} {track}",
        f"a=ssrc:{ssrc} cname:{local.cname}",
        f"a=ssrc:{ssrc} msid:{local.stream_id} {track}",
    ]


def rid_line(video: VideoParams) -> str:
    """Return the RID restriction line for the video tier."""
    return (
        f"a=rid:{RID} send max-width={video.width};max-height={video.height};"
        f"max-fps={video.fps};max-br={video.peak_bitrate_kbps * 1000}"
    )


def _video_section(
    local: LocalParams,
    video: VideoParams,
    payload_type: int,
    mid: str,
    direction: str,
    setup: str,
) -> list[str]:
    address, port = _default_address(local.candidates)
    pt = payload_type
    return [
        f"m=video {port} UDP/TLS/RTP/SAVPF {pt}",
        _connection_line(address),
        *_transport_lines(local, setup),
        f"a=mid:{mid}",
        f"a={direction}",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        f"a=rtpmap:{pt} H265/{VIDEO_CLOCK_RATE}",
        f"a=rtcp-fb:{pt} nack",
        f"a=rtcp-fb:{pt} nack pli",
        f"a=rtcp-fb:{pt} ccm fir",
        rid_line(video),
        *_ssrc_lines(local, local.video_ssrc, "video"),
    ]


def _audio_section(
    local: LocalParams, payload_type: int, mid: str, direction: str, setup: str
) -> list[str]:
    address, port = _default_address(local.candidates)
    pt = payload_type
    lines = [
        f"m=audio {port} UDP/TLS/RTP/SAVPF {pt}",
        _connection_line(address),
        *_transport_lines(local, setup),
        f"a=mid:{mid}",
        f"a={direction}",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        f"a=rtpmap:{pt} opus/{AUDIO_CLOCK_RATE}/2",
        f"a=fmtp:{pt} minptime=20;useinbandfec=1;stereo=0;sprop-stereo=0",
        f"a=ptime:{OPUS_PTIME}",
    ]
    if local.audio_ssrc is not None and direction in ("sendonly", "sendrecv"):
        lines += _ssrc_lines(local, local.audio_ssrc, "audio")
    return lines


def _session_lines(local: LocalParams, mids: Sequence[str]) -> list[str]:
    return [
        "v=0",
        f"o=- {local.session_id} {local.session_version} IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        f"a=group:BUNDLE {' '.join(mids)}",
        f"a=msid-semantic: WMS {local.stream_id}",
    ]


def build_offer(
    local: LocalParams, video: VideoParams, audio: AudioParams | None
) -> str:
    """Build the accessory's offer."""
    mids = [VIDEO_MID] + ([AUDIO_MID] if audio else [])
    lines = _session_lines(local, mids)
    lines += _video_section(
        local, video, video.payload_type, VIDEO_MID, "sendonly", "actpass"
    )
    if audio:
        direction = "sendrecv" if audio.talkback else "sendonly"
        lines += _audio_section(
            local, audio.payload_type, AUDIO_MID, direction, "actpass"
        )
    return "\r\n".join(lines) + "\r\n"


_ANSWER_DIRECTION = {
    # (offered direction, we may receive) -> answered direction
    ("sendrecv", True): "sendrecv",
    ("sendrecv", False): "sendonly",
    ("recvonly", True): "sendonly",
    ("recvonly", False): "sendonly",
    ("sendonly", True): "recvonly",
    ("sendonly", False): "inactive",
    ("inactive", True): "inactive",
    ("inactive", False): "inactive",
}


@dataclass(frozen=True)
class Answer:
    """An answer to a controller's offer and the payload types it selected."""

    sdp: str
    video_payload_type: int | None
    audio_payload_type: int | None
    video_mid: str | None
    audio_mid: str | None
    audio_direction: str | None = None


def build_answer(
    offer: SessionDescription,
    local: LocalParams,
    setup: str,
    video: VideoParams,
    audio: AudioParams | None,
) -> Answer:
    """Answer a controller's re-offer with the established transport.

    The first H.265 video section and the first Opus audio section are
    accepted, every other section is rejected with port 0.
    """
    accepted: list[str] = []
    body: list[str] = []
    video_pt = audio_pt = None
    video_mid = audio_mid = audio_direction = None
    for index, media in enumerate(offer.media):
        mid = media.mid if media.mid is not None else str(index)
        pt = None
        if media.kind == "video" and video_pt is None and media.port:
            pt = media.payload_type_for("H265", VIDEO_CLOCK_RATE)
        elif media.kind == "audio" and audio and audio_pt is None and media.port:
            pt = media.payload_type_for("opus", AUDIO_CLOCK_RATE, 2)
        if pt is None:
            body += _rejected_section(media, mid)
            continue
        accepted.append(mid)
        if media.kind == "video":
            video_pt, video_mid = pt, mid
            direction = _ANSWER_DIRECTION[(media.direction, False)]
            body += _video_section(local, video, pt, mid, direction, setup)
        else:
            audio_pt, audio_mid = pt, mid
            talkback = bool(audio and audio.talkback)
            direction = audio_direction = _ANSWER_DIRECTION[(media.direction, talkback)]
            body += _audio_section(local, pt, mid, direction, setup)
    if video_pt is None:
        raise SdpError("Offer has no H.265 video")
    lines = _session_lines(local, accepted) + body
    return Answer(
        "\r\n".join(lines) + "\r\n",
        video_pt,
        audio_pt,
        video_mid,
        audio_mid,
        audio_direction,
    )


def _rejected_section(media: MediaDescription, mid: str) -> list[str]:
    formats = " ".join(media.formats) or "0"
    return [
        f"m={media.kind} 0 {media.protocol} {formats}",
        "c=IN IP4 0.0.0.0",
        f"a=mid:{mid}",
        "a=inactive",
    ]
