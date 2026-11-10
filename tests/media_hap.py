"""A synthetic camera channel for the HAP media tests (not a test module)."""

from __future__ import annotations

import asyncio
import contextlib

from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera

from .media_peer import KEYFRAME, TRAIL, Source

CLIENT = ("127.0.0.1", 50000)
# A synthetic camera: an IRAP picture every GOP_FRAMES frames.
GOP_FRAMES = 10
FRAME_INTERVAL = 0.02


class SyntheticPull:
    """Plays a camera channel: synthetic H.265 to the source's ports."""

    def __init__(self) -> None:
        """Not running yet."""
        self.starts = 0
        self.stops = 0
        self.keyframe_requests: list[tuple[float | None, float | None]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self, rtp_input: tuple[str, int, int]) -> None:
        """Send frames until stopped."""
        self.starts += 1
        self._task = asyncio.ensure_future(self._send(Source(rtp_input)))

    async def _send(self, source: Source) -> None:
        try:
            frame = 0
            while True:
                source.send_video(KEYFRAME if frame % GOP_FRAMES == 0 else [TRAIL])
                frame += 1
                await asyncio.sleep(FRAME_INTERVAL)
        finally:
            source.close()

    async def stop(self) -> None:
        """Stop sending."""
        self.stops += 1
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def keyframe_requested(self, age: float | None, gop: float | None) -> None:
        """Record the request; the next GOP brings a keyframe."""
        self.keyframe_requests.append((age, gop))


def bridged(driver: AccessoryDriver, camera: Camera) -> Camera:
    """Add the camera to a bridge on the driver."""
    bridge = Bridge(driver, "Bridge")
    bridge.add_accessory(camera)
    driver.add_accessory(bridge)
    return camera
