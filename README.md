# HAP-python

HomeKit Accessory Protocol implementation in python.

With this project, you can create HomeKit accessories in python and add them to your iOS Home app.

The project was developed for a Raspberry Pi, but it should work on other platforms.

To kick-start things, you can open `main.py`, where you can find out how to launch a mock temperature sensor. To start, run

```
python3 main.py
```

and you should see it in the Home app (be sure to be in the same network). Stop it by
hitting Ctrl+C.

There are example accessories for some sensors in [the accessories folder](pyhap/accessories) (e.g. AM2302 temperature and humidity sensor).

## API

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
In the above, the run method is just a while loop that sets the current temperature to a
random number between 18 and 26. The `run_sentinel` is a `threading.Event` object that
every accessory has and it can be used to gracefully notify the accessory that it should
stop running.


In some cases the run method can suitable be skipped, e.g. see [LightBulb](pyhap/accessories/LightBulb.py).

### 3. Specify how to stop
When the accessory is stopped by the accessory driver, it first sets the `run_sentinel`
and then calls `Accessory.stop()`. This is your chance to clean up any resources you like,
e.g. files, sockets, gpios, etc.

## Installation

To install HAP-python, git clone this project and do:

```
python3 setup.py install
```

This will install HAP-python in your python packages, so that you can import it as pyhap.
Alternatively, you can install only the dependencies and run with the root directory of this project in your `PYTHONPATH`.

To uninstall, just do:

```
pip3 uninstall HAP-python
```

## Acknowledgements

This project would have been possible without the work done on [HAP-NodeJS by KhaosT](https://github.com/KhaosT/HAP-NodeJS).

## Notice

The characteristics and services that are supported by HomeKit may not all be present in the [resources folder](pyhap/resources).
Also, there are some missing parts, like default values for characteristics.

Lastly, I am not aware of any bugs, but I am more than confident that such exist.

Suggestions are always welcome.

Have fun!
