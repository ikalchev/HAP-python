from setuptools import setup

setup(
  name="HAP-python",
  description="HomeKit Accessory Protocol implementation in python",
  author="Ivan Kalchev",
  version="0.5",
  classifiers=[
     'Development Status :: 4 - Beta',
     'Programming Language :: Python :: 3'
  ],
  license="Apache-2.0",
  packages=[
     "pyhap",
        "pyhap.accessories"
  ],
  install_requires = [
     "pycryptodome",
     "tlslite-ng",
     "ed25519",
     "zeroconf",
     "curve25519-donna"
   ],                 
   package_data = {
      "pyhap" : ["resources/*"],
   }
)
