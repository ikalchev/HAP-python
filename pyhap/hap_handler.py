"""This module implements the communication of HAP.

The HAPServerHandler manages the state of the connection and handles incoming requests.
"""
import asyncio
from http import HTTPStatus
import json
import logging
from urllib.parse import parse_qs, urlparse
import uuid

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import curve25519
import ed25519

import pyhap.tlv as tlv
from pyhap.util import long_to_bytes
from pyhap.const import CATEGORY_BRIDGE
from .hap_crypto import hap_hkdf, pad_tls_nonce

SNAPSHOT_TIMEOUT = 10

logger = logging.getLogger(__name__)


class HAPResponse:
    """A response to a HAP HTTP request."""

    def __init__(self):
        """Create an empty response."""
        self.status_code = 500
        self.reason = "Internal Server Error"
        self.headers = []
        self.body = []
        self.shared_key = None
        self.task = None


# Various "tag" constants for HAP's TLV encoding.
class HAP_TLV_TAGS:
    REQUEST_TYPE = b"\x00"
    USERNAME = b"\x01"
    SALT = b"\x02"
    PUBLIC_KEY = b"\x03"
    PASSWORD_PROOF = b"\x04"
    ENCRYPTED_DATA = b"\x05"
    SEQUENCE_NUM = b"\x06"
    ERROR_CODE = b"\x07"
    PROOF = b"\x0A"


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
    INSUFFICIENT_AUTHORIZATION = -70411


# Error codes and the like, guessed by packet inspection
class HAP_OPERATION_CODE:
    INVALID_REQUEST = b"\x02"
    INVALID_SIGNATURE = b"\x04"


class TimeoutException(Exception):
    pass


class UnprivilegedRequestException(Exception):
    pass


class NotAllowedInStateException(Exception):
    pass


class HAPServerHandler:
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
        "PUT": {"/characteristics": "handle_set_characteristics"},
    }

    PAIRING_RESPONSE_TYPE = "application/pairing+tlv8"
    JSON_RESPONSE_TYPE = "application/hap+json"

    PAIRING_3_SALT = b"Pair-Setup-Encrypt-Salt"
    PAIRING_3_INFO = b"Pair-Setup-Encrypt-Info"
    PAIRING_3_NONCE = pad_tls_nonce(b"PS-Msg05")

    PAIRING_4_SALT = b"Pair-Setup-Controller-Sign-Salt"
    PAIRING_4_INFO = b"Pair-Setup-Controller-Sign-Info"

    PAIRING_5_SALT = b"Pair-Setup-Accessory-Sign-Salt"
    PAIRING_5_INFO = b"Pair-Setup-Accessory-Sign-Info"
    PAIRING_5_NONCE = pad_tls_nonce(b"PS-Msg06")

    PVERIFY_1_SALT = b"Pair-Verify-Encrypt-Salt"
    PVERIFY_1_INFO = b"Pair-Verify-Encrypt-Info"
    PVERIFY_1_NONCE = pad_tls_nonce(b"PV-Msg02")

    PVERIFY_2_NONCE = pad_tls_nonce(b"PV-Msg03")

    def __init__(self, accessory_handler, client_address):
        """
        @param accessory_handler: An object that controls an accessory's state.
        @type accessory_handler: AccessoryDriver
        """
        self.accessory_handler = accessory_handler
        self.state = self.accessory_handler.state
        self.enc_context = None
        self.client_address = client_address
        self.is_encrypted = False

        self.path = None
        self.command = None
        self.headers = None
        self.request_body = None

        self.response = None

    def _set_encryption_ctx(
        self, client_public, private_key, public_key, shared_key, pre_session_key
    ):
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
            "pre_session_key": pre_session_key,
        }

    def send_response(self, code, message=None):
        """Add the response header to the headers buffer and log the
        response code.
        Does not add Server or Date
        """
        self.response.status_code = int(code)
        self.response.reason = message or "OK"

    def send_header(self, header, value):
        """Add the response header to the headers buffer."""
        self.response.headers.append((header, value))

    def end_response(self, bytesdata):
        """Combines adding a length header and actually sending the data."""
        self.response.body = bytesdata

    def dispatch(self, request, body=None):
        """Dispatch the request to the appropriate handler method."""
        self.path = request.target.decode()
        self.command = request.method.decode()
        self.headers = {k.decode(): v.decode() for k, v in request.headers}
        self.request_body = body
        response = HAPResponse()
        self.response = response

        logger.debug(
            "Request %s from address '%s' for path '%s': %s",
            self.command,
            self.client_address,
            self.path,
            self.headers,
        )

        path = urlparse(self.path).path
        assert path in self.HANDLERS[self.command]
        try:
            getattr(self, self.HANDLERS[self.command][path])()
        except NotAllowedInStateException:
            self.send_response_with_status(
                403, HAP_SERVER_STATUS.INSUFFICIENT_AUTHORIZATION
            )
        except UnprivilegedRequestException:
            self.send_response_with_status(
                401, HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES
            )
        except TimeoutException:
            self.send_response_with_status(500, HAP_SERVER_STATUS.OPERATION_TIMED_OUT)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Failed to process request for: %s", path)
            self.send_response_with_status(
                500, HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
            )

        self.response = None
        return response

    def send_response_with_status(self, http_code, hap_server_status):
        """Send a generic HAP status response."""
        self.send_response(http_code)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(json.dumps({"status": hap_server_status}).encode("utf-8"))

    def handle_pairing(self):
        """Handles arbitrary step of the pairing process."""
        if self.state.paired:
            raise NotAllowedInStateException

        tlv_objects = tlv.decode(self.request_body)
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]

        if sequence == b"\x01":
            self._pairing_one()
        elif sequence == b"\x03":
            self._pairing_two(tlv_objects)
        elif sequence == b"\x05":
            self._pairing_three(tlv_objects)

    def _pairing_one(self):
        """Send the SRP salt and public key to the client.

        The SRP verifier is created at this step.
        """
        logger.debug("Pairing [1/5]")
        self.accessory_handler.setup_srp_verifier()
        salt, B = self.accessory_handler.srp_verifier.get_challenge()

        data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            b"\x02",
            HAP_TLV_TAGS.SALT,
            salt,
            HAP_TLV_TAGS.PUBLIC_KEY,
            long_to_bytes(B),
        )

        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

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
            response = tlv.encode(
                HAP_TLV_TAGS.SEQUENCE_NUM,
                b"\x04",
                HAP_TLV_TAGS.ERROR_CODE,
                HAP_OPERATION_CODE.INVALID_REQUEST,
            )
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            self.end_response(response)
            return

        data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM, b"\x04", HAP_TLV_TAGS.PASSWORD_PROOF, hamk
        )
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
        hkdf_enc_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_3_SALT, self.PAIRING_3_INFO
        )

        cipher = ChaCha20Poly1305(hkdf_enc_key)
        decrypted_data = cipher.decrypt(
            self.PAIRING_3_NONCE, bytes(encrypted_data), b""
        )
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
        output_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_4_SALT, self.PAIRING_4_INFO
        )

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
        output_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_5_SALT, self.PAIRING_5_INFO
        )

        server_public = self.state.public_key.to_bytes()
        mac = self.state.mac.encode()

        material = output_key + mac + server_public
        private_key = self.state.private_key
        server_proof = private_key.sign(material)

        message = tlv.encode(
            HAP_TLV_TAGS.USERNAME,
            mac,
            HAP_TLV_TAGS.PUBLIC_KEY,
            server_public,
            HAP_TLV_TAGS.PROOF,
            server_proof,
        )

        cipher = ChaCha20Poly1305(encryption_key)
        aead_message = bytes(cipher.encrypt(self.PAIRING_5_NONCE, bytes(message), b""))

        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        should_confirm = self.accessory_handler.pair(client_uuid, client_ltpk)

        if not should_confirm:
            self.send_response_with_status(
                500, HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
            )
            return

        tlv_data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            b"\x06",
            HAP_TLV_TAGS.ENCRYPTED_DATA,
            aead_message,
        )
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(tlv_data)

    def handle_pair_verify(self):
        """Handles arbitrary step of the pair verify process.

        Pair verify is session negotiation.
        """
        if not self.state.paired:
            raise NotAllowedInStateException

        tlv_objects = tlv.decode(self.request_body)
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]
        if sequence == b"\x01":
            self._pair_verify_one(tlv_objects)
        elif sequence == b"\x03":
            self._pair_verify_two(tlv_objects)
        else:
            raise ValueError(
                "Unknown pairing sequence of %s during pair verify" % (sequence)
            )

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
            lambda x: x,
        )

        mac = self.state.mac.encode()
        material = public_key.serialize() + mac + client_public
        server_proof = self.state.private_key.sign(material)

        output_key = hap_hkdf(shared_key, self.PVERIFY_1_SALT, self.PVERIFY_1_INFO)

        self._set_encryption_ctx(
            client_public, private_key, public_key, shared_key, output_key
        )

        message = tlv.encode(
            HAP_TLV_TAGS.USERNAME, mac, HAP_TLV_TAGS.PROOF, server_proof
        )

        cipher = ChaCha20Poly1305(output_key)
        aead_message = bytes(cipher.encrypt(self.PVERIFY_1_NONCE, bytes(message), b""))
        data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            b"\x02",
            HAP_TLV_TAGS.ENCRYPTED_DATA,
            aead_message,
            HAP_TLV_TAGS.PUBLIC_KEY,
            public_key.serialize(),
        )
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
        decrypted_data = cipher.decrypt(
            self.PVERIFY_2_NONCE, bytes(encrypted_data), b""
        )
        assert decrypted_data is not None  # TODO:

        dec_tlv_objects = tlv.decode(bytes(decrypted_data))
        client_username = dec_tlv_objects[HAP_TLV_TAGS.USERNAME]
        material = (
            self.enc_context["client_public"]
            + client_username
            + self.enc_context["public_key"].serialize()
        )

        client_uuid = uuid.UUID(str(client_username, "ascii"))
        perm_client_public = self.state.paired_clients.get(client_uuid)
        if perm_client_public is None:
            logger.debug(
                "Client %s attempted pair verify without being paired first.",
                client_uuid,
            )
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            data = tlv.encode(
                HAP_TLV_TAGS.ERROR_CODE, HAP_OPERATION_CODE.INVALID_REQUEST
            )
            self.end_response(data)
            return

        verifying_key = ed25519.VerifyingKey(perm_client_public)
        try:
            verifying_key.verify(dec_tlv_objects[HAP_TLV_TAGS.PROOF], material)
        except ed25519.BadSignatureError:
            logger.error("Bad signature, abort.")
            self.send_response(200)
            self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
            data = tlv.encode(
                HAP_TLV_TAGS.ERROR_CODE, HAP_OPERATION_CODE.INVALID_REQUEST
            )
            self.end_response(data)
            return

        logger.debug(
            "Pair verify with client '%s' completed. Switching to "
            "encrypted transport.",
            self.client_address,
        )

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b"\x04")
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

        self.response.shared_key = self.enc_context["shared_key"]
        self.is_encrypted = True
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
            logger.warning(
                "Attempt to access unauthorised content from %s", self.client_address
            )
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_response(b"")

        requested_chars = json.loads(self.request_body.decode("utf-8"))
        logger.debug("Set characteristics content: %s", requested_chars)

        # TODO: Outline how chars return errors on set_chars.
        try:
            self.accessory_handler.set_characteristics(
                requested_chars, self.client_address
            )
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception("Exception in set_characteristics: %s", ex)
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_response(b"")
        else:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_response(b"")

    def handle_pairings(self):
        """Handles a client request to update or remove a pairing."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        tlv_objects = tlv.decode(self.request_body)
        request_type = tlv_objects[HAP_TLV_TAGS.REQUEST_TYPE][0]
        if request_type == 3:
            self._handle_add_pairing(tlv_objects)
        elif request_type == 4:
            self._handle_remove_pairing(tlv_objects)
        else:
            raise ValueError(
                "Unknown pairing request type of %s during pair verify" % (request_type)
            )

    def _handle_add_pairing(self, tlv_objects):
        """Update client information."""
        logger.debug("Adding client pairing.")
        client_username = tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_public = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        should_confirm = self.accessory_handler.pair(client_uuid, client_public)
        if not should_confirm:
            self.send_response_with_status(
                500, HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
            )
            return

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, b"\x02")
        self.send_response(200)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

        # Avoid updating the announcement until
        # after the response is sent as homekit will
        # drop the connection and fail to pair if it
        # sees the accessory is now paired as it doesn't
        # know that it was the one doing the pairing.
        self.accessory_handler.finish_pair()

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

        # Avoid updating the announcement until
        # after the response is sent.
        self.accessory_handler.finish_pair()

    def handle_resource(self):
        """Get a snapshot from the camera."""

        data = json.loads(self.request_body.decode("utf-8"))

        if self.accessory_handler.accessory.category == CATEGORY_BRIDGE:
            accessory = self.accessory_handler.accessory.accessories.get(data['aid'])
            if not accessory:
                raise ValueError('Accessory with aid == {} not found'.format(data['aid']))
        else:
            accessory = self.accessory_handler.accessory

        loop = asyncio.get_event_loop()
        if hasattr(accessory, "async_get_snapshot"):
            coro = accessory.async_get_snapshot(data)
        elif hasattr(accessory, "get_snapshot"):
            coro = asyncio.wait_for(
                loop.run_in_executor(
                    None, accessory.get_snapshot, data
                ),
                SNAPSHOT_TIMEOUT,
            )
        else:
            raise ValueError(
                "Got a request for snapshot, but the Accessory "
                'does not define a "get_snapshot" or "async_get_snapshot" method'
            )

        task = asyncio.ensure_future(coro)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.response.task = task
