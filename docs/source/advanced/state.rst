.. _advanced-state:

================
State management
================

HAP-python keeps two kinds of state: the *pairing identity* of the accessory
itself, and the *saved settings* your accessories choose to keep. Both live in
one file — ``accessory.state`` by default — owned by the
:class:`~pyhap.accessory_driver.AccessoryDriver` through the
:class:`~pyhap.state.State` object.

.. contents:: On this page
   :local:
   :depth: 1

What is persisted, and why
==========================

A controller recognises an accessory by its long-term key pair and device
MAC, not by its IP address or port. The state file is what makes an accessory
*be the same device* across restarts. It holds:

* the MAC address (the accessory's HomeKit device ID, advertised over mDNS)
  and the setup ID used in the QR code;
* the Ed25519 long-term key pair — the accessory's pairing identity;
* the paired controllers, with their public keys and permissions
  (admin or restricted);
* the config version (``c#`` in the mDNS advertisement) and the accessories
  hash;
* the per-accessory saved state (see below).

Losing the state file means losing the identity: controllers can no longer
pair-verify with the accessory and it must be paired again. The file contains
the accessory's *private* key — keep it private, back it up carefully.

Where and how
==============

The file's path is set with the ``persist_file`` argument of the
:class:`~pyhap.accessory_driver.AccessoryDriver` (``~`` is expanded); the
default is ``accessory.state`` in the working directory. The format is plain
JSON, written by the :class:`~pyhap.encoder.AccessoryEncoder`. Two details
matter operationally:

* Saves are *atomic*: the state is written to a temporary file first, which
  then replaces the old one. A crash mid-save cannot corrupt the pairings.
* Saves run in a thread pool executor, off the event loop, and are scheduled
  with :meth:`AccessoryDriver.async_persist
  <pyhap.accessory_driver.AccessoryDriver.async_persist>` — call it whenever
  you changed something that should survive a restart; it is cheap to call
  often.

When it is loaded
=================

The driver loads the state file when the accessory is added to it
(:meth:`AccessoryDriver.add_accessory
<pyhap.accessory_driver.AccessoryDriver.add_accessory>`). If the file exists,
its contents are loaded into :class:`~pyhap.state.State` and — because your
accessory objects were built *before* the load — the driver then calls
:meth:`Accessory.restore_accessory_state
<pyhap.accessory.Accessory.restore_accessory_state>` on the accessory, so it
can apply its saved settings. If the file does not exist, it is created and
the fresh state is persisted.

The file is re-written whenever the pairings or the configuration change:
pairing and unpairing, config changes, and your own
:meth:`~pyhap.accessory_driver.AccessoryDriver.async_persist` calls.

Per-accessory saved state
==========================

Accessories that have settings of their own — anything a controller or your
code configured that must survive a restart — keep them in the state file
under their AID:

.. code-block:: python

    class TimerSwitch(Accessory):

        def __init__(self, driver, name):
            super().__init__(driver, name)
            service = self.add_preload_service("Switch")
            self.timer = service.configure_char(
                "Timeout", setter_callback=self.set_timeout
            )

        def set_timeout(self, value):
            # a setting that must survive a restart
            state = self.driver.accessory_state(self.aid)
            state["timeout"] = value
            self.driver.async_persist()

        def restore_accessory_state(self):
            # applied after the driver loads the state file
            state = self.driver.accessory_state(self.aid)
            if "timeout" in state:
                self.timer.set_value(state["timeout"], should_notify=False)

Accessories built into a :class:`~pyhap.accessory.Bridge` each get their own
entry, keyed by their AID; the bridge applies the saved state of each of
them in turn.

.. note::
   Keep the values simple (JSON-compatible) and tolerate missing keys on
   restore — nothing is validated or versioned for you.

.. _advanced-state-camera:

What a camera keeps
===================

A :class:`~pyhap.camera.Camera` stores its recording settings under its AID:
whether recording and its audio are on, the selected recording configuration
and the camera operating mode (snapshots, HomeKit camera active). Hubs do not
select a configuration again after a restart, so the camera restores the
selection, unless its supported recording configuration changed. The 17.99
values a controller writes, like the global operating mode and the motion
zones, are kept as well. A new home resets all of it.

A new home must not inherit the old one
=======================================

When the last pairing is removed, the controller's home is gone: a new owner
will pair the accessory into their home. The driver then clears the saved
per-accessory state and calls :meth:`Accessory.reset_accessory_state
<pyhap.accessory.Accessory.reset_accessory_state>` on your accessory, so it
can forget anything the previous home configured. Anything your accessory
keeps that a *user* set — schedules, zones, toggles — should be reset there.

Configuration changes
======================

Adding or removing services or characteristics at runtime changes what
controllers should fetch. Tell the driver with :meth:`AccessoryDriver.config_changed
<pyhap.accessory_driver.AccessoryDriver.config_changed>`: it persists the
state, increments the config version and re-advertises over mDNS, so
controllers know to re-fetch the accessory data.

The accessories hash and config version are compared by controllers to what
they fetched before — this is also why the instance IDs (IIDs) of existing
services must stay stable, and why services should be added in the same
order on every start.

Custom encoders
===============

The file format is not fixed: the driver uses whatever
:class:`~pyhap.encoder.AccessoryEncoder` it was given, so a custom encoder can
store the state differently (encrypted, in a database, …). The encoder must
be able to persist and restore at least the identity of the accessory and
its pairings: the MAC, the key pair, the paired clients with their
permissions, the config version and the accessories hash.