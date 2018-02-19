"""
Various classes that construct representations of
HAP services and characteristics from a json
representation.

The idea is, give a name of a service and you get an
instance of it (as long as it is described in some
json file).
"""
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
    """Looks up type descriptions based on a name.
    """

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

    def __init__(self, fp, char_class=Characteristic):
        """Initialise to look into the given file for characteristics.

        :param fp: File-like object to read from.
        :type fp: input stream

        :param char_class: The class which to instantiate when creating Characteristics.
            Defaults to `Characteristic`.
        :type char_class: type
        """
        super(CharLoader, self).__init__(fp)
        self.char_class = char_class

    def get(self, name, char_class=None):
        """Instantiate and return a `char_class` with the given name.

        This method looks into the json-described characteristics read during init
        and attempts to find the description of a characteristic with the given name.
        If successful, uses the description to instantiate `char_class` or, if not given,
        `self.char_class`.

        :param name: Name of the characteristic to look for.
        :type name: str

        :param char_class: The class which to instantiate when creating `Characteristics`.
            Defaults to `None`, in which case `self.char_class`.
        :type char_class: type

        :return: Instantiated Characteristic.
        :rtype: char_class or self.char_class

        :raise: KeyError when no characteristic description can be found with the
            given name.
        """
        char_info = super(CharLoader, self).get(name)
        type_id = uuid.UUID(char_info["UUID"])
        props = char_info
        props.pop("UUID")
        char_type = char_class or self.char_class
        return char_type(name, type_id, props)


class ServiceLoader(TypeLoader):
    """Creates Service objects based on a description.

    .. seealso:: pyhap/resources/services.json
    """

    def __init__(self, fp, char_loader=None, service_class=Service):
        """Initialise to look into the given file for services.

        :param fp: File-like object to read from.
        :type fp: input stream

        :param char_loader: `TypeLoader` object to use when creating the
            characteristics for adding to instantiated services.
        :type char_loader: TypeLoader

        :param service_class: The class which to instantiate when creating services.
            Defaults to `Service`.
        :type char_class: type
        """
        super(ServiceLoader, self).__init__(fp)
        self.char_loader = char_loader or get_char_loader()
        self.service_class = service_class

    def get(self, name, service_class=None):
        """Instantiate and return a `service_class` with the given name.

        This method looks into the described services read during init
        and attempts to find the description of a service with the given name.
        If successful, uses the description to instantiate `service_class` or, if not given,
        `self.service_class`. It then adds all required characteristics to the service, as
        specified in the description for this `Service`.

        :param name: Name of the service to look for.
        :type name: str

        :param char_class: The class which to instantiate when creating `Service`.
            Defaults to `None`, in which case `self.service_class`.
        :type char_class: type

        :return: Instantiated Service.
        :rtype: service_class or self.service_class

        :raise KeyError: when no service description can be found with the
            given name.
        """
        serv_info = super(ServiceLoader, self).get(name)
        type_id = uuid.UUID(serv_info["UUID"])
        service_type = service_class or self.service_class
        s = service_type(type_id, name)
        chars = [self.char_loader.get(c) for c in serv_info["RequiredCharacteristics"]]
        s.add_characteristic(*chars)
        return s


def get_char_loader(desc_file=CHARACTERISTICS_FILE):
    """Get a CharacteristicLoader with characteristic descriptions in the given file.

    Uses a "singleton" when the file is `CHARACTERISTICS_FILE`.
    """
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
    """Get a ServiceLoader with service descriptions in the given file.

    Uses a "singleton" when the file is `SERVICES_FILE`.
    """
    global _serv_loader
    if desc_file == SERVICES_FILE:
        if _serv_loader is None:
            with open(desc_file, "r") as fp:
                _serv_loader = ServiceLoader(fp)
        return _serv_loader

    with open(desc_file, "r") as fp:
        ld = ServiceLoader(fp)
    return ld
