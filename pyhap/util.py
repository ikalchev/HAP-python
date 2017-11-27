import socket
import os
import binascii


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
