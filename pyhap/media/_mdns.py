"""Resolve the mDNS names of a WebRTC answer's candidates.

A controller may name itself by an mDNS ``.local`` name instead of an IP
address. The names are resolved here, on the caller's loop, with the Zeroconf
instance of the camera's driver: the media transport must not create one. A
worker has no instance of its own, and where an application makes every new
instance its shared one, closing the new one would close that.

Names that do not resolve stay as they are; the transport ignores them and
such a peer is then only found by its own checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import dataclasses
import ipaddress
import logging
import re
from typing import TYPE_CHECKING

from . import sdp as sdp_module
from .ice import Candidate

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncZeroconf

    from pyhap import hksv3

logger = logging.getLogger(__name__)

# Seconds all names of an answer get, in parallel: the answer waits for them.
# Zeroconf first asks for a unicast answer, which can get lost where several
# mDNS stacks share the port; the answer to its next question then came after
# about 1.3 seconds.
MDNS_TIMEOUT = 2.0
# Names resolved per answer; each costs a query.
MAX_MDNS_NAMES = 4
MAX_MDNS_NAME = 253
# The SDP parser splits on line feeds and reads a candidate after this.
_SDP_PREFIX = "a=candidate:"
# Where the connection address of a candidate is, the fifth field (RFC 8839).
_HOST = re.compile(r"\s*(?:a=)?(?:candidate:)?\s*(?:\S+\s+){4}(?P<host>\S+)")


def _mdns_host(candidate: str) -> str | None:
    """The mDNS name of a candidate the transport uses once it has an address.

    Read as the transport reads it: a UDP candidate of component 1 with a
    valid port. Others must not use up the names an answer has resolved.
    """
    try:
        parsed = Candidate.from_sdp(sdp_module.candidate_value(candidate))
    except ValueError:
        return None
    host = parsed.host
    if (
        parsed.component == 1
        and parsed.transport.lower() == "udp"
        and 0 < parsed.port < 65536
        and host.lower().endswith(".local")
        and len(".local") < len(host) <= MAX_MDNS_NAME
    ):
        return host
    return None


def _with_address(candidate: str, addresses: Mapping[str, str]) -> str:
    """The candidate with its mDNS name replaced by the resolved address."""
    host = _mdns_host(candidate)
    address = addresses.get(host.lower()) if host else None
    match = _HOST.match(candidate) if address else None
    if match is None:
        return candidate
    start, end = match.span("host")
    return f"{candidate[:start]}{address}{candidate[end:]}"


def _usable(address: str) -> bool:
    """Whether an mDNS answer names a peer: a unicast address, not loopback."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return not (
        parsed.is_unspecified
        or parsed.is_multicast
        or parsed.is_loopback
        or parsed == ipaddress.IPv4Address("255.255.255.255")
    )


async def _lookup(
    zeroconf: AsyncZeroconf, names: Sequence[str], timeout: float
) -> dict[str, str]:
    """Ask for every name at once; a name without an answer is left out.

    Returns after ``timeout`` seconds at the latest.
    """
    try:
        from zeroconf import AddressResolver  # noqa: PLC0415 - rarely needed
    except ImportError:
        logger.debug("This zeroconf cannot resolve mDNS candidates")
        return {}
    instance = zeroconf.zeroconf
    if not names:
        return {}
    if instance.loop is not None and instance.loop is not asyncio.get_running_loop():
        logger.debug("The Zeroconf instance runs on another loop, names stay")
        return {}
    if not instance.started:
        # A request would wait for the start, and giving up on the request
        # would cancel that start for everyone.
        logger.debug("The Zeroconf instance has not started, names stay")
        return {}

    async def resolve(name: str) -> str | None:
        try:
            resolver = AddressResolver(f"{name}.")
            if not await resolver.async_request(instance, int(timeout * 1000)):
                return None
            addresses = resolver.parsed_scoped_addresses()
        except Exception as err:  # noqa: BLE001 - e.g. an instance that stopped
            logger.debug("mDNS name %r not resolved: %r", name, err)
            return None
        return addresses[0] if addresses else None

    tasks = {name: asyncio.ensure_future(resolve(name)) for name in names}
    try:
        done, _ = await asyncio.wait(tasks.values(), timeout=timeout)
    finally:
        for task in tasks.values():
            task.cancel()
    return {
        name: address
        for name, task in tasks.items()
        if task in done and (address := task.result()) is not None
    }


async def resolve_answer(
    zeroconf: AsyncZeroconf | None,
    sdp: str,
    candidates: Sequence[hksv3.IceCandidate],
    timeout: float = MDNS_TIMEOUT,
) -> tuple[str, list[hksv3.IceCandidate]]:
    """The answer with the mDNS names of its candidates resolved.

    Covers the candidates HAP carries next to the SDP and those inside it,
    as far as the sender uses them. At most :data:`MAX_MDNS_NAMES` names are
    asked for, within ``timeout`` seconds. Without a Zeroconf instance that
    runs on this loop nothing is resolved.
    """
    try:
        answer = sdp_module.parse(sdp_module.merge_candidates(sdp, candidates))
        transport = answer.transport()
    except sdp_module.SdpError:
        # The sender refuses this answer.
        return sdp, list(candidates)
    if not transport.ice_ufrag or not transport.ice_pwd or not transport.fingerprints:
        # It refuses this one too, without waiting for names first.
        return sdp, list(candidates)
    lines = sdp.split("\n")
    written = [
        *(line[len(_SDP_PREFIX) :] for line in lines if line.startswith(_SDP_PREFIX)),
        *(c.candidate for c in candidates),
    ]
    # A name is asked for when the sender uses a candidate with it and it can
    # be replaced where it is written.
    replaceable = {host.lower() for text in written if (host := _mdns_host(text))}
    hosts = [_mdns_host(text) for text in answer.candidates]
    names = list(
        dict.fromkeys(
            host.lower() for host in hosts if host and host.lower() in replaceable
        )
    )
    if not names:
        return sdp, list(candidates)
    if zeroconf is None:
        logger.debug("No Zeroconf instance to resolve %d mDNS candidates", len(names))
        return sdp, list(candidates)
    if len(names) > MAX_MDNS_NAMES:
        logger.debug("Resolving only %d of the mDNS candidates", MAX_MDNS_NAMES)
    answers = await _lookup(zeroconf, names[:MAX_MDNS_NAMES], timeout)
    addresses = {name: answer for name, answer in answers.items() if _usable(answer)}
    logger.debug("Resolved %d of %d mDNS candidates", len(addresses), len(names))
    resolved_sdp = "\n".join(
        f"{_SDP_PREFIX}{_with_address(line[len(_SDP_PREFIX) :], addresses)}"
        if line.startswith(_SDP_PREFIX)
        else line
        for line in lines
    )
    return resolved_sdp, [
        dataclasses.replace(c, candidate=_with_address(c.candidate, addresses))
        for c in candidates
    ]
