"""Tests for the callback error protocol."""

import pytest

from pyhap.const import HAP_SERVER_STATUS
from pyhap.errors import CALLBACK_STATUSES, HapStatusError


def test_allowed_status() -> None:
    """Each callback status is kept as the status to answer with."""
    for status in CALLBACK_STATUSES:
        error = HapStatusError(status)
        assert error.status == status
        assert str(status) in str(error)


def test_resource_busy_is_allowed() -> None:
    """The common refusal status constructs and keeps its value."""
    error = HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
    assert error.status == HAP_SERVER_STATUS.RESOURCE_BUSY


def test_callback_statuses_are_curated() -> None:
    """The curated set is exactly the codes a callback may decide."""
    assert CALLBACK_STATUSES == frozenset(
        {
            HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            HAP_SERVER_STATUS.RESOURCE_BUSY,
            HAP_SERVER_STATUS.OUT_OF_RESOURCE,
            HAP_SERVER_STATUS.RESOURCE_DOES_NOT_EXIST,
            HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST,
            HAP_SERVER_STATUS.INSUFFICIENT_AUTHORIZATION,
        }
    )


@pytest.mark.parametrize(
    "status",
    [
        HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES,
        HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC,
        HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC,
        HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED,
        HAP_SERVER_STATUS.OPERATION_TIMED_OUT,
        HAP_SERVER_STATUS.SUCCESS,
        42,
    ],
)
def test_reserved_status_raises_value_error(status: int) -> None:
    """A driver-reserved or unknown status fails at construction."""
    with pytest.raises(ValueError, match="reserved for the accessory driver"):
        HapStatusError(status)


def test_is_an_exception() -> None:
    """The error can be raised and caught as an exception."""
    with pytest.raises(HapStatusError) as raised:
        raise HapStatusError(HAP_SERVER_STATUS.OUT_OF_RESOURCE)
    assert raised.value.status == HAP_SERVER_STATUS.OUT_OF_RESOURCE
