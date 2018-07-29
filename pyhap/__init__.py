import os

ROOT = os.path.abspath(os.path.dirname(__file__))
RESOURCE_DIR = os.path.join(ROOT, "resources")

CHARACTERISTICS_FILE = os.path.join(RESOURCE_DIR, "characteristics.json")
SERVICES_FILE = os.path.join(RESOURCE_DIR, "services.json")

HAP_PYTHON_VERSION = (1, 1, 9)
"""
HAP-python current version.
"""
