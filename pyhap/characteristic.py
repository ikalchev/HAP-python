"""
All things for a HAP characteristic.

A Characteristic is the smallest unit of the smart home, e.g.
a temperature measuring or a device status.
"""
import logging

logger = logging.getLogger(__name__)


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


class Characteristic:
    """Represents a HAP characteristic, the smallest unit of the smart home.

    A HAP characteristic is some measurement or state, like battery status or
    the current temperature. Characteristics are contained in services.
    Each characteristic has a unique type UUID and a set of properties,
    like format, min and max values, valid values and others.
    """

    __slots__ = ('display_name', 'type_id', 'properties', 'broker',
                 'setter_callback', 'value')

    def __init__(self, display_name, type_id, properties):
        """Initialise with the given properties.

        :param display_name: Name that will be displayed for this characteristic, i.e.
            the `description` in the HAP representation.
        :type display_name: str

        :param type_id: UUID unique to this type of characteristic.
        :type type_id: uuid.UUID

        :param properties: A dict of properties, such as Format, ValidValues, etc.
        :type properties: dict
        """
        self.display_name = display_name
        self.type_id = type_id
        self.properties = properties
        self.broker = None
        self.setter_callback = None
        self.value = self._get_default_value()

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
            return self.to_valid_value(value)

    def to_valid_value(self, value):
        """Perform validation and conversion to valid value"""
        if self.properties.get('ValidValues'):
            if value not in self.properties['ValidValues'].values():
                logger.error('%s: value={} is an invalid value.',
                             self.display_name, value)
                raise ValueError('{}: value={} is an invalid value.'
                                 .format(self.display_name, value))
        elif self.properties['Format'] == HAP_FORMAT.STRING:
            value = str(value)[:256]
        elif self.properties['Format'] == HAP_FORMAT.BOOL:
            value = bool(value)
        elif self.properties['Format'] in HAP_FORMAT.NUMERIC:
            if not isinstance(value, (int, float)):
                logger.error('%s: value=%s is not a numeric value.',
                             self.display_name, value)
                raise ValueError('{}: value={} is not a numeric value.'
                                 .format(self.display_name, value))
            value = min(self.properties.get('maxValue', value), value)
            value = max(self.properties.get('minValue', value), value)
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
        if properties:
            self.properties.update(properties)

        if valid_values:
            self.properties['ValidValues'] = valid_values

    def set_value(self, value, should_notify=True):
        """Set the given raw value. It is checked if it is a valid value.

        If not set_value will be aborted and an error message will be
        displayed.

        :param value: The value to assign as this Characteristic's value.
        :type value: Depends on properties["Format"]

        :param should_notify: Whether a the change should be sent to
            subscribed clients. Notify will be performed if the broker is set.
        :type should_notify: bool
        """
        logger.debug('%s: Set value to %s', self.display_name, value)
        value = self.to_valid_value(value)
        self.value = value
        if should_notify and self.broker:
            self.notify()

    def client_update_value(self, value):
        """Called from broker for value change in Home app.

        Change self.value to value and call callback.
        """
        logger.debug('%s: Client update value to %s',
                      self.display_name, value)
        self.value = value
        self.notify()
        if self.setter_callback:
            self.setter_callback(value)

    def notify(self):
        """Notify clients about a value change. Sends the value.

        .. seealso:: accessory.publish
        .. seealso:: accessory_driver.publish
        """
        self.broker.publish(self.value, self)

    def to_HAP(self):
        """Create a HAP representation of this Characteristic.

        Used for json serialization.

        :return: A HAP representation.
        :rtype: dict
        """
        hap_rep = {
            'iid': self.broker.iid_manager.get_iid(self),
            'type': str(self.type_id).upper(),
            'description': self.display_name,
            'perms': self.properties['Permissions'],
            'format': self.properties['Format'],
        }

        if self.properties['Format'] in HAP_FORMAT.NUMERIC:
            hap_rep.update({k: self.properties[k] for k in
                            self.properties.keys() & 
                            ('maxValue', 'minValue', 'minStep', 'unit')})
        elif self.properties['Format'] == HAP_FORMAT.STRING:
            if len(self.value) > 64:
                hap_rep['maxLen'] = min(len(self.value), 256)
        if HAP_PERMISSIONS.READ in self.properties['Permissions']:
            hap_rep['value'] = self.value

        return hap_rep
