.. _intro-install:

==================
Installation Guide
==================

Before We Begin
===============

HAP-python requires Python 3.4+.
This guide will cover the current version of Raspbian and Ubuntu LTS.
It is somewhat safe to assume the process for newer versions of Ubuntu
will work.


Installing Pre-Requisites
=========================

Raspbian Stretch
----------------

As a prerequisite, you will need Avahi/Bonjour installed (due to ``zeroconf`` package)::

    sudo apt install libavahi-compat-libdnssd-dev


Ubuntu 16.04 LTS
----------------

Same with Raspbian, we will need to install Avahi/Bonjour, but a fresh 16.04 install will
require the ``python3-dev`` package as well::

    sudo apt install libavahi-compat-libdnssd-dev python3-dev


Installing HAP-python
=====================

Make a directory for your project, and ``cd`` into it::

    ~ $ mkdir hk_project
    ~ $ cd hk_project
    ~/hk_project $

It is best to use a virtualenv for most Python projects, we can use one here as well.
Make sure that you have the ``venv`` module installed for Python 3::

    sudo apt install python3-venv

To create a virtualenv and activate it, simply run these commands inside your project
directory::

    python3 -m venv venv
    source venv/bin/activate

Because we used a Python 3 virtualenv and activated it, we can install ``HAP-python``
with ``pip``::

    pip install HAP-python
