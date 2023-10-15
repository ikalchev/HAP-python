"""
All things for a HAP characteristic.

A Characteristic is the smallest unit of the smart home, e.g.
a temperature measuring or a device status.
"""
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple
from uuid import UUID

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
from .util import hap_type_to_uuid, uuid_to_hap_type

if TYPE_CHECKING:
    from .accessory import Accessory
    from .service import Service

logger = logging.getLogger(__name__)

# ### HAP Format ###
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


def _validate_properties(properties: Dict[str, Any]) -> None:
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
    """

    __slots__ = (
        "broker",
        "_display_name",
        "_properties",
        "type_id",
        "_value",
        "getter_callback",
        "setter_callback",
        "service",
        "_uuid_str",
        "_loader_display_name",
        "allow_invalid_client_values",
        "unique_id",
        "_to_hap_cache_with_value",
        "_to_hap_cache",
        "_always_null",
    )

    def __init__(
        self,
        display_name: Optional[str],
        type_id: UUID,
        properties: Dict[str, Any],
        allow_invalid_client_values: bool = False,
        unique_id: Optional[str] = None,
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
        self.broker: Optional["Accessory"] = None
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
        self._properties: Dict[str, Any] = properties
        self.type_id = type_id
        self._value = self._get_default_value()
        self.getter_callback: Optional[Callable[[], Any]] = None
        self.setter_callback: Optional[Callable[[Any], None]] = None
        self.service: Optional["Service"] = None
        self.unique_id = unique_id
        self._uuid_str = uuid_to_hap_type(type_id)
        self._loader_display_name: Optional[str] = None
        self._to_hap_cache_with_value: Optional[Dict[str, Any]] = None
        self._to_hap_cache: Optional[Dict[str, Any]] = None

    @property
    def display_name(self) -> Optional[str]:
        """Return the display name of the characteristic."""
        return self._display_name

    @display_name.setter
    def display_name(self, value: str) -> None:
        """Set the display name of the characteristic."""
        self._display_name = value
        self._clear_cache()

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
    def properties(self) -> Dict[str, Any]:
        """Return the properties of the characteristic.

        Properties should not be modified directly. Use override_properties instead.
        """
        return self._properties

    def __repr__(self) -> str:
        """Return the representation of the characteristic."""
        return (
            f"<characteristic display_name={self._display_name} unique_id={self.unique_id} "
            f"value={self._value} properties={self._properties}>"
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

        :return: Current Characteristic Value
        """
        if self.getter_callback:
            # pylint: disable=not-callable
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
        properties: Optional[Dict[str, Any]] = None,
        valid_values: Optional[Dict[str, Any]] = None,
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
            self.value = self.to_valid_value(self._value)
            self.valid_value_or_raise(self._value)
        except ValueError:
            self.value = self._get_default_value()

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
        self, value: Any, sender_client_addr: Optional[Tuple[str, int]] = None
    ) -> None:
        """Called from broker for value change in Home app.

        Change self.value to value and call callback.
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
            # pylint: disable=not-callable
            response = self.setter_callback(value)
        changed = self._value != previous_value
        if changed:
            self.notify(sender_client_addr)
        if self._always_null:
            self.value = None
        return response

    def notify(self, sender_client_addr: Optional[Tuple[str, int]] = None) -> None:
        """Notify clients about a value change. Sends the value.

        .. seealso:: accessory.publish
        .. seealso:: accessory_driver.publish
        """
        immediate = self.type_id in IMMEDIATE_NOTIFY
        self.broker.publish(self.value, self, sender_client_addr, immediate)

    # pylint: disable=invalid-name
    def to_HAP(self, include_value: bool = True) -> Dict[str, Any]:
        """Create a HAP representation of this Characteristic.

        Used for json serialization.

        :return: A HAP representation.
        :rtype: dict
        """
        if include_value:
            if self._to_hap_cache_with_value is not None and not self.getter_callback:
                return self._to_hap_cache_with_value
        elif self._to_hap_cache is not None:
            return self._to_hap_cache

        properties = self._properties
        permissions = properties[PROP_PERMISSIONS]
        prop_format = properties[PROP_FORMAT]
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
            hap_rep[HAP_REPR_VALUE] = self.get_value()

        if not include_value:
            self._to_hap_cache = hap_rep
        elif not self.getter_callback:
            # Only cache if there is no getter_callback
            self._to_hap_cache_with_value = hap_rep
        return hap_rep

    @classmethod
    def from_dict(
        cls, name: str, json_dict: Dict[str, Any], from_loader: bool = False
    ) -> "Characteristic":
        """Initialize a characteristic object from a dict.

        :param json_dict: Dictionary containing at least the keys `Format`,
            `Permissions` and `UUID`
        :type json_dict: dict
        """
        type_id = hap_type_to_uuid(json_dict.pop("UUID"))
        char = cls(name, type_id, properties=json_dict)
        if from_loader:
            char._loader_display_name = (  # pylint: disable=protected-access
                char.display_name
            )
        return char
