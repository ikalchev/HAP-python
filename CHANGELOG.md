# Changelog

All notable changes to this project will be documented in this file (since version `1.1.8`).
If you notice that something is missing, please open an issue or submit a PR.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/).

<!--
Sections
### Added
### Changed
### Deprecated
### Removed
### Fixed
### Breaking Changes
### Developers
-->

## [4.9.1] - 2023-10-25

- Fix handling of explict close. [#467](https://github.com/ikalchev/HAP-python/pull/467)

## [4.9.0] - 2023-10-15

- Hashing of accessories no longer includes their values, resulting in more reliable syncs between
  devices. [#464](https://github.com/ikalchev/HAP-python/pull/464)

## [4.8.0] - 2023-10-06

- Add AccessoryInformation:HardwareFinish and NFCAccess characteristics/services.
  [#454](https://github.com/ikalchev/HAP-python/pull/454)
- Fix handling of multiple pairings. [#456](https://github.com/ikalchev/HAP-python/pull/456)
- Save raw client username bytes if they are missing on successful pair verify.[#458](https://github.com/ikalchev/HAP-python/pull/458)
- Add support for Write Responses. [#459](https://github.com/ikalchev/HAP-python/pull/459)
- Ensure tasks are not garbage-collected before they finish. [#460](https://github.com/ikalchev/HAP-python/pull/460)

## [4.7.1] - 2023-07-31

- Improve encryption performance. [#448](https://github.com/ikalchev/HAP-python/pull/448)
- Switch timeouts to use `async_timeout`. [#447](https://github.com/ikalchev/HAP-python/pull/447)

## [4.7.0] - 2023-06-18

- Allow passing multiple ip to advertise on to AccessoryDriver. [#442](https://github.com/ikalchev/HAP-python/pull/442)
- Fix for the new home architecture - retain the original format of the UUID. [#441](https://github.com/ikalchev/HAP-python/pull/441)
- Add python 3.11 to the CI. [#440](https://github.com/ikalchev/HAP-python/pull/440)
- Use orjson.loads in loader to speed up startup. [#436](https://github.com/ikalchev/HAP-python/pull/436)

## [4.6.0] - 2022-12-10

- Patch for [WinError 5] Access Denied. [#421](https://github.com/ikalchev/HAP-python/pull/421)
- Add support for a custom iid manager. [#423](https://github.com/ikalchev/HAP-python/pull/423)
- Fix pairing with iOS 16. [#424](https://github.com/ikalchev/HAP-python/pull/424)
- Fix error logging when `get_characteristics` fails. [#425](https://github.com/ikalchev/HAP-python/pull/425)
- Add necessary support for Adaptive Lightning. [#428](https://github.com/ikalchev/HAP-python/pull/428)

## [4.5.0] - 2022-06-28

- Speed up "get accessories". [#418](https://github.com/ikalchev/HAP-python/pull/418)
- Increase minimum python version to 3.7. [#417](https://github.com/ikalchev/HAP-python/pull/417)
- Speed up encryption by using ChaCha20Poly1305Reusable. [#413](https://github.com/ikalchev/HAP-python/pull/413)
- Speed up serialization using orjson. [#412](https://github.com/ikalchev/HAP-python/pull/412)
- Avoid redundant parsing of the URL. [#402](https://github.com/ikalchev/HAP-python/pull/402)

## [4.4.0] - 2022-11-01

### Added
- Allow invalid client values when enabled. [#392](https://github.com/ikalchev/HAP-python/pull/392)

## [4.3.0] - 2021-10-07

### Fixed
- Only send the latest state in case of multiple events for the same characteristic. [#385](https://github.com/ikalchev/HAP-python/pull/385)
- Handle invalid formats from clients. [#387](https://github.com/ikalchev/HAP-python/pull/387)

## [4.2.1] - 2021-09-06

### Fixed
- Fix floating point values with minStep. [#382](https://github.com/ikalchev/HAP-python/pull/382)

## [4.2.0] - 2021-09-04

### Changed
- Bump zeroconf to 0.36.2. [#380](https://github.com/ikalchev/HAP-python/pull/380)

### Fixed
- Handle additional cases of invalid values. [#378](https://github.com/ikalchev/HAP-python/pull/378)

### Added
- Allow passing the zeroconf server name when creating the AccessoryDriver. [#379](https://github.com/ikalchev/HAP-python/pull/379)

## [4.1.0] - 2021-08-22

### Added
- Add support for saving permissions when pairing. [#372](https://github.com/ikalchev/HAP-python/pull/372)
- Add accessory-level callbacks. [#373](https://github.com/ikalchev/HAP-python/pull/373)

### Changed
- Increment the config version when the accessory changes. [#376](https://github.com/ikalchev/HAP-python/pull/376)

## [4.0.0] - 2021-07-22

- Add support for HAP v 1.1. [#365](https://github.com/ikalchev/HAP-python/pull/365)

## [3.6.0] - 2021-07-22

- Reduce event overhead. [#360](https://github.com/ikalchev/HAP-python/pull/360)
- Ensure floating point values are truncated for int formats. [#361](https://github.com/ikalchev/HAP-python/pull/361)
- Remove python 3.10 alpha from ci workflow. [#362](https://github.com/ikalchev/HAP-python/pull/362)
- Protocol 1.1: Add support for prepared writes. [#366](https://github.com/ikalchev/HAP-python/pull/366)
- Decrease snapshot timeout to avoid being disconnected. [#367](https://github.com/ikalchev/HAP-python/pull/367)
- Avoid writing delayed camera snapshots when the connection is closed. [#368](https://github.com/ikalchev/HAP-python/pull/368)

## [3.5.2] - 2021-07-22

- Switch from ed25519 to pynacl. [#355](https://github.com/ikalchev/HAP-python/pull/355)

## [3.5.1] - 2021-07-04

# Changed
- Bumped zeroconf to 0.32. [#351](https://github.com/ikalchev/HAP-python/pull/351)

# Fixed
- Handle additional cases of invalid hostnames. [#348](https://github.com/ikalchev/HAP-python/pull/348)

# Breaking Changes
- Python 3.5 is no longer supported. [#354](https://github.com/ikalchev/HAP-python/pull/354)

## [3.5.0] - 2021-05-31

# Changed
- Add async registration for zeroconf. [#342](https://github.com/ikalchev/HAP-python/pull/342)
- Reduce payload sizes by adding support for short UUIDs and compact json (~40% reduction). [#345](https://github.com/ikalchev/HAP-python/pull/345)

# Fixed
- Coalesce events when possible. [#346](https://github.com/ikalchev/HAP-python/pull/346)
- Remove watcher from Windows. [#343](https://github.com/ikalchev/HAP-python/pull/343)

## [3.4.1] - 2021-03-28

# Fixed
- Fix `run_at_interval` with multiple accessories. [#335](https://github.com/ikalchev/HAP-python/pull/335)
- Ensure HTTP 200 status is sent when there are no failures for `get_characteristics`. Improves battery life. [#337](https://github.com/ikalchev/HAP-python/pull/337)

## [3.4.0] - 2021-03-06

### Added
- Python 3.10 support. [#328](https://github.com/ikalchev/HAP-python/pull/328)

### Fixed
- Improve connection stability with large responses. [#320](https://github.com/ikalchev/HAP-python/pull/320)
- Fix `Accessroy.run` not being awaited from a bridge. [#323](https://github.com/ikalchev/HAP-python/pull/323)
- Clean up event subscriptions on client disconnect. [#324](https://github.com/ikalchev/HAP-python/pull/324)

### Removed
- Remove legacy python code. [#321](https://github.com/ikalchev/HAP-python/pull/321)
- Remove deprecated `get_char_loader` and `get_serv_loader`. [#322](https://github.com/ikalchev/HAP-python/pull/322)

### Developers
- Increase code coverage. [#325](https://github.com/ikalchev/HAP-python/pull/325), [#326](https://github.com/ikalchev/HAP-python/pull/326), [#330](https://github.com/ikalchev/HAP-python/pull/330), [#331](https://github.com/ikalchev/HAP-python/pull/331), [#332](https://github.com/ikalchev/HAP-python/pull/332)
- Add bandit to CI. [#329](https://github.com/ikalchev/HAP-python/pull/329)


## [3.3.2] - 2021-03-01

### Fixed
- Resolve unavailable condition on restart. [#318](https://github.com/ikalchev/HAP-python/pull/318)
- Resolve config version overflow. [#318](https://github.com/ikalchev/HAP-python/pull/318)

## [3.3.1] - 2021-02-28

### Changed
- Implement partial success response for `set_characteristics` (was `BAD_REQUEST` on error). [#316](https://github.com/ikalchev/HAP-python/pull/316) 

## [3.3.0] - 2021-02-13

### Fixed
- Fix an issue that would cause pairing to fail (implement `list pairings`). [#307](https://github.com/ikalchev/HAP-python/pull/307)
- Remove unsupported characters from Accessory names. [#310](https://github.com/ikalchev/HAP-python/pull/310)
- Speed up event subscription for new connections. [#308](https://github.com/ikalchev/HAP-python/pull/308)
- Properly handle camera snapshots. [#311](https://github.com/ikalchev/HAP-python/pull/311)
- Properly handle pairing attempt when already paired. [#314](https://github.com/ikalchev/HAP-python/pull/314)

### Changed
- Use github actions for codecov. [#312](https://github.com/ikalchev/HAP-python/pull/312), [#313](https://github.com/ikalchev/HAP-python/pull/313)

## [3.2.0] - 2021-01-31

### Changed
- HTTP server is now based on asyncio. [#301](https://github.com/ikalchev/HAP-python/pull/301)

### Fixed
- Fix a bug in the pairing URL generator. [#303](https://github.com/ikalchev/HAP-python/pull/303)

## [3.1.0] - 2020-12-13

### Fixed
- Ensure an error response is generated on exception. [#292](https://github.com/ikalchev/HAP-python/pull/292)
- Improve error reporting during pairing. [#289](https://github.com/ikalchev/HAP-python/pull/289)
- Handle request for an empty read instead of throwing an exception. [#288](https://github.com/ikalchev/HAP-python/pull/288)
- Fix thread safety in get characteristics. [#287](https://github.com/ikalchev/HAP-python/pull/287) 

## [3.0.0] - 2020-07-25

### Added
- Support for multiple camera streams. [#273](https://github.com/ikalchev/HAP-python/pull/273)

### Changed
- Use SimpleQueue instead of Queue when available (performance improvements). [#274](https://github.com/ikalchev/HAP-python/pull/274)

### Fixed
- Make sure accessory setup code appears when running under systemd. [#276](https://github.com/ikalchev/HAP-python/pull/276)

## [2.9.2] - 2020-07-05

### Added
- Improve event loop handling. [#270](https://github.com/ikalchev/HAP-python/pull/270)
- Auto-detect the IP address in the camera demo so it can work out of the box. [#268](https://github.com/ikalchev/HAP-python/pull/268)

### Fixed
- Correctly handling of a single byte read request. [#267](https://github.com/ikalchev/HAP-python/pull/267)

## [2.9.1] - 2020-05-31

### Added
- Add compatibility with zeroconf 0.27. [#263](https://github.com/ikalchev/HAP-python/pull/263)

## [2.9.0] - 2020-05-29

### Fixed
- Fix random disconnect after upgrade to encrypted. [#253](https://github.com/ikalchev/HAP-python/pull/253)
- Convert the characteristic UUID to string only once. [#256](https://github.com/ikalchev/HAP-python/pull/256)
- Fix pairing failure - split read/write encryption upgrade. [#258](https://github.com/ikalchev/HAP-python/pull/258)
- Allow negotiated framerate to be used - add "-framerate" parameterto avfoundation. [#260](https://github.com/ikalchev/HAP-python/pull/260)

### Added
- Add support for unavailable accessories. [#252](https://github.com/ikalchev/HAP-python/pull/252)

### Developers
- Cleanup and fixes for python 3.7 and 3.8. Enable pylint in Travis. [#255](https://github.com/ikalchev/HAP-python/pull/255)

## [2.8.4] - 2020-05-12

### Fixed
- Fix race condition that causes pairing and unpairing failures. [#246](https://github.com/ikalchev/HAP-python/pull/246)
- Fix loop on dropped connections that causes temporary stalls and connection loss. [#249](https://github.com/ikalchev/HAP-python/pull/249)
- Fix exception on missing video fields. [#245](https://github.com/ikalchev/HAP-python/pull/245)

## [2.8.3] - 2020-05-01

### Fixed
- Fix exception caused by wrong parameter encoding to tlv.encode in camera.py. [#243](https://github.com/ikalchev/HAP-python/pull/243)
- Log exceptions when handling a request in the HTTP server. [#241](https://github.com/ikalchev/HAP-python/pull/241)

## [2.8.2] - 2020-04-10

### Added
- Add an option to select the zeroconf broadcast interface. [#239](https://github.com/ikalchev/HAP-python/pull/239)
- Allow service callbacks to handle multiple AIDs. [#237](https://github.com/ikalchev/HAP-python/pull/237)

## [2.8.1] - 2020-04-06

### Fixed
- Fix an issue where reading just one byte at the beginning of a block can crash the connection. [#235](https://github.com/ikalchev/HAP-python/pull/235)
- Improve camera accessory integration. [#231](https://github.com/ikalchev/HAP-python/pull/231)

## [2.8.0] - 2020-04-02

### Added
- Add support for service-level callbacks. You can now register a callback that will be called for all characteristics that belong to it. [#229](https://github.com/ikalchev/HAP-python/pull/229)

### Fixed
-  - Switch the symmetric cipher to use the cryptography module. This greatly improves performance. [#232](https://github.com/ikalchev/HAP-python/pull/232)

## [2.7.0] - 2020-01-26

### Added
- Example Accessory that exposes the raspberry pi GPIO pins as a relay. [#220](https://github.com/ikalchev/HAP-python/pull/220)

### Fixed
- The HAP server is now HTTP version 1.1. [#216](https://github.com/ikalchev/HAP-python/pull/216)
- Fixed an issue where accessories on the server can appear non-responsive. [#216](https://github.com/ikalchev/HAP-python/pull/216)
- Correctly end HAP responses in some error cases. [#217](https://github.com/ikalchev/HAP-python/pull/217)
- Fixed an issue where an accessory can appear as non-responsive after an event. Events for value updates will not be sent to the client that initiated them. [#215](https://github.com/ikalchev/HAP-python/pull/215)

## [2.6.0] - 2019-09-21

### Added
- The `AccessoryDriver` can now advertise on a different address than the one the server is running on. This is useful when pyhap is running behind a NAT. [#203](https://github.com/ikalchev/HAP-python/pull/203)

## [2.5.0] - 2019-04-10

### Added
- Added support for Television accessories.

## [2.4.2] - 2019-01-04

### Fixed
- Fixed an issue where stopping the `AccessoryDriver` can fail with `RuntimeError('dictionary changed size during iteration')`.
- Fixed an issue where the `HAPServer` can crash when sending events to clients.

### Added
- Tests for `hap_server`.

## [2.4.1] - 2018-11-11

### Fixed
- Correctly proxy `_io_refs` to the socket. [#145](https://github.com/ikalchev/HAP-python/issues/145)


## [2.4.0] - 2018-11-10

### Added
- Added a `asyncio.SafeChildWatcher` as part of `AccessoryDriver.start` if started in the main thread.
- Added `Camera.stop`, which terminates all streaming processes.
- `AccessoryDriver.safe_mode` parameter. Set with `driver.safe_mode = True` before `driver.start` to disable `update_advertisement` call for `pair` and `unpair`. After unpairing a restart is necessary. [#168](https://github.com/ikalchev/HAP-python/pull/168)

### Changed
- The default implementations of the `Camera`'s `start_stream`, `stop_stream` and
`reconfigure_stream` are now async.
- The streaming process is started with `asyncio.create_subprocess_exec` instead of
`subprocess.Popen`
- Moved most of the metadata from `setup.py` to `setup.cfg`. Added long description.

### Fixed
- `AccessoryDriver.add_job` now correctly schedules coroutines wrapped in functools.partial.
- Fixed the slow shutdown in python 3.7, which was caused by the changed
behavior of `ThreadingMixIn.server_close`.
- Fixed an issue where sockets are blocked on `recv` while in `CLOSE_WAIT` state, which
can eventually exhausts the limit of open sockets. [#145](https://github.com/ikalchev/HAP-python/issues/145)

### Reverted
- `Char.client_update_value` no longer ignores duplicate values. Reverts [#162](https://github.com/ikalchev/HAP-python/pull/162). [#166](https://github.com/ikalchev/HAP-python/pull/166)


## [2.3.0] - 2018-10-25

### Added
- Added support for the camera accessory. [#161](https://github.com/ikalchev/HAP-python/pull/161)
- Added a demo script that starts four fake accessories.
- Added `NeoPixelsLightStrip` accessory. [#144](https://github.com/ikalchev/HAP-python/pull/144)
- Added new Accessory categories. [Commit](https://github.com/ikalchev/HAP-python/commit/dbf1d5d8fea814af52098f3a2f3cd5a47cd889c1)

### Changed
- Updated the README with information on how to setup a camera accessory.
- Spelling fix - executor (accessory_driver). [#159](https://github.com/ikalchev/HAP-python/pull/159)
- Char.client_update_value now ignores call if value is already set. This could happen during automations. [#162](https://github.com/ikalchev/HAP-python/pull/162)

### Fixed
- Updated README. [#138](https://github.com/ikalchev/HAP-python/pull/138)
- Accessory return codes for characteristics. [#143](https://github.com/ikalchev/HAP-python/pull/143)
- Deprecation notice in `accessory.py/config_changed`. [#150](https://github.com/ikalchev/HAP-python/pull/150)
- Add end_response in set_characteristics. [#153](https://github.com/ikalchev/HAP-python/pull/153)



## [2.2.2] - 2018-05-29

### Fixed
- Spelling mistake in `setup.cfg` that caused broken builds. [#130](https://github.com/ikalchev/HAP-python/pull/130)



## [2.2.1] - 2018-05-29 - Broken

### Fixed
- Package data is now included again. [#128](https://github.com/ikalchev/HAP-python/pull/128)



## [2.2.0] - 2018-05-26 - Broken

### Added
- Option to pass custom loader object to `driver` with parameter `loader`. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- Default port for `accessory_driver.port = 51234`. [#105](https://github.com/ikalchev/HAP-python/pull/105)

### Changed
- The `loader` object is now stored in the `driver` and can be accessed through `driver.loader`. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- Use the `Accessory.run_at_interval` decorator for the `run` method, instead of `while True: sleep(x); do_stuff()`. [#124](https://github.com/ikalchev/HAP-python/pull/124)

### Breaking Changes
- The `driver` doesn't take the top `accessory` anymore. Instead it's added through `driver.add_accessory()` after the initialization. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- All `driver` init parameter are now required to be passed as keywords. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- Any `accessory` needs the `driver` object for its initialization, passed as first argument. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- Removed class `AsyncAccessory`. All of its methods are now fully integrated into the `Accessory` class. `run`, `stop` can be either normal or async methods and `run_at_interval` works with both as well. [#124](https://github.com/ikalchev/HAP-python/pull/124)

### Developers
- Removed `acc.set_driver()` and `acc.set_sentinel()` methods. `acc.run_sentinel`, `acc.aio_stop_event` and `acc.loop` are now accessed through `acc.driver.xxx`. `run_sentinel` is changed to `stop_event`. [#105](https://github.com/ikalchev/HAP-python/pull/105)
- Added scripts for `setup` and `release`.  [#125](https://github.com/ikalchev/HAP-python/pull/125)
- Added `async` helper methods and restructured `start` and `stop` methods for `async` conversion. [#124](https://github.com/ikalchev/HAP-python/pull/124)



## [2.1.0] - 2018-05-18

### Added
- Added `getter_callback` to Characteristics. [#90](https://github.com/ikalchev/HAP-python/pull/90)
- The `pincode` can now be assigned as a parameter for the driver. [#120](https://github.com/ikalchev/HAP-python/pull/120)

### Changed
- Improved documentation for version `2.0.0`. [#114](https://github.com/ikalchev/HAP-python/pull/114)

### Deprecated
- The `accessory` and `bridge` parameter `mac` and `pincode` are now deprecated. [#120](https://github.com/ikalchev/HAP-python/pull/120)
- `Accessory.config_changed`, use `driver.config_changed` instead. [#120](https://github.com/ikalchev/HAP-python/pull/120)
- `Accessory.paired`, use `driver.state.paired` instead. [#120](https://github.com/ikalchev/HAP-python/pull/120)

### Fixed
- Typo in log message in `accessory_driver.stop`. [#112](https://github.com/ikalchev/HAP-python/pull/112)

### Breaking Changes
- Moved all accessories from `pyhap.accessories` to an `accessories` folder at the root of the project. [#115](https://github.com/ikalchev/HAP-python/pull/115)
- Removed unused method `accessory.create`. [#117](https://github.com/ikalchev/HAP-python/pull/117)
- Removed `iid_manager` and `setup_id` parameter from `accessory` and `bridge` `init` calls. [#117](https://github.com/ikalchev/HAP-python/pull/117)

### Developers
- The `driver` event loop name changed from `event_loop` to `loop`. [#107](https://github.com/ikalchev/HAP-python/pull/107)
- `pyhap.accessories` is now a native namespace package. See `pyhap/accessories/README.md` for details on how to integrate third party Accessories. [#115](https://github.com/ikalchev/HAP-python/pull/115)
- Added static code checks. To run them locally use `tox -e lint` and `tox -e pylint`. [#118](https://github.com/ikalchev/HAP-python/pull/118)
- Added `State` helper class to keep track of (semi-)static information. [#120](https://github.com/ikalchev/HAP-python/pull/120)
- Variables that are related to pairing and storing static information have been moved to `driver.state`. That includes from `accessory`: `config_version`, `mac`, `setup_id`, `private_key`, `public_key` and `paired_clients` as well as the `add_paired_client` and `removed_paired_client` methods. For `accessory_driver`: `address` and `port`. [#120](https://github.com/ikalchev/HAP-python/pull/120)



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
