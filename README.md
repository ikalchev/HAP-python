[![PyPI version](https://badge.fury.io/py/HAP-python.svg)](https://badge.fury.io/py/HAP-python)
# HAP-python

HomeKit Accessory Protocol implementation in python 3 (tested with 3.4, 3.5 and 3.6).
With this project, you can integrate your own accessories and add them to your
iOS Home app. Since Siri is integrated with the Home app, you can start voice-control your
accessories right away - e.g. "What is the temperature in my bedroom." and "Turn on the
lights in the Dining Room."

The project was developed for a Raspberry Pi, but it should work on other platforms. You
can even integrate with HAP-python remotely using HTTP (see below). To kick-start things,
you can open `main.py`, where you can find out how to launch a mock temperature sensor.
Just run `python3 main.py` and you should see it in the Home app (be sure to be in the same network).
Stop it by hitting Ctrl+C.

There are example accessories in [the accessories folder](pyhap/accessories) (e.g. AM2302 temperature and humidity sensor).

## Table of Contents
1. [API](#API)
2. [Installation](#Installation)
3. [Integrating non-compatible devices](#HttpAcc)
4. [Run at boot (and a Switch to shutdown your device)](#AtBoot)
5. [Notice](#Notice)

## Installation <a name="Installation"></a>

As a prerequisite, you will need Avahi/Bonjour installed (due to zeroconf package).
On a Raspberry Pi, you can get it with:
```
sudo apt-get install libavahi-compat-libdnssd-dev
```
`avahi-utils` may also fit the bill. Then, you can install with `pip3` (you will need `sudo` or `--user` for the install):
```sh
pip3 install HAP-python
```

This will install HAP-python in your python packages, so that you can import it as `pyhap`. To uninstall, just do:
```
pip3 uninstall HAP-python
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
        # If overriding this method, be sure to call the super's implementation first,
        # because it calls _set_services and performs some other important actions.
        super(TemperatureSensor, self).__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")
        # Having a callback is optional, but you can use it to add functionality.
        self.temp_char.setter_callback = self.temperature_changed

    def temperature_changed(self, value):
        """This will be called every time the value of the CurrentTemperature Characteristic
        is changed. Use setter_callbacks to react to user actions, e.g. setting the
        lights On could fire some GPIO code to turn on a LED (see pyhap/accessories/LightBulb.py).

        NOTE: no need to set the value yourself, this is done for you, before the callback
        is called.
        """
        print("Temperature changed to: ", value)

    def _set_services(self):
        """We override this method to add the services that we want our accessory to
        support. Services have "mandatory" characteristics and optional
        characteristics. Mandatory characteristics are automatically added
        to your selected service, but you must add optional characteristics yourself.
        Take a look at pyhap/accessories/FakeFan.py for an example of how to do that.
        """
        super(TemperatureSensor, self)._set_services()  # Adds some neccessary characteristics.
        # A loader creates Service and Characteristic objects based on json representation
        # such as the Apple-defined ones in pyhap/resources/.
        temp_sensor_service = loader.get_serv_loader().get("TemperatureSensor")
        self.add_service(temp_sensor_service)

    def run(self):
        """We override this method to implement what the accessory will do when it is
        started. An accessory is started and stopped from the AccessoryDriver.

        It might be convenient to use the Accessory's run_sentinel, which is a
        threading.Event object which is set when the accessory should stop running.

        In this example, we wait 3 seconds to see if the run_sentinel will be set and if
        not, we set the current temperature to a random number.
        """
        while not self.run_sentinel.wait(3):
            self.temp_char.set_value(random.randint(18, 26))

    def stop(self):
        """We override this method to clean up any resources or perform final actions, as
        this is called by the AccessoryDriver when the Accessory is being stopped (it is
        called right after run_sentinel is set).
        """
        print("Stopping accessory.")
```

## Integrating non-compatible devices <a name="HttpAcc"></a>
HAP-python may not be available for many IoT devices. For them, HAP-python allows devices
to be bridged by means of communicating with an HTTP server - the [HttpBridge](pyhap/accessories/Http.py). You can add as many remote accessories as you like.

For example, the bellow snippet creates an Http Accessory that listens on port 51800
for updates on the TemperatureSensor service:
```python
import pyhap.util as util
import pyhap.loader as loader
from pyhap.accessories.Http import HttpBridge
from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver

# get loaders
service_loader = loader.get_serv_loader()
char_loader = loader.get_char_loader()

# Create an accessory with the temperature sensor service.
# Also, add an optional characteristic StatusLowBattery to that service.
remote_accessory = Accessory("foo")
tservice = service_loader.get("TemperatureSensor")
tservice.add_opt_characteristic(
    char_loader.get("StatusLowBattery"))
remote_accessory.add_service(tservice)

# Create the HTTP Bridge and add the accessory to it.
address = ("", 51111)
http_bridge = HttpBridge(address=address,
                         display_name="HTTP Bridge",
                         pincode=b"203-23-999")
http_bridge.add_accessory(remote_accessory)
print(remote_accessory)
# Gives you the AID. These are assigned from 2 onwards, in order of addition.

# Add to driver and run.
driver = AccessoryDriver(http_bridge, 51826)
driver.start()
```
Now, remote accessories can do an HTTP POST to the address of the device where the
accessory is running (port 51111) with the following content:
```json
{
    "aid": 2,
    "services": {
        "TemperatureSensor": {
            "CurrentTemperature" : 20,
            "StatusLowBattery": true,
        }
    }
}
```
This will update the value of the characteristic "CurrentTemperature" to 20 degrees C
and "StatusLowBattery" to `true`.
Needless to say the communication to the Http Bridge poses a security risk, so
keep that in mind.

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
