.. _intro-overview:

======================
HAP-python at a glance
======================

HAP-python is an application framework to build Accessories and Bridges for
Apple's HomeKit protocol. It implements the accessory side of HAP — pairing,
the encrypted HTTP transport, change events and mDNS advertising — so that
controllers such as the Home app, a home hub or Siri can discover, pair with
and control what you build. You describe your device in HomeKit's object
model and supply the behaviour; HAP-python serves it.

HAP-python is async (asyncio based) and requires Python 3.11 or newer. One
:class:`~pyhap.accessory_driver.AccessoryDriver` hosts one
:class:`~pyhap.accessory.Accessory` — or a
:class:`~pyhap.accessory.Bridge` carrying many accessories — and runs
everything on a single event loop.

The object model
================

The HomeKit object model is a tree, and every piece of it has one role:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Object
     - Role
   * - :class:`~pyhap.accessory.Accessory`
     - One device in the Home app: a sensor, a lamp, a camera. Holds a list
       of services, routes value changes from them to the driver, and runs
       your code. A standalone accessory has the accessory ID (AID) 1.
   * - :class:`~pyhap.accessory.Bridge`
     - An accessory that exposes other accessories, each with its own AID,
       as one HomeKit device. A controller sees and addresses them
       individually.
   * - :class:`~pyhap.service.Service`
     - One functional unit of an accessory, e.g. a ``TemperatureSensor``.
       Holds the characteristics that make up the function and may link to
       other services. Services and their characteristics are defined by the
       HomeKit specification; the :class:`~pyhap.loader.Loader` serves the
       definitions bundled with HAP-python.
   * - :class:`~pyhap.characteristic.Characteristic`
     - The smallest unit: one value with a type UUID, a format, a range and
       permissions (read, write, notify, …), e.g. ``CurrentTemperature``.
       Controllers read, write and subscribe to characteristics, each
       addressed by the (AID, IID) pair. Your callbacks serve the reads and
       writes.
   * - :class:`~pyhap.accessory_driver.AccessoryDriver`
     - Owns the event loop and glues everything together: it starts and
       stops the HAP server and the mDNS advertisement, mediates requests
       between the server and your objects, fans value changes out to
       subscribed controllers, and persists the state file.
   * - :class:`~pyhap.state.State`
     - The semi-static data of the accessory: its device MAC, its long-term
       key pair, the paired controllers and the per-accessory saved state.
       Persisted to the state file; see :doc:`/advanced/state`.

How the pieces interact
=======================

.. mermaid::
   :align: center

   flowchart TD
       CTRL["Controllers<br>Home app · home hub · Siri"]

       subgraph PYHAP["HAP-python — one process, one event loop"]
           direction TB
           MDNS["mDNS advertisement (_hap._tcp)<br>name · category · config version"]
           HAPS["HAPServer<br>pair-setup · pair-verify · reads · writes · event push"]
           DRV["AccessoryDriver<br>pairings · permissions · subscriptions<br>persistence · shutdown"]
           DB[("accessory.state<br>keys · pairings · saved settings")]

           subgraph ACC["Accessory or Bridge"]
               direction LR
               SRV["Services"]
               CHR["Characteristics"]
               SRV --- CHR
           end

           YOU["Your code<br>run() / stop() · getter and setter callbacks"]
       end

       CTRL -- "discover" --> MDNS
       CTRL <-->|"HAP over HTTP"| HAPS
       HAPS -- "parsed requests" --> DRV
       DRV -- "look up by (AID, IID)" --> ACC
       DRV -- "load / save" --> DB
       DRV -- "re-advertise on config change" --> MDNS
       YOU -- "set_value()" --> CHR
       CHR -- "notify() → publish()" --> DRV
       DRV -- "EVENT to subscribed connections" --> HAPS
       YOU <-->|"getter_callback / setter_callback"| CHR

A controller's write
--------------------

Writes arrive as HTTP requests on the HAP server. The driver finds the
characteristic by its (AID, IID), enforces the characteristic's permissions
and calls your setter callback; the answer is sent back once the callback is
done. Other subscribers then learn the new value as an event.

.. mermaid::
   :align: center

   sequenceDiagram
       autonumber
       participant C as Controller
       participant H as HAPServer
       participant D as AccessoryDriver
       participant X as Characteristic
       participant S as Your setter callback

       C->>H: write aid.iid = value (encrypted)
       H->>D: set_characteristics(...)
       D->>D: enforce the write permissions:<br>write, timed write, admin-only access
       D->>X: client_update_value(value, client_addr)
       X->>S: setter_callback(value)
       Note over S: A coroutine setter is awaited here:<br>the write is answered once it is done
       S-->>X: optional write response
       X-->>D: per-characteristic status (+ response)
       D-->>H: results
       H-->>C: HTTP response
       D->>H: EVENT for the other subscribers of aid.iid
       H-->>C: EVENT aid.iid = value

A value change to subscribers
-----------------------------

Whatever your code does — a periodic measurement in :meth:`Accessory.run
<pyhap.accessory.Accessory.run>`, a callback from a sensor, another thread —
it ends in :meth:`Characteristic.set_value
<pyhap.characteristic.Characteristic.set_value>`. The change is published up
the tree and delivered to every subscribed connection, except the one that
caused it:

.. mermaid::
   :align: center

   sequenceDiagram
       autonumber
       participant R as Your run() (or any thread)
       participant X as Characteristic
       participant A as Accessory / Bridge
       participant D as AccessoryDriver
       participant H as HAPServer
       participant C as Subscribed controllers

       R->>X: set_value(value)
       X->>A: notify() → publish()
       A->>D: publish(aid, iid, value)
       D->>H: push EVENT to each subscriber of the topic
       H-->>C: EVENT aid.iid = value
       Note over D,C: The connection that caused the change<br>is not sent its own event

Publishing is thread-safe: the value is validated where it is set and the
delivery is scheduled onto the event loop, so ``set_value`` may be called
from any thread.

Where to go next
================

:doc:`/intro/tutorial` builds a first accessory end to end.
:doc:`/advanced/state` covers the state file and how your accessory saves
its settings. :doc:`/advanced/async` explains the event loop rules and the
scheduling utilities — read it before writing ``run`` methods or callbacks.
:doc:`/media/camera` documents the camera accessory and its stream services.
The :doc:`/api/index` has the full API reference.