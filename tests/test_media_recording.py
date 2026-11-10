"""Tests for recordings from a media source with a prebuffer."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

import pytest

from pyhap import hksv_recording as hr
from pyhap.hds_recording import fmp4_recording_packets
from pyhap.media import mpegts, recording as recording_module, rtp
from pyhap.media.hevc import AccessUnit
from pyhap.media.recording import (
    PTS_START,
    PrebufferRing,
    Recorder,
    _Timestamps,
    recording_ffmpeg_args,
)
from pyhap.media.source import MediaSource

from .media_peer import KEYFRAME, TRAIL, Source, wait_until

FFMPEG = shutil.which("ffmpeg")
# Copies stdin to stdout, standing in for the transcoder.
CAT = [
    sys.executable,
    "-c",
    "import os\nwhile data := os.read(0, 65536):\n    os.write(1, data)",
]


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def unit(keyframe: bool, timestamp: int = 0) -> AccessUnit:
    """An access unit of the test NAL units."""
    return AccessUnit(timestamp, list(KEYFRAME) if keyframe else [TRAIL])


def audio(timestamp: int = 0) -> rtp.RtpPacket:
    """An Opus RTP packet."""
    return rtp.RtpPacket(97, 1, timestamp, 0xBBBB, False, b"\x78" * 40)


def selected(
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    profile: int = 1,
    level: int = 2,
    sample_rate: hr.AudioSampleRate = hr.AudioSampleRate.KHZ_32,
) -> hr.SelectedRecordingConfiguration:
    """A selected recording configuration as a hub writes one."""
    return hr.SelectedRecordingConfiguration(
        recording=hr.SupportedRecordingConfiguration(prebuffer_length_ms=4000),
        video=hr.VideoCodecConfiguration(
            hr.VideoCodecType.H264,
            profile,
            level,
            2000,
            4000,
            [hr.VideoAttributes(width, height, fps)],
        ),
        audio=hr.AudioCodecConfiguration(
            sample_rate=sample_rate, max_audio_bitrate_kbps=64
        ),
    )


def ts_packets(data: bytes) -> list[bytes]:
    """Split MPEG-TS into its packets."""
    assert len(data) % mpegts.PACKET_SIZE == 0
    packets = [
        data[i : i + mpegts.PACKET_SIZE]
        for i in range(0, len(data), mpegts.PACKET_SIZE)
    ]
    assert all(packet[0] == 0x47 for packet in packets)
    return packets


def pes_payloads(data: bytes, pid: int) -> list[bytes]:
    """The PES packets of a PID, reassembled."""
    pes: list[bytearray] = []
    for packet in ts_packets(data):
        if ((packet[1] & 0x1F) << 8 | packet[2]) != pid:
            continue
        offset = 4
        if packet[3] & 0x20:
            offset += 1 + packet[4]
        if packet[1] & 0x40:
            pes.append(bytearray())
        pes[-1] += packet[offset:]
    return [bytes(p) for p in pes]


def test_crc32_mpeg2() -> None:
    """Verify the PSI CRC with the standard check value."""
    assert mpegts.crc32_mpeg2(b"123456789") == 0x0376E6E7


@pytest.mark.parametrize("pts", [0, 90000, 1 << 32, (1 << 33) - 1])
def test_pts_round_trip(pts: int) -> None:
    """Verify the 33-bit PTS encoding."""
    assert mpegts.decode_pts(mpegts.encode_pts(pts)) == pts


def test_opus_control_header() -> None:
    """Verify the Opus size bytes of the control header."""
    assert mpegts.opus_control_header(100) == b"\x7f\xe0\x64"
    assert mpegts.opus_control_header(300) == b"\x7f\xe0\xff\x2d"


AUD = bytes([mpegts.NAL_AUD << 1, 0x01, 0x50])


def test_video_pes_carries_annex_b_and_pts() -> None:
    """Verify a keyframe goes with the tables, start codes and its PTS."""
    muxer = mpegts.MpegTsMuxer()
    data = muxer.video(KEYFRAME, 123456, keyframe=True)
    pids = [(p[1] & 0x1F) << 8 | p[2] for p in ts_packets(data)]
    assert pids[:2] == [mpegts.PAT_PID, mpegts.PMT_PID]
    (pes,) = pes_payloads(data, mpegts.VIDEO_PID)
    assert pes[:4] == b"\x00\x00\x01\xe0"
    assert mpegts.decode_pts(pes[9:14]) == 123456
    # An access unit delimiter first, then the unit; the PES is aligned.
    assert pes[6] == 0x84
    aud = mpegts.START_CODE + AUD
    assert pes[14:].startswith(aud + mpegts.START_CODE + KEYFRAME[0])
    # Stuffing goes into the adaptation field, the payload ends with the NAL.
    assert pes.endswith(KEYFRAME[-1])
    assert not muxer.video([TRAIL], 0, keyframe=False).startswith(data[:188])


def test_video_keeps_an_existing_delimiter() -> None:
    """Verify a unit that has an access unit delimiter gets no second one."""
    data = mpegts.MpegTsMuxer().video([AUD, TRAIL], 0, keyframe=False)
    (pes,) = pes_payloads(data, mpegts.VIDEO_PID)
    assert pes[14:].count(AUD) == 1


def test_video_moves_a_later_delimiter_to_the_front() -> None:
    """Verify a delimiter behind parameter sets is moved, not doubled."""
    data = mpegts.MpegTsMuxer().video([*KEYFRAME[:3], AUD, KEYFRAME[3]], 0, True)
    (pes,) = pes_payloads(data, mpegts.VIDEO_PID)
    assert pes[14:].count(AUD) == 1
    assert pes[14:].startswith(mpegts.START_CODE + AUD)


def test_video_delimiter_has_the_temporal_id_of_the_unit() -> None:
    """Verify the inserted delimiter takes the TemporalId of the VCL NAL unit."""
    trail = bytes([1 << 1, 0x03]) + TRAIL[2:]
    data = mpegts.MpegTsMuxer().video([trail], 0, keyframe=False)
    (pes,) = pes_payloads(data, mpegts.VIDEO_PID)
    assert pes[14:].startswith(mpegts.START_CODE + bytes([0x46, 0x03, 0x50]))


@pytest.mark.parametrize("size", [1, 2, 3, 170, 183, 184, 185, 400])
def test_short_payloads_are_stuffed(size: int) -> None:
    """Verify each PES fits whole TS packets whatever its size."""
    muxer = mpegts.MpegTsMuxer()
    data = muxer.opus(b"\x01" * size, 0)
    (pes,) = pes_payloads(data, mpegts.AUDIO_PID)
    assert pes.endswith(b"\x01" * size)
    assert pes[:4] == b"\x00\x00\x01\xbd"


def test_no_audio_without_audio() -> None:
    """Verify a muxer without audio drops Opus and has no audio stream."""
    muxer = mpegts.MpegTsMuxer(audio=False)
    assert muxer.opus(b"\x01", 0) == b""
    assert b"Opus" not in muxer.tables()
    assert b"Opus" in mpegts.MpegTsMuxer().tables()


def test_ring_starts_at_an_irap_and_trims() -> None:
    """Verify the ring keeps the latest IRAP at least its seconds old."""
    clock = Clock()
    ring = PrebufferRing(4.0, clock=clock)
    ring.on_source_video(unit(False), 1)
    ring.on_source_audio(audio())
    assert ring.snapshot(4.0) == []
    for second in range(12):
        clock.now = 100.0 + second
        ring.on_source_video(unit(second % 2 == 0, second), 1)
        ring.on_source_audio(audio(second))
    # Now 111: the latest IRAP at or before 107 came at 106.
    assert ring.snapshot(4.0)[0].arrival == 106.0
    assert ring.snapshot(4.0)[0].keyframe
    assert ring.snapshot(1.0)[0].arrival == 110.0
    assert ring.snapshot(99.0)[0].arrival == 106.0
    assert ring.buffered == pytest.approx(5.0)


def test_ring_bounds_its_bytes() -> None:
    """Verify the ring drops whole GOPs beyond its byte limit."""
    clock = Clock()
    ring = PrebufferRing(60.0, max_bytes=200_000, clock=clock)
    for second in range(20):
        clock.now = 100.0 + second
        ring.on_source_video(unit(True, second), 1)
    assert ring.snapshot(60.0)[0].arrival >= 115.0


def test_ring_caps_bytes_without_a_second_irap() -> None:
    """Verify a GOP beyond the byte limit empties the ring until the next IRAP."""
    clock = Clock()
    limit = sum(map(len, KEYFRAME)) + 5 * len(TRAIL)
    ring = PrebufferRing(60.0, max_bytes=limit, clock=clock)
    ring.on_source_video(unit(True), 1)
    for second in range(20):
        clock.now = 100.0 + second
        ring.on_source_video(unit(False, second), 1)
    assert ring.snapshot(60.0) == []
    ring.on_source_video(unit(False), 1)
    assert ring.snapshot(60.0) == []
    ring.on_source_video(unit(True), 1)
    assert len(ring.snapshot(60.0)) == 1


def test_ring_restarts_on_a_new_ssrc() -> None:
    """Verify a restarted pull clears the ring until its next IRAP."""
    clock = Clock()
    ring = PrebufferRing(4.0, clock=clock)
    ring.on_source_video(unit(True), 1)
    ring.on_source_video(unit(False), 2)
    assert ring.snapshot(4.0) == []
    ring.on_source_video(unit(True), 2)
    assert len(ring.snapshot(4.0)) == 1


def test_ring_closed_source() -> None:
    """Verify the ring empties and reports when its source ends."""
    ring = PrebufferRing(4.0)
    reasons: list[str] = []
    ring.on_closed = reasons.append
    ring.on_source_video(unit(True), 1)
    ring.on_source_closed("gone")
    assert reasons == ["gone"]
    assert ring.snapshot(4.0) == []


def test_timestamps_map_to_a_common_timeline() -> None:
    """Verify RTP timestamps wrap and re-anchor after a jump."""
    video = _Timestamps(90000, origin=10.0)
    assert video.pts(0xFFFFFF00, 10.0) == PTS_START
    assert video.pts(0x100, 10.0) == PTS_START + 0x200
    audio_ts = _Timestamps(48000, origin=10.0)
    assert audio_ts.pts(48000, 10.5) == PTS_START + 45000
    assert audio_ts.pts(96000, 11.5) == PTS_START + 135000
    # A new SSRC jumps its timestamps; the arrival time places it.
    assert audio_ts.pts(5, 12.5) == PTS_START + 225000
    # Never backwards and never repeated.
    assert audio_ts.pts(4, 12.5) == PTS_START + 225001
    assert audio_ts.pts(3, 12.5) == PTS_START + 225002


def test_ffmpeg_args_follow_the_selection() -> None:
    """Verify size, rate, profile, level, GOP and audio of the transcode."""
    args = recording_ffmpeg_args(selected(), audio=True, ffmpeg="ff")
    joined = " ".join(args)
    assert args[0] == "ff"
    assert "-f mpegts -i pipe:0" in joined
    assert "scale=1280:720:" in joined
    assert "pad=1280:720:" in joined
    assert "-r 30 -b:v 2000k" in joined
    assert "-profile:v main -level:v 4.0" in joined
    assert "-g 120" in joined
    assert "expr:gte(t,n_forced*4)" in joined
    assert "-map 0:a:0" in joined
    assert "-ar 32000" in joined
    assert "anullsrc" not in joined
    assert args[-1] == "pipe:1"


@pytest.mark.parametrize(
    ("fragment_ms", "gop", "interval"), [(2000, 60, "2"), (0, 30, "1")]
)
def test_ffmpeg_args_follow_the_fragment_length(
    fragment_ms: int, gop: int, interval: str
) -> None:
    """Verify the IRAP interval is the selected fragment length, at least 1 s."""
    config = selected()
    config.recording.media_containers[0].fragment_length_ms = fragment_ms
    joined = " ".join(recording_ffmpeg_args(config, audio=True))
    assert f"-g {gop} " in joined
    assert f"expr:gte(t,n_forced*{interval})" in joined


def test_ffmpeg_args_silent_and_hardware() -> None:
    """Verify a silent track without audio and no x264 options for others."""
    args = recording_ffmpeg_args(
        selected(profile=2, sample_rate=hr.AudioSampleRate.KHZ_16),
        audio=False,
        video_encoder="h264_videotoolbox",
        encoder_args=("-realtime", "1"),
    )
    joined = " ".join(args)
    assert "anullsrc=r=16000:cl=mono" in joined
    assert "-map 1:a:0" in joined
    assert "-shortest" in joined
    assert "-level:v" not in joined
    assert "-preset" not in joined
    assert "-profile:v high -realtime 1" in joined
    assert "anullsrc" in " ".join(
        recording_ffmpeg_args(selected(), audio=True, source_audio=False)
    )


async def test_recorder_starts_with_buffered_media() -> None:
    """Verify a recording starts with the buffer, then the live media."""
    source = MediaSource()
    ports = await source.start()
    sender = Source(ports)
    recorder = Recorder(source, prebuffer=4.0)
    chunks: list[bytes] = []
    ended: list[str | None] = []
    recorder.on_data = chunks.append
    recorder.on_ended = ended.append
    try:
        sender.send_video([TRAIL])
        sender.send_video(KEYFRAME)
        sender.send_audio(b"\x01" * 30)
        sender.send_video([TRAIL])
        await wait_until(
            lambda: sum(i.unit is not None for i in recorder.ring.snapshot(0)) == 2
        )
        await recorder.start(CAT, 4.0)
        assert recorder.recording
        sender.send_video([TRAIL])
        sender.send_audio(b"\x02" * 30)
        await wait_until(lambda: len(pes_payloads_safe(chunks)) >= 3)
        video = pes_payloads(join_whole(chunks), mpegts.VIDEO_PID)
        assert video[0][14:].startswith(
            mpegts.START_CODE + AUD + mpegts.START_CODE + KEYFRAME[0]
        )
        assert mpegts.decode_pts(video[0][9:14]) == PTS_START
        assert mpegts.decode_pts(video[1][9:14]) == PTS_START + 3000
        await recorder.stop()
        assert not recorder.recording
        assert ended == []
    finally:
        await recorder.close()
        sender.close()
        source.close()


def join_whole(chunks: list[bytes]) -> bytes:
    """The chunks cut to whole TS packets."""
    data = b"".join(chunks)
    return data[: len(data) - len(data) % mpegts.PACKET_SIZE]


def pes_payloads_safe(chunks: list[bytes]) -> list[bytes]:
    """The video PES packets received so far."""
    data = join_whole(chunks)
    return pes_payloads(data, mpegts.VIDEO_PID) if data else []


async def test_recorder_reports_the_transcoder_exit() -> None:
    """Verify a failing transcoder ends the recording with its exit code."""
    source = MediaSource()
    await source.start()
    recorder = Recorder(source, prebuffer=4.0)
    ended: list[str | None] = []
    recorder.on_ended = ended.append
    try:
        script = "import sys; sys.stderr.write('No such encoder: x\\n'); sys.exit(3)"
        await recorder.start([sys.executable, "-c", script], 4.0)
        await wait_until(lambda: bool(ended))
        assert ended == ["ffmpeg exited with 3: No such encoder: x"]
        assert not recorder.recording
        assert not recorder.closed
    finally:
        await recorder.close()
        source.close()


async def test_recorder_reports_a_cut_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a transcoder that fell behind ends the recording with an error."""
    monkeypatch.setattr(recording_module, "MAX_PENDING_BYTES", 1)
    source = MediaSource()
    ports = await source.start()
    sender = Source(ports)
    recorder = Recorder(source, prebuffer=4.0)
    ended: list[str | None] = []
    recorder.on_ended = ended.append
    try:
        await recorder.start(CAT, 4.0)
        sender.send_video(KEYFRAME)
        await wait_until(lambda: bool(ended))
        assert ended == ["the transcoder fell behind, the recording was cut short"]
    finally:
        await recorder.close()
        sender.close()
        source.close()


async def test_recorder_ends_with_its_source() -> None:
    """Verify a closed source ends the recording and the recorder."""
    source = MediaSource()
    await source.start()
    recorder = Recorder(source, prebuffer=4.0)
    ended: list[str | None] = []
    closed: list[str] = []
    recorder.on_ended = ended.append
    recorder.on_closed = closed.append
    await recorder.start(CAT, 4.0)
    source.close("gone")
    assert ended == ["media source ended: gone"]
    assert closed == ["gone"]
    assert recorder.closed
    with pytest.raises(RuntimeError):
        await recorder.start(CAT, 4.0)
    await recorder.close()


async def test_one_recording_at_a_time() -> None:
    """Verify a second start fails while one runs."""
    source = MediaSource()
    await source.start()
    recorder = Recorder(source, prebuffer=4.0)
    try:
        await recorder.start(CAT, 4.0)
        with pytest.raises(RuntimeError):
            await recorder.start(CAT, 4.0)
    finally:
        await recorder.close()
        source.close()


def has_ffmpeg() -> bool:
    """Whether real ffmpeg tests are wanted and possible."""
    return FFMPEG is not None and os.environ.get("HAP_PYTHON_FFMPEG_TESTS") == "1"


@pytest.mark.skipif(not has_ffmpeg(), reason="set HAP_PYTHON_FFMPEG_TESTS=1")
async def test_real_transcode_includes_the_prebuffer() -> None:
    """Verify ffmpeg makes H.264 fragments that start with buffered seconds."""
    assert FFMPEG is not None
    source = MediaSource()
    host, video_port, audio_port = await source.start()
    pull = await asyncio.create_subprocess_exec(
        *(FFMPEG, "-hide_banner", "-loglevel", "error", "-re"),
        *("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25"),
        *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"),
        *("-map", "0:v", "-c:v", "libx265"),
        *("-x265-params", "keyint=25:bframes=0:log-level=error"),
        *("-bsf:v", "dump_extra", "-f", "rtp", f"rtp://{host}:{video_port}"),
        *("-map", "1:a", "-c:a", "libopus", "-ac", "1", "-ar", "48000"),
        *("-f", "rtp", f"rtp://{host}:{audio_port}"),
    )
    recorder = Recorder(source, prebuffer=3.0)
    reader = asyncio.StreamReader()
    recorder.on_data = reader.feed_data
    recorder.on_ended = lambda _error: reader.feed_eof()
    try:
        await wait_until(lambda: recorder.ring.buffered >= 3.0)
        started = time.monotonic()
        await recorder.start(
            recording_ffmpeg_args(selected(640, 360), audio=True, ffmpeg=FFMPEG), 3.0
        )
        packets = []
        async for packet in fmp4_recording_packets(reader):
            packets.append((time.monotonic() - started, packet))
            if len(packets) == 2:
                break
        init, (arrived, first) = packets[0][1], packets[1]
        assert b"moov" in init.data[:64] or b"ftyp" in init.data[:8]
        assert b"moof" in first.data[:16]
        # A 4 s fragment within much less than 4 s: it holds the buffer.
        assert arrived < 3.0
    finally:
        await recorder.close()
        pull.terminate()
        await pull.wait()
        source.close()
