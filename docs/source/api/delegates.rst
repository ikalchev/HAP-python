.. _api-delegates:

=========
Delegates
=========

The delegate interfaces of the callback interface: an object a
characteristic or service is wired up with, receiving a
:class:`~pyhap.context.Context` per call.

.. currentmodule:: pyhap.delegates

.. autoclass:: CharacteristicDelegate
   :members:

.. autoclass:: ServiceDelegate
   :members:

Async by design
===============

Every delegate method is a coroutine function. The driver awaits every
call and enforces the context's deadline - there is no sync/async
detection. A method that is not a coroutine function is a misuse that
fails at runtime when the driver dispatches it, not a validated error.

Wiring a delegate
=================

Assign an instance to the ``delegate`` of a characteristic or a
service, or pass it through :meth:`Service.configure_char
<pyhap.service.Service.configure_char>`:

.. code-block:: python

   class MyLock(CharacteristicDelegate):
       async def on_set(self, value, context):
           ...

   char = service.configure_char("LockTargetState", delegate=MyLock())
   # is the same as
   service.get_characteristic("LockTargetState").delegate = MyLock()

The assignment stores the delegate, that is all: using something else
than a :class:`CharacteristicDelegate`, or a method that is not a
coroutine function, fails at runtime when the driver calls it.

A runnable accessory built this way ships with the repository:
``accessories/delegate_lightbulb.py`` (listed in :doc:`/intro/examples`).

``delegate`` is never ``None``: without an assigned one it is the
default delegate — the wrapper of the legacy callbacks without any —
which answers the reads with the stored value and checks nothing on
writes. ``None`` resets to it.

A characteristic or service has either a delegate or the legacy
callbacks, never both: assigning one to an object that has the other
raises :class:`ValueError` in both directions, because there would be
no telling at dispatch which of the two should run. ``None`` clears
one side, and the other can then be assigned. Reading
``getter_callback``/``setter_callback`` of an object with an assigned
delegate raises :class:`ValueError` too - there is no legacy callback
behind it to return.

One instance may serve many characteristics: the context of a call
carries :attr:`~pyhap.context.CharReadContext.characteristic` and says
which one a call is about, e.g. one delegate per accessory rather than
one per characteristic.

**Rollout state.** Every write, read and subscription dispatches
through the delegates: the write path runs its service-level
pre-checks and its ``on_set`` calls as described above and answers with
the per-characteristic statuses; the reads run ``on_get`` under the
read deadline; ``on_subscribe`` and ``on_unsubscribe`` run when a
controller (un)subscribes, isolated from the request, and
``on_unsubscribe`` again when its connection closes while subscribed.
The legacy callbacks run wrapped inside those calls: a sync setter
inside the awaited ``on_set``, a grouped service or accessory setter as
its pre-check, a getter inside ``on_get``. pyhap's own camera stack
assigns delegates outright - its reads, writes, its stream sessions
belong to the connection that set them up, by
:class:`~pyhap.connection.HAPConnectionHandle`. Assigning a legacy
:attr:`~pyhap.characteristic.Characteristic.setter_callback` /
:attr:`~pyhap.characteristic.Characteristic.getter_callback`, or a
service's or accessory's ``setter_callback``, warns
``DeprecationWarning`` (see :doc:`/advanced/async`).

Dispatch
========

The driver never calls a delegate's methods directly: it dispatches
through the target object's own methods -
:meth:`~pyhap.characteristic.Characteristic.dispatch_get`,
:meth:`~pyhap.characteristic.Characteristic.dispatch_set` and
:meth:`~pyhap.characteristic.Characteristic.subscription_hook` on a
characteristic, :meth:`~pyhap.service.Service.dispatch_set` on a
service. Those build the call's context (see
:meth:`~pyhap.context.CharReadContext.of` and its siblings) and await
the hook. Wire a delegate to the characteristic or service; do not
override the ``dispatch_*`` methods - an override would silently take
the delegate's place.
