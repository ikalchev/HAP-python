"""Tests for pyhap.media.ffmpeg with a stand-in ffmpeg and a fake worker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import os
from pathlib import Path
import signal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyhap import hksv3
from pyhap.camera_stream import StreamError
from pyhap.media import ffmpeg as ffmpeg_module
from pyhap.media.ffmpeg import (
    FFmpegInput,
    FFmpegPull,
    FFmpegSourceProvider,
    redact_urls,
)

from .media_fakes import (
    AUDIO,
    LOW,
    MEDIUM,
    FakeWorker,
    make_session,
    write_fake_ffmpeg,
)

pytestmark = pytest.mark.timeout(5)

TIER_ONE = hksv3.VideoStreamTier(1, 3, 1, 1, 1, 1)
DEFAULTS = ("0:v:0", "0:a:0", True, 2.0)
INPUTS: dict[int | hksv3.VideoQuality | str, list[str] | FFmpegInput] = {
    1: ["-i", "tier-one"],
    hksv3.VideoQuality.MEDIUM: ["-i", "medium"],
    "low": FFmpegInput(("-i", "low"), video_map="0:1", audio=False, gop=4.0),
}


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> Path:
    """An ffmpeg stand-in."""
    return write_fake_ffmpeg(tmp_path)


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Poll a predicate on the event loop."""
    while not predicate():
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("tier", "expected", "name"),
    [
        (TIER_ONE, FFmpegInput(("-i", "tier-one"), *DEFAULTS), "tier 1"),
        (MEDIUM, FFmpegInput(("-i", "medium"), *DEFAULTS), "medium"),
        (LOW, FFmpegInput(("-i", "low"), "0:1", "0:a:0", False, 4.0), "low"),
    ],
)
async def test_ffmpeg_inputs(
    tier: hksv3.VideoStreamTier, expected: FFmpegInput, name: str
) -> None:
    """Verify an identifier wins over a quality, also given by name."""
    provider = FFmpegSourceProvider(INPUTS)

    assert await provider.input_for(tier) == expected
    assert provider.channel_name(tier) == name


async def test_ffmpeg_no_input() -> None:
    """Verify a tier without an input refuses the offer."""
    provider = FFmpegSourceProvider({"high": ["-i", "high"]})

    with pytest.raises(StreamError, match="No ffmpeg input"):
        await provider.input_for(LOW)


async def resolve(tier: hksv3.VideoStreamTier) -> list[str] | None:
    """An input resolver with an input for the Medium tier only."""
    return ["-i", f"resolved-{tier.identifier}"] if tier is MEDIUM else None


async def test_ffmpeg_input_resolver() -> None:
    """Verify an async resolver maps tiers to inputs, None refusing the offer."""
    provider = FFmpegSourceProvider(resolve, video_map="0:2", audio=False, gop=1.0)

    found = await provider.input_for(MEDIUM)

    assert found == FFmpegInput(("-i", "resolved-2"), "0:2", "0:a:0", False, 1.0)
    with pytest.raises(StreamError, match="No ffmpeg input"):
        await provider.input_for(LOW)


def test_ffmpeg_channel_name_of_an_unknown_quality() -> None:
    """Verify a tier of an unknown quality is named by its identifier."""
    provider = FFmpegSourceProvider(resolve)

    assert provider.channel_name(hksv3.VideoStreamTier(7, 9, 1, 1, 1, 1)) == "tier 7"


def test_ffmpeg_command() -> None:
    """Verify ffmpeg copies H.265 and encodes Opus to the source's ports."""
    provider = FFmpegSourceProvider(INPUTS, ffmpeg="/bin/ffmpeg", stats_period=10)
    found = FFmpegInput(("-i", "cam"), "0:v:1", "0:a:2")

    command = provider.command(found, ("127.0.0.1", 6000, 6002), 24000)

    assert command == [
        *("/bin/ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"),
        *("-stats", "-stats_period", "10", "-i", "cam"),
        *("-map", "0:v:1", "-c:v", "copy", "-bsf:v", "dump_extra"),
        *("-payload_type", "99", "-rtpflags", "skip_rtcp"),
        *("-f", "rtp", "rtp://127.0.0.1:6000"),
        *("-map", "0:a:2", "-c:a", "libopus", "-ac", "1", "-ar", "48000"),
        *("-b:a", "24000", "-application", "lowdelay", "-frame_duration", "20"),
        *("-payload_type", "110", "-rtpflags", "skip_rtcp"),
        *("-f", "rtp", "rtp://127.0.0.1:6002"),
    ]


def test_ffmpeg_command_packet_time() -> None:
    """Verify the Opus frame duration follows the audio tier's packet time."""
    provider = FFmpegSourceProvider(INPUTS)

    command = provider.command(
        FFmpegInput(("-i", "cam")), ("127.0.0.1", 6000, 6002), 32000, packet_time=40
    )

    assert command[command.index("-frame_duration") + 1] == "40"
    assert command[command.index("-b:a") + 1] == "32000"


async def test_ffmpeg_provider_without_audio_tier(fake_ffmpeg: Path) -> None:
    """Verify a multi-tier session without audio leases with the default tier."""
    worker = FakeWorker()
    provider = FFmpegSourceProvider(INPUTS, worker=cast(Any, worker))

    lease = await provider(make_session(), LOW, None)

    assert lease.channel == "low"
    await lease.release()


def test_ffmpeg_command_without_audio() -> None:
    """Verify a source without audio gets video only, IPv6 in brackets."""
    provider = FFmpegSourceProvider(INPUTS)

    command = provider.command(FFmpegInput(("-i", "cam")), ("::1", 6000, 0), 24000)

    assert command[-1] == "rtp://[::1]:6000"
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "-nostats" in command
    assert "libopus" not in command


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "rtsps://user:pw@192.168.1.1:7441/T0k3n?enableSrtp: 404 Not Found",
            "rtsps://192.168.1.1:7441/<redacted> 404 Not Found",
        ),
        (
            "Input #0, rtsp, from 'rtsp://[::1]:554/a/b':",
            "Input #0, rtsp, from 'rtsp://[::1]:554/<redacted>':",
        ),
        ("http://cam.local:80 timed out", "http://cam.local:80 timed out"),
        ("frame=  25 fps=0.0 q=-1.0", "frame=  25 fps=0.0 q=-1.0"),
        (
            "rtsp://my user:p w@10.0.0.2:554/live failed",
            "rtsp://10.0.0.2:554/<redacted> failed",
        ),
        (
            """rtsp://user:p'w"x@10.0.0.2/live failed""",
            "rtsp://10.0.0.2/<redacted> failed",
        ),
    ],
    ids=[
        "credentials-token-query",
        "quoted-ipv6",
        "host-only",
        "no-url",
        "userinfo-spaces",
        "userinfo-quotes",
    ],
)
def test_redact_urls(line: str, expected: str) -> None:
    """Verify only scheme, host and port of a URL stay in the log."""
    assert redact_urls(line) == expected


async def test_overlong_stderr_line_is_cut(caplog: pytest.LogCaptureFixture) -> None:
    """Verify ffmpeg's log cuts an overlong line and goes on after it."""
    caplog.set_level(logging.DEBUG, "pyhap.media.ffmpeg")
    stderr = asyncio.StreamReader()
    stderr.feed_data(b"x" * (3 * ffmpeg_module.MAX_LOG_LINE) + b"\nnext\nlast")
    stderr.feed_eof()

    await ffmpeg_module._log_output("medium", cast(Any, SimpleNamespace(stderr=stderr)))

    assert caplog.messages == [
        "ffmpeg medium:  [truncated]",
        "ffmpeg medium: next",
        "ffmpeg medium: last",
    ]


def test_overlong_line_is_skipped_without_buffering() -> None:
    """Verify the rest of an overlong line is skipped, not buffered."""
    lines = ffmpeg_module.StderrLines()
    word = b"word " * (ffmpeg_module.MAX_LOG_LINE // 5)

    assert lines.feed(word + b"tail") == [word[:-1] + b" [truncated]"]
    assert lines.pending == b""
    assert lines.feed(b"y" * ffmpeg_module.MAX_LOG_LINE) == []
    assert lines.pending == b""
    assert lines.feed(b"rest\rnext\n") == [b"next"]
    assert lines.feed(b"after\n") == [b"after"]
    assert lines.end() == []


def test_line_over_the_limit_across_reads_is_cut() -> None:
    """Verify a complete line longer than the limit is cut too."""
    lines = ffmpeg_module.StderrLines()

    assert lines.feed(b"a " * 2000) == []
    assert lines.feed(b"b" * 200 + b"\n") == [b"a " * 1999 + b"a [truncated]"]


@pytest.mark.parametrize(
    "url",
    [
        b"rtsp://admin:pw@10.0.0.2/live",
        b"rtsp://admin:pa ss@10.0.0.2/live",
        b"'rtsp://admin:pw@10.0.0.2/live'",
    ],
    ids=["userinfo", "userinfo-space", "quoted"],
)
def test_cut_drops_a_url_cut_in_two(url: bytes) -> None:
    """Verify a URL running over the limit is dropped, not split."""
    line = b"x " * ((ffmpeg_module.MAX_LOG_LINE - 20) // 2) + url + b" tail"

    (cut,) = ffmpeg_module.StderrLines().feed(line + b"\n")

    assert b"admin" not in cut
    assert cut.endswith(b"x [truncated]")


def test_cut_keeps_a_complete_url() -> None:
    """Verify only the URL cut in two is dropped, not an earlier one."""
    line = (
        b"x " * ((ffmpeg_module.MAX_LOG_LINE - 40) // 2)
        + b"rtsp://cam1/a rtsp://admin:pw@10.0.0.2/live tail"
    )

    (cut,) = ffmpeg_module.StderrLines().feed(line + b"\n")

    assert cut.endswith(b" rtsp://cam1/a [truncated]")


async def test_ffmpeg_provider_without_audio(fake_ffmpeg: Path) -> None:
    """Verify an input without audio opens a video-only source."""
    worker = FakeWorker()
    provider = FFmpegSourceProvider(INPUTS, worker=cast(Any, worker))

    lease = await provider(make_session(), LOW, AUDIO)

    assert lease.source.rtp_input == ("127.0.0.1", 5000, 0)
    assert lease.channel == "low"
    await lease.release()


async def test_provider_diagnostics() -> None:
    """Verify the diagnostics name each leased channel with its leases."""
    worker = FakeWorker()
    provider = FFmpegSourceProvider(INPUTS, worker=cast(Any, worker))
    lease = await provider(make_session(), LOW, AUDIO)
    channels = provider.diagnostics()["channels"]
    assert channels["low"]["leases"] == 1
    assert channels["low"]["restarts"] == 0
    await lease.release()


def pull_on(fake_ffmpeg: Path, gop: float = 10.0) -> FFmpegPull:
    """An ffmpeg pull running the fake ffmpeg."""
    return FFmpegPull(
        lambda rtp_input: [str(fake_ffmpeg), f"rtp://{rtp_input[0]}"],
        "medium",
        gop,
        restart_delay=0.01,
    )


async def test_ffmpeg_pull_restarts_after_exit(fake_ffmpeg: Path) -> None:
    """Verify ffmpeg is started again when it exits while it is needed."""
    (fake_ffmpeg.parent / "exit-now").touch()
    pull = pull_on(fake_ffmpeg)

    await pull.start(("127.0.0.1", 1, 2))
    await wait_until(lambda: pull.restarts >= 2)
    (fake_ffmpeg.parent / "exit-now").unlink()
    await pull.stop()

    assert pull.pid is None


async def test_ffmpeg_pull_reports_exits_and_backs_off(fake_ffmpeg: Path) -> None:
    """Verify exits reach on_error with the redacted last line, and back off."""
    (fake_ffmpeg.parent / "exit-now").touch()
    errors: list[tuple[str, str]] = []
    pull = FFmpegPull(
        lambda rtp_input: [str(fake_ffmpeg), f"rtp://{rtp_input[0]}"],
        "medium",
        10.0,
        restart_delay=0.01,
        on_error=lambda name, message: errors.append((name, message)),
    )
    await pull.start(("127.0.0.1", 1, 2))
    await wait_until(lambda: len(errors) >= 3)
    (fake_ffmpeg.parent / "exit-now").unlink()
    await pull.stop()
    name, message = errors[0]
    assert name == "medium"
    assert message.startswith("exited with 1: rtsps://192.168.1.1:7441/<redacted>")
    assert "pw" not in message
    assert pull.diagnostics()["last_error"] == message
    assert pull._failures >= 3


async def test_ffmpeg_pull_backs_off_failed_starts(
    fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify failed starts keep backing off, each reported once, not counted."""
    # Every run counts as healthy, so only the failed starts may back off.
    monkeypatch.setattr(ffmpeg_module, "HEALTHY_RUN", 0.0)
    errors: list[str] = []
    pull = FFmpegPull(
        lambda rtp_input: [str(fake_ffmpeg), f"rtp://{rtp_input[0]}"],
        "medium",
        10.0,
        restart_delay=0.01,
        on_error=lambda _name, message: errors.append(message),
    )
    await pull.start(("127.0.0.1", 1, 2))
    pid = pull.pid
    assert pid is not None
    fake_ffmpeg.rename(fake_ffmpeg.with_name("gone"))
    os.kill(pid, signal.SIGTERM)
    await wait_until(lambda: len(errors) >= 4)
    await pull.stop()

    assert errors[0].startswith("exited with")
    assert all(message.startswith("could not start") for message in errors[1:])
    assert pull._failures == len(errors)
    assert pull.restarts == 0


async def test_failed_first_start_reaches_on_error(tmp_path: Path) -> None:
    """Verify a missing ffmpeg raises and is reported to on_error."""
    errors: list[tuple[str, str]] = []
    pull = FFmpegPull(
        lambda _rtp_input: [str(tmp_path / "missing-ffmpeg")],
        "medium",
        10.0,
        restart_delay=0.01,
        on_error=lambda name, message: errors.append((name, message)),
    )
    with pytest.raises(OSError):
        await pull.start(("127.0.0.1", 1, 2))
    assert errors and errors[0][0] == "medium"
    assert errors[0][1].startswith("could not start")
    assert pull.last_error == errors[0][1]
    assert pull.pid is None


@pytest.mark.parametrize(
    ("gop", "age", "measured"),
    [
        (10.0, None, None),  # Started recently: its first IRAP picture is on the way.
        (0.01, 0.01, None),  # The last IRAP picture is recent.
        (0.01, 1.0, 10.0),  # The measured GOP is long: the next one is on the way.
    ],
    ids=["starting", "recent", "measured-gop"],
)
async def test_ffmpeg_pull_keeps_running(
    fake_ffmpeg: Path, gop: float, age: float | None, measured: float | None
) -> None:
    """Verify a keyframe request does not restart a healthy ffmpeg."""
    pull = pull_on(fake_ffmpeg, gop)
    await pull.start(("127.0.0.1", 1, 2))
    pid = pull.pid
    await asyncio.sleep(0.05)

    pull.keyframe_requested(age, measured)
    await asyncio.sleep(0.05)

    assert (pull.pid, pull.restarts) == (pid, 0)
    await pull.stop()


@pytest.mark.parametrize(
    ("gop", "age", "measured"),
    [(0.01, None, None), (0.01, 1.0, None), (10.0, 1.0, 0.01)],
    ids=["no-irap", "configured-gop", "measured-gop"],
)
async def test_ffmpeg_pull_restarts_for_a_keyframe(
    fake_ffmpeg: Path, gop: float, age: float | None, measured: float | None
) -> None:
    """Verify ffmpeg restarts when no IRAP picture came for two GOPs."""
    pull = pull_on(fake_ffmpeg, gop)
    await pull.start(("127.0.0.1", 1, 2))
    pid = pull.pid
    pull._started -= 1
    await asyncio.sleep(0.05)

    pull.keyframe_requested(age, measured)
    await wait_until(lambda: pull.restarts == 1 and pull.pid is not None)

    assert pull.pid != pid
    await pull.stop()
