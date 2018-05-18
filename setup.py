from setuptools import setup

setup(
    name="HAP-python",
    description="HomeKit Accessory Protocol implementation in python3",
    author="Ivan Kalchev",
    version="2.1.0",
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
    ],
    install_requires=[
        "curve25519-donna",
        "ed25519",
        "pycryptodome",
        "tlslite-ng",
        "zeroconf",
    ],
    extras_require={
        'QRCode': [
            'base36',
            'pyqrcode',
        ],
        "dev": [
            "pytest",
            "tox",
        ],
    },
    package_data={
        "pyhap": ["resources/*"],
    }
)
