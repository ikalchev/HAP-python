"""Shared helpers for the pyhap.media tests (not a test module)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
from typing import Any

from pyhap.media.hevc import NAL_AP, fragment

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


def seeded_bytes(seed: str, length: int) -> bytes:
    """Reproduce the vector generator's SHA-256 counter byte stream."""
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return out[:length]


def plaintext(spec: dict[str, Any]) -> bytes:
    """Regenerate a plaintext stored as seed and length."""
    return seeded_bytes(spec["seed"], spec["len"])


def blob(value: bytes) -> Any:
    """Describe bytes the way the generator stores them."""
    if len(value) <= 64:
        return value.hex()
    return {"len": len(value), "sha256": hashlib.sha256(value).hexdigest()}


def scrypted_nal(spec: list[Any]) -> bytes:
    """Build a NAL unit from a generator ``[type, size, seed]`` spec."""
    kind, size, seed = spec
    return bytes([kind << 1, 1]) + seeded_bytes(seed, size - 2)


def scrypted_payloads(unit: list[Any], fu_chunk: int) -> list[bytes]:
    """Build the RTP payloads of one generator ``[kind, nal specs]`` entry."""
    kind, specs = unit
    nals = [scrypted_nal(spec) for spec in specs]
    if kind == "ap":
        header = bytes([NAL_AP << 1, 1])
        return [header + b"".join(struct.pack(">H", len(n)) + n for n in nals)]
    if kind == "fu":
        return fragment(nals[0], fu_chunk + 3)
    return nals


def scrypted_input(case: dict[str, Any]) -> list[tuple[int, int, bool, bytes]]:
    """Rebuild the ``(sequence, timestamp, marker, payload)`` HEVC RTP input."""
    packets: list[tuple[int, int, bool, bytes]] = []
    sequence = int(case["input"][0]["sequence"])
    for index, unit in enumerate(case["access_units"]):
        payloads = [
            payload
            for spec in unit
            for payload in scrypted_payloads(spec, case["fu_chunk"])
        ]
        for position, payload in enumerate(payloads):
            marker = position == len(payloads) - 1
            packets.append((sequence, 3000 * (index + 1), marker, payload))
            sequence = (sequence + 1) & 0xFFFF
    return packets
