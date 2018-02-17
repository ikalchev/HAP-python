import socket
import random
import binascii
import sys


ALPHANUM = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
HEX_DIGITS = '0123456789ABCDEF'

rand = random.SystemRandom()


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
        *(rand.choice(HEX_DIGITS) for _ in range(12)))


def generate_setup_id():
    return ''.join([
        rand.choice(ALPHANUM)
        for i in range(4)
    ])


def generate_pincode():
    return '{}{}{}-{}{}-{}{}{}'.format(
        *(rand.randint(0, 9) for i in range(8))
    ).encode('ascii')


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
