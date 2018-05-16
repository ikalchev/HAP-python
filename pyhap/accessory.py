"""Module for the Accessory classes."""
import asyncio
import itertools
import logging
import struct
import threading

from pyhap import util, SUPPORT_QR_CODE
from pyhap.const import (
    STANDALONE_AID, HAP_REPR_AID, HAP_REPR_IID, HAP_REPR_SERVICES,
    HAP_REPR_VALUE, CATEGORY_OTHER, CATEGORY_BRIDGE)
from pyhap.iid_manager import IIDManager
from pyhap.loader import get_loader

if SUPPORT_QR_CODE:
    import base36
    from pyqrcode import QRCode

logger = logging.getLogger(__name__)


class Accessory:
    """A representation of a HAP accessory.

    Inherit from this class to build your own accessories.

    At the end of the init of this class, the _set_services method is called.
    Use this to set your HAP services.
    """

    category = CATEGORY_OTHER

    def __init__(self, display_name, aid=None, mac=None, pincode=None):
        """Initialise with the given properties.

        :param display_name: Name to be displayed in the Home app.
        :type display_name: str

        :param aid: The accessory ID, uniquely identifying this accessory.
            `Accessories` that advertised on the network must have the
            standalone AID. Defaults to None, in which case the `AccessoryDriver`
            will assign the standalone AID to this `Accessory`.
        :type aid: int

        :param mac: Deprecated.

        :param pincode: Deprecated.

        :param setup_id: Setup ID can be provided, although, per spec, should be random
            every time the instance is started. If not provided on init, will be random.
            4 digit string 0-9 A-Z
        :type setup_id: str
        """
        if mac or pincode:
            logger.warning(
                "The 'mac' and 'pincode' parameter are now deprecated."
                "Assign the 'pincode' to the driver instead.")
        self.display_name = display_name
        self.aid = aid
        self.mac = mac
        self.reachable = True
        self._pincode = pincode
        self.driver = None
        # threading.Event that gets set when the Accessory should stop.
        self.run_sentinel = None
        self.loop = None
        self.aio_stop_event = None

        self.services = []
        self.iid_manager = IIDManager()

        self.add_info_service()

        self._set_services()

    def __repr__(self):
        """Return the representation of the accessory."""
        services = [s.display_name for s in self.services]
        return "<accessory display_name='{}' services={}>" \
            .format(self.display_name, services)

    def __getstate__(self):
        state = self.__dict__.copy()
        state['driver'] = None
        state['run_sentinel'] = None
        return state

    def _set_services(self):
        """Set the services for this accessory.

        .. deprecated:: 2.0
           Initialize the service inside the accessory `init` method instead.
        """
        pass

    def add_info_service(self):
        """Helper method to add the required `AccessoryInformation` service.

        Called in `__init__` to be sure that it is the first service added.
        May be overridden.
        """
        serv_info = get_loader().get_service('AccessoryInformation')
        serv_info.configure_char('Name', value=self.display_name)
        serv_info.configure_char('SerialNumber', value='default')
        self.add_service(serv_info)

    def set_info_service(self, firmware_revision=None, manufacturer=None,
                         model=None, serial_number=None):
        """Quick assign basic accessory information."""
        serv_info = self.get_service('AccessoryInformation')
        if firmware_revision:
            serv_info.configure_char(
                'FirmwareRevision', value=firmware_revision)
        if manufacturer:
            serv_info.configure_char('Manufacturer', value=manufacturer)
        if model:
            serv_info.configure_char('Model', value=model)
        if serial_number:
            if len(serial_number) >= 1:
                serv_info.configure_char('SerialNumber', value=serial_number)
            else:
                logger.warning(
                    "Couldn't add SerialNumber for %s. The SerialNumber must "
                    "be at least one character long.", self.display_name)

    def add_preload_service(self, service, chars=None):
        """Create a service with the given name and add it to this acc."""
        loader = get_loader()
        service = loader.get_service(service)
        if chars:
            chars = chars if isinstance(chars, list) else [chars]
            for char_name in chars:
                char = loader.get_char(char_name)
                service.add_characteristic(char)
        self.add_service(service)
        return service

    def set_sentinel(self, run_sentinel, aio_stop_event, loop):
        """Assign a run sentinel that can signal stopping.

        The run sentinel is a threading.Event object that can be used to manage
        continuous running of the Accessory, e.g. a loop reading from a sensor every 3
        seconds. The sentinel is "set" typically by the AccessoryDriver just before
        Accessory.stop is called.

        Example usage in the run method:
        >>> while not self.run_sentinel.wait(3): # If not set, every 3 seconds
        ...    sensor.readTemperature()
        """
        self.run_sentinel = run_sentinel
        self.aio_stop_event = aio_stop_event
        self.loop = loop

    def config_changed(self):
        """Notify the accessory about configuration changes.

        These include new services or updated characteristic values, e.g.
        the Name of a service changed.

        This method also notifies the driver about the change, so that it can
        publish the changes to the world.

        .. note:: If you are changing the configuration of a bridged accessory
           (i.e. an Accessory that is contained in a Bridge),
           you should call the `config_changed` method on the Bridge.

        Deprecated. Use `driver.state_change()` instead.
        """
        logger.warning(
            'This method is now deprecated. Use \' '
            'driver.state_version\' instead.')
        self.driver.state_changed()

    def add_service(self, *servs):
        """Add the given services to this Accessory.

        This also assigns unique IIDS to the services and their Characteristics.

        .. note:: Do not add or remove characteristics from services that have been added
            to an Accessory, as this will lead to inconsistent IIDs.

        :param servs: Variable number of services to add to this Accessory.
        :type: Service
        """
        for s in servs:
            self.services.append(s)
            self.iid_manager.assign(s)
            s.broker = self
            for c in s.characteristics:
                self.iid_manager.assign(c)
                c.broker = self

    def get_service(self, name):
        """Return a Service with the given name.

        A single Service is returned even if more than one Service with the same name
        are present.

        :param name: The display_name of the Service to search for.
        :type name: str

        :return: A Service with the given name or None if no such service exists in this
            Accessory.
        :rtype: Service
        """
        return next((s for s in self.services if s.display_name == name), None)

    def set_driver(self, driver):
        self.driver = driver
        if self.mac:
            self.driver.state.mac = self.mac
        if self._pincode:
            self.driver.state.pincode = self._pincode

    def xhm_uri(self):
        """Generates the X-HM:// uri (Setup Code URI)

        :rtype: str
        """
        buffer = bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00')

        value_low = int(self.driver.state.pincode.replace(b'-', b''), 10)
        value_low |= 1 << 28
        struct.pack_into('>L', buffer, 4, value_low)

        if self.category == CATEGORY_OTHER:
            buffer[4] = buffer[4] | 1 << 7

        value_high = self.category >> 1
        struct.pack_into('>L', buffer, 0, value_high)

        encoded_payload = base36.dumps(
            struct.unpack_from('>L', buffer, 4)[0] +
            (struct.unpack_from('>L', buffer, 0)[0] * (1 << 32))).upper()
        encoded_payload = encoded_payload.rjust(9, '0')

        return 'X-HM://' + encoded_payload + self.driver.state.setup_id

    def get_characteristic(self, aid, iid):
        """Get the characteristic for the given IID.

        The AID is used to verify if the search is in the correct accessory.
        """
        if aid != self.aid:
            return None

        return self.iid_manager.get_obj(iid)

    def to_HAP(self):
        """A HAP representation of this Accessory.

        :return: A HAP representation of this accessory. For example:

        .. code-block:: python

           { "aid": 1,
               "services": [{
                   "iid" 2,
                   "type": ...,
                   ...
               }]
           }

        :rtype: dict
        """
        return {
            HAP_REPR_AID: self.aid,
            HAP_REPR_SERVICES: [s.to_HAP() for s in self.services],
        }

    def setup_message(self):
        """Print setup message to console.

        For QRCode `base36`, `pyqrcode` are required.
        Installation through `pip install HAP-python[QRCode]`
        """
        pincode = self.driver.state.pincode.decode()
        if SUPPORT_QR_CODE:
            xhm_uri = self.xhm_uri()
            print('Setup payload: {}'.format(xhm_uri), flush=True)
            print('Scan this code with your HomeKit app on your iOS device:',
                  flush=True)
            print(QRCode(xhm_uri).terminal(quiet_zone=2), flush=True)
            print('Or enter this code in your HomeKit app on your iOS device: '
                  '{}'.format(pincode))
        else:
            print('To use the QR Code feature, use \'pip install '
                  'HAP-python[QRCode]\'')
            print('Enter this code in your HomeKit app on your iOS device: {}'
                  .format(pincode))

    @staticmethod
    def run_at_interval(seconds):
        """Decorator that runs decorated method every x seconds, until stopped.

        .. code-block:: python

            @Accessory.run_at_interval(3)
            def run(self):
                print("Hello again world!")

        :param seconds: The amount of seconds to wait for the event to be set.
            Determines the interval on which the decorated method will be called.
        :type seconds: float
        """
        # decorator returns a decorator with the argument it got
        def _repeat(func):
            def _wrapper(self, *args, **kwargs):
                while not self.run_sentinel.wait(seconds):
                    func(self, *args, **kwargs)
            return _wrapper
        return _repeat

    def run(self):
        """Called when the Accessory should start doing its thing.

        Called when HAP server is running, advertising is set, etc.
        """
        pass

    def stop(self):
        """Called when the Accessory should stop what is doing and clean up any resources."""
        pass

    # Driver

    def publish(self, value, sender):
        """Append AID and IID of the sender and forward it to the driver.

        Characteristics call this method to send updates.

        .. note:: The method will not fail if the driver is not set - it will do nothing.

        :param data: Data to publish, usually from a Characteristic.
        :type data: dict

        :param sender: The Service or Characteristic from which the call originated.
        :type: Service or Characteristic
        """
        if self.driver is None:
            return

        acc_data = {
            HAP_REPR_AID: self.aid,
            HAP_REPR_IID: self.iid_manager.get_iid(sender),
            HAP_REPR_VALUE: value,
        }
        self.driver.publish(acc_data)


class AsyncAccessory(Accessory):

    @staticmethod
    def run_at_interval(seconds):
        """Decorator that runs decorated method every x seconds, until stopped.

        .. code-block:: python

            @AsyncAccessory.run_at_interval(3)
            async def run(self):
                print("Hello again world!")

        :param seconds: The amount of seconds to wait for the event to be set.
            Determines the interval on which the decorated method will be called.
        :type seconds: float
        """
        # decorator returns a decorator with the argument it got
        def _repeat(func):
            async def _wrapper(self, *args, **kwargs):
                while not await util.event_wait(self.aio_stop_event,
                                                seconds,
                                                self.loop):
                    await func(self, *args, **kwargs)
            return _wrapper
        return _repeat

    async def run(self):
        """Override in the implementation if needed."""
        pass


class Bridge(AsyncAccessory):
    """A representation of a HAP bridge.

    A `Bridge` can have multiple `Accessories`.
    """

    category = CATEGORY_BRIDGE

    def __init__(self, display_name, mac=None, pincode=None):
        """
        :param mac: Deprecated.

        :param pincode: Deprecated.
        """
        super().__init__(display_name, aid=STANDALONE_AID, mac=mac,
                         pincode=pincode)
        self.accessories = {}  # aid: acc

    def set_sentinel(self, run_sentinel, aio_stop_event, loop):
        """Set the same sentinel to all contained accessories."""
        super().set_sentinel(run_sentinel, aio_stop_event, loop)
        for acc in self.accessories.values():
            acc.set_sentinel(run_sentinel, aio_stop_event, loop)

    def add_accessory(self, acc):
        """Add the given ``Accessory`` to this ``Bridge``.

        Every ``Accessory`` in a ``Bridge`` must have an AID and this AID must be
        unique among all the ``Accessories`` in the same `Bridge`. If the given
        ``Accessory``'s AID is None, a unique AID will be assigned to it. Otherwise,
        it will be verified that the AID is not the standalone aid (``STANDALONE_AID``)
        and that there is no other ``Accessory`` already in this ``Bridge`` with that AID.

        .. note:: A ``Bridge`` cannot be added to another ``Bridge``.

        :param acc: The ``Accessory`` to be bridged.
        :type acc: Accessory

        :raise ValueError: When the given ``Accessory`` is of category ``CATEGORY_BRIDGE``
            or if the AID of the ``Accessory`` clashes with another ``Accessory`` already in this
            ``Bridge``.
        """
        if acc.category == CATEGORY_BRIDGE:
            raise ValueError("Bridges cannot be bridged")

        if acc.aid is None:
            # For some reason AID=7 gets unsupported. See issue #61
            acc.aid = next(aid for aid in itertools.count(2)
                           if aid != 7 and aid not in self.accessories)
        elif acc.aid == self.aid or acc.aid in self.accessories:
            raise ValueError("Duplicate AID found when attempting to add accessory")

        self.accessories[acc.aid] = acc

    def set_driver(self, driver):
        super().set_driver(driver)
        for _, acc in self.accessories.items():
            acc.driver = driver

    def to_HAP(self):
        """Returns a HAP representation of itself and all contained accessories.

        .. seealso:: Accessory.to_HAP
        """
        return [acc.to_HAP() for acc in (super(), *self.accessories.values())]

    def get_characteristic(self, aid, iid):
        """.. seealso:: Accessory.to_HAP"""
        if self.aid == aid:
            return self.iid_manager.get_obj(iid)

        acc = self.accessories.get(aid)
        if acc is None:
            return None

        return acc.get_characteristic(aid, iid)

    async def _wrap_in_thread(self, method):
        """Coroutine which starts the given method in a thread."""
        # Not going through loop.run_in_executor, because this thread may never
        # terminate.
        threading.Thread(target=method).start()

    async def run(self):
        """Schedule tasks for each of the accessories' run method."""
        tasks = []
        for acc in self.accessories.values():
            if isinstance(acc, AsyncAccessory):
                task = self.loop.create_task(acc.run())
            else:
                task = self.loop.create_task(self._wrap_in_thread(acc.run))
            tasks.append(task)
        await asyncio.gather(*tasks, loop=self.loop)

    def stop(self):
        """Calls stop() on all contained accessories."""
        super().stop()
        for acc in self.accessories.values():
            acc.stop()


def get_topic(aid, iid):
    return str(aid) + '.' + str(iid)
