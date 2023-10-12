"""This module implements the HAP Service."""

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from uuid import UUID

from pyhap.const import (
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_LINKED,
    HAP_REPR_PRIMARY,
    HAP_REPR_TYPE,
)

from .characteristic import Characteristic
from .util import hap_type_to_uuid, uuid_to_hap_type

if TYPE_CHECKING:
    from .accessory import Accessory
    from .loader import Loader


class Service:
    """A representation of a HAP service.

    A Service contains multiple characteristics. For example, a
    TemperatureSensor service has the characteristic CurrentTemperature.
    """

    __slots__ = (
        "broker",
        "characteristics",
        "display_name",
        "type_id",
        "linked_services",
        "is_primary_service",
        "setter_callback",
        "unique_id",
        "_uuid_str",
    )

    def __init__(
        self,
        type_id: UUID,
        display_name: Optional[str] = None,
        unique_id: Optional[str] = None,
    ) -> None:
        """Initialize a new Service object."""
        self.broker: Optional["Accessory"] = None
        self.characteristics: List[Characteristic] = []
        self.linked_services: List[Service] = []
        self.display_name = display_name
        self.type_id = type_id
        self.is_primary_service = None
        self.setter_callback: Optional[Callable[[Any], None]] = None
        self.unique_id = unique_id
        self._uuid_str = uuid_to_hap_type(type_id)

    def __repr__(self):
        """Return the representation of the service."""
        chars_dict = {c.display_name: c.value for c in self.characteristics}
        return f"<service display_name={self.display_name} unique_id={self.unique_id} chars={chars_dict}>"

    def add_linked_service(self, service: "Service") -> None:
        """Add the given service as "linked" to this Service."""
        iid_manager = self.broker.iid_manager
        if not any(
            iid_manager.get_iid(service) == iid_manager.get_iid(original_service)
            for original_service in self.linked_services
        ):
            self.linked_services.append(service)

    def add_characteristic(self, *chars: Characteristic) -> None:
        """Add the given characteristics as "mandatory" for this Service."""
        for char in chars:
            if not any(
                char.type_id == original_char.type_id
                for original_char in self.characteristics
            ):
                char.service = self
                self.characteristics.append(char)

    def get_characteristic(self, name: str) -> Characteristic:
        """Return a Characteristic object by the given name from this Service.

        :param name: The name of the characteristic to search for.
        :type name: str

        :raise ValueError if characteristic is not found.

        :return: A characteristic with the given name.
        :rtype: Characteristic
        """
        for char in self.characteristics:
            if char.display_name == name:
                return char
        raise ValueError("Characteristic not found")

    def configure_char(
        self,
        char_name: str,
        properties=None,
        valid_values=None,
        value=None,
        setter_callback=None,
        getter_callback=None,
    ) -> Characteristic:
        """Helper method to return fully configured characteristic."""
        char = self.get_characteristic(char_name)
        if properties or valid_values:
            char.override_properties(properties, valid_values)
        if value:
            char.set_value(value, should_notify=False)
        if setter_callback:
            char.setter_callback = setter_callback
        if getter_callback:
            char.getter_callback = getter_callback
        return char

    # pylint: disable=invalid-name
    def to_HAP(self, include_value: bool = True) -> Dict[str, Any]:
        """Create a HAP representation of this Service.

        :return: A HAP representation.
        :rtype: dict.
        """
        hap = {
            HAP_REPR_IID: self.broker.iid_manager.get_iid(self),
            HAP_REPR_TYPE: self._uuid_str,
            HAP_REPR_CHARS: [c.to_HAP(include_value) for c in self.characteristics],
        }

        if self.is_primary_service is not None:
            hap[HAP_REPR_PRIMARY] = self.is_primary_service

        if self.linked_services:
            linked: List[int] = []
            for linked_service in self.linked_services:
                linked.append(linked_service.broker.iid_manager.get_iid(linked_service))
            hap[HAP_REPR_LINKED] = linked

        return hap

    @classmethod
    def from_dict(
        cls, name: str, json_dict: Dict[str, Any], loader: "Loader"
    ) -> "Service":
        """Initialize a service object from a dict.

        :param json_dict: Dictionary containing at least the keys `UUID` and
            `RequiredCharacteristics`
        :type json_dict: dict
        """
        type_id = hap_type_to_uuid(json_dict.pop("UUID"))
        service = cls(type_id, name)
        for char_name in json_dict["RequiredCharacteristics"]:
            service.add_characteristic(loader.get_char(char_name))
        return service
