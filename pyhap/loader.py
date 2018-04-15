"""
Various classes that construct representations of
HAP services and characteristics from a json
representation.

The idea is, give a name of a service and you get an
instance of it (as long as it is described in some
json file).
"""
import json

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import Characteristic
from pyhap.service import Service

# Because we are loading mostly from the characteristics.json
# and services.json files, these loaders are "cached".
_char_loader = None
_serv_loader = None


class TypeLoader:
    """Looks up type descriptions based on a name."""

    def __init__(self, fp):
        """Initialise with the type descriptions in the given file.

        :param fp: File-like object to read from.
        :type fp: input stream
        """
        self.types = json.load(fp)

    def get(self, name):
        """Get type description with the given name.

        :rtype: dict
        """
        return self.types[name].copy()


class CharLoader(TypeLoader):
    """Creates Characteristic objects based on a description.

    .. seealso:: pyhap/resources/characteristics.json
    """

    def get(self, name):
        """Instantiate and return a `char_class` with the given name.

        :param name: Name of the characteristic to look for.
        :type name: str

        :return: Instantiated Characteristic.
        :rtype: Characteristic object
        """
        json_dict = super().get(name)
        return Characteristic.from_dict(name, json_dict)


class ServiceLoader(TypeLoader):
    """Creates Service objects based on a description.

    .. seealso:: pyhap/resources/services.json
    """

    def __init__(self, fp, char_loader=None):
        """Initialise to look into the given file for services.

        :param fp: File-like object to read from.
        :type fp: input stream

        :param char_loader: `TypeLoader` object to use when creating the
            characteristics for adding to instantiated services.
        :type char_loader: TypeLoader
        """
        super().__init__(fp)
        self.char_loader = char_loader or get_char_loader()

    def get(self, name):
        """Instantiate and return a Service object with the given name.

        :param name: Name of the service to look for.
        :type name: str

        :return: Instantiated Service.
        :rtype: Service object
        """
        json_dict = super().get(name)
        return Service.from_dict(name, json_dict, self.char_loader)


def get_char_loader(desc_file=CHARACTERISTICS_FILE):
    """Get a CharacteristicLoader with characteristic descriptions in the given file.

    Uses a 'singleton' when the file is `CHARACTERISTICS_FILE`.
    """
    global _char_loader
    if desc_file == CHARACTERISTICS_FILE:
        if _char_loader is None:
            with open(desc_file, 'r') as fp:
                _char_loader = CharLoader(fp)
        return _char_loader

    with open(desc_file, 'r') as fp:
        ld = CharLoader(fp)
    return ld


def get_serv_loader(desc_file=SERVICES_FILE):
    """Get a ServiceLoader with service descriptions in the given file.

    Uses a 'singleton' when the file is `SERVICES_FILE`.
    """
    global _serv_loader
    if desc_file == SERVICES_FILE:
        if _serv_loader is None:
            with open(desc_file, 'r') as fp:
                _serv_loader = ServiceLoader(fp)
        return _serv_loader

    with open(desc_file, 'r') as fp:
        ld = ServiceLoader(fp)
    return ld
