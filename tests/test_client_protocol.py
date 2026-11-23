"""Unit tests for the HAP client frame parser (``pyhap.client.protocol``)."""

import pytest

from pyhap.client import parse_frame


def _response_frame(status: int, body: bytes) -> bytes:
    """A serialized HTTP/1.1 response frame."""
    return (
        f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


def test_incomplete_header_returns_none_and_keeps_buffer() -> None:
    """A buffer without the header terminator holds no frame yet."""
    buffer = bytearray(b"HTTP/1.1 200 OK\r\nContent-Length: 2")
    assert parse_frame(buffer) is None
    assert buffer == b"HTTP/1.1 200 OK\r\nContent-Length: 2"


def test_incomplete_body_returns_none_then_completes() -> None:
    """Headers may arrive before the full body; the buffer waits for it."""
    raw = _response_frame(200, b"{}")
    buffer = bytearray(raw[:-1])
    assert parse_frame(buffer) is None
    assert buffer == raw[:-1]

    buffer += raw[-1:]
    frame = parse_frame(buffer)
    assert frame is not None
    assert frame.kind == "response"
    assert frame.status == 200
    assert frame.body == b"{}"
    assert frame.received_ns > 0
    assert not buffer  # the frame bytes were consumed


def test_unexpected_start_line_raises() -> None:
    """Data that is neither an HTTP response nor an EVENT frame is rejected."""
    buffer = bytearray(b"GARBAGE/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(RuntimeError, match="unexpected data"):
        parse_frame(buffer)


def test_event_frame_is_parsed() -> None:
    """An EVENT/1.0 push frame is recognized as an event frame."""
    body = b'{"characteristics": []}'
    raw = (
        b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )
    frame = parse_frame(bytearray(raw))
    assert frame is not None
    assert frame.kind == "event"
    assert frame.status == 200
    assert frame.body == body


def test_multiple_frames_in_one_buffer() -> None:
    """Several frames delivered in one read are split off one by one."""
    first = _response_frame(200, b"a")
    second = _response_frame(204, b"")
    buffer = bytearray(first + second)
    frame_one = parse_frame(buffer)
    frame_two = parse_frame(buffer)
    assert frame_one is not None and frame_one.body == b"a"
    assert frame_two is not None and frame_two.status == 204 and frame_two.body == b""
    assert not buffer
    assert parse_frame(buffer) is None


def test_header_names_are_case_insensitive_and_optional() -> None:
    """Content-Length matching is case/space insensitive; absent means 0."""
    raw = b"HTTP/1.1 200 OK\r\ncontent-LENGTH : 3\r\n\r\nabc"
    frame = parse_frame(bytearray(raw))
    assert frame is not None
    assert frame.body == b"abc"

    no_length = b"HTTP/1.1 204 No Content\r\nContent-Type: x\r\n\r\n"
    frame = parse_frame(bytearray(no_length))
    assert frame is not None
    assert frame.status == 204
    assert frame.body == b""
