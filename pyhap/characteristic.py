"""All things for a HAP characteristic.

A Characteristic is the smallest unit of the smart home, e.g.
a temperature measuring or a device status.
"""

from collections.abc import Callable
import inspect
import logging
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID
import warnings

from .const import (
    HAP_PERMISSION_READ,
    HAP_REPR_DESC,
    HAP_REPR_FORMAT,
    HAP_REPR_IID,
    HAP_REPR_MAX_LEN,
    HAP_REPR_PERM,
    HAP_REPR_TYPE,
    HAP_REPR_VALID_VALUES,
    HAP_REPR_VALUE,
)
from .delegates import CharacteristicDelegate
from .util import hap_type_to_uuid, uuid_to_hap_type

if TYPE_CHECKING:
    from .accessory import Accessory
    from .service import Service

logger = logging.getLogger(__name__)


# ### HAP Format ###
# A characteristic value as HAP JSON carries it.
HAPValue = bool | int | float | str | None


class _CharacteristicWriteTarget(TypedDict):
    aid: int
    iid: int


class CharacteristicWrite(_CharacteristicWriteTarget, total=False):
    """One characteristic of a write, as HAP JSON carries it.

    ``value`` is the value to write, ``ev`` subscribes or unsubscribes and
    ``r`` asks for a write response. ``authData`` and ``remote`` are part of
    the HAP request too, the driver does not read them.
    """

    value: HAPValue
    ev: bool
    r: bool
    authData: str
    remote: bool


HAP_FORMAT_BOOL = "bool"
HAP_FORMAT_INT = "int"
HAP_FORMAT_FLOAT = "float"
HAP_FORMAT_STRING = "string"
HAP_FORMAT_ARRAY = "array"
HAP_FORMAT_DICTIONARY = "dictionary"
HAP_FORMAT_UINT8 = "uint8"
HAP_FORMAT_UINT16 = "uint16"
HAP_FORMAT_UINT32 = "uint32"
HAP_FORMAT_UINT64 = "uint64"
HAP_FORMAT_DATA = "data"
HAP_FORMAT_TLV8 = "tlv8"

HAP_FORMAT_DEFAULTS = {
    HAP_FORMAT_BOOL: False,
    HAP_FORMAT_INT: 0,
    HAP_FORMAT_FLOAT: 0.0,
    HAP_FORMAT_STRING: "",
    HAP_FORMAT_ARRAY: "",
    HAP_FORMAT_DICTIONARY: "",
    HAP_FORMAT_UINT8: 0,
    HAP_FORMAT_UINT16: 0,
    HAP_FORMAT_UINT32: 0,
    HAP_FORMAT_UINT64: 0,
    HAP_FORMAT_DATA: "",
    HAP_FORMAT_TLV8: "",
}

HAP_FORMAT_NUMERICS = {
    HAP_FORMAT_INT,
    HAP_FORMAT_FLOAT,
    HAP_FORMAT_UINT8,
    HAP_FORMAT_UINT16,
    HAP_FORMAT_UINT32,
    HAP_FORMAT_UINT64,
}

DEFAULT_MAX_LENGTH = 64
ABSOLUTE_MAX_LENGTH = 256

# ### HAP Units ###
HAP_UNIT_ARC_DEGREE = "arcdegrees"
HAP_UNIT_CELSIUS = "celsius"
HAP_UNIT_LUX = "lux"
HAP_UNIT_PERCENTAGE = "percentage"
HAP_UNIT_SECONDS = "seconds"

# ### Properties ###
# Accesses only admin controllers may use. Not part of the HAP representation.
# The accessory driver enforces "write" only. Reads, the accessory database
# and events are not restricted.
PROP_ADMIN_ONLY_ACCESS = "AdminOnlyAccess"
ADMIN_ONLY_WRITE = "write"
PROP_FORMAT = "Format"
PROP_MAX_VALUE = "maxValue"
PROP_MIN_STEP = "minStep"
PROP_MIN_VALUE = "minValue"
PROP_PERMISSIONS = "Permissions"
PROP_UNIT = "unit"
PROP_VALID_VALUES = "ValidValues"

PROP_NUMERIC = {PROP_MAX_VALUE, PROP_MIN_VALUE, PROP_MIN_STEP, PROP_UNIT}

CHAR_BUTTON_EVENT = UUID("00000126-0000-1000-8000-0026BB765291")
CHAR_PROGRAMMABLE_SWITCH_EVENT = UUID("00000073-0000-1000-8000-0026BB765291")


IMMEDIATE_NOTIFY = {
    CHAR_BUTTON_EVENT,  # Button Event
    CHAR_PROGRAMMABLE_SWITCH_EVENT,  # Programmable Switch Event
}

# Special case, Programmable Switch Event always have a null value
ALWAYS_NULL = {
    CHAR_PROGRAMMABLE_SWITCH_EVENT,  # Programmable Switch Event
}


class CharacteristicError(Exception):
    """Generic exception class for characteristic errors."""


def _validate_properties(properties: dict[str, Any]) -> None:
    """Throw an exception on invalid properties."""
    if (
        HAP_REPR_MAX_LEN in properties
        and properties[HAP_REPR_MAX_LEN] > ABSOLUTE_MAX_LENGTH
    ):
        raise ValueError(f"{HAP_REPR_MAX_LEN} may not exceed {ABSOLUTE_MAX_LENGTH}")


class Characteristic:
    """Represents a HAP characteristic, the smallest unit of the smart home.

    A HAP characteristic is some measurement or state, like battery status or
    the current temperature. Characteristics are contained in services.
    Each characteristic has a unique type UUID and a set of properties,
    like format, min and max values, valid values and others.

    Set ``read_back_response`` to True for a readable control point that a
    controller writes and then reads, such as Setup Endpoints: what the
    setter returns or leaves as the value is read back only by the connection
    that wrote, and the shared value stays as it was. An async setter must
    return the answer and must not set the value itself, a value it sets is
    shared with every controller.
    """

    __slots__ = (
        "broker",
        "_display_name",
        "_properties",
        "type_id",
        "_value",
        "_getter_callback",
        "_setter_callback",
        "_delegate",
        "service",
        "_uuid_str",
        "_loader_display_name",
        "allow_invalid_client_values",
        "unique_id",
        "_to_hap_cache_with_value",
        "_to_hap_cache",
        "_always_null",
        "read_back_response",
    )

    def __init__(
        self,
        display_name: str | None,
        type_id: UUID | str,
        properties: dict[str, Any],
        allow_invalid_client_values: bool = False,
        unique_id: str | None = None,
    ) -> None:
        """Initialise with the given properties.

        :param display_name: Name that will be displayed for this
            characteristic, i.e. the `description` in the HAP representation.
        :type display_name: str

        :param type_id: UUID unique to this type of characteristic.
        :type type_id: uuid.UUID

        :param properties: A dict of properties, such as Format,
            ValidValues, etc.
        :type properties: dict
        """
        _validate_properties(properties)
        self.broker: Accessory | None = None
        #
        # As of iOS 15.1, Siri requests TargetHeatingCoolingState
        # as Auto reguardless if its a valid value or not.
        #
        # Consumers of this api may wish to set allow_invalid_client_values
        # to True and handle converting the Auto state to Cool or Heat
        # depending on the device.
        #
        self._always_null = type_id in ALWAYS_NULL
        self.allow_invalid_client_values = allow_invalid_client_values
        self._display_name = display_name
        self._properties: dict[str, Any] = properties
        self.type_id = type_id
        self._value = self._get_default_value()
        self._getter_callback: Callable[[], Any] | None = None
        # The legacy setter is called with the written value. The return
        # value is sent back as the write response when the characteristic
        # has the "wr" permission and the controller asked for one. A setter
        # may also be a coroutine function; a controller's write is answered
        # once it is done.
        self._setter_callback: Callable[..., Any] | None = None
        # The delegate of the reads, writes and subscriptions of this
        # characteristic, see ``CharacteristicDelegate``. Either a delegate
        # or the legacy callbacks, never both.
        self.delegate: CharacteristicDelegate | None = None
        self.service: Service | None = None
        self.unique_id = unique_id
        self._uuid_str = uuid_to_hap_type(type_id)
        self._loader_display_name: str | None = None
        self._to_hap_cache_with_value: dict[str, Any] | None = None
        self._to_hap_cache: dict[str, Any] | None = None
        # For a control point a controller writes and then reads, such as
        # Setup Endpoints: the setter's return value is what the writing
        # connection reads back, like a write response.
        self.read_back_response = False

    @property
    def display_name(self) -> str | None:
        """Return the display name of the characteristic."""
        return self._display_name

    @display_name.setter
    def display_name(self, value: str) -> None:
        """Set the display name of the characteristic."""
        self._display_name = value
        self._clear_cache()

    @property
    def delegate(self) -> CharacteristicDelegate | None:
        """The characteristic's delegate, or ``None`` without one.

        A characteristic has either a delegate or the legacy callbacks,
        never both: assigning a delegate to a characteristic that has one
        raises ``ValueError``, and the reverse. A delegate takes over the
        reads of the characteristic, see
        :class:`~pyhap.delegates.CharacteristicDelegate`.
        """
        return self._delegate

    @delegate.setter
    def delegate(self, delegate: CharacteristicDelegate | None) -> None:
        """Assign the delegate; ``None`` removes it."""
        if delegate is not None and (
            self._setter_callback is not None or self._getter_callback is not None
        ):
            raise ValueError(
                f"{self._display_name} has a legacy callback, remove it before"
                " assigning a delegate"
            )
        self._delegate = delegate

    @delegate.deleter
    def delegate(self) -> None:
        """Remove the delegate."""
        self._delegate = None

    @property
    def getter_callback(self) -> Callable[[], Any] | None:
        """The legacy getter, called with nothing on every read of the value.

        Deprecated: a delegate's :meth:`~pyhap.delegates.CharacteristicDelegate.on_get`
        answers the reads instead.
        """
        return self._getter_callback

    @getter_callback.setter
    def getter_callback(self, callback: Callable[[], Any] | None) -> None:
        """Assign the legacy getter; ``None`` removes it."""
        self._assign_legacy_callback("getter_callback", callback, warn_stacklevel=3)

    @getter_callback.deleter
    def getter_callback(self) -> None:
        """Remove the legacy getter."""
        self._getter_callback = None

    @property
    def setter_callback(self) -> Callable[..., Any] | None:
        """The legacy setter, called with the value on a controller's write.

        The return value is sent back as the write response when the
        characteristic has the "wr" permission and the controller asked
        for one. May also be a coroutine function; a controller's write is
        answered once it is done. What a callback needs to know about the
        request comes through a delegate instead, see
        :class:`~pyhap.delegates.CharacteristicDelegate`.
        """
        return self._setter_callback

    @setter_callback.setter
    def setter_callback(self, callback: Callable[..., Any] | None) -> None:
        """Assign the legacy setter; ``None`` removes it."""
        self._assign_legacy_callback("setter_callback", callback, warn_stacklevel=3)

    @setter_callback.deleter
    def setter_callback(self) -> None:
        """Remove the legacy setter."""
        self._setter_callback = None

    def _assign_legacy_callback(
        self, name: str, callback: Callable[..., Any] | None, *, warn_stacklevel: int
    ) -> None:
        """Store a legacy callback, announcing its deprecation.

        The delegate conflict is checked first. Both the property setters
        above and :meth:`~pyhap.service.Service.configure_char` call this
        with ``warn_stacklevel=3``, so the warning names the user code
        that assigns the callback.
        """
        if callback is not None and self._delegate is not None:
            raise ValueError(
                f"{self._display_name} has a delegate, remove it before"
                " assigning a legacy callback"
            )
        if callback is not None:
            warnings.warn(
                f"The legacy {name} is deprecated, use a CharacteristicDelegate"
                " instead",
                DeprecationWarning,
                stacklevel=warn_stacklevel,
            )
        if name == "getter_callback":
            self._getter_callback = callback
        else:
            self._setter_callback = callback

    @property
    def value(self) -> Any:
        """Return the value of the characteristic."""
        return self._value

    @value.setter
    def value(self, value: Any) -> None:
        """Set the value of the characteristic."""
        self._value = value
        self._clear_cache()

    @property
    def properties(self) -> dict[str, Any]:
        """Return the properties of the characteristic.

        Properties should not be modified directly. Use override_properties instead.
        """
        return self._properties

    def __repr__(self) -> str:
        """Return the representation of the characteristic."""
        return (
            f"<characteristic display_name={self._display_name} "
            f"unique_id={self.unique_id} value={self._value} "
            f"properties={self._properties}>"
        )

    def _get_default_value(self) -> Any:
        """Return default value for format."""
        if self._always_null:
            return None

        valid_values = self._properties.get(PROP_VALID_VALUES)
        if valid_values:
            return min(valid_values.values())

        value = HAP_FORMAT_DEFAULTS[self._properties[PROP_FORMAT]]
        return self.to_valid_value(value)

    def get_value(self) -> Any:
        """This is to allow for calling `getter_callback`

        The legacy read: runs ``getter_callback`` when one is set and stores
        its answer. A delegate-backed characteristic answers its stored value
        here — the driver's read path calls its ``on_get`` instead, see
        :class:`~pyhap.delegates.CharacteristicDelegate`.

        :return: Current Characteristic Value
        """
        if self.getter_callback:
            self.value = self.to_valid_value(value=self.getter_callback())
        return self._value

    def valid_value_or_raise(self, value: Any) -> None:
        """Raise ValueError if PROP_VALID_VALUES is set and the value is not present."""
        if self._always_null:
            return
        valid_values = self._properties.get(PROP_VALID_VALUES)
        if not valid_values:
            return
        if value in valid_values.values():
            return
        error_msg = f"{self._display_name}: value={value} is an invalid value."
        logger.error(error_msg)
        raise ValueError(error_msg)

    def to_valid_value(self, value: Any) -> Any:
        """Perform validation and conversion to valid value."""
        properties = self._properties
        prop_format = properties[PROP_FORMAT]

        if prop_format == HAP_FORMAT_STRING:
            return str(value)[: properties.get(HAP_REPR_MAX_LEN, DEFAULT_MAX_LENGTH)]

        if prop_format == HAP_FORMAT_BOOL:
            return bool(value)

        if prop_format in HAP_FORMAT_NUMERICS:
            if not isinstance(value, (int, float)):
                error_msg = (
                    f"{self._display_name}: value={value} is not a numeric value."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            min_step = properties.get(PROP_MIN_STEP)
            if value and min_step:
                value = round(min_step * round(value / min_step), 14)
            value = min(properties.get(PROP_MAX_VALUE, value), value)
            value = max(properties.get(PROP_MIN_VALUE, value), value)
            if prop_format != HAP_FORMAT_FLOAT:
                return int(value)

        return value

    def override_properties(
        self,
        properties: dict[str, Any] | None = None,
        valid_values: dict[str, Any] | None = None,
    ) -> None:
        """Override characteristic property values and valid values.

        :param properties: Dictionary with values to override the existing
            properties. Only changed values are required.
        :type properties: dict

        :param valid_values: Dictionary with values to override the existing
            valid_values. Valid values will be set to new dictionary.
        :type valid_values: dict
        """
        if not properties and not valid_values:
            raise ValueError("No properties or valid_values specified to override.")

        self._clear_cache()

        if properties:
            _validate_properties(properties)
            self._properties.update(properties)

        if valid_values:
            self._properties[PROP_VALID_VALUES] = valid_values

        if self._always_null:
            self.value = None
            return

        try:
            candidate = self.to_valid_value(self._value)
        except ValueError:
            self.value = self._get_default_value()
            return

        valid_values_dict = self._properties.get(PROP_VALID_VALUES)
        if valid_values_dict and candidate not in valid_values_dict.values():
            self.value = self._get_default_value()
        else:
            self.value = candidate

    def _clear_cache(self) -> None:
        """Clear the cached HAP representation."""
        self._to_hap_cache = None
        self._to_hap_cache_with_value = None

    def set_value(self, value: Any, should_notify: bool = True) -> None:
        """Set the given raw value. It is checked if it is a valid value.

        If not set_value will be aborted and an error message will be
        displayed.

        `Characteristic.setter_callback`
        You may also define a `setter_callback` on the `Characteristic`.
        This will be called with the value being set as the arg.

        .. seealso:: Characteristic.value

        :param value: The value to assign as this Characteristic's value.
        :type value: Depends on properties["Format"]

        :param should_notify: Whether a the change should be sent to
            subscribed clients. Notify will be performed if the broker is set.
        :type should_notify: bool
        """
        logger.debug("set_value: %s to %s", self._display_name, value)
        value = self.to_valid_value(value)
        self.valid_value_or_raise(value)
        changed = self._value != value
        self.value = value
        if changed and should_notify and self.broker:
            self.notify()
        if self._always_null:
            self.value = None

    def client_update_value(
        self,
        value: Any,
        sender_client_addr: tuple[str, int] | None = None,
        *,
        notify: bool = True,
        defer_notify: bool = False,
    ) -> Any:
        """Called from broker for value change in Home app.

        Change self.value to value and call callback. Returns what the callback
        returned. With ``notify`` False, subscribers are not told about the
        written value, e.g. for a request that only the writing connection may
        read back. With ``defer_notify``, a coroutine the callback returns is
        for the caller to await, and the caller then calls
        :meth:`client_update_finished`, so subscribers only learn the value
        once the setter succeeded.
        """
        original_value = value
        if not self._always_null or original_value is not None:
            value = self.to_valid_value(value)
        if not self.allow_invalid_client_values:
            self.valid_value_or_raise(value)
        logger.debug(
            "client_update_value: %s to %s (original: %s) from client: %s",
            self._display_name,
            value,
            original_value,
            sender_client_addr,
        )
        previous_value = self._value
        self.value = value
        response = None
        if self.setter_callback:
            response = self.setter_callback(value)
        if not (defer_notify and inspect.iscoroutine(response)):
            self.client_update_finished(
                previous_value, sender_client_addr, notify=notify
            )
        return response

    def _store_client_value(self, value: Any) -> Any:
        """Store a controller's write without running any callback.

        The new-style write path of the driver: validate and store the
        value like :meth:`client_update_value` does, tell no subscriber
        and call no callback - the driver runs the characteristic's
        ``on_set`` and notifies once it succeeded, or restores this value
        if the write is refused. Returns the value's previous state.
        """
        if not self._always_null or value is not None:
            value = self.to_valid_value(value)
        if not self.allow_invalid_client_values:
            self.valid_value_or_raise(value)
        previous_value = self._value
        self.value = value
        return previous_value

    def client_update_finished(
        self,
        previous_value: Any,
        sender_client_addr: tuple[str, int] | None,
        *,
        notify: bool = True,
    ) -> None:
        """Finish a client's write, e.g. after awaiting its coroutine setter.

        Tells subscribers about the new value if it differs from
        ``previous_value``, see :meth:`client_update_value`.
        """
        if notify and self._value != previous_value:
            self.notify(sender_client_addr)
        if self._always_null:
            self.value = None

    def notify(self, sender_client_addr: tuple[str, int] | None = None) -> None:
        """Notify clients about a value change. Sends the value.

        .. seealso:: accessory.publish
        .. seealso:: accessory_driver.publish
        """
        # An unbridged characteristic cannot notify; the attribute error on
        # purpose mirrors the historical behavior some tests rely on.
        if TYPE_CHECKING:
            assert self.broker is not None  # noqa: S101
        immediate = self.type_id in IMMEDIATE_NOTIFY
        self.broker.publish(self.value, self, sender_client_addr, immediate)

    def to_HAP(self, include_value: bool = True) -> dict[str, Any]:
        """Create a HAP representation of this Characteristic.

        Used for json serialization.

        :return: A HAP representation.
        :rtype: dict
        """
        if include_value:
            if (
                self._to_hap_cache_with_value is not None
                and self.getter_callback is None
                and self.delegate is None
            ):
                return self._to_hap_cache_with_value
        elif self._to_hap_cache is not None:
            return self._to_hap_cache

        properties = self._properties
        permissions = properties[PROP_PERMISSIONS]
        prop_format = properties[PROP_FORMAT]
        assert self.broker is not None  # noqa: S101
        hap_rep = {
            HAP_REPR_IID: self.broker.iid_manager.get_iid(self),
            HAP_REPR_TYPE: self._uuid_str,
            HAP_REPR_PERM: permissions,
            HAP_REPR_FORMAT: prop_format,
        }
        # HAP_REPR_DESC (description) is optional and takes up
        # quite a bit of space in the payload. Only include it
        # if it has been changed from the default loader version
        loader_display_name = self._loader_display_name
        display_name = self._display_name
        if not loader_display_name or loader_display_name != display_name:
            hap_rep[HAP_REPR_DESC] = display_name

        if prop_format in HAP_FORMAT_NUMERICS:
            hap_rep.update(
                {k: properties[k] for k in PROP_NUMERIC.intersection(properties)}
            )

            if PROP_VALID_VALUES in properties:
                hap_rep[HAP_REPR_VALID_VALUES] = sorted(
                    properties[PROP_VALID_VALUES].values()
                )
        elif prop_format == HAP_FORMAT_STRING:
            max_length = properties.get(HAP_REPR_MAX_LEN, DEFAULT_MAX_LENGTH)
            if max_length != DEFAULT_MAX_LENGTH:
                hap_rep[HAP_REPR_MAX_LEN] = max_length

        if include_value and HAP_PERMISSION_READ in permissions:
            try:
                hap_rep[HAP_REPR_VALUE] = self.get_value()
            except Exception:
                # One failing getter must not fail the whole accessories
                # response: the characteristic is listed without a value.
                logger.exception(
                    "%s: Error getting the value; answering without it",
                    self._display_name,
                )

        if not include_value:
            self._to_hap_cache = hap_rep
        elif self.getter_callback is None and self.delegate is None:
            # Only cache if nothing answers reads dynamically: a getter
            # callback or a delegate's on_get may answer something else
            # than the stored value.
            self._to_hap_cache_with_value = hap_rep
        return hap_rep

    @classmethod
    def from_dict(
        cls, name: str, json_dict: dict[str, Any], from_loader: bool = False
    ) -> "Characteristic":
        """Initialize a characteristic object from a dict.

        :param json_dict: Dictionary containing at least the keys `Format`,
            `Permissions` and `UUID`
        :type json_dict: dict
        """
        type_id = hap_type_to_uuid(json_dict.pop("UUID"))
        char = cls(name, type_id, properties=json_dict)
        if from_loader:
            char._loader_display_name = char.display_name
        return char
