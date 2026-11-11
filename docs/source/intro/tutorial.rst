.. _intro-tutorial:

=========
Tutorials
=========

.. contents:: On this page
   :local:
   :depth: 1

Your first accessory
====================

This tutorial builds a temperature sensor: a standalone accessory that
reports a new value every few seconds. The complete script is at the end of
the page.

Install HAP-python (see :doc:`install`) into a virtualenv, then start from
the imports:

.. code-block:: python

    import logging
    import random

    from pyhap.accessory import Accessory
    from pyhap.accessory_driver import AccessoryDriver
    from pyhap.const import CATEGORY_SENSOR

    logging.basicConfig(level=logging.INFO)

Define the accessory
--------------------

An accessory is a subclass of :class:`~pyhap.accessory.Accessory`. In
``__init__`` you give it its services and characteristics:

.. code-block:: python

    class TemperatureSensor(Accessory):

        category = CATEGORY_SENSOR  # shown with the right icon in Home

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            service = self.add_preload_service("TemperatureSensor")
            self.char_temp = service.configure_char("CurrentTemperature")

``add_preload_service`` loads the service definition — the set of
characteristics the HomeKit specification gives it — from the definitions
bundled with HAP-python, and adds it to the accessory.
``configure_char`` fetches the ``CurrentTemperature`` characteristic of the
service and keeps a reference to it.

Report values
-------------

Whatever your accessory does, it ends with a value on a characteristic.
Override :meth:`~pyhap.accessory.Accessory.run` — it is called once the HAP
server is up — and set the value with
:meth:`~pyhap.characteristic.Characteristic.set_value`; every subscribed
controller is told about each change:

.. code-block:: python

        @Accessory.run_at_interval(3)
        async def run(self):
            self.char_temp.set_value(random.randint(18, 26))

:class:`~pyhap.accessory.Accessory.run_at_interval` repeats your ``run``
every 3 seconds, until the driver stops. A real sensor would read its
hardware instead of rolling dice — see :doc:`/advanced/async` for how to
await things here without blocking the HAP server.

Start the driver
----------------

The :class:`~pyhap.accessory_driver.AccessoryDriver` hosts the accessory: it
runs the event loop, serves HAP requests and advertises the accessory over
mDNS:

.. code-block:: python

    def main():
        driver = AccessoryDriver(port=51826)
        sensor = TemperatureSensor(driver, "MyTemperatureSensor")
        driver.add_accessory(sensor)

        import signal
        signal.signal(signal.SIGTERM, driver.signal_handler)

        driver.start()

    if __name__ == "__main__":
        main()

``add_accessory`` loads (or creates) the state file — the pairings and keys
that make the accessory *the same device* across restarts; see
:doc:`/advanced/state`.

Pair with your iPhone
---------------------

Run the script. The driver prints the setup code and, with the ``QRCode``
extra installed (``pip install HAP-python[QRCode]``), a QR code:

.. code-block:: text

    Starting accessory MyTemperatureSensor...
    Enter this code in your HomeKit app on your iOS device: 123-45-678

Open the Home app, add an accessory, and enter the code (or scan the QR).
Your sensor is on the network. Its ``CurrentTemperature`` updates every three
seconds — turn a read into a write and the same machinery runs in reverse:
a characteristic's ``setter_callback`` is called with whatever the
controller wrote.

The complete script
-------------------

.. code-block:: python

    import logging
    import random
    import signal

    from pyhap.accessory import Accessory
    from pyhap.accessory_driver import AccessoryDriver
    from pyhap.const import CATEGORY_SENSOR

    logging.basicConfig(level=logging.INFO)


    class TemperatureSensor(Accessory):

        category = CATEGORY_SENSOR

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            service = self.add_preload_service("TemperatureSensor")
            self.char_temp = service.configure_char("CurrentTemperature")

        @Accessory.run_at_interval(3)
        async def run(self):
            self.char_temp.set_value(random.randint(18, 26))


    def main():
        driver = AccessoryDriver(port=51826)
        sensor = TemperatureSensor(driver, "MyTemperatureSensor")
        driver.add_accessory(sensor)
        signal.signal(signal.SIGTERM, driver.signal_handler)
        driver.start()


    if __name__ == "__main__":
        main()

Reacting to writes
==================

A switch shows the other direction: the controller writes, your code reacts.
Give the characteristic a setter callback:

.. code-block:: python

    class Switch(Accessory):

        category = CATEGORY_SWITCH

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            service = self.add_preload_service("Switch")
            service.configure_char("On", setter_callback=self.set_on)

        def set_on(self, value):
            logging.info("Switch turned %s", "on" if value else "off")
            # drive the relay here

The callback runs when a controller writes. It may also be a coroutine
function — the write is answered once it completes, so it must be quick;
start slow work as a task. To know *which* controller wrote, declare a
parameter named ``sender_client_addr`` — see :doc:`/advanced/async`.

Bridging several accessories
============================

A :class:`~pyhap.accessory.Bridge` exposes many accessories as one HomeKit
device — the usual shape for a hub that controls many sensors or lamps:

.. code-block:: python

    bridge = Bridge(driver, "MyBridge")
    bridge.add_accessory(TemperatureSensor(driver, "Bedroom"))
    bridge.add_accessory(Switch(driver, "Coffee maker"))
    driver.add_accessory(bridge)

Every bridged accessory gets its own AID and appears in Home individually.

What to read next
==================

* :doc:`/intro/overview` — how the objects fit together, with diagrams.
* :doc:`/advanced/async` — before writing ``run`` methods or callbacks.
* :doc:`/advanced/state` — saving your accessory's settings.
* :doc:`/media/camera` — cameras, streams and recording.