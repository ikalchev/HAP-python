"""This module implements the communication of HAP.

The HAPServerHandler manages the state of the connection and handles incoming requests.
"""
import asyncio
from http import HTTPStatus
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import ParseResult, parse_qs, urlparse
import uuid

import async_timeout
from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable as ChaCha20Poly1305
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
import h11

from pyhap import tlv
from pyhap.const import (
    CATEGORY_BRIDGE,
    HAP_PERMISSIONS,
    HAP_REPR_CHARS,
    HAP_REPR_STATUS,
    HAP_SERVER_STATUS,
)
from pyhap.util import long_to_bytes

from .hap_crypto import hap_hkdf, pad_tls_nonce
from .state import State
from .util import from_hap_json, to_hap_json

if TYPE_CHECKING:
    from .accessory_driver import AccessoryDriver

# iOS will terminate the connection if it does not respond within
# 10 seconds, so we only allow 9 seconds to avoid this.
RESPONSE_TIMEOUT = 9


logger = logging.getLogger(__name__)


class HAPResponse:
    """A response to a HAP HTTP request."""

    def __init__(self):
        """Create an empty response."""
        self.status_code = 500
        self.reason = "Internal Server Error"
        self.headers = []
        self.body = b""
        self.shared_key = None
        self.task: Optional[asyncio.Future] = None
        self.pairing_changed = False

    def __repr__(self):
        """Return a human readable view of the response."""
        return (
            f"<HAPResponse {self.status_code} {self.reason} {self.headers} {self.body}>"
        )


class HAP_TLV_STATES:
    M1 = b"\x01"
    M2 = b"\x02"
    M3 = b"\x03"
    M4 = b"\x04"
    M5 = b"\x05"
    M6 = b"\x06"


class HAP_TLV_ERRORS:
    AUTHENTICATION = b"\x02"
    UNAVAILABLE = b"\x06"
    BUSY = b"\x07"


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
    PERMISSIONS = b"\x0B"
    SEPARATOR = b"\xFF"


class UnprivilegedRequestException(Exception):
    pass


async def _run_with_timeout(coro, timeout: float) -> bytes:
    """Run a coroutine with a timeout."""
    async with async_timeout.timeout(timeout):
        return await coro


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
        "PUT": {
            "/characteristics": "handle_set_characteristics",
            "/prepare": "handle_prepare",
        },
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
        self.accessory_handler: AccessoryDriver = accessory_handler
        self.state: State = self.accessory_handler.state
        self.enc_context: Optional[Dict[str, Any]] = None
        self.client_address = client_address
        self.is_encrypted = False
        self.client_uuid: Optional[uuid.UUID] = None

        self.path: Optional[str] = None
        self.command: Optional[str] = None
        self.headers: Optional[Dict[str, str]] = None
        self.request_body: Optional[bytes] = None
        self.parsed_url: Optional[ParseResult] = None

        self.response: Optional[HAPResponse] = None

    def _set_encryption_ctx(
        self,
        client_public: bytes,
        private_key: x25519.X25519PrivateKey,
        public_key: x25519.X25519PublicKey,
        shared_key: bytes,
        pre_session_key: bytes,
    ) -> None:
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

    def send_response(self, http_status: HTTPStatus) -> None:
        """Add the response header to the headers buffer and log the
        response code.
        Does not add Server or Date
        """
        assert self.response is not None  # nosec
        self.response.status_code = http_status.value
        self.response.reason = http_status.phrase

    def send_header(self, header: str, value: str) -> None:
        """Add the response header to the headers buffer."""
        assert self.response is not None  # nosec
        self.response.headers.append((header, value))

    def end_response(self, bytesdata: bytes) -> None:
        """Combines adding a length header and actually sending the data."""
        assert self.response is not None  # nosec
        self.response.body = bytesdata

    def dispatch(
        self, request: h11.Request, body: Optional[bytes] = None
    ) -> HAPResponse:
        """Dispatch the request to the appropriate handler method."""
        self.path = request.target.decode()
        self.command = request.method.decode()
        self.headers = {k.decode(): v.decode() for k, v in request.headers}
        self.request_body = body
        self.parsed_url = urlparse(self.path)
        response = HAPResponse()
        self.response = response

        logger.debug(
            "%s: Request %s for path '%s': %s",
            self.client_address,
            self.command,
            self.path,
            self.headers,
        )

        path = self.parsed_url.path
        try:
            getattr(self, self.HANDLERS[self.command][path])()
        except UnprivilegedRequestException:
            self.send_response_with_status(
                HTTPStatus.UNAUTHORIZED, HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "%s: Failed to process request for: %s", self.client_address, path
            )
            self.send_response_with_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            )

        self.response = None
        return response

    def generic_failure_response(self) -> HAPResponse:
        """Generate a generic failure response."""
        self.response = HAPResponse()
        self.send_response_with_status(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
        )
        response = self.response
        self.response = None
        return response

    def send_response_with_status(
        self, http_code: HTTPStatus, hap_server_status: int
    ) -> None:
        """Send a generic HAP status response."""
        self.send_response(http_code)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(to_hap_json({"status": hap_server_status}))

    def handle_pairing(self) -> None:
        """Handles arbitrary step of the pairing process."""
        if self.state.paired:
            self._send_tlv_pairing_response(
                tlv.encode(
                    HAP_TLV_TAGS.SEQUENCE_NUM,
                    HAP_TLV_STATES.M2,
                    HAP_TLV_TAGS.ERROR_CODE,
                    HAP_TLV_ERRORS.UNAVAILABLE,
                )
            )
            return

        tlv_objects = tlv.decode(self.request_body)
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]

        if sequence == HAP_TLV_STATES.M1:
            self._pairing_one()
        elif sequence == HAP_TLV_STATES.M3:
            self._pairing_two(tlv_objects)
        elif sequence == HAP_TLV_STATES.M5:
            self._pairing_three(tlv_objects)

    def _pairing_one(self) -> None:
        """Send the SRP salt and public key to the client.

        The SRP verifier is created at this step.
        """
        logger.debug("%s: Pairing [1/5]", self.client_address)
        self.accessory_handler.setup_srp_verifier()
        salt, B = self.accessory_handler.srp_verifier.get_challenge()

        data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            HAP_TLV_STATES.M2,
            HAP_TLV_TAGS.SALT,
            salt,
            HAP_TLV_TAGS.PUBLIC_KEY,
            long_to_bytes(B),
        )
        self._send_tlv_pairing_response(data)

    def _pairing_two(self, tlv_objects: Dict[bytes, bytes]) -> None:
        """Obtain the challenge from the client (A) and client's proof that it
        knows the password (M). Verify M and generate the server's proof based on
        A (H_AMK). Send the H_AMK to the client.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("%s: Pairing [2/5]", self.client_address)
        A = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        M = tlv_objects[HAP_TLV_TAGS.PASSWORD_PROOF]
        verifier = self.accessory_handler.srp_verifier
        verifier.set_A(A)

        hamk = verifier.verify(M)

        if hamk is None:  # Probably the provided pincode was wrong.
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M4)
            return

        data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            HAP_TLV_STATES.M4,
            HAP_TLV_TAGS.PASSWORD_PROOF,
            hamk,
        )
        self._send_tlv_pairing_response(data)

    def _pairing_three(self, tlv_objects: Dict[bytes, bytes]) -> None:
        """Expand the SRP session key to obtain a new key. Use it to verify and decrypt
            the recieved data. Continue to step four.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("%s: Pairing [3/5]", self.client_address)
        encrypted_data = tlv_objects[HAP_TLV_TAGS.ENCRYPTED_DATA]

        session_key = self.accessory_handler.srp_verifier.get_session_key()
        hkdf_enc_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_3_SALT, self.PAIRING_3_INFO
        )

        cipher = ChaCha20Poly1305(hkdf_enc_key)
        try:
            decrypted_data = cipher.decrypt(
                self.PAIRING_3_NONCE, bytes(encrypted_data), b""
            )
        except InvalidTag:
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M6)
            return

        dec_tlv_objects = tlv.decode(bytes(decrypted_data))
        client_username_bytes = dec_tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_ltpk = dec_tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        client_proof = dec_tlv_objects[HAP_TLV_TAGS.PROOF]

        self._pairing_four(
            client_username_bytes, client_ltpk, client_proof, hkdf_enc_key
        )

    def _pairing_four(
        self,
        client_username_bytes: bytes,
        client_ltpk: bytes,
        client_proof: bytes,
        encryption_key: bytes,
    ) -> None:
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
        logger.debug("%s: Pairing [4/5]", self.client_address)
        session_key = self.accessory_handler.srp_verifier.get_session_key()
        output_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_4_SALT, self.PAIRING_4_INFO
        )

        data = output_key + client_username_bytes + client_ltpk
        verifying_key = ed25519.Ed25519PublicKey.from_public_bytes(client_ltpk)

        try:
            verifying_key.verify(client_proof, data)
        except InvalidSignature:
            logger.error("Bad signature, abort.")
            raise

        self._pairing_five(client_username_bytes, client_ltpk, encryption_key)

    def _pairing_five(
        self, client_username_bytes: bytes, client_ltpk: bytes, encryption_key: bytes
    ) -> None:
        """At that point we know the client has the accessory password and has a valid key
        pair. Add it as a pair and send a sever proof.

        Parameters are as for _pairing_four.
        """
        logger.debug("%s: Pairing [5/5]", self.client_address)
        session_key = self.accessory_handler.srp_verifier.get_session_key()
        output_key = hap_hkdf(
            long_to_bytes(session_key), self.PAIRING_5_SALT, self.PAIRING_5_INFO
        )

        server_public = self.state.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
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

        client_username_str = client_username_bytes.decode("utf-8")
        client_uuid = uuid.UUID(client_username_str)
        logger.debug(
            "Finishing pairing with admin %s uuid=%s", client_username_str, client_uuid
        )
        should_confirm = self.accessory_handler.pair(
            client_username_bytes, client_ltpk, HAP_PERMISSIONS.ADMIN
        )

        if not should_confirm:
            self.send_response_with_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST,
            )
            return

        tlv_data = tlv.encode(
            HAP_TLV_TAGS.SEQUENCE_NUM,
            HAP_TLV_STATES.M6,
            HAP_TLV_TAGS.ENCRYPTED_DATA,
            aead_message,
        )
        assert self.response is not None  # nosec
        self.response.pairing_changed = True
        self._send_tlv_pairing_response(tlv_data)

    def handle_pair_verify(self) -> None:
        """Handles arbitrary step of the pair verify process.

        Pair verify is session negotiation.
        """
        if not self.state.paired:
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M2)
            return

        tlv_objects = tlv.decode(self.request_body)
        sequence = tlv_objects[HAP_TLV_TAGS.SEQUENCE_NUM]
        if sequence == HAP_TLV_STATES.M1:
            self._pair_verify_one(tlv_objects)
        elif sequence == HAP_TLV_STATES.M3:
            self._pair_verify_two(tlv_objects)
        else:
            raise ValueError(
                f"Unknown pairing sequence of {sequence} during pair verify"
            )

    def _pair_verify_one(self, tlv_objects: Dict[bytes, bytes]) -> None:
        """Generate new session key pair and send a proof to the client.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("%s: Pair verify [1/2].", self.client_address)
        client_public: bytes = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]

        private_key = x25519.X25519PrivateKey.generate()
        public_key = private_key.public_key()
        shared_key = private_key.exchange(
            x25519.X25519PublicKey.from_public_bytes(client_public)
        )

        mac = self.state.mac.encode()
        material = (
            public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            + mac
            + client_public
        )
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
            HAP_TLV_STATES.M2,
            HAP_TLV_TAGS.ENCRYPTED_DATA,
            aead_message,
            HAP_TLV_TAGS.PUBLIC_KEY,
            public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
        self._send_tlv_pairing_response(data)

    def _pair_verify_two(self, tlv_objects: Dict[bytes, bytes]) -> None:
        """Verify the client proof and upgrade to encrypted transport.

        @param tlv_objects: The TLV data received from the client.
        @type tlv_object: dict
        """
        logger.debug("%s: Pair verify [2/2]", self.client_address)
        encrypted_data = tlv_objects[HAP_TLV_TAGS.ENCRYPTED_DATA]
        cipher = ChaCha20Poly1305(self.enc_context["pre_session_key"])
        try:
            decrypted_data = cipher.decrypt(
                self.PVERIFY_2_NONCE, bytes(encrypted_data), b""
            )
        except InvalidTag:
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M4)
            return

        dec_tlv_objects = tlv.decode(bytes(decrypted_data))
        client_username = dec_tlv_objects[HAP_TLV_TAGS.USERNAME]
        public_key: x25519.X25519PublicKey = self.enc_context["public_key"]
        raw_public_key = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        material = self.enc_context["client_public"] + client_username + raw_public_key

        client_uuid = uuid.UUID(str(client_username, "utf-8"))
        perm_client_public = self.state.paired_clients.get(client_uuid)
        if perm_client_public is None:
            logger.error(
                "%s: Client %s with uuid %s attempted pair verify "
                "without being paired first (public_key=%s, paired clients=%s).",
                self.accessory_handler.accessory.display_name,
                self.client_address,
                client_uuid,
                raw_public_key.hex(),
                {uuid: key.hex() for uuid, key in self.state.paired_clients.items()},
            )
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M4)
            return

        verifying_key = ed25519.Ed25519PublicKey.from_public_bytes(perm_client_public)
        try:
            verifying_key.verify(dec_tlv_objects[HAP_TLV_TAGS.PROOF], material)
        except (InvalidSignature, KeyError) as ex:
            logger.error("%s: %s, abort.", self.client_address, ex)
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M4)
            return

        logger.debug(
            "%s: Pair verify with client completed. Switching to "
            "encrypted transport.",
            self.client_address,
        )

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M4)
        self._send_tlv_pairing_response(data)

        if client_uuid not in self.state.uuid_to_bytes:
            # We are missing the raw bytes for this client, so we need to
            # add them to the state and persist so list pairings works.
            self.state.uuid_to_bytes[client_uuid] = client_username
            self.accessory_handler.async_persist()

        assert self.response is not None  # nosec
        self.response.shared_key = self.enc_context["shared_key"]
        self.is_encrypted = True
        self.client_uuid = client_uuid
        del self.enc_context

    def handle_accessories(self) -> None:
        """Handles a client request to get the accessories."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        hap_rep = self.accessory_handler.get_accessories()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(to_hap_json(hap_rep))

    def handle_get_characteristics(self) -> None:
        """Handles a client request to get certain characteristics."""
        if not self.is_encrypted:
            raise UnprivilegedRequestException

        # Check that char exists and ...
        assert self.parsed_url is not None  # nosec
        params = parse_qs(self.parsed_url.query)
        response = self.accessory_handler.get_characteristics(
            params["id"][0].split(",")
        )
        chars = response[HAP_REPR_CHARS]

        had_failure = any(
            result[HAP_REPR_STATUS] != HAP_SERVER_STATUS.SUCCESS for result in chars
        )
        if had_failure:
            self.send_response(HTTPStatus.MULTI_STATUS)
        else:
            self.send_response(HTTPStatus.OK)
            for result in chars:
                del result[HAP_REPR_STATUS]

        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(to_hap_json(response))

    def handle_set_characteristics(self) -> None:
        """Handles a client request to update certain characteristics."""
        if not self.is_encrypted:
            logger.warning(
                "%s: Attempt to access unauthorised content", self.client_address
            )
            self.send_response(HTTPStatus.UNAUTHORIZED)
            return

        assert self.request_body is not None  # nosec
        requested_chars = from_hap_json(self.request_body.decode("utf-8"))
        logger.debug(
            "%s: Set characteristics content: %s", self.client_address, requested_chars
        )

        response = self.accessory_handler.set_characteristics(
            requested_chars, self.client_address
        )
        if response is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            return

        self.send_response(HTTPStatus.MULTI_STATUS)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(to_hap_json(response))

    def handle_prepare(self):
        """Handles a client request to prepare to write."""
        if not self.is_encrypted:
            logger.warning(
                "%s: Attempt to access unauthorised content", self.client_address
            )
            self.send_response(HTTPStatus.UNAUTHORIZED)
            return

        request = from_hap_json(self.request_body.decode("utf-8"))
        logger.debug("%s: prepare content: %s", self.client_address, request)

        response = self.accessory_handler.prepare(request, self.client_address)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.JSON_RESPONSE_TYPE)
        self.end_response(to_hap_json(response))

    def handle_pairings(self) -> None:
        """Handles a client request to update or remove a pairing."""
        # Must be an admin to handle pairings
        assert self.client_uuid is not None  # nosec
        if not self.is_encrypted or not self.state.is_admin(self.client_uuid):
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M2)
            return

        tlv_objects = tlv.decode(self.request_body)
        request_type = tlv_objects[HAP_TLV_TAGS.REQUEST_TYPE][0]
        if request_type == 3:
            self._handle_add_pairing(tlv_objects)
        elif request_type == 4:
            self._handle_remove_pairing(tlv_objects)
        elif request_type == 5:
            self._handle_list_pairings()
        else:
            raise ValueError(
                f"Unknown pairing request type of {request_type} during pair verify"
            )

    def _handle_add_pairing(self, tlv_objects):
        """Update client information."""
        client_username_bytes = tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_public = tlv_objects[HAP_TLV_TAGS.PUBLIC_KEY]
        permissions = tlv_objects[HAP_TLV_TAGS.PERMISSIONS]
        logger.debug(
            "%s: Adding client pairing for %s with permissions %s.",
            self.client_address,
            client_username_bytes,
            permissions,
        )
        should_confirm = self.accessory_handler.pair(
            client_username_bytes, client_public, permissions
        )
        if not should_confirm:
            self._send_authentication_error_tlv_response(HAP_TLV_STATES.M2)
            return

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M2)
        self._send_tlv_pairing_response(data)

    def _handle_remove_pairing(self, tlv_objects: Dict[bytes, bytes]) -> None:
        """Remove pairing with the client."""
        client_username_bytes: bytes = tlv_objects[HAP_TLV_TAGS.USERNAME]
        client_username_str = client_username_bytes.decode("utf-8")
        client_uuid = uuid.UUID(client_username_str)
        was_paired = self.state.paired
        logger.debug(
            "%s: Removing client pairing (%s) uuid=%s (was previously paired=%s).",
            self.client_address,
            client_username_str,
            client_uuid,
            was_paired,
        )
        # If the client does not exist, we must
        # respond with success per the spec
        if client_uuid in self.state.paired_clients:
            self.accessory_handler.unpair(client_uuid)

        data = tlv.encode(HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M2)
        self._send_tlv_pairing_response(data)

        if not self.state.paired_clients and was_paired:
            # Only update the announcement when the last
            # client is removed, otherwise the controller
            # may not remove them all
            logger.debug("%s: updating mdns to unpaired", self.client_address)
            assert self.response is not None  # nosec
            self.response.pairing_changed = True

    def _handle_list_pairings(self) -> None:
        """List current pairings."""
        logger.debug("%s: list pairings", self.client_address)
        response = [HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M2]
        state = self.state
        for client_uuid, client_public in state.paired_clients.items():
            admin = self.state.is_admin(client_uuid)
            response.extend(
                [
                    HAP_TLV_TAGS.USERNAME,
                    # iOS 16+ requires the username to be uppercase
                    # or it will unpair the accessory because it thinks
                    # the username is invalid. We try to send back the
                    # exact bytes that was used to pair if we have it
                    state.uuid_to_bytes.get(client_uuid)
                    or str(client_uuid).encode("utf-8").upper(),
                    HAP_TLV_TAGS.PUBLIC_KEY,
                    client_public,
                    HAP_TLV_TAGS.PERMISSIONS,
                    HAP_PERMISSIONS.ADMIN if admin else HAP_PERMISSIONS.USER,
                    HAP_TLV_TAGS.SEPARATOR,
                    b"",
                ]
            )

        if response[-2] == HAP_TLV_TAGS.SEPARATOR:
            # The last pairing should not have a separator
            response.pop()
            response.pop()

        data = tlv.encode(*response)
        self._send_tlv_pairing_response(data)

    def _send_authentication_error_tlv_response(self, sequence: bytes) -> None:
        """Send an authentication error tlv response."""
        self._send_tlv_pairing_response(
            tlv.encode(
                HAP_TLV_TAGS.SEQUENCE_NUM,
                sequence,
                HAP_TLV_TAGS.ERROR_CODE,
                HAP_TLV_ERRORS.AUTHENTICATION,
            )
        )

    def _send_tlv_pairing_response(self, data: bytes) -> None:
        """Send a TLV encoded pairing response."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.PAIRING_RESPONSE_TYPE)
        self.end_response(data)

    def handle_resource(self) -> None:
        """Get a snapshot from the camera."""
        assert self.request_body is not None  # nosec
        data = from_hap_json(self.request_body.decode("utf-8"))

        if self.accessory_handler.accessory.category == CATEGORY_BRIDGE:
            accessory = self.accessory_handler.accessory.accessories.get(data["aid"])
            if not accessory:
                raise ValueError(f"Accessory with aid == {data['aid']} not found")
        else:
            accessory = self.accessory_handler.accessory

        loop = asyncio.get_event_loop()
        if hasattr(accessory, "async_get_snapshot"):
            coro = accessory.async_get_snapshot(data)
        elif hasattr(accessory, "get_snapshot"):
            coro = loop.run_in_executor(None, accessory.get_snapshot, data)
        else:
            raise ValueError(
                "Got a request for snapshot, but the Accessory "
                'does not define a "get_snapshot" or "async_get_snapshot" method'
            )

        task = asyncio.ensure_future(_run_with_timeout(coro, RESPONSE_TIMEOUT))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        assert self.response is not None  # nosec
        self.response.task = task
