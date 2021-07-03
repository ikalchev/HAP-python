[![PyPI version](https://badge.fury.io/py/HAP-python.svg)](https://badge.fury.io/py/HAP-python) [![Build Status](https://github.com/ikalchev/HAP-python/workflows/CI/badge.svg)](https://github.com/ikalchev/HAP-python) [![codecov](https://codecov.io/gh/ikalchev/HAP-python/branch/master/graph/badge.svg)](https://codecov.io/gh/ikalchev/HAP-python) [![Python Versions](https://img.shields.io/pypi/pyversions/HAP-python.svg)](https://pypi.python.org/pypi/HAP-python/) [![Documentation Status](https://readthedocs.org/projects/hap-python/badge/?version=latest)](http://hap-python.readthedocs.io/en/latest/?badge=latest) [![Downloads](https://pepy.tech/badge/hap-python)](https://pepy.tech/project/hap-python)
# HAP-python

HomeKit Accessory Protocol implementation in python 3.
With this project, you can integrate your own smart devices and add them to your
iOS Home app. Since Siri is integrated with the Home app, you can start voice-control your
accessories right away.

Main features:

* Camera - HAP-python supports the camera accessory from version 2.3.0!
* asyncio support - You can run various tasks or accessories in the event loop.
* Out of the box support for Apple-defined services - see them in [the resources folder](pyhap/resources).
* Secure pairing by just scanning the QR code.
* Integrated with the home automation framework [Home Assistant](https://github.com/home-assistant/home-assistant).

The project was developed for a Raspberry Pi, but it should work on other platforms. To kick-start things,
you can open `main.py` or `busy_home.py`, where you will find some fake accessories.
Just run one of them, for example `python3 busy_home.py`, and you can add it in
the Home app (be sure to be in the same network).
Stop it by hitting Ctrl+C.

There are example accessories as well as integrations with real products
in [the accessories folder](accessories). See how to configure your camera in
[camera_main.py](camera_main.py).

## Table of Contents
1. [API](#API)
2. [Installation](#Installation)
3. [Setting up a camera](#Camera)
4. [Run at boot (and a Switch to shutdown your device)](#AtBoot)
5. [Notice](#Notice)

## Installation <a name="Installation"></a>

As of version 3.5.1, HAP-python no longer supports python older than 3.6, because we
are moving to asyncio. If your platform does not have a compatible python out of the
box, you can install it manually or just use an older version of HAP-python.

As a prerequisite, you will need Avahi/Bonjour installed (due to zeroconf package).
On a Raspberry Pi, you can get it with:
```
$ sudo apt-get install libavahi-compat-libdnssd-dev
```
`avahi-utils` may also fit the bill. Then, you can install with `pip3` (you will need `sudo` or `--user` for the install):
```sh
$ pip3 install HAP-python[QRCode]
```

This will install HAP-python in your python packages, so that you can import it as `pyhap`. To uninstall, just do:
```
$ pip3 uninstall HAP-python
```

## API <a name="API"></a>

A typical flow for using HAP-python starts with implementing an Accessory. This is done by
subclassing [Accessory](pyhap/accessory.py) and putting in place a few details
(see below). After that, you give your accessory to an AccessoryDriver to manage. This
will take care of advertising it on the local network, setting a HAP server and
running the Accessory. Take a look at [main.py](main.py) for a quick start on that.

```python
from pyhap.accessory import Accessory, Category
import pyhap.loader as loader

class TemperatureSensor(Accessory):
    """Implementation of a mock temperature sensor accessory."""

    category = Category.SENSOR  # This is for the icon in the iOS Home app.

    def __init__(self, *args, **kwargs):
        """Here, we just store a reference to the current temperature characteristic and
        add a method that will be executed every time its value changes.
        """
        # If overriding this method, be sure to call the super's implementation first.
        super().__init__(*args, **kwargs)

        # Add the services that this Accessory will support with add_preload_service here
        temp_service = self.add_preload_service('TemperatureSensor')
        self.temp_char = temp_service.get_characteristic('CurrentTemperature')

        # Having a callback is optional, but you can use it to add functionality.
        self.temp_char.setter_callback = self.temperature_changed

    def temperature_changed(self, value):
        """This will be called every time the value of the CurrentTemperature
        is changed. Use setter_callbacks to react to user actions, e.g. setting the
        lights On could fire some GPIO code to turn on a LED (see pyhap/accessories/LightBulb.py).
        """
        print('Temperature changed to: ', value)

    @Acessory.run_at_interval(3)  # Run this method every 3 seconds
    # The `run` method can be `async` as well
    def run(self):
        """We override this method to implement what the accessory will do when it is
        started.

        We set the current temperature to a random number. The decorator runs this method
        every 3 seconds.
        """
        self.temp_char.set_value(random.randint(18, 26))

    # The `stop` method can be `async` as well
    def stop(self):
        """We override this method to clean up any resources or perform final actions, as
        this is called by the AccessoryDriver when the Accessory is being stopped.
        """
        print('Stopping accessory.')
```

## Service Callbacks

When you are working with tightly coupled characteristics such as "On" and "Brightness,"
you may need to use a service callback to receive all changes in a single request.

With characteristic callbacks, you do now know that a "Brightness" characteristic is
about to be processed right after an "On" and may end up setting a LightBulb to 100%
and then dim it back down to the expected level.

```python
from pyhap.accessory import Accessory
from pyhap.const import Category
import pyhap.loader as loader

class Light(Accessory):
    """Implementation of a mock light accessory."""

    category = Category.CATEGORY_LIGHTBULB  # This is for the icon in the iOS Home app.

    def __init__(self, *args, **kwargs):
        """Here, we just store a reference to the on and brightness characteristics and
        add a method that will be executed every time their value changes.
        """
        # If overriding this method, be sure to call the super's implementation first.
        super().__init__(*args, **kwargs)

        # Add the services that this Accessory will support with add_preload_service here
        serv_light = self.add_preload_service('Lightbulb')
        self.char_on = serv_light.configure_char('On', value=self._state)
        self.char_brightness = serv_light.configure_char('Brightness', value=100)

        serv_light.setter_callback = self._set_chars

    def _set_chars(self, char_values):
        """This will be called every time the value of the on of the
        characteristics on the service changes.
        """
        if "On" in char_values:
            print('On changed to: ', char_values["On"])
        if "Brightness" in char_values:
            print('Brightness changed to: ', char_values["Brightness"])

    @Acessory.run_at_interval(3)  # Run this method every 3 seconds
    # The `run` method can be `async` as well
    def run(self):
        """We override this method to implement what the accessory will do when it is
        started.

        We set the current temperature to a random number. The decorator runs this method
        every 3 seconds.
        """
        self.char_on.set_value(random.randint(0, 1))
        self.char_brightness.set_value(random.randint(1, 100))

    # The `stop` method can be `async` as well
    def stop(self):
        """We override this method to clean up any resources or perform final actions, as
        this is called by the AccessoryDriver when the Accessory is being stopped.
        """
        print('Stopping accessory.')
```

## Setting up a camera <a name="Camera"></a>

The [Camera accessory](pyhap/camera.py) implements the HomeKit Protocol for negotiating stream settings,
such as the picture width and height, number of audio channels and others.
Starting a video and/or audio stream is very platform specific. Because of this,
you need to figure out what video and audio settings your camera supports and set them
in the `options` parameter that is passed to the `Camera` Accessory. Refer to the
documentation for the `Camera` contructor for the settings you need to specify.

By default, HAP-python will execute the `ffmpeg` command with the negotiated parameters
when the stream should be started and will `terminate` the started process when the
stream should be stopped (see the default: `Camera.FFMPEG_CMD`).
If the default command is not supported or correctly formatted for your platform,
the streaming can fail.

For these cases, HAP-python has hooks so that you can insert your own command or implement
the logic for starting or stopping the stream. There are two options:

1. Pass your own command that will be executed when the stream should be started.

    You pass the command as a value to the key `start_stream_cmd` in the `options` parameter to
    the constuctor of the `Camera` Accessory. The command is formatted using the
    negotiated stream configuration parameters. For example, if the negotiated width
    is 640 and you pass `foo start -width {width}`, the command will be formatted as
    `foo start -width 640`.

    The full list of negotiated stream configuration parameters can be found in the
    documentation for the `Camera.start` method.

2. Implement your own logic to start, stop and reconfigure the stream.

    If you need more flexibility in managing streams, you can directly implement the
    `Camera` methods `start`, `stop` and `reconfigure`. Each will be called when the
    stream should be respectively started, stopped or reconfigured. The start and
    reconfigure methods are given the negotiated stream configuration parameters.

    Have a look at the documentation of these methods for more information.

Finally, if you can take snapshots from the camera, you may want to implement the
`Camera.snapshot` method. By default, this serves a stock photo.

## Run at boot <a name="AtBoot"></a>
This is a quick way to get `HAP-python` to run at boot on a Raspberry Pi. It is recommended
to turn on "Wait for network" in `raspi-config`. If this turns to be unreliable, see
[this](https://www.raspberrypi.org/forums/viewtopic.php?f=66&t=187225).

Copy the below in `/etc/systemd/system/HAP-python.service` (needs sudo).
```
[Unit]
Description = HAP-python daemon
Wants = pigpiod.service  # Remove this if you don't depend on pigpiod
After = local-fs.target network-online.target pigpiod.service

[Service]
User = lesserdaemon  # It's a good idea to use some unprivileged system user
# Script starting HAP-python, e.g. main.py
# Be careful to set any paths you use, e.g. for persisting the state.
ExecStart = /usr/bin/python3 /home/lesserdaemon/.hap-python/hap-python.py

[Install]
WantedBy = multi-user.target
```

Test that everything is fine by doing:

```sh
> sudo systemctl start HAP-python
> systemctl status HAP-python
> sudo journalctl -u HAP-python  # to see the output of the start up script.
> sudo systemctl stop HAP-python
```

To enable or disable at boot, do:

```sh
> sudo systemctl enable HAP-python
> sudo systemctl disable HAP-python
```

### Shutdown switch

If you are running `HAP-python` on a Raspberry Pi, you may want to add a
[Shutdown Switch](pyhap/accessories/ShutdownSwitch.py) to your Home. This is a
Switch Accessory, which, when triggered, executes `sudo shutdown -h now`, i.e.
it shutdowns and halts the Pi. This allows you to safely unplug it.

For the above to work, you need to enable passwordless `/sbin/shutdown` to whichever
user is running `HAP-python`. For example, do:
```sh
$ sudo visudo # and add the line: "<hap-user> ALL=NOPASSWD: /sbin/shutdown".
```

## Notice <a name="Notice"></a>

Some HAP know-how was taken from [HAP-NodeJS by KhaosT](https://github.com/KhaosT/HAP-NodeJS).

I am not aware of any bugs, but I am more than confident that such exist. If you find any,
please report and I will try to fix them.

Suggestions are always welcome.

Have fun!
