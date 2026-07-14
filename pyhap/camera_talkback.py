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

from dataclasses import dataclass
import logging
import socket
import struct
import threading
from typing import Callable

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
    """

    def __init__(
        self,
        srtp_key_and_salt: bytes,
        on_payload: Callable[[bytes], None],
        is_ipv6: bool = False,
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

        family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_DGRAM)
        self._sock.bind(("::" if is_ipv6 else "0.0.0.0", 0))
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
        return self._sock.getsockname()[1]

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
                data, _addr = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                rtp_plain = self._session.unprotect(data)
                packet = parse_rtp_packet(rtp_plain)
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to process incoming talkback audio packet")
                continue
            self._on_payload(packet.payload)
