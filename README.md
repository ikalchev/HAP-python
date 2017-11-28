# HAP-python

HomeKit Accessory Protocol implementation in python 3. With this project, you can create
accessories in python and add them to your iOS Home app.

The project was developed for a Raspberry Pi, but it should work on other platforms. You
can even integrate with HAP-python remotely using HTTP (see below).

To kick-start things, you can open `main.py`, where you can find out how to launch a mock temperature sensor. To start, run

```
python3 main.py
```

and you should see it in the Home app (be sure to be in the same network). Stop it by
hitting Ctrl+C.

There are example accessories for some sensors in [the accessories folder](pyhap/accessories) (e.g. AM2302 temperature and humidity sensor).

## Table of Contents
1. [API](#API)
2. [Integrating non-compatible devices](#HttpAcc)
3. [Installation](#Installation)
4. [Run at boot](#AtBoot)
5. [Notice](#Notice)

## API <a name="API"></a>

A typical flow for using HAP-python starts with implementing an Accessory. This is done by
subclassing [Accessory](pyhap/accessory.py) and putting in place a few details
(see below). After that, you give your accessory to an AccessoryDriver to manage. This
will take care of advertising it on the local network, setting a HAP server and
running the Accessory. Take a look at [main.py](main.py) for a quick start.

The main things to do when implementing an accessory are:

### 1. Set the services that the new accessory will support
This is done by implementing the `_set_services` method, which is called during the
initialisation of Accessory. For example:
```python
class TheAccessory(Accessory):
...
    def _set_services(self):
        super(TheAccessory, self)._set_services()
        service_loader = loader.get_serv_loader()
        tempService = service_loader.get("TemperatureSensor")
        self.add_service(tempService)
```
The `loader` creates Service objects based on json representation. These can be found in
[the resources folder](pyhap/resources). The json files contain the services and
characteristics (most of them, at least) specified by Apple. Have a look. By adding a service, its
characteristics also get added for you.

### 2. Specify what the Accessory will do
The accessory is eventually run in its own thread. The thread is started with the method
`run` with no arguments. For example:
```python
class TheAccessory(Accessory):
...
    def run(self):
        tempChar = self.get_service("TemperatureSensor")\
                       .get_characteristic("CurrentTemperature")
        while not self.run_sentinel.wait(3):
            tempChar.set_value(random.randint(18, 26))
```
Here, the run method is just a while loop that sets the current temperature to a
random number. The `run_sentinel` is a `threading.Event` object that
every accessory has and it is used to notify the accessory that it should stop running.
In some cases you can skip implementing the run method, e.g. see [LightBulb](pyhap/accessories/LightBulb.py).

### 3. Specify how to stop
When the accessory is stopped by the accessory driver, it first sets the `run_sentinel`
and then calls `Accessory.stop()`. This is your chance to clean up any resources you like,
e.g. files, sockets, gpios, etc.

## Integrating non-compatible devices <a name="HttpAcc"></a>
HAP-python may not be available for many IoT devices. For them, HAP-python allows devices
to be bridged by means of communicating with an HTTP server - the [Http Accessory](pyhap/accessories/Http.py).

For example, the bellow snippet creates an Http Accessory that listens on port 51800
for updates on the TemperatureSensor service:
```python
import pyhap.util as util
from pyhap.accessories.Http import Http
from pyhap.accessory import STANDALONE_AID
from pyhap.accessory_driver import AccessoryDriver

http = Http(("", 51800), ["TemperatureSensor"], "HTTP bridge",
            aid=STANDALONE_AID, mac=util.generate_mac(), pincode=b"203-23-999")
driver = AccessoryDriver(http, 51826)
driver.start()
```
Now, remote accessories can do an HTTP POST to the address of the device where the
accessory is running (port 51800) with the following content:
```json
{ "TemperatureSensor" : {
     "CurrentTemperature" : 20 }
}
```
This will update the value of the characteristic "CurrentTemperature" to 20 degrees C.

Needless to say the communication to the Http Accessory poses a security risk, so
keep that in mind.

## Installation <a name="Installation"></a>

To install HAP-python, you will need `setuptools`. Just git clone this project and do:

```
python3 setup.py install
```

This will install HAP-python in your python packages, so that you can import it as `pyhap`.
Alternatively, you can install only the dependencies and run with the root directory of this project in your `PYTHONPATH`.

To uninstall with pip, just do:

```
pip3 uninstall HAP-python
```

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

## Notice <a name="Notice"></a>

Some HAP know-how was taken from [HAP-NodeJS by KhaosT](https://github.com/KhaosT/HAP-NodeJS).

The characteristics and services that are supported by HomeKit may not all be present in the [resources folder](pyhap/resources).
Also, there are some missing parts, like default values for characteristics.

Lastly, I am not aware of any bugs, but I am more than confident that such exist.

Suggestions are always welcome.

Have fun!
