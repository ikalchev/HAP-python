import os

_ROOT = os.path.abspath(os.path.dirname(__file__))
_RESOURCE_DIR = os.path.join(_ROOT, "resources")

CHARACTERISTICS_FILE = os.path.join(_RESOURCE_DIR, "characteristics.json")
SERVICES_FILE = os.path.join(_RESOURCE_DIR, "services.json")

HAP_PYTHON_VERSION = (2, 0, 0)
"""
HAP-python current version.
"""


SUPPORT_QR_CODE = False
try:
    import base36 as _
    import pyqrcode as _
    SUPPORT_QR_CODE = True
except ImportError:
    pass
"""
Flag if QR Code dependencies are installed.
Installation with `pip install HAP-python[QRCode]`.
"""
