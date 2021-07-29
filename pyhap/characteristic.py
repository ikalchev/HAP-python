"""
All things for a HAP characteristic.

A Characteristic is the smallest unit of the smart home, e.g.
a temperature measuring or a device status.
"""
import logging

from uuid import UUID


from pyhap.const import (
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

PROP_NUMERIC = (PROP_MAX_VALUE, PROP_MIN_VALUE, PROP_MIN_STEP, PROP_UNIT)

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


class Characteristic:
    """Represents a HAP characteristic, the smallest unit of the smart home.

    A HAP characteristic is some measurement or state, like battery status or
    the current temperature. Characteristics are contained in services.
    Each characteristic has a unique type UUID and a set of properties,
    like format, min and max values, valid values and others.
    """

    __slots__ = (
        "broker",
        "display_name",
        "properties",
        "type_id",
        "value",
        "getter_callback",
        "setter_callback",
        "service",
        "_uuid_str",
        "_loader_display_name",
    )

    def __init__(self, display_name, type_id, properties):
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
        self.broker = None
        self.display_name = display_name
        self.properties = properties
        self.type_id = type_id
        self.value = self._get_default_value()
        self.getter_callback = None
        self.setter_callback = None
        self.service = None
        self._uuid_str = uuid_to_hap_type(type_id)
        self._loader_display_name = None

    def __repr__(self):
        """Return the representation of the characteristic."""
        return "<characteristic display_name={} value={} properties={}>".format(
            self.display_name, self.value, self.properties
        )

    def _get_default_value(self):
        """Return default value for format."""
        if self.type_id in ALWAYS_NULL:
            return None

        if self.properties.get(PROP_VALID_VALUES):
            return min(self.properties[PROP_VALID_VALUES].values())

        value = HAP_FORMAT_DEFAULTS[self.properties[PROP_FORMAT]]
        return self.to_valid_value(value)

    def get_value(self):
        """This is to allow for calling `getter_callback`

        :return: Current Characteristic Value
        """
        if self.getter_callback:
            # pylint: disable=not-callable
            self.value = self.to_valid_value(value=self.getter_callback())
        return self.value

    def to_valid_value(self, value):
        """Perform validation and conversion to valid value."""
        if self.properties.get(PROP_VALID_VALUES):
            if value not in self.properties[PROP_VALID_VALUES].values():
                error_msg = "{}: value={} is an invalid value.".format(
                    self.display_name, value
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
        elif self.properties[PROP_FORMAT] == HAP_FORMAT_STRING:
            value = str(value)[:256]
        elif self.properties[PROP_FORMAT] == HAP_FORMAT_BOOL:
            value = bool(value)
        elif self.properties[PROP_FORMAT] in HAP_FORMAT_NUMERICS:
            if not isinstance(value, (int, float)):
                error_msg = "{}: value={} is not a numeric value.".format(
                    self.display_name, value
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            value = min(self.properties.get(PROP_MAX_VALUE, value), value)
            value = max(self.properties.get(PROP_MIN_VALUE, value), value)
            if self.properties[PROP_FORMAT] != HAP_FORMAT_FLOAT:
                value = int(value)
        return value

    def override_properties(self, properties=None, valid_values=None):
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

        if properties:
            self.properties.update(properties)

        if valid_values:
            self.properties[PROP_VALID_VALUES] = valid_values

        if self.type_id in ALWAYS_NULL:
            self.value = None
            return

        try:
            self.value = self.to_valid_value(self.value)
        except ValueError:
            self.value = self._get_default_value()

    def set_value(self, value, should_notify=True):
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
        logger.debug("set_value: %s to %s", self.display_name, value)
        value = self.to_valid_value(value)
        changed = self.value != value
        self.value = value
        if changed and should_notify and self.broker:
            self.notify()
        if self.type_id in ALWAYS_NULL:
            self.value = None

    def client_update_value(self, value, sender_client_addr=None):
        """Called from broker for value change in Home app.

        Change self.value to value and call callback.
        """
        logger.debug(
            "client_update_value: %s to %s from client: %s",
            self.display_name,
            value,
            sender_client_addr,
        )
        changed = self.value != value
        self.value = value
        if changed:
            self.notify(sender_client_addr)
        if self.setter_callback:
            # pylint: disable=not-callable
            self.setter_callback(value)
        if self.type_id in ALWAYS_NULL:
            self.value = None

    def notify(self, sender_client_addr=None):
        """Notify clients about a value change. Sends the value.

        .. seealso:: accessory.publish
        .. seealso:: accessory_driver.publish
        """
        immediate = self.type_id in IMMEDIATE_NOTIFY
        self.broker.publish(self.value, self, sender_client_addr, immediate)

    # pylint: disable=invalid-name
    def to_HAP(self):
        """Create a HAP representation of this Characteristic.

        Used for json serialization.

        :return: A HAP representation.
        :rtype: dict
        """
        hap_rep = {
            HAP_REPR_IID: self.broker.iid_manager.get_iid(self),
            HAP_REPR_TYPE: self._uuid_str,
            HAP_REPR_PERM: self.properties[PROP_PERMISSIONS],
            HAP_REPR_FORMAT: self.properties[PROP_FORMAT],
        }
        # HAP_REPR_DESC (description) is optional and takes up
        # quite a bit of space in the payload. Only include it
        # if it has been changed from the default loader version
        if (
            not self._loader_display_name
            or self._loader_display_name != self.display_name
        ):
            hap_rep[HAP_REPR_DESC] = self.display_name

        value = self.get_value()
        if self.properties[PROP_FORMAT] in HAP_FORMAT_NUMERICS:
            hap_rep.update(
                {k: self.properties[k] for k in self.properties.keys() & PROP_NUMERIC}
            )

            if PROP_VALID_VALUES in self.properties:
                hap_rep[HAP_REPR_VALID_VALUES] = sorted(
                    self.properties[PROP_VALID_VALUES].values()
                )
        elif self.properties[PROP_FORMAT] == HAP_FORMAT_STRING:
            if len(value) > 64:
                hap_rep[HAP_REPR_MAX_LEN] = min(len(value), 256)
        if HAP_PERMISSION_READ in self.properties[PROP_PERMISSIONS]:
            hap_rep[HAP_REPR_VALUE] = value

        return hap_rep

    @classmethod
    def from_dict(cls, name, json_dict, from_loader=False):
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
