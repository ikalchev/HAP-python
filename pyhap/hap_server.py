"""This module implements the communication of HAP.

The HAPServer is the point of contact to and from the world.
The HAPServerHandler manages the state of the connection and handles incoming requests.
The HAPSocket is a socket implementation that manages the "TLS" of the connection.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
from http import HTTPStatus
import logging
import socket
import struct
import json
import errno
import uuid
from urllib.parse import urlparse, parse_qs
import socketserver
import threading

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

import curve25519
import ed25519

import pyhap.tlv as tlv
from pyhap.util import long_to_bytes
from pyhap.const import __version__

logger = logging.getLogger(__name__)

backend = default_backend()


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
    HKDF_HASH = hashes.SHA512()  # Hash function to use in key expansion
    TAG_LENGTH = 16  # ChaCha20Poly1305 tag length
    TLS_NONCE_LEN = 12  # bytes, length of TLS encryption nonce


def _pad_tls_nonce(nonce, total_len=HAP_CRYPTO.TLS_NONCE_LEN):
    """Pads a nonce with zeroes so that total_len is reached."""
    return nonce.rjust(total_len, b"\x00")


def hap_hkdf(key, salt, info):
    """Just a shorthand."""
    hkdf = HKDF(
        algorithm=HAP_CRYPTO.HKDF_HASH,
        length=HAP_CRYPTO.HKDF_KEYLEN,
        salt=salt,
        info=info,
        backend=backend,
    )
    return hkdf.derive(key)


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
            "/resource": "handle_resource",
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
        self.server_version = 'pyhap/' + __version__
        # HTTP/1.1 allows a keep-alive which makes
        # a large accessory list usable in homekit
        # If iOS has to reconnect to query each accessory
        # it can be painfully slow and lead to lock up on the
        # client side as well as non-responsive devices
        self.protocol_version = 'HTTP/1.1'
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
        # Setting this head will take care of setting
        # self.close_connection to the right value
        self.send_header("Connection", ("close" if close_connection else "keep-alive"))
        # Important: we need to send the headers and the
        # content in a single write to avoid homekit
        # on the client side stalling and making
        # devices appear non-responsive.
        #
        # The below code does what end_headers does internally
        # except it combines the headers and the content
        # into a single write instead of two calls to
        # self.wfile.write
        #
        # TODO: Is there a better way that doesn't involve
        # touching _headers_buffer ?
        #
        self.connection.sendall(b"".join(self._headers_buffer) + b"\r\n" + bytesdata)
        self._headers_buffer = []

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
            self.end_response(b'')
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

        cipher = ChaCha20Poly1305(hkdf_enc_key)
        decrypted_data = cipher.decrypt(self.PAIRING_3_NONCE, bytes(encrypted_data), b"")
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

        cipher = ChaCha20Poly1305(encryption_key)
        aead_message = bytes(
            cipher.encrypt(self.PAIRING_5_NONCE, bytes(message), b""))

        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        should_confirm = self.accessory_handler.pair(client_uuid, client_ltpk)

        if not should_confirm:
            self.send_response(500)
            self.end_response(b'')
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

        cipher = ChaCha20Poly1305(output_key)
        aead_message = bytes(
            cipher.encrypt(self.PVERIFY_1_NONCE, bytes(message), b""))
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
        cipher = ChaCha20Poly1305(self.enc_context["pre_session_key"])
        decrypted_data = cipher.decrypt(self.PVERIFY_2_NONCE, bytes(encrypted_data), b"")
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
            logger.warning('Attemp to access unauthorised content from %s',
                           self.client_address)
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_response(b'', close_connection=True)

        data_len = int(self.headers['Content-Length'])
        requested_chars = json.loads(
            self.rfile.read(data_len).decode('utf-8'))
        logger.debug('Set characteristics content: %s', requested_chars)

        # TODO: Outline how chars return errors on set_chars.
        try:
            self.accessory_handler.set_characteristics(requested_chars,
                                                       self.client_address)
        except Exception as e:
            logger.exception('Exception in set_characteristics: %s', e)
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_response(b'')
        else:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_response(b'')

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
            self.end_response(b'')
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

    def handle_resource(self):
        """Get a snapshot from the camera."""
        if not hasattr(self.accessory_handler.accessory, 'get_snapshot'):
            raise ValueError('Got a request for snapshot, but the Accessory '
                             'does not define a "get_snapshot" method')
        data_len = int(self.headers['Content-Length'])
        image_size = json.loads(
                        self.rfile.read(data_len).decode('utf-8'))
        image = self.accessory_handler.accessory.get_snapshot(image_size)
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.end_response(image)


class HAPSocket:
    """A socket implementing the HAP crypto. Just feed it as if it is a normal socket.

    This implementation is something like a Proxy pattern - some calls to socket
    methods are wrapped and some are forwarded as is.

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
        """Initialise from the given socket."""
        self.socket = sock

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

    def __getattr__(self, attribute_name):
        """Defer unknown behaviour to the socket"""
        return getattr(self.socket, attribute_name)

    def _get_io_refs(self):
        """Get `socket._io_refs`."""
        return self.socket._io_refs

    def _set_io_refs(self, value):
        """Set `socket._io_refs`."""
        self.socket._io_refs = value

    _io_refs = property(_get_io_refs, _set_io_refs)
    """`socket.makefile` uses a `SocketIO` to wrap the socket stream. Internally,
    this uses `socket._io_refs` directly to determine if a socket object needs to be
    closed when its FileIO object is closed.

    Because `_io_refs` is assigned as part of this process, it bypasses getattr. To get
    around this, let's make _io_refs our property and proxy calls to the socket.
    """

    def makefile(self, *args, **kwargs):
        """Return a file object that reads/writes to this object.

        We need to implement this, otherwise the socket's makefile will use the socket
        object and we won't en/decrypt.
        """
        return socket.socket.makefile(self, *args, **kwargs)

    def _set_ciphers(self):
        """Generate out/inbound encryption keys and initialise respective ciphers."""
        outgoing_key = hap_hkdf(self.shared_key, self.CIPHER_SALT, self.OUT_CIPHER_INFO)
        self.out_cipher = ChaCha20Poly1305(outgoing_key)

        incoming_key = hap_hkdf(self.shared_key, self.CIPHER_SALT, self.IN_CIPHER_INFO)
        self.in_cipher = ChaCha20Poly1305(incoming_key)

    # socket.socket interface

    def _with_out_lock(func):
        """Return a function that acquires the outbound lock and executes func."""
        def _wrapper(self, *args, **kwargs):
            with self.out_lock:
                return func(self, *args, **kwargs)
        return _wrapper

    def recv_into(self, buffer, nbytes=None, flags=0):
        """Receive and decrypt up to nbytes in the given buffer."""
        data = self.recv(nbytes or len(buffer), flags)
        buffer[:len(data)] = data
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
                # Always wait for a full block to arrive
                block_length_bytes = self.socket.recv(
                    self.LENGTH_LENGTH, socket.MSG_WAITALL
                )
                if not block_length_bytes:
                    return result
                # Init. info about the block we just started.
                # Note we are setting the total length to block_length + mac length
                self.curr_in_total = \
                    struct.unpack("H", block_length_bytes)[0] + HAP_CRYPTO.TAG_LENGTH
                self.num_in_recv = 0
                self.curr_in_block = b""
                buflen -= self.LENGTH_LENGTH
            else:
                # Read as much from the current block as possible.
                part = self.socket.recv(min(buflen,
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
                    block_length = self.curr_in_total - HAP_CRYPTO.TAG_LENGTH
                    plaintext = self.in_cipher.decrypt(
                        nonce, bytes(self.curr_in_block),
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
            block = bytes(data[offset: offset + length])
            nonce = _pad_tls_nonce(struct.pack("Q", self.out_count))
            ciphertext = length_bytes \
                + self.out_cipher.encrypt(nonce, block, length_bytes)
            offset += length
            self.out_count += 1
            result += ciphertext
        self.socket.sendall(result)
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
                           errno.ETIMEDOUT, errno.EHOSTDOWN, errno.EBADF)

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
        logger.debug("Connection timeout for %s with exception %s", client_addr, exception)
        logger.debug("Current connections %s", self.connections)
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

    def finish_request(self, request, client_address):
        """Handle the client request.

        HAP connections are not closed. Once the client negotiates a session,
        the connection is kept open for both incoming and outgoing traffic, including
        for sending events.

        The client can gracefully close the connection, but in other cases it can just
        leave, which will result in a timeout. In either case, we need to remove the
        connection from ``self.connections``, because it could also be used for
        pushing events to the server.
        """
        try:
            self.RequestHandlerClass(request, client_address,
                                     self, self.accessory_handler)
        except (OSError, socket.timeout) as e:
            self._handle_sock_timeout(client_address, e)
            logger.debug('Connection timeout')
        finally:
            logger.debug('Cleaning connection to %s', client_address)
            conn_sock = self.connections.pop(client_address, None)
            if conn_sock is not None:
                self._close_socket(conn_sock)

    def server_close(self):
        """Close all connections."""
        logger.info('Stopping HAP server')

        # When the AccessoryDriver is shutting down, it will stop advertising the
        # Accessory on the network before stopping the server. At that point, clients
        # can see the Accessory disappearing and could close the connection. This can
        # happen while we deal with all connections here so we will get a "changed while
        # iterating" exception. To avoid that, make a copy and iterate over it instead.
        for sock in list(self.connections.values()):
            self._close_socket(sock)
        self.connections.clear()
        super().server_close()

    def push_event(self, bytesdata, client_addr):
        """Send an event to the current connection with the provided data.

        :param bytesdata: The data to send.
        :type bytesdata: bytes

        :param client_addr: A client (address, port) tuple to which to send the data.
        :type client_addr: tuple <str, int>

        :return: True if sending was successful, False otherwise.
        :rtype: bool
        """
        client_socket = self.connections.get(client_addr)
        if client_socket is None:
            logger.debug('No socket for %s', client_addr)
            return False
        data = self.create_hap_event(bytesdata)
        try:
            client_socket.sendall(data)
            return True
        except (OSError, socket.timeout) as e:
            logger.debug('exception %s for %s in push_event()', e, client_addr)
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
