.. _api-context:

========
Contexts
========

The contexts passed to delegate and lifecycle calls. New input data
arrives as new fields of these classes; a callback's signature never
changes.

Why objects, not ids
====================

The contexts identify their target by the object itself, never by an
iid. Iids are the wire protocol's identity - assigned by the driver at
publish time and meaningless outside a running driver - and a delegate
is wired at construction time, before iids even exist. The object
serves as the identity for dispatch (``is`` checks, the keys of a
grouped ``on_set``'s ``values``) and carries what a callback needs:
the display name, the stored value, the properties, the owning
service. Translating to and from the wire format is the driver's job,
not a delegate's.

.. currentmodule:: pyhap.context

.. autoclass:: Context
   :members:

.. autoclass:: CharReadContext
   :members:

.. autoclass:: CharWriteContext
   :members:

.. autoclass:: ServiceWriteContext
   :members:
