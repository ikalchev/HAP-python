"""SFrame end-to-end media encryption (RFC 9605) for HomeKit WebRTC streams.

HomeKit protects WebRTC media with SFrame on top of SRTP. The controller
learns the accessory's send key from the "SFrame Configuration" TLV of the
Solicit Offer response and hands its own receive keys over with Update
Session.

The cipher suites are those Scrypted's HomeKit WebRTC plugin uses: two
AES-256-CTR + HMAC-SHA512 suites built with the RFC 9605 section 4.5.1
construction, suite 6 with an 80-bit tag for video and suite 8 with a 32-bit
tag for audio. The RFC 9605 suites 1-3 share the same construction with
AES-128-CTR + HMAC-SHA256 and are kept for the RFC test vectors.

Every RTP stream derives its own base key from the 32-byte HAP key, with the
stream's SSRC as HKDF salt, so the same key never encrypts two streams with
the same counter. A whole media frame is protected at once and then split into
RTP payloads that start with a one-byte descriptor (0x80 first, 0x40 last).

The header, key schedule and AEAD follow RFC 9605 and are checked against its
Appendix C test vectors. The HomeKit suites, the per-stream key and the
packetization match the byte layout of Scrypted's HomeKit WebRTC plugin
(https://github.com/koush/scrypted, ``plugins/webrtc``).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
import enum
import hmac
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_UINT64 = (1 << 64) - 1
HOMEKIT_KEY_LENGTH = 32
STREAM_KEY_INFO = b"SFrame 1.0 RTP Stream"
NONCE_LENGTH = 12

DESCRIPTOR_START = 0x80
DESCRIPTOR_END = 0x40
DEFAULT_MAX_PAYLOAD = 1200
REPLAY_WINDOW = 64
MAX_FRAME = 64 * 1024
MAX_RECEIVE_KEYS = 16
MAX_RECEIVE_STREAMS = 16


class SFrameError(Exception):
    """An SFrame frame could not be protected or unprotected."""


class SFrameSuite(enum.IntEnum):
    """SFrame cipher suites built on AES-CTR and HMAC (RFC 9605 4.5.1).

    The HomeKit suites 6 and 8 are not registered with IANA; they and the
    per-SSRC HKDF salt are what HomeKit controllers use, as observed with
    Scrypted's HomeKit WebRTC plugin.
    """

    AES_128_CTR_HMAC_SHA256_80 = 1
    AES_128_CTR_HMAC_SHA256_64 = 2
    AES_128_CTR_HMAC_SHA256_32 = 3
    HOMEKIT_VIDEO = 6
    HOMEKIT_AUDIO = 8


@dataclass(frozen=True)
class _SuiteParams:
    hash_name: str
    enc_key_length: int
    key_length: int
    tag_length: int


_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "sha256": hashes.SHA256,
    "sha512": hashes.SHA512,
}

_SUITES = {
    SFrameSuite.AES_128_CTR_HMAC_SHA256_80: _SuiteParams("sha256", 16, 48, 10),
    SFrameSuite.AES_128_CTR_HMAC_SHA256_64: _SuiteParams("sha256", 16, 48, 8),
    SFrameSuite.AES_128_CTR_HMAC_SHA256_32: _SuiteParams("sha256", 16, 48, 4),
    SFrameSuite.HOMEKIT_VIDEO: _SuiteParams("sha512", 32, 96, 10),
    SFrameSuite.HOMEKIT_AUDIO: _SuiteParams("sha512", 32, 96, 4),
}


def _hash(name: str) -> hashes.HashAlgorithm:
    # A KeyError for an unknown name: never fall back to another hash.
    return _HASHES[name]()


def _check_uint64(value: int) -> None:
    if not 0 <= value <= MAX_UINT64:
        raise SFrameError("SFrame integer exceeds uint64")


def encode_header(kid: int, counter: int) -> bytes:
    """Encode an SFrame header (RFC 9605 section 4.3)."""
    config = 0
    fields = []
    for value, shift in ((kid, 4), (counter, 0)):
        _check_uint64(value)
        if value <= 7:
            config |= value << shift
            continue
        length = (value.bit_length() + 7) // 8
        config |= (8 | (length - 1)) << shift
        fields.append(value.to_bytes(length, "big"))
    return bytes([config]) + b"".join(fields)


def decode_header(frame: bytes) -> tuple[int, int, int]:
    """Decode an SFrame header and return ``(kid, counter, header_length)``."""
    if not frame:
        raise SFrameError("Missing SFrame header")
    offset = 1
    values = []
    for field in (frame[0] >> 4, frame[0] & 0x0F):
        if not field & 8:
            values.append(field)
            continue
        length = (field & 7) + 1
        if offset + length > len(frame):
            raise SFrameError("Truncated SFrame header")
        values.append(int.from_bytes(frame[offset : offset + length], "big"))
        offset += length
    return values[0], values[1], offset


@dataclass(frozen=True)
class SFrameKeys:
    """The ``sframe_key`` and ``sframe_salt`` of one KID (RFC 9605 4.4.2)."""

    key: bytes
    salt: bytes


def derive_key_salt(base_key: bytes, kid: int, suite: SFrameSuite) -> SFrameKeys:
    """Derive the SFrame key and salt for a base key and KID."""
    _check_uint64(kid)
    params = _SUITES[SFrameSuite(suite)]
    suffix = struct.pack(">QH", kid, suite)

    def derive(label: bytes, length: int) -> bytes:
        return HKDF(
            algorithm=_hash(params.hash_name),
            length=length,
            salt=None,
            info=label + suffix,
        ).derive(base_key)

    return SFrameKeys(
        key=derive(b"SFrame 1.0 Secret key ", params.key_length),
        salt=derive(b"SFrame 1.0 Secret salt ", NONCE_LENGTH),
    )


def derive_stream_base_key(key: bytes, ssrc: int) -> bytes:
    """Derive the per-RTP-stream base key from a 32-byte HomeKit key."""
    if len(key) != HOMEKIT_KEY_LENGTH:
        raise SFrameError("HomeKit SFrame key must be 32 bytes")
    return HKDF(
        algorithm=hashes.SHA512(),
        length=64,
        salt=struct.pack(">I", ssrc),
        info=STREAM_KEY_INFO,
    ).derive(key)


def derive_stream_keys(
    key: bytes, kid: int, ssrc: int, suite: SFrameSuite
) -> SFrameKeys:
    """Derive the SFrame key and salt of one RTP stream."""
    return derive_key_salt(derive_stream_base_key(key, ssrc), kid, suite)


def _tag(
    params: _SuiteParams, auth_key: bytes, nonce: bytes, aad: bytes, ct: bytes
) -> bytes:
    lengths = struct.pack(">QQQ", len(aad), len(ct), params.tag_length)
    mac = hmac.new(auth_key, lengths + nonce, params.hash_name)
    mac.update(aad)
    mac.update(ct)
    return mac.digest()[: params.tag_length]


def _ctr(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(enc_key), modes.CTR(nonce + bytes(4)))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def aead_encrypt(
    suite: SFrameSuite, key: bytes, nonce: bytes, aad: bytes, plaintext: bytes
) -> bytes:
    """AES-CTR + HMAC AEAD encryption (RFC 9605 section 4.5.1)."""
    params = _SUITES[SFrameSuite(suite)]
    enc_key, auth_key = key[: params.enc_key_length], key[params.enc_key_length :]
    ciphertext = _ctr(enc_key, nonce, plaintext)
    return ciphertext + _tag(params, auth_key, nonce, aad, ciphertext)


def aead_decrypt(
    suite: SFrameSuite, key: bytes, nonce: bytes, aad: bytes, ciphertext: bytes
) -> bytes:
    """AES-CTR + HMAC AEAD decryption (RFC 9605 section 4.5.1)."""
    params = _SUITES[SFrameSuite(suite)]
    if len(ciphertext) < params.tag_length:
        raise SFrameError("Truncated SFrame authentication tag")
    enc_key, auth_key = key[: params.enc_key_length], key[params.enc_key_length :]
    body, tag = ciphertext[: -params.tag_length], ciphertext[-params.tag_length :]
    if not hmac.compare_digest(_tag(params, auth_key, nonce, aad, body), tag):
        raise SFrameError("SFrame authentication failed")
    return _ctr(enc_key, nonce, body)


def _nonce(salt: bytes, counter: int) -> bytes:
    return (int.from_bytes(salt, "big") ^ counter).to_bytes(NONCE_LENGTH, "big")


def protect(
    keys: SFrameKeys,
    suite: SFrameSuite,
    kid: int,
    counter: int,
    plaintext: bytes,
    metadata: bytes = b"",
) -> bytes:
    """Encrypt one frame into ``header || ciphertext || tag`` (RFC 9605 4.4.3)."""
    header = encode_header(kid, counter)
    sealed = aead_encrypt(
        suite, keys.key, _nonce(keys.salt, counter), header + metadata, plaintext
    )
    return header + sealed


def unprotect(
    keys: SFrameKeys, suite: SFrameSuite, frame: bytes, metadata: bytes = b""
) -> bytes:
    """Decrypt one SFrame ciphertext (RFC 9605 4.4.4)."""
    _kid, counter, header_length = decode_header(frame)
    return aead_decrypt(
        suite,
        keys.key,
        _nonce(keys.salt, counter),
        frame[:header_length] + metadata,
        frame[header_length:],
    )


class SFrameSender:
    """Protects the frames of one outgoing RTP stream."""

    def __init__(self, key: bytes, kid: int, ssrc: int, suite: SFrameSuite) -> None:
        """Derive the stream keys; the counter starts at zero."""
        self.kid = kid
        self.ssrc = ssrc
        self.suite = SFrameSuite(suite)
        self._keys = derive_stream_keys(key, kid, ssrc, self.suite)
        self.counter = 0

    def protect(self, plaintext: bytes) -> bytes:
        """Protect the next frame."""
        # Exhaustion must fail rather than reuse a nonce with the same key.
        if self.counter > MAX_UINT64:
            raise SFrameError("SFrame counter exhausted")
        counter = self.counter
        self.counter += 1
        return protect(self._keys, self.suite, self.kid, counter, plaintext)


@dataclass
class _ReceiveStream:
    key: bytes
    keys: SFrameKeys
    highest: int = -1
    received: int = 0


class SFrameReceiver:
    """Unprotects incoming frames with the controller's receive keys.

    State is kept per KID and SSRC with a 64-frame replay window, so a frame
    that was already accepted, or is older than the window, is rejected. At
    most :data:`MAX_RECEIVE_KEYS` keys are held, and replay state for
    :data:`MAX_RECEIVE_STREAMS` streams; the least recently used stream is
    forgotten first.
    """

    def __init__(self, suite: SFrameSuite = SFrameSuite.HOMEKIT_AUDIO) -> None:
        """Start without keys."""
        self.suite = SFrameSuite(suite)
        self._keys: dict[int, bytes] = {}
        self._streams: OrderedDict[tuple[int, int], _ReceiveStream] = OrderedDict()

    @property
    def has_keys(self) -> bool:
        """Whether any receive key is installed."""
        return bool(self._keys)

    def update_keys(
        self, add: Iterable[tuple[bytes, int]], remove: Iterable[int]
    ) -> None:
        """Remove and then add ``(key, kid)`` receive keys.

        Everything is validated first; on an error no key changes.
        """
        added = [(bytes(key), kid) for key, kid in add]
        removed = set(remove)
        for key, kid in added:
            _check_uint64(kid)
            if len(key) != HOMEKIT_KEY_LENGTH:
                raise SFrameError("HomeKit SFrame key must be 32 bytes")
        kept = self._keys.keys() - removed
        if len(kept | {kid for _key, kid in added}) > MAX_RECEIVE_KEYS:
            raise SFrameError("Too many SFrame receive keys")
        for kid in removed:
            self._keys.pop(kid, None)
        for stream_id in [sid for sid in self._streams if sid[0] in removed]:
            del self._streams[stream_id]
        for key, kid in added:
            self._keys[kid] = key

    def clear(self) -> None:
        """Forget all keys and replay state."""
        self._keys.clear()
        self._streams.clear()

    def unprotect(self, frame: bytes, ssrc: int) -> bytes:
        """Authenticate and decrypt one frame of the given RTP stream."""
        kid, counter, _length = decode_header(frame)
        key = self._keys.get(kid)
        if key is None:
            raise SFrameError("Unknown SFrame receive KID")
        stream = self._streams.get((kid, ssrc))
        if stream is None or stream.key != key:
            stream = _ReceiveStream(key, derive_stream_keys(key, kid, ssrc, self.suite))
        age = stream.highest - counter
        if age >= REPLAY_WINDOW or (age >= 0 and stream.received >> age & 1):
            raise SFrameError("Replayed SFrame frame")
        plaintext = unprotect(stream.keys, self.suite, frame)
        if counter > stream.highest:
            shift = counter - stream.highest
            stream.received = (
                1 if shift >= REPLAY_WINDOW else (stream.received << shift | 1)
            ) & MAX_UINT64
            stream.highest = counter
        else:
            stream.received |= 1 << age
        self._streams[(kid, ssrc)] = stream
        self._streams.move_to_end((kid, ssrc))
        while len(self._streams) > MAX_RECEIVE_STREAMS:
            self._streams.popitem(last=False)
        return plaintext


def packetize(frame: bytes, max_payload: int = DEFAULT_MAX_PAYLOAD) -> list[bytes]:
    """Split a protected frame into RTP payloads with an SFrame descriptor."""
    if max_payload < 2:
        raise ValueError("SFrame payload budget must include descriptor and data")
    chunk = max_payload - 1
    payloads = []
    for offset in range(0, max(len(frame), 1), chunk):
        descriptor = (DESCRIPTOR_START if offset == 0 else 0) | (
            DESCRIPTOR_END if offset + chunk >= len(frame) else 0
        )
        payloads.append(bytes([descriptor]) + frame[offset : offset + chunk])
    return payloads


class SFrameDepacketizer:
    """Reassembles protected frames from descriptor-prefixed RTP payloads.

    A frame over ``max_frame`` bytes is dropped.
    """

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        """Start between frames."""
        self.max_frame = max_frame
        self._parts: list[bytes] | None = None
        self._size = 0
        self._last_sequence: int | None = None

    def push(self, sequence: int, payload: bytes) -> bytes | None:
        """Add one RTP payload; return the frame once its last part arrived."""
        in_order = (
            self._last_sequence is not None
            and sequence == (self._last_sequence + 1) & 0xFFFF
        )
        self._last_sequence = sequence
        if not payload:
            self._parts = None
            return None
        descriptor = payload[0]
        if descriptor & DESCRIPTOR_START:
            self._parts = []
            self._size = 0
        elif self._parts is None or not in_order:
            self._parts = None
            return None
        self._size += len(payload) - 1
        if self._size > self.max_frame:
            self._parts = None
            return None
        self._parts.append(payload[1:])
        if not descriptor & DESCRIPTOR_END:
            return None
        frame = b"".join(self._parts)
        self._parts = None
        return frame
