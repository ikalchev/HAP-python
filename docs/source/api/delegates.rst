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
