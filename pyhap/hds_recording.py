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
from dataclasses import dataclass
from enum import IntEnum
import logging
from typing import AsyncIterator, Callable, Optional

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


class RecordingStreamManager:
    """Handle ``dataSend`` recording streams on one HDS connection."""

    def __init__(
        self,
        connection,
        delegate: RecordingDelegate,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._connection = connection
        self._delegate = delegate
        self._chunk_size = chunk_size
        self._stream_id: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._stopping = False
        connection.add_request_handler(_DATA_SEND, "open", self._handle_open)
        connection.add_request_handler(_DATA_SEND, "close", self._handle_close)
        connection.add_request_handler(_DATA_SEND, "ack", self._handle_ack)
        # Stop the recording task (and its delegate/subprocess) if the controller
        # drops the connection instead of sending a clean dataSend/close.
        connection.add_close_callback(lambda _connection: self._stop())

    @staticmethod
    def _reject(reason: RecordingReason):
        # A rejected request carries HDSStatus.PROTOCOL_SPECIFIC_ERROR in the
        # header and the specific reason in the message body.
        return HDSStatus.PROTOCOL_SPECIFIC_ERROR, {"status": reason}

    def _handle_open(self, message: Message):
        body = message.message
        if not isinstance(body, dict) or "streamId" not in body:
            return self._reject(RecordingReason.UNEXPECTED_FAILURE)
        if (
            body.get("target") != _TARGET_CONTROLLER
            or body.get("type") != _TYPE_RECORDING
        ):
            return self._reject(RecordingReason.UNEXPECTED_FAILURE)
        if self._task is not None and not self._task.done():
            return self._reject(RecordingReason.BUSY)
        self._stream_id = body["streamId"]
        self._closed = False
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._stream())
        return HDSStatus.SUCCESS, {}

    def _handle_close(self, message: Message):
        body = message.message
        # A stale or duplicate close for an earlier stream must not stop the
        # currently running one.
        if isinstance(body, dict) and body.get("streamId") != self._stream_id:
            return self._reject(RecordingReason.UNEXPECTED_FAILURE)
        self._stop()
        return HDSStatus.SUCCESS, {}

    def _handle_ack(self, message: Message):
        return HDSStatus.SUCCESS, {}

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
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Recording stream %s failed", self._stream_id, exc_info=True)
        finally:
            # Deterministically run the delegate's cleanup (e.g. terminate its
            # ffmpeg subprocess) even when the controller dropped the connection.
            aclose = getattr(generator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # pylint: disable=broad-exception-caught
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


async def _read_mp4_box(reader: asyncio.StreamReader) -> Optional[bytes]:
    """Read one top-level MP4 box (header + body) from ``reader``.

    Returns ``None`` at end of stream.
    """
    try:
        header = await reader.readexactly(8)
    except asyncio.IncompleteReadError:
        return None
    size = int.from_bytes(header[0:4], "big")
    if size == 1:  # 64-bit extended size
        ext = await reader.readexactly(8)
        size = int.from_bytes(ext, "big")
        body = await reader.readexactly(size - 16)
        return header + ext + body
    if size < 8:
        return None
    body = await reader.readexactly(size - 8)
    return header + body


async def fmp4_recording_packets(
    reader: asyncio.StreamReader,
) -> AsyncIterator["RecordingPacket"]:
    """Turn a fragmented-MP4 byte stream into HKSV recording packets.

    Reads MP4 boxes from ``reader`` - any object with an ``readexactly``
    coroutine, e.g. an ffmpeg subprocess' ``stdout`` or an aiohttp/go2rtc
    response body. The leading ``ftyp``+``moov`` boxes are yielded as the single
    initialization packet; each following ``moof``(+``mdat``) fragment is
    yielded as a media fragment, and the fragment produced right before end of
    stream is flagged ``is_last`` so the recording ends gracefully.

    This is source-agnostic: point it at whatever produces keyframe-aligned
    fragmented MP4 whose fragment length matches the selected recording
    configuration. It never owns the source - the caller starts and stops it
    (e.g. terminates the ffmpeg subprocess in its own ``finally``), which the
    recording stream lifecycle already drives via the delegate's ``aclose()``.
    """
    init = bytearray()
    sent_init = False
    pending: Optional[bytearray] = None
    while True:
        box = await _read_mp4_box(reader)
        if box is None:
            break
        box_type = bytes(box[4:8])
        if box_type in (b"ftyp", b"moov"):
            init += box
        elif box_type == b"moof":
            if not sent_init:
                yield RecordingPacket(bytes(init))
                sent_init = True
            # A new fragment starts; flush the previous one (not the last).
            if pending is not None:
                yield RecordingPacket(bytes(pending))
            pending = bytearray(box)
        elif pending is not None:
            # mdat (and any styp / other boxes) belong to the current fragment.
            pending += box
        elif not sent_init:
            init += box
    # End of stream: the buffered fragment is the last one.
    if pending is not None:
        yield RecordingPacket(bytes(pending), is_last=True)
