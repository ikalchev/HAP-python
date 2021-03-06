"""Encodes and decodes Tag-Length-Value (tlv8) data."""
import struct

from pyhap import util


def encode(*args, to_base64=False):
    """Encode the given byte args in TLV format.

    :param args: Even-number, variable length positional arguments repeating a tag
        followed by a value.
    :type args: ``bytes``

    :param toBase64: Whether to encode the resuting TLV byte sequence to a base64 str.
    :type toBase64: ``bool``

    :return: The args in TLV format
    :rtype: ``bytes`` if ``toBase64`` is False and ``str`` otherwise.
    """
    if len(args) % 2 != 0:
        raise ValueError("Even number of args expected (%d given)" % len(args))

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
                encoded = encoded + tag + b"\xFF" + data[y * 255 : (y + 1) * 255]
            remaining = total_length % 255
            encoded = encoded + tag + struct.pack("B", remaining) + data[-remaining:]

        pieces.append(encoded)

    result = b"".join(pieces)

    return util.to_base64_str(result) if to_base64 else result


def decode(data, from_base64=False):
    """Decode the given TLV-encoded ``data`` to a ``dict``.

    :param from_base64: Whether the given ``data`` should be base64 decoded first.
    :type from_base64: ``bool``

    :return: A ``dict`` containing the tags as keys and the values as values.
    :rtype: ``dict``
    """
    if from_base64:
        data = util.base64_to_bytes(data)

    objects = {}
    current = 0
    while current < len(data):
        # The following hack is because bytes[x] is an int
        # and we want to keep the tag as a byte.
        tag = data[current : current + 1]
        length = data[current + 1]
        value = data[current + 2 : current + 2 + length]
        if tag in objects:
            objects[tag] = objects[tag] + value
        else:
            objects[tag] = value

        current = current + 2 + length

    return objects
