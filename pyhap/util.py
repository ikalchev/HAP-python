import asyncio
import base64
import socket
import random
import binascii
import sys


ALPHANUM = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
HEX_DIGITS = '0123456789ABCDEF'

rand = random.SystemRandom()


def callback(func):
    """Decorator for non blocking functions."""
    setattr(func, "_pyhap_callback", True)
    return func


def get_local_address():
    """
    Grabs the local IP address using a socket.

    :return: Local IP Address in IPv4 format.
    :rtype: str
    """
    # TODO: try not to talk 8888 for this
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
    finally:
        s.close()
    return addr


def long_to_bytes(n):
    """
    Convert a ``long int`` to ``bytes``

    :param n: Long Integer
    :type n: int

    :return: ``long int`` in ``bytes`` format.
    :rtype: bytes
    """
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
    """
    Generates a fake mac address used in broadcast.

    :return: MAC address in format XX:XX:XX:XX:XX:XX
    :rtype: str
    """
    return "{}{}:{}{}:{}{}:{}{}:{}{}:{}{}".format(
        *(rand.choice(HEX_DIGITS) for _ in range(12)))


def generate_setup_id():
    """
    Generates a random Setup ID for an ``Accessory`` or ``Bridge``.

    Used in QR codes and the setup hash.

    :return: 4 digit alphanumeric code.
    :rtype: str
    """
    return ''.join([
        rand.choice(ALPHANUM)
        for i in range(4)
    ])


def generate_pincode():
    """
    Generates a random pincode.

    :return: pincode in format ``xxx-xx-xxx``
    :rtype: bytearray
    """
    return '{}{}{}-{}{}-{}{}{}'.format(
        *(rand.randint(0, 9) for i in range(8))
    ).encode('ascii')


def b2hex(bts):
    """Produce a hex string representation of the given bytes.

    :param bts: bytes to convert to hex.
    :type bts: bytes
    :rtype: str
    """
    return binascii.hexlify(bts).decode("ascii")


def hex2b(hex_str):
    """Produce bytes from the given hex string representation.

    :param hex: hex string
    :type hex: str
    :rtype: bytes
    """
    return binascii.unhexlify(hex_str.encode("ascii"))


tohex = bytes.hex if sys.version_info >= (3, 5) else b2hex
"""Python-version-agnostic tohex function. Equivalent to bytes.hex in python 3.5+.
"""

fromhex = bytes.fromhex if sys.version_info >= (3, 5) else hex2b
"""Python-version-agnostic fromhex function. Equivalent to bytes.fromhex in python 3.5+.
"""


def to_base64_str(bytes_input) -> str:
    return base64.b64encode(bytes_input).decode('utf-8')


def base64_to_bytes(str_input) -> bytes:
    return base64.b64decode(str_input.encode('utf-8'))


def byte_bool(boolv):
    return b'\x01' if boolv else b'\x00'


async def event_wait(event, timeout, loop=None):
    """Wait for the given event to be set or for the timeout to expire.

    :param event: The event to wait for.
    :type event: asyncio.Event

    :param timeout: The timeout for which to wait, in seconds.
    :type timeout: float

    :return: ``event.is_set()``
    :rtype: bool
    """
    try:
        await asyncio.wait_for(event.wait(), timeout, loop=loop)
    except asyncio.TimeoutError:
        pass
    return event.is_set()
