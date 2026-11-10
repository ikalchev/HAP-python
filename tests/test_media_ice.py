"""Tests for pyhap.media.ice over loopback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import socket
from typing import cast

import pytest

from pyhap.media import ice, stun
from pyhap.media.ice import Candidate, IceAgent
from pyhap.media.stun import Attr, Class, Message, Method

pytestmark = pytest.mark.timeout(5)

LOOPBACK = ["127.0.0.1"]


@pytest.fixture(autouse=True)
def fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten retransmissions so failing checks end quickly."""
    monkeypatch.setattr(ice, "CHECK_RTOS", (0.05, 0.1, 0.2))
    monkeypatch.setattr(ice, "TA", 0.0)


async def connect_pair(
    first: IceAgent, second: IceAgent
) -> tuple[list[Candidate], list[Candidate]]:
    """Gather, exchange credentials and connect two agents."""
    first_candidates = await first.gather(LOOPBACK)
    second_candidates = await second.gather(LOOPBACK)
    first.remote_username, first.remote_password = (
        second.local_username,
        second.local_password,
    )
    second.remote_username, second.remote_password = (
        first.local_username,
        first.local_password,
    )
    await asyncio.wait_for(
        asyncio.gather(
            first.connect(second_candidates), second.connect(first_candidates)
        ),
        3,
    )
    return first_candidates, second_candidates


@pytest.fixture
async def agents() -> AsyncIterator[tuple[IceAgent, IceAgent]]:
    """Yield a connected (controlling, controlled) pair."""
    controlling, controlled = IceAgent(controlling=True), IceAgent(controlling=False)
    await connect_pair(controlling, controlled)
    yield controlling, controlled
    await controlling.close()
    await controlled.close()


async def test_both_roles_select_the_same_pair(
    agents: tuple[IceAgent, IceAgent],
) -> None:
    """Verify both agents select the pair between their sockets."""
    controlling, controlled = agents
    assert controlling.selected_pair is not None
    assert controlled.selected_pair is not None
    local = controlling.local_candidates[0]
    assert controlled.selected_pair.remote == (local.host, local.port)
    await controlling.send(b"\x80hello")
    assert await asyncio.wait_for(controlled.recv(), 1) == b"\x80hello"
    await controlled.send(b"\x80back")
    assert await asyncio.wait_for(controlling.recv(), 1) == b"\x80back"


@pytest.mark.parametrize("controlling", [True, False])
async def test_role_conflict_is_resolved(controlling: bool) -> None:
    """Verify two agents in the same role settle on opposite roles."""
    first, second = IceAgent(controlling=controlling), IceAgent(controlling=controlling)
    first.tie_breaker, second.tie_breaker = 2, 1
    await connect_pair(first, second)
    assert first.controlling
    assert not second.controlling
    assert first.selected_pair is not None
    assert second.selected_pair is not None
    await first.close()
    await second.close()


class Responder(asyncio.DatagramProtocol):
    """A peer that only answers checks, like a controller's relay."""

    def __init__(self, password: str, drop: int = 0, verify: bool = True) -> None:
        self.password = password.encode()
        self.drop = drop
        self.verify = verify
        self.requests: list[Message] = []
        self.silent = False
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast("asyncio.DatagramTransport", transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not stun.is_stun(data) or self.silent:
            return
        request = stun.parse(data)
        if self.verify and not stun.check_integrity(request, self.password):
            return
        self.requests.append(request)
        if self.drop:
            self.drop -= 1
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
        assert self.transport is not None
        self.transport.sendto(stun.encode(response, self.password), addr)


async def responder(
    password: str = "relaypassword-relaypassword", drop: int = 0, verify: bool = True
) -> tuple[Responder, Candidate]:
    """Bind a responder on loopback and return it with its candidate."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: Responder(password, drop, verify), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    candidate = Candidate("1", 1, "udp", 100, "127.0.0.1", port, "relay")
    return protocol, candidate


async def relay_agent(
    candidate: Candidate, password: str = "relaypassword-relaypassword", **kwargs: float
) -> IceAgent:
    """A controlling agent pointed at one relay candidate."""
    agent = IceAgent(controlling=True, **kwargs)
    await agent.gather(LOOPBACK)
    agent.remote_username, agent.remote_password = "relay", password
    await asyncio.wait_for(agent.connect([candidate]), 3)
    return agent


async def test_controlling_against_answering_relay() -> None:
    """Verify a peer that never checks is reached by our checks and nomination."""
    relay, candidate = await responder()
    agent = await relay_agent(candidate)
    assert agent.selected_pair is not None
    assert agent.selected_pair.remote == ("127.0.0.1", candidate.port)
    assert Attr.USE_CANDIDATE in relay.requests[-1]
    assert relay.requests[0].username == f"relay:{agent.local_username}"
    assert relay.requests[0].tie_breaker(Attr.ICE_CONTROLLING) == agent.tie_breaker
    await agent.close()


async def test_lost_check_is_retransmitted() -> None:
    """Verify a check succeeds on a retransmission."""
    relay, candidate = await responder(drop=1)
    agent = await relay_agent(candidate)
    assert agent.selected_pair is not None
    first, second = relay.requests[:2]
    assert first.transaction_id == second.transaction_id
    await agent.close()


async def test_checks_fail_without_answers() -> None:
    """Verify connect() fails once every check timed out."""
    relay, candidate = await responder()
    relay.silent = True
    with pytest.raises(ConnectionError, match="checks failed"):
        await relay_agent(candidate)
    assert len(relay.requests) == 0


async def test_response_with_wrong_integrity_is_ignored() -> None:
    """Verify a response authenticated with another password does not count."""
    relay, candidate = await responder(
        password="other-password-other-password", verify=False
    )
    with pytest.raises(ConnectionError, match="checks failed"):
        await relay_agent(candidate)
    assert relay.requests


async def test_consent_expires_without_responses() -> None:
    """Verify the agent closes when consent is not refreshed."""
    relay, candidate = await responder()
    agent = await relay_agent(candidate, consent_interval=0.05, consent_timeout=0.3)
    reasons: list[str] = []
    agent.on_closed = reasons.append
    await asyncio.sleep(0.15)
    assert len(relay.requests) > 2  # consent checks went out
    relay.silent = True
    with pytest.raises(ConnectionError, match="consent expired"):
        await asyncio.wait_for(agent.recv(), 2)
    assert reasons == ["consent expired"]


async def test_consent_kept_while_answered() -> None:
    """Verify answered consent checks keep the agent open."""
    relay, candidate = await responder()
    agent = await relay_agent(candidate, consent_interval=0.05, consent_timeout=0.3)
    await asyncio.sleep(0.6)
    assert agent.selected_pair is not None
    assert not agent._closed
    await agent.close()


async def test_data_only_from_validated_addresses(
    agents: tuple[IceAgent, IceAgent],
) -> None:
    """Verify data from an address without a pair is dropped."""
    controlling, controlled = agents
    local = controlled.local_candidates[0]
    stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        stranger.sendto(b"\x80spoofed", (local.host, local.port))
        await asyncio.sleep(0.05)
        await controlling.send(b"\x80real")
        assert await asyncio.wait_for(controlled.recv(), 1) == b"\x80real"
        assert controlled._queue.empty()
    finally:
        stranger.close()


async def test_unauthenticated_request_is_not_answered() -> None:
    """Verify a check with a wrong password gets no response."""
    agent = IceAgent(controlling=False)
    (local,) = await agent.gather(LOOPBACK)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(0.2)
    try:
        request = Message(
            Method.BINDING,
            Class.REQUEST,
            attributes={Attr.USERNAME: f"{agent.local_username}:x".encode()},
        )
        probe.sendto(stun.encode(request, b"wrong"), (local.host, local.port))
        with pytest.raises(TimeoutError):
            probe.recvfrom(1500)
    finally:
        probe.close()
        await agent.close()


async def test_queue_is_bounded(agents: tuple[IceAgent, IceAgent]) -> None:
    """Verify a burst beyond the queue bound is dropped."""
    _controlling, controlled = agents
    pair = controlled.selected_pair
    assert pair is not None
    for index in range(ice.MAX_QUEUED + 5):
        controlled._datagram_received(
            pair.local, bytes([0x80, index & 0x7F]), pair.remote
        )
    assert controlled._queue.qsize() == ice.MAX_QUEUED


async def test_lite_remote_makes_us_controlling() -> None:
    """Verify a controlled agent turns controlling for a lite peer."""
    relay, candidate = await responder()
    agent = IceAgent(controlling=False)
    await agent.gather(LOOPBACK)
    agent.remote_username, agent.remote_password = (
        "relay",
        "relaypassword-relaypassword",
    )
    agent.remote_is_lite = True
    await asyncio.wait_for(agent.connect([candidate]), 3)
    assert agent.controlling
    assert agent.selected_pair is not None
    await agent.close()


async def test_send_before_connect_fails() -> None:
    """Verify sending without a selected pair raises ConnectionError."""
    agent = IceAgent(controlling=True)
    with pytest.raises(ConnectionError):
        await agent.send(b"x")
    await agent.close()
    with pytest.raises(ConnectionError):
        await agent.gather(LOOPBACK)


async def test_unbindable_address_is_skipped() -> None:
    """Verify an address that cannot be bound gives no candidate."""
    agent = IceAgent(controlling=True)
    candidates = await agent.gather(["192.0.2.123", "127.0.0.1"])
    assert [c.host for c in candidates] == ["127.0.0.1"]
    await agent.close()


def test_candidate_sdp_roundtrip() -> None:
    """Verify candidates parse and print, with related addresses."""
    text = "abc 1 udp 2130706431 192.0.2.1 5000 typ srflx raddr 10.0.0.1 rport 6000"
    candidate = Candidate.from_sdp(text)
    assert candidate.type == "srflx"
    assert candidate.related_port == 6000
    assert candidate.to_sdp() == text


@pytest.mark.parametrize(
    "text", ["1 1 udp 1 10.0.0.1 1000", "1 1 udp 1 10.0.0.1 1000 type host", "x"]
)
def test_bad_candidates(text: str) -> None:
    """Verify malformed candidates raise ValueError."""
    with pytest.raises(ValueError):
        Candidate.from_sdp(text)


def test_pair_priority_formula() -> None:
    """Verify the RFC 8445 pair priority is symmetric between the roles."""
    assert ice.pair_priority(10, 5) == (5 << 32) + 20 + 1
    assert ice.pair_priority(5, 10) == (5 << 32) + 20
