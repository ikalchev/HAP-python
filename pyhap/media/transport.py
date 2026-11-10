"""ICE, DTLS-SRTP transport for the HomeKit WebRTC sender.

One BUNDLEd, rtcp-muxed transport: :mod:`pyhap.media.ice` runs ICE with host
candidates including consent freshness (RFC 7675), ``pyOpenSSL`` runs the
DTLS handshake over memory BIOs and ``pylibsrtp`` protects RTP and RTCP with
the keys exported from DTLS (RFC 5764).

Packets arriving on the ICE transport are told apart by their first byte
(RFC 7983): 0-3 is STUN, consumed by the ICE agent, 20-63 is DTLS, 128-191
is SRTP or SRTCP. Other data is only accepted from the remote address of the
selected candidate pair (before selection: of any pair of the check list),
and only a bounded number of datagrams is queued.
"""

# The DTLS-SRTP parts (the cipher list, the SRTP key and salt layout, the
# AES-GCM profile probe and the self-signed certificate) follow aiortc:
#
# Copyright (c) Jeremy Lainé.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright notice,
#       this list of conditions and the following disclaimer in the documentation
#       and/or other materials provided with the distribution.
#     * Neither the name of aiortc nor the names of its contributors may
#       be used to endorse or promote products derived from this software without
#       specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
import contextlib
import dataclasses
from dataclasses import dataclass
import datetime
import enum
import ipaddress
import logging
import os
import struct

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
import ifaddr
from OpenSSL import SSL
import pylibsrtp

from pyhap import util

from .ice import Candidate, IceAgent, resolve_mdns

logger = logging.getLogger(__name__)

DTLS_MTU = 1200
DTLS_RECORD_HEADER = 13
SRTP_EXPORTER_LABEL = b"EXTRACTOR-dtls_srtp"
DEFAULT_CONNECT_TIMEOUT = 20.0
MAX_REMOTE_CANDIDATES = 32
MAX_MDNS_NAME = 253
MDNS_TIMEOUT = 2.0
# The DTLS 1.2 protocol version, which pyOpenSSL has no constant for.
DTLS1_2_VERSION = 0xFEFD
# mDNS names resolved per connection; each costs a zeroconf query.
MAX_MDNS_CANDIDATES = 4
FINGERPRINT_ALGORITHMS = {
    "sha-256": hashes.SHA256(),
    "sha-384": hashes.SHA384(),
    "sha-512": hashes.SHA512(),
}


class TransportError(Exception):
    """ICE or DTLS failed."""


class DtlsRole(enum.Enum):
    """The local DTLS role, decided by the answer's ``a=setup``."""

    CLIENT = "client"
    SERVER = "server"

    @classmethod
    def from_remote_setup(cls, setup: str | None) -> DtlsRole:
        """Map the remote ``a=setup`` to the local role (RFC 8842)."""
        return cls.SERVER if setup == "active" else cls.CLIENT

    @property
    def setup(self) -> str:
        """The ``a=setup`` value describing this role in a later answer."""
        return "active" if self is DtlsRole.CLIENT else "passive"


@dataclass(frozen=True)
class _SrtpProfile:
    openssl_name: bytes
    libsrtp_profile: int
    key_length: int
    salt_length: int

    def key_and_salt(self, material: bytes, index: int) -> bytes:
        key = material[index * self.key_length :][: self.key_length]
        salt_start = 2 * self.key_length + index * self.salt_length
        return key + material[salt_start : salt_start + self.salt_length]


def _srtp_profiles() -> list[_SrtpProfile]:
    candidates = [
        _SrtpProfile(
            b"SRTP_AEAD_AES_128_GCM",
            pylibsrtp.Policy.SRTP_PROFILE_AEAD_AES_128_GCM,
            16,
            12,
        ),
        _SrtpProfile(
            b"SRTP_AES128_CM_SHA1_80",
            pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
            16,
            14,
        ),
    ]
    profiles = []
    for profile in candidates:
        # AES-GCM depends on how libsrtp was built.
        with contextlib.suppress(pylibsrtp.Error):
            pylibsrtp.Policy(srtp_profile=profile.libsrtp_profile)
            profiles.append(profile)
    return profiles


SRTP_PROFILES = _srtp_profiles()


def fingerprint(certificate: x509.Certificate, algorithm: str = "sha-256") -> str:
    """Return a certificate fingerprint as colon-separated upper-case hex."""
    digest = certificate.fingerprint(FINGERPRINT_ALGORITHMS[algorithm]).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class Certificate:
    """A self-signed ECDSA P-256 DTLS certificate."""

    def __init__(self) -> None:
        """Generate the key and certificate."""
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, os.urandom(16).hex())]
        )
        now = datetime.datetime.now(tz=datetime.UTC)
        self.certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(self.key, hashes.SHA256())
        )

    @property
    def fingerprint(self) -> str:
        """The SHA-256 fingerprint for ``a=fingerprint``."""
        return fingerprint(self.certificate)

    def ssl_context(self) -> SSL.Context:
        """Create a DTLS context offering the supported SRTP profiles."""
        context = SSL.Context(SSL.DTLS_METHOD)
        context.set_options(SSL.OP_NO_TICKET | SSL.OP_NO_RENEGOTIATION)
        # WebRTC requires DTLS 1.2 (RFC 8827 section 6.5).
        context.set_min_proto_version(DTLS1_2_VERSION)
        # The peer is authenticated by the SDP fingerprint, not by a CA.
        context.set_verify(
            SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, lambda *_args: True
        )
        context.use_certificate(self.certificate)
        context.use_privatekey(self.key)
        context.set_cipher_list(
            b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:"
            b"ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA"
        )
        context.set_tlsext_use_srtp(b":".join(p.openssl_name for p in SRTP_PROFILES))
        return context


def pack_records(data: bytes, mtu: int = DTLS_MTU) -> list[bytes]:
    """Split DTLS output into datagrams of whole records, at most ``mtu`` each.

    A record larger than the MTU is sent alone.
    """
    datagrams: list[bytes] = []
    current = b""
    offset = 0
    while offset < len(data):
        if offset + DTLS_RECORD_HEADER > len(data):
            record = data[offset:]
        else:
            (length,) = struct.unpack_from(">H", data, offset + 11)
            record = data[offset : offset + DTLS_RECORD_HEADER + length]
        offset += len(record)
        if current and len(current) + len(record) > mtu:
            datagrams.append(current)
            current = b""
        current += record
    if current:
        datagrams.append(current)
    return datagrams


def is_rtcp(packet: bytes) -> bool:
    """Whether a muxed RTP/RTCP packet is RTCP (RFC 5761 section 4)."""
    return len(packet) >= 2 and 64 <= packet[1] & 0x7F <= 95


def default_host_addresses(use_ipv6: bool) -> list[str]:
    """The addresses of the LAN interface: the one with the default route.

    That interface's IPv4 address and, with ``use_ipv6``, its global and
    unique local IPv6 addresses. Gathering on every interface would offer
    container, VPN and other bridge addresses to the controller.
    """
    lan = util.get_local_address()
    for adapter in ifaddr.get_adapters():
        if not any(ip.ip == lan for ip in adapter.ips):
            continue
        addresses = [lan]
        if use_ipv6:
            addresses += [
                ip.ip[0]
                for ip in adapter.ips
                if isinstance(ip.ip, tuple) and _usable_ipv6(ip.ip[0])
            ]
        return addresses
    return [lan]


def _usable_ipv6(address: str) -> bool:
    parsed = ipaddress.IPv6Address(address.split("%", 1)[0])
    return not (parsed.is_link_local or parsed.is_loopback)


class DtlsSrtpTransport:
    """One ICE transport carrying DTLS-SRTP with RTP/RTCP multiplexing."""

    def __init__(
        self,
        *,
        ice_controlling: bool,
        host_addresses: Sequence[str] | None = None,
        use_ipv6: bool = True,
        certificate: Certificate | None = None,
    ) -> None:
        """Prepare the transport; nothing is bound until :meth:`gather`."""
        self.certificate = certificate or Certificate()
        self.on_rtp: Callable[[bytes], None] | None = None
        self.on_rtcp: Callable[[bytes], None] | None = None
        self.on_closed: Callable[[str], None] | None = None
        self.role: DtlsRole | None = None
        self.srtp_profile: _SrtpProfile | None = None
        self._ice = IceAgent(controlling=ice_controlling)
        self._host_addresses = host_addresses
        self._use_ipv6 = use_ipv6
        self._ssl: SSL.Connection | None = None
        self._tx_srtp: pylibsrtp.Session | None = None
        self._rx_srtp: pylibsrtp.Session | None = None
        self._reader: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_done = asyncio.Event()
        self._handshake_done = False
        self._connected = False
        self._closed = False

    @property
    def local_username(self) -> str:
        """The local ICE ufrag."""
        return self._ice.local_username

    @property
    def local_password(self) -> str:
        """The local ICE password."""
        return self._ice.local_password

    @property
    def _tls(self) -> SSL.Connection:
        if self._ssl is None:
            raise TransportError("DTLS not started")
        return self._ssl

    @property
    def connected(self) -> bool:
        """Whether SRTP is ready."""
        return self._connected and not self._closed

    @property
    def closed(self) -> bool:
        """Whether the transport was closed."""
        return self._closed

    @property
    def write_buffer_size(self) -> int:
        """Bytes queued in the socket that did not go out yet."""
        return self._ice.write_buffer_size

    async def gather(self) -> list[str]:
        """Gather local candidates and return them as ``candidate:`` strings.

        Raises :class:`TransportError` when the transport is closed before
        or while gathering; nothing stays bound then.
        """
        if self._closed:
            raise TransportError("Transport is closed")
        addresses = (
            list(self._host_addresses)
            if self._host_addresses is not None
            else default_host_addresses(self._use_ipv6)
        )
        try:
            candidates = await self._ice.gather(addresses)
        except ConnectionError as err:
            raise TransportError("Transport is closed") from err
        if self._closed:
            raise TransportError("Transport is closed")
        return [f"candidate:{c.to_sdp()}" for c in candidates]

    async def connect(
        self,
        *,
        remote_username: str,
        remote_password: str,
        remote_candidates: Iterable[str],
        remote_fingerprints: Sequence[tuple[str, str]],
        role: DtlsRole,
        ice_lite: bool = False,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Run ICE and the DTLS handshake, then start receiving.

        At most :data:`MAX_REMOTE_CANDIDATES` distinct UDP candidates of
        component 1 are used; adding them (which may resolve mDNS names)
        counts against ``timeout``. Raises :class:`TransportError` when
        either step fails, times out or the transport is closed meanwhile.
        """
        if self._closed:
            raise TransportError("Transport is closed")
        self.role = role
        self._ice.remote_username = remote_username
        self._ice.remote_password = remote_password
        self._ice.remote_is_lite = ice_lite
        candidates = _remote_candidates(remote_candidates)
        try:
            await asyncio.wait_for(
                self._connect(candidates, remote_fingerprints), timeout
            )
        except TimeoutError as err:
            raise TransportError("WebRTC connection timed out") from err
        except (ConnectionError, SSL.Error) as err:
            raise TransportError(f"WebRTC connection failed: {err}") from err
        except Exception as err:
            # For example an mDNS socket that cannot bind, or an SRTP error.
            raise TransportError(f"WebRTC connection failed: {err!r}") from err
        if self._closed:
            raise TransportError("Transport is closed")
        self._connected = True
        self._reader = asyncio.create_task(self._read_loop())

    async def _connect(
        self,
        candidates: list[Candidate],
        remote_fingerprints: Sequence[tuple[str, str]],
    ) -> None:
        await self._ice.connect(await _resolve_candidates(candidates))
        self._ssl = SSL.Connection(self.certificate.ssl_context())
        if self.role is DtlsRole.SERVER:
            self._ssl.set_accept_state()
        else:
            self._ssl.set_connect_state()
        while True:
            try:
                self._ssl.do_handshake()
                break
            except SSL.WantReadError:
                await self._flush()
                await self._receive_dtls()
        await self._flush()
        self._verify_fingerprint(remote_fingerprints)
        self._setup_srtp()

    async def _receive_dtls(self) -> None:
        """Wait for one DTLS datagram, retransmitting on DTLS timeouts."""
        while True:
            timeout = self._tls.DTLSv1_get_timeout()
            try:
                data = await asyncio.wait_for(self._ice.recv(), timeout)
            except TimeoutError:
                self._tls.DTLSv1_handle_timeout()
                await self._flush()
                continue
            if 19 < data[0] < 64:
                self._tls.bio_write(data)
                return

    def _verify_fingerprint(self, remote: Sequence[tuple[str, str]]) -> None:
        certificate = self._tls.get_peer_certificate(as_cryptography=True)
        if certificate is None:
            raise TransportError("DTLS peer sent no certificate")
        known = [(a, v) for a, v in remote if a.lower() in FINGERPRINT_ALGORITHMS]
        if not known or any(
            fingerprint(certificate, algorithm.lower()) != value.upper()
            for algorithm, value in known
        ):
            raise TransportError("DTLS fingerprint mismatch")

    def _setup_srtp(self) -> None:
        name = self._tls.get_selected_srtp_profile()
        profile = next((p for p in SRTP_PROFILES if p.openssl_name == name), None)
        if profile is None:
            raise TransportError("No SRTP profile negotiated")
        self.srtp_profile = profile
        material = self._tls.export_keying_material(
            SRTP_EXPORTER_LABEL, 2 * (profile.key_length + profile.salt_length)
        )
        tx_index = 1 if self.role is DtlsRole.SERVER else 0
        tx_policy = pylibsrtp.Policy(
            key=profile.key_and_salt(material, tx_index),
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND,
            srtp_profile=profile.libsrtp_profile,
        )
        # NACK retransmissions protect the same packet index again.
        tx_policy.allow_repeat_tx = True
        tx_policy.window_size = 1024
        rx_policy = pylibsrtp.Policy(
            key=profile.key_and_salt(material, 1 - tx_index),
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
            srtp_profile=profile.libsrtp_profile,
        )
        rx_policy.window_size = 1024
        self._tx_srtp = pylibsrtp.Session(tx_policy)
        self._rx_srtp = pylibsrtp.Session(rx_policy)

    async def _flush(self) -> None:
        data = b""
        while True:
            try:
                chunk = self._tls.bio_read(65536)
            except SSL.WantReadError:
                break
            if not chunk:
                break
            data += chunk
        for datagram in pack_records(data):
            await self._ice.send(datagram)

    async def _read_loop(self) -> None:
        reason = "closed"
        try:
            while True:
                data = await self._next_datagram()
                if not data:
                    continue
                if 19 < data[0] < 64:
                    if not await self._handle_dtls(data):
                        reason = "dtls closed by remote"
                        break
                elif 127 < data[0] < 192:
                    self._handle_srtp(data)
        except ConnectionError:
            reason = "ice connection lost"
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure ends the session
            logger.debug("WebRTC transport read failed", exc_info=True)
            reason = f"read failed: {err!r}"
        finally:
            if not self._closed:
                self._close_task = asyncio.get_running_loop().create_task(
                    self._close(reason)
                )

    async def _next_datagram(self) -> bytes | None:
        """Receive one datagram, still serving DTLS retransmissions if needed."""
        timeout = None if self._handshake_done else self._tls.DTLSv1_get_timeout()
        if timeout is None:
            self._handshake_done = True
            return await self._ice.recv()
        try:
            return await asyncio.wait_for(self._ice.recv(), timeout)
        except TimeoutError:
            self._tls.DTLSv1_handle_timeout()
            await self._flush()
            return None

    async def _handle_dtls(self, data: bytes) -> bool:
        self._tls.bio_write(data)
        try:
            self._tls.recv(1500)
        except SSL.ZeroReturnError:
            return False
        except SSL.Error:
            pass
        await self._flush()
        return True

    def _handle_srtp(self, data: bytes) -> None:
        if self._rx_srtp is None:
            return
        try:
            if is_rtcp(data):
                packet = self._rx_srtp.unprotect_rtcp(data)
                callback = self.on_rtcp
            else:
                packet = self._rx_srtp.unprotect(data)
                callback = self.on_rtp
        except pylibsrtp.Error as err:
            logger.debug("Dropping undecryptable SRTP packet: %s", err)
            return
        if callback is None:
            return
        try:
            callback(packet)
        except Exception:
            # A failing consumer must not take the transport down.
            logger.exception("Error handling received RTP/RTCP")

    async def send_rtp(self, packet: bytes) -> None:
        """Protect and send one RTP packet."""
        if not self.connected or self._tx_srtp is None:
            return
        await self._ice.send(self._tx_srtp.protect(packet))

    async def send_rtcp(self, packet: bytes) -> None:
        """Protect and send one (compound) RTCP packet."""
        if not self.connected or self._tx_srtp is None:
            return
        await self._ice.send(self._tx_srtp.protect_rtcp(packet))

    async def close(self) -> None:
        """Send a DTLS close_notify and release the sockets.

        Returns once the transport is closed, also when a close for another
        reason is already under way.
        """
        await self._close("closed locally")
        await self._close_done.wait()

    async def _close(self, reason: str) -> None:
        """Release everything; never raises, every step runs."""
        if self._closed:
            return
        self._closed = True
        self._connected = False
        if self._ssl is not None and reason == "closed locally":
            try:
                self._ssl.shutdown()
                await self._flush()
            except Exception:  # noqa: BLE001
                logger.debug("DTLS close_notify failed", exc_info=True)
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        try:
            await self._ice.close()
        except Exception:  # noqa: BLE001
            logger.debug("Closing ICE failed", exc_info=True)
        logger.debug("WebRTC transport closed: %s", reason)
        self._close_done.set()
        if self.on_closed:
            try:
                self.on_closed(reason)
            except Exception:
                logger.exception("Error in WebRTC transport close callback")


def _usable_remote_address(host: str, port: int) -> bool:
    """Whether a remote candidate has a valid port and a unicast or mDNS host.

    Unspecified, multicast and broadcast addresses are not usable peers.
    """
    if not 0 < port < 65536:
        return False
    if host.lower().endswith(".local"):
        return len(host) > len(".local") and len(host) <= MAX_MDNS_NAME
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        # Checked as IPv4: broadcast always, multicast on older Pythons.
        address = address.ipv4_mapped
    return not (
        address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
    )


async def _resolve_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Replace mDNS host names by their address; unresolved ones are dropped.

    At most :data:`MAX_MDNS_CANDIDATES` names are resolved, in parallel, and
    an address a name resolves to must be a usable unicast address too.
    """
    names = [c.host for c in candidates if c.host.lower().endswith(".local")]
    if len(set(names)) > MAX_MDNS_CANDIDATES:
        logger.debug("Resolving only %d of the mDNS candidates", MAX_MDNS_CANDIDATES)
    wanted = list(dict.fromkeys(names))[:MAX_MDNS_CANDIDATES]
    addresses: dict[str, str | None] = {}
    if wanted:
        try:
            addresses = await resolve_mdns(wanted, MDNS_TIMEOUT)
        except OSError as err:
            logger.debug("Cannot resolve mDNS candidates: %s", err)
    resolved = []
    for candidate in candidates:
        if not candidate.host.lower().endswith(".local"):
            resolved.append(candidate)
            continue
        address = addresses.get(candidate.host)
        if address is None or not _usable_resolved_address(address, candidate.port):
            logger.debug("Ignoring unresolved candidate %s", candidate.host)
            continue
        resolved.append(dataclasses.replace(candidate, host=address))
    return resolved


def _usable_resolved_address(address: str, port: int) -> bool:
    """Whether an mDNS answer names a usable peer: unicast, not loopback."""
    if address.lower().endswith(".local") or not _usable_remote_address(address, port):
        return False
    return not ipaddress.ip_address(address.split("%", 1)[0]).is_loopback


def _remote_candidates(values: Iterable[str]) -> list[Candidate]:
    """Parse distinct UDP candidates of component 1, at most the cap.

    Candidates whose port is out of range, whose address is unspecified,
    multicast or broadcast, or whose host is neither an IP address nor an
    mDNS ``.local`` name are dropped.
    """
    seen: set[str] = set()
    candidates = []
    for value in values:
        text = value.strip().removeprefix("a=").removeprefix("candidate:")
        if text in seen:
            continue
        seen.add(text)
        try:
            parsed = Candidate.from_sdp(text)
        except ValueError:
            logger.debug("Ignoring unparsable candidate %r", value)
            continue
        if not _usable_remote_address(parsed.host, parsed.port):
            logger.debug("Ignoring candidate with an unusable address %r", value)
            continue
        if parsed.component == 1 and parsed.transport.lower() == "udp":
            candidates.append(parsed)
            if len(candidates) == MAX_REMOTE_CANDIDATES:
                break
    return candidates
