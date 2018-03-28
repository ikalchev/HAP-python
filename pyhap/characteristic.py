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


_HAP_NUMERIC_FIELDS = ('maxValue', 'minValue', 'minStep', 'unit')
"""Fields that should be included in the HAP representation of the characteristic.

That is, if they are present in the specification of a numeric-value characteristic.
"""


class Characteristic:
    """Represents a HAP characteristic, the smallest unit of the smart home.

    A HAP characteristic is some measurement or state, like battery status or
    the current temperature. Characteristics are contained in services.
    Each characteristic has a unique type UUID and a set of properties,
    like format, min and max values, valid values and others.
    """

    __slots__ = ['display_name', 'type_id', 'properties', 'broker',
                 'setter_callback', 'value']

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
        if 'Format' not in properties or 'Permissions' not in properties:
            raise ValueError('Invalid properties')
        self.display_name = display_name
        self.type_id = type_id
        self.properties = properties
        self.broker = broker
        self.setter_callback = None
        self.value = value or self._get_default_value()

    def __repr__(self):
        """Return the representation of the characteristic."""
        return '<characteristic display_name={} value={} properties={}>' \
            .format(self.display_name, self.value, self.properties)

    def _get_default_value(self):
        """Helper method. Return default value for format."""
        if self.properties.get('ValidValues'):
            return min(self.properties['ValidValues'].values())
        else:
            value = HAP_FORMAT.DEFAULT[self.properties['Format']]
            return self._validate_value(value)

    def _validate_value(self, value):
        """Perform value validation depending on format."""
        if self.properties.get('ValidValues'):
            if value not in self.properties['ValidValues'].values():
                raise ValueError
            else:
                return value
        elif self.properties['Format'] == HAP_FORMAT.STRING:
            return str(value)[:256]
        elif self.properties['Format'] == HAP_FORMAT.NUMERIC:
            value = min(self.properties.get('maxValue', value), value)
            return max(self.properties.get('minValue', value), value)
        elif self.properties['Format'] == HAP_FORMAT.BOOL:
            return bool(value)
        elif self.properties['Format'] == HAP_FORMAT.ARRAY:
            # TODO: Add validation
            pass
        elif self.properties['Format'] == HAP_FORMAT.DICTIONARY:
            # TODO: Add validation
            pass
        elif self.properties['Format'] == HAP_FORMAT.DATA:
            # TODO: Add validation
            pass
        elif self.properties['Format'] == HAP_FORMAT.TLV8:
            # TODO: Add validation
            pass

        return value

    def set_value(self, value, should_notify=True, should_callback=True):
        """Set the given raw value.

        :param value: The value to assign as this Characteristic's value.
        :type value: Depends on properties["Format"]

        :param should_notify: Whether a the change should be sent to subscribed clients.
            The notification is called _after_ the setter callback.
            Notify will be performed if broker is set.
        :type should_notify: bool

        :param should_callback: Whether to invoke the callback, if such is set. This
            is useful in cases where you and HAP clients can both update the value and
            you don't want your callback called when you set the value, but want it
            called when clients do. Defaults to True.
        :type should_callback: bool

        :raise ValueError: When the value being assigned is not one of the valid values
            for this Characteristic.
        """
        self.value = self._validate_value(value)
        if self.setter_callback and should_callback:
            self.setter_callback(self.value)
        if self.broker and should_notify:
            self.notify()

    def override_properties(self, properties=None, valid_values=None):
        """Override characteristic property values and valid values.

        :param properties: Dictionary with values to override the existing
            properties. Only changed values are required.
        :type properties: dict

        :param valid_values: Dictionary with values to override the existing
            valid_values. Valid values will be set to new dictionary.
        :type valid_values: dict
        """
        if properties:
            self.properties.update(properties)

        if valid_values:
            self.properties['ValidValues'] = valid_values

    def notify(self):
        """Notify clients about a value change. Sends the value.

        .. note:: Non-blocking, i.e. does not wait for the update to be sent.
        .. seealso:: accessory_driver.publish

        :raise RuntimeError: When the broker is not set.
        """
        if not self.broker:
            raise RuntimeError('Notify failed, because broker is not set')

        data = {
            'type_id': self.type_id,
            'value': self.value,
        }
        self.broker.publish(data, self)

    def to_HAP(self, iid_manager):
        """Create a HAP representation of this Characteristic. Sends the value.

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

        if self.properties["Format"] in HAP_FORMAT.NUMERIC:
            value_info = {k: self.properties[k] for k in
                          self.properties.keys() & _HAP_NUMERIC_FIELDS}
        else:
            value_info = dict()

        if self.properties["Format"] == HAP_FORMAT.STRING:
            if len(self.value) > 64:
                value_info["maxLen"] = min(len(self.value), 256)
        if HAP_PERMISSIONS.READ in self.properties["Permissions"]:
            value_info["value"] = self.value

        hap_rep.update(value_info)
        return hap_rep
