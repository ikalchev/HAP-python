.. _api-client:

======
Client
======

`pyhap` also ships a HAP controller (client) under `pyhap.client.HAPClient`.
The client uses the HAP protocol to control an accessory similarly to how you
can control it through the Home app on iOS. Event subscribtion is also supported.

The client can be persistent, i.e. after it is started it can persist its
identity (what the accesory remembers and later verifies) into a file for
subsequent reuse.

The main limitation of the client currently is that it does not support pairing up
from scratch (see below).

The client is used in the integration tests and in the benchmark suite, in case
you are looking for examples.

Pairing and trust assumptions
=============================

The client assumes a pyhap accessory has already completed `pair-setup`. That is,
the client does not support pairing from scratch right now - this is mainly because of
a missing part of the server-side crypto protocol.

Two things must therefore already exist when you connect:

1. **The client identity must be paired with the accessory.** Pairings are
   per controller identity: an existing iPhone pairing does not cover the
   client. When you run the accessory yourself (the usual case for this
   library), pair the identity before connecting::

       client = HAPClient.new_persistent(filepath="controller.key")
       # Safe to do multiple times, it's idempotent.
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
   pair-verify. This is not available from pair-verify itself (a real
   controller learns it from pair-setup's M6 response). Pass it once — as
   raw bytes (``accessory_ltpk=...``) or read from the accessory's state
   file (``accessory_state="accessory.state"``) — and it is pinned in the
   client file and verified on every later run. If the client file already
   pins a *different* LTPK, ``new_persistent`` raises ``ValueError``:
   migrating the client to another accessory is done by updating (or
   deleting) the client file yourself, not via the method. Without any LTPK
   the session is still encrypted, but the accessory is not authenticated.

Running the client on a different machine than the accessory
-------------------------------------------------------------

1. On the client machine, create the persistent identity once and copy
   ``controller.key`` to the accessory machine (it holds the client's
   private key — only copy it to machines you trust)::

       client = HAPClient.new_persistent(filepath="controller.key")

2. On the accessory machine, with the accessory **stopped** (a running
   driver keeps its state in memory and would overwrite the edit on its
   next persist), register the identity in the state file offline — no
   driver needed::

       from pyhap.client import ClientIdentity, add_client_pairing

       add_client_pairing("accessory.state", ClientIdentity.load("controller.key"))

   Then start the accessory again; it loads the updated ``accessory.state``.
   While the driver is running, ``driver.pair(...)`` (above) does the same
   in-process; an already-paired admin can also add pairings remotely via
   the ``/pairings`` endpoint, which this client does not implement.
3. On the client machine, move only the accessory's *public* key back — do
   not copy the whole ``accessory.state``, it contains the accessory's
   private key (read just the public key out with
   :func:`pyhap.client.load_accessory_ltpk`, or copy the file and pass it
   once). The key is then pinned in the client file, so only
   ``controller.key`` is needed afterwards. The state file does not store
   the port, so pass host:port explicitly::

       client = HAPClient.new_persistent(
           filepath="controller.key",
           accessory_state="accessory.state",  # copied from the accessory machine
       )
       await client.connect("192.168.1.50", 51827)

Example
=======

Host an accessory and drive it with a client in the same process.
``advertise=False`` runs the HAP server *without* the mDNS advertisement
(a real deployment advertises, so the Home app can discover it; tests and
local setups can skip it):

.. code-block:: python

    from pyhap.accessory_driver import AccessoryDriver
    from pyhap.client import HAPClient
    from pyhap.const import HAP_PERMISSIONS

    driver = AccessoryDriver(port=51827, advertise=False)  # HAP server only, no mDNS
    driver.add_accessory(MyAccessory(driver, "My Accessory"))  # your Accessory subclass

    # Loads controller.key if it exists, otherwise creates a new identity
    # and saves it there. Pair the identity once (bypasses the slow SRP
    # pair-setup); the pairing is stored in the accessory's own state file.
    client = HAPClient.new_persistent(filepath="controller.key")
    driver.pair(
        client.identity.uuid_bytes,
        client.identity.public_key_bytes,
        HAP_PERMISSIONS.ADMIN,
    )

    await driver.async_start()                    # start the HAP server
    await client.connect("127.0.0.1", 51827)      # pair-verify + encryption
    await client.get_accessories()                # GET /accessories
    await client.subscribe(1, on_iid)             # EVENT notifications on
    await client.write_characteristic(1, on_iid, True)
    await client.wait_event(1, on_iid, True)      # an update arrives as EVENT
    await client.close()
    await driver.async_stop()

Against an already-running accessory (e.g. on another machine) there is no
driver to instantiate: pass the accessory's state file once to pin its LTPK
(see the section above), and connect to its host:port explicitly — the state
file stores the keys and pairings but not the port::

    client = HAPClient.new_persistent(
        filepath="controller.key",
        accessory_state="accessory.state",  # copied from the accessory machine
    )
    await client.connect("192.168.1.50", 51827)

.. automodule:: pyhap.client
   :members:
