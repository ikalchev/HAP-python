"""Classic HomeKit Secure Video recording transfer over HDS ``dataSend``.

Once a controller has set up an HDS connection and completed the ``control``
handshake, it opens a ``dataSend`` stream of type ``ipcamera.recording``. The
accessory answers and then streams fragmented-MP4 recording fragments as
``dataSend``/``data`` events: the first fragment is the MP4 initialization
segment (``moov``), the rest are media fragments. Fragments larger than a chunk
are split across several events and reassembled by the controller using the
per-chunk metadata.

The fragments themselves come from a delegate: an async generator yielding
:class:`RecordingPacket` objects. This module owns the protocol; producing the
MP4 bytes is the accessory's job.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import IntEnum
import functools
import logging

from pyhap.hds_protocol import HDSStatus, Message

logger = logging.getLogger(__name__)

_DATA_SEND = "dataSend"
_TYPE_RECORDING = "ipcamera.recording"
_TARGET_CONTROLLER = "controller"

# The controller reassembles chunks; this only bounds a single HDS frame.
DEFAULT_CHUNK_SIZE = 0x40000

MEDIA_INITIALIZATION = "mediaInitialization"
MEDIA_FRAGMENT = "mediaFragment"


class RecordingReason(IntEnum):
    NORMAL = 0
    NOT_ALLOWED = 1
    BUSY = 2
    CANCELLED = 3
    UNSUPPORTED = 4
    UNEXPECTED_FAILURE = 5
    TIMEOUT = 6
    BAD_DATA = 7
    PROTOCOL_ERROR = 8
    INVALID_CONFIGURATION = 9


@dataclass
class RecordingPacket:
    """One recording fragment produced by the delegate."""

    data: bytes
    is_last: bool = False


# A delegate is called with the stream id and yields RecordingPackets. The first
# yielded packet is the MP4 initialization segment.
RecordingDelegate = Callable[[int], AsyncIterator[RecordingPacket]]
# An open gate returns a reason to reject a dataSend/open, or None to accept it.
OpenGate = Callable[["RecordingStreamManager"], RecordingReason | None]


class RecordingStreamManager:
    """Handle ``dataSend`` recording streams on one HDS connection."""

    def __init__(
        self,
        connection,
        delegate: RecordingDelegate,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        open_gate: OpenGate | None = None,
    ) -> None:
        self._connection = connection
        self._delegate = functools.partial(self._guard_delegate, delegate)
        self._chunk_size = chunk_size
        self._open_gate = open_gate
        self._stream_id: int | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._stopping = False
        connection.add_request_handler(_DATA_SEND, "open", self._handle_open)
        # The controller sends close and ack as events, not requests.
        connection.add_event_handler(_DATA_SEND, "close", self._handle_close)
        connection.add_event_handler(_DATA_SEND, "ack", self._handle_ack)
        # Stop the recording task (and its delegate/subprocess) if the controller
        # drops the connection instead of sending a clean dataSend/close.
        connection.add_close_callback(lambda _connection: self._stop())

    @staticmethod
    def _reject(reason: RecordingReason):
        # A rejected request carries HDSStatus.PROTOCOL_SPECIFIC_ERROR in the
        # header and the specific reason in the message body.
        return HDSStatus.PROTOCOL_SPECIFIC_ERROR, {"status": reason}

    @property
    def streaming(self) -> bool:
        """Whether a stream runs and is not being stopped."""
        task = self._task
        return task is not None and not task.done() and not self._stopping

    async def _guard_delegate(
        self, delegate: RecordingDelegate, stream_id: int
    ) -> AsyncIterator[RecordingPacket]:
        # Tell the controller when the delegate raises or runs out early instead
        # of leaving it waiting. The stream stops iterating after the last
        # packet, so falling off the loop means the delegate ended too soon.
        generator = delegate(stream_id)
        try:
            async for packet in generator:
                yield packet
        except Exception:
            self._send_failure(stream_id)
            raise
        finally:
            # The stream only closes this wrapper, so close the delegate here.
            aclose = getattr(generator, "aclose", None)
            if aclose is not None:
                await aclose()
        self._send_failure(stream_id)

    def _send_failure(self, stream_id: int) -> None:
        if not self._closed:
            self._connection.send_event(
                _DATA_SEND,
                "close",
                {"streamId": stream_id, "reason": RecordingReason.UNEXPECTED_FAILURE},
            )

    def _handle_open(self, message: Message):
        body = message.message
        if not isinstance(body, dict) or "streamId" not in body:
            return self._reject(RecordingReason.UNEXPECTED_FAILURE)
        if (
            body.get("target") != _TARGET_CONTROLLER
            or body.get("type") != _TYPE_RECORDING
        ):
            return self._reject(RecordingReason.UNEXPECTED_FAILURE)
        if self._open_gate is not None:
            reason = self._open_gate(self)
            if reason is not None:
                return self._reject(reason)
        # On one connection a new stream waits for the old task's cleanup, as
        # both share this manager's stream state.
        if self._task is not None and not self._task.done():
            return self._reject(RecordingReason.BUSY)
        self._stream_id = body["streamId"]
        self._closed = False
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._stream())
        return HDSStatus.SUCCESS, {}

    def _handle_close(self, message: Message) -> None:
        # A stale or duplicate close for an earlier stream must not stop the
        # currently running one.
        if self._is_current_stream(message):
            logger.debug(
                "Recording stream %s closed by controller, reason %s",
                self._stream_id,
                message.message.get("reason"),
            )
            self._stop()

    def _handle_ack(self, message: Message) -> None:
        # The controller acknowledges endOfStream, which completes the stream.
        if self._is_current_stream(message):
            self._stop()

    def _is_current_stream(self, message: Message) -> bool:
        body = message.message
        return isinstance(body, dict) and body.get("streamId") == self._stream_id

    def _stop(self) -> None:
        # Cancel at most once: _stop() is reached from both dataSend/close and
        # connection loss, and a second cancel() landing inside the stream's
        # `finally: await generator.aclose()` would abort the delegate's own
        # cleanup (e.g. reaping its ffmpeg subprocess) with a CancelledError.
        self._closed = True
        task = self._task
        if task is not None and not task.done() and not self._stopping:
            self._stopping = True
            task.cancel()

    async def _stream(self) -> None:
        self._connection.track_task(asyncio.current_task())
        assert self._stream_id is not None  # noqa: S101
        generator = self._delegate(self._stream_id)
        try:
            sequence_number = 1
            async for packet in generator:
                if self._closed:
                    break
                await self._send_fragment(
                    packet.data,
                    sequence_number,
                    initialization=sequence_number == 1,
                    is_last=packet.is_last,
                )
                if packet.is_last:
                    break
                sequence_number += 1
        except Exception:
            logger.warning("Recording stream %s failed", self._stream_id, exc_info=True)
        finally:
            # A close, ack or connection loss from here on must not cancel the
            # delegate's cleanup below.
            self._stopping = True
            # Deterministically run the delegate's cleanup (e.g. terminate its
            # ffmpeg subprocess) even when the controller dropped the connection.
            aclose = getattr(generator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                # aclose() may raise anything; the connection is gone
                # either way, so cleanup is best-effort.
                except Exception:  # noqa: S110, BLE001
                    pass

    async def _send_fragment(
        self, fragment: bytes, sequence_number: int, initialization: bool, is_last: bool
    ) -> None:
        offset = 0
        chunk_sequence_number = 1
        total = len(fragment)
        # A zero-length fragment still needs a single event to carry its metadata.
        while True:
            # Respect transport write back-pressure so a slow controller cannot
            # make us buffer an entire multi-megabyte fragment in memory.
            await self._connection.drain()
            if self._closed:
                return
            chunk = fragment[offset : offset + self._chunk_size]
            offset += len(chunk)
            last_chunk = offset >= total
            metadata = {
                "dataType": MEDIA_INITIALIZATION if initialization else MEDIA_FRAGMENT,
                "dataSequenceNumber": sequence_number,
                "dataChunkSequenceNumber": chunk_sequence_number,
                "isLastDataChunk": last_chunk,
            }
            if chunk_sequence_number == 1:
                metadata["dataTotalSize"] = total
            event = {
                "streamId": self._stream_id,
                "packets": [{"data": chunk, "metadata": metadata}],
            }
            if last_chunk and is_last:
                event["endOfStream"] = True
            self._connection.send_event(_DATA_SEND, "data", event)
            chunk_sequence_number += 1
            if last_chunk:
                break


# Cap what is buffered, so a non-MP4 source or a corrupt box size fails at
# once instead of being buffered until the source ends. Real init segments
# are a few KiB. A 10 s fragment at 50 Mbit/s is about 60 MiB.
_MAX_INIT_SIZE = 4 * 1024 * 1024
_MAX_FRAGMENT_SIZE = 64 * 1024 * 1024


async def _read_mp4_box(reader: asyncio.StreamReader, max_size: int) -> bytes | None:
    """Read one top-level MP4 box (header + body) from ``reader``.

    Returns ``None`` at end of stream (also mid-box) and for a size smaller
    than the header, including 0 (box runs to end of file). Raises
    ``ValueError`` for a box larger than ``max_size``, before reading its body.
    """
    try:
        header = await reader.readexactly(8)
        size = int.from_bytes(header[0:4], "big")
        if size == 1:  # 64-bit extended size
            header += await reader.readexactly(8)
            size = int.from_bytes(header[8:16], "big")
        if size < len(header):
            return None
        if size > max_size:
            raise ValueError(f"MP4 box of {size} bytes exceeds limit of {max_size}")
        return header + await reader.readexactly(size - len(header))
    except asyncio.IncompleteReadError:
        return None


async def fmp4_recording_packets(
    reader: asyncio.StreamReader,
) -> AsyncIterator[RecordingPacket]:
    """Turn a fragmented-MP4 byte stream into HKSV recording packets.

    Reads MP4 boxes from ``reader`` - any object with an ``readexactly``
    coroutine, e.g. an ffmpeg subprocess' ``stdout`` or an aiohttp/go2rtc
    response body. Everything up to and including ``moov`` is yielded as the
    initialization packet once ``moov`` is complete, and each ``moof``+``mdat``
    fragment once its ``mdat`` is complete. Fragments must have one ``mdat``
    per ``moof`` (CMAF style, as ffmpeg writes them). Other boxes between
    fragments (``styp``, ``sidx``, ffmpeg's trailing ``mfra``, a second
    ``mdat``) are dropped. End of stream counts as a normal end and is
    signalled with an empty packet flagged ``is_last``. A caller that knows
    the source failed (e.g. from ffmpeg's exit code) should raise instead.
    Raises ``ValueError`` if no ``moov`` shows up within ``_MAX_INIT_SIZE``
    bytes or a fragment exceeds ``_MAX_FRAGMENT_SIZE`` bytes.

    This is source-agnostic: point it at whatever produces keyframe-aligned
    fragmented MP4 whose fragment length matches the selected recording
    configuration. It never owns the source - the caller starts and stops it
    (e.g. terminates the ffmpeg subprocess in its own ``finally``), which the
    recording stream lifecycle already drives via the delegate's ``aclose()``.
    """
    init = bytearray()
    sent_init = False
    fragment: bytearray | None = None
    warned_mdat = False
    while True:
        if sent_init:
            max_size = _MAX_FRAGMENT_SIZE - len(fragment or b"")
        else:
            max_size = _MAX_INIT_SIZE - len(init)
        box = await _read_mp4_box(reader, max_size)
        if box is None:
            break
        box_type = bytes(box[4:8])
        if not sent_init:
            init += box
            if box_type == b"moov":
                yield RecordingPacket(bytes(init))
                sent_init = True
        elif box_type == b"moof":
            fragment = bytearray(box)
        elif fragment is not None:
            fragment += box
            if box_type == b"mdat":
                yield RecordingPacket(bytes(fragment))
                fragment = None
        elif box_type == b"mdat" and not warned_mdat:
            # Once per stream: several mdat per moof would hit this every fragment.
            logger.warning("Dropping mdat outside a fragment, expected one per moof")
            warned_mdat = True
    # Fragments go out before we know which one is last, so end with an empty
    # packet. An incomplete fragment is dropped.
    if sent_init:
        yield RecordingPacket(b"", is_last=True)
