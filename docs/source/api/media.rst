.. _api-media:

=============
pyhap.media
=============

The shared media core of the camera streams; see
:doc:`/media/architecture` for the design.

Media source
============

.. autoclass:: pyhap.media.source.MediaSource
   :members:
   :special-members: __init__

.. autoclass:: pyhap.media.source.MediaConsumer
   :members:

Source pool
===========

.. autoclass:: pyhap.media.pool.SourcePool
   :members:
   :special-members: __init__

.. autoclass:: pyhap.media.pool.ChannelLease
   :members:

Multi-tier RTP
==============

.. autoclass:: pyhap.media.multi_tier_delegate.MultiTierMediaDelegate
   :members:
   :special-members: __init__

.. autoclass:: pyhap.media.multi_tier_delegate.PooledSourceProvider
   :members:

.. autoclass:: pyhap.media.multi_tier.MultiTierSender
   :members:
   :special-members: __init__

Worker
======

.. autoclass:: pyhap.media.worker.MediaWorker
   :members:
   :special-members: __init__

.. autoclass:: pyhap.media.worker.RemoteMediaSource
   :members:
   :special-members: __init__

RTP and H.265
=============

.. autoclass:: pyhap.media.rtp.OutboundStream
   :members:

.. autoclass:: pyhap.media.hevc.HevcDepacketizer
   :members:

.. autoclass:: pyhap.media.hevc.HevcPacketizer
   :members:

FFmpeg provider
================

.. autoclass:: pyhap.media.ffmpeg.FFmpegSourceProvider
   :members:
   :special-members: __init__

.. autoclass:: pyhap.media.ffmpeg.FFmpegPull
   :members:

.. autoclass:: pyhap.media.ffmpeg.FFmpegInput
   :members:

Errors
======

.. autoclass:: pyhap.media.MediaError
   :members:

.. autoclass:: pyhap.media.worker.MediaWorkerError
   :members: