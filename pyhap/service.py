"""This module implements the HAP Service."""


class Service(object):
    """A representation of a HAP service.

    A Service contains multiple characteristics. For example, a TemperatureSensor service
    has the characteristic CurrentTemperature.

    For the services that iOS natively supports, we distinguish two types of
    characteristics - (1) "mandatory", which must be present when communciating with an
    iOS client, and (2) optional, which may or may not be present. All mandatory and
    optional characteristics for iOS-supported services are defined in the
    resources/services.json file. When you load a service using the loader module, only
    the mandatory characteristics are aded - you need to add any optional characteristics
    yourself. This is because once a charateristic is present, iOS will want values for
    it and we need to know how to set these.
    """
    def __init__(self, type_id, display_name=None):
        self.display_name = display_name
        self.type_id = type_id
        self.characteristics = []
        self.opt_characteristics = []
        # TODO: name characteristic

    def _add_chars(self, container, *chars):
        """Helper method to add the given characteristics to the given container."""
        for c in chars:
            if not any(c.type_id == oc.type_id for oc in container):
                container.append(c)

    def add_characteristic(self, *chars):
        """Add the given characteristics as "mandatory" for this Service."""
        self._add_chars(self.characteristics, *chars)

    def add_opt_characteristic(self, *chars):
        """Add the given characteristics as optional for this Service."""
        self._add_chars(self.opt_characteristics, *chars)

    def get_characteristic(self, name, check_optional=True):
        """Return a Characteristic object by the given name from this Service.

        Checks only the mandatory characteristics by default.

        @param name: The name of the characteristic to search for.
        @type name: str

        @param check_optional: Whether to search in the optional characteristics as well.
        @type check_optional: bool

        @return: A characteristic with the given name or None if not found.
        @rtype: Characteristic
        """
        char = next((c for c in self.characteristics if c.display_name == name),
                    None)
        if char is None and check_optional:
            char = next((c for c in self.opt_characteristics if c.display_name == name),
                        None)
        assert char is not None
        return char

    def to_HAP(self, iid_manager=None):
        """Create a HAP representation of this Service.

        @param base_iid: The IID for this Service, as assigned from the Accessory.
        @type base_iid: int

        @return: A HAP representation.
        @rtype: dict.
        """
        assert iid_manager is not None
        characteristics = [c.to_HAP(iid_manager)
                           for c in self.characteristics + self.opt_characteristics]

        hap_rep = {
            "iid": iid_manager.get_iid(self),
            "type": str(self.type_id).upper(),
            "characteristics": characteristics,
        }
        return hap_rep
