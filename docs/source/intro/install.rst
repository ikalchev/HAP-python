.. _intro-install:

==================
Installation Guide
==================

Before We Begin
===============

HAP-python requires Python 3.11 or newer and is installed with ``pip``. All
of its dependencies ship binary wheels for the common platforms — Linux
(including Raspberry Pi OS on ARM), macOS and Windows — so no system
packages are needed for a plain install. If you are on a platform without
wheels for ``cryptography`` or ``pylibsrtp``, a C compiler and the OpenSSL /
libsrtp2 headers are needed to build them.


Installing HAP-python
=====================

Make a directory for your project, create a virtualenv and activate it::

    ~ $ mkdir hk_project
    ~ $ cd hk_project
    ~/hk_project $ python3 -m venv venv
    ~/hk_project $ source venv/bin/activate

Then install HAP-python with pip::

    (venv) ~/hk_project $ pip install HAP-python


Extras
======

To print a pairing QR code, install the ``QRCode`` extra::

    (venv) ~/hk_project $ pip install HAP-python[QRCode]

Without it, HAP-python still pairs — you enter the setup code by hand in
the Home app.


Verifying the install
=====================

From the project directory, run the bundled example accessory::

    (venv) ~/hk_project $ python main.py

It starts a temperature sensor on port 51826, prints a setup code (or QR
code with the extra installed) and advertises itself over mDNS — you can
pair with it from the Home app. Continue with :doc:`tutorial`.