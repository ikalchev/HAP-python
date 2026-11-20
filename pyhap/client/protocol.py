"""Frame parsing for a HAP client connection.

A connection carries plain HTTP/1.1 responses and interleaved ``EVENT/1.0``
push frames; :func:`parse_frame` splits one frame out of a byte buffer without
an HTTP state machine, so the two can be told apart as they arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
import time

PAIRING_CONTENT_TYPE = b"application/pairing+tlv8"
HAP_CONTENT_TYPE = b"application/hap+json"

_HEADER_SEPARATOR = b"\r\n\r\n"


@dataclass(frozen=True)
class Frame:
    """A parsed HTTP response or EVENT frame received from the server."""

    kind: str  # "response" or "event"
    status: int
    body: bytes
    received_ns: int


def parse_frame(buffer: bytearray) -> Frame | None:
    """Parse one frame from ``buffer``.

    Returns ``None`` if the buffer does not hold a complete frame yet, in
    which case the buffer is left untouched. Otherwise the frame is returned
    and its bytes are consumed from the buffer.
    """
    header_end = buffer.find(_HEADER_SEPARATOR)
    if header_end < 0:
        return None
    lines = bytes(buffer[:header_end]).split(b"\r\n")
    start = lines[0]
    if start.startswith(b"EVENT/"):
        kind = "event"
    elif start.startswith(b"HTTP/"):
        kind = "response"
    else:
        raise RuntimeError(f"Client received unexpected data: {start!r}")
    status = int(start.split(b" ", 2)[1])
    content_length = 0
    for header in lines[1:]:
        name, _, value = header.partition(b":")
        if name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    body_start = header_end + len(_HEADER_SEPARATOR)
    total = body_start + content_length
    if len(buffer) < total:
        return None
    body = bytes(buffer[body_start:total])
    del buffer[:total]
    return Frame(kind, status, body, time.perf_counter_ns())
