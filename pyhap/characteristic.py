# All things for a HAP characteristic.


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


"""Fields that should be included in the HAP representation of the characteristic.

That is, if they are present in the specification of a numeric-value characteristic.
"""
_HAP_NUMERIC_FIELDS = {"maxValue", "minValue", "minStep", "unit"}


class Characteristic(object):

    def __init__(self, display_name, type_id, properties, value=None, broker=None):
        self.display_name = display_name
        self.type_id = type_id
        assert "format" in properties and "perms" in properties
        self.properties = properties
        self.value = value or HAP_FORMAT.DEFAULT[properties["format"]]
        self.broker = broker
        self.setter_callback = None
        self.hap_value_template = self._create_value_HAP_template()

    def set_value(self, value, should_notify=True):
        self.value = value
        if self.setter_callback is not None:
            self.setter_callback(value)
        if should_notify:
            self.notify()

    def get_value(self):
        return self.value

    def notify(self):
        data = {
          "type_id": self.type_id,
          "value": self.value,
        }
        self.broker.publish(data)

    def _create_value_HAP_template(self):
        template = dict()
        if self.properties["format"] in HAP_FORMAT.NUMERIC:
            template = {k: self.properties[k]
                        for k in self.properties.keys() & _HAP_NUMERIC_FIELDS}
        return template

    def _value_to_HAP(self):
        hap_rep = self.hap_value_template.copy()

        if self.properties["format"] == HAP_FORMAT.STRING:
            val = self.value[:256]
            if len(self.value) > 64:
                hap_rep["maxLen"] = min(len(self.value), 256)
        elif self.properties["format"] in HAP_FORMAT.NUMERIC:
            val = self.value
            if "maxValue" in hap_rep:
                val = min(self.properties["maxValue"], self.value)
            if "minValue" in hap_rep:
                val = max(self.properties["minValue"], self.value)
        else:
            val = self.value

        if HAP_PERMISSIONS.READ in self.properties["perms"]:
            hap_rep["value"] = val

        return hap_rep

    def to_HAP(self, uuids):

        hap_rep = {
            "iid": uuids[self.type_id],
            "type": str(self.type_id).upper(),
            "description": self.display_name,
            "perms": self.properties["perms"],
            "format": self.properties["format"],
        }
        hap_rep.update(self._value_to_HAP())

        return hap_rep
