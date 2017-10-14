import socket
import os
import binascii

#TODO: try not to talk 8888 for this
def get_local_address():
   s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   try:
      s.connect(("8.8.8.8", 80))
      addr = s.getsockname()[0]
   finally:
      s.close()
   return addr

def long_to_bytes(n):
    l = list()
    x = 0
    off = 0
    while x != n:
        b = (n >> off) & 0xFF
        l.append(b)
        x = x | (b << off)
        off += 8
    l.reverse()
    return bytes(l)

def generate_mac():
   return "{}{}:{}{}:{}{}:{}{}:{}{}:{}{}".format(
               *str(binascii.hexlify(os.urandom(6)), "utf-8").upper())