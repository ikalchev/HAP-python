#!/usr/bin/env python3
from setuptools import setup

import pyhap.const as pyhap_const

NAME = "HAP-python"
DESCRIPTION = "HomeKit Accessory Protocol implementation in python"
URL = "https://github.com/ikalchev/{}".format(NAME)
AUTHOR = "Ivan Kalchev"


PROJECT_URLS = {
    "Bug Reports": "{}/issues".format(URL),
    "Documentation": "http://hap-python.readthedocs.io/en/latest/",
    "Source": "{}/tree/master".format(URL),
}


MIN_PY_VERSION = ".".join(map(str, pyhap_const.REQUIRED_PYTHON_VER))

with open("README.md", "r", encoding="utf-8") as f:
    README = f.read()


REQUIRES = ["cryptography", "zeroconf>=0.32.0", "h11"]


setup(
    name=NAME,
    version=pyhap_const.__version__,
    description=DESCRIPTION,
    long_description=README,
    long_description_content_type="text/markdown",
    url=URL,
    packages=["pyhap"],
    include_package_data=True,
    project_urls=PROJECT_URLS,
    python_requires=">={}".format(MIN_PY_VERSION),
    install_requires=REQUIRES,
    license="Apache License 2.0",
    license_file="LICENSE",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    extras_require={
        "QRCode": ["base36", "pyqrcode"],
    },
)
