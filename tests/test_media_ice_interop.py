"""Interop of pyhap.media.ice with aioice, a full ICE implementation."""

from __future__ import annotations

import asyncio

import pytest

from pyhap.media.ice import Candidate, IceAgent

aioice = pytest.importorskip("aioice")

pytestmark = pytest.mark.timeout(10)


async def peer(controlling: bool) -> tuple[aioice.Connection, list[Candidate]]:
    """An aioice peer with host candidates on the machine's interfaces."""
    connection = aioice.Connection(ice_controlling=controlling, use_ipv6=False)
    await connection.gather_candidates()
    candidates = [
        Candidate.from_sdp(c.to_sdp())
        for c in connection.local_candidates
        if c.type == "host"
    ]
    if not candidates:
        await connection.close()
        pytest.skip("No non-loopback IPv4 address")
    return connection, candidates


@pytest.mark.parametrize("controlling", [True, False], ids=["ours", "theirs"])
async def test_connects_with_aioice(controlling: bool) -> None:
    """Verify our agent and aioice connect and exchange data, in both roles."""
    other, other_candidates = await peer(not controlling)
    agent = IceAgent(controlling=controlling)
    ours = await agent.gather([other_candidates[0].host])
    agent.remote_username = other.local_username
    agent.remote_password = other.local_password
    other.remote_username = agent.local_username
    other.remote_password = agent.local_password
    for candidate in ours:
        await other.add_remote_candidate(aioice.Candidate.from_sdp(candidate.to_sdp()))
    await other.add_remote_candidate(None)
    try:
        await asyncio.wait_for(
            asyncio.gather(agent.connect(other_candidates), other.connect()), 8
        )
        await agent.send(b"\x80ours")
        assert await asyncio.wait_for(other.recv(), 2) == b"\x80ours"
        await other.send(b"\x80theirs")
        assert await asyncio.wait_for(agent.recv(), 2) == b"\x80theirs"
    finally:
        await agent.close()
        await other.close()
