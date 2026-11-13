"""This module implements the HAP Service."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pyhap.const import (
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_LINKED,
    HAP_REPR_PRIMARY,
    HAP_REPR_TYPE,
)

from .characteristic import Characteristic
from .delegates import CharacteristicDelegate, ServiceDelegate, _service_delegate
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
        "_setter_callback",
        "_delegate",
        "_delegate_hooks",
        "unique_id",
        "_uuid_str",
    )

    def __init__(
        self,
        type_id: UUID,
        display_name: str | None = None,
        unique_id: str | int | None = None,
    ) -> None:
        """Initialize a new Service object."""
        self.broker: Accessory | None = None
        self.characteristics: list[Characteristic] = []
        self.linked_services: list[Service] = []
        self.display_name = display_name
        self.type_id = type_id
        self.is_primary_service = None
        self._delegate: ServiceDelegate | None = None
        self._delegate_hooks: frozenset[str] = frozenset()
        self.setter_callback: Callable[[Any], None] | None = None
        self.unique_id = unique_id
        self._uuid_str = uuid_to_hap_type(type_id)

    @property
    def delegate(self) -> ServiceDelegate | None:
        """The service's delegate, or ``None`` without one.

        Assign a :class:`~pyhap.delegates.ServiceDelegate` instance, or its
        class to instantiate one with no arguments. The assignment is when
        everything about the delegate is decided: the driver records that its
        ``on_set`` is overridden and a coroutine function, and a method that
        is not fails with a ``TypeError``.
        """
        return self._delegate

    @delegate.setter
    def delegate(
        self, delegate: ServiceDelegate | type[ServiceDelegate] | None
    ) -> None:
        """Wire the delegate; ``None`` removes it."""
        self._delegate, self._delegate_hooks = (
            (None, frozenset()) if delegate is None else _service_delegate(delegate)
        )

    @delegate.deleter
    def delegate(self) -> None:
        """Remove the delegate."""
        self._delegate, self._delegate_hooks = None, frozenset()

    @property
    def setter_callback(self) -> Callable[[Any], None] | None:
        """The legacy setter, called with the request's values by name.

        Called with a ``{display_name: value}`` dict after the
        per-characteristic setters of a write. Replaced over time by the
        delegate's grouped ``on_set``, assigning one does not remove this.
        """
        return self._setter_callback

    @setter_callback.setter
    def setter_callback(self, callback: Callable[[Any], None] | None) -> None:
        """Set the legacy setter; ``None`` removes it."""
        self._setter_callback = callback

    @setter_callback.deleter
    def setter_callback(self) -> None:
        """Remove the legacy setter."""
        self._setter_callback = None

    def __repr__(self) -> str:
        """Return the representation of the service."""
        chars_dict = {c.display_name: c.value for c in self.characteristics}
        return (
            f"<service display_name={self.display_name} "
            f"unique_id={self.unique_id} chars={chars_dict}>"
        )

    def add_linked_service(self, service: "Service") -> None:
        """Add the given service as "linked" to this Service."""
        assert self.broker is not None  # noqa: S101
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
        properties: Any = None,
        valid_values: Any = None,
        value: Any = None,
        setter_callback: Any = None,
        getter_callback: Any = None,
        delegate: CharacteristicDelegate | type[CharacteristicDelegate] | None = None,
    ) -> Characteristic:
        """Helper method to return fully configured characteristic.

        :param char_name: The name of the characteristic to configure.
        :param properties: Properties to override on the characteristic.
        :param valid_values: Valid values to set on the characteristic.
        :param value: The value to set on the characteristic.
        :param setter_callback: The legacy setter of the characteristic.
        :param getter_callback: The legacy getter of the characteristic.
        :param delegate: The characteristic's
            :class:`~pyhap.delegates.CharacteristicDelegate`, an instance or
            its class - instantiated with no arguments.
        """
        char = self.get_characteristic(char_name)
        if properties or valid_values:
            char.override_properties(properties, valid_values)
        if value:
            char.set_value(value, should_notify=False)
        if setter_callback:
            char.setter_callback = setter_callback
        if getter_callback:
            char.getter_callback = getter_callback
        if delegate is not None:
            char.delegate = delegate
        return char

    def to_HAP(self, include_value: bool = True) -> dict[str, Any]:
        """Create a HAP representation of this Service.

        :return: A HAP representation.
        :rtype: dict.
        """
        assert self.broker is not None  # noqa: S101
        hap = {
            HAP_REPR_IID: self.broker.iid_manager.get_iid(self),
            HAP_REPR_TYPE: self._uuid_str,
            HAP_REPR_CHARS: [c.to_HAP(include_value) for c in self.characteristics],
        }

        if self.is_primary_service is not None:
            hap[HAP_REPR_PRIMARY] = self.is_primary_service

        if self.linked_services:
            linked: list[int] = []
            for linked_service in self.linked_services:
                assert linked_service.broker is not None  # noqa: S101
                iid = linked_service.broker.iid_manager.get_iid(linked_service)
                assert iid is not None  # noqa: S101
                linked.append(iid)
            hap[HAP_REPR_LINKED] = linked

        return hap

    @classmethod
    def from_dict(
        cls, name: str, json_dict: dict[str, Any], loader: "Loader"
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
