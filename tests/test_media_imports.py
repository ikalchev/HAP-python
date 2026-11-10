"""Tests that pyhap.media needs no ICE library."""

from __future__ import annotations

import subprocess
import sys


def test_webrtc_imports_without_aioice() -> None:
    """Verify the WebRTC sender runs its own ICE, without aioice."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['aioice'] = None;"
            " import pyhap.media.sender, pyhap.media._worker, pyhap.camera",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
