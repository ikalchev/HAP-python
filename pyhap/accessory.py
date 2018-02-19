import threading
import logging
import itertools
import struct
from os import urandom

import ed25519
import base36
from pyqrcode import QRCode

import pyhap.util as util
from pyhap.loader import get_serv_loader

logger = logging.getLogger(__name__)


class Category:
    """Known category values.

    Category is a hint to iOS clients about what "type" of Accessory this represents,
    for UI only.
    """
    OTHER = 1
    BRIDGE = 2
    FAN = 3
    GARAGE_DOOR_OPENER = 4
    LIGHTBULB = 5
    DOOR_LOCK = 6
    OUTLET = 7
    SWITCH = 8
    THERMOSTAT = 9
    SENSOR = 10
    ALARM_SYSTEM = 11
    DOOR = 12
    WINDOW = 13
    WINDOW_COVERING = 14
    PROGRAMMABLE_SWITCH = 15
    RANGE_EXTENDER = 16
    CAMERA = 17


# Standalone accessory ID (i.e. not bridged)
STANDALONE_AID = 1


class IIDManager(object):
    """Maintains a mapping between Service/Characteristic objects and IIDs."""

    def __init__(self):
        """Initialise an empty instance."""
        self.iids = {}
        self.reverse_iids = {}

    def assign(self, obj):
        """Assign an IID to the given object.

        If the object already has an assigned ID, log a warning and do nothing.

        :param obj: The object that will be assigned an IID.
        :type obj: Service or Characteristic
        """
        if obj in self.reverse_iids:
            logger.warning("The given Service or Characteristic with UUID %s "
                           "already has an assigned IID %s, ignoring.",
                           obj.type_id, self.reverse_iids[obj])
            return
        iid = len(self.iids) + 1
        self.iids[iid] = obj
        self.reverse_iids[obj] = iid

    def remove(self, obj=None, iid=None):
        """Remove an object or an object with the given IID."""
        if obj is not None:
            iid = self.reverse_iids.pop(obj, None)
            if iid is None:
                logger.error("Object %s not found.", obj)
                return
            del self.iids[iid]
        else:
            obj = self.iids.pop(iid, None)
            if obj is None:
                logger.error("IID %s not found.", iid)
                return
            del self.reverse_iids[obj]

    def get_iid(self, obj):
        """Get the IID assigned to the given object.

        :return: IID assigned to the given object or None if the object is not found.
        :rtype: int
        """
        return self.reverse_iids.get(obj)

    def get_obj(self, iid):
        """Get the object that is assigned the given IID.

        :return: The object with the given IID or None if no object has that IID.
        :rtype: Service or Characteristic
        """
        return self.iids.get(iid)


class Accessory(object):
    """A representation of a HAP accessory.

    Inherit from this class to build your own accessories.

    At the end of the init of this class, the _set_services method is called.
    Use this to set your HAP services.
    """

    category = Category.OTHER

    @classmethod
    def create(cls, display_name, pincode, aid=STANDALONE_AID):
        mac = util.generate_mac()
        return cls(display_name, aid=aid, mac=mac, pincode=pincode)

    def __init__(self, display_name, aid=None, mac=None, pincode=None,
                 iid_manager=None, setup_id=None):
        """Initialise with the given properties.

        :param display_name: Name to be displayed in the Home app.
        :type display_name: str

        :param aid: The accessory ID, uniquely identifying this accessory.
            `Accessories` that advertised on the network must have the
            standalone AID. Defaults to None, in which case the `AccessoryDriver`
            will assign the standalone AID to this `Accessory`.
        :type aid: int

        :param mac: The MAC address of this `Accessory`, needed by HAP clients.
            Defaults to None, in which case the `AccessoryDriver`
            will assign a random MAC address to this `Accessory`.
        :type mac: str

        :param pincode: The pincode that HAP clients must prove they know in order
            to pair with this `Accessory`. Defaults to None, in which case a random
            pincode is generated. The pincode has the format "xxx-xx-xxx", where x is
            a digit.
        :type pincode: bytearray

        :param setup_id: Setup ID can be provided, although, per spec, should be random
            every time the instance is started. If not provided on init, will be random.
            4 digit string 0-9 A-Z
        :type setup_id: str
        """
        self.display_name = display_name
        self.aid = aid
        self.mac = mac
        self.config_version = 2
        self.reachable = True
        self._pincode = pincode
        self._setup_id = setup_id
        self.broker = None
        # threading.Event that gets set when the Accessory should stop.
        self.run_sentinel = None

        sk, vk = ed25519.create_keypair()
        self.private_key = sk
        self.public_key = vk
        self.paired_clients = {}
        self.services = []
        self.iid_manager = iid_manager or IIDManager()

        self._set_services()

    def __getstate__(self):
        state = self.__dict__.copy()
        state["broker"] = None
        state["run_sentinel"] = None
        return state

    @property
    def setup_id(self):
        if not getattr(self, '_setup_id', None):
            self._setup_id = util.generate_setup_id()
        return self._setup_id

    @property
    def pincode(self):
        if not getattr(self, '_pincode', None):
            self._pincode = util.generate_pincode()
        return self._pincode

    def _set_services(self):
        """Sets the services for this accessory.

        The default implementation adds only the AccessoryInformation services
        and sets its Name characteristic to the Accessory's display name.

        .. note:: When inheriting from Accessory and overriding this method,
            always call the base implementation first, as it reserves IID of
            1 for the Accessory Information service (HAP requirement).
        """
        info_service = get_serv_loader().get("AccessoryInformation")
        info_service.get_characteristic("Name")\
                    .set_value(self.display_name, False)
        info_service.get_characteristic("Manufacturer")\
                    .set_value("Default-Manufacturer", False)
        info_service.get_characteristic("Model")\
                    .set_value("Default-Model", False)
        info_service.get_characteristic("SerialNumber")\
                    .set_value("Default-SerialNumber", False)
        # FIXME: Need to ensure AccessoryInformation is with IID 1.
        self.add_service(info_service)

    def set_sentinel(self, run_sentinel):
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

    def config_changed(self):
        """Notify the accessory about configuration changes.

        These include new services or updated characteristic values, e.g.
        the Name of a service changed.

        This method also notifies the broker about the change, so that it can
        publish the changes to the world.

        .. note:: If you are changing the configuration of a bridged accessory
           (i.e. an Accessory that is contained in a Bridge),
           you should call the `config_changed` method on the Bridge.

        """
        self.config_version += 1
        self.broker.config_changed()

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
            for c in s.characteristics + s.opt_characteristics:
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

    def set_broker(self, broker):
        self.broker = broker

    def add_paired_client(self, client_uuid, client_public):
        """Adds the given client to the set of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID

        :param client_public: The client's public key (not the session public key).
        :type client_public: bytes
        """
        self.paired_clients[client_uuid] = client_public

    def remove_paired_client(self, client_uuid):
        """Deletes the given client from the set of paired clients.

        :param client_uuid: The client's UUID.
        :type client_uuid: uuid.UUID
        """
        self.paired_clients.pop(client_uuid)

    @property
    def paired(self):
        return len(self.paired_clients) > 0

    @property
    def xhm_uri(self):
        """Generates the X-HM:// uri (Setup Code URI)

        :rtype: str
        """
        buffer = bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00')

        value_low = int(self.pincode.replace(b'-', b''), 10)
        value_low |= 1 << 28
        struct.pack_into('>L', buffer, 4, value_low)

        if self.category == Category.OTHER:
            buffer[4] = buffer[4] | 1 << 7

        value_high = self.category >> 1
        struct.pack_into('>L', buffer, 0, value_high)

        encoded_payload = base36.dumps(struct.unpack_from('>L', buffer, 4)[0]
                                       + (struct.unpack_from('>L', buffer, 0)[0] * (1 << 32))).upper()
        encoded_payload = encoded_payload.rjust(9, '0')

        return 'X-HM://' + encoded_payload + self.setup_id

    @property
    def qr_code(self):
        """Generate a QR code for paring with this accessory.

        :rtype: QRCode
        """
        return QRCode(self.xhm_uri)

    def print_qr(self):
        """Print the setup code in QR format to console.
        """
        print(self.qr_code.terminal(), flush=True)

    def get_characteristic(self, aid, iid):
        """Get's the characteristic for the given IID.

        The AID isused to verify if the search is in the correct accessory.
        """
        if aid != self.aid:
            return None

        return self.iid_manager.get_obj(iid)

    def to_HAP(self, iid_manager=None):
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
        iid_manager = iid_manager or self.iid_manager
        services_HAP = [s.to_HAP(iid_manager) for s in self.services]
        hap_rep = {"aid": self.aid, "services": services_HAP, }
        return hap_rep

    def setup_message(self):
        print('Setup payload: %s' % self.xhm_uri, flush=True)
        print('Scan this code with your HomeKit app on your iOS device:', flush=True)
        self.print_qr()
        print('Or enter this code in your HomeKit app on your iOS device: %s' % self.pincode.decode())

    def run(self):
        """Called when the Accessory should start doing its thing.

        Called when HAP server is running, advertising is set, etc.
        """
        pass

    def stop(self):
        """Called when the Accessory should stop what is doing and clean up any resources.
        """
        pass

    # Broker

    def publish(self, data, sender):
        """Append AID and IID of the sender and forward it to the broker.

        Characteristics call this method to send updates.

        .. note:: The method will not fail if the broker is not set - it will do nothing.

        :param data: Data to publish, usually from a Characteristic.
        :type data: dict

        :param sender: The Service or Characteristic from which the call originated.
        :type: Service or Characteristic
        """
        if self.broker is None:
            return

        acc_data = {
            "aid": self.aid,
            "iid": self.iid_manager.get_iid(sender),
            "value": data["value"],
        }
        self.broker.publish(acc_data)


class Bridge(Accessory):
    """A representation of a HAP bridge.

    A `Bridge` can have multiple `Accessories`.
    """

    category = Category.BRIDGE

    def __init__(self, display_name, mac=None, pincode=None,
                 iid_manager=None, setup_id=None):
        aid = STANDALONE_AID
        # A Bridge cannot be Bridge, hence talks directly to HAP clients.
        # Thus, we need a mac.
        mac = mac or util.generate_mac()
        super(Bridge, self).__init__(display_name, aid=aid, mac=mac,
                                     pincode=pincode, iid_manager=iid_manager,
                                     setup_id=setup_id)
        self.accessories = {}  # aid: acc

    def _set_services(self):
        """Call the base method and add the BridgingState Service."""
        super(Bridge, self)._set_services()
        self.add_service(
            get_serv_loader().get("BridgingState"))

    def set_sentinel(self, run_sentinel):
        """Set the same sentinel to all contained accessories."""
        super(Bridge, self).set_sentinel(run_sentinel)
        for acc in self.accessories.values():
            acc.set_sentinel(run_sentinel)

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

        :raise ValueError: When the given ``Accessory`` is of category ``Category.BRIDGE``
            or if the AID of the ``Accessory`` clashes with another ``Accessory`` already in this
            ``Bridge``.
        """
        if acc.category == Category.BRIDGE:
            raise ValueError("Bridges cannot be bridged")

        if acc.aid is None:
            acc.aid = next(aid for aid in itertools.count(2)
                           if aid not in self.accessories)
        elif acc.aid == self.aid or acc.aid in self.accessories:
            raise ValueError("Duplicate AID found when attempting to add accessory")

        self.accessories[acc.aid] = acc

    def set_broker(self, broker):
        super(Bridge, self).set_broker(broker)
        for _, acc in self.accessories.items():
            acc.broker = broker

    def to_HAP(self, iid_manager=None):
        """Returns a HAP representation of itself and all contained accessories.

        .. seealso:: Accessory.to_HAP
        """
        hap_rep = [super(Bridge, self).to_HAP(iid_manager), ]

        for _, acc in self.accessories.items():
            hap_rep.append(acc.to_HAP(iid_manager))

        return hap_rep

    def get_characteristic(self, aid, iid):
        """.. seealso:: Accessory.to_HAP
        """
        if self.aid == aid:
            return self.iid_manager.get_obj(iid)

        acc = self.accessories.get(aid)
        if acc is None:
            return None

        return acc.get_characteristic(aid, iid)

    def run(self):
        """Creates and starts a new thread for each of the contained accessories' run
            method.
        """
        for acc in self.accessories.values():
            threading.Thread(target=acc.run).start()

    def stop(self):
        """Calls stop() on all contained accessories."""
        super(Bridge, self).stop()
        for acc in self.accessories.values():
            acc.stop()


def get_topic(aid, iid):
    return str(aid) + "." + str(iid)
