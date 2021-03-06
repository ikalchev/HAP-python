# Server Side SRP implementation

import os
from .util import long_to_bytes


def bytes_to_long(s):
    # Bytes should be interpreted from left to right, hence the byteorder
    return int.from_bytes(s, byteorder="big")


# b    Secret ephemeral values (long)
# A    Public ephemeral values (long)
# Ab   Public ephemeral values (bytes)
# B    Public ephemeral values (long)
# Bb   Public ephemeral values (bytes)
# g    A generator modulo N (long)
# gb   A generator modulo N (bytes)
# I    Username (bytes)
# k    Multiplier parameter (long)
# N    Large safe prime (long)
# Nb   Large safe prime (bytes)
# p    Cleartext Password (bytes)
# s    Salt (bytes)
# u    Random scrambling parameter (bytes)
# v    Password verifier (long)


class Server:
    def __init__(self, ctx, u, p, s=None, v=None, b=None):
        self.hashfunc = ctx["hashfunc"]
        self.N = ctx["N"]
        self.Nb = long_to_bytes(self.N)
        self.g = ctx["g"]
        self.gb = long_to_bytes(self.g)
        self.N_len = ctx["N_len"]
        self.s = s or os.urandom(ctx["salt_len"])
        self.I = u  # noqa: E741
        self.p = p
        self.v = v or self._get_verifier()
        self.k = self._get_k()
        self.b = b or bytes_to_long(os.urandom(ctx["secret_len"]))
        self.B = self._derive_B()
        self.Bb = long_to_bytes(self.B)

        self.Ab = None
        self.A = None
        self.S = None
        self.Sb = None
        self.K = None
        self.Kb = None
        self.M = None
        self.u = None
        self.HAMK = None

    def _digest(self, data):
        return self.hashfunc(data).digest()

    def _hexdigest_int16(self, data):
        return int(self.hashfunc(data).hexdigest(), 16)

    def _derive_B(self):
        return (self.k * self.v + pow(self.g, self.b, self.N)) % self.N

    def _get_private_key(self):
        return self._hexdigest_int16(self.s + self._digest(self.I + b":" + self.p))

    def _get_verifier(self):
        return pow(self.g, self._get_private_key(), self.N)

    def _get_k(self):
        return self._hexdigest_int16(self.Nb + self._padN(self.gb))

    def _get_K(self):
        return self._hexdigest_int16(self.Sb)

    def _padN(self, bytestr):
        return bytestr.rjust(self.N_len // 8, b"\x00")

    def _derive_premaster_secret(self):
        self.u = self._hexdigest_int16(self._padN(self.Ab) + self._padN(self.Bb))
        Avu = self.A * pow(self.v, self.u, self.N)
        return pow(Avu, self.b, self.N)

    def _get_M(self):
        hN = self._digest(self.Nb)
        hG = self._digest(self.gb)
        hGroup = bytes(hN[i] ^ hG[i] for i in range(0, len(hN)))
        hU = self._digest(self.I)
        return self._digest(hGroup + hU + self.s + self.Ab + self.Bb + self.Kb)

    def set_A(self, bytes_A):
        self.A = bytes_to_long(bytes_A)
        self.Ab = bytes_A
        self.S = self._derive_premaster_secret()
        self.Sb = long_to_bytes(self.S)
        self.K = self._get_K()
        self.Kb = long_to_bytes(self.K)
        self.M = self._get_M()
        self.HAMK = self._get_HAMK()

    def _get_HAMK(self):
        return self._digest(self.Ab + self.M + self.Kb)

    def get_challenge(self):
        return (self.s, self.B)

    def verify(self, M):
        return self.HAMK if self.M == M else None

    def get_session_key(self):
        return self.K
