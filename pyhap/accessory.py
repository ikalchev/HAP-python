"""Module for the Accessory classes."""

from collections.abc import Callable, Iterable
import itertools
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from . import SUPPORT_QR_CODE, util
from .characteristic import Characteristic
from .const import (
    CATEGORY_BRIDGE,
    CATEGORY_OTHER,
    HAP_PROTOCOL_VERSION,
    HAP_REPR_AID,
    HAP_REPR_IID,
    HAP_REPR_SERVICES,
    HAP_REPR_VALUE,
    STANDALONE_AID,
)
from .iid_manager import IIDManager
from .service import Service

if SUPPORT_QR_CODE:
    import base36
    from pyqrcode import QRCode


if TYPE_CHECKING:
    from .accessory_driver import AccessoryDriver


HAP_PROTOCOL_INFORMATION_SERVICE_UUID = UUID("000000A2-0000-1000-8000-0026BB765291")

logger = logging.getLogger(__name__)


class Accessory:
    """A representation of a HAP accessory.

    Inherit from this class to build your own accessories.
    """

    category = CATEGORY_OTHER

    def __init__(
        self,
        driver: "AccessoryDriver",
        display_name: str | None,
        aid: int | None = None,
        iid_manager: IIDManager | None = None,
    ) -> None:
        """Initialise with the given properties.

        :param display_name: Name to be displayed in the Home app.
        :type display_name: str

        :param aid: The accessory ID, uniquely identifying this accessory.
            `Accessories` that advertised on the network must have the
            standalone AID. Defaults to None, in which case the `AccessoryDriver`
            will assign the standalone AID to this `Accessory`.
        :type aid: int
        """
        self.aid: int | None = aid
        self.display_name: str | None = display_name
        self.driver = driver
        self.services: list[Service] = []
        self.iid_manager = iid_manager or IIDManager()
        self.setter_callback: Callable[[Any], None] | None = None

        self.add_info_service()
        if aid == STANDALONE_AID:
            self.add_protocol_version_service()

    def __repr__(self) -> str:
        """Return the representation of the accessory."""
        services = [s.display_name for s in self.services]
        return f"<accessory display_name='{self.display_name}' services={services}>"

    @property
    def available(self) -> bool:
        """Accessory is available.

        If available is False, get_characteristics will return
        SERVICE_COMMUNICATION_FAILURE for the accessory which will
        show as unavailable.

        Expected to be overridden.
        """
        return True

    def add_info_service(self) -> None:
        """Helper method to add the required `AccessoryInformation` service.

        Called in `__init__` to be sure that it is the first service added.
        May be overridden.
        """
        serv_info = self.driver.loader.get_service("AccessoryInformation")
        serv_info.configure_char("Name", value=self.display_name)
        serv_info.configure_char("SerialNumber", value="default")
        self.add_service(serv_info)

    def add_protocol_version_service(self):
        """Helper method to add the required HAP Protocol Information service"""
        serv_hap_proto_info = Service(
            HAP_PROTOCOL_INFORMATION_SERVICE_UUID, "HAPProtocolInformation"
        )
        serv_hap_proto_info.add_characteristic(self.driver.loader.get_char("Version"))
        serv_hap_proto_info.configure_char("Version", value=HAP_PROTOCOL_VERSION)
        self.add_service(serv_hap_proto_info)

    def set_info_service(
        self,
        firmware_revision: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        serial_number: str | None = None,
    ):
        """Quick assign basic accessory information."""
        serv_info = self.get_service("AccessoryInformation")
        assert serv_info is not None  # noqa: S101
        if firmware_revision:
            serv_info.configure_char("FirmwareRevision", value=firmware_revision)
        if manufacturer:
            serv_info.configure_char("Manufacturer", value=manufacturer)
        if model:
            serv_info.configure_char("Model", value=model)
        if serial_number is not None:
            if len(serial_number) >= 1:
                serv_info.configure_char("SerialNumber", value=serial_number)
            else:
                logger.warning(
                    "Couldn't add SerialNumber for %s. The SerialNumber must "
                    "be at least one character long.",
                    self.display_name,
                )

    def add_preload_service(
        self,
        service: str,
        chars: str | Iterable[str] | None = None,
        unique_id: str | None = None,
    ) -> Service:
        """Create a service with the given name and add it to this acc."""
        serv = self.driver.loader.get_service(service)
        if unique_id is not None:
            serv.unique_id = unique_id
        if chars:
            char_names = [chars] if isinstance(chars, str) else chars
            for char_name in char_names:
                char = self.driver.loader.get_char(char_name)
                serv.add_characteristic(char)
        self.add_service(serv)
        return serv

    def set_primary_service(self, primary_service: Service) -> None:
        """Set the primary service of the acc."""
        for service in self.services:
            service.is_primary_service = service.type_id == primary_service.type_id

    def add_service(self, *servs: Service) -> None:
        """Add the given services to this Accessory.

        This also assigns unique IIDS to the services and their Characteristics.

        .. note:: Do not add or remove characteristics from services that have
            been added to an Accessory, as this will lead to inconsistent IIDs.

        :param servs: Variable number of services to add to this Accessory.
        :type: Service
        """
        for s in servs:
            s.broker = self
            self.services.append(s)
            self.iid_manager.assign(s)
            for c in s.characteristics:
                c.broker = self
                self.iid_manager.assign(c)

    def get_service(self, name: str) -> Service | None:
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

    def xhm_uri(self) -> str:
        """Generates the X-HM:// uri (Setup Code URI)

        :rtype: str
        """
        payload = 0
        payload |= 0 & 0x7  # version

        payload <<= 4
        payload |= 0 & 0xF  # reserved bits

        payload <<= 8
        payload |= self.category & 0xFF  # category

        payload <<= 4
        payload |= 2 & 0xF  # flags

        payload <<= 27
        payload |= (
            int(self.driver.state.pincode.replace(b"-", b""), 10) & 0x7FFFFFFF
        )  # pincode

        encoded_payload = base36.dumps(payload).upper()
        encoded_payload = encoded_payload.rjust(9, "0")

        return "X-HM://" + encoded_payload + self.driver.state.setup_id

    def get_characteristic(self, aid: int, iid: int) -> "Characteristic | None":
        """Get the characteristic for the given IID.

        The AID is used to verify if the search is in the correct accessory.
        """
        if aid != self.aid:
            return None

        obj = self.iid_manager.get_obj(iid)
        return obj if isinstance(obj, Characteristic) else None

    def to_HAP(self, include_value: bool = True) -> dict[str, Any]:
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
            HAP_REPR_SERVICES: [
                s.to_HAP(include_value=include_value) for s in self.services
            ],
        }

    def setup_message(self):
        """Print setup message to console.

        For QRCode `base36`, `pyqrcode` are required.
        Installation through `pip install HAP-python[QRCode]`
        """
        pincode = self.driver.state.pincode.decode()
        if SUPPORT_QR_CODE:
            xhm_uri = self.xhm_uri()
            print(f"Setup payload: {xhm_uri}", flush=True)
            print(
                "Scan this code with your HomeKit app on your iOS device:", flush=True
            )
            print(QRCode(xhm_uri).terminal(quiet_zone=2), flush=True)
            print(
                f"Or enter this code in your HomeKit app on your iOS device: {pincode}",
                flush=True,
            )
        else:
            print(
                "To use the QR Code feature, use 'pip install HAP-python[QRCode]'",
                flush=True,
            )
            print(
                f"Enter this code in your HomeKit app on your iOS device: {pincode}",
                flush=True,
            )

    @staticmethod
    def run_at_interval(seconds):
        """Decorator that runs decorated method every x seconds, until stopped.

        Can be used with normal and async methods.

        .. code-block:: python

            @Accessory.run_at_interval(3)
            def run(self):
                print("Hello again world!")

        :param seconds: The amount of seconds to wait for the event to be set.
            Determines the interval on which the decorated method will be called.
        :type seconds: float
        """

        def _repeat(func):
            async def _wrapper(self, *args):
                while True:
                    await self.driver.async_add_job(func, self, *args)
                    if await util.event_wait(self.driver.aio_stop_event, seconds):
                        break

            return _wrapper

        return _repeat

    async def run(self):
        """Called when the Accessory should start doing its thing.

        Called when HAP server is running, advertising is set, etc.
        Can be overridden with a normal or async method.
        """

    async def stop(self):
        """Called when the Accessory should stop what it is doing and clean up
        any resources.

        Can be overridden with a normal or async method.
        """

    def restore_accessory_state(self) -> None:
        """Apply this accessory's entry of ``driver.state.accessory_states``.

        The driver calls this after it loads its state file, for accessories
        built before that. Does nothing by default.
        """

    def reset_accessory_state(self) -> None:
        """Forget settings made by the previous home.

        The driver calls this when the last pairing is removed. Does nothing by
        default.
        """

    # Driver

    def publish(self, value, sender, sender_client_addr=None, immediate=False):
        """Append AID and IID of the sender and forward it to the driver.

        Characteristics call this method to send updates.

        :param data: Data to publish, usually from a Characteristic.
        :type data: dict

        :param sender: The Service or Characteristic from which the call originated.
        :type: Service or Characteristic
        """
        acc_data = {
            HAP_REPR_AID: self.aid,
            HAP_REPR_IID: self.iid_manager.get_iid(sender),
            HAP_REPR_VALUE: value,
        }
        self.driver.publish(acc_data, sender_client_addr, immediate)


class Bridge(Accessory):
    """A representation of a HAP bridge.

    A `Bridge` can have multiple `Accessories`.
    """

    category = CATEGORY_BRIDGE

    def __init__(
        self,
        driver: "AccessoryDriver",
        display_name: str | None,
        iid_manager: IIDManager | None = None,
    ) -> None:
        super().__init__(
            driver, display_name, aid=STANDALONE_AID, iid_manager=iid_manager
        )
        self.accessories = {}  # aid: acc

    def add_accessory(self, acc: "Accessory") -> None:
        """Add the given ``Accessory`` to this ``Bridge``.

        Every ``Accessory`` in a ``Bridge`` must have an AID and this AID must be
        unique among all the ``Accessories`` in the same `Bridge`. If the given
        ``Accessory``'s AID is None, a unique AID will be assigned to it. Otherwise,
        it will be verified that the AID is not the standalone aid (``STANDALONE_AID``)
        and that there is no other ``Accessory`` already in this ``Bridge``
        with that AID.

        .. note:: A ``Bridge`` cannot be added to another ``Bridge``.

        :param acc: The ``Accessory`` to be bridged.
        :type acc: Accessory

        :raise ValueError: When the given ``Accessory`` is of category
            ``CATEGORY_BRIDGE`` or if the AID of the ``Accessory`` clashes with
            another ``Accessory`` already in this ``Bridge``.
        """
        if acc.category == CATEGORY_BRIDGE:
            raise ValueError("Bridges cannot be bridged")

        if acc.aid is None:
            # For some reason AID=7 gets unsupported. See issue #61
            acc.aid = next(
                aid
                for aid in itertools.count(2)
                if aid != 7 and aid not in self.accessories
            )
        elif acc.aid == self.aid or acc.aid in self.accessories:
            raise ValueError("Duplicate AID found when attempting to add accessory")

        self.accessories[acc.aid] = acc

    # A Bridge serializes one HAP dict per bridged accessory, which
    # legitimately widens the parent's single-dict return type.
    def to_HAP(  # ty: ignore[invalid-method-override]
        self, include_value: bool = True
    ) -> list[dict[str, Any]]:
        """Returns a HAP representation of itself and all contained accessories.

        .. seealso:: Accessory.to_HAP
        """
        return [
            acc.to_HAP(include_value=include_value)
            for acc in (super(), *self.accessories.values())
        ]

    def get_characteristic(self, aid: int, iid: int) -> "Characteristic | None":
        """.. seealso:: Accessory.to_HAP"""
        if self.aid == aid:
            obj = self.iid_manager.get_obj(iid)
            return obj if isinstance(obj, Characteristic) else None

        acc = self.accessories.get(aid)
        if acc is None:
            return None

        return acc.get_characteristic(aid, iid)

    def restore_accessory_state(self) -> None:
        """Restore the state of each bridged accessory."""
        for acc in self.accessories.values():
            try:
                acc.restore_accessory_state()
            except Exception:
                logger.warning(
                    "Failed to restore the state of %s", acc.display_name, exc_info=True
                )

    def reset_accessory_state(self) -> None:
        """Reset the state of each bridged accessory."""
        for acc in self.accessories.values():
            try:
                acc.reset_accessory_state()
            except Exception:
                logger.warning(
                    "Failed to reset the state of %s", acc.display_name, exc_info=True
                )

    async def run(self) -> None:
        """Schedule tasks for each of the accessories' run method."""
        for acc in self.accessories.values():
            self.driver.async_add_job(acc.run)

    async def stop(self) -> None:
        """Calls stop() on all contained accessories."""
        await self.driver.async_add_job(super().stop)
        for acc in self.accessories.values():
            await self.driver.async_add_job(acc.stop)


def get_topic(aid: int, iid: int) -> str:
    return str(aid) + "." + str(iid)
