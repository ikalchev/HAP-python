# Changelog

All notable changes to this project will be documented in this file (since version `1.1.8`).  
If you notice that something is missing, please open an issue or submit a PR.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/).


## [Unreleased]

### Changed
- Improved documentation for version `2.0.0`. [#114](https://github.com/ikalchev/HAP-python/pull/114)

### Fixed
- Typo in log message in `accessory_driver.stop`. [#112](https://github.com/ikalchev/HAP-python/pull/112)

### Breaking Changes
- Removed unused method `accessory.create`. [#117](https://github.com/ikalchev/HAP-python/pull/117)
- Removed `iid_manager` and `setup_id` parameter from `accessory` and `bridge` `init` calls. [#117](https://github.com/ikalchev/HAP-python/pull/117)

### Developers
- The `driver` event loop name changed from `event_loop` to `loop`. [#107](https://github.com/ikalchev/HAP-python/pull/107)



## [2.0.0] - 2018-05-04

### Added
- New helper methods to run the `run` method repeatedly, until the driver is stopped. `Accessory.repeat(time)` or `AsyncAccessory.repeat(time)`. [#74](https://github.com/ikalchev/HAP-python/pull/74)
- New helper method `service.configure_char`. Shortcut to configuring a characteristic. [#84](https://github.com/ikalchev/HAP-python/pull/84)
- Characteristics and Services can now be created from a json dictionary with `from_dict`. [#85](https://github.com/ikalchev/HAP-python/pull/85)
- Added helper method to enable easy override of the `AccessoryInformation` service. [#102](https://github.com/ikalchev/HAP-python/pull/102)
- Added helper method to load a service and chars and add it to an accessory. [#102](https://github.com/ikalchev/HAP-python/pull/102)

### Changed
- Accessory.run method is now called through an event loop. You can either inherit from `Accessory` like before: The `run` method will be wrapped in a thread. Or inherit from `AsyncAccessory` and implement `async def run`. This will lead to the execution in the event loop. [#74](https://github.com/ikalchev/HAP-python/pull/74)
- Scripts are now located in a separate directory: `scripts`. [#81](https://github.com/ikalchev/HAP-python/pull/81)
- `driver.start` starts the event loop with `loop.run_forever()`. [#83](https://github.com/ikalchev/HAP-python/pull/83)
- Debug logs for `char.set_value` and `char.client_update_value`. [#99](https://github.com/ikalchev/HAP-python/pull/99)
- Changed default values associated with the `AccessoryInformation` service. [#102](https://github.com/ikalchev/HAP-python/pull/102)
- `Accessory._set_services` is now deprecated. Instead services should be initialized in the accessories `init` method. [#102](https://github.com/ikalchev/HAP-python/pull/102)

### Fixed
- Overriding properties now checks that value is still a valid value, otherwise value will be set to the default value. [#82](https://github.com/ikalchev/HAP-python/pull/82)
- The `AccessoryInformation` service will always have the `iid=1`. [#102](https://github.com/ikalchev/HAP-python/pull/102)

### Breaking Changes
- With introduction of async methods the min required Python version changes to `3.5`. [#74](https://github.com/ikalchev/HAP-python/pull/74)
- The `Accessory.Category` class was removed and the `Category` constants moved to `pyhap/const.py` with the naming: `CATEGORY_[OLD_NAME]` (e.g. `CATEGORY_OTHER`) [#86](https://github.com/ikalchev/HAP-python/pull/86)
- Updated `Accessories` to work with changes. [#74](https://github.com/ikalchev/HAP-python/pull/74), [#89](https://github.com/ikalchev/HAP-python/pull/89)
- Renamed `Accessory.broker` to `Accessory.Driver`. `acc.set_broker` is now `acc.set_driver`. [#104](https://github.com/ikalchev/HAP-python/pull/104)
- QR Code is now optional. It requires `pip install HAP-python[QRCode]`. [#103](https://github.com/ikalchev/HAP-python/pull/103)
- `Loader.get_serv_loader` and `Loader.get_char_loader` are replaced by `Loader.get_loader`, since it now handles loading chars and services in one class. [#108](https://github.com/ikalchev/HAP-python/pull/108)

### Developers
- `to_HAP` methods don't require the `iid_manager` any more [#84](https://github.com/ikalchev/HAP-python/pull/84), [#85](https://github.com/ikalchev/HAP-python/pull/85)
- `Service._add_chars` is now integrated in `Service.add_characteristic` [85](https://github.com/ikalchev/HAP-python/pull/85)
- `driver.update_advertisment` is now `driver.update_advertisement` [85](https://github.com/ikalchev/HAP-python/pull/85)
- `TypeLoader`, `CharLoader` and `ServiceLoader` are now combined into the `Loader` with the new methods `get_char` and `get_service` to load new chars and services. [85](https://github.com/ikalchev/HAP-python/pull/85)
- Moved some constants to `pyhap/const.py` and removed `HAP_FORMAT`, `HAP_UNITS` and `HAP_PERMISSIONS` in favor for `HAP_FORMAT_[OLD_FORMAT]`, etc. [#86](https://github.com/ikalchev/HAP-python/pull/86)
- Updated tests and added new test dependency `pytest-timeout` [#88](https://github.com/ikalchev/HAP-python/pull/88)
- Rewrote `IIDManager` and split `IIDManager.remove` into `remove_obj` and `remove_iid`. [#100](https://github.com/ikalchev/HAP-python/pull/100)
- `requirements.txt` file has been added for min, `requirements_all.txt` covers all requirements. [#103](https://github.com/ikalchev/HAP-python/pull/103)



## [1.1.9] - 2018-04-06

### Breaking Changes
- `Characteristics` are now initialized with only `display_name`, `type_id` and `properties` as parameter. Removed `value` and `broker`. [73](https://github.com/ikalchev/HAP-python/pull/73)
- Split `Characteristic.set_value` method into `set_value` and `client_update_value`. `set_value` is intended to send value updates to HomeKit, it won't call the `setter_callback` anymore. `client_update_value` is now used by the `driver` to update the `value` of the char accordingly and call `setter_callback`. It will also notify any other clients about the value change. [73](https://github.com/ikalchev/HAP-python/pull/73)

### Developers
- Removed `Characteristic.NotConfiguredError`. [73](https://github.com/ikalchev/HAP-python/pull/73)
- Updated tests. [73](https://github.com/ikalchev/HAP-python/pull/73)
- `Characteristic.to_HAP` doesn't require the `iid_manager` any more. [73](https://github.com/ikalchev/HAP-python/pull/73)
- `Characteristic.notify` doesn't check if broker is set anymore. [73](https://github.com/ikalchev/HAP-python/pull/73)
- Added helper function `Characteristic._get_default_value`. [73](https://github.com/ikalchev/HAP-python/pull/73)
- Added helper function `Characterisitc.to_valid_value`. [73](https://github.com/ikalchev/HAP-python/pull/73)



## [1.1.8] - 2018-03-29

### Added
- New method `Characteristic.override_properties`. [#66](https://github.com/ikalchev/HAP-python/pull/62)
- Added new `Apple-defined` types. Please check the [commit](https://github.com/ikalchev/HAP-python/commit/eaccedb8ba5a5a90b71584a477a19aa099e3cf8f) to see which have changed.

### Changed
- Driver calls `char.set_value` now with `should_notify=True` instead of `False` to notify other clients about the value change as well. [#62](https://github.com/ikalchev/HAP-python/pull/62)

### Fixes
- Accessories with `AID=7` stopped working [#61](https://github.com/ikalchev/HAP-python/pull/61). Don't assign it to new accessories.

### Breaking Changes
- Default value for `ValidValues` parameter is now the `valid value` with the least value. Mostly `0` or `1`. [#57](https://github.com/ikalchev/HAP-python/pull/57)
- Removed the deprecated method `char.get_value`. [#67](https://github.com/ikalchev/HAP-python/pull/67)
- Removed optional characteristics (`Service.opt_characteristics`) from the service characterization. They have been handled similar to `Service.characteristics` internally. They are still part of `pyhap/resources/services.json` however. [#67](https://github.com/ikalchev/HAP-python/pull/67)
- Updated the `Apple-defined` types. Unsupported once have been removed. Please check the [commit](https://github.com/ikalchev/HAP-python/commit/eaccedb8ba5a5a90b71584a477a19aa099e3cf8f) to see which have changed.

### Developers
- Removed `Characteristic._create_hap_template()` and merged it into `Characteristic.to_HAP`. [#66](https://github.com/ikalchev/HAP-python/pull/66)
- Removed `char.has_valid_values` and replaced it with runtime checks. [#66](https://github.com/ikalchev/HAP-python/pull/66)
- Added a `requirements_all.txt` file. [#65](https://github.com/ikalchev/HAP-python/pull/65)



## [1.1.7] - 2018-02-25

No changelog for this version has been added yet.
