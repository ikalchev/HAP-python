"""Tests for splitting a fragmented-MP4 stream into recording packets."""

# pylint: disable=protected-access

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhap import hds_recording
from pyhap.hds_recording import fmp4_recording_packets


def _box(box_type, body=b"\x00" * 8):
    return (8 + len(body)).to_bytes(4, "big") + box_type + body


FTYP = _box(b"ftyp")
MOOV = _box(b"moov", b"\x01" * 20)
FRAGMENT_1 = _box(b"moof", b"\x02" * 12) + _box(b"mdat", b"\x03" * 40)
FRAGMENT_2 = _box(b"moof", b"\x04" * 12) + _box(b"mdat", b"\x05" * 40)


def _reader(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _collect(reader):
    return [(p.data, p.is_last) async for p in fmp4_recording_packets(reader)]


@pytest.mark.asyncio
async def test_packets_yielded_without_waiting_for_next_box():
    """Init is yielded at moov and a fragment at its mdat, not at the next moof."""
    reader = asyncio.StreamReader()
    packets = fmp4_recording_packets(reader)

    reader.feed_data(FTYP + MOOV)
    init = await asyncio.wait_for(packets.__anext__(), 1)
    assert (init.data, init.is_last) == (FTYP + MOOV, False)

    reader.feed_data(FRAGMENT_1)
    fragment = await asyncio.wait_for(packets.__anext__(), 1)
    assert (fragment.data, fragment.is_last) == (FRAGMENT_1, False)

    reader.feed_eof()
    last = await asyncio.wait_for(packets.__anext__(), 1)
    assert (last.data, last.is_last) == (b"", True)
    with pytest.raises(StopAsyncIteration):
        await packets.__anext__()


@pytest.mark.asyncio
async def test_split_reads_across_chunk_boundaries():
    """Boxes arriving in small chunks that cut headers and bodies are reassembled."""
    stream = FTYP + MOOV + FRAGMENT_1 + FRAGMENT_2
    reader = asyncio.StreamReader()

    async def feed():
        for offset in range(0, len(stream), 3):
            reader.feed_data(stream[offset : offset + 3])
            await asyncio.sleep(0)
        reader.feed_eof()

    feeder = asyncio.create_task(feed())
    packets = await _collect(reader)
    await feeder
    assert packets == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (FRAGMENT_2, False),
        (b"", True),
    ]


@pytest.mark.asyncio
async def test_boxes_between_fragments_and_trailing_mfra_dropped():
    """styp/sidx between fragments and ffmpeg's trailing mfra are not sent."""
    stream = (
        FTYP
        + MOOV
        + _box(b"styp")
        + _box(b"sidx")
        + FRAGMENT_1
        + FRAGMENT_2
        + _box(b"mfra", b"\x06" * 30)
    )
    assert await _collect(_reader(stream)) == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (FRAGMENT_2, False),
        (b"", True),
    ]


@pytest.mark.asyncio
async def test_extra_mdat_dropped_with_one_warning(caplog):
    """A second mdat per moof is dropped, with a single warning per stream."""
    extra = _box(b"mdat", b"\x08" * 16)
    stream = FTYP + MOOV + FRAGMENT_1 + extra + FRAGMENT_2 + extra
    assert await _collect(_reader(stream)) == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (FRAGMENT_2, False),
        (b"", True),
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "Dropping mdat outside a fragment, expected one per moof"
    ]


@pytest.mark.asyncio
async def test_new_moof_replaces_fragment_without_mdat():
    """A moof without mdat is discarded when the next moof starts a fragment."""
    stream = FTYP + MOOV + _box(b"moof", b"\x09" * 12) + FRAGMENT_1
    assert await _collect(_reader(stream)) == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (b"", True),
    ]


@pytest.mark.asyncio
async def test_box_size_below_header_ends_stream():
    """A box size smaller than its header (0 means up to EOF) ends the stream."""
    bad_headers = [size.to_bytes(4, "big") + b"moof" for size in (0, 4, 7)]
    # 64-bit form: size32 == 1, then a 64-bit size below its 16-byte header.
    bad_headers.append((1).to_bytes(4, "big") + b"moof" + (15).to_bytes(8, "big"))
    for header in bad_headers:
        bad = header + b"\x00" * 16
        assert await _collect(_reader(FTYP + MOOV + FRAGMENT_1 + bad + FRAGMENT_2)) == [
            (FTYP + MOOV, False),
            (FRAGMENT_1, False),
            (b"", True),
        ]


def _header(box_type, size):
    """Box header announcing ``size`` bytes, without the body."""
    return size.to_bytes(4, "big") + box_type


@pytest.mark.asyncio
async def test_init_cap_boundary():
    """Up to 4 MiB before moov are read, one more byte raises."""
    budget = 4 * 1024 * 1024 - len(FTYP)
    # The size check runs before the body is read, so a header is enough.
    assert not await _collect(_reader(FTYP + _header(b"free", budget)))
    with pytest.raises(ValueError):
        await _collect(_reader(FTYP + _header(b"free", budget + 1)))


@pytest.mark.asyncio
async def test_fragment_cap_boundary():
    """A moof+mdat fragment may be 64 MiB, one more byte raises."""
    moof = _box(b"moof")
    budget = 64 * 1024 * 1024 - len(moof)
    init = FTYP + MOOV + FRAGMENT_1
    assert await _collect(_reader(init + moof + _header(b"mdat", budget))) == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (b"", True),
    ]
    with pytest.raises(ValueError):
        await _collect(_reader(init + moof + _header(b"mdat", budget + 1)))


@pytest.mark.asyncio
async def test_non_mp4_source_raises_without_waiting_for_eof():
    """Text read as a box header has a huge size and fails at once."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"HTTP/1.1 404 Not Found\r\n")
    with pytest.raises(ValueError):
        await asyncio.wait_for(_collect(reader), 1)


@pytest.mark.asyncio
async def test_eof_drops_incomplete_fragment():
    """A fragment cut off by EOF (moof only, or truncated mdat) is not sent."""
    truncated = _box(b"moof") + _box(b"mdat", b"\x07" * 40)[:20]
    assert await _collect(_reader(FTYP + MOOV + FRAGMENT_1 + truncated)) == [
        (FTYP + MOOV, False),
        (FRAGMENT_1, False),
        (b"", True),
    ]
    assert await _collect(_reader(FTYP + MOOV + _box(b"moof"))) == [
        (FTYP + MOOV, False),
        (b"", True),
    ]


@pytest.mark.asyncio
async def test_eof_before_init_yields_nothing():
    """Without a complete moov there is no recording to end."""
    assert not await _collect(_reader(b""))
    assert not await _collect(_reader(FTYP + MOOV[:10]))


@pytest.mark.asyncio
async def test_empty_last_packet_ends_stream_on_the_wire():
    """The empty final packet is sent as one zero-length chunk with endOfStream."""
    connection = MagicMock()
    connection.drain = AsyncMock()

    async def delegate(_stream_id):
        async for packet in fmp4_recording_packets(_reader(FTYP + MOOV + FRAGMENT_1)):
            yield packet

    manager = hds_recording.RecordingStreamManager(connection, delegate)
    manager._stream_id = 7
    await manager._stream()

    events = [call.args[2] for call in connection.send_event.call_args_list]
    assert [e["packets"][0]["data"] for e in events] == [FTYP + MOOV, FRAGMENT_1, b""]
    assert [e.get("endOfStream") for e in events] == [None, None, True]
    last = events[-1]["packets"][0]["metadata"]
    assert last["dataType"] == hds_recording.MEDIA_FRAGMENT
    assert last["dataSequenceNumber"] == 3
    assert last["dataTotalSize"] == 0
    assert last["isLastDataChunk"] is True
