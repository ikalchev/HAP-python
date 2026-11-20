"""This module implements the HAP Service."""

from collections.abc import Callable, Mapping
import inspect
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID
import warnings

from pyhap.const import (
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_LINKED,
    HAP_REPR_PRIMARY,
    HAP_REPR_TYPE,
)

from .characteristic import Characteristic
from .context import Context, ServiceWriteContext
from .delegates import CharacteristicDelegate, ServiceDelegate
from .util import hap_type_to_uuid, uuid_to_hap_type

if TYPE_CHECKING:
    from .accessory import Accessory
    from .loader import Loader

logger = logging.getLogger(__name__)


class _LegacySetterDelegateWrapper(ServiceDelegate):
    """Adapt the legacy setter of a service to the delegate interface.

    One wrapper is the delegate of a service with a legacy setter: ``on_set``
    applies what its ``setter`` answers, with the values by characteristic
    display name, like the legacy write path calls it. It is also the
    default delegate of a service without one: ``setter`` ``None``, it
    checks nothing.
    """

    __slots__ = ("service", "setter")

    def __init__(
        self,
        *,
        service: "Service",
        setter: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.service = service
        self.setter = setter

    async def on_set(self, values: Any, context: Any) -> None:
        if self.setter is None:
            return
        result = self.setter(
            {char.display_name: value for char, value in values.items()}
        )
        if inspect.iscoroutine(result):
            result.close()
            logger.warning(
                "%s: only characteristic setters may be coroutine functions",
                self.service.display_name,
            )


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
        "_delegate",
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
        # The delegate of the grouped writes of this service, see
        # ``ServiceDelegate``. Never ``None``: without an assigned one
        # this is the default one, the legacy setter wrapper without a
        # setter, see ``_LegacySetterDelegateWrapper``.
        self._delegate: ServiceDelegate = _LegacySetterDelegateWrapper(service=self)
        self.unique_id = unique_id
        self._uuid_str = uuid_to_hap_type(type_id)

    @property
    def delegate(self) -> ServiceDelegate:
        """The service's delegate, never ``None``.

        Without an assigned delegate this is the default one: the legacy
        setter wrapper without a setter, which checks nothing. A service
        has either a delegate or the legacy ``setter_callback``, never
        both: assigning one to a service that has the other raises
        ``ValueError``.
        """
        return self._delegate

    @delegate.setter
    def delegate(self, delegate: ServiceDelegate | None) -> None:
        """Assign the delegate; ``None`` resets to the default one."""
        legacy = self._legacy
        if legacy is not None and legacy.setter is not None:
            raise ValueError(
                f"{self.display_name} has a legacy setter_callback, remove it"
                " before assigning a delegate"
            )
        self._delegate = (
            _LegacySetterDelegateWrapper(service=self) if delegate is None else delegate
        )

    @property
    def _legacy(self) -> _LegacySetterDelegateWrapper | None:
        """The legacy setter wrapper, or ``None`` when a delegate is assigned.

        The one place that tells the two interfaces apart: the wrapper is
        the delegate of a service without an assigned one.
        """
        delegate = self._delegate
        return delegate if isinstance(delegate, _LegacySetterDelegateWrapper) else None

    @property
    def setter_callback(self) -> Callable[[dict[str, Any]], None] | None:
        """The legacy setter, called with the request's values by name.

        Called with a ``{display_name: value}`` dict before the
        per-characteristic setters apply the writes - a pre-check that
        may refuse them.

        Deprecated: a delegate's :meth:`~pyhap.delegates.ServiceDelegate.on_set`
        checks the writes of the service instead.
        """
        legacy = self._legacy
        if legacy is not None:
            return legacy.setter
        raise ValueError(
            f"{self.display_name} has a ServiceDelegate instead of a"
            " legacy setter_callback"
        )

    @setter_callback.setter
    def setter_callback(
        self, callback: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Assign the legacy setter; ``None`` removes it.

        Deprecated: a delegate's :meth:`~pyhap.delegates.ServiceDelegate.on_set`
        checks the writes of the service instead.
        """
        if callback is not None:
            warnings.warn(
                "The legacy setter_callback is deprecated, use a ServiceDelegate"
                " instead",
                DeprecationWarning,
                stacklevel=2,
            )
        legacy = self._legacy
        if legacy is None:
            raise ValueError(
                f"{self.display_name} has a delegate, remove it before"
                " assigning a legacy setter_callback"
            )
        legacy.setter = callback

    async def dispatch_set(
        self, values: Mapping[Characteristic, Any], context: Context, *, is_admin: bool
    ) -> None:
        """Check a grouped write of this service through its delegate.

        Called by the driver's write path before any per-characteristic
        ``on_set`` runs, with every written value of the request: builds
        this service's :class:`~pyhap.context.ServiceWriteContext` and
        awaits the delegate's ``on_set``. Assign a
        :class:`~pyhap.delegates.ServiceDelegate` instead of overriding
        this method - an override would silently take the delegate's
        place.

        :param values: Every written value of the request, by characteristic.
        :param context: The base context of the write request.
        :param is_admin: Whether the writing controller has admin privileges.
        """
        await self.delegate.on_set(
            values,
            ServiceWriteContext.of(context, service=self, is_admin=is_admin),
        )

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
        delegate: CharacteristicDelegate | None = None,
    ) -> Characteristic:
        """Helper method to return fully configured characteristic.

        :param char_name: The name of the characteristic to configure.
        :param properties: Properties to override on the characteristic.
        :param valid_values: Valid values to set on the characteristic.
        :param value: The value to set on the characteristic.
        :param setter_callback: The legacy setter of the characteristic.
        :param getter_callback: The legacy getter of the characteristic.
        :param delegate: The characteristic's
            :class:`~pyhap.delegates.CharacteristicDelegate` instance.
        """
        char = self.get_characteristic(char_name)
        if properties or valid_values:
            char.override_properties(properties, valid_values)
        if value:
            char.set_value(value, should_notify=False)
        if setter_callback is not None:
            char._assign_legacy_callback("setter_callback", setter_callback)
        if getter_callback is not None:
            char._assign_legacy_callback("getter_callback", getter_callback)
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
