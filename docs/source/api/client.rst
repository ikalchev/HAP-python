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

Example
=======

Drive a running accessory as a controller, with the identity persisted so the
controller is recognized again after a restart:

.. code-block:: python

    from pyhap.client import HAPClient
    from pyhap.const import HAP_PERMISSIONS

    # Loads controller.key if it exists, otherwise creates a new identity
    # and saves it there.
    client = HAPClient.new_persistent(
        filepath="controller.key", accessory_ltpk=accessory_ltpk_raw_bytes
    )
    # Pair the identity once (bypasses the slow SRP pair-setup); the pairing
    # is stored in the accessory's own state file.
    driver.pair(
        client.identity.uuid_bytes,
        client.identity.public_key_bytes,
        HAP_PERMISSIONS.ADMIN,
    )

    await client.connect("127.0.0.1", port)       # pair-verify + encryption
    await client.get_accessories()                # GET /accessories
    await client.subscribe(1, on_iid)             # EVENT notifications on
    await client.write_characteristic(1, on_iid, True)
    await client.wait_event(1, on_iid, True)      # an update arrives as EVENT
    await client.close()

.. automodule:: pyhap.client
   :members:
