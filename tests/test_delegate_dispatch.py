"""Tests for the delegate write dispatch of the driver."""

import asyncio
from collections.abc import Mapping
import inspect
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid1, uuid4

import pytest

from pyhap.accessory import STANDALONE_AID, Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import (
    HAP_FORMAT_INT,
    HAP_PERMISSION_READ,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.connection import HAPConnectionHandle
from pyhap.const import (
    HAP_PERMISSION_NOTIFY,
    HAP_PERMISSION_WRITE,
    HAP_PERMISSION_WRITE_RESPONSE,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
    HAP_REPR_WRITE_RESPONSE,
    HAP_SERVER_STATUS,
)
from pyhap.context import CharReadContext, CharWriteContext, ServiceWriteContext
from pyhap.delegates import CharacteristicDelegate, ServiceDelegate
from pyhap.errors import HapStatusError
from pyhap.service import Service

WRITER = ("1.2.3.4", 5)
OTHER_CONTROLLER = ("5.6.7.8", 9)
ASYNC_TIMEOUT = 1.0
CHAR_PROPS = {
    PROP_FORMAT: HAP_FORMAT_INT,
    PROP_PERMISSIONS: [
        HAP_PERMISSION_READ,
        HAP_PERMISSION_WRITE,
        HAP_PERMISSION_NOTIFY,
    ],
}
NO_VALUE = object()


def _add_service_with_chars(
    driver: AccessoryDriver, count: int = 1, permissions: list[str] | None = None
) -> tuple[Accessory, Service, list[Characteristic]]:
    """Add an accessory with one service of ``count`` int characteristics."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)
    service = Service(uuid1(), "Test Service")
    chars = []
    for index in range(count):
        char = Characteristic(
            f"Value{index}",
            uuid1(),
            {
                PROP_FORMAT: HAP_FORMAT_INT,
                PROP_PERMISSIONS: list(permissions or CHAR_PROPS[PROP_PERMISSIONS]),
            },
        )
        service.add_characteristic(char)
        chars.append(char)
    acc.add_service(service)
    driver.add_accessory(acc)
    return acc, service, chars


async def _run_scheduled_jobs(rounds: int = 8) -> None:
    """Let jobs scheduled by the driver and their tasks run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _write_chars(
    driver: AccessoryDriver,
    acc: Accessory,
    char_values: list[tuple[Characteristic, Any]],
    client: Any = WRITER,
    timeout: float = ASYNC_TIMEOUT,
    client_uuid: UUID | None = None,
    connection: HAPConnectionHandle | None = None,
    write_response: bool = False,
    ev: bool | None = None,
) -> dict[str, Any] | None:
    """Write ``char_values`` like a controller and wait for the dispatch."""
    queries = []
    for char, value in char_values:
        entry: dict[str, Any] = {
            HAP_REPR_AID: acc.aid,
            HAP_REPR_IID: acc.iid_manager.get_iid(char),
        }
        if value is not NO_VALUE:
            entry[HAP_REPR_VALUE] = value
        if write_response:
            entry[HAP_REPR_WRITE_RESPONSE] = True
        if ev is not None:
            entry[HAP_PERMISSION_NOTIFY] = ev
        queries.append(entry)
    result = driver._set_characteristics_for_controller(
        {HAP_REPR_CHARS: queries},
        client,
        timeout,
        client_uuid=client_uuid,
        connection=connection,
    )
    if inspect.isawaitable(result):
        result = await result
    return result


def _status_of(
    result: dict[str, Any] | None, acc: Accessory, char: Characteristic
) -> int:
    """The status one characteristic was answered with."""
    assert result is not None
    aid = acc.aid
    assert aid is not None
    iid = acc.iid_manager.get_iid(char)
    for entry in result[HAP_REPR_CHARS]:
        if entry[HAP_REPR_AID] == aid and entry[HAP_REPR_IID] == iid:
            status: int = entry[HAP_REPR_STATUS]
            return status
    raise AssertionError(f"No result for {char}")


class _CharDelegate(CharacteristicDelegate):
    """A characteristic delegate that records its calls."""

    def __init__(
        self, answer: Any = None, release: asyncio.Event | None = None
    ) -> None:
        self.calls: list[tuple[Any, CharWriteContext]] = []
        self.answer = answer
        self.release = release

    async def on_set(self, value: Any, context: CharWriteContext) -> Any:
        self.calls.append((value, context))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.release is not None:
            await self.release.wait()
        return self.answer


class _SubDelegate(CharacteristicDelegate):
    """A delegate that records its (un)subscription calls."""

    def __init__(self, answer: Any = None) -> None:
        self.subscribes: list[CharReadContext] = []
        self.unsubscribes: list[CharReadContext] = []
        self.answer = answer

    async def on_subscribe(self, context: CharReadContext) -> None:
        self.subscribes.append(context)
        if isinstance(self.answer, Exception):
            raise self.answer

    async def on_unsubscribe(self, context: CharReadContext) -> None:
        self.unsubscribes.append(context)
        if isinstance(self.answer, Exception):
            raise self.answer


class _GateDelegate(ServiceDelegate):
    """A service gate that records its calls."""

    def __init__(
        self, answer: Any = None, release: asyncio.Event | None = None
    ) -> None:
        self.calls: list[tuple[Mapping[Characteristic, Any], ServiceWriteContext]] = []
        self.answer = answer
        self.release = release

    async def on_set(
        self, values: Mapping[Characteristic, Any], context: ServiceWriteContext
    ) -> None:
        self.calls.append((values, context))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.release is not None:
            await self.release.wait()


class _OrderedChar(_CharDelegate):
    """A char delegate that notes when it ran."""

    def __init__(self, order: list[str], name: str) -> None:
        super().__init__()
        self.order = order
        self.name = name

    async def on_set(self, value: Any, context: CharWriteContext) -> Any:
        self.order.append(self.name)
        return await super().on_set(value, context)


async def test_on_set_receives_value_and_context(driver: AccessoryDriver) -> None:
    """A controller's write runs the char's on_set with a full context."""
    acc, _, (char,) = _add_service_with_chars(driver)
    handle = HAPConnectionHandle(WRITER, 1)
    delegate = _CharDelegate()
    char.delegate = delegate

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 7)], connection=handle)

    ((value, context),) = delegate.calls
    assert value == 7
    assert context.characteristic is char
    assert context.client_addr == WRITER
    assert context.client_uuid is None
    assert context.connection is handle
    assert context.is_admin is False
    assert context.deadline > 0
    assert char.value == 7
    notify.assert_called_once_with(WRITER)
    # Every write succeeded without a value to answer: no response body.
    assert result is None


async def test_on_set_write_response(driver: AccessoryDriver) -> None:
    """An on_set's return value is the write response of a wr char."""
    acc, _, (char,) = _add_service_with_chars(
        driver,
        permissions=[
            HAP_PERMISSION_READ,
            HAP_PERMISSION_WRITE,
            HAP_PERMISSION_WRITE_RESPONSE,
        ],
    )
    char.delegate = _CharDelegate(answer=7)

    result = await _write_chars(driver, acc, [(char, 5)], write_response=True)
    assert result is not None
    (entry,) = result[HAP_REPR_CHARS]
    assert entry[HAP_REPR_VALUE] == 7


async def test_on_set_read_back_answer(driver: AccessoryDriver) -> None:
    """What on_set answers to a read back char is read back by the writer."""
    acc, _, (char,) = _add_service_with_chars(
        driver,
        permissions=[HAP_PERMISSION_READ, HAP_PERMISSION_WRITE, HAP_PERMISSION_NOTIFY],
    )
    char.read_back_response = True
    char.delegate = _CharDelegate(answer=7)
    char_id = f"{STANDALONE_AID}.{acc.iid_manager.get_iid(char)}"

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 5)])

    assert result is None
    (writer_rep,) = driver.get_characteristics([char_id], WRITER)[HAP_REPR_CHARS]
    (other_rep,) = driver.get_characteristics([char_id], OTHER_CONTROLLER)[
        HAP_REPR_CHARS
    ]
    assert writer_rep[HAP_REPR_VALUE] == 7
    assert other_rep[HAP_REPR_VALUE] == 0
    assert char.value == 0
    # The read back answer replaced the value; nothing to tell subscribers.
    notify.assert_not_called()


async def test_on_set_hap_status_refused(driver: AccessoryDriver) -> None:
    """A refused write answers its status, restores and notifies nobody."""
    acc, _, (char,) = _add_service_with_chars(driver)
    char.delegate = _CharDelegate(
        answer=HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
    )

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 7)])

    assert _status_of(result, acc, char) == HAP_SERVER_STATUS.RESOURCE_BUSY
    assert char.value == 0
    notify.assert_not_called()


async def test_on_set_failure_answers_70402(driver: AccessoryDriver) -> None:
    """Any other exception of on_set answers -70402, restores, notifies nobody."""
    acc, _, (char,) = _add_service_with_chars(driver)
    char.delegate = _CharDelegate(answer=RuntimeError("no"))

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 7)])

    assert (
        _status_of(result, acc, char) == HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    )
    assert char.value == 0
    notify.assert_not_called()


async def test_on_set_timeout_answers_70408(driver: AccessoryDriver) -> None:
    """An on_set that misses the deadline answers -70408 and restores."""
    acc, _, (char,) = _add_service_with_chars(driver)
    char.delegate = _CharDelegate(release=asyncio.Event())

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 7)], timeout=0.01)

    assert _status_of(result, acc, char) == HAP_SERVER_STATUS.OPERATION_TIMED_OUT
    assert char.value == 0
    notify.assert_not_called()


async def test_invalid_value_answers_70402_without_on_set(
    driver: AccessoryDriver,
) -> None:
    """A value that does not fit the char answers -70402 and runs no on_set."""
    acc, _, (char,) = _add_service_with_chars(driver)
    delegate = _CharDelegate()
    char.delegate = delegate

    result = await _write_chars(driver, acc, [(char, "not an int")])

    assert (
        _status_of(result, acc, char) == HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    )
    assert delegate.calls == []
    assert char.value == 0


async def test_null_write_runs_no_on_set(driver: AccessoryDriver) -> None:
    """A null write applies nothing and runs no on_set, like the legacy path."""
    acc, _, (char,) = _add_service_with_chars(driver)
    delegate = _CharDelegate()
    char.delegate = delegate

    result = await _write_chars(driver, acc, [(char, None)])

    assert result is None
    assert delegate.calls == []
    assert char.value == 0


async def test_gate_veto_restores_and_skips_on_set(driver: AccessoryDriver) -> None:
    """A gate's refusal restores every write of its service, notifies nobody."""
    acc, service, (char_a, char_b) = _add_service_with_chars(driver, count=2)
    gate = _GateDelegate(answer=HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY))
    service.delegate = gate
    delegates = [_CharDelegate(), _CharDelegate()]
    char_a.delegate = delegates[0]
    char_b.delegate = delegates[1]

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char_a, 1), (char_b, 2)])

    ((values, context),) = gate.calls
    assert values == {char_a: 1, char_b: 2}
    assert context.service is service
    assert context.is_admin is False
    assert context.client_addr == WRITER
    assert _status_of(result, acc, char_a) == HAP_SERVER_STATUS.RESOURCE_BUSY
    assert _status_of(result, acc, char_b) == HAP_SERVER_STATUS.RESOURCE_BUSY
    assert char_a.value == 0
    assert char_b.value == 0
    assert delegates[0].calls == []
    assert delegates[1].calls == []
    notify.assert_not_called()


async def test_gate_timeout_answers_70408(driver: AccessoryDriver) -> None:
    """A gate that misses the deadline refuses its writes with -70408."""
    acc, service, (char,) = _add_service_with_chars(driver)
    service.delegate = _GateDelegate(release=asyncio.Event())
    delegate = _CharDelegate()
    char.delegate = delegate

    result = await _write_chars(driver, acc, [(char, 7)], timeout=0.01)

    assert _status_of(result, acc, char) == HAP_SERVER_STATUS.OPERATION_TIMED_OUT
    assert delegate.calls == []
    assert char.value == 0


async def test_gate_failure_answers_70402(driver: AccessoryDriver) -> None:
    """Any other gate exception refuses its writes with -70402."""
    acc, service, (char,) = _add_service_with_chars(driver)
    service.delegate = _GateDelegate(answer=RuntimeError("no"))
    delegate = _CharDelegate()
    char.delegate = delegate

    result = await _write_chars(driver, acc, [(char, 7)])

    assert (
        _status_of(result, acc, char) == HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    )
    assert delegate.calls == []
    assert char.value == 0


async def test_gate_passes_then_writes_apply(driver: AccessoryDriver) -> None:
    """An accepting gate lets the writes of its service apply and notify."""
    acc, service, (char,) = _add_service_with_chars(driver)
    order: list[str] = []

    class _OrderedGate(_GateDelegate):
        async def on_set(
            self, values: Mapping[Characteristic, Any], context: ServiceWriteContext
        ) -> None:
            order.append("gate")
            await super().on_set(values, context)

    service.delegate = _OrderedGate()
    char.delegate = _OrderedChar(order, "on_set")

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(char, 7)])

    assert result is None
    assert order == ["gate", "on_set"]
    assert char.value == 7
    notify.assert_called_once_with(WRITER)


async def test_gate_covers_legacy_chars_of_its_service(driver: AccessoryDriver) -> None:
    """A gated service's legacy callback chars are applied after the gate."""
    acc, service, (delegate_char, legacy_char) = _add_service_with_chars(
        driver, count=2
    )
    order: list[str] = []

    class _OrderedGate(_GateDelegate):
        async def on_set(
            self, values: Mapping[Characteristic, Any], context: ServiceWriteContext
        ) -> None:
            order.append("gate")
            await super().on_set(values, context)

    setter = MagicMock(side_effect=lambda value: order.append("setter"))
    legacy_char.setter_callback = setter
    gate = _OrderedGate()
    service.delegate = gate
    delegate_char.delegate = _OrderedChar(order, "on_set")

    result = await _write_chars(driver, acc, [(delegate_char, 1), (legacy_char, 2)])

    assert result is None
    ((values, _),) = gate.calls
    assert values == {delegate_char: 1, legacy_char: 2}
    assert order[0] == "gate"
    assert "gate" not in order[1:]
    setter.assert_called_once_with(2)
    assert delegate_char.value == 1
    assert legacy_char.value == 2


async def test_gate_veto_skips_legacy_setter(driver: AccessoryDriver) -> None:
    """A gate's refusal never applies the legacy chars of its service."""
    acc, service, (_, legacy_char) = _add_service_with_chars(driver, count=2)
    setter = MagicMock()
    legacy_char.setter_callback = setter
    service.delegate = _GateDelegate(
        answer=HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
    )

    result = await _write_chars(driver, acc, [(legacy_char, 7)])

    assert _status_of(result, acc, legacy_char) == HAP_SERVER_STATUS.RESOURCE_BUSY
    setter.assert_not_called()
    assert legacy_char.value == 0


async def test_gate_veto_only_its_service(driver: AccessoryDriver) -> None:
    """A gate's refusal does not touch the writes of other services."""
    acc, gated_service, (gated_char,) = _add_service_with_chars(driver)
    plain_service = Service(uuid1(), "Plain Service")
    plain_char = Characteristic("PlainValue", uuid1(), CHAR_PROPS)
    plain_service.add_characteristic(plain_char)
    acc.add_service(plain_service)
    setter = MagicMock()
    plain_char.setter_callback = setter
    gated_service.delegate = _GateDelegate(
        answer=HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
    )

    with patch.object(Characteristic, "notify") as notify:
        result = await _write_chars(driver, acc, [(gated_char, 1), (plain_char, 2)])

    assert _status_of(result, acc, gated_char) == HAP_SERVER_STATUS.RESOURCE_BUSY
    assert _status_of(result, acc, plain_char) == HAP_SERVER_STATUS.SUCCESS
    setter.assert_called_once_with(2)
    assert plain_char.value == 2
    assert gated_char.value == 0
    notify.assert_called_once_with(WRITER)


async def test_accessory_setter_sees_delegate_writes(driver: AccessoryDriver) -> None:
    """The legacy accessory setter still learns every written value."""
    acc, service, (char,) = _add_service_with_chars(driver)
    acc.setter_callback = MagicMock()
    char.delegate = _CharDelegate()

    await _write_chars(driver, acc, [(char, 7)])

    acc.setter_callback.assert_called_once_with({service: {char: 7}})


async def test_on_set_is_admin(driver: AccessoryDriver) -> None:
    """The context says whether the writing controller is an admin."""
    acc, _, (char,) = _add_service_with_chars(driver)
    delegate = _CharDelegate()
    char.delegate = delegate
    admin_uuid = uuid4()
    plain_uuid = uuid4()
    driver.state.add_paired_client(str(admin_uuid).encode(), b"pub", b"\x01")
    driver.state.add_paired_client(str(plain_uuid).encode(), b"pub", b"\x00")

    await _write_chars(driver, acc, [(char, 7)], client_uuid=admin_uuid)
    await _write_chars(driver, acc, [(char, 8)], client_uuid=plain_uuid)

    assert delegate.calls[0][1].is_admin is True
    assert delegate.calls[0][1].client_uuid == admin_uuid
    assert delegate.calls[1][1].is_admin is False


async def test_write_response_dropped_when_closed_before_on_set_done(
    driver: AccessoryDriver,
) -> None:
    """An on_set answering after its connection closed gets no response kept."""
    acc, _, (char,) = _add_service_with_chars(
        driver,
        permissions=[
            HAP_PERMISSION_READ,
            HAP_PERMISSION_WRITE,
            HAP_PERMISSION_WRITE_RESPONSE,
        ],
    )
    release = asyncio.Event()
    char.delegate = _CharDelegate(answer=7, release=release)

    write = asyncio.ensure_future(
        _write_chars(driver, acc, [(char, 5)], write_response=True)
    )
    await asyncio.sleep(0)
    driver.connection_lost(WRITER)
    await asyncio.sleep(0)
    release.set()

    assert await write is None
    assert driver._write_responses == {}


async def test_on_subscribe_receives_a_full_context(driver: AccessoryDriver) -> None:
    """A controller's subscription runs on_subscribe with a full context."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    handle = HAPConnectionHandle(WRITER, 1)
    client_uuid = uuid4()
    delegate = _SubDelegate()
    char.delegate = delegate
    char_topic = f"{acc.aid}.{acc.iid_manager.get_iid(char)}"

    result = await _write_chars(
        driver,
        acc,
        [(char, NO_VALUE)],
        ev=True,
        client_uuid=client_uuid,
        connection=handle,
    )
    await _run_scheduled_jobs()

    assert result is None
    (context,) = delegate.subscribes
    assert context.characteristic is char
    assert context.client_addr == WRITER
    assert context.client_uuid == client_uuid
    assert context.connection is handle
    assert context.deadline == float("inf")
    assert driver.topics == {char_topic: {WRITER}}


async def test_on_unsubscribe_receives_the_context(driver: AccessoryDriver) -> None:
    """A controller's unsubscription runs on_unsubscribe."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    delegate = _SubDelegate()
    char.delegate = delegate

    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=True)
    await _run_scheduled_jobs()
    result = await _write_chars(driver, acc, [(char, NO_VALUE)], ev=False)
    await _run_scheduled_jobs()

    assert result is None
    (context,) = delegate.unsubscribes
    assert context.characteristic is char
    assert context.client_addr == WRITER
    assert driver.topics == {}


async def test_subscription_hooks_run_only_on_the_change(
    driver: AccessoryDriver,
) -> None:
    """Repeated (un)subscriptions of the same controller run no hook."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    delegate = _SubDelegate()
    char.delegate = delegate

    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=True)
    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=True)
    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=False)
    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=False)
    await _run_scheduled_jobs()

    assert len(delegate.subscribes) == 1
    assert len(delegate.unsubscribes) == 1


async def test_on_subscribe_failure_is_isolated(
    driver: AccessoryDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing on_subscribe is logged and the subscription registers."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    delegate = _SubDelegate(answer=RuntimeError("no"))
    char.delegate = delegate
    char_topic = f"{acc.aid}.{acc.iid_manager.get_iid(char)}"

    result = await _write_chars(driver, acc, [(char, NO_VALUE)], ev=True)
    await _run_scheduled_jobs()

    assert result is None
    assert driver.topics == {char_topic: {WRITER}}
    assert "failed to run on_subscribe" in caplog.text


async def test_close_runs_on_unsubscribe(driver: AccessoryDriver) -> None:
    """A connection that closes while subscribed runs on_unsubscribe."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    handle = HAPConnectionHandle(WRITER, 1)
    client_uuid = uuid4()
    delegate = _SubDelegate()
    char.delegate = delegate

    await _write_chars(
        driver,
        acc,
        [(char, NO_VALUE)],
        ev=True,
        client_uuid=client_uuid,
        connection=handle,
    )
    await _run_scheduled_jobs()
    driver.connection_lost(WRITER, connection=handle, client_uuid=client_uuid)
    await _run_scheduled_jobs()

    (context,) = delegate.unsubscribes
    assert context.characteristic is char
    assert context.client_addr == WRITER
    assert context.client_uuid == client_uuid
    assert context.connection is handle
    assert driver.topics == {}


async def test_close_without_connection_info_runs_on_unsubscribe(
    driver: AccessoryDriver,
) -> None:
    """A close without its connection's identity still runs on_unsubscribe."""
    acc, _, (char,) = _add_service_with_chars(driver)
    driver.loop = asyncio.get_running_loop()
    delegate = _SubDelegate()
    char.delegate = delegate

    await _write_chars(driver, acc, [(char, NO_VALUE)], ev=True)
    await _run_scheduled_jobs()
    driver.connection_lost(WRITER)
    await _run_scheduled_jobs()

    (context,) = delegate.unsubscribes
    assert context.client_uuid is None
    # Without the closed connection's handle the hook still learns the
    # address, on a handle of its own.
    assert context.connection.client_addr == WRITER
    assert driver.topics == {}


async def test_direct_set_characteristics_schedules_delegates(
    driver: AccessoryDriver,
) -> None:
    """A direct call stores the values and runs the dispatch as a job."""
    driver.loop = asyncio.get_running_loop()
    acc, _, (char,) = _add_service_with_chars(driver)
    delegate = _CharDelegate()
    char.delegate = delegate
    notified = asyncio.Event()

    def _record(*_: Any) -> None:
        notified.set()

    query = {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: acc.iid_manager.get_iid(char),
                HAP_REPR_VALUE: 7,
            }
        ]
    }

    with patch.object(Characteristic, "notify", side_effect=_record) as notify:
        result = driver.set_characteristics(query, WRITER)

        # The optimistic answer of a direct call, like a coroutine setter's.
        assert result is None
        assert char.value == 7
        assert delegate.calls == []

        await asyncio.wait_for(notified.wait(), ASYNC_TIMEOUT)

        ((value, context),) = delegate.calls
        assert value == 7
        assert context.client_addr == WRITER
        assert context.connection.client_addr is None
        assert context.deadline == float("inf")
        notify.assert_called_once_with(WRITER)
