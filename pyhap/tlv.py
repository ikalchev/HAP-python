# Encodes and decodes Tag-Length-Value data.
import struct


def encode(*args):
    assert len(args) % 2 == 0

    pieces = []
    for x in range(0, len(args), 2):
        tag = args[x]
        data = args[x + 1]
        total_length = len(data)
        if len(data) <= 255:
            encoded = tag + struct.pack("B", total_length) + data
        else:
            encoded = b""
            for x in range(0, total_length // 255):
                encoded = encoded + tag + b'\xFF' + data[x * 255: (x + 1) * 255]
            remaining = total_length % 255
            encoded = encoded + tag + struct.pack("B", remaining) \
                + data[-remaining:]

        pieces.append(encoded)

    return b"".join(pieces)


def decode(data):

    objects = {}
    current = 0
    while current < len(data):
        # The following hack is because bytes[x] is an int
        # and we want to keep the tag as a byte.
        tag = data[current: current + 1]
        length = data[current + 1]
        value = data[current + 2: current + 2 + length]
        if tag in objects:
            objects[tag] = objects[tag] + value
        else:
            objects[tag] = value

        current = current + 2 + length

    return objects
