"""End-to-end test with real ffmpeg RTP input; opt in with an env variable.

Set ``HAP_PYTHON_FFMPEG_TESTS=1`` to run it. ffmpeg encodes a test pattern
with libx265 and Opus and sends plain RTP to the sender as an integrator
would. The loopback controller decrypts the frames; ffmpeg must then decode
every one of them.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from pyhap.media import AudioParams  # noqa: E402

from .media_peer import Controller, make_sender, negotiate  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FRAMES = 45
OPT_IN = "HAP_PYTHON_FFMPEG_TESTS"


def has_encoders() -> bool:
    """Whether ffmpeg tests are wanted and ffmpeg has libx265 and libopus."""
    if FFMPEG is None or os.environ.get(OPT_IN) != "1":
        return False
    encoders = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return "libx265" in encoders and "libopus" in encoders


pytestmark = pytest.mark.skipif(
    not has_encoders(), reason=f"set {OPT_IN}=1, needs ffmpeg with libx265"
)


def ffmpeg_command(host: str, video_port: int, audio_port: int) -> list[str]:
    """Build the integrator-style ffmpeg command."""
    return [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x240:rate=30:duration={FRAMES / 30}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={FRAMES / 30}",
        "-map",
        "0:v",
        "-c:v",
        "libx265",
        "-preset",
        "ultrafast",
        "-x265-params",
        "keyint=15:min-keyint=15:bframes=0:log-level=error",
        "-bsf:v",
        "dump_extra",
        "-f",
        "rtp",
        f"rtp://{host}:{video_port}",
        "-map",
        "1:a",
        "-c:a",
        "libopus",
        "-ac",
        "1",
        "-frame_duration",
        "20",
        "-f",
        "rtp",
        f"rtp://{host}:{audio_port}",
    ]


def annex_b(frames: list[list[bytes]]) -> bytes:
    """Write access units as an Annex B elementary stream."""
    return b"".join(b"\x00\x00\x00\x01" + nal for frame in frames for nal in frame)


def nal_types(frame: list[bytes]) -> list[int]:
    """Return the NAL unit types of an access unit."""
    return [nal[0] >> 1 & 0x3F for nal in frame]


def decoded_frames(path: Path) -> int:
    """Decode an H.265 stream with ffmpeg, failing on any decode error."""
    result = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            "-f",
            "hevc",
            "-i",
            str(path),
            "-f",
            "framemd5",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return sum(1 for line in result.stdout.splitlines() if line and line[0] != "#")


@pytest.mark.timeout(30)
async def test_ffmpeg_hevc_and_opus(tmp_path: Path) -> None:
    """Verify real ffmpeg RTP comes out decodable after SRTP and SFrame."""
    sender = make_sender(audio=AudioParams())
    controller = Controller()
    await negotiate(sender, controller)
    process = await asyncio.create_subprocess_exec(*ffmpeg_command(*sender.rtp_input))
    assert await asyncio.wait_for(process.wait(), 20) == 0
    await asyncio.sleep(0.3)
    received = controller.received
    stream = tmp_path / "received.h265"
    stream.write_bytes(annex_b(received.video))
    assert len(received.video) == FRAMES
    assert nal_types(received.video[0])[:3] == [32, 33, 34]
    assert decoded_frames(stream) == len(received.video)
    assert len(received.audio) > 50
    await sender.close()
    await controller.close()
