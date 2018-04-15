"""This module implements the HAP Service."""
from uuid import UUID

from .characteristic import Characteristic


class Service:
    """A representation of a HAP service.

    A Service contains multiple characteristics. For example, a
    TemperatureSensor service has the characteristic CurrentTemperature.
    """

    __slots__ = ('display_name', 'type_id', 'characteristics', 'broker')

    def __init__(self, type_id, display_name=None):
        self.display_name = display_name
        self.type_id = type_id
        self.characteristics = []
        self.broker = None

    def __repr__(self):
        """Return the representation of the service."""
        return "<service display_name='{}' chars={}>" \
            .format(self.display_name,
                    {c.display_name: c.value for c in self.characteristics})

    def add_characteristic(self, *chars):
        """Add the given characteristics as "mandatory" for this Service."""
        for char in chars:
            if not any(char.type_id == original_char.type_id
                    for original_char in self.characteristics):
                self.characteristics.append(char)

    def get_characteristic(self, name):
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
        raise ValueError('Characteristic not found')

    def configure_char(char_name, properties=None, valid_values=None,
                       value=None, setter_callback=None):
        """Helper method to return fully configured characteristic."""
        char = service.get_characteristic(char_name)
        if properties or valid_values:
            char.override_properties(properties)
        if value:
            char.set_value(value, should_notify=False)
        if setter_callback:
            char.setter_callback = setter_callback
        return char

    def to_HAP(self):
        """Create a HAP representation of this Service.

        :return: A HAP representation.
        :rtype: dict.
        """
        return {
            'iid': self.broker.iid_manager.get_iid(self),
            'type': str(self.type_id).upper(),
            'characteristics': [c.to_HAP() for c in self.characteristics],
        }

    @classmethod
    def from_dict(cls, name, json_dict, char_loader):
        """Initialize a service object from a dict."""
        type_id = UUID(json_dict.pop('UUID'))
        service = cls(type_id, name)
        for name in json_dict['RequiredCharacteristics']:
            service.add_characteristic(char_loader.get(name))
        return service
