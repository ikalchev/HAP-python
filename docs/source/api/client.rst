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

Pairing and trust assumptions
=============================

The client speaks **pair-verify only**. It never runs pair-setup — the SRP
handshake with the pincode that the Home app performs once when scanning the
QR code. Two things must therefore already exist when you connect:

1. **The client identity must be paired with the accessory.** Pairings are
   per controller identity: an existing iPhone pairing does not cover the
   client. When you run the accessory yourself (the usual case for this
   library), pair the identity in-process once, before connecting::

       client = HAPClient.new_persistent(filepath="controller.key")
       driver.pair(
           client.identity.uuid_bytes,
           client.identity.public_key_bytes,
           HAP_PERMISSIONS.ADMIN,
       )
       await client.connect("127.0.0.1", port)

   Against a remote accessory you do not control, a pairing can only be
   added by pair-setup (not implemented in the client) or by an
   already-paired admin through the ``/pairings`` endpoint (also not
   implemented) — otherwise pair-verify is rejected.

2. **The accessory LTPK is optional, but recommended.** The LTPK is the
   accessory's long-term public key, used to verify the accessory during
   pair-verify; it cannot be learned from pair-verify itself (a real
   controller learns it from pair-setup's M6 response). Pass it once — as
   raw bytes (``accessory_ltpk=...``) or read from the accessory's state
   file (``accessory_state="accessory.state"``) — and it is pinned in the
   client file and verified on every later run. Without any LTPK the session
   is still encrypted, but the accessory is not authenticated.

Running the client on a different machine than the accessory
-------------------------------------------------------------

1. On the accessory machine, pair the client identity once (``driver.pair``
   as above, or add it in the Home app).
2. Move only the accessory's *public* key to the client machine — do not
   copy the whole ``accessory.state``, it contains the accessory's private
   key. Read the public key out with :func:`pyhap.client.load_accessory_ltpk`
   (or take the ``"public_key"`` hex field of the file), and pass the state
   file's path once; the key is then pinned in the client file, so only
   ``controller.key`` is needed afterwards::

       client = HAPClient.new_persistent(
           filepath="controller.key",
           accessory_state="/path/to/accessory.state",  # copied from the accessory machine
       )
       await client.connect("192.168.1.50", 51826)

Example
=======

Drive a running accessory as a controller, with the identity persisted so the
controller is recognized again after a restart:

.. code-block:: python

    from pyhap.client import HAPClient
    from pyhap.const import HAP_PERMISSIONS

    # Loads controller.key if it exists, otherwise creates a new identity
    # and saves it there. Pass accessory_ltpk=... or accessory_state=... once
    # to pin the accessory's long-term key for verification; without it the
    # session is still encrypted, but the accessory is not authenticated.
    client = HAPClient.new_persistent(filepath="controller.key")
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
