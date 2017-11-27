class Service(object):
    """
    A representation of a HAP service.

    A Service contains multiple characteristics.
    """
    def __init__(self, type_id, display_name=None, subtype=None):
        self.display_name = display_name
        self.type_id = type_id
        self.subtype = subtype
        self.characteristics = []
        self.opt_characteristics = []
        # TODO: name characteristic

    def add_characteristic(self, *chars):
        for c in chars:
            if not any(c.type_id == oc.type_id for oc in self.characteristics):
                self.characteristics.append(c)

    def get_characteristic(self, name):
        char = next((c for c in self.characteristics if c.display_name == name),
                    None)
        assert char is not None
        return char

    def to_HAP(self, uuids):
        characteristics = [c.to_HAP(uuids) for c in self.characteristics]
        hap_rep = {
            "iid": uuids[self.type_id],
            "type": str(self.type_id).upper(),
            "characteristics": characteristics,
        }
        return hap_rep
