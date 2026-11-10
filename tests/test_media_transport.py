"""Tests for pyhap.media.transport over loopback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from pyhap.media import transport  # noqa: E402
from pyhap.media.transport import (  # noqa: E402
    Certificate,
    DtlsRole,
    DtlsSrtpTransport,
    TransportError,
)

pytestmark = pytest.mark.timeout(5)

RTP = bytes([0x80, 100, 0, 1, 0, 0, 0, 1, 0, 0, 0, 7]) + b"payload"
RTCP_PLI = bytes([0x81, 206, 0, 2, 0, 0, 0, 1, 0, 0, 0, 7])


def make_transport(controlling: bool) -> DtlsSrtpTransport:
    """Create a loopback-only transport."""
    return DtlsSrtpTransport(
        ice_controlling=controlling, host_addresses=["127.0.0.1"], use_ipv6=False
    )


async def connect(
    offerer: DtlsSrtpTransport,
    answerer: DtlsSrtpTransport,
    offerer_role: DtlsRole,
    answerer_fingerprint: str | None = None,
) -> None:
    """Exchange parameters and connect both sides."""
    offer_candidates = await offerer.gather()
    answer_candidates = await answerer.gather()
    answerer_role = (
        DtlsRole.SERVER if offerer_role is DtlsRole.CLIENT else DtlsRole.CLIENT
    )
    await asyncio.gather(
        offerer.connect(
            remote_username=answerer.local_username,
            remote_password=answerer.local_password,
            remote_candidates=answer_candidates,
            remote_fingerprints=[
                ("sha-256", answerer_fingerprint or answerer.certificate.fingerprint)
            ],
            role=offerer_role,
            timeout=5,
        ),
        answerer.connect(
            remote_username=offerer.local_username,
            remote_password=offerer.local_password,
            remote_candidates=offer_candidates,
            remote_fingerprints=[("sha-256", offerer.certificate.fingerprint)],
            role=answerer_role,
            timeout=5,
        ),
    )


@pytest.fixture(params=[DtlsRole.CLIENT, DtlsRole.SERVER], ids=["client", "server"])
async def pair(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[DtlsSrtpTransport, DtlsSrtpTransport]]:
    """Yield a connected (offerer, answerer) pair."""
    offerer, answerer = make_transport(True), make_transport(False)
    await connect(offerer, answerer, request.param)
    yield offerer, answerer
    await offerer.close()
    await answerer.close()


def collect(target: DtlsSrtpTransport) -> tuple[asyncio.Queue[bytes], ...]:
    """Route received RTP and RTCP into queues."""
    rtp: asyncio.Queue[bytes] = asyncio.Queue()
    rtcp: asyncio.Queue[bytes] = asyncio.Queue()
    target.on_rtp = rtp.put_nowait
    target.on_rtcp = rtcp.put_nowait
    return rtp, rtcp


async def closed_reason(target: DtlsSrtpTransport) -> asyncio.Future[str]:
    """Return a future resolved with the close reason."""
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    target.on_closed = future.set_result
    return future


async def test_srtp_both_directions(
    pair: tuple[DtlsSrtpTransport, DtlsSrtpTransport],
) -> None:
    """Verify RTP and RTCP are protected and unprotected both ways."""
    offerer, answerer = pair
    answer_rtp, _ = collect(answerer)
    _, offer_rtcp = collect(offerer)
    await offerer.send_rtp(RTP)
    await answerer.send_rtcp(RTCP_PLI)
    assert await asyncio.wait_for(answer_rtp.get(), 2) == RTP
    assert await asyncio.wait_for(offer_rtcp.get(), 2) == RTCP_PLI
    assert offerer.connected
    assert offerer.srtp_profile == answerer.srtp_profile


async def test_close_notify_reaches_peer(
    pair: tuple[DtlsSrtpTransport, DtlsSrtpTransport],
) -> None:
    """Verify closing one side closes the other through DTLS close_notify."""
    offerer, answerer = pair
    reason = await closed_reason(answerer)
    await offerer.close()
    assert await asyncio.wait_for(reason, 2) == "dtls closed by remote"
    assert answerer.closed
    assert not offerer.connected


async def test_fingerprint_mismatch() -> None:
    """Verify a peer certificate not matching the SDP is refused."""
    offerer, answerer = make_transport(True), make_transport(False)
    wrong = Certificate().fingerprint
    with pytest.raises(TransportError):
        await connect(offerer, answerer, DtlsRole.CLIENT, answerer_fingerprint=wrong)
    await offerer.close()
    await answerer.close()


async def test_connect_timeout() -> None:
    """Verify ICE without a reachable peer times out."""
    lone = make_transport(True)
    await lone.gather()
    with pytest.raises(TransportError, match="timed out"):
        await lone.connect(
            remote_username="user",
            remote_password="password0123456789abcd",
            remote_candidates=["candidate:1 1 udp 1 127.0.0.1 9 typ host", "bogus"],
            remote_fingerprints=[("sha-256", "00")],
            role=DtlsRole.CLIENT,
            timeout=0.5,
        )
    await lone.close()
    await lone.close()


async def test_connect_after_close_fails() -> None:
    """Verify a closed transport cannot connect."""
    lone = make_transport(True)
    await lone.close()
    with pytest.raises(TransportError, match="closed"):
        await lone.connect(
            remote_username="u",
            remote_password="p",
            remote_candidates=[],
            remote_fingerprints=[],
            role=DtlsRole.CLIENT,
        )


async def test_send_before_connect_is_dropped() -> None:
    """Verify sending before SRTP is ready is a no-op."""
    lone = make_transport(True)
    await lone.send_rtp(RTP)
    await lone.send_rtcp(RTCP_PLI)
    assert not lone.connected


@pytest.mark.parametrize(
    ("setup", "role", "answer_setup"),
    [
        ("active", DtlsRole.SERVER, "passive"),
        ("passive", DtlsRole.CLIENT, "active"),
        (None, DtlsRole.CLIENT, "active"),
    ],
)
def test_role_from_setup(setup: str | None, role: DtlsRole, answer_setup: str) -> None:
    """Verify the remote a=setup decides the local DTLS role."""
    assert DtlsRole.from_remote_setup(setup) is role
    assert role.setup == answer_setup


def record(length: int, content_type: int = 22) -> bytes:
    """Build a DTLS record with a body of the given length."""
    header = bytes([content_type, 0xFE, 0xFD]) + bytes(8)
    return header + length.to_bytes(2, "big") + bytes(length)


@pytest.mark.parametrize(
    ("lengths", "expected"),
    [
        ([100], [113]),
        ([100, 200], [326]),
        ([600, 600], [613, 613]),
        ([1500, 10], [1513, 23]),
        ([10, 1500, 10], [23, 1513, 23]),
    ],
)
def test_pack_records(lengths: list[int], expected: list[int]) -> None:
    """Verify records are packed whole into datagrams up to the MTU."""
    data = b"".join(map(record, lengths))
    assert list(map(len, transport.pack_records(data, 1200))) == expected


def test_pack_records_truncated_tail() -> None:
    """Verify a trailing fragment shorter than a header is still sent."""
    assert transport.pack_records(record(5) + b"\x16\xfe") == [record(5) + b"\x16\xfe"]


@pytest.mark.parametrize(
    ("packet", "rtcp"),
    [(RTP, False), (RTCP_PLI, True), (bytes([0x80, 200]), True), (b"\x80", False)],
    ids=["rtp", "pli", "sr", "short"],
)
def test_is_rtcp(packet: bytes, rtcp: bool) -> None:
    """Verify RTP/RTCP demultiplexing by payload type."""
    assert transport.is_rtcp(packet) is rtcp


def test_certificate_fingerprint_format() -> None:
    """Verify the fingerprint is 32 colon-separated hex pairs."""
    value = Certificate().fingerprint
    assert len(value.split(":")) == 32
    assert value == value.upper()


async def test_failing_callback_keeps_transport(
    pair: tuple[DtlsSrtpTransport, DtlsSrtpTransport],
) -> None:
    """Verify an exception in a consumer does not stop receiving."""
    offerer, answerer = pair
    received: asyncio.Queue[bytes] = asyncio.Queue()
    answerer.on_rtp = fail_once(received)
    await offerer.send_rtp(RTP)
    await offerer.send_rtp(RTP[:3] + b"\x02" + RTP[4:])
    assert (await asyncio.wait_for(received.get(), 2))[3] == 2
    assert answerer.connected


def fail_once(queue: asyncio.Queue[bytes]) -> Callable[[bytes], None]:
    """Return a callback that raises on its first packet."""
    calls: list[bytes] = []

    def callback(packet: bytes) -> None:
        calls.append(packet)
        if len(calls) == 1:
            raise RuntimeError("consumer bug")
        queue.put_nowait(packet)

    return callback


def test_remote_candidates_capped_and_deduplicated() -> None:
    """Verify duplicates, other components and TCP go; the cap holds."""
    values = [
        "candidate:1 1 udp 1 10.0.0.1 1000 typ host",
        "a=candidate:1 1 udp 1 10.0.0.1 1000 typ host",
        "candidate:2 2 udp 1 10.0.0.1 1001 typ host",
        "candidate:3 1 tcp 1 10.0.0.1 1002 typ host tcptype passive",
        "garbage",
        *numbered_candidates(40),
    ]
    parsed = transport._remote_candidates(values)
    assert len(parsed) == transport.MAX_REMOTE_CANDIDATES
    assert parsed[0].port == 1000
    assert parsed[1].port == 2000


@pytest.mark.parametrize(
    "value",
    [
        "candidate:1 1 udp 1 10.0.0.1 0 typ host",
        "candidate:1 1 udp 1 10.0.0.1 65536 typ host",
        "candidate:1 1 udp 1 example.com 1000 typ host",
        "candidate:1 1 udp 1 10.0.0.1x 1000 typ host",
        f"candidate:1 1 udp 1 {'a' * 250}.local 1000 typ host",
        "candidate:1 1 udp 1 .local 1000 typ host",
        "candidate:1 1 udp 1 0.0.0.0 1000 typ host",
        "candidate:1 1 udp 1 :: 1000 typ host",
        "candidate:1 1 udp 1 224.0.0.251 1000 typ host",
        "candidate:1 1 udp 1 ff02::1 1000 typ host",
        "candidate:1 1 udp 1 255.255.255.255 1000 typ host",
        "candidate:1 1 udp 1 ::ffff:224.0.0.1 1000 typ host",
        "candidate:1 1 udp 1 ::ffff:255.255.255.255 1000 typ host",
    ],
    ids=[
        "port-0",
        "port-high",
        "dns-name",
        "bad-ip",
        "long-mdns",
        "empty-mdns",
        "unspecified-v4",
        "unspecified-v6",
        "multicast-v4",
        "multicast-v6",
        "broadcast",
        "mapped-multicast",
        "mapped-broadcast",
    ],
)
def test_remote_candidates_drop_unusable_addresses(value: str) -> None:
    """Verify bad ports, non-unicast IPs and hosts other than IP or mDNS drop."""
    assert transport._remote_candidates([value]) == []


@pytest.mark.parametrize(
    "value",
    [
        "candidate:1 1 udp 1 10.0.0.1 65535 typ host",
        "candidate:1 1 udp 1 fe80::1%en0 1000 typ host",
        "candidate:1 1 udp 1 0a1b2c3d-4e5f.local 1000 typ host",
    ],
    ids=["ipv4", "ipv6-zone", "mdns"],
)
def test_remote_candidates_keep_usable_addresses(value: str) -> None:
    """Verify IP addresses and mDNS names with a valid port are kept."""
    assert len(transport._remote_candidates([value])) == 1


def numbered_candidates(count: int) -> list[str]:
    """Return ``count`` distinct host candidates on ports from 2000."""
    return [
        f"candidate:n{i} 1 udp 1 10.0.1.1 {2000 + i} typ host" for i in range(count)
    ]


def test_default_host_addresses_use_lan_interface() -> None:
    """Verify the default is the LAN address's interface, without link-local."""
    # conftest pins the LAN address to 127.0.0.1.
    assert transport.default_host_addresses(use_ipv6=True) == ["127.0.0.1"]


async def test_close_during_gather_releases_sockets() -> None:
    """Verify closing while candidates are gathered binds nothing."""
    lone = make_transport(True)
    gathering = asyncio.ensure_future(lone.gather())
    await asyncio.sleep(0)
    await lone.close()
    with pytest.raises(TransportError, match="closed"):
        await gathering
    assert lone._ice._sockets == []
    with pytest.raises(TransportError, match="closed"):
        await lone.gather()


async def test_read_failure_closes_transport(
    pair: tuple[DtlsSrtpTransport, DtlsSrtpTransport],
) -> None:
    """Verify an unexpected error in the read loop ends the transport."""
    offerer, answerer = pair
    reason = await closed_reason(answerer)
    answerer._handle_srtp = broken  # type: ignore[method-assign]
    await offerer.send_rtp(RTP)
    assert (await asyncio.wait_for(reason, 2)).startswith("read failed")
    assert answerer.closed


def broken(data: bytes) -> None:
    """Fail like a bug in packet handling."""
    raise RuntimeError("bug")


async def test_close_survives_failing_ice_close() -> None:
    """Verify close() completes and reports even when ICE close fails."""
    lone = make_transport(True)
    lone._ice.close = fail_async  # type: ignore[method-assign]
    reasons: list[str] = []
    lone.on_closed = reasons.append
    await lone.close()
    assert reasons == ["closed locally"]
    assert lone.write_buffer_size == 0


async def fail_async(reason: str = "closed") -> None:
    """Fail like a broken socket close."""
    raise OSError("close failed")


async def test_unexpected_connect_error_is_a_transport_error() -> None:
    """Verify any failure while connecting comes out as TransportError."""
    lone = make_transport(True)
    setattr(lone, "_connect", fail_connect)  # noqa: B010 - replaces a method
    with pytest.raises(TransportError, match="mdns"):
        await lone.connect(
            remote_username="u",
            remote_password="p",
            remote_candidates=[],
            remote_fingerprints=[],
            role=DtlsRole.CLIENT,
        )
    await lone.close()


async def fail_connect(*_args: object) -> None:
    """Fail like an mDNS socket that cannot bind."""
    raise OSError("mdns port in use")


async def test_close_waits_for_a_close_under_way(
    pair: tuple[DtlsSrtpTransport, DtlsSrtpTransport],
) -> None:
    """Verify close() returns only when an earlier close has finished."""
    offerer, _answerer = pair
    closing = asyncio.ensure_future(offerer._close("ice connection lost"))
    await asyncio.sleep(0)
    await offerer.close()
    assert offerer._close_done.is_set()
    await closing
