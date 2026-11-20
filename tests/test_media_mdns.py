"""Tests for pyhap.media._mdns, the mDNS names of answer candidates."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
import zeroconf

from pyhap import hksv3
from pyhap.camera_stream import MAX_HOOK_TIMEOUT
from pyhap.media import _mdns, sdp

pytestmark = pytest.mark.timeout(5)

ZEROCONF = cast(Any, object())
NAME = "0a1b2c3d-4e5f.local"
ADDRESS = "192.168.1.20"
CREDENTIALS = (
    "a=ice-ufrag:abcd\r\n"
    "a=ice-pwd:0123456789abcdef012345\r\n"
    f"a=fingerprint:sha-256 {':'.join(['AB'] * 32)}\r\n"
)
ANSWER = (
    "v=0\r\n"
    "o=- 1 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    f"{CREDENTIALS}"
    "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:0\r\n"
)


def _hap(candidate: str) -> hksv3.IceCandidate:
    return hksv3.IceCandidate(candidate, "0", 0)


def _candidate(host: str, number: int = 1) -> hksv3.IceCandidate:
    return _hap(f"candidate:{number} 1 udp 2122260223 {host} 5000 typ host")


def _answers(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]
) -> list[tuple[list[str], float]]:
    """Answer lookups from a table and return what was asked."""
    asked: list[tuple[list[str], float]] = []

    async def lookup(_zeroconf: Any, names: Sequence[str], timeout: float) -> Any:
        asked.append((list(names), timeout))
        return {name: answers[name] for name in names if name in answers}

    monkeypatch.setattr(_mdns, "_lookup", lookup)
    return asked


def _instance(
    loop: asyncio.AbstractEventLoop | None = None, started: bool = True
) -> Any:
    """Stand in for an AsyncZeroconf whose instance runs on ``loop``."""
    return SimpleNamespace(zeroconf=SimpleNamespace(loop=loop, started=started))


async def test_names_are_resolved_in_the_sdp_and_the_hap_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a resolved name is replaced everywhere, however it is written."""
    asked = _answers(monkeypatch, {NAME: ADDRESS})
    answer = (
        ANSWER
        + f"a=candidate:7 1 udp 1 {NAME.upper()} 5000 typ host\r\n"
        + "a=candidate:8 1 udp 1 10.0.0.9 5000 typ host\r\n"
    )

    resolved_sdp, resolved = await _mdns.resolve_answer(
        ZEROCONF, answer, [_candidate(NAME), _candidate("10.0.0.1", 2)]
    )

    assert asked == [([NAME], _mdns.MDNS_TIMEOUT)]
    assert resolved_sdp == answer.replace(NAME.upper(), ADDRESS)
    assert resolved == [_candidate(ADDRESS), _candidate("10.0.0.1", 2)]


async def test_only_the_address_of_a_candidate_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the name is replaced where it is the address, other bytes stay."""
    asked = _answers(monkeypatch, {NAME: ADDRESS})
    line = "a=candidate:{0} 1\tUDP  1 {0} 5000 typ host raddr {0} rport 9"
    answer = ANSWER.replace("\r\n", "\n") + line.format(NAME) + "\na=x"

    resolved_sdp, resolved = await _mdns.resolve_answer(ZEROCONF, answer, [], 0.5)

    assert asked == [([NAME], 0.5)]
    assert resolved_sdp == (
        ANSWER.replace("\r\n", "\n")
        + f"a=candidate:{NAME} 1\tUDP  1 {ADDRESS} 5000 typ host raddr {NAME} rport 9"
        + "\na=x"
    )
    assert resolved == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "224.0.0.251",
        "ff02::fb",
        "0.0.0.0",
        "255.255.255.255",
        "::ffff:255.255.255.255",
        "x",
    ],
    ids=[
        "loopback",
        "loopback-v6",
        "loopback-mapped",
        "multicast",
        "multicast-v6",
        "unspecified",
        "broadcast",
        "broadcast-mapped",
        "no-address",
    ],
)
async def test_answers_that_name_no_peer_are_not_taken(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """Verify a name stays when its answer is not a unicast peer address."""
    _answers(monkeypatch, {NAME: address})

    _, resolved = await _mdns.resolve_answer(ZEROCONF, ANSWER, [_candidate(NAME)])

    assert resolved == [_candidate(NAME)]


async def test_candidates_are_read_as_the_transport_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify only candidates the transport uses have their names asked for."""
    used = [
        _hap("candidate: 1 1 udp 1 spaced.local 5000 typ host"),
        _hap("  a=candidate:2 01 UDP 1 prefixed.local 5000 typ host"),
        _hap("  candidate: 3 1 udp 1 led.local 5000 typ host"),
        _hap("a=candidate: 4 1 udp 1 both.local 5000 typ host"),
    ]
    unused = [
        _hap("candidate:5 1 tcp 1 tcp.local 5000 typ host"),
        _hap("candidate:6 2 udp 1 rtcp.local 5000 typ host"),
        _hap("candidate:7 1 udp 1 port.local 0 typ host"),
        _hap("candidate:8 1 udp 1 short.local 5000"),
        # The transport reads it, but its name cannot be replaced in place.
        _hap("candidate:candidate: 9 1 udp 1 twice.local 5000 typ host"),
    ]
    session_level = "a=candidate:10 1 udp 1 session.local 5000 typ host\r\n"
    answer = ANSWER.replace("m=video", f"{session_level}m=video")
    names = ["spaced.local", "prefixed.local", "led.local", "both.local"]
    extra = ["tcp.local", "rtcp.local", "port.local", "twice.local", "session.local"]
    asked = _answers(monkeypatch, dict.fromkeys([*names, *extra], ADDRESS))

    resolved_sdp, resolved = await _mdns.resolve_answer(
        ZEROCONF, answer, [*unused, *used]
    )

    assert asked == [(names, _mdns.MDNS_TIMEOUT)]
    assert resolved_sdp == answer
    assert resolved[: len(unused)] == unused
    assert [c.candidate for c in resolved[len(unused) :]] == [
        "candidate: 1 1 udp 1 192.168.1.20 5000 typ host",
        "  a=candidate:2 01 UDP 1 192.168.1.20 5000 typ host",
        "  candidate: 3 1 udp 1 192.168.1.20 5000 typ host",
        "a=candidate: 4 1 udp 1 192.168.1.20 5000 typ host",
    ]


@pytest.mark.parametrize(
    ("length", "asked"),
    [(253, 1), (254, 0), (len(".local"), 0)],
    ids=["longest-dns-name", "too-long", "no-label"],
)
async def test_names_of_a_length_dns_has_are_asked_for(
    monkeypatch: pytest.MonkeyPatch, length: int, asked: int
) -> None:
    """Verify an over-long name, or none before the suffix, is not asked for."""
    name = f"{'a' * (length - len('.local'))}.local"
    requests = _answers(monkeypatch, {name: ADDRESS})

    await _mdns.resolve_answer(ZEROCONF, ANSWER, [_candidate(name)])

    assert len(requests) == asked


def test_default_timeout_covers_a_lost_first_answer() -> None:
    """Verify the wait reaches the answer to zeroconf's second question.

    That one came after about 1.3 seconds where the first answer got lost.
    The wait also has to leave the answer hook most of its time.
    """
    assert 1.5 <= _mdns.MDNS_TIMEOUT <= MAX_HOOK_TIMEOUT / 4


async def test_few_names_are_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an answer cannot make the camera ask for many names."""
    names = [f"host-{number}.local" for number in range(6)]
    asked = _answers(monkeypatch, dict.fromkeys(names, ADDRESS))

    _, resolved = await _mdns.resolve_answer(
        ZEROCONF, ANSWER, [_candidate(name) for name in [*names, names[0].upper()]]
    )

    assert asked == [(names[: _mdns.MAX_MDNS_NAMES], _mdns.MDNS_TIMEOUT)]
    assert [c.candidate.split()[4] for c in resolved] == [
        *[ADDRESS] * 4,
        *names[4:],
        ADDRESS,
    ]


@pytest.mark.parametrize(
    ("instance", "answer", "candidates"),
    [
        (None, ANSWER, [_candidate(NAME)]),
        (ZEROCONF, ANSWER, [_candidate("10.0.0.1")]),
        (ZEROCONF, ANSWER + "a=x\r\n" * sdp.MAX_SDP_SIZE, [_candidate(NAME)]),
        (ZEROCONF, ANSWER.replace(CREDENTIALS, ""), [_candidate(NAME)]),
    ],
    ids=["no-instance", "no-names", "too-large", "no-credentials"],
)
async def test_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch,
    instance: Any,
    answer: str,
    candidates: list[hksv3.IceCandidate],
) -> None:
    """Verify an answer is kept as it is when there is nothing to resolve."""
    asked = _answers(monkeypatch, {NAME: ADDRESS})

    assert await _mdns.resolve_answer(instance, answer, candidates) == (
        answer,
        candidates,
    )
    assert asked == []


def _new_instances(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record the Zeroconf instances that get created, and refuse them."""
    created: list[object] = []

    def refuse(instance: object, *_args: object, **_kwargs: object) -> None:
        created.append(instance)
        raise AssertionError("A lookup must not create a Zeroconf instance")

    # Patched on the classes: zeroconf.asyncio holds its own name for Zeroconf.
    monkeypatch.setattr("zeroconf.Zeroconf.__init__", refuse)
    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf.__init__", refuse)
    return created


class _Resolver:
    """Stand in for zeroconf.AddressResolver: answers after a delay, if any."""

    delays: dict[str, float]
    requests: list[tuple[str, Any, int]]
    cancelled: list[str]

    def __init__(self, name: str) -> None:
        self.name = name

    async def async_request(self, instance: Any, timeout: int) -> bool:
        self.requests.append((self.name, instance, timeout))
        delay = self.delays.get(self.name)
        try:
            await asyncio.sleep(9 if delay is None else delay)
        except asyncio.CancelledError:
            self.cancelled.append(self.name)
            raise
        return delay is not None

    def parsed_scoped_addresses(self) -> list[str]:
        return [ADDRESS, "fe80::1%eth0"]


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> type[_Resolver]:
    """Replace zeroconf's resolver by one with a table of answer delays."""
    _Resolver.delays, _Resolver.requests, _Resolver.cancelled = {}, [], []
    monkeypatch.setattr(zeroconf, "AddressResolver", _Resolver)
    return _Resolver


async def test_lookup_uses_the_given_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify every name is asked on the instance; failures leave a name out."""
    table: dict[str, list[str]] = {
        "a.local.": [ADDRESS, "fe80::1%eth0"],
        "b.local.": [],
    }
    requests: list[tuple[str, Any, int]] = []

    class Resolver:
        def __init__(self, name: str) -> None:
            self.name = name

        async def async_request(self, instance: Any, timeout: int) -> bool:
            requests.append((self.name, instance, timeout))
            if self.name == "c.local.":
                raise OSError("no route")
            return self.name in table

        def parsed_scoped_addresses(self) -> list[str]:
            return table[self.name]

    created = _new_instances(monkeypatch)
    monkeypatch.setattr(zeroconf, "AddressResolver", Resolver)
    instance = _instance(asyncio.get_running_loop())

    found = await _mdns._lookup(
        instance, ["a.local", "b.local", "c.local", "d.local"], 1.5
    )

    assert found == {"a.local": ADDRESS}
    assert requests == [(f"{name}.local.", instance.zeroconf, 1500) for name in "abcd"]
    assert created == []


async def test_lookup_takes_what_arrived_in_time(resolver: type[_Resolver]) -> None:
    """Verify answers count as they come, a late one is given up."""
    resolver.delays = {"first.local.": 0.05, "second.local.": 0.4}
    loop = asyncio.get_running_loop()
    started = loop.time()

    found = await _mdns._lookup(
        _instance(loop), ["first.local", "second.local", "never.local"], 0.6
    )
    await asyncio.sleep(0)

    assert found == {"first.local": ADDRESS, "second.local": ADDRESS}
    assert loop.time() - started < 1.0
    assert resolver.cancelled == ["never.local."]


async def test_lookup_is_given_up_with_its_caller(resolver: type[_Resolver]) -> None:
    """Verify the requests end when the caller is cancelled, e.g. its hook."""
    lookup = asyncio.ensure_future(
        _mdns._lookup(_instance(asyncio.get_running_loop()), ["never.local"], 3)
    )
    await asyncio.sleep(0.05)

    lookup.cancel()
    await asyncio.wait([lookup])
    await asyncio.sleep(0)

    assert lookup.cancelled()
    assert resolver.cancelled == ["never.local."]


@pytest.mark.parametrize(
    "case", ["other-loop", "not-started", "old-zeroconf", "no-names"]
)
async def test_lookup_without_a_usable_zeroconf(
    monkeypatch: pytest.MonkeyPatch, resolver: type[_Resolver], case: str
) -> None:
    """Verify nothing is asked where a request could not work or do harm."""
    loop = asyncio.get_running_loop()
    other = asyncio.new_event_loop()
    instance = {
        "other-loop": _instance(other),
        # Giving up on a request would cancel the start of the instance.
        "not-started": _instance(loop, started=False),
        "old-zeroconf": _instance(loop),
        "no-names": _instance(loop),
    }[case]
    monkeypatch.setitem(
        sys.modules, "zeroconf", None if case == "old-zeroconf" else zeroconf
    )

    names = [] if case == "no-names" else ["a.local"]
    assert await _mdns._lookup(instance, names, 0.2) == {}
    assert resolver.requests == []
    other.close()
