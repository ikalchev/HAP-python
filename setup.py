from setuptools import setup

setup(
    name="HAP-python",
    description="HomeKit Accessory Protocol implementation in python3",
    author="Ivan Kalchev",
    version="1.1.9",
    url="https://github.com/ikalchev/HAP-python.git",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "License :: OSI Approved :: Apache Software License",
        "Intended Audience :: Developers",
    ],
    license="Apache-2.0",
    packages=[
        "pyhap",
        "pyhap.accessories"
    ],
    install_requires=[
        "base36",
        "curve25519-donna",
        "ed25519",
        "pycryptodome",
        "pyqrcode",
        "tlslite-ng",
        "zeroconf",
    ],
    extras_require={
        "dev": [
            "pytest",
            "tox",
        ],
    },
    package_data={
        "pyhap": ["resources/*"],
    }
)
