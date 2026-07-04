"""HomeKit Data Stream (HDS) transport primitives.

HDS is the TCP side channel HomeKit Secure Video uses to move fMP4 recording
fragments (and other bulk payloads) off the HAP control connection. A controller
sets up the transport with SetupDataStreamTransport, connects to the advertised
TCP port and exchanges frames encrypted with keys derived from the HAP session's
shared secret plus a per-transport salt.

This module is protocol-only: key derivation, frame encryption/decryption and
the setup-request/response TLV shapes. The listener and the higher-level HDS
protocol (control/dataSend topics) build on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import struct
from typing import Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from pyhap import tlv
from pyhap.hap_crypto import hap_hkdf

# --- Setup Data Stream Transport (HAP R17 section 12) ---

TRANSPORT_TYPE_TCP = b"\x00"

# SetupDataStreamTransport write (Transfer Transport Configuration).
SETUP_TYPES = {
    "SESSION_COMMAND_TYPE": b"\x01",
    "TRANSPORT_TYPE": b"\x02",
    "CONTROLLER_KEY_SALT": b"\x03",
}

SESSION_COMMAND_START = b"\x00"

# SetupDataStreamTransport response.
SETUP_RESPONSE_TYPES = {
    "STATUS": b"\x01",
    "TRANSPORT_TYPE_SESSION_PARAMETERS": b"\x02",
    "ACCESSORY_KEY_SALT": b"\x03",
}

TRANSPORT_SESSION_PARAM_TCP_LISTENING_PORT = b"\x01"

SETUP_STATUS_SUCCESS = b"\x00"
SETUP_STATUS_GENERIC_ERROR = b"\x01"
SETUP_STATUS_BUSY = b"\x02"

# HDS derives its two directional keys from the HAP session shared secret salted
# with both key salts. The info strings are named from the controller's point of
# view: the controller READS (decrypts) what the accessory encrypts, so the
# accessory encrypts outgoing frames with the "Read" key and decrypts incoming
# frames with the "Write" key.
_KEY_INFO_ACCESSORY_ENCRYPT = b"HDS-Read-Encryption-Key"
_KEY_INFO_ACCESSORY_DECRYPT = b"HDS-Write-Encryption-Key"

_NONCE_LENGTH = 12
_TAG_LENGTH = 16
_FRAME_HEADER_LENGTH = 4
# The 24-bit length field allows up to 16 MiB, but real HDS frames are far
# smaller (recording chunks are 256 KiB, control frames tiny). Cap well above
# that so both the encoder and the decoder reject/bound anything larger,
# limiting how much a peer can make us buffer for a single frame.
_MAX_PAYLOAD_LENGTH = 0x100000  # 1 MiB


@dataclass
class SetupRequest:
    """Decoded SetupDataStreamTransport controller request."""

    controller_key_salt: bytes
    transport_type: bytes = TRANSPORT_TYPE_TCP
    session_command: bytes = SESSION_COMMAND_START

    @classmethod
    def decode(cls, data: bytes) -> "SetupRequest":
        objs = tlv.decode(data)
        return cls(
            controller_key_salt=objs[SETUP_TYPES["CONTROLLER_KEY_SALT"]],
            transport_type=objs.get(SETUP_TYPES["TRANSPORT_TYPE"], TRANSPORT_TYPE_TCP),
            session_command=objs.get(
                SETUP_TYPES["SESSION_COMMAND_TYPE"], SESSION_COMMAND_START
            ),
        )


def encode_setup_response(
    listening_port: int,
    accessory_key_salt: bytes,
    status: bytes = SETUP_STATUS_SUCCESS,
) -> bytes:
    """Encode a SetupDataStreamTransport response advertising the TCP port."""
    if status != SETUP_STATUS_SUCCESS:
        return tlv.encode(SETUP_RESPONSE_TYPES["STATUS"], status)
    session_params = tlv.encode(
        TRANSPORT_SESSION_PARAM_TCP_LISTENING_PORT,
        struct.pack("<H", listening_port),
    )
    return tlv.encode(
        SETUP_RESPONSE_TYPES["STATUS"],
        status,
        SETUP_RESPONSE_TYPES["TRANSPORT_TYPE_SESSION_PARAMETERS"],
        session_params,
        SETUP_RESPONSE_TYPES["ACCESSORY_KEY_SALT"],
        accessory_key_salt,
    )


def derive_keys(
    shared_secret: bytes, controller_key_salt: bytes, accessory_key_salt: bytes
) -> Tuple[bytes, bytes]:
    """Derive the (encrypt, decrypt) HDS keys from the HAP session shared secret.

    ``encrypt`` seals accessory->controller frames; ``decrypt`` opens
    controller->accessory frames. The salt is the concatenation of the two key
    salts, matching the controller's derivation.
    """
    salt = controller_key_salt + accessory_key_salt
    encrypt_key = hap_hkdf(shared_secret, salt, _KEY_INFO_ACCESSORY_ENCRYPT)
    decrypt_key = hap_hkdf(shared_secret, salt, _KEY_INFO_ACCESSORY_DECRYPT)
    return encrypt_key, decrypt_key


def new_key_salt() -> bytes:
    """Return a fresh 32-byte accessory key salt."""
    return os.urandom(32)


def _nonce(counter: int) -> bytes:
    # 96-bit nonce: four zero bytes then the 64-bit little-endian counter
    # (right-justified), the same layout the HAP control channel uses.
    return b"\x00\x00\x00\x00" + struct.pack("<Q", counter)


class HDSCrypto:
    """Frame encryption/decryption for one HDS transport.

    Each 4-byte frame header (a 1-byte type followed by a 24-bit big-endian
    payload length) is authenticated as additional data over an encrypted
    payload, with a per-direction monotonically increasing nonce counter.
    """

    def __init__(self, encrypt_key: bytes, decrypt_key: bytes) -> None:
        self._encrypt_cipher = ChaCha20Poly1305(encrypt_key)
        self._decrypt_cipher = ChaCha20Poly1305(decrypt_key)
        self._encrypt_count = 0
        self._decrypt_count = 0

    def encrypt_frame(self, payload: bytes, frame_type: int = 1) -> bytes:
        """Encrypt a payload into a full HDS frame (header + ciphertext + tag)."""
        if len(payload) > _MAX_PAYLOAD_LENGTH:
            raise ValueError("HDS payload too large for a single frame")
        header = bytes([frame_type]) + len(payload).to_bytes(3, "big")
        nonce = _nonce(self._encrypt_count)
        self._encrypt_count += 1
        ciphertext = self._encrypt_cipher.encrypt(nonce, payload, header)
        return header + ciphertext

    def decrypt_frame(self, buffer: bytearray) -> Optional[bytes]:
        """Decrypt one complete frame from the front of ``buffer``.

        Returns the plaintext payload and removes the frame from ``buffer``, or
        ``None`` when a full frame is not yet buffered (leaving ``buffer``
        untouched so the caller can retry once more data arrives).
        """
        if len(buffer) < _FRAME_HEADER_LENGTH:
            return None
        header = bytes(buffer[:_FRAME_HEADER_LENGTH])
        payload_length = int.from_bytes(header[1:4], "big")
        if payload_length > _MAX_PAYLOAD_LENGTH:
            # A hostile/broken peer must not be able to make us buffer an
            # arbitrarily large frame; reject well beyond any real HDS frame.
            raise ValueError("HDS frame exceeds the maximum payload length")
        frame_length = _FRAME_HEADER_LENGTH + payload_length + _TAG_LENGTH
        if len(buffer) < frame_length:
            return None
        ciphertext = bytes(buffer[_FRAME_HEADER_LENGTH:frame_length])
        nonce = _nonce(self._decrypt_count)
        plaintext = self._decrypt_cipher.decrypt(nonce, ciphertext, header)
        self._decrypt_count += 1
        del buffer[:frame_length]
        return plaintext
