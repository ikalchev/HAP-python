"""
All things for a HAP characteristic.

A Characteristic is the smallest unit of the smart home, e.g.
a temperature measuring or a device status.
"""

class HAP_FORMAT:
    BOOL = 'bool'
    INT = 'int'
    FLOAT = 'float'
    STRING = 'string'
    ARRAY = 'array'
    DICTIONARY = 'dictionary'
    UINT8 = 'uint8'
    UINT16 = 'uint16'
    UINT32 = 'uint32'
    UINT64 = 'uint64'
    DATA = 'data'
    TLV8 = 'tlv8'

    NUMERIC = (INT, FLOAT, UINT8, UINT16, UINT32, UINT64)

    DEFAULT = {
        BOOL: False,
        INT:  0,
        FLOAT: 0.,
        STRING: "",
        ARRAY: "",
        DICTIONARY: "",
        UINT8: 0,
        UINT16: 0,
        UINT32: 0,
        UINT64: 0,
        DATA: "",
        TLV8: "",
    }


class HAP_UNITS:
    CELSIUS = 'celsius'
    PERCENTAGE = 'percentage'
    ARC_DEGREE = 'arcdegrees'
    LUX = 'lux'
    SECONDS = 'seconds'


class HAP_PERMISSIONS:
    READ = 'pr'
    WRITE = 'pw'
    NOTIFY = 'ev'
    HIDDEN = 'hd'


class CharacteristicError(Exception):
    pass


class NotConfiguredError(Exception):
    """Raised when an operation is attempted on a characteristic that has not been
    fully configured.
    """
    pass


_HAP_NUMERIC_FIELDS = {"maxValue", "minValue", "minStep", "unit"}
"""Fields that should be included in the HAP representation of the characteristic.

That is, if they are present in the specification of a numeric-value characteristic.
"""


class Characteristic(object):
    """Represents a HAP characteristic, the smallest unit of the smart home.

    A HAP characteristic is some measurement or state, like battery status or
    the current temperature. Characteristics are contained in services.
    Each charactaristic has a unique type UUID and a set of properties,
    like format, min and max values, valid values and others.
    """

    def __init__(self, display_name, type_id, properties, value=None, broker=None):
        """Initialise with the given properties.

        :param display_name: Name that will be displayed for this characteristic, i.e.
            the `description` in the HAP representation.
        :type display_name: str

        :param type_id: UUID unique to this type of characteristic.
        :type type_id: uuid.UUID

        :param properties: A dict of properties, such as Format, ValidValues, etc.
        :type properties: dict

        :param value: The initial value to set to this characteristic. If no value is given,
            the assigned value happens as:
            - if there is a ValidValue property, use some value from it.
            - else use `HAP_FORMAT.DEFAULT` for the format of this characteristic.
        :type value: Depends on `properties["Format"]`
        """
        assert "Format" in properties and "Permissions" in properties
        self.display_name = display_name
        self.type_id = type_id
        self.properties = properties
        self.has_valid_values = "ValidValues" in self.properties
        if value is None:
            if self.has_valid_values:
                self.value = next(iter(self.properties["ValidValues"].values()))
            else:
                self.value = HAP_FORMAT.DEFAULT[properties["Format"]]
        else:
            self.value = value
        self.broker = broker
        self.setter_callback = None
        self.hap_template = self._create_hap_template()

    def _create_hap_template(self):
        """Create a HAP template for describing this Characteristic.

        Contains properties that do not change or change rarely, e.g. the type.
        """
        template = dict()
        if self.properties["Format"] in HAP_FORMAT.NUMERIC:
            template = {k: self.properties[k]
                        for k in self.properties.keys() & _HAP_NUMERIC_FIELDS}
        return template

    def set_value(self, value, should_notify=True, should_callback=True):
        """Set the given raw value. It is checked if it is a valid value.

        :param value: The value to assign as this Characteristic's value.
        :type value: Depends on properties["Format"]

        :param should_notify: Whether a the change should be sent to subscribed clients.
            The notification is called _after_ the setter callback. Notify will be
            performed if and only if the broker is set, i.e. not None.
        :type should_notify: bool

        :param should_callback: Whether to invoke the callback, if such is set. This
            is useful in cases where you and HAP clients can both update the value and
            you don't want your callback called when you set the value, but want it
            called when clients do. Defaults to True.
        :type should_callback: bool

        :raise ValueError: When the value being assigned is not one of the valid values
            for this Characteristic.
        """
        if (self.has_valid_values
                and value not in self.properties["ValidValues"].values()):
            raise ValueError
        self.value = value
        if self.setter_callback is not None and should_callback:
            self.setter_callback(value)
        if should_notify and self.broker is not None:
            self.notify()

    def get_value(self):
        """Get the raw value of this Characteristic.

        .. deprecated:: v1.1.0 Use self.value instead.
        """
        return self.value

    def get_hap_value(self):
        """Get the value of the characteristic, constrained with the HAP properties.
        """
        val = self.value
        if self.properties["Format"] == HAP_FORMAT.STRING:
            val = val[:256]
        elif self.properties["Format"] in HAP_FORMAT.NUMERIC:
            if "maxValue" in self.properties:
                val = min(self.properties["maxValue"], val)
            if "minValue" in self.properties:
                val = max(self.properties["minValue"], val)
        return val

    def notify(self):
        """Notify clients about a value change.

        .. note:: Non-blocking, i.e. does not wait for the update to be sent.
        .. note:: Uses the `get_hap_value`, i.e. sends the HAP value.
        .. seealso:: accessory_driver.publish

        :raise NotConfiguredError: When the broker is not set.
        """
        if self.broker is None:
            raise NotConfiguredError("Attempted to notify when `broker` is None. "
                                     "Consider adding the characteristic to a "
                                     "Service and then to an Accessory.")

        data = {
            "type_id": self.type_id,
            "value": self.get_hap_value(),
        }
        self.broker.publish(data, self)

    def to_HAP(self, iid_manager):
        """Create a HAP representation of this Characteristic.

        .. note:: Uses the `get_hap_value`, i.e. sends the HAP value.

        :param iid_manager: IID manager to query for this object's IID.
        :type iid_manager: IIDManager

        :return: A HAP representation.
        :rtype: dict
        """
        hap_rep = {
            "iid": iid_manager.get_iid(self),
            "type": str(self.type_id).upper(),
            "description": self.display_name,
            "perms": self.properties["Permissions"],
            "format": self.properties["Format"],
        }

        value_info = self.hap_template.copy()
        val = self.get_hap_value()
        if self.properties["Format"] == HAP_FORMAT.STRING:
            if len(val) > 64:
                value_info["maxLen"] = min(len(val), 256)
        if HAP_PERMISSIONS.READ in self.properties["Permissions"]:
            value_info["value"] = val

        hap_rep.update(value_info)
        return hap_rep
