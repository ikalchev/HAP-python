"""The delegate interfaces of the callback interface.

A delegate is an object a characteristic or service is wired up with,
:class:`CharacteristicDelegate` for one characteristic or several shared
ones, :class:`ServiceDelegate` for a whole service. Unlike the legacy
``setter_callback``/``getter_callback`` attributes it replaces over time,
a delegate receives a context (:class:`~pyhap.context.CharContext`) that carries what a
callback needs to know about the request - and grows new fields instead of
new callback signatures.

Registration, on e.g. ``char.delegate = ...``, is when everything about a
delegate is decided once: the driver records which methods are overridden
and rejects an overridden method that is not a coroutine function. The
read and write paths never introspect a delegate again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .context import CharReadContext, CharWriteContext

if TYPE_CHECKING:
    from .characteristic import Characteristic


class CharacteristicDelegate:
    """Delegate of a characteristic's reads, writes and subscriptions.

    Subclass and override what you need; the noop defaults do nothing.
    One instance may serve many characteristics: the context carries
    ``characteristic`` and identifies which one a call is about.

    Every method is a coroutine function BY DESIGN. The driver awaits
    every call - there is no sync/async detection on this path - and
    enforces the context's deadline: a call that is not done by it is
    cancelled and answers ``-70408``. Heavy synchronous work still
    blocks the loop and the request; keep such work in a task or an
    executor and await it.

    An unoverridden method means the characteristic has no hook for it;
    an overridden one that returns ``None`` is an answer - e.g. a getter
    that answers the null value of a Programmable Switch Event. A
    callback that fails answers with its status instead of the generic
    ``-70402`` when it raises :class:`~pyhap.errors.HapStatusError`.
    """

    async def on_get(self, context: CharReadContext) -> Any:
        """Answer a read of the characteristic with its value.

        An unoverridden ``on_get`` reads the characteristic's stored
        value. An override runs under the context's deadline and answers
        the read; return the value, which must fit the characteristic's
        format like any other value.
        """

    async def on_set(self, value: Any, context: CharWriteContext) -> Any:
        """Apply a controller's write of ``value``.

        Runs after the write passed its gate hooks, under the context's
        deadline. The value is already stored and subscribers are told
        once this call is done. A return value is the write response for
        a characteristic with the ``wr`` permission, else it is dropped;
        raise :class:`~pyhap.errors.HapStatusError` to answer a status.
        """

    async def on_subscribe(self, context: CharReadContext) -> None:
        """A controller subscribed to the characteristic's events.

        Use this to start work only while someone listens, e.g. polling
        a sensor that costs battery. Failures are logged and isolated;
        the subscription itself always registers.
        """

    async def on_unsubscribe(self, context: CharReadContext) -> None:
        """A controller unsubscribed from the characteristic's events.

        This also runs when its connection closes while subscribed.
        Failures are logged and isolated.
        """


class ServiceDelegate:
    """Delegate of a whole service.

    The service's ``on_set`` is the gate of its grouped writes: a request
    that writes several characteristics of the service runs it before
    any per-characteristic ``on_set``, with every value of the request -
    so a thermostat sees target mode and target temperature together.
    Raise :class:`~pyhap.errors.HapStatusError` to refuse the whole
    request before anything is applied: values are restored, subscribers
    are told nothing, and per-characteristic ``on_set`` calls do not run.
    """

    async def on_set(
        self, values: Mapping[Characteristic, Any], context: CharWriteContext
    ) -> None:
        """Veto or accept a grouped write before it is applied.

        :param values: Every written value of the request, by
            :class:`~pyhap.characteristic.Characteristic`.
        """
