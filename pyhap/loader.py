"""
Various classes that construct representations of
HAP services and characteristics from a json
representation.

The idea is, give a name of a service and you get an
instance of it (as long as it is described in some
json file).
"""
import json
import logging

from pyhap import CHARACTERISTICS_FILE, SERVICES_FILE
from pyhap.characteristic import Characteristic
from pyhap.service import Service

_loader = None
logger = logging.getLogger(__name__)


class Loader:
    """Looks up type descriptions based on a name.

    .. seealso:: pyhap/resources/services.json
    .. seealso:: pyhap/resources/characteristics.json
    """

    def __init__(self, path_char=CHARACTERISTICS_FILE, path_service=SERVICES_FILE):
        """Initialize a new Loader instance."""
        self.char_types = self._read_file(path_char)
        self.serv_types = self._read_file(path_service)

    @staticmethod
    def _read_file(path):
        """Read file and return a dict."""
        with open(path, "r", encoding="utf8") as file:
            return json.load(file)

    def get_char(self, name):
        """Return new Characteristic object."""
        char_dict = self.char_types[name].copy()
        if (
            "Format" not in char_dict
            or "Permissions" not in char_dict
            or "UUID" not in char_dict
        ):
            raise KeyError("Could not load char {}!".format(name))
        return Characteristic.from_dict(name, char_dict, from_loader=True)

    def get_service(self, name):
        """Return new service object."""
        service_dict = self.serv_types[name].copy()
        if "RequiredCharacteristics" not in service_dict or "UUID" not in service_dict:
            raise KeyError("Could not load service {}!".format(name))
        return Service.from_dict(name, service_dict, self)

    @classmethod
    def from_dict(cls, char_dict=None, serv_dict=None):
        """Create a new instance directly from json dicts."""
        loader = cls.__new__(Loader)
        loader.char_types = char_dict or {}
        loader.serv_types = serv_dict or {}
        return loader


def get_loader():
    """Get a service and char loader.

    If already initialized it returns the existing one.
    """
    # pylint: disable=global-statement
    global _loader
    if _loader is None:
        _loader = Loader()
    return _loader
