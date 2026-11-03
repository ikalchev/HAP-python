"""Tests for authenticated connection-scoped callbacks."""

from unittest.mock import Mock
from uuid import uuid4

import pytest

from pyhap.characteristic import Characteristic
from pyhap.hap_protocol import HAPServerProtocol
from pyhap.hap_session import HAPSession


def test_session_callbacks_isolate_values_and_keep_legacy_behavior():
    char = Characteristic(
        "test", uuid4(), {"Format": "string", "Permissions": ["pr", "pw"]}
    )
    a, b = HAPSession(uuid4(), b"a" * 32), HAPSession(uuid4(), b"b" * 32)
    values = {}
    char.session_setter_callback = lambda value, session: values.setdefault(
        session, value
    )
    char.session_getter_callback = lambda session: values[session]
    assert char.client_update_value("one", session=a) == "one"
    assert char.client_update_value("two", session=b) == "two"
    assert char.get_value(a) == "one"
    assert char.get_value(b) == "two"
    assert char.get_value() != "one"
    a.close()
    with pytest.raises(RuntimeError):
        char.get_value(a)
    with pytest.raises(RuntimeError):
        char.client_update_value("bad", session=a)
    assert char.get_value(b) == "two"
    char.broker = Mock()
    char.setter_callback = lambda value: "legacy"
    assert char.client_update_value("legacy") == "legacy"


def test_close_is_idempotent_and_isolates_connections():
    a, b = HAPSession(uuid4(), b"a" * 32), HAPSession(uuid4(), b"b" * 32)
    first, second = Mock(), Mock()
    a.add_close_callback(first)
    b.add_close_callback(second)
    protocol = HAPServerProtocol(Mock(), {}, Mock())
    protocol.handler = Mock(session=a)
    protocol.transport = Mock()
    protocol.close()
    protocol.close()
    first.assert_called_once_with()
    second.assert_not_called()
    assert a.closed and a.shared_key is None
    assert not b.closed and b.shared_key == b"b" * 32
    later = Mock()
    a.add_close_callback(later)
    later.assert_called_once_with()


def test_start_without_removed_child_watcher(driver):
    """Python 3.14 can start without the removed child-watcher API."""
    from unittest.mock import patch

    with (
        patch(
            "pyhap.accessory_driver.asyncio.SafeChildWatcher", create=True
        ) as watcher,
        patch("pyhap.accessory_driver.hasattr", return_value=False),
        patch.object(driver, "async_start", new=Mock(return_value=None)),
        patch.object(driver, "add_job"),
        patch.object(driver.loop, "run_forever"),
        patch.object(driver.loop, "close"),
    ):
        driver.start()
    watcher.assert_not_called()
