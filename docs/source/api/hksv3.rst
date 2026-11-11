.. _api-hksv3:

=========================
HKSV 17.99 types
=========================

The TLV types of the HomeKit Secure Video 17.99 camera services: sensor
configuration, stream tiers, motion zones and the WebRTC session messages.
The delegates of :doc:`/media/camera` exchange these with the camera services.

.. note::
   HomeKit Secure Video 17.99 is a Developer Preview: this API is
   experimental and may change.

Sensors, tiers and zones
========================

.. autoclass:: pyhap.hksv3.SensorType
   :members:

.. autoclass:: pyhap.hksv3.SensorIntent
   :members:

.. autoclass:: pyhap.hksv3.SensorConfiguration
   :members:

.. autoclass:: pyhap.hksv3.VideoStreamCapability
   :members:

.. autoclass:: pyhap.hksv3.VideoQuality
   :members:

.. autoclass:: pyhap.hksv3.VideoStreamTier
   :members:

.. autoclass:: pyhap.hksv3.AudioStreamTier
   :members:

.. autoclass:: pyhap.hksv3.CameraCapabilities
   :members:

.. autoclass:: pyhap.hksv3.ZonePolygon
   :members:

.. autoclass:: pyhap.hksv3.ZoneData
   :members:

.. autoclass:: pyhap.hksv3.CameraZones
   :members:

Multi-tier RTP streaming
========================

.. autoclass:: pyhap.hksv3.RTPStreamingCommand
   :members:

.. autoclass:: pyhap.hksv3.RTPStreamingStatus
   :members:

.. autoclass:: pyhap.hksv3.RTPStreamingControl
   :members:

.. autoclass:: pyhap.hksv3.SetupEndpointsStatus
   :members:

.. autoclass:: pyhap.hksv3.EndpointAddress
   :members:

.. autoclass:: pyhap.hksv3.SRTPParameters
   :members:

.. autoclass:: pyhap.hksv3.SRTPCryptoSuite
   :members:

WebRTC streaming
================

.. autoclass:: pyhap.hksv3.WebRTCStreamingCommand
   :members:

.. autoclass:: pyhap.hksv3.WebRTCStreamingStatus
   :members:

.. autoclass:: pyhap.hksv3.WebRTCOffer
   :members:

.. autoclass:: pyhap.hksv3.WebRTCOfferOptions
   :members:

.. autoclass:: pyhap.hksv3.WebRTCReofferAnswer
   :members:

.. autoclass:: pyhap.hksv3.IceCandidate
   :members:

.. autoclass:: pyhap.hksv3.SFrameKey
   :members: