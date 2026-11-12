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

A setter may opt into receiving the writer's address by declaring a
parameter literally named ``sender_client_addr`` — useful when a write must
be tied to the connection that made it, e.g. Setup Endpoints. Any other
signature is called with just the value.

A setter's return value is used as the *write response* for characteristics
with the ``wr`` permission — the value the writing controller reads back in
the HTTP response. A write response, and the read back value of a
``read_back_response`` characteristic, belongs to the connection that wrote:
it wins over the getter and a later ``set_value`` for that connection only,
and it is dropped when the connection closes — also when a coroutine setter
answers only after the connection already closed.

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
  warning; prefer :func:`pyhap.util.async_create_background_task`.
* **Subprocesses on the loop's executor.** If your accessory runs ffmpeg or
  other subprocesses (cameras do), be aware the loop reaps them; the driver
  sets a child watcher when it owns the loop. Starting subprocesses from a
  non-main thread of an embedded loop may need a watcher of your own.