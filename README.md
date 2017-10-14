HAP-python
==========
HomeKit Accessory Protocol implementation in python.

With this project, you can create HomeKit accessories in python.

The project was developed for a Raspberry Pi, but it should work on other platforms.

To kick-start things, you can open `main.py`, where you can find out how to launch a mock temperature sensor. To start, run

```
python3 main.py
```

and you should see it in the Home application (be sure to be in the same network).

There are example accessories for some sensors in [the accessories folder](pyhap/accessories) (e.g. AM2302 temperature and humidity sensor).

Installation
============
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

Acknowledgements
================
This project would have been possible without the work done on [HAP-NodeJS by KhaosT](https://github.com/KhaosT/HAP-NodeJS).

Notice
======
The characteristics and services that are supported by HomeKit may not all be present in the [resources folder](pyhap/resources).
Also, there are some missing parts, like default values for characteristics.

Lastly, I am not aware of any bugs, but I am more than confident that such exist.

Suggestions are always welcome.

Have fun!
