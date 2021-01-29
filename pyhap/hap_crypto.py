"""This module partially implements crypto for HAP."""
import logging
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

CRYPTO_BACKEND = default_backend()


class HAP_CRYPTO:
    HKDF_KEYLEN = 32  # bytes, length of expanded HKDF keys
    HKDF_HASH = hashes.SHA512()  # Hash function to use in key expansion
    TAG_LENGTH = 16  # ChaCha20Poly1305 tag length
    TLS_NONCE_LEN = 12  # bytes, length of TLS encryption nonce


def pad_tls_nonce(nonce, total_len=HAP_CRYPTO.TLS_NONCE_LEN):
    """Pads a nonce with zeroes so that total_len is reached."""
    return nonce.rjust(total_len, b"\x00")


def hap_hkdf(key, salt, info):
    """Just a shorthand."""
    hkdf = HKDF(
        algorithm=HAP_CRYPTO.HKDF_HASH,
        length=HAP_CRYPTO.HKDF_KEYLEN,
        salt=salt,
        info=info,
        backend=CRYPTO_BACKEND,
    )
    return hkdf.derive(key)


class HAPCrypto:
    """A wrapper for HAP crypt protocol."""

    MAX_BLOCK_LENGTH = 0x400
    LENGTH_LENGTH = 2
    MIN_PAYLOAD_LENGTH = 1  # This is probably larger, but its only an optimization
    MIN_BLOCK_LENGTH = LENGTH_LENGTH + HAP_CRYPTO.TAG_LENGTH + MIN_PAYLOAD_LENGTH

    CIPHER_SALT = b"Control-Salt"
    OUT_CIPHER_INFO = b"Control-Read-Encryption-Key"
    IN_CIPHER_INFO = b"Control-Write-Encryption-Key"

    def __init__(self, shared_key) -> None:
        self._out_count = 0
        self._in_count = 0
        self._crypt_in_buffer = bytearray()  # Encrypted buffer
        self.reset(shared_key)

    def reset(self, shared_key):
        """Setup the ciphers."""
        self._out_cipher = ChaCha20Poly1305(
            hap_hkdf(shared_key, self.CIPHER_SALT, self.OUT_CIPHER_INFO)
        )
        self._in_cipher = ChaCha20Poly1305(
            hap_hkdf(shared_key, self.CIPHER_SALT, self.IN_CIPHER_INFO)
        )

    def receive_data(self, buffer: bytes) -> None:
        """Receive data into the encrypted buffer."""
        self._crypt_in_buffer += buffer

    def decrypt(self) -> bytes:
        """Decrypt and return any complete blocks in the buffer as plaintext

        The received full cipher blocks are decrypted and returned and partial cipher
        blocks are buffered locally.
        """
        result = b""

        while len(self._crypt_in_buffer) > self.MIN_BLOCK_LENGTH:
            block_length_bytes = self._crypt_in_buffer[: self.LENGTH_LENGTH]
            block_size = struct.unpack("H", block_length_bytes)[0]
            block_size_with_length = (
                self.LENGTH_LENGTH + block_size + HAP_CRYPTO.TAG_LENGTH
            )

            if len(self._crypt_in_buffer) < block_size_with_length:
                logger.debug("Incoming buffer does not have the full block")
                return result

            # Trim off the length
            del self._crypt_in_buffer[: self.LENGTH_LENGTH]

            data_size = block_size + HAP_CRYPTO.TAG_LENGTH
            nonce = pad_tls_nonce(struct.pack("Q", self._in_count))

            result += self._in_cipher.decrypt(
                nonce,
                bytes(self._crypt_in_buffer[:data_size]),
                bytes(block_length_bytes),
            )

            self._in_count += 1

            # Now trim out the decrypted data
            del self._crypt_in_buffer[:data_size]

        return result

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt and send the return bytes."""
        result = b""
        offset = 0
        total = len(data)
        while offset < total:
            length = min(total - offset, self.MAX_BLOCK_LENGTH)
            length_bytes = struct.pack("H", length)
            block = bytes(data[offset : offset + length])
            nonce = pad_tls_nonce(struct.pack("Q", self._out_count))
            ciphertext = length_bytes + self._out_cipher.encrypt(
                nonce, block, length_bytes
            )
            offset += length
            self._out_count += 1
            result += ciphertext
        return result
