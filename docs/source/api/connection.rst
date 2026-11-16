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
connection that reused the address.

The driver keys its per-connection state by the handle too: event
subscriptions, prepared writes, write responses and the server's
connections. Nothing of a closed connection reaches a later one, and an
answer that arrives only after its connection closed is dropped instead
of kept. A direct call without a connection (``driver.set_characteristics``
and friends) acts as a virtual connection of its client address, so its
writes, prepared timed writes and read-backs pair up across calls.
