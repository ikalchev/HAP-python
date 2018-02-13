import socket
import os
import binascii
import sys


def get_local_address():
    # TODO: try not to talk 8888 for this
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
    finally:
        s.close()
    return addr


def long_to_bytes(n):
    byteList = list()
    x = 0
    off = 0
    while x != n:
        b = (n >> off) & 0xFF
        byteList.append(b)
        x = x | (b << off)
        off += 8
    byteList.reverse()
    return bytes(byteList)


def generate_mac():
    return "{}{}:{}{}:{}{}:{}{}:{}{}:{}{}".format(
               *str(binascii.hexlify(os.urandom(6)), "utf-8").upper())


def b2hex(bts):
    """Produce a hex string representation of the given bytes.

    @type bts: bytes
    @rtype: string
    """
    return binascii.hexlify(bts).decode("ascii")

def hex2b(hex):
    """Produce bytes from the given hex string representation.

    @type hex: string
    @rtype: bytes
    """
    return binascii.unhexlify(hex.encode("ascii"))

tohex = bytes.hex if sys.version_info >= (3, 5) else b2hex
"""Python-version-agnostic tohex function. Equivalent to bytes.hex in python 3.5+.
"""

fromhex = bytes.fromhex if sys.version_info >= (3, 5) else hex2b
"""Python-version-agnostic fromhex function. Equivalent to bytes.fromhex in python 3.5+.
"""
