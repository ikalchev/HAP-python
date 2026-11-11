.. _intro-examples:

========
Examples
========

The `accessories directory`_ of the repository ships runnable examples, one
file per accessory. Each builds on the same shape as the tutorial — a
subclass of :class:`~pyhap.accessory.Accessory` with services and callbacks
— so they work both as a starting point and as a reference for a particular
device.

.. _accessories directory: https://github.com/ikalchev/HAP-python/tree/master/accessories

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Example
     - What it shows
   * - ``LightBulb.py``
     - A light with On/Brightness/Hue/Saturation characteristics and
       setter callbacks driving the state.
   * - ``TemperatureSensor.py``
     - A sensor reporting a value periodically from ``run``.
   * - ``MotionSensor.py``, ``adaptive_lightbulb.py``
     - A sensor that triggers characteristics on motion; a light that
       changes its value from a background ``run`` task.
   * - ``TV.py``, ``FakeFan.py``, ``DisplaySwitch.py``
     - TVs, fans and switches: services with several linked characteristics.
   * - ``BMP180.py``, ``AM2302.py``, ``TSL2591.py``, ``SDS011.py``
     - Reading real hardware (I²C, GPIO, serial) and publishing it, polled
       from ``run`` with ``run_at_interval``.
   * - ``camera_main.py``
     - A full camera accessory with negotiated streams (see
       :doc:`/media/camera`) and the default ffmpeg streaming.
   * - ``ShutdownSwitch.py``, ``RPI_Relay.py``, ``NeoPixelLightStrip.py``
     - Acting on the host: system commands, GPIO, LED strips.
   * - ``Http.py``
     - An accessory that fetches its state over HTTP.
   * - ``busy_home.py``
     - A bridge with many accessories of mixed kinds.

To run one, install the repository and run the file with the project's
virtualenv active — or copy it into your own script and adapt it. The
``main.py`` at the root of the repository is the minimal standalone version
of the tutorial's sensor.

For cameras, start from ``accessories/camera_main.py``: the options dict
describes what the camera supports, and HAP-python negotiates the sessions
(see :doc:`/media/camera`).