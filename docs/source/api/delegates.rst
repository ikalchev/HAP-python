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

The assignment stores the delegate, that is all: a delegate is a
plain attribute with a type, and using something else than a
:class:`CharacteristicDelegate` or a method that is not a coroutine
function fails at runtime when the driver dispatches it. ``None`` or
``del`` removes it.

A characteristic or service has either a delegate or the legacy
callbacks, never both: assigning one to an object that has the other
raises :class:`ValueError` in both directions, because there would be
no telling at dispatch which of the two should run. ``None`` clears
one side, and the other can then be assigned.

One instance may serve many characteristics: the context of a call
carries :attr:`~pyhap.context.CharReadContext.characteristic` and says
which one a call is about, e.g. one delegate per accessory rather than
one per characteristic.

**Rollout state.** The driver dispatches the writes of delegates from
this release on: a write to a characteristic with a delegate, or to a
service with one, runs its gates and its ``on_set`` calls as described
above, and answers with the per-characteristic statuses. The subscription
hooks dispatch too: ``on_subscribe`` and ``on_unsubscribe`` run when a
controller (un)subscribes, isolated from the request, and
``on_unsubscribe`` again when its connection closes while subscribed.
Reads dispatch as well: the ``on_get`` of a delegate-backed characteristic
answers ``GET /characteristics`` and ``GET /accessories`` under the read
deadline, like a write's callbacks. pyhap's own camera stack uses delegates
for everything its camera code answers - its reads, writes, its stream
sessions belong to the connection that set them up, by
:class:`~pyhap.connection.HAPConnectionHandle`. The legacy
:attr:`~pyhap.characteristic.Characteristic.setter_callback` /
:attr:`~pyhap.characteristic.Characteristic.getter_callback` attributes
keep working unchanged for code that uses them alone; their deprecation
is announced separately when it happens.

A request may mix both interfaces. A characteristic with a legacy
callback keeps its legacy dispatch, byte for byte, unless its service
has a delegate: the writes of a gated service - delegate or legacy
callback - all pass its gate first, so the gate can refuse them as a
group.
