.. _api:

=================
API Documentation
=================

Accessory
=========

Base class for HAP Accessories.

.. autoclass:: pyhap.accessory.Accessory
   :members:


AccessoryDriver
===============

Accessory Driver class to host an Accessory.

.. autoclass:: pyhap.accessory_driver.AccessoryDriver
   :members:


Bridge
======

Bridge Class to host multiple HAP Accessories.

.. autoclass:: pyhap.accessory.Bridge
   :members:


Characteristic
==============

Characteristic Base class for a HAP Accessory ``Service``.

.. seealso:: pyhap.service.Service

.. autoclass:: pyhap.characteristic.Characteristic
   :members:


CharLoader
==========

Useful for loading ``Characteristic`` for a ``Service``.

.. autoclass:: pyhap.loader.CharLoader


Service
=======

Service Base class for a HAP ``Accessory``.

.. autoclass:: pyhap.service.Service
   :members:


ServiceLoader
=============

Useful for creating a ``Service``.

.. autoclass:: pyhap.loader.ServiceLoader
   :members:


Util
====

Utilities Module

.. automodule:: pyhap.util
   :members:
   :undoc-members:
