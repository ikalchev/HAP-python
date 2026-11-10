"""A small ICE agent with host candidates only (RFC 8445 subset).

One component (BUNDLE with rtcp-mux), UDP, one socket per local address.
Both roles: the controlling agent checks every pair and nominates the best
pair that succeeded with a second check carrying USE-CANDIDATE (regular
nomination); the controlled agent answers checks, runs triggered checks and
selects the pair the controlling agent nominates. Role conflicts are
resolved with the tie-breaker and 487 (RFC 8445 section 7.3.1.1).

No STUN or TURN server is used: HomeKit controllers reach the accessory
through a relay of their own (remote) or on the LAN, and the accessory's
outgoing check opens its NAT towards the relay. A request from an unknown
address with a valid MESSAGE-INTEGRITY becomes a peer-reflexive pair.

After selection, consent freshness (RFC 7675) sends a check on the selected
pair every few seconds; without an authenticated response for
:data:`CONSENT_TIMEOUT` seconds the agent closes. Non-STUN datagrams are
only accepted from the remote address of the selected pair or of another
pair that passed its checks (before selection: of any pair), into a
bounded queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Sequence
import contextlib
from dataclasses import dataclass, field
import enum
import ipaddress
import logging
import random
import secrets
import socket
import string
import time
from typing import Any, cast

from . import stun
from .stun import Attr, Class, Message, Method

logger = logging.getLogger(__name__)

# Pacing of new checks (RFC 8445 section 14.2).
TA = 0.05
# Retransmission timeouts of one check; about 8 s in all.
CHECK_RTOS = (0.2, 0.4, 0.8, 1.6, 1.6, 1.6, 1.6)
CONSENT_INTERVAL = 5.0
CONSENT_TIMEOUT = 30.0
MAX_PAIRS = 64
MAX_QUEUED = 512
MAX_QUEUED_BEFORE_CONNECT = 32
HOST_TYPE_PREFERENCE = 126
PRFLX_TYPE_PREFERENCE = 110
COMPONENT = 1
ICE_CHARS = string.ascii_letters + string.digits + "+/"


def _random_ice_string(length: int) -> str:
    return "".join(secrets.choice(ICE_CHARS) for _ in range(length))


def candidate_priority(type_preference: int, local_preference: int) -> int:
    """The priority of a component-1 candidate (RFC 8445 section 5.1.2.1)."""
    return (type_preference << 24) | (local_preference << 8) | (256 - COMPONENT)


def pair_priority(controlling: int, controlled: int) -> int:
    """The priority of a pair (RFC 8445 section 6.1.2.3)."""
    low, high = min(controlling, controlled), max(controlling, controlled)
    return (low << 32) + 2 * high + (1 if controlling > controlled else 0)


def _address(addr: Sequence[object]) -> tuple[str, int]:
    """Normalize a socket address to (compressed IP without scope, port)."""
    host = str(addr[0]).split("%", 1)[0]
    with contextlib.suppress(ValueError):
        host = ipaddress.ip_address(host).compressed
    return host, int(str(addr[1]))


@dataclass(frozen=True)
class Candidate:
    """An ICE candidate as in an SDP ``a=candidate`` line."""

    foundation: str
    component: int
    transport: str
    priority: int
    host: str
    port: int
    type: str
    related_address: str | None = None
    related_port: int | None = None

    @classmethod
    def from_sdp(cls, value: str) -> Candidate:
        """Parse the text after ``candidate:``; raises ValueError if malformed."""
        parts = value.split()
        if len(parts) < 8 or parts[6] != "typ":
            raise ValueError(f"Bad candidate {value!r}")
        extensions = dict(zip(parts[8::2], parts[9::2], strict=False))
        rport = extensions.get("rport")
        return cls(
            foundation=parts[0],
            component=int(parts[1]),
            transport=parts[2],
            priority=int(parts[3]),
            host=parts[4],
            port=int(parts[5]),
            type=parts[7],
            related_address=extensions.get("raddr"),
            related_port=int(rport) if rport is not None else None,
        )

    def to_sdp(self) -> str:
        """The text after ``candidate:``."""
        value = (
            f"{self.foundation} {self.component} {self.transport} {self.priority} "
            f"{self.host} {self.port} typ {self.type}"
        )
        if self.related_address is not None and self.related_port is not None:
            value += f" raddr {self.related_address} rport {self.related_port}"
        return value


class PairState(enum.Enum):
    """The state of a candidate pair."""

    WAITING = "waiting"
    IN_PROGRESS = "in progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(eq=False)
class CandidatePair:
    """A local socket and a remote address."""

    local: _Socket
    remote: tuple[str, int]
    remote_priority: int
    state: PairState = PairState.WAITING
    # The controlling peer sent USE-CANDIDATE on this pair.
    remote_nominated: bool = False
    last_response: float = field(default=0.0)

    def priority(self, controlling: bool) -> int:
        """The pair priority from this agent's role."""
        local = self.local.candidate.priority
        if controlling:
            return pair_priority(local, self.remote_priority)
        return pair_priority(self.remote_priority, local)

    def __str__(self) -> str:
        """``local -> remote`` for logs."""
        local = self.local.candidate
        return f"{local.host}:{local.port} -> {self.remote[0]}:{self.remote[1]}"


class _Socket(asyncio.DatagramProtocol):
    """One local UDP socket and its host candidate."""

    def __init__(self, agent: IceAgent) -> None:
        self.agent = agent
        self.transport: asyncio.DatagramTransport | None = None
        self.candidate: Candidate

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast("asyncio.DatagramTransport", transport)

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        self.agent._datagram_received(self, data, _address(addr))

    def error_received(self, exc: Exception) -> None:
        logger.debug("ICE socket error: %s", exc)

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(data, addr)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()


class IceAgent:
    """ICE for one component with host candidates.

    Call :meth:`gather`, then set :attr:`remote_username` and
    :attr:`remote_password` and :meth:`connect`. :meth:`recv` raises
    ConnectionError once the agent is closed, also after consent expired.
    """

    def __init__(
        self,
        *,
        controlling: bool,
        consent_interval: float = CONSENT_INTERVAL,
        consent_timeout: float = CONSENT_TIMEOUT,
    ) -> None:
        """Create the agent; nothing is bound until :meth:`gather`."""
        self.controlling = controlling
        self.local_username = _random_ice_string(8)
        self.local_password = _random_ice_string(24)
        self.remote_username: str | None = None
        self.remote_password: str | None = None
        self.remote_is_lite = False
        self.on_closed: Callable[[str], None] | None = None
        self.tie_breaker = secrets.randbits(64)
        self._consent_interval = consent_interval
        self._consent_timeout = consent_timeout
        self._sockets: list[_Socket] = []
        self._pairs: list[CandidatePair] = []
        self._selected: CandidatePair | None = None
        self._transactions: dict[
            bytes, tuple[CandidatePair, asyncio.Future[Message]]
        ] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._connecting = False
        self._closed = False
        self._close_reason = "closed"

    @property
    def local_candidates(self) -> list[Candidate]:
        """The host candidates gathered."""
        return [s.candidate for s in self._sockets]

    @property
    def selected_pair(self) -> CandidatePair | None:
        """The pair media flows on, once selected."""
        return self._selected

    @property
    def write_buffer_size(self) -> int:
        """Bytes the selected pair's socket could not send yet."""
        pair = self._selected
        transport = pair.local.transport if pair is not None else None
        if transport is None:
            return 0
        # Selector datagram transports have it; typeshed only lists it on
        # WriteTransport.
        size = getattr(transport, "get_write_buffer_size", None)
        return int(size()) if size is not None else 0

    async def gather(self, addresses: Iterable[str]) -> list[Candidate]:
        """Bind one UDP socket per address; returns the host candidates.

        An address that cannot be bound is skipped. Raises ConnectionError
        when the agent is closed before or while binding.
        """
        if self._closed:
            raise ConnectionError("ICE agent is closed")
        if self._sockets:
            return self.local_candidates
        loop = asyncio.get_running_loop()
        for index, address in enumerate(dict.fromkeys(addresses)):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            try:
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: _Socket(self), local_addr=(address, 0), family=family
                )
            except OSError as err:
                logger.debug("ICE cannot bind %s: %s", address, err)
                continue
            if self._closed:
                protocol.close()
                break
            port = transport.get_extra_info("sockname")[1]
            protocol.candidate = Candidate(
                foundation=str(index + 1),
                component=COMPONENT,
                transport="udp",
                priority=candidate_priority(
                    HOST_TYPE_PREFERENCE, max(0, 65535 - index)
                ),
                host=address,
                port=port,
                type="host",
            )
            self._sockets.append(protocol)
            logger.debug("ICE local candidate %s", protocol.candidate.to_sdp())
        if self._closed:
            self._close_sockets()
            raise ConnectionError("ICE agent is closed")
        return self.local_candidates

    async def connect(self, remote_candidates: Iterable[Candidate]) -> None:
        """Run the checks until a pair is selected.

        Raises ConnectionError when every check failed (controlling) or the
        agent is closed; the caller bounds the time.
        """
        if self._closed:
            raise ConnectionError("ICE agent is closed")
        if self.remote_username is None or self.remote_password is None:
            raise ConnectionError("Remote ICE credentials are missing")
        if self.remote_is_lite and not self.controlling:
            # A lite peer is always controlled (RFC 8445 section 6.1.1).
            self.controlling = True
        for candidate in remote_candidates:
            logger.debug(
                "ICE remote candidate %s:%s typ %s",
                candidate.host,
                candidate.port,
                candidate.type,
            )
            for sock in self._sockets:
                self._add_pair(
                    sock, (candidate.host, candidate.port), candidate.priority
                )
        self._connecting = True
        # Also covers pairs the remote checked before connect().
        self._spawn(self._start_checks())
        while self._selected is None:
            # Cleared before looking, so a change meanwhile is not missed.
            self._wake.clear()
            if self._closed:
                raise ConnectionError(f"ICE agent is closed: {self._close_reason}")
            if self.controlling:
                succeeded = [p for p in self._pairs if p.state is PairState.SUCCEEDED]
                if succeeded:
                    best = max(succeeded, key=lambda p: p.priority(True))
                    logger.debug("ICE nominating %s", best)
                    if await self._check(best, nominate=True) and self.controlling:
                        self._select(best)
                    continue
                if self._pairs and all(
                    p.state is PairState.FAILED for p in self._pairs
                ):
                    raise ConnectionError("ICE checks failed")
            await self._wake.wait()
        self._spawn(self._consent())

    def _add_pair(
        self, sock: _Socket, remote: tuple[str, int], remote_priority: int
    ) -> CandidatePair | None:
        remote = _address(remote)
        if (":" in remote[0]) != (":" in sock.candidate.host):
            return None
        for pair in self._pairs:
            if pair.local is sock and pair.remote == remote:
                return pair
        if len(self._pairs) >= MAX_PAIRS:
            return None
        pair = CandidatePair(sock, remote, remote_priority)
        self._pairs.append(pair)
        logger.debug("ICE pair %s", pair)
        return pair

    async def _start_checks(self) -> None:
        ordered = sorted(
            self._pairs, key=lambda p: p.priority(self.controlling), reverse=True
        )
        for pair in ordered:
            if self._selected is not None or self._closed:
                return
            if pair.state is PairState.WAITING:
                self._spawn(self._check(pair))
                await asyncio.sleep(TA)

    async def _check(self, pair: CandidatePair, nominate: bool = False) -> bool:
        """Run one connectivity check; True when it succeeded."""
        if not nominate:
            if pair.state is not PairState.WAITING:
                return pair.state is PairState.SUCCEEDED
            pair.state = PairState.IN_PROGRESS
        response = None
        for _ in range(2):
            sent_controlling = self.controlling
            response = await self._transact(pair, nominate=nominate)
            if response is not None and response.error_code == 487:
                # Take the other role than the request had, unless a request
                # of the peer already switched it (RFC 8445 section 7.2.5.1).
                if self.controlling == sent_controlling:
                    self._switch_role("487 role conflict response")
                continue
            break
        ok = response is not None and response.message_class is Class.SUCCESS
        if ok:
            pair.state = PairState.SUCCEEDED
            pair.last_response = time.monotonic()
            logger.debug(
                "ICE check %s succeeded%s", pair, " (nominated)" if nominate else ""
            )
            if not self.controlling and pair.remote_nominated:
                self._select(pair)
        else:
            pair.state = PairState.FAILED
            logger.debug("ICE check %s failed", pair)
        self._wake.set()
        return ok

    def _request(self, nominate: bool) -> Message:
        attributes: dict[int, bytes] = {
            Attr.USERNAME: f"{self.remote_username}:{self.local_username}".encode(),
            Attr.PRIORITY: candidate_priority(PRFLX_TYPE_PREFERENCE, 65535).to_bytes(
                4, "big"
            ),
        }
        role = Attr.ICE_CONTROLLING if self.controlling else Attr.ICE_CONTROLLED
        attributes[role] = self.tie_breaker.to_bytes(8, "big")
        if nominate and self.controlling:
            attributes[Attr.USE_CANDIDATE] = b""
        return Message(Method.BINDING, Class.REQUEST, attributes=attributes)

    async def _transact(
        self,
        pair: CandidatePair,
        nominate: bool = False,
        rtos: Sequence[float] | None = None,
    ) -> Message | None:
        """Send a request with retransmissions; the response or None."""
        assert self.remote_password is not None  # noqa: S101 - set before checks
        request = self._request(nominate)
        data = stun.encode(request, self.remote_password.encode())
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._transactions[request.transaction_id] = (pair, future)
        try:
            for rto in CHECK_RTOS if rtos is None else rtos:
                if self._closed:
                    return None
                pair.local.sendto(data, pair.remote)
                with contextlib.suppress(TimeoutError):
                    return await asyncio.wait_for(asyncio.shield(future), rto)
            return None
        finally:
            self._transactions.pop(request.transaction_id, None)
            future.cancel()

    def _select(self, pair: CandidatePair) -> None:
        if self._selected is not None:
            return
        self._selected = pair
        logger.debug(
            "ICE selected %s (%s)",
            pair,
            "controlling" if self.controlling else "controlled",
        )
        self._wake.set()

    def _switch_role(self, why: str) -> None:
        self.controlling = not self.controlling
        logger.debug(
            "ICE role is now %s: %s",
            "controlling" if self.controlling else "controlled",
            why,
        )
        self._wake.set()

    async def _consent(self) -> None:
        """Keep consent fresh on the selected pair (RFC 7675)."""
        pair = self._selected
        assert pair is not None  # noqa: S101 - started after selection
        pair.last_response = max(pair.last_response, time.monotonic())
        while not self._closed:
            await asyncio.sleep(self._consent_interval * random.uniform(0.8, 1.2))  # noqa: S311
            if self._closed:
                return
            self._spawn(self._consent_check(pair))
            silent = time.monotonic() - pair.last_response
            if silent > self._consent_timeout:
                logger.debug("ICE consent expired on %s after %.0f s", pair, silent)
                await self.close("consent expired")
                return

    async def _consent_check(self, pair: CandidatePair) -> None:
        # One transmission; the next round is the retransmission.
        response = await self._transact(pair, rtos=(self._consent_interval,))
        if response is not None and response.message_class is Class.SUCCESS:
            pair.last_response = time.monotonic()
            logger.debug("ICE consent refreshed on %s", pair)

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.debug("ICE task failed: %r", error)

    def _datagram_received(
        self, sock: _Socket, data: bytes, addr: tuple[str, int]
    ) -> None:
        if self._closed or not data:
            return
        if stun.is_stun(data):
            try:
                message = stun.parse(data)
            except stun.StunError as err:
                logger.debug("ICE dropping STUN from %s: %s", addr, err)
                return
            if message.message_class is Class.REQUEST:
                self._handle_request(sock, message, addr)
            elif message.message_class in (Class.SUCCESS, Class.ERROR):
                self._handle_response(sock, message, addr)
            return
        if not self._data_allowed(sock, addr):
            return
        limit = MAX_QUEUED if self._selected is not None else MAX_QUEUED_BEFORE_CONNECT
        if self._queue.qsize() < limit:
            self._queue.put_nowait(data)

    def _data_allowed(self, sock: _Socket, addr: tuple[str, int]) -> bool:
        selected = self._selected
        if selected is not None and selected.local is sock and selected.remote == addr:
            return True
        # The peer may send on another pair that passed its checks; before
        # selection, any pair of the check list is a valid source.
        return any(
            p.local is sock
            and p.remote == addr
            and (selected is None or p.state is PairState.SUCCEEDED)
            for p in self._pairs
        )

    def _handle_request(
        self, sock: _Socket, request: Message, addr: tuple[str, int]
    ) -> None:
        username = request.username or ""
        if (
            request.method is not Method.BINDING
            or not username.startswith(f"{self.local_username}:")
            or not stun.check_integrity(request, self.local_password.encode())
        ):
            logger.debug("ICE dropping unauthenticated request from %s", addr)
            return
        theirs_controlling = request.tie_breaker(Attr.ICE_CONTROLLING)
        theirs_controlled = request.tie_breaker(Attr.ICE_CONTROLLED)
        if self.controlling and theirs_controlling is not None:
            if self.tie_breaker >= theirs_controlling:
                self._respond_error(sock, request, addr, 487, "Role Conflict")
                return
            self._switch_role("peer is controlling with a higher tie-breaker")
        elif not self.controlling and theirs_controlled is not None:
            if self.tie_breaker >= theirs_controlled:
                self._switch_role("peer is controlled with a lower tie-breaker")
            else:
                self._respond_error(sock, request, addr, 487, "Role Conflict")
                return
        response = Message(
            Method.BINDING,
            Class.SUCCESS,
            transaction_id=request.transaction_id,
            attributes={
                Attr.XOR_MAPPED_ADDRESS: stun.xor_address(
                    addr[0], addr[1], request.transaction_id
                )
            },
        )
        sock.sendto(stun.encode(response, self.local_password.encode()), addr)
        pair = self._add_pair(sock, addr, request.priority or 0)
        if pair is None:
            return
        if Attr.USE_CANDIDATE in request and not self.controlling:
            if not pair.remote_nominated:
                logger.debug("ICE peer nominated %s", pair)
            pair.remote_nominated = True
            if pair.state is PairState.SUCCEEDED:
                self._select(pair)
                return
        # Triggered check (RFC 8445 section 7.3.1.4); needs the remote
        # password, so before connect() it runs when connect() starts.
        if pair.state is PairState.WAITING and self._connecting:
            self._spawn(self._check(pair))
        elif pair.state is PairState.FAILED and self._connecting:
            pair.state = PairState.WAITING
            self._spawn(self._check(pair))

    def _respond_error(
        self,
        sock: _Socket,
        request: Message,
        addr: tuple[str, int],
        code: int,
        reason: str,
    ) -> None:
        response = Message(
            Method.BINDING,
            Class.ERROR,
            transaction_id=request.transaction_id,
            attributes={Attr.ERROR_CODE: stun.error_value(code, reason)},
        )
        sock.sendto(stun.encode(response, self.local_password.encode()), addr)

    def _handle_response(
        self, sock: _Socket, response: Message, addr: tuple[str, int]
    ) -> None:
        entry = self._transactions.get(response.transaction_id)
        if entry is None:
            return
        pair, future = entry
        password = self.remote_password
        if (
            pair.local is not sock
            or pair.remote != addr
            or password is None
            or not stun.check_integrity(response, password.encode())
        ):
            logger.debug("ICE dropping unauthenticated response from %s", addr)
            return
        if not future.done():
            future.set_result(response)

    async def send(self, data: bytes) -> None:
        """Send one datagram on the selected pair."""
        pair = self._selected
        if pair is None or self._closed:
            raise ConnectionError("ICE is not connected")
        pair.local.sendto(data, pair.remote)

    async def recv(self) -> bytes:
        """Receive one non-STUN datagram; ConnectionError once closed."""
        data = await self._queue.get()
        if data is None:
            self._queue.put_nowait(None)
            raise ConnectionError(f"ICE closed: {self._close_reason}")
        return data

    async def close(self, reason: str = "closed") -> None:
        """Stop checks and consent and release the sockets."""
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        self._wake.set()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._close_sockets()
        self._queue.put_nowait(None)
        if self.on_closed is not None and reason != "closed":
            try:
                self.on_closed(reason)
            except Exception:
                logger.exception("Error in ICE close callback")

    def _close_sockets(self) -> None:
        sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


async def resolve_mdns(name: str, timeout: float) -> str | None:
    """Resolve an mDNS ``.local`` host name to an IP address with zeroconf."""
    from zeroconf import AddressResolver  # noqa: PLC0415 - rarely needed
    from zeroconf.asyncio import AsyncZeroconf  # noqa: PLC0415

    zeroconf = AsyncZeroconf()
    try:
        resolver = AddressResolver(name if name.endswith(".") else f"{name}.")
        if not await resolver.async_request(zeroconf.zeroconf, int(timeout * 1000)):
            return None
        addresses = resolver.parsed_scoped_addresses()
        return addresses[0] if addresses else None
    finally:
        await zeroconf.async_close()
