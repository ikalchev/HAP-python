"""Encodes and decodes Tag-Length-Value (tlv8) data."""

import struct
from typing import Any, Literal, cast, overload

from pyhap import util


@overload
def encode(*args: Any, to_base64: Literal[False] = ...) -> bytes: ...


@overload
def encode(*args: Any, to_base64: Literal[True]) -> str: ...


def encode(*args: Any, to_base64: bool = False) -> bytes | str:
    """Encode the given byte args in TLV format.

    :param args: Even-number, variable length positional arguments repeating a tag
        followed by a value.
    :type args: ``bytes``

    :param toBase64: Whether to encode the resuting TLV byte sequence to a base64 str.
    :type toBase64: ``bool``

    :return: The args in TLV format
    :rtype: ``bytes`` if ``toBase64`` is False and ``str`` otherwise.
    """
    arg_len = len(args)
    if arg_len % 2 != 0:
        raise ValueError(f"Even number of args expected ({arg_len} given)")

    pieces = []
    for x in range(0, len(args), 2):
        tag = args[x]
        data = args[x + 1]
        total_length = len(data)
        if len(data) <= 255:
            encoded = tag + struct.pack("B", total_length) + data
        else:
            encoded = b""
            for y in range(0, total_length // 255):
                encoded = encoded + tag + b"\xff" + data[y * 255 : (y + 1) * 255]
            remaining = total_length % 255
            if remaining:
                encoded = (
                    encoded + tag + struct.pack("B", remaining) + data[-remaining:]
                )

        pieces.append(encoded)

    result = b"".join(pieces)

    return util.to_base64_str(result) if to_base64 else result


@overload
def decode(data: bytes, from_base64: Literal[False] = ...) -> dict[bytes, Any]: ...


@overload
def decode(data: str, from_base64: Literal[True]) -> dict[bytes, Any]: ...


def decode(data: bytes | str, from_base64: bool = False) -> dict[bytes, Any]:
    """Decode the given TLV-encoded ``data`` to a ``dict``.

    :param from_base64: Whether the given ``data`` should be base64 decoded first.
    :type from_base64: ``bool``

    :return: A ``dict`` containing the tags as keys and the values as values.
    :rtype: ``dict``
    """
    if from_base64:
        data = util.base64_to_bytes(data)
    # Callers deliver bytes, or a base64 str that was just decoded above.
    data = cast("bytes", data)

    objects = {}
    chunked = isinstance(data, bytes)
    current = 0
    while current < len(data):
        if current + 2 > len(data):
            # A hostile or truncated buffer must fail loudly instead of
            # raising IndexError or silently mis-parsing.
            raise ValueError(f"Truncated TLV record at offset {current}")
        tag = data[current : current + 1]
        length = data[current + 1]
        end = current + 2 + length
        if end > len(data):
            raise ValueError(f"TLV value truncated at offset {current}")
        value = data[current + 2 : end]
        if chunked:
            chunks = objects.get(tag)
            if chunks is None:
                objects[tag] = [value]
            else:
                chunks.append(value)
        elif tag in objects:
            objects[tag] = objects[tag] + value
        else:
            objects[tag] = value
        current = end

    if chunked:
        return {tag: b"".join(chunks) for tag, chunks in objects.items()}
    return objects
