"""This module implements the communication of HAP.

The HAPServer is the point of contact to and from the world.
The HAPServerHandler manages the state of the connection and handles incoming requests.
The HAPSocket is a socket implementation that manages the "TLS" of the connection.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import socket
import struct
import json
import errno
import uuid
from urllib.parse import urlparse, parse_qs
import socketserver
import threading

from tlslite.utils.chacha20_poly1305 import CHACHA20_POLY1305
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import SHA512
import curve25519
import ed25519

import pyhap.tlv as tlv
from pyhap.util import long_to_bytes

logger = logging.getLogger(__name__)


# Various "tag" constants for HAP's TLV encoding.
class HAP_TLV_TAGS:
    REQUEST_TYPE = b'\x00'
    USERNAME = b'\x01'
    SALT = b'\x02'
    PUBLIC_KEY = b'\x03'
    PASSWORD_PROOF = b'\x04'
    ENCRYPTED_DATA = b'\x05'
    SEQUENCE_NUM = b'\x06'
    ERROR_CODE = b'\x07'
    PROOF = b'\x0A'


# Status codes for underlying HAP calls
class HAP_SERVER_STATUS:
    SUCCESS = 0
    INSUFFICIENT_PRIVILEGES = -70401
    SERVICE_COMMUNICATION_FAILURE = -70402
    RESOURCE_BUSY = -70403
    READ_ONLY_CHARACTERISTIC = -70404
    WRITE_ONLY_CHARACTERISTIC = -70405
    NOTIFICATION_NOT_SUPPORTED = -70406
    OUT_OF_RESOURCE = -70407
    OPERATION_TIMED_OUT = -70408
    RESOURCE_DOES_NOT_EXIST = -70409
    INVALID_VALUE_IN_REQUEST = -70410


# Error codes and the like, guessed by packet inspection
class HAP_OPERATION_CODE:
    INVALID_REQUEST = b'\x02'
    INVALID_SIGNATURE = b'\x04'


class HAP_CRYPTO:
    HKDF_KEYLEN = 32  # bytes, length of expanded HKDF keys
    HKDF_HASH = SHA512  # Hash function to use in key expansion
    TLS_NONCE_LEN = 12  # bytes, length of TLS encryption nonce


def _pad_tls_nonce(nonce, total_len=HAP_CRYPTO.TLS_NONCE_LEN):
    """Pads a nonce with zeroes so that total_len is reached."""
    return nonce.rjust(total_len, b"\x00")


def hap_hkdf(key, salt, info):
    """Just a shorthand."""
    return HKDF(key, HAP_CRYPTO.HKDF_KEYLEN, salt, HAP_CRYPTO.HKDF_HASH, context=info)


class UnprivilegedRequestException(Exception):
    pass


class NotAllowedInStateException(Exception):
    pass


class HAPServerHandler(BaseHTTPRequestHandler):
    """Manages HAP connection state and handles incoming HTTP requests."""

    # Mapping from paths to methods that handle them.
    HANDLERS = {
        "POST": {
            "/pair-setup": "handle_pairing",
            "/pair-verify": "handle_pair_verify",
            "/pairings": "handle_pairings",
        },

        "GET": {
            "/accessories": "handle_accessories",
            "/characteristics": "handle_get_characteristics",
        },

        "PUT": {
            "/characteristics": "handle_set_characteristics",
        }
    }

    PAIRING_RESPONSE_TYPE = "application/pairing+tlv8"
    JSON_RESPONSE_TYPE = "application/hap+json"

    PAIRING_3_SALT = b"Pair-Setup-Encrypt-Salt"
    PAIRING_3_INFO = b"Pair-Setup-Encrypt-Info"
    PAIRING_3_NONCE = _pad_tls_nonce(b"PS-Msg05")

    PAIRING_4_SALT = b"Pair-Setup-Controller-Sign-Salt"
    PAIRING_4_INFO = b"Pair-Setup-Controller-Sign-Info"

    PAIRING_5_SALT = b"Pair-Setup-Accessory-Sign-Salt"
    PAIRING_5_INFO = b"Pair-Setup-Accessory-Sign-Info"
    PAIRING_5_NONCE = _pad_tls_nonce(b"PS-Msg06")

    PVERIFY_1_SALT = b"Pair-Verify-Encrypt-Salt"
    PVERIFY_1_INFO = b"Pair-Verify-Encrypt-Info"
    PVERIFY_1_NONCE = _pad_tls_nonce(b"PV-Msg02")

    PVERIFY_2_NONCE = _pad_tls_nonce(b"PV-Msg03")

    def __init__(self, sock, client_addr, server, accessory_handler):
        """
        @param accessory_handler: An object that controls an accessory's state.
        @type accessory_handler: AccessoryDriver
        """
        self.accessory_handler = accessory_handler
        self.state = self.accessory_handler.state
        self.enc_context = None
        self.is_encrypted = False
        # Redirect separate handlers to the dispatch method
        self.do_GET = self.do_POST = self.do_PUT = self.dispatch

        super(HAPServerHandler, self).__init__(sock, client_addr, server)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def _set_encryption_ctx(self, client_public, private_key, public_key, shared_key,
                            pre_session_key):
        """Sets the encryption context.

        The encryption context is generated in pair verify step one and is used to
        create encrypted transported in pair verify step two.

        @param client_public: The client's session public key.
        @type client_public: bytes

        @param private_key: The state's session private key.
        @type private_key: bytes

        @param shared_key: The resulted session key.
        @type shared_key: bytes

        @param pre_session_key: The key used during session negotiation
            (pair verify one and two).
        @type pre_session_key: bytes
        """
        self.enc_context = {
            "client_public": client_public,
            "private_key": private_key,
            "public_key": public_key,
            "shared_key": shared_key,
            "pre_session_key": pre_session_key
        }

    def _upgrade_to_encrypted(self):
        """Set encryption for the underlying transport.

        @note: Replaces self.request, self.wfile and self.rfile.
        """
        self.request = self.server.upgrade_to_encrypted(self.client_address,
                                                        self.enc_context["shared_key"])
        # Recreate the file handles over the socket
        # TODO: consider calling super().setup(), although semantically not correct
        self.connection = self.request
        self.rfile = self.connection.makefile('rb', self.rbufsize)
        self.wfile = self.connection.makefile('wb')
        self.is_encrypted = True

    def end_response(self, bytesdata, close_connection=False):
        """Combines adding a length header and actually sending the data."""
        self.send_header("Content-Length", len(bytesdata))
        self.end_headers()
        self.wfile.write(bytesdata)
        self.close_connection = 1 if close_connection else 0

    def dispatch(self):
        """Dispatch the request to the appropriate handler method."""
        logger.debug("Request %s from address '%s' for path '%s'.",
                     self.command, self.client_address, self.path)
        path = urlparse(self.path).path
        assert path in self.HANDLERS[self.command]
        try:
            getattr(self, self.HANDLERS[self.command][path])()
        except NotAllowedInStateException:
            self.send_response(403)
        except UnprivilegedRequestException:
            response = {"status": HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES}
            data = json.dumps(response).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
            self.end_response(data)

    def handle_pairing(self):
        """Handles arbitrary step of the pairing process."""
        if self.state.paired:
            raise NotAllowedInStateException

        length = int(self.headers["Content-Length"])
        tlv_objects = tlv.decode(self.rfile.read(length))
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]

        if sequence == b'\x01':
            self._pairing_one()
        elif sequence == b'\x03':
            self._pairing_two(tlv_objects)
        elif sequence == b'\x05':
            self._pairing_three(tlv_objects)

    def _pairing_one(self):
        """Send the SRP salt and public key to the client.

        The SRP verifier is created at this step.
        """
        logger.debug("Pairing [1/5]")
        self.accessory_handler.setup_srp_verifier()
        salt, B = self.accessory_handler.srp_verifier.get_challenge()

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x02',
                          HAP_TLV_TAGS.SALT, salt,
                          HAP_TLV_TAGS.PUBLIC_KEY, long_to_bytes(B))

        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data, False)

    def _pairing_two(self, tlv_objects):
        """Obtain the challenge from the client (A) and client's proof that it
        knows the password (M). Verify M and generate the server's proof based on
        A (H_AMK). Send the H_AMK to the client.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("Pairing [2/5]")
        A = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        M = tlv_objects[HAP_TLV_TAGS.PASSWORD_PROOF]
        verifier = self.accessory_handler.srp_verifier
        verifier.set_A(A)

        hamk = verifier.verify(M)

        if hamk is None:  # Probably the provided pincode was wrong.
            response = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x04',
                                  HAP_TLV_TAGS.ERROR_CODE,
                                  HAP_OPERATION_CODE.INVALID_REQUEST)
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            self.end_response(response)
            return

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x04',
                          HAP_TLV_TAGS.PASSWORD_PROOF, hamk)
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

    def _pairing_three(self, tlv_objects):
        """Expand the SRP session key to obtain a new key. Use it to verify and decrypt
            the recieved data. Continue to step four.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("Pairing [3/5]")
        encrypted_data = tlv_objects[HAP_TLV_TAGS.ENCRYPTED_DATA]

        session_key = self.accessory_handler.srp_verifier.get_session_key()
        hkdf_enc_key = hap_hkdf(long_to_bytes(session_key),
                                self.PAIRING_3_SALT, self.PAIRING_3_INFO)

        cipher = CHACHA20_POLY1305(hkdf_enc_key, "python")
        decrypted_data = cipher.open(self.PAIRING_3_NONCE, bytearray(encrypted_data), b"")
        assert decrypted_data is not None

        dec_tlv_objects = tlv.decode(bytes(decrypted_data))
        client_username = dec_tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_ltpk = dec_tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        client_proof = dec_tlv_objects[HAP_TLV_TAGS.PROOF]

        self._pairing_four(client_username, client_ltpk, client_proof, hkdf_enc_key)

    def _pairing_four(self, client_username, client_ltpk, client_proof, encryption_key):
        """Expand the SRP session key to obtain a new key.
            Use it to verify that the client's proof of the private key. Continue to
            step five.

        @param client_username: The client's username.
        @type client_username: bytes.

        @param client_ltpk: The client's public key.
        @type client_ltpk: bytes

        @param client_proof: The client's proof of password.
        @type client_proof: bytes

        @param encryption_key: The encryption key for this step.
        @type encryption_key: bytes
        """
        logger.debug("Pairing [4/5]")
        session_key = self.accessory_handler.srp_verifier.get_session_key()
        output_key = hap_hkdf(long_to_bytes(session_key),
                              self.PAIRING_4_SALT, self.PAIRING_4_INFO)

        data = output_key + client_username + client_ltpk
        verifying_key = ed25519.VerifyingKey(client_ltpk)

        try:
            verifying_key.verify(client_proof, data)
        except ed25519.BadSignatureError:
            logger.error("Bad signature, abort.")
            raise

        self._pairing_five(client_username, client_ltpk, encryption_key)

    def _pairing_five(self, client_username, client_ltpk, encryption_key):
        """At that point we know the client has the accessory password and has a valid key
        pair. Add it as a pair and send a sever proof.

        Parameters are as for _pairing_four.
        """
        logger.debug("Pairing [5/5]")
        session_key = self.accessory_handler.srp_verifier.get_session_key()
        output_key = hap_hkdf(long_to_bytes(session_key),
                              self.PAIRING_5_SALT, self.PAIRING_5_INFO)

        server_public = self.state.public_key.to_bytes()
        mac = self.state.mac.encode()

        material = output_key + mac + server_public
        private_key = self.state.private_key
        server_proof = private_key.sign(material)

        message = tlv.encode(HAP_TLV_TAGS.USERNAME, mac,
                             HAP_TLV_TAGS.PUBLIC_KEY, server_public,
                             HAP_TLV_TAGS.PROOF, server_proof)

        cipher = CHACHA20_POLY1305(encryption_key, "python")
        aead_message = bytes(
            cipher.seal(self.PAIRING_5_NONCE, bytearray(message), b""))

        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        should_confirm = self.accessory_handler.pair(client_uuid, client_ltpk)

        if not should_confirm:
            self.send_response(500)
            return

        tlv_data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x06',
                              HAP_TLV_TAGS.ENCRYPTED_DATA, aead_message)
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(tlv_data)

    def handle_pair_verify(self):
        """Handles arbitrary step of the pair verify process.

        Pair verify is session negotiation.
        """
        if not self.state.paired:
            raise NotAllowedInStateException

        length = int(self.headers["Content-Length"])
        tlv_objects = tlv.decode(self.rfile.read(length))
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]
        if sequence == b'\x01':
            self._pair_verify_one(tlv_objects)
        elif sequence == b'\x03':
            self._pair_verify_two(tlv_objects)
        else:
            raise

    def _pair_verify_one(self, tlv_objects):
        """Generate new session key pair and send a proof to the client.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("Pair verify [1/2].")
        client_public = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]

        private_key = curve25519.Private()
        public_key = private_key.get_public()
        shared_key = private_key.get_shared_key(
            curve25519.Public(client_public),
            # Key is hashed before being returned, we don't want it; This fixes that.
            lambda x: x)

        mac = self.state.mac.encode()
        material = public_key.serialize() + mac + client_public
        server_proof = self.state.private_key.sign(material)

        output_key = hap_hkdf(shared_key, self.PVERIFY_1_SALT, self.PVERIFY_1_INFO)

        self._set_encryption_ctx(client_public, private_key, public_key,
                                 shared_key, output_key)

        message = tlv.encode(HAP_TLV_TAGS.USERNAME, mac,
                             HAP_TLV_TAGS.PROOF, server_proof)

        cipher = CHACHA20_POLY1305(output_key, "python")
        aead_message = bytes(
            cipher.seal(self.PVERIFY_1_NONCE, bytearray(message), b""))
        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x02',
                          HAP_TLV_TAGS.ENCRYPTED_DATA, aead_message,
                          HAP_TLV_TAGS.PUBLIC_KEY, public_key.serialize())
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

    def _pair_verify_two(self, tlv_objects):
        """Verify the client proof and upgrade to encrypted transport.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("Pair verify [2/2]")
        encrypted_data = tlv_objects[HAP_TLV_TAGS.ENCRYPTED_DATA]
        cipher = CHACHA20_POLY1305(self.enc_context["pre_session_key"], "python")
        decrypted_data = cipher.open(self.PVERIFY_2_NONCE, bytearray(encrypted_data), b"")
        assert decrypted_data is not None  # TODO:

        dec_tlv_objects = tlv.decode(bytes(decrypted_data))
        client_username = dec_tlv_objects[HAP_TLV_TAGS.USERNAME]
        material = self.enc_context["client_public"] \
            + client_username \
            + self.enc_context["public_key"].serialize()

        client_uuid = uuid.UUID(str(client_username, "ascii"))
        perm_client_public = self.state.paired_clients.get(client_uuid)
        if perm_client_public is None:
            logger.debug("Client %s attempted pair verify without being paired first.",
                         client_uuid)
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            data = tlv.encode(HAP_TLV_TAGS.ERROR_CODE, HAP_OPERATION_CODE.INVALID_REQUEST)
            self.end_response(data)
            return

        verifying_key = ed25519.VerifyingKey(perm_client_public)
        try:
            verifying_key.verify(dec_tlv_objects[HAP_TLV_TAGS.PROOF], material)
        except ed25519.BadSignatureError:
            logger.error("Bad signature, abort.")
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            data = tlv.encode(HAP_TLV_TAGS.ERROR_CODE, HAP_OPERATION_CODE.INVALID_REQUEST)
            self.end_response(data)
            return

        logger.debug("Pair verify with client '%s' completed. Switching to "
                     "encrypted transport.", self.client_address)

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b'\x04')
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)
        self._upgrade_to_encrypted()
        del self.enc_context

    def handle_accessories(self):
        """Handles a client request to get the accessories."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        hap_rep = self.accessory_handler.get_accessories()
        data = json.dumps(hap_rep).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(data)

    def handle_get_characteristics(self):
        """Handles a client request to get certain characteristics."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        # Check that char exists and ...
        params = parse_qs(urlparse(self.path).query)
        chars = self.accessory_handler.get_characteristics(params["id"][0].split(","))

        data = json.dumps(chars).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(data)

    def handle_set_characteristics(self):
        """Handles a client request to update certain characteristics."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        # TODO: assert self.headers["authorization"] == state.pincode
        data_len = int(self.headers["Content-Length"])
        assert data_len > 0
        requested_chars = json.loads(
            self.rfile.read(data_len).decode("utf-8"))

        chars = self.accessory_handler.set_characteristics(requested_chars,
                                                           self.client_address)

        data = json.dumps(chars).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(data)

    def handle_pairings(self):
        """Handles a client request to update or remove a pairing."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        data_len = int(self.headers["Content-Length"])
        tlv_objects = tlv.decode(self.rfile.read(data_len))
        request_type = tlv_objects[HAP_TLV_TAGS.REQUEST_TYPE][0]
        if request_type == 3:
            self._handle_add_pairing(tlv_objects)
        elif request_type == 4:
            self._handle_remove_pairing(tlv_objects)
        else:
            raise ValueError

    def _handle_add_pairing(self, tlv_objects):
        """Update client information."""
        logger.debug("Adding client pairing.")
        client_username = tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_public = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        should_confirm = self.accessory_handler.pair(
            client_uuid, client_public)
        if not should_confirm:
            self.send_response(500)
            return

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b"\x02")
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

    def _handle_remove_pairing(self, tlv_objects):
        """Remove pairing with the client."""
        logger.debug("Removing client pairing.")
        client_username = tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        self.accessory_handler.unpair(client_uuid)

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b"\x02")
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)


class HAPSocket(socket.socket):
    """A socket implementing the HAP crypto. Just feed it as if it is a normal socket.

    @note: HAP requires something like HTTP push. This implies we can have regular HTTP
    response and an outbound HTTP push at the same time on the same socket - a race
    condition. Thus, HAPSocket implements exclusive access to send and sendall to deal
    with this situation.
    """

    MAX_BLOCK_LENGTH = 0x400
    LENGTH_LENGTH = 2

    CIPHER_SALT = b"Control-Salt"
    OUT_CIPHER_INFO = b"Control-Read-Encryption-Key"
    IN_CIPHER_INFO = b"Control-Write-Encryption-Key"

    def __init__(self, sock, shared_key):
        """Initialises this socket from the given socket."""
        socket.socket.__init__(self, sock.family, sock.type, sock.proto, sock.fileno())
        sock.detach()
        # See if we are connected
        try:
            self.getpeername()
        except OSError as e:
            if e.errno != errno.ENOTCONN:
                raise
            self._connected = False
        else:
            self._connected = True

        self._closed = False

        self.shared_key = shared_key
        self.out_count = 0
        self.in_count = 0
        self.out_cipher = None
        self.in_cipher = None
        self.out_lock = threading.RLock()  # for locking send operations
        # NOTE: Some future python implementation of HTTP Server or Server Handler can use
        # methods different than the ones we lock now (send, sendall).
        # This will break the encryption/decryption before introducing a race condition,
        # but don't forget locking these other methods after fixing the crypto.

        self._set_ciphers()
        self.curr_in_total = None  # Length of the current incoming block
        self.num_in_recv = None  # Number of bytes received from the incoming block
        self.curr_in_block = None  # Bytes of the current incoming block

    def _set_ciphers(self):
        """Generate out/inbound encryption keys and initialise respective ciphers."""
        outgoing_key = hap_hkdf(self.shared_key, self.CIPHER_SALT, self.OUT_CIPHER_INFO)
        self.out_cipher = CHACHA20_POLY1305(outgoing_key, "python")

        incoming_key = hap_hkdf(self.shared_key, self.CIPHER_SALT, self.IN_CIPHER_INFO)
        self.in_cipher = CHACHA20_POLY1305(incoming_key, "python")

    # socket.socket interface

    def _with_out_lock(func):
        """Return a function that acquires the outbound lock and executes func."""
        def _wrapper(self, *args, **kwargs):
            with self.out_lock:
                return func(self, *args, **kwargs)
        return _wrapper

    def recv_into(self, buffer, nbytes=1042, flags=0):
        """Receive and decrypt up to nbytes in the given buffer."""
        data = self.recv(nbytes, flags)
        for i, b in enumerate(data):
            buffer[i] = b
        return len(data)

    def recv(self, buflen=1042, flags=0):
        """Receive up to buflen bytes.

        The received full cipher blocks are decrypted and returned and partial cipher
        blocks are buffered locally.
        """
        assert not flags and buflen > self.LENGTH_LENGTH

        result = b""
        # Read from the socket until the given amount of bytes is read.
        while buflen > 1:
            # If we are not processing a block already, we need to first get the
            # length of the next block, which is the first two bytes before it.
            if self.curr_in_block is None:
                if buflen < self.LENGTH_LENGTH:
                    # It may be that we already read some data and we have
                    # 1 byte left, return whatever we have.
                    return result
                block_length_bytes = socket.socket.recv(self, self.LENGTH_LENGTH)
                if not block_length_bytes:
                    return result
                # TODO: handle this
                assert len(block_length_bytes) == self.LENGTH_LENGTH
                # Init. info about the block we just started.
                # Note we are setting the total length to block_length + mac length
                self.curr_in_total = \
                    struct.unpack("H", block_length_bytes)[0] + self.in_cipher.tagLength
                self.num_in_recv = 0
                self.curr_in_block = b""
                buflen -= self.LENGTH_LENGTH
            else:
                # Read as much from the current block as possible.
                part = socket.socket.recv(self,
                                          min(buflen,
                                              self.curr_in_total - self.num_in_recv))
                # Check what is actually received
                actual_len = len(part)
                self.curr_in_block += part
                buflen -= actual_len
                self.num_in_recv += actual_len
                if self.num_in_recv == self.curr_in_total:
                    # We read a whole block. Decrypt it and append it to the result.
                    nonce = _pad_tls_nonce(struct.pack("Q", self.in_count))
                    # Note we are removing the mac length from the total length
                    block_length = self.curr_in_total - self.in_cipher.tagLength
                    plaintext = self.in_cipher.open(
                        nonce, bytearray(self.curr_in_block),
                        struct.pack("H", block_length))
                    result += plaintext
                    self.in_count += 1
                    self.curr_in_block = None
                    break

        return result

    @_with_out_lock
    def send(self, data, flags=0):
        """Encrypt and send the given data."""
        # TODO: the two methods need to be handled differently, but...
        # The reason for the below hack is that SocketIO calls this method instead of
        # sendall.
        return self.sendall(data, flags)

    @_with_out_lock
    def sendall(self, data, flags=0):
        """Encrypt and send the given data."""
        assert not flags
        result = b""
        offset = 0
        total = len(data)
        while offset < total:
            length = min(total - offset, self.MAX_BLOCK_LENGTH)
            length_bytes = struct.pack("H", length)
            block = bytearray(data[offset: offset + length])
            nonce = _pad_tls_nonce(struct.pack("Q", self.out_count))
            ciphertext = length_bytes \
                + self.out_cipher.seal(nonce, block, length_bytes)
            offset += length
            self.out_count += 1
            result += ciphertext
        socket.socket.sendall(self, result)
        return total


class HAPServer(socketserver.ThreadingMixIn,
                HTTPServer):
    """Point of contact for HAP clients.

    The HAPServer handles all incoming client requests (e.g. pair) and also handles
    communication from Accessories to clients (value changes). The outbound communication
    is something like HTTP push.

    @note: Client requests responses as well as outgoing event notifications happen through
    the same socket for the same client. This introduces a race condition - an Accessory
    decides to push a change in current temperature, while in the same time the HAP client
    decides to query the state of the Accessory. To overcome this the HAPSocket class
    implements exclusive access to the send methods.
    """

    EVENT_MSG_STUB = b"EVENT/1.0 200 OK\r\n" \
                     b"Content-Type: application/hap+json\r\n" \
                     b"Content-Length: "

    TIMEOUT_ERRNO_CODES = (errno.ECONNRESET, errno.EPIPE, errno.EHOSTUNREACH,
                           errno.ETIMEDOUT)

    @classmethod
    def create_hap_event(cls, bytesdata):
        """Creates a HAP HTTP EVENT response for the given data.

        @param data: Payload of the request.
        @type data: bytes
        """
        return cls.EVENT_MSG_STUB \
            + str(len(bytesdata)).encode("utf-8") \
            + b"\r\n" * 2 \
            + bytesdata

    def __init__(self,
                 addr_port,
                 accessory_handler,
                 handler_type=HAPServerHandler):
        super(HAPServer, self).__init__(addr_port, handler_type)
        self.connections = {}  # (address, port): socket
        self.accessory_handler = accessory_handler

    def _close_socket(self, sock):
        """Shutdown and close the given socket."""
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except socket.error:
            pass
        sock.close()

    def _handle_sock_timeout(self, client_addr, exception):
        """Handle a socket timeout.

        Closes the socket for ``client_addr``.

        :raise exception: if it is not a timeout.
        """
        # NOTE: In python <3.3 socket.timeout is not OSError, hence the above.
        # Also, when it is actually an OSError, it MAY not have an errno equal to
        # ETIMEDOUT.
        sock = self.connections.pop(client_addr, None)
        if sock is not None:
            self._close_socket(sock)
        if not isinstance(exception, socket.timeout) \
                and exception.errno not in self.TIMEOUT_ERRNO_CODES:
            raise exception

    def get_request(self):
        """Calls the super's method, caches the connection and returns."""
        client_socket, client_addr = super(HAPServer, self).get_request()
        logger.info("Got connection with %s.", client_addr)
        self.connections[client_addr] = client_socket
        return (client_socket, client_addr)

    def finish_request(self, sock, client_addr):
        try:
            self.RequestHandlerClass(sock, client_addr, self, self.accessory_handler)
        except (OSError, socket.timeout) as e:
            self._handle_sock_timeout(client_addr, e)
            logger.debug("Connection timeout")

    def server_close(self):
        """Close all connections."""
        logger.info("Stopping HAP server")
        super(HAPServer, self).server_close()
        for sock in self.connections.values():
            self._close_socket(sock)
        self.connections.clear()

    def push_event(self, bytesdata, client_addr):
        """Send an event to the current connection with the provided data.

        .. note: Sets a timeout of PUSH_EVENT_TIMEOUT for the duration of socket.sendall.

        :param bytesdata: The data to send.
        :type bytesdata: bytes

        :param client_addr: A client (address, port) tuple to which to send the data.
        :type client_addr: tuple <str, int>

        :return: True if sending was successful, False otherwise.
        :rtype: bool
        """
        client_socket = self.connections.get(client_addr)
        if client_socket is None:
            return False
        data = self.create_hap_event(bytesdata)
        try:
            client_socket.sendall(data)
            return True
        except (OSError, socket.timeout) as e:
            self._handle_sock_timeout(client_addr, e)
            return False

    def upgrade_to_encrypted(self, client_address, shared_key):
        """Replace the socket for the given client with HAPSocket.

        @param client_address: The client address for which to upgrade the socket.
        @type client_address: tuple(addr, port)

        @param shared_key: The sessio key.
        @type shared_key: bytes.
        """
        client_socket = self.connections[client_address]
        hap_socket = HAPSocket(client_socket, shared_key)
        self.connections[client_address] = hap_socket
        return hap_socket
