.. _api-client:

======
Client
======

A HAP controller (client) that speaks the real protocol against a running
accessory: pair-verify, the encrypted transport, characteristic
reads/writes, event subscriptions and ``EVENT`` push frames. The benchmark
suite and the integration tests use it to drive an
:class:`pyhap.accessory_driver.AccessoryDriver` end to end; it is also handy
for testing your own accessory.

.. automodule:: pyhap.client
   :members:
