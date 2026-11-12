[![PyPI version](https://badge.fury.io/py/HAP-python.svg)](https://badge.fury.io/py/HAP-python) [![CI](https://github.com/ikalchev/HAP-python/actions/workflows/ci.yaml/badge.svg)](https://github.com/ikalchev/HAP-python/actions/workflows/ci.yaml) [![codecov](https://codecov.io/gh/ikalchev/HAP-python/graph/badge.svg)](https://codecov.io/gh/ikalchev/HAP-python) [![Python Versions](https://img.shields.io/pypi/pyversions/HAP-python.svg)](https://pypi.python.org/pypi/HAP-python/) [![Documentation Status](https://readthedocs.org/projects/hap-python/badge/?version=latest)](http://hap-python.readthedocs.io/en/latest/?badge=latest) [![Downloads](https://pepy.tech/badge/hap-python)](https://pepy.tech/project/hap-python)

# HAP-python

HAP-python is a [HomeKit Accessory Protocol](https://developer.apple.com/homekit/) implementation in Python 3.
It lets you expose your own smart home devices as HomeKit accessories, so you can add them
to the Home app on your Apple device.

Main features:

* **Camera support** — negotiate stream settings with iOS and start video/audio streams (see [pyhap/camera.py](pyhap/camera.py)).
* **asyncio support** — run various tasks or accessories in the event loop.
* **Out of the box support for Apple-defined services** — see them in [the resources folder](pyhap/resources).
* **Secure pairing** — just scan a QR code, no manual codes to type in.
* **Proven in production** — HAP-python powers the HomeKit integration of the home automation framework [Home Assistant](https://github.com/home-assistant/home-assistant).

Full API documentation is available on [readthedocs](http://hap-python.readthedocs.io/).

## Installation

As a prerequisite, you will need Avahi/Bonjour installed (due to the zeroconf package).
On a Raspberry Pi, you can get it with:
```sh
$ sudo apt-get install libavahi-compat-libdnssd-dev
```
`avahi-utils` may also fit the bill. Then, you can install with e.g. `pip`:
```sh
$ pip install HAP-python[QRCode]
```

This will install HAP-python in your Python packages, so that you can import it as
`pyhap`. The following optional features can be installed via pip extras:

* `QRCode` — print a pairing QR code in the terminal (`base36`, `pyqrcode`).
* `WebRTC` — WebRTC live view for cameras (`pyOpenSSL`, `ifaddr`).
* `bench` — dependencies for running the benchmarks (`psutil`, `py-spy`).

## Demo

Implementing an accessory is as simple as subclassing
[Accessory](pyhap/accessory.py) and handing it over to an
[AccessoryDriver](pyhap/accessory_driver.py), which advertises it on the local network,
sets up a HAP server and runs it:

```python
import signal

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB


class LightBulb(Accessory):
    """A simple mock light bulb that reacts to being toggled."""

    category = CATEGORY_LIGHTBULB  # This is for the icon in the iOS Home app.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        service = self.add_preload_service('Lightbulb')
        # The callback fires every time the value is set from the Home app.
        self.char_on = service.configure_char('On', setter_callback=self.bulb_toggled)

    def bulb_toggled(self, value):
        print(f'The light is now {"on" if value else "off"}')
        # Here you could e.g. fire some GPIO code to switch a real light.


# Start the accessory on the network, so it can be added in the Home app
# (be sure to be on the same network). Stop it with Ctrl+C.
driver = AccessoryDriver(port=51826)
driver.add_accessory(accessory=LightBulb(driver, 'My LightBulb'))

# We want SIGTERM (terminate) to be handled by the driver itself,
# so that it can gracefully stop the accessory, server and advertising.
signal.signal(signal.SIGTERM, driver.signal_handler)

driver.start()
```

Run the script, add the accessory in the Home app, and toggling the light in the app will
print its new state. To do work in the background, you can also override the `run` and
`stop` methods — see [main.py](main.py) for an example of that.

There are more example accessories — as well as integrations with real products such as
the DHT11 temperature sensor, NeoPixel light strips and displays — in
[the accessories folder](accessories). See how to configure your camera in
[accessories/camera_main.py](accessories/camera_main.py).

## Camera

The [Camera accessory](pyhap/camera.py) implements the HomeKit Protocol for negotiating
stream settings, such as the picture width and height or the number of audio channels.
Because starting a video and/or audio stream is very platform specific, you need to
figure out what video and audio settings your camera supports and describe them in the
`options` parameter passed to the `Camera` constructor. HAP-python can then either run a
custom stream command for you, or you can take full control by implementing the
`start_stream`, `stop_stream` and `reconfigure_stream` methods. See the documentation of
the `Camera` class and its methods in [pyhap/camera.py](pyhap/camera.py) for details, and
[accessories/camera_main.py](accessories/camera_main.py) for a working example.

## Use of AI

AI-assisted contributions are welcome. However, you remain fully responsible for them: disclose AI use, verify the code yourself (tests, lint, docs), keep PRs focused and concise, and respond to review feedback as you would for your own hand-written code.

Contributions that ignore this may be closed without review.
