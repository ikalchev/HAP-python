"""Support for the HomeKit camera audio talkback (two-way audio) channel.

The HomeKit Accessory Protocol Specification (section 11 "IP Cameras") requires
that, for a camera to support two-way audio, the accessory reports a local port
where it listens for the audio the controller (e.g. the iOS Home app) sends back
during a live view session ("Accessory Address" in the SetupEndpoints response,
Table 9-16 of the spec). Prior to this module, :mod:`pyhap.camera` echoed back the
*controller's* address in that field instead of a real local listening port,
which meant no HAP camera accessory built with this library could ever receive
the talkback audio: the controller had nowhere to send it.

This module implements the receiving side of that channel: a UDP socket bound to
an OS-assigned local port, SRTP decryption using the same key already negotiated
by :meth:`pyhap.camera.Camera.set_endpoints`, and RTP depacketization. The
decoded-from-RTP payload (still encoded with whichever audio codec was
negotiated, typically Opus) is handed to a callback for the accessory
implementation to decode/consume as it sees fit -- this mirrors how
:meth:`pyhap.camera.Camera.start_stream` already leaves video/audio encoding of
the *outgoing* stream to the implementation (or ffmpeg) rather than pyhap
itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import logging
import socket
import struct
import threading
from typing import cast

try:
    import pylibsrtp
except ImportError:  # pragma: no cover - exercised via requires_talkback()
    pylibsrtp = None

logger = logging.getLogger(__name__)

RTP_HEADER_LEN = 12


def talkback_available() -> bool:
    """Whether the optional dependency for talkback support is installed."""
    return pylibsrtp is not None


@dataclass
class RtpPacket:
    """A parsed RTP packet (RFC 3550), payload still codec-encoded."""

    version: int
    padding: bool
    marker: bool
    payload_type: int
    sequence_number: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp_packet(packet: bytes) -> RtpPacket:
    """Parse the RTP header of an already SRTP-decrypted packet."""
    if len(packet) < RTP_HEADER_LEN:
        raise ValueError(f"Packet too short to be RTP: {len(packet)} bytes")

    b0, b1, seq, timestamp, ssrc = struct.unpack("!BBHII", packet[:12])
    version = b0 >> 6
    if version != 2:
        raise ValueError(f"Unsupported RTP version: {version}")
    padding = bool((b0 >> 5) & 1)
    extension = (b0 >> 4) & 1
    csrc_count = b0 & 0x0F
    marker = bool(b1 >> 7)
    payload_type = b1 & 0x7F

    offset = RTP_HEADER_LEN + csrc_count * 4
    if extension:
        if len(packet) < offset + 4:
            raise ValueError("Truncated RTP extension header")
        ext_len_words = struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
        offset += 4 + ext_len_words * 4

    if padding and packet:
        pad_len = packet[-1]
        if pad_len > len(packet) - offset:
            raise ValueError(
                f"RTP padding length ({pad_len}) exceeds packet "
                f"({len(packet)} bytes with {offset} header)"
            )
        payload = packet[offset : len(packet) - pad_len]
    else:
        payload = packet[offset:]

    return RtpPacket(
        version=version,
        padding=padding,
        marker=marker,
        payload_type=payload_type,
        sequence_number=seq,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
    )


def is_allowed_sender(addr: tuple[str, int], controller_address: str | None) -> bool:
    """Whether a packet's source address is the negotiated controller's.

    :param addr: The ``(ip, port)`` a packet was received from.
    :param controller_address: The controller address negotiated during
        SetupEndpoints, or ``None`` to accept any source (legacy behavior).
    """
    if controller_address is None:
        return True
    source = addr[0]
    if type(source) is str and type(controller_address) is str:
        if source == controller_address:
            # The same spelling always names the same address; skip parsing.
            return True
        is_ipv6 = ":" in source
        if is_ipv6 != (":" in controller_address):
            # Only IPv6 literals contain ":", and IPv4 never equals IPv6.
            return False
        # Both are of one family, so parse with that class alone.
        parse = ipaddress.IPv6Address if is_ipv6 else ipaddress.IPv4Address
    else:
        parse = ipaddress.ip_address
    try:
        return parse(source) == parse(controller_address)
    except ValueError:
        # Not an IP literal (e.g. a hostname); fall back to string comparison.
        return source == controller_address


class TalkbackReceiver:
    """Listens for the HomeKit camera talkback (two-way) audio of one session.

    :param srtp_key_and_salt: The audio SRTP master key concatenated with the
        master salt, exactly as already negotiated in
        :meth:`pyhap.camera.Camera.set_endpoints` (``audio_master_key +
        audio_master_salt``). Reused here, not renegotiated.
    :param on_payload: Called from a background thread with the RTP payload
        (``bytes``) of each received packet, still encoded with whichever audio
        codec was negotiated for the session (see ``a_codec`` in the stream
        configuration passed to :meth:`pyhap.camera.Camera.start_stream`).
    :param is_ipv6: Whether to bind an IPv6 socket instead of IPv4.
    :param controller_address: The controller address negotiated during
        SetupEndpoints. Packets from any other source are dropped.
    """

    def __init__(
        self,
        srtp_key_and_salt: bytes,
        on_payload: Callable[[bytes], None],
        is_ipv6: bool = False,
        controller_address: str | None = None,
    ) -> None:
        if pylibsrtp is None:
            raise RuntimeError(
                "Talkback support requires the 'pylibsrtp' package. "
                "Install it with: pip install HAP-python[Talkback]"
            )

        policy = pylibsrtp.Policy(
            key=srtp_key_and_salt,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
        )
        self._session = pylibsrtp.Session(policy)
        self._on_payload = on_payload
        self._controller_address = controller_address

        family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_DGRAM)
        # Bind to all interfaces only to obtain an ephemeral port; the
        # receiver is handed to the controller, not exposed publicly.
        self._sock.bind(("::" if is_ipv6 else "0.0.0.0", 0))  # noqa: S104
        self._sock.settimeout(0.5)

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="hap-camera-talkback", daemon=True
        )

    @property
    def local_port(self) -> int:
        """The local port this receiver is bound to.

        This is the value that must be reported in the ``AUDIO_RTP_PORT`` field
        of the ``Accessory Address`` TLV in the ``SetupEndpoints`` response, so
        that the controller knows where to send the talkback audio.
        """
        return cast(int, self._sock.getsockname()[1])

    def start(self) -> None:
        """Start listening for incoming audio in a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop listening and release the socket."""
        self._stop_event.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except TimeoutError:
                # socket.timeout aliases TimeoutError from Python 3.10 on, but
                # is an OSError subclass on 3.9, which this project supports,
                # so catching TimeoutError here would kill the receiver thread
                # on the first (0.5s) poll timeout on Python 3.9.
                continue
            except OSError:
                break
            if not is_allowed_sender(addr, self._controller_address):
                # The socket listens on all interfaces; only the negotiated
                # controller may feed the two-way audio channel.
                logger.debug(
                    "Dropping talkback audio from unexpected source %s", addr[0]
                )
                continue
            try:
                rtp_plain = self._session.unprotect(data)
                packet = parse_rtp_packet(rtp_plain)
            except Exception:
                logger.exception("Failed to process incoming talkback audio packet")
                continue
            self._on_payload(packet.payload)
