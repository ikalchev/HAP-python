.. _api-delegates:

=========
Delegates
=========

The delegate interfaces of the callback interface: an object a
characteristic or service is wired up with, receiving a
:class:`~pyhap.context.CharContext` per call. Registration is when the driver
records what a delegate overrides; the read and write paths never
introspect it again.

.. currentmodule:: pyhap.delegates

.. autoclass:: CharacteristicDelegate
   :members:

.. autoclass:: ServiceDelegate
   :members:

Async by design
===============

Every delegate method is a coroutine function. The driver awaits every
call and enforces the context's deadline - there is no sync/async
detection. An overridden method that is not a coroutine function fails
registration with a :class:`TypeError`, so a wrong method is caught at
its source.

Wiring a delegate
=================

Assign to the ``delegate`` of a characteristic or a service, or pass
it through :meth:`Service.configure_char
<pyhap.service.Service.configure_char>` - an instance, or a class that
is then instantiated with no arguments:

.. code-block:: python

   class MyLock(CharacteristicDelegate):
       async def on_set(self, value, context):
           ...

   char = service.configure_char("LockTargetState", delegate=MyLock)
   # is the same as
   service.get_characteristic("LockTargetState").delegate = MyLock()

The assignment is when everything about the delegate is decided: which
methods it overrides is recorded, an overridden method that is not a
coroutine function fails with a :class:`TypeError`, and the read and
write paths never introspect the delegate again. ``None`` or ``del``
removes it.

One instance may serve many characteristics: the context of a call
carries :attr:`~pyhap.context.CharReadContext.characteristic` and says
which one a call is about, e.g. one delegate per accessory rather than
one per characteristic.

**Rollout state.** The driver stores and validates delegates from this
release on; the write and read pipelines dispatch them in the following
releases of the callback interface refactor. Until then, a delegate is
wired but not yet called, and the legacy
:attr:`~pyhap.characteristic.Characteristic.setter_callback` /
:attr:`~pyhap.characteristic.Characteristic.getter_callback` attributes
keep working unchanged - assigning a delegate does not remove them.
