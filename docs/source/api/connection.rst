.. _api-connection:

==========
Connection
==========

The identity of a controller connection.

.. currentmodule:: pyhap.connection

.. autoclass:: HAPConnectionHandle
   :members:

Why not just the client address
===============================

The ``(address, port)`` of a connection is unique only while it lives - a
later connection may reuse the address of a closed one. A handle compares
equal only for the same connection, so anything a callback keeps per
connection, a write response, a camera session, cannot leak to a
connection that reused the address. See the class docstring for the
phased migration of the driver's per-client state to the handle.
