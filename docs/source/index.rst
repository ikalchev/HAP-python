======================================
Welcome to HAP-python's documentation!
======================================

This documentation contains everything you need to know about
HAP-python.

Getting Help
============

Having Trouble? Post an issue on the GitHub repo, with as much
information as possible about your issue.

.. _issue tracker: https://github.com/ikalchev/HAP-python/issues


First Steps
===========

.. toctree::
   :maxdepth: 2
   :hidden:

   intro/overview
   intro/install
   intro/tutorial
   intro/examples

:doc:`intro/overview`
    HAP-python at a glance: the object model, and how the accessory,
    services, characteristics and the driver interact.

:doc:`intro/install`
    How to install HAP-python on a Debian based system, such as
    a Raspberry Pi, or Ubuntu.

:doc:`intro/tutorial`
    Get started building your first HomeKit Accessory.

:doc:`intro/examples`
    A set of prebuilt accessories to either build your own class
    around, or to use as a starting point into your own custom
    Accessory class.


Advanced
========

.. toctree::
   :maxdepth: 2
   :hidden:

   advanced/state
   advanced/async

:doc:`advanced/state`
    The state file: what is persisted, when it is loaded and saved, and
    how your accessory keeps its own settings across restarts.

:doc:`advanced/async`
    The event loop rules: what runs where, what must not block, and the
    scheduling utilities HAP-python provides.


Camera Streaming
================

.. toctree::
   :maxdepth: 2
   :hidden:

   media/camera
   media/architecture

:doc:`media/camera`
    The Camera accessory: classic RTP streaming, HomeKit Secure Video
    recording, and the 17.99 multi-tier and WebRTC stream services with
    their delegates and sessions.

:doc:`media/architecture`
    How the camera stream services share one media pull per channel and
    keep the per-packet work off the caller's loop.


API Reference
=============

.. toctree::
   :maxdepth: 2
   :hidden:

   api/index

:doc:`api/index`
    API documentation for HAP-python.