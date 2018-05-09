# An incomplete implementation of SRP (i.e. the server side of SRP).
# I remember there was a problem with an srp module that I used
# as a guideline.
# TODO: make it a complete implementation.
import os

#
# s - bytes
# x - int
# k - int
# K - int
# S - int
# u - bytes
# p - bytes


def padN(bytestr, ctx):
    return bytestr.rjust(ctx["N_len"] // 8, b'\x00')


def _bytes_to_long(s):
    n = ord(s[0])
    for b in (ord(x) for x in s[1:]):
        n = (n << 8) | b
    return n


def bytes_to_long(s):
    # Bytes should be interpreted from left to right, hence the byteorder
    return int.from_bytes(s, byteorder="big")


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


def get_x(u, p, s, ctx):
    hf = ctx["hashfunc"]()
    hf.update(u + b":" + p)
    up = hf.digest()
    hf = ctx["hashfunc"]()
    hf.update(s + up)
    return int(hf.hexdigest(), 16)


def get_verifier(u, p, s, ctx):
    x = get_x(u, p, s, ctx)
    return pow(ctx['g'], x, ctx['N'])


def get_k(ctx):
    hf = ctx["hashfunc"]()
    hf.update(long_to_bytes(ctx["N"]) + padN(long_to_bytes(ctx["g"]), ctx))
    return int(hf.hexdigest(), 16)


def get_session_key(S, ctx):
    hf = ctx['hashfunc']()
    hf.update(long_to_bytes(S))
    return int(hf.hexdigest(), 16)


class Server(object):

    def __init__(self, ctx, u, p, s=None, v=None):
        self.ctx = ctx
        self.u = u
        self.p = p
        self.s = s or os.urandom(self.ctx["salt_len"])
        self.v = v or get_verifier(u, p, self.s, self.ctx)
        self.k = get_k(ctx)
        self.b = bytes_to_long(os.urandom(256))  # TODO: specify length
        self.B = self.derive_B()

    def derive_B(self):
        return (self.k * self.v + pow(self.ctx["g"], self.b, self.ctx["N"])) \
            % self.ctx["N"]

    def set_A(self, bytes_A):
        self.A = int.from_bytes(bytes_A, byteorder="big")
        self.S = self.derive_premaster_secret()
        self.K = get_session_key(self.S, self.ctx)
        self.M = self.get_M()

    def get_challenge(self):
        return (self.s, self.B)

    def derive_premaster_secret(self):
        hf = self.ctx['hashfunc']()
        hf.update(padN(long_to_bytes(self.A), self.ctx) +
                  padN(long_to_bytes(self.B), self.ctx))
        U = int(hf.hexdigest(), 16)
        Avu = self.A * pow(self.v, U, self.ctx["N"])
        return pow(Avu, self.b, self.ctx["N"])

    def get_M(self):
        hf = self.ctx['hashfunc']()
        hf.update(long_to_bytes(self.ctx['N']))
        hN = hf.digest()
        hf = self.ctx['hashfunc']()
        hf.update(long_to_bytes(self.ctx['g']))
        hG = hf.digest()
        hGroup = bytes(hN[i] ^ hG[i] for i in range(0, len(hN)))
        hf = self.ctx['hashfunc']()
        hf.update(self.u)
        hU = hf.digest()
        hf = self.ctx['hashfunc']()
        hf.update(hGroup + hU + self.s + long_to_bytes(self.A) +
                  long_to_bytes(self.B) + long_to_bytes(self.K))
        return hf.digest()

    def verify(self, M):
        if self.M != M:
            return None
        self.HAMK = self.get_HAMK()
        return self.HAMK

    def get_HAMK(self):
        hf = self.ctx['hashfunc']()
        hf.update(long_to_bytes(self.A) + self.M + long_to_bytes(self.K))
        return hf.digest()

    def get_session_key(self):
        return self.K
