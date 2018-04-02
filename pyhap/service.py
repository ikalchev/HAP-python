"""This module implements the HAP Service."""


class Service(object):
    """A representation of a HAP service.

    A Service contains multiple characteristics. For example, a
    TemperatureSensor service has the characteristic CurrentTemperature.

    When you load a service using the loader module, only the required
    characteristics are added - you need to add any optional characteristics
    yourself. This is because once a characteristic is present, iOS will want
    values for it and we need to know how to set these.
    """
    def __init__(self, type_id, display_name=None):
        self.display_name = display_name
        self.type_id = type_id
        self.characteristics = []
        # TODO: name characteristic

    def __repr__(self):
        """Return the representation of the service."""
        return "<service display_name='{}' chars={}>" \
            .format(self.display_name,
                    {c.display_name: c.value for c in self.characteristics})

    def _add_chars(self, container, *chars):
        """Helper method to add the given characteristics to the given container."""
        for c in chars:
            if not any(c.type_id == oc.type_id for oc in container):
                container.append(c)

    def add_characteristic(self, *chars):
        """Add the given characteristics as "mandatory" for this Service."""
        self._add_chars(self.characteristics, *chars)

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

    def to_HAP(self, iid_manager=None):
        """Create a HAP representation of this Service.

        :param base_iid: The IID for this Service, as assigned from the Accessory.
        :type base_iid: int

        :return: A HAP representation.
        :rtype: dict.
        """
        assert iid_manager is not None
        characteristics = [c.toHAP() for c in self.characteristics]

        hap_rep = {
            "iid": iid_manager.get_iid(self),
            "type": str(self.type_id).upper(),
            "characteristics": characteristics,
        }
        return hap_rep
