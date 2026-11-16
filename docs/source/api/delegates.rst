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

**Rollout state.** The driver stores delegates from this release on;
the write and read pipelines dispatch them in the following releases of
the callback interface refactor. Until then, a delegate is wired but
not yet called. The legacy
:attr:`~pyhap.characteristic.Characteristic.setter_callback` /
:attr:`~pyhap.characteristic.Characteristic.getter_callback` attributes
keep working unchanged for code that uses them alone, and are
deprecated once the delegates are dispatched.
