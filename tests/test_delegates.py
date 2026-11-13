"""Tests for the delegate base classes."""

import asyncio
from collections.abc import Awaitable, Mapping
import inspect
from typing import Any
from uuid import uuid1

from pyhap.characteristic import (
    HAP_FORMAT_INT,
    HAP_PERMISSION_READ,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.connection import HAPConnectionHandle
from pyhap.const import HAP_PERMISSION_WRITE
from pyhap.context import CharReadContext, CharWriteContext
from pyhap.delegates import CharacteristicDelegate, ServiceDelegate


def _char() -> Characteristic:
    return Characteristic(
        "Value",
        uuid1(),
        {
            PROP_FORMAT: HAP_FORMAT_INT,
            PROP_PERMISSIONS: [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE],
        },
    )


def _read_context() -> CharReadContext:
    return CharReadContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=None,
        connection=HAPConnectionHandle(("1.2.3.4", 5), 3),
        deadline=1.0,
        characteristic=_char(),
    )


def _write_context() -> CharWriteContext:
    return CharWriteContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=None,
        connection=HAPConnectionHandle(("1.2.3.4", 5), 3),
        deadline=1.0,
        characteristic=None,
        is_admin=True,
    )


async def _run(coro: Awaitable[object]) -> object:
    return await coro


def test_noop_defaults_are_coroutine_functions() -> None:
    """The noop defaults are awaited by the driver, so they are coroutines."""
    for delegate, method in (
        (CharacteristicDelegate, "on_get"),
        (CharacteristicDelegate, "on_set"),
        (CharacteristicDelegate, "on_subscribe"),
        (CharacteristicDelegate, "on_unsubscribe"),
        (ServiceDelegate, "on_set"),
    ):
        assert inspect.iscoroutinefunction(getattr(delegate, method)), method


async def test_noop_defaults_answer_none() -> None:
    """An unoverridden delegate does nothing and answers None."""
    delegate = CharacteristicDelegate()

    assert await _run(delegate.on_get(_read_context())) is None
    assert await _run(delegate.on_set(1, _write_context())) is None
    assert await _run(delegate.on_subscribe(_read_context())) is None
    assert await _run(delegate.on_unsubscribe(_read_context())) is None
    service = ServiceDelegate()
    assert await _run(service.on_set({}, _write_context())) is None


async def test_overrides_answer_values() -> None:
    """A subclass overrides what it needs and awaits like the driver does."""
    value = object()

    class Getter(CharacteristicDelegate):
        async def on_get(self, context: CharReadContext) -> object:
            return value

    assert await _run(Getter().on_get(_read_context())) is value


async def test_an_override_may_await_inside() -> None:
    """A delegate may await inside its methods, like a real device call."""
    done = asyncio.Event()

    class Setter(ServiceDelegate):
        async def on_set(
            self, values: Mapping[Characteristic, Any], context: CharWriteContext
        ) -> None:
            await done.wait()

    done.set()
    assert await _run(Setter().on_set({}, _write_context())) is None
