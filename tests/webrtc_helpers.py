"""Helpers for the WebRTC media tests (not a test module)."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .media_helpers import seeded_bytes

DATA = Path(__file__).parent / "data"


@dataclass
class IceCandidate:
    """Stands in for pyhap.hksv3.IceCandidate: the same fields."""

    candidate: str
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None


@dataclass
class SFrameKey:
    """Stands in for pyhap.hksv3.SFrameKey: the same fields."""

    key: bytes
    kid: int


def load_vectors(name: str) -> Any:
    """Load a JSON vector file from tests/data."""
    return json.loads((DATA / name).read_text())


def plaintext(spec: dict[str, Any]) -> bytes:
    """Regenerate a plaintext stored as seed and length."""
    return seeded_bytes(spec["seed"], spec["len"])
