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
    """Looks up type descriptions based on a name.

    .. seealso:: pyhap/resources/services.json
    .. seealso:: pyhap/resources/characteristics.json
    """

    def __init__(self, fp):
        """Initialise with the type descriptions in the given file.

        :param fp: File-like object to read from.
        :type fp: input stream
        """
        self.types = json.load(fp)

    def get(self, name):
        """Get type description with the given name."""
        return self.types[name].copy()

    def get_char(self, name):
        """Return new Characteristic object.

        :raise KeyError: When characteristic file did not contain necessary
            keys.
        """
        char_dict = self.get(name)
        if 'Format' not in char_dict or \
            'Permissions' not in char_dict or \
                'UUID' not in char_dict:
            raise KeyError('Could not load char {}!'.format(name))
        return Characteristic.from_dict(name, char_dict)

    def get_service(self, name, char_loader=None):
        """Return new service object.

        :param char_loader: `TypeLoader` object to use when creating the
            characteristics for adding to instantiated service.
        :type char_loader: TypeLoader

        :raise KeyError: When service file did not contain necessary keys.
        """
        char_loader = char_loader or get_char_loader()
        service_dict = self.get(name)
        if 'RequiredCharacteristics' not in service_dict or \
                'UUID' not in service_dict:
            raise KeyError('Could not load service {}!'.format(name))
        return Service.from_dict(name, service_dict, char_loader)


def get_char_loader(desc_file=CHARACTERISTICS_FILE):
    """Get a CharacteristicLoader with characteristic descriptions in the given file.

    Uses a 'singleton' when the file is `CHARACTERISTICS_FILE`.
    """
    global _char_loader
    if desc_file == CHARACTERISTICS_FILE:
        if _char_loader is None:
            with open(desc_file, 'r') as fp:
                _char_loader = TypeLoader(fp)
        return _char_loader

    with open(desc_file, 'r') as fp:
        ld = TypeLoader(fp)
    return ld


def get_serv_loader(desc_file=SERVICES_FILE):
    """Get a ServiceLoader with service descriptions in the given file.

    Uses a 'singleton' when the file is `SERVICES_FILE`.
    """
    global _serv_loader
    if desc_file == SERVICES_FILE:
        if _serv_loader is None:
            with open(desc_file, 'r') as fp:
                _serv_loader = TypeLoader(fp)
        return _serv_loader

    with open(desc_file, 'r') as fp:
        ld = TypeLoader(fp)
    return ld
