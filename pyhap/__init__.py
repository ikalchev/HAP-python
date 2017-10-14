import os

_ROOT = os.path.abspath(os.path.dirname(__file__))
_RESOURCE_DIR = os.path.join(_ROOT, "resources")

CHARACTERISTICS_FILE = os.path.join(_RESOURCE_DIR, "characteristics.json")
SERVICES_FILE = os.path.join(_RESOURCE_DIR, "services.json")
