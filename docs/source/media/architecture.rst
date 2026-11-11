.. _media-architecture:

===========================
Camera streaming design
===========================

The HomeKit camera stream services — multi-tier live view, WebRTC and HKSV
recording — leave the media to a delegate. Everything a media delegate needs
is shared in :mod:`pyhap.media`, added with the "media core": **one camera
connection per channel** instead of per session, and the per-packet work kept
off the loop that serves the HAP protocol.

.. note::
   The multi-tier RTP sender (:mod:`pyhap.media.multi_tier`), the HKSV
   recorder (:mod:`pyhap.media.recording`) and the WebRTC sender
   (:mod:`pyhap.media.sender`) use this core. This page describes the shared
   part they are built on: :mod:`pyhap.media`.

The problem
===========

Before the media core, every stream session cost its own camera connection:
one ffmpeg per session pulled the camera, trans-coded it, and sent it to one
controller. Two phones watching the same camera meant two camera connections
and two transcodes.

Two pressures shaped the design:

* *Sessions of one camera must share the camera.* A channel — one media input
  of the camera, for example one quality tier — has one media pull and one
  input source, fanned out to every session of that channel.
* *Per-packet work must not run on the caller's loop.* An application that
  embeds HAP-python usually runs everything on one event loop; Home Assistant
  runs the HAP accessory on its main loop. RTP reassembly and SRTP for every
  packet of every session belong on a loop of their own.

Channels and sessions
=====================

channel
    One media input of a camera, identified by any hashable key. Tiers that
    resolve to the same input share a channel.

session
    One HomeKit stream session. It attaches to the channel's source as a
    *consumer* and gets a *lease* on it.

Per channel (shared):

* the :class:`~pyhap.media.source.MediaSource` — two loopback UDP ports and
  the H.265 reassembly, done **once** for all sessions, plus the measured
  input bit rate and GOP;
* the media *pull* — for example one ffmpeg process started by
  :class:`~pyhap.media.ffmpeg.FFmpegSourceProvider` (video copied with
  ``dump_extra``, audio transcoded to Opus).

Per session (not shared):

* the RTP output stream — SSRC, sequence and timestamp rewriting, sender
  reports, NACK history — see :class:`pyhap.media.rtp.OutboundStream`;
* SRTP/SFrame protection and RTCP for that one controller.

The worker
==========

The sources (and, with the sender PRs, the session senders) run in a
:class:`~pyhap.media.worker.MediaWorker`: **one event loop shared by all
cameras of the application**. Only HAP signalling stays on the caller's
loop; the worker's proxies have the same async API as the real objects and
call their events back on the caller's loop.

Two modes:

* a **thread** of the caller's process (the default) — shares the loggers,
  costs no second interpreter, but shares the GIL with the application;
* a **child process** (``MediaWorker(separate_process=True)``, POSIX only) —
  logs to its own rotating file ``<log_dir>/pyhap-media-<pid>.log``, with its
  stderr and stdout redirected there so a crash leaves its traceback;
  starting a worker process removes such files that were not written for a
  week. ``log_dir`` is configurable, defaulting to a per-user directory in
  the temp dir.

Either way the worker talks over a socket pair in length-prefixed frames: a
4-byte JSON length, a 4-byte binary length, then a JSON object and raw bytes.
**Nothing is pickled.** When the worker ends, every session and source of it
ends with a reason and the next use starts a new one.

The flow
========

.. mermaid::
   :align: center

   flowchart TD
       CAM["Camera / RTSP source"]

       subgraph "Caller process — control plane (HAP signalling)"
           SRV["Stream services + session senders"]
           POOL["SourcePool<br>one source and one pull per channel"]
           PULL["FFmpegPull<br>spawn / restart / terminate"]
           FF["ffmpeg child process<br>one per channel"]
       end

       subgraph "MediaWorker — data plane<br>a thread by default, or a child process"
           SRC["MediaSource<br>two UDP ports, H.265 reassembly,<br>bitrate + GOP measurement"]
           SND["Session senders<br>RTP rewriting, SRTP, RTCP, pacing"]
       end

       CAM -->|"one pull per channel"| FF
       SRV -->|"lease / activate / release"| POOL
       POOL -->|"first answered session"| PULL
       PULL -->|"spawn / restart / terminate"| FF
       POOL -.->|"open / close source<br>(socketpair, JSON frames)"| SRC
       FF -->|"plain RTP to the loopback ports"| SRC
       SRC -->|"access units, reassembled once"| SND
       SND -->|"SRTP / SFrame"| CTRL["HomeKit controllers"]

The pull (ffmpeg) lives on the caller's side: it does not matter which
process owns the loopback ports, and its lifecycle — start on the first
answered session, restart with backoff, stop — is low-frequency control work
tied to the pool's lease counting. The worker carries the per-packet load:
reassembly, and with the sender PRs the RTP rewriting and SRTP of every
session.

The lifecycle of a channel
===========================

The ports of a source are bound by the first lease; the pull starts with the
first answered session and stops with the last one; the source is closed
with the last lease.

.. mermaid::
   :align: center

   sequenceDiagram
       autonumber
       participant A as Session 1
       participant B as Session 2
       participant P as SourcePool
       participant F as ChannelPull (ffmpeg)
       participant S as MediaSource

       A->>P: lease(channel)
       P->>S: bind the input ports
       A->>P: activate()
       P->>F: start pull
       B->>P: lease(channel)
       Note over P,S: same channel: source and pull are reused
       B->>P: activate()
       Note over F: pull already running
       A->>P: release()
       Note over F: pull keeps running for session 2
       B->>P: release()
       P->>F: stop pull
       P->>S: close source ("no sessions left")

A lease (:class:`~pyhap.media.pool.ChannelLease`) is:

``lease(channel, pull)``
    Take a claim on the channel's source, opening the source and the pull
    if the channel has none yet. ``pull`` is a factory so that a failing
    one leaves no source behind.

``activate()``
    The session was answered: count it, and start the pull if it is the
    first answered session of the channel.

``keyframe_requested(age, gop)``
    Forward a controller's keyframe request to the pull. The ffmpeg provider
    restarts its process only when no IRAP picture arrived for twice the GOP
    and ffmpeg has run at least that long — a copied stream gets its next
    IRAP picture with the next GOP anyway.

``release()``
    The session ended, exactly once. The last release stops the pull and
    closes the source.

If the source ends on its own — its worker exited, or the camera is gone —
every attached session is told and ends, and the channel is forgotten; the
next lease opens a fresh source.

The modules
===========

.. list-table::
   :header-rows: 1

   * - Module
     - Responsibility
   * - :mod:`pyhap.media`
     - ``MediaError``, lazy exports of the package
   * - :mod:`pyhap.media.rtp`
     - RTP/RTCP (RFC 3550, RFC 4585), an SRTP output stream
   * - :mod:`pyhap.media.hevc`
     - H.265 RTP depacketizer and packetizer (RFC 7798)
   * - :mod:`pyhap.media.source`
     - ``MediaSource``: the RTP input ports and the one reassembly
   * - :mod:`pyhap.media.pool`
     - ``SourcePool``: channels, leases, pull start/stop
   * - :mod:`pyhap.media.ffmpeg`
     - ``FFmpegSourceProvider``: the reference pull per channel
   * - :mod:`pyhap.media.worker`
     - ``MediaWorker``: the loop the sources run on
   * - :mod:`pyhap.media._worker`
     - The worker thread/process itself (internal)

Dependencies
============

``pylibsrtp`` became a regular dependency of HAP-python with the media core:
talkback, multi-tier streams and the media package all need SRTP. The
``Talkback`` extra remains as an empty alias for existing installs.