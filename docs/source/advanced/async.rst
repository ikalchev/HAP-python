.. _advanced-async:

=======================
Async rules and helpers
=======================

HAP-python is asyncio based: **one event loop serves everything** — the HAP
server, pairing, event delivery, your ``run`` method and your callbacks. This
page lists the rules that follow from that, and the utilities the library
ships so you do not have to work around it.

.. contents:: On this page
   :local:
   :depth: 1

Who runs the loop
==================

Two ways to run HAP-python:

Standalone
   :meth:`AccessoryDriver.start <pyhap.accessory_driver.AccessoryDriver.start>`
   creates the event loop, runs it and blocks until the accessory stops. It
   also sets up a thread pool executor for blocking work and handles
   ``KeyboardInterrupt`` and ``SIGTERM`` (via :meth:`AccessoryDriver.signal_handler
   <pyhap.accessory_driver.AccessoryDriver.signal_handler>`) by stopping
   gracefully.

Embedded
   Pass your own loop with ``loop=`` — this is what embedders such as Home
   Assistant do. The driver does not create a loop or an executor then; you
   run the loop, and the accessory starts when you call :meth:`AccessoryDriver.start_service
   <pyhap.accessory_driver.AccessoryDriver.start_service>`.

Scheduling work: add_job
========================

The driver is the single entry point for running things on or off the loop:

* :meth:`AccessoryDriver.add_job <pyhap.accessory_driver.AccessoryDriver.add_job>`
  schedules a callable from **any thread**, thread-safely.
* :meth:`AccessoryDriver.async_add_job <pyhap.accessory_driver.AccessoryDriver.async_add_job>`
  does the same from **within the loop**.

What gets scheduled depends on what you pass:

* a **coroutine** or a **coroutine function** becomes a task on the loop;
* a function decorated with :func:`@pyhap.util.callback <pyhap.util.callback>`
  is invoked in the loop as soon as it is free (``call_soon``) — no task is
  created for it;
* **any other callable** runs in a thread of the executor, off the loop.

That last rule is the important one: the way to run blocking work is to pass
a plain function to ``add_job`` — you get a future, the loop stays free, and
the HAP server keeps answering while your code blocks.

Your accessory's code
=====================

``run``
   Called once the HAP server is up and the accessory is advertised.
   Override it with a coroutine (or a plain method — it is scheduled the
   same way) and use it to poll hardware, watch for events, and publish
   values. It is started with ``add_job``, so ``await`` inside it is
   enough to run other work concurrently.

``run_at_interval``
   The decorator :meth:`Accessory.run_at_interval
   <pyhap.accessory.Accessory.run_at_interval>` repeats your ``run`` method
   every N seconds and stops when the driver stops:

   .. code-block:: python

       @Accessory.run_at_interval(3)
       async def run(self):
           value = await read_sensor()
           self.char_temp.set_value(value)

   A ``run`` that loops on its own should watch ``driver.aio_stop_event``:

   .. code-block:: python

       async def run(self):
           while not await util.event_wait(self.driver.aio_stop_event, 3):
               self.char_temp.set_value(await read_sensor())

``stop``
   Awaited by the driver when it stops. Cancel your tasks and release
   resources here — subprocesses, sockets, sensors.

``client_disconnected``
   Called in the loop whenever a controller's connection closes, with the
   client's ``(address, port)``. Override it to end anything that connection
   started — e.g. its camera streams; a controller that just walks away
   never sends anything else.

``client_connected`` / ``client_verified``
   The other two connection hooks, async by design like the delegates.
   ``client_connected`` runs when a controller's TCP connection is
   accepted — before it has proven anything about itself — and
   ``client_verified`` when it finishes pair verify, with its UUID as the
   second argument: the same identity as the context's
   :attr:`~pyhap.context.Context.client_uuid`, stable across all of that
   controller's connections. Both run as isolated jobs: their exceptions
   are logged, and the connection or the pair verify response never waits
   for them.

Characteristic callbacks
========================

* ``getter_callback`` — a plain callable, invoked on every read of the
  characteristic. Keep it fast: it runs on the loop, and a slow getter
  delays every read of that characteristic.
* ``setter_callback`` — a plain callable **or a coroutine function**. When a
  controller writes, the driver calls it and **answers the write only once
  the callback is done** — a coroutine setter is awaited, and subscribers
  are told about the value only after it succeeded. HomeKit answers time out
  in about 10 seconds: a setter must finish quickly, or start its heavy work
  as a task (``asyncio.create_task`` / ``driver.add_job``) and return.

A legacy setter is always called with just the value. What a callback
needs to know about the request — the writer's connection, its privileges,
the deadline — arrives through the delegate interface below instead; the
former ``sender_client_addr`` parameter-name opt-in is gone with it.

**The legacy callbacks are deprecated.** They keep working, wrapped
into the delegate calls below: a sync setter runs inside the awaited
``on_set``, a coroutine setter is awaited, and the grouped callbacks of a
service or an accessory run as its pre-check. But assigning one — a
characteristic's
:attr:`~pyhap.characteristic.Characteristic.setter_callback` or
:attr:`~pyhap.characteristic.Characteristic.getter_callback`, the
``setter_callback``/``getter_callback`` parameters of
:meth:`~pyhap.service.Service.configure_char`, or a service's or an
accessory's ``setter_callback`` — announces a ``DeprecationWarning``
that names the assigning line. Watch for it in your test runs and move
new code to the delegates below.

**Answering a HAP status.** A callback that fails does not have to answer
the generic ``-70402`` (service communication failure): raise
:class:`pyhap.errors.HapStatusError` with a status the controller can act
on, and the characteristic answers with it instead —

.. code-block:: python

   from pyhap.const import HAP_SERVER_STATUS
   from pyhap.errors import HapStatusError

   def set_lock_target_state(value):
       if not lock.is_ready():
           raise HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
       lock.set_state(value)

The status set is curated — see :ref:`api-errors` for which statuses a
callback may decide and why the rest belong to the driver. Every other
exception keeps answering ``-70402``.

Getters answer the same way: a read of the characteristic returns the
status instead of a value, while the other characteristics of the request
keep their own results. The accessories list carries no per-characteristic
status, so there a failing getter — whatever it raises — answers by listing
its characteristic without a value, and one broken accessory cannot break
another one's pairing.

A setter's return value is used as the *write response* for characteristics
with the ``wr`` permission — the value the writing controller reads back in
the HTTP response. A write response, and the read back value of a
``read_back_response`` characteristic, belongs to the connection that wrote:
it wins over the getter and a later ``set_value`` for that connection only,
and it is dropped when the connection closes — also when a coroutine setter
answers only after the connection already closed.

Delegate writes
================

Every write runs through the delegates, see :ref:`api-delegates` — a
legacy callback above is wrapped into one, so one pipeline serves both:

1. Validation, privileges and timed-write checks — the same ones as
   before. An explicit ``null`` value answers ``-70410``: nothing is
   applied and no callback runs.
2. The values are stored, but subscribers are not told yet — and the
   request of a *private* write (a write response or a
   ``read_back_response`` characteristic) is put back at once, so no
   other controller reads it while the setters run. A controller's write
   is recorded as a *client update* of the characteristic's value: the
   legacy :meth:`Characteristic.client_update_value
   <pyhap.characteristic.Characteristic.client_update_value>` runs the
   whole update in one call, while this path splits it into storing the
   value, the delegates' ``on_set`` and step 5's
   :meth:`Characteristic.client_update_finished
   <pyhap.characteristic.Characteristic.client_update_finished` telling
   the subscribers.
3. The service-level pre-check: the ``on_set`` of each written service's
   :class:`~pyhap.delegates.ServiceDelegate` runs, concurrently, with
   every written value of its service — the legacy grouped setter of a
   service or an accessory without a delegate runs here too, wrapped.
   A :class:`pyhap.errors.HapStatusError` refuses the writes of that
   service — their values are restored, nobody is notified, their
   per-characteristic ``on_set`` calls do not run, and the raised
   status is answered. A timeout (``-70408``) or any other failure
   (``-70402``) refuses the same way.
4. The per-characteristic ``on_set`` calls of the surviving writes run
   concurrently, in request order, under the same deadline.
5. Subscribers learn the values of the writes that succeeded; a write
   that raised, failed or missed its deadline restores the previous
   value and notifies nobody, answering its status.

The deadline in the context is one per request — the driver answers a
request it cannot finish in time with ``-70408`` per characteristic. A
direct ``set_characteristics`` call has no controller waiting on a
timeout: it stores the values, answers at once — optimistically, all
successes — and runs the delegates as a job without a deadline; the
write responses and read back answers arrive through the store, read
back by the writing connection.

The camera stack itself writes through delegates: every characteristic
pyhap's own camera code answers — the stream management services' control
points, the HDS transport setup, the recording and HKSV 17.99 settings —
runs its handler through the interface above. Its multi-tier and WebRTC
sessions belong to the connection that set them up, by
:class:`~pyhap.connection.HAPConnectionHandle`, so a later connection that
reuses a closed one's address cannot control them.

Delegate reads
================

Reads await delegates the same way writes do — this is the point of the
getter and the setter having the same async-ness. ``GET
/characteristics`` and ``GET /accessories`` await the ``on_get`` of every
readable characteristic of the request — a legacy getter is wrapped into
one — concurrently, under the same deadline as a write:

* a read that misses the deadline answers ``-70408`` for its
  characteristic; cancelling the response — e.g. because the connection
  closed — cancels the reads, like it cancels writes;
* a failing read answers its status: a raised
  :class:`pyhap.errors.HapStatusError` answers its code, anything else
  ``-70402``. The accessories list carries no per-characteristic status,
  so there a failing read lists its characteristic without a value,
  like a failing getter does;
* the answer is validated like any value, then stored on the
  characteristic — like a legacy getter's answer. Subscribers are not
  told: a read is not a change.

A write response still wins over ``on_get``: the connection that wrote a
``read_back_response`` characteristic reads back what it wrote, and the
other connections read ``on_get``'s answer.

Direct calls cannot await: ``driver.get_characteristics(...)`` and
``driver.get_accessories()`` answer the stored values at once and run the
reads as a job without a deadline, like a direct write runs its setters.
The HAP representation — ``to_HAP`` — answers the stored value too; a
fresh answer comes from the read path, since ``on_get`` or a legacy
getter may answer something else. The representation cache skips every
characteristic whose reads are answered dynamically, and
``get_value()`` keeps running a legacy getter inline for a direct call.

Delegate subscriptions
======================

A controller enables events for a characteristic by writing it with
``ev``, and a delegate hears about that: the
``on_subscribe``/``on_unsubscribe`` of the characteristic's
:class:`~pyhap.delegates.CharacteristicDelegate` run with a
:class:`~pyhap.context.CharReadContext` — the same context as a read,
without a deadline.

The hooks are isolated from what caused them: they run as background
jobs, so the write request (or the connection close) never waits for
one, and a failing hook is logged without affecting the subscription —
the subscription itself has already registered. They run on the change
only: subscribing a controller that is already subscribed, or
unsubscribing one that is not, runs nothing.

``on_unsubscribe`` also runs when a subscribed controller's connection
closes, with the connection's identity and its UUID when the driver
knows them. Use the pair to run work only while someone listens — e.g.
poll a battery-powered sensor between ``on_subscribe`` and
``on_unsubscribe``:

.. code-block:: python

   class SensorDelegate(CharacteristicDelegate):
       async def on_subscribe(self, context):
           self.task = asyncio.create_task(self._poll())

       async def on_unsubscribe(self, context):
           self.task.cancel()

Connections, not addresses
==========================

Everything the driver keeps per controller is keyed by the connection,
not by its address: event subscriptions, prepared timed writes, write
responses and the server's connections, all by
:class:`~pyhap.connection.HAPConnectionHandle`. The address of a
connection is unique only while it lives, so keying by it needed guards
at every boundary - a response that arrived after its connection closed
had to be dropped, or a later connection reusing the address could have
read it. Keyed by the handle, nothing of a closed connection reaches a
later one at all: it cannot read its write responses or control its
camera sessions, and an answer that arrives only after its connection
closed is dropped instead of kept, because the store keeps a response
only of a connection it may still answer.

A direct call without a connection - ``driver.set_characteristics`` and
friends - acts as a virtual connection of its client address, so its
prepared timed writes, its stored write responses and its read-backs
pair up across calls, like the requests of a controller pair up over its
one connection.

Provided utilities
==================

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Utility
     - What it is for
   * - :func:`pyhap.util.callback`
     - Marks a function to be scheduled with ``call_soon`` — run in the
       loop, in order, without creating a task.
   * - :func:`pyhap.util.event_wait`
     - ``await util.event_wait(event, timeout)`` waits for an
       ``asyncio.Event`` up to ``timeout`` seconds and returns
       ``event.is_set()``. The standard way to poll and still exit on stop;
       see the ``run`` example above.
   * - :func:`pyhap.util.async_create_background_task`
     - Creates a task nobody awaits and keeps a strong reference to it, so
       it is neither garbage-collected mid-run nor silently forgotten. Use
       it for fire-and-forget work you want to be sure runs to completion.
   * - :meth:`Accessory.run_at_interval <pyhap.accessory.Accessory.run_at_interval>`
     - Repeats your ``run`` method every N seconds until the driver stops.
   * - :meth:`AccessoryDriver.add_job <pyhap.accessory_driver.AccessoryDriver.add_job>`
     - Thread-safe scheduling of callables and coroutines; plain callables
       run in the executor, off the loop.
   * - :func:`pyhap.util.get_local_address`
     - The local IP address of the machine, e.g. for a camera's
       ``options["address"]``.

Caveats
=======

* **Never block the loop.** A blocking call in ``run``, a setter or a getter
  stalls every HAP request, every event and every other accessory in the
  process. Blocking work goes through ``add_job`` (executor) or
  ``asyncio.to_thread``.
* **From other threads, use the safe entry points.** ``set_value`` (and
  therefore ``publish``) is thread-safe: the delivery is scheduled onto the
  loop with ``call_soon_threadsafe``. For anything else, go through
  ``driver.add_job``.
* **Events only reach subscribers.** A value change is delivered to the
  connections that enabled events (``ev``) for that characteristic — and
  never back to the connection that caused it.
* **Un-awaited tasks are not free.** A bare ``asyncio.create_task`` result
  can be garbage-collected before it runs, and its exception is only a
  warning; prefer :func:`pyhap.util.async_create_background_task`. The same
  applies to awaitables returned *by* awaited calls — e.g. zeroconf's
  ``async_unregister_service`` returns the goodbye-broadcast task, which must
  be awaited too, or it is still pending when the loop closes. The test
  suite enforces this: ``RuntimeWarning`` ("coroutine ... was never
  awaited") is promoted to a test error via ``filterwarnings``, a guard
  fixture in ``tests/conftest.py`` fails any test during which asyncio logs
  "Task was destroyed but it is pending!", and ruff's ``RUF006`` flags
  ``create_task``/``ensure_future`` results that are not kept.
* **Subprocesses on the loop's executor.** If your accessory runs ffmpeg or
  other subprocesses (cameras do), be aware the loop reaps them; the driver
  sets a child watcher when it owns the loop. Starting subprocesses from a
  non-main thread of an embedded loop may need a watcher of your own.