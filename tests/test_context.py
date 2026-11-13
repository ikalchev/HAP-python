"""Tests for the callback contexts."""

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid1, uuid4

import pytest

from pyhap.characteristic import (
    HAP_FORMAT_INT,
    HAP_PERMISSION_READ,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.connection import HAPConnectionHandle
from pyhap.const import HAP_PERMISSION_WRITE
from pyhap.context import CharContext, CharReadContext, CharWriteContext


def _char() -> Characteristic:
    return Characteristic(
        "Value",
        uuid1(),
        {
            PROP_FORMAT: HAP_FORMAT_INT,
            PROP_PERMISSIONS: [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE],
        },
    )


def _handle() -> HAPConnectionHandle:
    return HAPConnectionHandle(("1.2.3.4", 5), 3)


def test_context_fields() -> None:
    """The context carries what a callback knows about the controller."""
    client_uuid = uuid4()
    context = CharContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=client_uuid,
        connection=_handle(),
        deadline=123.5,
    )

    assert context.client_addr == ("1.2.3.4", 5)
    assert context.client_uuid == client_uuid
    assert context.connection.client_addr == ("1.2.3.4", 5)
    assert context.deadline == 123.5


def test_contexts_are_frozen() -> None:
    """A callback cannot change its context."""
    context = CharReadContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=None,
        connection=_handle(),
        deadline=1.0,
        characteristic=_char(),
    )

    with pytest.raises(FrozenInstanceError):
        context.deadline = 2.0  # ty: ignore[invalid-assignment]


def test_write_context_groups_and_admin() -> None:
    """A grouped write carries no characteristic, and the admin flag."""
    context = CharWriteContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=None,
        connection=_handle(),
        deadline=1.0,
        characteristic=None,
        is_admin=False,
    )

    assert context.characteristic is None
    assert context.is_admin is False


def test_write_context_of_one_characteristic() -> None:
    """A per-characteristic write carries its characteristic."""
    characteristic = _char()
    context = CharWriteContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=None,
        connection=_handle(),
        deadline=1.0,
        characteristic=characteristic,
        is_admin=True,
    )

    assert context.characteristic is characteristic
    assert context.is_admin is True


def test_read_context_carries_the_characteristic() -> None:
    """A read carries the characteristic it is about."""
    characteristic = _char()
    context = CharReadContext(
        client_addr=("1.2.3.4", 5),
        client_uuid=UUID(int=0),
        connection=_handle(),
        deadline=1.0,
        characteristic=characteristic,
    )

    assert context.characteristic is characteristic
    assert not hasattr(context, "is_admin")
