.. _api-errors:

=====
Errors
=====

Errors raised by accessory callbacks to answer a HAP request with a status.

.. currentmodule:: pyhap.errors

.. autoclass:: HapStatusError
   :members:

Which statuses a callback may answer with
=========================================

.. data:: CALLBACK_STATUSES

   The frozenset of HAP statuses a callback may raise
   :class:`HapStatusError` with. Everything else - privileges and
   permission failures, read-only and write-only characteristics,
   unsupported notifications and timeouts - is enforced or answered by
   the accessory driver itself and reserved.
