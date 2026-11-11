.. _media-camera:

=================
Camera streaming
=================

:class:`pyhap.camera.Camera` is an Accessory that negotiates camera stream
settings with a HAP controller and leaves the media itself to you. The
negotiation — which codecs, resolutions and security parameters the camera
supports, which session the controller wants, when it starts and stops — is
HAP-python's; producing the video and audio is your part, either by
overriding a few methods or by implementing a *delegate*.

.. note::
   HomeKit Secure Video 17.99 is a **Developer Preview** of the guide: the
   multi-tier RTP and WebRTC services described here are experimental and
   their API may change.

.. contents:: On this page
   :local:
   :depth: 1

The camera and its options
===========================

A ``Camera`` is built from an ``options`` dict; each key switches on a part
of the camera (see :meth:`Camera.__init__ <pyhap.camera.Camera.__init__>` for
the full list):

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Option
     - What it adds
   * - ``video``, ``audio``
     - The classic ``CameraRTPStreamManagement`` service: live view over
       RTP/SRTP, negotiated per session.
   * - ``recording``
     - HomeKit Secure Video recording: the controller configures what to
       record, and the camera uploads fragments over the HomeKit Data Stream.
   * - ``sensors``
     - The 17.99 camera capabilities and motion zones: the image sensors of
       the camera (e.g. one motion sensor, or one per zone), each with its
       own stream services.
   * - ``multi_tier``
     - A 17.99 multi-tier RTP stream management service per sensor — H.265
       quality tiers the controller switches between.
   * - ``webrtc``
     - A 17.99 WebRTC stream management service per sensor — the path remote
       viewing takes.
   * - ``talkback``
     - Two-way audio: a ``Speaker`` service and a UDP channel the controller
       sends its audio to.
   * - ``global_operating_mode``
     - The 17.99 global operating mode service (e.g.
       ``ManuallyDisabled``, ``NightVision``); while streaming is disabled
       the camera refuses to set up live streams.

Classic RTP live view
=====================

With ``video`` and ``audio`` options, the camera exposes the
``SelectedRTPStreamConfiguration`` flow: the controller writes its address
and SRTP keys to ``SetupEndpoints``, the camera answers, and a write to the
selected configuration starts the stream. HAP-python hands each session to
your methods:

* :meth:`Camera.start_stream <pyhap.camera.Camera.start_stream>` — start
  sending media with the negotiated parameters (resolution, ports, SRTP
  keys, …). The default runs the ``start_stream_cmd`` command (an ffmpeg
  template) with the negotiated parameters substituted in.
* :meth:`Camera.reconfigure_stream <pyhap.camera.Camera.reconfigure_stream>`
  — switch parameters of a running stream.
* :meth:`Camera.stop_stream <pyhap.camera.Camera.stop_stream>` — end it.
* :meth:`Camera.get_snapshot <pyhap.camera.Camera.get_snapshot>` — serve a
  still image in the requested size.

All of the media hooks may be coroutines; a controller's request waits for
them, so they must answer quickly (see :doc:`/advanced/async`).

HomeKit Secure Video recording
==============================

With ``recording``, the controller configures what to record — the container,
video and audio settings — through the ``CameraRecordingManagement`` service.
When it turns recording on, your camera produces the fragments:

.. code-block:: python

    async def handle_recording_stream(self, stream_id):
        # the first packet is the MP4 init segment
        yield RecordingPacket(metadata=..., data=...)
        while ...:
            fragment = await read_fragment()
            yield RecordingPacket(metadata=..., data=fragment)

The async generator yields fragmented-MP4 packets
(:class:`pyhap.hds_recording.RecordingPacket`); HAP-python sets up the HomeKit
Data Stream connection with the controller and sends them, encrypted, as
they come. The controller decides when recording is active; your code
decides what a fragment contains. Motion and doorbell events are signalled
through the camera's sensor characteristics, which your code sets like any
other value.

The 17.99 stream services
==========================

The ``sensors``, ``multi_tier`` and ``webrtc`` options add the newer stream
services, one set per image sensor. A controller drives them through the same
write/answer handshake, but the media is no longer a method override — it is
a **delegate**, one object per camera:

* :class:`~pyhap.camera_multi_tier.MultiTierDelegate` sends the media of
  multi-tier RTP sessions;
* :class:`~pyhap.camera_webrtc.WebRTCDelegate` runs the media of WebRTC
  sessions.

A delegate is called with *hooks* — one coroutine per thing the controller
asks for:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - :class:`~pyhap.camera_multi_tier.MultiTierDelegate` hook
     - Called when the controller …
   * - ``handle_prepare(session)``
     - prepares a session (before the Setup Endpoints answer)
   * - ``handle_start(session, video_tier, audio_tier)``
     - starts streaming in the selected tiers
   * - ``handle_reconfigure(session, video_tier, audio_tier)``
     - switches a running stream to other tiers
   * - ``handle_end(session)``
     - releases a session that ended — exactly once per session the
       delegate saw

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - :class:`~pyhap.camera_webrtc.WebRTCDelegate` hook
     - Called when the controller …
   * - ``handle_solicit_offer(session, video_tier, audio_tier)``
     - asks for the offer of a new session
   * - ``handle_answer(session, sdp, candidates)``
     - sends its answer — start the media now
   * - ``handle_reoffer(session, sdp, options)``
     - makes a new offer for an active session
   * - ``handle_update_session(session, add_keys, remove_kids)``
     - changes the SFrame keys it sends with
   * - ``handle_end(session)``
     - releases a session that ended — exactly once per session the
       delegate saw

Raise :class:`pyhap.camera_stream.StreamError` from a hook to answer the
controller's request with a specific status; anything else answers with an
error. A hook that runs past its deadline (``hook_timeout``, bounded by the
HAP response timeout) is cancelled and its session ends.

Sessions
--------

Every controller connection's stream is a *session* — a
:class:`~pyhap.camera_stream.StreamSession` — and the camera keeps a state
machine per session:

.. mermaid::
   :align: center

   stateDiagram-v2
       direction LR
       [*] --> NEW
       NEW --> PREPARED: handle_prepare (multi-tier)
       NEW --> SOLICITED: handle_solicit_offer (WebRTC)
       SOLICITED --> ANSWERING: answer received
       ANSWERING --> ACTIVE: answer applied
       PREPARED --> STARTED: handle_start
       STARTED --> STARTED: handle_reconfigure
       ACTIVE --> ACTIVE: handle_reoffer / keys updated
       PREPARED --> ENDED
       STARTED --> ENDED
       ACTIVE --> ENDED
       ENDED --> [*]

Three rules shape the lifecycle:

* **A session belongs to the connection that set it up.** Only that
  connection may control it, and when the connection closes, its sessions
  end (the camera calls your ``handle_end`` for them).
* **A hook runs as a task the session owns**, with a deadline; a HAP request
  waits for it until the deadline and is then answered — a slow hook does
  not stall the camera forever.
* **``handle_end`` runs exactly once** per session the delegate saw, in the
  background if need be — whatever failed or timed out before. Until it
  finishes, the session still counts towards the limits
  (``max_sessions`` per sensor, ``max_sessions_per_connection``).

A delegate keeps per-session state on the *session object* (``session.data``
is yours) — not keyed by session ID: a controller may reuse the ID of a
session that is still being released. When the media fails on your side, end
the session yourself with :meth:`StreamSession.end
<pyhap.camera_stream.StreamSession.end>` — e.g. when a viewer hangs up their
WebRTC connection, which the HAP side cannot see.

Where the media core fits
=========================

Sessions of one camera should not each pull the camera themselves, and
per-packet work should not run on the caller's loop. The
:mod:`pyhap.media` package is the shared core for delegates that want both:
one media *pull* per *channel* (one camera input, e.g. a quality tier),
fanned out to every session of that channel, with the reassembly and
protection running on a media *worker* — a thread or a child process, one
per application.

The delegates are free to use it or not: the
:class:`~pyhap.camera_multi_tier.MultiTierDelegate` and
:class:`~pyhap.camera_webrtc.WebRTCDelegate` protocols say only what the
hooks must do. See :doc:`/media/architecture` for the design of that core —
channels and sessions, the worker, and the lease lifecycle.

For multi-tier RTP there is a ready delegate on that core:
:class:`~pyhap.media.multi_tier_delegate.MultiTierMediaDelegate` sends each
session from the source of its tier's channel, and moves it to another
channel when the controller switches tiers. Give it a source provider with
one ffmpeg input per tier:

.. code-block:: python

    from pyhap.media.ffmpeg import FFmpegSourceProvider
    from pyhap.media.multi_tier_delegate import MultiTierMediaDelegate
    from pyhap.media.worker import MediaWorker

    worker = MediaWorker()
    provider = FFmpegSourceProvider({"high": [...], "medium": [...]}, worker=worker)
    camera = Camera(
        {
            ...,
            "multi_tier": {"delegate": MultiTierMediaDelegate(provider)},
        },
        driver,
        "Camera",
    )

Start the worker before the first session; see
:class:`~pyhap.media.multi_tier_delegate.MultiTierMediaDelegate`.

Talkback
========

With ``talkback``, the camera answers the Setup Endpoints handshake with a
local UDP port, and the controller sends its microphone to it, SRTP-encrypted
with the session's keys. :class:`pyhap.camera_talkback.TalkbackReceiver`
binds the port, decrypts and depacketizes, and calls
:meth:`Camera.talkback_audio_received <pyhap.camera.Camera.talkback_audio_received>`
with the audio payload of each packet — however your platform wants to
consume it.