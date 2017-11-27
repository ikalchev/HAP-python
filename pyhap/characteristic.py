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


class Characteristic(object):

    def __init__(self, display_name, type_id, properties, value=None, broker=None):
        self.display_name = display_name
        self.type_id = type_id
        assert "format" in properties and "perms" in properties
        self.properties = properties
        self.value = value or HAP_FORMAT.DEFAULT[properties["format"]]
        self.broker = broker
        self.setter_callback = None

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

    def _value_to_HAP(self):
        hap_rep = {}

        if self.properties["format"] == HAP_FORMAT.STRING:
            val = self.value[:256]
            if len(self.value) > 64:
                hap_rep["maxLen"] = min(len(self.value), 256)
        elif self.properties["format"] in HAP_FORMAT.NUMERIC:
            if self.value > self.properties["max_value"]:
                val = self.properties["max_value"]
            else:
                val = max(self.value, self.properties["min_value"])
            hap_rep["maxValue"] = self.properties["max_value"]
            hap_rep["minValue"] = self.properties["min_value"]
            hap_rep["minStep"] = self.properties["min_step"]
            if "unit" in self.properties:
                hap_rep["unit"] = self.properties["unit"]
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
