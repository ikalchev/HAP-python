# Various classes that construct representations of
# HAP services and characteristics from a json
# representation.
#
# The idea is, give a name of a service and you get an
# instance of it (as long as it is described in some
# json file).
import uuid
import json

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import Characteristic
from pyhap.service import Service

# Because we are loading mostly from the characteristics.json
# and services.json files, these loaders are "cached".
_char_loader = None
_serv_loader = None


class TypeLoader(object):

    def __init__(self, fp):
        self.types = json.load(fp)

    def get(self, name):
        return self.types[name].copy()


class CharLoader(TypeLoader):

    def get(self, name, char_class=Characteristic):
        char_info = super(CharLoader, self).get(name)
        type_id = uuid.UUID(char_info["UUID"])
        props = char_info
        props.pop("UUID")

        return char_class(name, type_id, props)


class ServiceLoader(TypeLoader):

    def __init__(self, desc_file, char_loader=None):
        super(ServiceLoader, self).__init__(desc_file)
        self.char_loader = char_loader or get_char_loader()

    def get(self, name):
        serv_info = super(ServiceLoader, self).get(name)
        type_id = uuid.UUID(serv_info["UUID"])
        s = Service(type_id, name)
        chars = [self.char_loader.get(c) for c in serv_info["RequiredCharacteristics"]]
        s.add_characteristic(*chars)
        return s


def get_char_loader(desc_file=CHARACTERISTICS_FILE):
    global _char_loader
    if desc_file == CHARACTERISTICS_FILE:
        if _char_loader is None:
            with open(desc_file, "r") as fp:
                _char_loader = CharLoader(fp)
        return _char_loader

    with open(desc_file, "r") as fp:
        ld = CharLoader(fp)
    return ld


def get_serv_loader(desc_file=SERVICES_FILE):
    global _serv_loader
    if desc_file == SERVICES_FILE:
        if _serv_loader is None:
            with open(desc_file, "r") as fp:
                _serv_loader = ServiceLoader(fp)
        return _serv_loader

    with open(desc_file, "r") as fp:
        ld = ServiceLoader(fp)
    return ld
