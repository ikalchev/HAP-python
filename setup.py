from setuptools import setup

import pyhap.const as pyhap_const


PROJECT_NAME = 'HAP-python'
URL = 'https://github.com/ikalchev/{}'.format(PROJECT_NAME)
PROJECT_URLS = {
    'Bug Reports': '{}/issues'.format(URL),
    'Documentation': 'http://hap-python.readthedocs.io/en/latest/',
    'Source': '{}/tree/master'.format(URL),
}

PYPI_URL = 'https://pypi.python.org/pypi/{}'.format(PROJECT_NAME)
DOWNLOAD_URL = '{}/archive/{}.zip'.format(URL, pyhap_const.__version__)

MIN_PY_VERSION = '.'.join(map(str, pyhap_const.REQUIRED_PYTHON_VER))

setup(
    name=PROJECT_NAME,
    version=pyhap_const.__version__,
    url=URL,
    project_urls=PROJECT_URLS,
    download_url=DOWNLOAD_URL,
    python_requires='>={}'.format(MIN_PY_VERSION),
)
