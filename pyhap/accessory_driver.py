"""AccessoryDriver - glues together the HAP Server, accessories and mDNS advertising.

The :class:`AccessoryDriver` is the server side of a pyhap accessory: it answers
the controller requests that arrive over the HAP server (reads and writes of
characteristics, prepared timed writes, pairing), advertises the accessory over
mDNS, persists its state and fans value changes out to subscribed controllers.

Request flow
------------

Every request enters through a public entry point - ``get_accessories``,
``get_characteristics``, ``set_characteristics``, ``prepare`` - and then splits
by caller:

- A controller's request comes from the ``HAPServerHandler`` through the
  ``*_for_controller`` variants. They return an awaitable which the handler
  schedules as the response task: the driver runs the delegates in-request,
  under a deadline (``RESPONSE_TIMEOUT``), and answers per-characteristic
  statuses - ``-70408`` for any call still running when the deadline passes.
- A direct call (tests, embedders) answers at once, optimistically with the
  stored values, and runs the delegates as a background job without a
  deadline.

Write flow (``PUT /characteristics``)::

    handle_set_characteristics
      -> _set_characteristics_for_controller    (or set_characteristics)
      -> _apply_characteristic_writes          validate + store + collect
      -> _dispatch_delegates                   awaited in-request, or a job
           -> _run_pre_checks                  service-level gates, may veto
           -> _run_char_writes                 per-characteristic on_set
      -> _finish_task_write per write          status, write response, notify
      -> _set_characteristics_response

Read flow (``GET /characteristics``, ``GET /accessories``)::

    handle_get_characteristics / handle_accessories
      -> _get_characteristics_for_controller / _get_accessories_for_controller
      -> _collect_characteristic_reads         stored value + collect the reads
      -> _read_delegate_values                 on_get under the deadline
      -> _finish_read + an answer callback     fill the characteristic's rep

Every delegate call receives a frozen context - client address and uuid,
connection handle, deadline - see ``pyhap.context``. The driver's
per-connection state (event topics, prepared writes, write responses) is keyed
by the connection handle, not by the address, so a connection that reuses a
closed one's address inherits none of its state, see
``pyhap.connection.HAPConnectionHandle``.

Sending updates to clients
--------------------------

The process of sending value changes to clients happens in two parts - on one hand,
the value change is indicated by a Characteristic and, on the other, that change is
sent to a client. To begin, typically, something in the Accessory's run method will do
set_value(foo, notify=True) on one of its contained Characteristic. This in turn will
create a HAP representation of the change and publish it to the Accessory. This will
then add some more information and eventually the value change will reach the
AccessoryDriver (all this happens through the publish() interface). The AccessoryDriver
will then check if there is a client that subscribed for events from this exact
Characteristic from this exact Accessory (remember, it could be a Bridge with more
than one Accessory in it). If so, a task is created to send the event to the
subscribers. This terminates the call chain and concludes the publishing process
from the Characteristic, the Characteristic does not block waiting for the actual
send to happen.
"""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import logging
import math
import os
import sys
import tempfile
import threading
import time
from typing import Any, NamedTuple, TypeGuard
from uuid import UUID

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from pyhap import util
from pyhap.accessory import Accessory, Bridge, get_topic
from pyhap.characteristic import (
    ADMIN_ONLY_WRITE,
    PROP_ADMIN_ONLY_ACCESS,
    PROP_PERMISSIONS,
    Characteristic,
    CharacteristicError,
    CharacteristicWrite,
    HAPValue,
)
from pyhap.connection import HAPConnectionHandle
from pyhap.const import (
    HAP_PERMISSION_NOTIFY,
    HAP_PERMISSION_READ,
    HAP_PERMISSION_TIMED_WRITE,
    HAP_PERMISSION_WRITE,
    HAP_PERMISSION_WRITE_RESPONSE,
    HAP_REPR_ACCS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_PID,
    HAP_REPR_SERVICES,
    HAP_REPR_STATUS,
    HAP_REPR_TTL,
    HAP_REPR_VALUE,
    HAP_REPR_WRITE_RESPONSE,
    STANDALONE_AID,
)
from pyhap.context import Context
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_server import HAPServer
from pyhap.hsrp import Server as SrpServer
from pyhap.loader import Loader
from pyhap.mdns import AccessoryMDNSServiceInfo
from pyhap.params import get_srp_context
from pyhap.service import Service
from pyhap.state import State

from .const import HAP_SERVER_STATUS
from .errors import HapStatusError
from .util import callback

logger = logging.getLogger(__name__)

SERVICE_CALLBACK = "callback"
SERVICE_CHARS = "chars"
SERVICE_IIDS = "iids"
KEYS_TO_EXCLUDE = {HAP_REPR_IID, HAP_REPR_AID}

# Consecutive failed pair-setup proofs (each is a setup-code guess) before
# pair-setup is locked out, and for how long.
MAX_FAILED_PAIRING_ATTEMPTS = 5
PAIRING_LOCKOUT_SECONDS = 60

# Prepared writes are short-lived (HomeKit uses ~10 second TTLs); cap both
# the TTL and the number of pending writes per client so a paired peer cannot
# grow ``prepared_writes`` without bound.
MAX_PREPARED_WRITES_PER_CLIENT = 16
MAX_PREPARED_WRITE_TTL_SECONDS = 30.0


def _is_hap_int(value: Any) -> TypeGuard[int]:
    """Whether ``value`` is a JSON integer, not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _stored_value(char: Characteristic, value: HAPValue) -> HAPValue:
    """Return the value ``client_update_value`` stores for ``value``."""
    try:
        stored: HAPValue = char.to_valid_value(value)
    except ValueError:
        return value
    return stored


class _DelegateRead(NamedTuple):
    """A delegate-backed read of one characteristic.

    ``rep`` is the HAP entry the read answers; the caller's ``answer``
    callback fills it with the value or the failure status.
    """

    rep: dict[str, Any]
    char: Characteristic


def _ignore_read_answer(read: _DelegateRead, value: Any, status: int | None) -> None:
    """Drop the rep answer of a background read of a direct call.

    A direct call answers the stored values at once; the background job
    only refreshes them, so its per-read answer has nothing left to fill.
    """


class _DelegateWrite(NamedTuple):
    """A characteristic write the delegates of a request apply.

    ``value`` is the validated value ``on_set`` receives, already
    stored: a write the pre-checks or ``on_set`` refuse is restored.
    """

    char: Characteristic
    value: Any
    previous_value: Any
    result: dict[str, Any]  # The result entry to complete.
    write_response: bool
    private_write: bool
    service: Service


class _ReadRequest(NamedTuple):
    """One read request's collected delegate reads and its base context."""

    reads: list[_DelegateRead]
    context: Context


class _WriteRequest:
    """One write request's collected delegate work.

    Carries the request's base context its delegate calls share, plus the
    writes collected while validating: every written service gets a
    pre-check first - the ``on_set`` of its delegate with every written
    value of the request, which may refuse them as a group - and the
    per-characteristic ``on_set`` calls apply the surviving writes.
    """

    __slots__ = ("context", "is_admin", "pre_checks", "writes")

    def __init__(self, context: Context, is_admin: bool) -> None:
        self.context = context
        self.is_admin = is_admin
        self.pre_checks: dict[Service, dict[Characteristic, Any]] = {}
        self.writes: list[_DelegateWrite] = []

    def __bool__(self) -> bool:
        """Whether the request has any write for the delegates to apply."""
        return bool(self.pre_checks or self.writes)


class _CallOutcome(NamedTuple):
    """The outcome of a finished delegate call: its answer or its status."""

    value: Any
    status: int | None
    error: BaseException | None


def _settle_private_write(
    char: Characteristic, previous_value: Any, request_value: Any, response: Any
) -> Any:
    """Settle a write that only its connection may read back.

    The written value of such a write is a request, e.g. a Setup
    Endpoints offer. What the callback answered is the state; whatever
    it leaves in the value is an answer for the writer only. Neither
    the former value of a refused write nor the request the callback
    left alone is an answer. Returns the answer, if there is one.
    """
    if char.read_back_response:
        if response is None and char.value not in (
            previous_value,
            _stored_value(char, request_value),
        ):
            response = char.value
        char.value = previous_value
    elif char.value == _stored_value(char, request_value):
        char.value = previous_value
    return response


def _retrieve_late_result(task: asyncio.Future[Any]) -> None:
    """Retrieve the outcome of a setter that ran on after it was cancelled."""
    if not task.cancelled() and (err := task.exception()) is not None:
        logger.debug("Setter failed after it was cancelled", exc_info=err)


def _set_characteristics_response(
    results: dict[int, dict[int, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Build the write response, or None if every write succeeded without a value."""
    characteristics = []
    nonempty_results_exist = False
    for aid, iid_results in results.items():
        for iid, result in iid_results.items():
            result[HAP_REPR_AID] = aid
            result[HAP_REPR_IID] = iid
            characteristics.append(result)
            if (
                result[HAP_REPR_STATUS] != HAP_SERVER_STATUS.SUCCESS
                or HAP_REPR_VALUE in result
            ):
                nonempty_results_exist = True

    return {HAP_REPR_CHARS: characteristics} if nonempty_results_exist else None


class AccessoryDriver:
    """An AccessoryDriver mediates between incoming requests from the HAPServer and
    the Accessory.

    The driver starts and stops the HAPServer, the mDNS advertisements and responds
    to events from the HAPServer. Concretely, it:

    - answers the controller requests of the HAP server: reads and writes of
      characteristics (``GET``/``PUT /characteristics``, ``GET /accessories``),
      prepared timed writes (``PUT /prepare``) and pairing;
    - runs the delegates of the accessory tree for those requests - the
      read/write flow is sketched in this module's docstring;
    - keeps the per-connection state of controllers - event subscriptions
      (``topics``), prepared writes and write responses - keyed by
      :class:`~pyhap.connection.HAPConnectionHandle`, so a connection that
      reuses a closed one's address inherits none of its state;
    - publishes value changes to the subscribed connections, skipping the
      connection a write came over;
    - persists the accessory state and advertises the accessory over mDNS.

    A controller's request runs its delegates in-request under a deadline and
    is answered once they are done; a direct call, e.g. to
    :meth:`set_characteristics`, is answered optimistically with the stored
    values and runs the delegates as a background job.
    """

    def __init__(
        self,
        *,
        address: Any = None,
        port: int = 51234,
        persist_file: str = "accessory.state",
        pincode: Any = None,
        encoder: Any = None,
        loader: Any = None,
        loop: Any = None,
        mac: Any = None,
        listen_address: Any = None,
        advertised_address: Any = None,
        interface_choice: Any = None,
        async_zeroconf_instance: Any = None,
        zeroconf_server: Any = None,
        advertise: bool = True,
        enforce_timed_writes: bool = False,
    ) -> None:
        """Initialize a new AccessoryDriver object.

        :param pincode: The pincode that HAP clients must prove they know in order
            to pair with this `Accessory`. Defaults to None, in which case a random
            pincode is generated. The pincode has the format "xxx-xx-xxx", where x is
            a digit.
        :type pincode: bytes

        :param port: The local port on which the accessory will be accessible.
            In other words, this is the port of the HAPServer.
        :type port: int

        :param address: The local address on which the accessory will be accessible.
            In other words, this is the address of the HAPServer. If not given, the
            driver will try to select an address.
        :type address: str

        :param persist_file: The file name in which the state of the accessory
            will be persisted. This uses `expandvars`, so may contain `~` to
            refer to the user's home directory.
        :type persist_file: str

        :param encoder: The encoder to use when persisting/loading the Accessory state.
        :type encoder: AccessoryEncoder

        :param loader: The loader to use when loading the Accessory state.
        :type loader: Loader

        :param loop: The asyncio event loop to run on. If not given, a new loop
            is created and a default executor is set on it, so plain callables
            added with :meth:`add_job` run in a thread pool and the driver owns
            the loop (see :meth:`start`). When a loop is given, the driver runs
            on it as-is - typical for embedders - and sets no executor.
        :type loop: asyncio.AbstractEventLoop

        :param mac: The MAC address which will be used to identify the accessory.
            If not given, the driver will try to select a MAC address.
        :type mac: str

        :param listen_address: The local address on the HAPServer will listen.
            If not given, the value of the address parameter will be used.
        :type listen_address: str

        :param advertised_address: The addresses of the HAPServer announced via mDNS.
            This can be used to announce an external address from behind a NAT.
            If not given, the value of the address parameter will be used.
        :type advertised_address: str | list[str]

        :param interface_choice: The zeroconf interfaces to listen on.
        :type InterfacesType: [InterfaceChoice.Default, InterfaceChoice.All]

        :param async_zeroconf_instance: An AsyncZeroconf instance. When running multiple
            accessories or bridges a single zeroconf instance can be shared to avoid
            the overhead of processing the same data multiple times.

        :param zeroconf_server: The server name that will be used for the zeroconf
            ServiceInfo.
        :type zeroconf_server: str

        :param advertise: Whether to advertise the accessory via mDNS when
            the driver starts. Pass False to run the HAP server without
            mDNS advertising, e.g. for tests or benchmarks.
        :type advertise: bool

        :param enforce_timed_writes: Whether to reject a plain write to a
            characteristic with the timed write ("tw") permission with -70410,
            as the HAP specification requires. Off by default, because Home
            hubs write some tw characteristics, e.g. HomeKit Camera Active,
            without a timed write.
        :type enforce_timed_writes: bool
        """
        if loop is None:
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()

            self.executor = ThreadPoolExecutor(
                max_workers=None, thread_name_prefix="SyncWorker"
            )
            loop.set_default_executor(self.executor)
            self.tid = threading.current_thread()
        else:
            self.tid = threading.main_thread()
            self.executor = None

        self.loop = loop

        self.accessory: Accessory | None = None
        self.advertise = advertise
        self.advertiser: AsyncZeroconf | None = async_zeroconf_instance
        self.zeroconf_server = zeroconf_server
        self.interface_choice = interface_choice

        self.persist_file = os.path.expanduser(persist_file)
        self.encoder = encoder or AccessoryEncoder()
        # Topics ("aid.iid") with the connections subscribed to them, see
        # HAPConnectionHandle.
        self.topics: dict[str, set[HAPConnectionHandle]] = {}
        self.loader = loader or Loader()
        self.aio_stop_event: asyncio.Event | None = None
        self.stop_event = threading.Event()

        self.safe_mode = False
        self.enforce_timed_writes = enforce_timed_writes

        self.mdns_service_info: ServiceInfo | None = None
        self._srp_verifier: SrpServer | None = None

        # Pair-setup rate limiting: each M1 regenerates a 3072-bit SRP
        # verifier and each failed proof is a pincode guess, so excess
        # failures lock pair-setup out for a while (see
        # ``register_failed_pairing_attempt``).
        self._failed_pairing_attempts = 0
        self._pairing_lockout_until = 0.0

        address = address or util.get_local_address()
        advertised_address = advertised_address or address
        self.state = State(
            address=advertised_address, mac=mac, pincode=pincode, port=port
        )

        self._listen_address: str | list[str] = listen_address or address
        network_tuple = (self._listen_address, self.state.port)
        self.http_server = HAPServer(network_tuple, self)
        # Timed writes pending their request, per connection.
        self.prepared_writes: dict[HAPConnectionHandle, dict[int, float]] = {}
        # Write responses of readable characteristics, per connection.
        self._write_responses: dict[
            HAPConnectionHandle, dict[Characteristic, HAPValue]
        ] = {}
        # The virtual handles of direct calls, see ``_connection_of``.
        self._direct_connections: dict[tuple[str, int] | None, HAPConnectionHandle] = {}
        # The identity of the next connection, see HAPConnectionHandle.
        self._connection_generation = 0

    def listen_address(self) -> str | list[str]:
        """Return the address, or list of addresses, the HAP server listens on."""
        return self._listen_address

    def start(self) -> None:
        """Start the event loop and call `start_service`.

        Pyhap will be stopped gracefully on a KeyBoardInterrupt.
        """
        try:
            logger.info("Starting the event loop")
            if (
                threading.current_thread() is threading.main_thread()
                and os.name != "nt"
                # The child watcher API was removed in Python 3.14; there the
                # default event loop reaps subprocesses without one.
                and hasattr(asyncio, "SafeChildWatcher")
            ):
                logger.debug("Setting child watcher")
                watcher = asyncio.SafeChildWatcher()
                watcher.attach_loop(self.loop)
                # Deprecated in 3.12, removed in 3.14; the hasattr guard
                # above keeps this branch unreachable on 3.14+.
                asyncio.set_child_watcher(watcher)  # ty: ignore[deprecated]
            else:
                logger.debug(
                    "Not setting a child watcher. Set one if "
                    "subprocesses will be started outside the main thread."
                )
            self.add_job(self.async_start())
            self.loop.run_forever()
        except KeyboardInterrupt:
            logger.debug("Got a KeyboardInterrupt, stopping driver")
            self.loop.call_soon_threadsafe(self.loop.create_task, self.async_stop())
            self.loop.run_forever()
        finally:
            self._cancel_pending_tasks()
            self.loop.close()
            logger.info("Closed the event loop")

    def _cancel_pending_tasks(self) -> None:
        """Cancel tasks still pending on the loop before it is closed.

        Without this, a task that is still running when the loop stops (e.g.
        a ``run_at_interval`` wrapper awaiting its ``run`` task) is destroyed
        pending at loop close, which asyncio logs as "Task was destroyed but
        it is pending!".
        """
        pending = [task for task in asyncio.all_tasks(self.loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

    def start_service(self) -> None:
        """Start the accessory on an already running event loop.

        Validates the driver and schedules :meth:`async_start` on its loop.
        Use this instead of :meth:`start` when the caller runs the loop, e.g.
        an embedder.
        """
        self._validate_start()
        self.add_job(self.async_start)

    def _validate_start(self) -> None:
        """Raise ValueError when no accessory is assigned to the driver yet."""
        if self.accessory is None:
            raise ValueError(
                "You must assign an accessory to the driver, before you can start it."
            )

    async def async_start(self) -> None:
        """Starts the accessory. Must run in the event loop.

        - Start the HAP server.
        - Update the accessories hash (persisting the state when it changed,
          so iOS drops its cached ``/accessories``).
        - Publish a mDNS advertisement (unless ``advertise`` is False).
        - Print the setup QR code if the accessory is not paired.
        - Call the accessory's run method as a job.

        All of the above are started as separate jobs. Accessory thread is set as
        daemon.
        """
        self._validate_start()
        assert self.accessory is not None  # noqa: S101
        self.aio_stop_event = asyncio.Event()

        logger.info(
            "Starting accessory %s on addresses %s, port %s.",
            self.accessory.display_name,
            self.state.addresses,
            self.state.port,
        )

        # Start listening for requests
        logger.debug("Starting server.")
        await self.http_server.async_start(self.loop)

        # Update the hash of the accessories
        # in case the config version needs to be
        # incremented to tell iOS to drop the cache
        # for /accessories
        if self.state.set_accessories_hash(self.accessories_hash):
            self.async_persist()

        if self.advertise:
            # Advertise the accessory as a mDNS service.
            logger.debug("Starting mDNS.")
            self.mdns_service_info = AccessoryMDNSServiceInfo(
                self.accessory, self.state, self.zeroconf_server
            )

            if not self.advertiser:
                zc_args = {}
                if self.interface_choice is not None:
                    zc_args["interfaces"] = self.interface_choice
                self.advertiser = AsyncZeroconf(**zc_args)
            await (
                await self.advertiser.async_register_service(
                    self.mdns_service_info, cooperating_responders=True
                )
            )

        # Print accessory setup message
        if not self.state.paired:
            self.accessory.setup_message()

        # Start the accessory so it can do stuff.
        logger.debug("Starting accessory %s", self.accessory.display_name)
        self.add_job(self.accessory.run)
        logger.debug(
            "AccessoryDriver for %s started successfully", self.accessory.display_name
        )

    def stop(self) -> None:
        """Stop the driver, from any thread.

        Schedules :meth:`async_stop` on the event loop.
        """
        self.add_job(self.async_stop)

    async def async_stop(self) -> None:
        """Stops the AccessoryDriver and shutdown all remaining tasks.

        Must run in the event loop. Unregisters the mDNS advertisement, stops
        the HAP server and the accessory, and - when the driver created its
        loop - shuts the executor down and stops the loop.
        """
        self.stop_event.set()
        accessory = self.accessory
        assert accessory is not None  # noqa: S101
        assert self.aio_stop_event is not None  # noqa: S101
        logger.debug("Stopping HAP server and event sending")
        if self.advertise:
            assert self.advertiser is not None  # noqa: S101
            assert self.mdns_service_info is not None  # noqa: S101
            logger.debug("Stopping mDNS advertising for %s", accessory.display_name)
            await (
                await self.advertiser.async_unregister_service(self.mdns_service_info)
            )
            await self.advertiser.async_close()

        self.aio_stop_event.set()

        self.http_server.async_stop()

        logger.info(
            "Stopping accessory %s on address %s, port %s.",
            accessory.display_name,
            self.state.addresses,
            self.state.port,
        )

        await self.async_add_job(accessory.stop)

        logger.debug(
            "AccessoryDriver for %s stopped successfully", accessory.display_name
        )

        # Executor=None means a loop wasn't passed in
        if self.executor is not None:
            logger.debug("Shutdown executors")
            self.executor.shutdown()
            self.loop.stop()

        logger.debug("Stop completed")

    def add_job(self, target: Any, *args: Any) -> None:
        """Add a job to the event loop, from any thread.

        The target may be a coroutine, a :func:`~pyhap.util.callback`
        function, a coroutine function - called with ``args`` - or a plain
        callable, which runs in the loop's executor. See :meth:`async_add_job`
        for the dispatch.
        """
        if target is None:
            raise ValueError("Don't call add_job with None.")
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    @util.callback
    def async_add_job(self, target: Any, *args: Any) -> Any:
        """Add a job from within the event loop, dispatching on the target's kind.

        A coroutine becomes a task; a :func:`~pyhap.util.callback` function is
        scheduled with ``loop.call_soon``; a coroutine function is called with
        ``args`` and becomes a task; anything else runs in the executor.
        Returns the task, or None for a callback.
        """
        task = None

        if asyncio.iscoroutine(target):
            task = self.loop.create_task(target)
        elif util.is_callback(target):
            self.loop.call_soon(target, *args)
        elif util.iscoro(target):
            task = self.loop.create_task(target(*args))
        else:
            task = self.loop.run_in_executor(None, target, *args)

        return task

    def add_accessory(self, accessory: Accessory) -> None:
        """Add the top level accessory to the driver.

        The top-level accessory must have the standalone AID (1); it is
        assigned automatically when the accessory has none. When a persist
        file exists, the accessory state is loaded from it and restored into
        the accessory; otherwise the current state is persisted.
        """
        self.accessory = accessory
        if accessory.aid is None:
            accessory.aid = STANDALONE_AID
        elif accessory.aid != STANDALONE_AID:
            raise ValueError("Top-level accessory must have the AID == 1.")
        if os.path.exists(self.persist_file):
            logger.info("Loading Accessory state from `%s`", self.persist_file)
            self.load()
            # Accessories built before load() have not seen their stored state.
            accessory.restore_accessory_state()
        else:
            logger.info("Storing Accessory state in `%s`", self.persist_file)
            self.persist()

    @util.callback
    def async_subscribe_client_topic(
        self, client: HAPConnectionHandle, topic: str, subscribe: bool = True
    ) -> None:
        """(Un)Subscribe the given client from the given topic.

        This method must be run in the event loop.

        :param client: The connection that should be subscribed, see
            :class:`~pyhap.connection.HAPConnectionHandle`.
        :type client: HAPConnectionHandle

        :param topic: The topic to which to subscribe.
        :type topic: str

        :param subscribe: Whether to subscribe or unsubscribe the client. Both
            subscribing an already subscribed client and unsubscribing a client
            that is not subscribed do nothing.
        :type subscribe: bool
        """
        if subscribe:
            subscribed_clients = self.topics.get(topic)
            if subscribed_clients is None:
                subscribed_clients = set()
                self.topics[topic] = subscribed_clients
            subscribed_clients.add(client)
            return

        if topic not in self.topics:
            return
        subscribed_clients = self.topics[topic]
        subscribed_clients.discard(client)
        if not subscribed_clients:
            del self.topics[topic]

    def client_connected(self, client_addr: tuple[str, int] | None) -> None:
        """Tell the accessory that a controller's connection was accepted.

        Schedules the accessory's
        :meth:`~pyhap.accessory.Accessory.client_connected` as an isolated
        job: the connection never waits for it and its exception is logged,
        not raised. Called by the HAP server in the event loop.
        """
        accessory = self.accessory
        if accessory is None:
            return
        self.add_job(
            self._run_isolated_hook,
            accessory.client_connected,
            (client_addr,),
            "client_connected",
            client_addr,
        )

    def client_verified(
        self, client_addr: tuple[str, int] | None, client_uuid: UUID
    ) -> None:
        """Tell the accessory that a controller finished pair verify on a connection.

        Schedules the accessory's
        :meth:`~pyhap.accessory.Accessory.client_verified` as an isolated
        job: the pair verify response never waits for it and its exception
        is logged, not raised. Called by the HAP server in the event loop.
        """
        accessory = self.accessory
        if accessory is None:
            return
        self.add_job(
            self._run_isolated_hook,
            accessory.client_verified,
            (client_addr, client_uuid),
            "client_verified",
            client_addr,
        )

    async def _run_isolated_hook(
        self,
        hook: Callable[..., Awaitable[None]],
        hook_args: tuple[Any, ...],
        name: str,
        client_addr: tuple[str, int] | None,
    ) -> None:
        """Await a lifecycle or subscription hook, logging its failure.

        The hook is called here, not by the scheduling caller, so it only
        runs once its job does. It runs isolated from what caused it: an
        accessory's exception never reaches the connection or the request,
        it is only logged.
        """
        try:
            await hook(*hook_args)
        except Exception:
            accessory = self.accessory
            logger.exception(
                "%s failed to run %s for %s",
                accessory.display_name if accessory else "the accessory",
                name,
                client_addr,
            )

    def connection_lost(
        self,
        client: Any,
        connection: HAPConnectionHandle | None = None,
        client_uuid: UUID | None = None,
    ) -> None:
        """Called when a connection is lost to a client.

        This method must be run in the event loop.

        :param client: A client (address, port) tuple that should be unsubscribed.
        :type client: tuple <str, int>

        :param connection: The identity of the closed connection. Its
            per-connection state is dropped; without it, the virtual handle
            of ``client`` that a direct call uses is dropped instead, see
            :meth:`_connection_of`.
        :type connection: HAPConnectionHandle or None

        :param client_uuid: The paired controller of the closed connection,
            or None when it never finished pair verify or the caller does
            not know it.
        :type client_uuid: uuid.UUID or None
        """
        connection = connection or self._connection_of(client)
        client_topics = []
        for topic, subscribed_clients in self.topics.items():
            if connection in subscribed_clients:
                # Make a copy to avoid changing
                # self.topics during iteration
                client_topics.append(topic)

        accessory = self.accessory
        for topic in client_topics:
            self.async_subscribe_client_topic(connection, topic, subscribe=False)
            if accessory is not None:
                self._dispatch_close_unsubscribe(
                    topic, accessory, client, client_uuid, connection
                )
        self.prepared_writes.pop(connection, None)
        self._write_responses.pop(connection, None)
        if accessory is None:
            return
        try:
            accessory.client_disconnected(client)
        except Exception:
            logger.exception(
                "%s failed to handle a closed connection", accessory.display_name
            )

    def _dispatch_close_unsubscribe(
        self,
        topic: str,
        accessory: Accessory,
        client_addr: Any,
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
    ) -> None:
        """Run ``on_unsubscribe`` of a topic a closed connection was subscribed to."""
        aid, iid = (int(i) for i in topic.split("."))
        char = accessory.get_characteristic(aid, iid)
        if char is not None:
            self._dispatch_subscription(
                char, False, client_addr, client_uuid, connection
            )

    def publish(
        self,
        data: dict[str, Any],
        connection: HAPConnectionHandle | None = None,
        immediate: bool = False,
    ) -> None:
        """Publishes an event to the subscribed clients.

        The event is sent only to the connections subscribed to the topic of
        the aid and iid contained in the data; the connection ``connection``
        - the one a write came over - is not sent its own write back. Safe to
        call from any thread: called from another thread, the send is
        scheduled onto the event loop.

        :param data: The data to publish. It must at least contain the keys "aid" and
            "iid".
        :type data: dict

        :param connection: The connection that caused the change, skipped in
            the fan-out, see :class:`~pyhap.connection.HAPConnectionHandle`.
        :type connection: HAPConnectionHandle or None

        :param immediate: Whether the server should send the event at once
            instead of batching it with the next one.
        :type immediate: bool
        """
        topic = get_topic(data[HAP_REPR_AID], data[HAP_REPR_IID])
        if topic not in self.topics:
            return

        if threading.current_thread() == self.tid:
            self.async_send_event(topic, data, connection, immediate)
            return

        self.loop.call_soon_threadsafe(
            self.async_send_event, topic, data, connection, immediate
        )

    def async_send_event(
        self,
        topic: str,
        data: dict[str, Any],
        sender: HAPConnectionHandle | None,
        immediate: bool,
    ) -> None:
        """Send an event to every connection subscribed to ``topic``.

        Must be called in the event loop. The ``sender`` connection, when
        given, is skipped - a controller is not sent its own write back. A
        connection whose socket turns out to be stale is unsubscribed from
        the topic. Does nothing once the driver is stopping.
        """
        assert self.aio_stop_event is not None  # noqa: S101
        if self.aio_stop_event.is_set():
            return

        subscribed_clients = self.topics.get(topic, [])
        logger.debug(
            "Send event: topic(%s), data(%s), sender(%s)",
            topic,
            data,
            sender,
        )
        unsubs = []
        for connection in subscribed_clients:
            if sender is not None and sender == connection:
                logger.debug(
                    "Skip sending event to client since "
                    "its the client that made the characteristic change: %s",
                    connection,
                )
                continue
            logger.debug(
                "Sending event to client: %s, immediate: %s", connection, immediate
            )
            pushed = self.http_server.push_event(data, connection, immediate)
            if not pushed:
                logger.debug(
                    "Could not send event to %s, probably stale socket.", connection
                )
                unsubs.append(connection)
                # Maybe consider removing the client_addr from every topic?

        for connection in unsubs:
            self.async_subscribe_client_topic(connection, topic, False)

    def config_changed(self) -> None:
        """Notify the driver that the accessory's configuration has changed.

        Persists the accessory, so that the new configuration is available on
        restart. Also, updates the mDNS advertisement, so that iOS clients know
        they need to fetch new data.
        """
        self.state.increment_config_version()
        self.persist()
        self.update_advertisement()

    def update_advertisement(self) -> None:
        """Update the mDNS advertisement, from any thread.

        Schedules :meth:`async_update_advertisement` on the event loop.
        """
        self.loop.call_soon_threadsafe(self.async_update_advertisement)

    @callback
    def async_update_advertisement(self) -> None:
        """Update the mDNS service info for the accessory, from the event loop.

        Rebuilds the advertisement from the current state - configuration
        version, pairing state, setup hash - so HomeKit sees the change, and
        re-registers it with the advertiser.
        """
        logger.debug("Updating mDNS advertisement")
        if not self.advertise:
            logger.debug("Not updating mDNS advertisement: advertising is disabled")
            return
        assert self.advertiser is not None  # noqa: S101
        self.mdns_service_info = AccessoryMDNSServiceInfo(
            self.accessory, self.state, self.zeroconf_server
        )
        util.async_create_background_task(
            self._async_update_service(self.advertiser, self.mdns_service_info)
        )

    async def _async_update_service(
        self, advertiser: AsyncZeroconf, info: ServiceInfo
    ) -> None:
        """Update the mDNS service and wait for the broadcast to finish."""
        await (await advertiser.async_update_service(info))

    def accessory_state(self, aid: int) -> dict[str, Any]:
        """Return the persisted state dict of accessory ``aid``.

        Accessories keep settings here that must survive a restart. Save
        changes with :meth:`async_persist`.
        """
        return self.state.accessory_states.setdefault(aid, {})

    @callback
    def async_persist(self) -> None:
        """Saves the state of the accessory.

        Must be run in the event loop.
        """
        logger.debug("Scheduling write of accessory state to disk")
        util.async_create_background_task(self.loop.run_in_executor(None, self.persist))

    def persist(self) -> None:
        """Save the state of the accessory to the persist file.

        Must run in executor. Writes to a temporary file first and then
        atomically replaces the persist file, so a crash mid-write cannot
        corrupt the stored state.
        """
        logger.debug("Writing of accessory state to disk")
        tmp_filename = None
        try:
            temp_dir = os.path.dirname(self.persist_file)
            logger.debug("Creating temp persist file in '%s'", temp_dir)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=temp_dir, delete=False
            ) as file_handle:
                tmp_filename = file_handle.name
                logger.debug(
                    "Created temp persist file '%s' named '%s'",
                    file_handle,
                    tmp_filename,
                )
                self.encoder.persist(file_handle, self.state)
            if (
                os.name == "nt"
            ):  # Or `[WinError 5] Access Denied` will be raised on Windows
                os.chmod(tmp_filename, 0o644)
                if os.path.exists(self.persist_file):
                    os.chmod(self.persist_file, 0o644)
            os.replace(tmp_filename, self.persist_file)
        except Exception:
            logger.exception("Failed to persist accessory state")
            raise
        finally:
            if tmp_filename and os.path.exists(tmp_filename):
                os.remove(tmp_filename)

    def load(self) -> None:
        """Load the persist file into the driver's state via the encoder.

        Must run in executor.
        """
        with open(self.persist_file, encoding="utf8") as file_handle:
            self.encoder.load_into(file_handle, self.state)

    @callback
    def pair(
        self,
        client_username_bytes: bytes,
        client_public: bytes,
        client_permissions: bytes,
    ) -> bool:
        """Called when a client has paired with the accessory.

        Persist the new accessory state.

        :param client_username_bytes: The client username bytes.
        :type client_username_bytes: bytes

        :param client_public: The client's public key.
        :type client_public: bytes

        :param client_permissions: The client's permissions.
        :type client_permissions: bytes (int)

        :return: Whether the pairing is successful.
        :rtype: bool
        """
        logger.info(
            "Paired with %s with permissions %s.",
            client_username_bytes,
            client_permissions,
        )
        # A successful pairing clears the rate limiter.
        self._failed_pairing_attempts = 0
        self._pairing_lockout_until = 0.0
        self.state.add_paired_client(
            client_username_bytes, client_public, client_permissions
        )
        self.async_persist()
        return True

    @callback
    def register_failed_pairing_attempt(self) -> None:
        """Register a failed pair-setup attempt.

        Each failed SRP proof is effectively a setup-code guess and each pair
        setup also regenerates a 3072-bit SRP verifier, so after too many
        consecutive failures pair-setup is locked out for a cooldown period.

        This method must be run in the event loop.
        """
        self._failed_pairing_attempts += 1
        if self._failed_pairing_attempts >= MAX_FAILED_PAIRING_ATTEMPTS:
            self._pairing_lockout_until = time.monotonic() + PAIRING_LOCKOUT_SECONDS
            self._failed_pairing_attempts = 0
            logger.warning(
                "Pair-setup locked out for %s seconds after %s failed attempts",
                PAIRING_LOCKOUT_SECONDS,
                MAX_FAILED_PAIRING_ATTEMPTS,
            )

    @property
    def pairing_locked_out(self) -> bool:
        """Whether pair-setup is currently locked out due to failed attempts."""
        return time.monotonic() < self._pairing_lockout_until

    @callback
    def unpair(self, client_uuid: Any) -> None:
        """Removes the paired client from the accessory.

        Persist the new accessory state.

        :param client_uuid: The client uuid.
        :type client_uuid: uuid.UUID
        """
        logger.info("Unpairing client %s.", client_uuid)
        self.state.remove_paired_client(client_uuid)
        if not self.state.paired and self.accessory is not None:
            try:
                self.accessory.reset_accessory_state()
            except Exception:
                # The removed pairing must be saved regardless.
                logger.warning("Failed to reset the accessory state", exc_info=True)
        self.async_persist()

    def finish_pair(self) -> None:
        """Finishing pairing or unpairing.

        Updates the accessory and updates the mDNS service.

        The mDNS announcement must not be updated until AFTER
        the final pairing response is sent or homekit will
        see that the accessory is already paired and assume
        it should stop pairing.
        """
        # Safe mode added to avoid error during pairing, see
        # https://github.com/home-assistant/home-assistant/issues/14567
        #
        # This may no longer be needed now that we defer
        # updating the advertisement until after the final
        # pairing response is sent.
        #
        if not self.safe_mode:
            self.update_advertisement()

    @property
    def srp_verifier(self) -> SrpServer:
        """Return the SRP verifier (created by setup_srp_verifier)."""
        assert self._srp_verifier is not None  # noqa: S101
        return self._srp_verifier

    def setup_srp_verifier(self) -> None:
        """Create the SRP verifier for pair-setup, stored as :attr:`srp_verifier`.

        The verifier uses the 3072-bit group with SHA-512 and the pincode as
        the password. Each pair-setup regenerates it, which is part of why
        failed attempts are rate limited, see
        :meth:`register_failed_pairing_attempt`.
        """
        # TODO: Move the below hard-coded values somewhere nice.
        ctx = get_srp_context(3072, hashlib.sha512, 16)
        verifier = SrpServer(ctx, b"Pair-Setup", self.state.pincode)
        self._srp_verifier = verifier

    @property
    def accessories_hash(self) -> str:
        """Hash of the ``/accessories`` metadata, to track configuration changes.

        The hash covers the accessories' metadata only
        (``get_accessories(include_value=False)``): iOS decides from the
        metadata whether its cached configuration is stale, and a changed
        characteristic value must not look like a configuration change. A
        changed hash makes :meth:`async_start` persist the state, so the new
        configuration version reaches the mDNS advertisement.
        """
        return hashlib.sha512(
            util.to_sorted_hap_json(self.get_accessories(include_value=False))
        ).hexdigest()

    def _hap_reps(self, include_value: bool = True) -> list[dict[str, Any]]:
        """The HAP reps of the accessory tree, as a list.

        A standalone accessory answers a single rep; a Bridge answers one per
        accessory, including the bridge itself.
        """
        assert self.accessory is not None  # noqa: S101
        hap_rep = self.accessory.to_HAP(include_value=include_value)
        if not isinstance(hap_rep, list):
            hap_rep = [
                hap_rep,
            ]
        return hap_rep

    def get_accessories(self, include_value: bool = True) -> dict[str, Any]:
        """Return the accessory tree in HAP format.

        :param include_value: Whether to include the characteristics' current
            values.
        :type include_value: bool

        :return: An example HAP representation is:

        .. code-block:: python

           {
              "accessories": [
                 {
                    "aid": 1,
                    "services": [
                       {
                          "iid": 1,
                          "type": ...,
                          "characteristics": [{
                             "iid": 2,
                             "type": ...,
                             "description": "CurrentTemperature",
                             ...
                          }]
                       }
                    ]
                 }
              ]
           }

        :rtype: dict

        A direct call answers the stored values and runs the delegate reads
        as a background job without a deadline, like a direct write runs its
        setters; a controller's request awaits them through their ``on_get``
        instead, see :meth:`_get_accessories_for_controller`.
        """
        hap_rep = self._hap_reps(include_value=include_value)
        if include_value:
            # A direct call answers the stored values of delegate-backed
            # characteristics and runs their reads as a job, like a direct
            # write runs its setters; a controller's request awaits them
            # instead, see ``_get_accessories_for_controller``.
            reads = self._collect_accessory_reads(hap_rep)
            if reads:
                self.add_job(
                    self._read_delegate_values,
                    _ReadRequest(
                        reads,
                        self._build_request_context(
                            None, None, self._connection_of(None), None
                        ),
                    ),
                    _ignore_read_answer,
                )
        logger.debug("Get accessories response: %s", hap_rep)
        return {HAP_REPR_ACCS: hap_rep}

    def _get_accessories_for_controller(
        self,
        client_addr: tuple[str, int],
        timeout: float,
        *,
        client_uuid: UUID | None = None,
        connection: HAPConnectionHandle | None = None,
    ) -> Awaitable[dict[str, Any]]:
        """Answer a controller's accessories request, awaiting the reads.

        Returns an awaitable of the response. Every readable
        characteristic's ``on_get`` runs under the deadline like a
        write's callbacks, and a read that fails or misses it answers
        its characteristic without a value, like a failing getter does.
        """
        connection = connection or self._connection_of(client_addr)
        hap_rep = self._hap_reps()
        reads = self._collect_accessory_reads(hap_rep)
        return self._read_accessories_response(
            hap_rep,
            _ReadRequest(
                reads,
                self._build_request_context(
                    client_addr, client_uuid, connection, timeout
                ),
            ),
        )

    async def _read_accessories_response(
        self,
        hap_rep: list[dict[str, Any]],
        request: _ReadRequest,
    ) -> dict[str, Any]:
        """Answer the delegate reads of an accessories request."""
        await self._read_delegate_values(request, self._answer_accessory_read)
        return {HAP_REPR_ACCS: hap_rep}

    def _collect_accessory_reads(
        self, hap_rep: list[dict[str, Any]]
    ) -> list[_DelegateRead]:
        """Pair the delegate-backed readable characteristics with their reps."""
        accessory = self.accessory
        reads: list[_DelegateRead] = []
        if accessory is None:
            return reads
        reps_by_iid: dict[tuple[Any, Any], dict[str, Any]] = {}
        for acc_rep in hap_rep:
            for service_rep in acc_rep.get(HAP_REPR_SERVICES, ()):
                for rep in service_rep.get(HAP_REPR_CHARS, ()):
                    reps_by_iid[(acc_rep[HAP_REPR_AID], rep[HAP_REPR_IID])] = rep
        accessories: list[Accessory] = (
            [accessory, *accessory.accessories.values()]
            if isinstance(accessory, Bridge)
            else [accessory]
        )
        for acc in accessories:
            for service in acc.services:
                for char in service.characteristics:
                    if HAP_PERMISSION_READ not in char.properties[PROP_PERMISSIONS]:
                        continue
                    # An unpublished accessory has no (AID, IID) pair in the
                    # response, so it has no rep to answer either.
                    rep = reps_by_iid.get((acc.aid, acc.iid_manager.get_iid(char)))
                    if rep is not None:
                        reads.append(_DelegateRead(rep, char))
        return reads

    def get_characteristics(
        self, char_ids: Any, client_addr: tuple[str, int] | None = None
    ) -> dict[str, Any]:
        """Returns values for the required characteristics.

        :param char_ids: A list of characteristic "paths", e.g. "1.2" is aid 1, iid 2.
        :type char_ids: list<str>

        :param client_addr: The client that reads. It reads back its own write
            responses.
        :type client_addr: tuple <str, int>

        :return: Status success for each required characteristic. For example:

        .. code-block:: python

           {
              "characteristics: [{
                 "aid": 1,
                 "iid": 2,
                 "status" 0
              }]
           }

        :rtype: dict

        A direct call answers the stored values at once and runs the
        reads as a job without a deadline, like a direct write runs its
        setters - a legacy getter is wrapped into the delegate the job
        calls. A controller's read goes through
        ``_get_characteristics_for_controller`` instead and awaits them.
        """
        connection = self._connection_of(client_addr)
        chars, reads = self._collect_characteristic_reads(char_ids, connection)
        if reads:
            self.add_job(
                self._read_delegate_values,
                _ReadRequest(
                    reads,
                    self._build_request_context(client_addr, None, connection, None),
                ),
                _ignore_read_answer,
            )
        logger.debug("Get chars response: %s", chars)
        return {HAP_REPR_CHARS: chars}

    def _get_characteristics_for_controller(
        self,
        char_ids: Any,
        client_addr: tuple[str, int],
        timeout: float,
        *,
        client_uuid: UUID | None = None,
        connection: HAPConnectionHandle | None = None,
    ) -> Awaitable[dict[str, Any]]:
        """Read characteristics for a controller, awaiting the reads.

        Returns an awaitable of the response. Every read characteristic's
        ``on_get`` runs under the deadline - a legacy getter is wrapped
        into the delegate - and a read not done by it answers ``-70408``.
        Cancelling the awaitable, e.g. because the connection closed,
        cancels the reads.
        """
        connection = connection or self._connection_of(client_addr)
        chars, reads = self._collect_characteristic_reads(char_ids, connection)
        return self._read_characteristics_response(
            chars,
            _ReadRequest(
                reads,
                self._build_request_context(
                    client_addr, client_uuid, connection, timeout
                ),
            ),
        )

    async def _read_characteristics_response(
        self,
        chars: list[dict[str, Any]],
        request: _ReadRequest,
    ) -> dict[str, Any]:
        """Answer the delegate reads of a characteristics request."""
        await self._read_delegate_values(request, self._answer_char_read)
        return {HAP_REPR_CHARS: chars}

    def _collect_characteristic_reads(
        self, char_ids: Any, connection: HAPConnectionHandle
    ) -> tuple[list[dict[str, Any]], list[_DelegateRead]]:
        """Answer a read per requested characteristic, collecting the reads.

        Every readable characteristic answers its stored value
        optimistically, and its read is collected for the caller to run
        through the delegate's ``on_get`` - a legacy getter is wrapped
        into one: a controller's request awaits it, a direct call runs
        it as a background job. A characteristic the connection wrote
        reads its write response back, which wins over any read callback.
        Unknown ids and unreadable characteristics keep the per-read
        failure status of their rep.
        """
        assert self.accessory is not None  # noqa: S101
        write_responses = self._write_responses.get(connection, {})
        chars = []
        reads: list[_DelegateRead] = []
        for aid_iid in char_ids:
            aid, iid = (int(i) for i in aid_iid.split("."))
            rep = {
                HAP_REPR_AID: aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            }

            try:
                if aid == STANDALONE_AID:
                    acc = self.accessory
                else:
                    # Accessory IIDs other than the standalone one only exist
                    # on bridged accessories; anything else is unknown and
                    # keeps the per-characteristic failure status.
                    acc = self._bridged_accessory(aid)
                    if acc is None and isinstance(self.accessory, Bridge):
                        # An unknown bridged aid gets no rep at all.
                        continue
                if acc is not None:
                    char = acc.iid_manager.get_obj(iid)
                    available = acc.available
                else:
                    char = None
                    available = False

                # A characteristic IID may resolve to a Service or be unknown;
                # only a Characteristic can report a value, and only when it
                # has the paired read permission.
                if isinstance(char, Characteristic):
                    if HAP_PERMISSION_READ not in char.properties[PROP_PERMISSIONS]:
                        rep[HAP_REPR_STATUS] = (
                            HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC
                        )
                    elif available:
                        if char in write_responses:
                            # The connection that wrote reads its write
                            # response back; it wins over any read callback.
                            rep[HAP_REPR_VALUE] = write_responses[char]
                            rep[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
                        else:
                            # The stored value, optimistically: the read
                            # runs the delegate's ``on_get`` and refreshes it.
                            rep[HAP_REPR_VALUE] = char.value
                            rep[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
                            reads.append(_DelegateRead(rep, char))
            except CharacteristicError:
                logger.error(
                    "%s: Error getting value for characteristic %s.",
                    self.accessory.display_name,
                    (aid, iid),
                )
            except Exception:
                logger.exception(
                    "%s: Unexpected error getting value for characteristic %s.",
                    self.accessory.display_name,
                    (aid, iid),
                )

            chars.append(rep)
        return chars, reads

    def derive_session_key(
        self, connection: HAPConnectionHandle | None, salt: bytes, info: bytes
    ) -> bytes | None:
        """Derive a key from the pair-verify session key of ``connection``.

        For transports set up from the HAP session, e.g. HomeKit Data Stream.
        Never returns the session key itself, see
        :meth:`HAPServerProtocol.derive_key`. Returns ``None`` when the
        connection is unknown or not encrypted.
        """
        if connection is None:
            return None
        protocol = self.http_server.connections.get(connection)
        if protocol is None:
            return None
        return protocol.derive_key(salt, info)

    def _local_address(
        self, connection: HAPConnectionHandle | None, ipv6: bool
    ) -> str | None:
        """The accessory's address on a controller's connection.

        Returns None without such a connection or if its address is not of
        the asked IP version. An IPv4 address mapped into IPv6 counts as
        IPv4, a scope ID is dropped.
        """
        protocol = self.http_server.connections.get(connection) if connection else None
        transport = protocol.transport if protocol else None
        sockname = transport.get_extra_info("sockname") if transport else None
        if not sockname:
            return None
        try:
            address = ipaddress.ip_address(str(sockname[0]).split("%", 1)[0])
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address) if address.version == (6 if ipv6 else 4) else None

    def set_characteristics(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        *,
        client_uuid: UUID | None = None,
        connection: HAPConnectionHandle | None = None,
    ) -> dict[str, Any] | None:
        """Called from ``HAPServerHandler`` when iOS configures the characteristics.

        Returns ``None`` when every write succeeded without a write response,
        otherwise the per-characteristic results.

        Every write runs through the delegates of its characteristic and its
        service: the service-level pre-checks first, then the
        per-characteristic ``on_set``. Called directly, this method answers
        at once, optimistically, and runs the delegates as a job without a
        deadline; a controller's request awaits them instead, see
        :meth:`_set_characteristics_for_controller`.

        :param chars_query: A configuration query. For example:

        .. code-block:: python

           {
              "characteristics": [{
                 "aid": 1,
                 "iid": 2,
                 "value": False, # Value to set
                 "ev": True # (Un)subscribe for events from this characteristics.
                 "r": True # Request write response
              }]
           }

        :type chars_query: dict

        :param client_uuid: The paired controller that sent the request. Writes
            to characteristics restricted to admins are refused with -70401
            unless it is an admin.
        :type client_uuid: uuid.UUID or None

        :param connection: The connection the write came over, see
            :class:`~pyhap.connection.HAPConnectionHandle`. ``None`` for a
            call without one: it acts as the virtual connection of
            ``client_addr``, see :meth:`_connection_of`.
        """
        connection = connection or self._connection_of(client_addr)
        results, request = self._apply_characteristic_writes(
            chars_query, client_addr, client_uuid, connection, None
        )
        if request:
            self.add_job(self._dispatch_delegates(request))
        return _set_characteristics_response(results)

    def _set_characteristics_for_controller(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        timeout: float,
        *,
        client_uuid: UUID | None = None,
        connection: HAPConnectionHandle | None = None,
    ) -> Awaitable[dict[str, Any] | None]:
        """Apply a controller's write through its delegates, awaiting them.

        Returns an awaitable of the response. The service-level pre-checks
        run first, each with every written value of its service, then the
        per-characteristic ``on_set`` calls apply the surviving writes, all
        under the request deadline: a call still running after ``timeout``
        seconds is cancelled and its characteristic answers -70408, a
        refused or failed write is restored and notifies nobody. Cancelling
        the awaitable, e.g. because the connection closed, cancels the
        calls. A setter that answers its write response only after the
        connection closed does not get it kept: the store drops it, see
        :meth:`_store_write_response`.
        """
        connection = connection or self._connection_of(client_addr)
        results, request = self._apply_characteristic_writes(
            chars_query, client_addr, client_uuid, connection, timeout
        )
        return self._apply_delegate_writes(results, request)

    def _apply_characteristic_writes(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
        timeout: float | None,
    ) -> tuple[dict[int, dict[int, dict[str, Any]]], _WriteRequest]:
        """Validate and store the writes, collecting them for the delegates.

        The values are only stored here: the delegates of the request
        apply them, see :meth:`_dispatch_delegates` - a write its
        pre-checks or its ``on_set`` refuse is restored.
        """
        context = self._build_request_context(
            client_addr, client_uuid, connection, timeout
        )
        request = _WriteRequest(context, self.state.is_admin(client_uuid))
        results = self._run_characteristic_writes(
            chars_query, client_addr, client_uuid, connection, request
        )
        return results, request

    def _run_characteristic_writes(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
        request: _WriteRequest,
    ) -> dict[int, dict[int, dict[str, Any]]]:
        """Validate the writes of a request, storing them and collecting the work.

        A timed write (the request carries a ``pid``) first consumes its
        prepared write: one that was never prepared, was used already or has
        expired is ignored as a whole with -70410, as the spec requires. The
        ``ev`` flags of the request then (un)subscribe the connection and
        dispatch the subscribe hooks, see :meth:`_notify`; characteristics
        without the notify permission are reported -70406. Every remaining
        write is checked and stored per query, see :meth:`_store_write_query`.
        The delegates apply the collected writes later, see
        :meth:`_dispatch_delegates` - a write they refuse is restored.
        """
        queries: list[CharacteristicWrite] = chars_query[HAP_REPR_CHARS]

        timed_write = HAP_REPR_PID in chars_query
        if timed_write and not self._take_prepared_write(
            chars_query[HAP_REPR_PID], connection
        ):
            # The spec requires a timed write that was never prepared, was
            # already used or has expired to be ignored as a whole.
            ignored: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
            for query in queries:
                ignored[query[HAP_REPR_AID]][query[HAP_REPR_IID]] = {
                    HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
                }
            return ignored

        not_notifiable = self._notify(queries, client_addr, client_uuid, connection)

        results: dict[int, dict[int, dict[str, Any]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for aid, iid in not_notifiable:
            results[aid][iid] = {
                HAP_REPR_STATUS: HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED
            }

        for query in queries:
            self._store_write_query(
                results,
                request,
                query,
                not_notifiable,
                timed_write,
                connection,
                client_addr,
            )

        return results

    def _bridged_accessory(self, aid: int) -> Accessory | None:
        """The bridged accessory ``aid`` addresses, None when there is none.

        Accessory IIDs other than the primary accessory's only exist on
        bridged accessories.
        """
        accessory = self.accessory
        if isinstance(accessory, Bridge):
            return accessory.accessories.get(aid)
        return None

    def _store_write_query(
        self,
        results: dict[int, dict[int, dict[str, Any]]],
        request: _WriteRequest,
        query: CharacteristicWrite,
        not_notifiable: set[tuple[int, int]],
        timed_write: bool,
        connection: HAPConnectionHandle,
        client_addr: tuple[str, int],
    ) -> None:
        """Validate one write of a request, store its value, collect its delegates.

        A query without a value - a plain (un)subscribe - is listed with
        status 0, so the response reports it when other results exist. A
        query whose characteristic is unknown gets no result entry at all.
        What survives the checks - privileges, write permission, timed-write
        requirement, see :meth:`_write_permission_status` - is stored and
        collected for the delegates, see :meth:`_collect_delegate_write`.
        """
        aid = query[HAP_REPR_AID]
        iid = query[HAP_REPR_IID]
        if (aid, iid) in not_notifiable:
            return
        if HAP_REPR_VALUE not in query:
            # Listed with status 0 if the response has other results.
            results[aid][iid] = {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS}
            return
        value = query.get(HAP_REPR_VALUE)

        primary_accessory = self.accessory
        assert primary_accessory is not None  # noqa: S101
        if aid == primary_accessory.aid:
            acc = primary_accessory
        else:
            acc = self._bridged_accessory(aid)
        if acc is None:
            return
        char = acc.get_characteristic(aid, iid)
        if char is None:
            return

        status = self._write_permission_status(
            char, value, request.is_admin, timed_write, client_addr
        )
        if status is not None:
            results[aid][iid] = {HAP_REPR_STATUS: status}
            return

        # Only characteristics with the write response permission answer
        # a write with a value, and only when the controller asks for it.
        write_response = (
            bool(query.get(HAP_REPR_WRITE_RESPONSE))
            and HAP_PERMISSION_WRITE_RESPONSE in char.properties[PROP_PERMISSIONS]
        )
        # A new write replaces a former response of this connection. The
        # request of a readable write response or read back characteristic
        # is not shared, whatever the setter does, also not with ev
        # subscribers.
        if value is not None:
            self._write_responses.get(connection, {}).pop(char, None)
        private_write = (
            (write_response or char.read_back_response)
            and value is not None
            and HAP_PERMISSION_READ in char.properties[PROP_PERMISSIONS]
        )
        previous_value = char.value
        service = char.service
        assert service is not None  # noqa: S101

        results[aid][iid] = self._collect_delegate_write(
            request,
            client_addr,
            char,
            service,
            value,
            previous_value,
            write_response,
            private_write,
        )

    def _write_permission_status(
        self,
        char: Characteristic,
        value: Any,
        is_admin: bool,
        timed_write: bool,
        client_addr: tuple[str, int],
    ) -> int | None:
        """The status a write is refused with before any callback runs, or None.

        Privileges are checked before the permissions, so a controller that
        may not write the characteristic learns nothing else. Also for an
        explicit null, so no setter sees the characteristic.
        """
        if not is_admin and ADMIN_ONLY_WRITE in char.properties.get(
            PROP_ADMIN_ONLY_ACCESS, ()
        ):
            return HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES
        if (
            value is not None
            and HAP_PERMISSION_WRITE not in char.properties[PROP_PERMISSIONS]
        ):
            return HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC
        if (
            value is not None
            and not timed_write
            and HAP_PERMISSION_TIMED_WRITE in char.properties[PROP_PERMISSIONS]
        ):
            if self.enforce_timed_writes:
                return HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
            logger.debug(
                "%s: %s requires a timed write but was written without one",
                client_addr,
                char.display_name,
            )
        return None

    async def _apply_delegate_writes(
        self,
        results: dict[int, dict[int, dict[str, Any]]],
        request: _WriteRequest,
    ) -> dict[str, Any] | None:
        """Apply the writes of a request through their delegates, build its response.

        This is the awaitable body of :meth:`_set_characteristics_for_controller`:
        once :meth:`_dispatch_delegates` is done, every write has its status and
        optional write response, and the response is built from them.
        """
        await self._dispatch_delegates(request)
        return _set_characteristics_response(results)

    def _next_connection_generation(self) -> int:
        """The identity number of the next connection, see HAPConnectionHandle."""
        self._connection_generation += 1
        return self._connection_generation

    def _connection_of(
        self, client_addr: tuple[str, int] | None
    ) -> HAPConnectionHandle:
        """The handle a direct call without a connection acts as.

        One per client address, memoized: the state of separate direct
        calls with the same address must pair up - a prepared timed write
        with the request that consumes it, a stored write response with the
        read that reads it back. A controller's requests carry the handle of
        their connection instead, minted when it was made.
        """
        connection = self._direct_connections.get(client_addr)
        if connection is None:
            connection = self._direct_connections[client_addr] = HAPConnectionHandle(
                client_addr, self._next_connection_generation()
            )
        return connection

    def _is_open(self, connection: HAPConnectionHandle) -> bool:
        """Whether ``connection`` is a connection the server may still answer.

        The virtual handle of a direct call (see :meth:`_connection_of`)
        has no connection and is always open. A handle of a closed
        connection is not: its protocol is gone from the server's
        connections, so a late answer of it is dropped instead of kept,
        see :meth:`_store_write_response`.
        """
        return (
            connection in self._direct_connections.values()
            or connection in self.http_server.connections
        )

    def _build_request_context(
        self,
        client_addr: tuple[str, int] | None,
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
        timeout: float | None,
    ) -> Context:
        """The context of a request: what its delegate calls share.

        A direct call has no controller waiting on a timeout, so a
        ``timeout`` of ``None`` means no deadline. ``connection`` is the
        handle of the request's connection - a controller's the one of its
        connection, a direct call the virtual handle of its address, see
        :meth:`_connection_of`.
        """
        deadline = math.inf if timeout is None else time.monotonic() + timeout
        return Context(
            client_addr=client_addr,
            client_uuid=client_uuid,
            connection=connection,
            deadline=deadline,
        )

    @staticmethod
    def _remaining(deadline: float) -> float | None:
        """Seconds left of the deadline, or None when it is infinite."""
        if math.isinf(deadline):
            return None
        return max(0.0, deadline - time.monotonic())

    async def _await_calls(
        self,
        tasks: dict[asyncio.Future[Any], Any],
        deadline: float,
        on_cancel: Callable[[asyncio.Future[Any]], None] | None = None,
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        """Wait for ``tasks`` until ``deadline``, cancelling the ones not done.

        Returns the (done, not done) sets; every not-done task is cancelled
        with a late-result retriever attached. When the wait itself is
        cancelled, every task is cancelled the same way - or settled with
        ``on_cancel`` when it is already done - and the CancelledError
        re-raised.
        """
        try:
            done, not_done = await asyncio.wait(
                tasks, timeout=self._remaining(deadline)
            )
        except asyncio.CancelledError:
            for task in tasks:
                if on_cancel is not None and task.done():
                    on_cancel(task)
                else:
                    task.cancel()
                    task.add_done_callback(_retrieve_late_result)
            raise
        for task in not_done:
            task.cancel()
            task.add_done_callback(_retrieve_late_result)
        return done, not_done

    @staticmethod
    def _outcome_of(task: asyncio.Future[Any]) -> _CallOutcome:
        """The outcome of a finished delegate call.

        A curated :class:`~pyhap.errors.HapStatusError` answers with its
        status; any other failure answers ``-70402``, like the legacy
        callbacks do.
        """
        try:
            return _CallOutcome(task.result(), None, None)
        except HapStatusError as e:
            return _CallOutcome(None, e.status, e)
        except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001 - carried for the caller to log
            return _CallOutcome(
                None, HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE, e
            )

    async def _read_delegate_values(
        self,
        request: _ReadRequest,
        answer: Callable[[_DelegateRead, Any, int | None], None],
    ) -> None:
        """Run the delegate reads of a request concurrently, answering each.

        Every read runs under the context's deadline; one that is not done
        by it is cancelled and answers ``-70408``. Its answer is validated
        and stored like a legacy getter's, then handed to ``answer`` with
        the rep it fills - a failure arrives with its status instead of
        a value.
        """
        context = request.context
        tasks: dict[asyncio.Future[Any], _DelegateRead] = {}
        for read in request.reads:
            tasks[asyncio.ensure_future(read.char.dispatch_get(context))] = read
        if not tasks:
            return
        done, not_done = await self._await_calls(tasks, context.deadline)
        for task in not_done:
            read = tasks[task]
            logger.warning(
                "%s: Timed out getting characteristic %s",
                context.client_addr,
                read.char.display_name,
            )
            self._finish_read(read, answer, HAP_SERVER_STATUS.OPERATION_TIMED_OUT)
        for task in done:
            read = tasks[task]
            outcome = self._outcome_of(task)
            if outcome.error is not None:
                if isinstance(outcome.error, HapStatusError):
                    # The read answers the controller with its own HAP status.
                    logger.debug(
                        "%s: Characteristic %s answered status %s",
                        context.client_addr,
                        read.char.display_name,
                        outcome.status,
                    )
                else:
                    logger.error(
                        "%s: Error getting value for characteristic %s",
                        context.client_addr,
                        read.char.display_name,
                        exc_info=outcome.error,
                    )
                self._finish_read(read, answer, outcome.status)
            else:
                self._finish_read(read, answer, None, outcome.value)

    @staticmethod
    def _finish_read(
        read: _DelegateRead,
        answer: Callable[[_DelegateRead, Any, int | None], None],
        status: int | None,
        value: Any = None,
    ) -> None:
        """Validate and store a delegate's read answer, then shape its rep.

        Like a legacy getter's answer, the value is stored on the
        characteristic - subscribers are not told, a read is not a change.
        An answer that does not fit the characteristic's format fails with
        ``-70402``, like a getter's would.
        """
        if status is None:
            try:
                if read.char._always_null or value is None:
                    value = None
                else:
                    value = read.char.to_valid_value(value)
                read.char.value = value
            except Exception:
                logger.exception(
                    "Error while storing the value read of characteristic %s",
                    read.char.display_name,
                )
                value = None
                status = HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
        answer(read, value, status)

    @staticmethod
    def _answer_char_read(read: _DelegateRead, value: Any, status: int | None) -> None:
        """Answer a characteristics read: the value or the failure status."""
        if status is None:
            read.rep[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
            read.rep[HAP_REPR_VALUE] = value
        else:
            read.rep.pop(HAP_REPR_VALUE, None)
            read.rep[HAP_REPR_STATUS] = status

    @staticmethod
    def _answer_accessory_read(
        read: _DelegateRead, value: Any, status: int | None
    ) -> None:
        """Answer an accessories read, which carries no per-char status.

        A failure lists the characteristic without a value, like a failing
        getter does.
        """
        if status is None:
            read.rep[HAP_REPR_VALUE] = value
        else:
            read.rep.pop(HAP_REPR_VALUE, None)

    async def _dispatch_delegates(self, request: _WriteRequest) -> None:
        """Call the delegates of the writes of a request.

        The service-level pre-checks run first, concurrently, each with
        every written value of its service: a curated
        :class:`~pyhap.errors.HapStatusError` refuses the writes of its
        service - values are restored, subscribers are told nothing - and
        so does a timeout or any other pre-check failure, with its status.
        The surviving writes run concurrently, each under the context's
        deadline; one that fails or misses it restores the previous value
        and notifies nobody.
        """
        if request.pre_checks:
            await self._run_pre_checks(request)
        if request.writes:
            await self._run_char_writes(request)

    async def _run_pre_checks(self, request: _WriteRequest) -> None:
        """Run the pre-checks of the request, refusing the writes they refuse.

        Every written service gets one: the ``on_set`` of its delegate,
        with every written value of the service - the legacy grouped
        setter of a service without a delegate runs here too, wrapped.
        An accessory with a legacy grouped setter gets one as well, with
        the values of all its written services.
        """
        context = request.context
        client_addr = context.client_addr
        # The services each pre-check refuses the writes of: its own for a
        # service pre-check, all written services of the accessory for an
        # accessory one.
        tasks: dict[asyncio.Future[Any], tuple[Service, ...]] = {}
        for service, values in request.pre_checks.items():
            tasks[
                asyncio.ensure_future(
                    service.dispatch_set(values, context, is_admin=request.is_admin)
                )
            ] = (service,)
        accessories: dict[Accessory, dict[Service, dict[Characteristic, Any]]] = {}
        for service, values in request.pre_checks.items():
            acc = service.broker
            if acc is not None and acc.setter_callback:
                accessories.setdefault(acc, {})[service] = values
        for acc, updates_by_service in accessories.items():
            services = tuple(updates_by_service)
            tasks[
                asyncio.ensure_future(acc._dispatch_legacy_setter(updates_by_service))
            ] = services
        if not tasks:
            return
        done, not_done = await self._await_calls(tasks, context.deadline)
        for task in not_done:
            services = tasks[task]
            logger.warning(
                "%s: Timed out checking the writes of %s",
                client_addr,
                ", ".join(service.display_name or "" for service in services),
            )
            self._veto_writes(request, services, HAP_SERVER_STATUS.OPERATION_TIMED_OUT)
        for task in done:
            services = tasks[task]
            outcome = self._outcome_of(task)
            if outcome.error is not None:
                status = outcome.status
                assert status is not None  # noqa: S101 - an error outcome carries a status
                if isinstance(outcome.error, HapStatusError):
                    # The pre-check answers the controller with its own status.
                    logger.debug(
                        "%s: A pre-check refused the writes of %s with status %s",
                        client_addr,
                        ", ".join(service.display_name or "" for service in services),
                        status,
                    )
                else:
                    logger.error(
                        "%s: Error checking the writes of %s",
                        client_addr,
                        ", ".join(service.display_name or "" for service in services),
                        exc_info=outcome.error,
                    )
                self._veto_writes(request, services, status)

    async def _run_char_writes(self, request: _WriteRequest) -> None:
        """Apply the writes the gates left standing, concurrently.

        Every write's delegate call runs under the request's deadline, see
        :meth:`_await_calls`: one still running when it passes is cancelled,
        answers -70408 and has its previous value restored. A finished call
        is settled by :meth:`_finish_task_write`. When the wait itself is
        cancelled, e.g. because the connection closed, finished calls are
        still settled and the rest are cancelled.
        """
        context = request.context
        client_addr = context.client_addr
        tasks: dict[asyncio.Future[Any], _DelegateWrite] = {}
        for write in request.writes:
            tasks[
                asyncio.ensure_future(
                    write.char.dispatch_set(
                        write.value, context, is_admin=request.is_admin
                    )
                )
            ] = write
        if not tasks:
            return
        done, not_done = await self._await_calls(
            tasks,
            context.deadline,
            on_cancel=lambda task: self._finish_task_write(task, tasks[task], context),
        )
        for task in not_done:
            write = tasks[task]
            logger.warning(
                "%s: Timed out setting characteristic %s",
                client_addr,
                write.char.display_name,
            )
            write.result[HAP_REPR_STATUS] = HAP_SERVER_STATUS.OPERATION_TIMED_OUT
            self._restore_write_value(write)
        for task in done:
            self._finish_task_write(task, tasks[task], context)

    def _finish_task_write(
        self,
        task: asyncio.Future[Any],
        write: _DelegateWrite,
        context: Context,
    ) -> None:
        """Settle one finished write: its status, its response, its notify.

        A failed write answers its status - a curated
        :class:`~pyhap.errors.HapStatusError` status, else -70402 - and puts
        the previous value back, notifying nobody. An applied write settles
        its private read-back bookkeeping, keeps the response for the
        writing connection (dropped when it closed meanwhile, see
        :meth:`_store_write_response`) and notifies the subscribers of the
        new value, unless the write is private.
        """
        client_addr = context.client_addr
        outcome = self._outcome_of(task)
        if outcome.error is not None:
            if isinstance(outcome.error, HapStatusError):
                # The delegate answers the controller with its own HAP status.
                logger.debug(
                    "%s: Characteristic %s answered status %s",
                    client_addr,
                    write.char.display_name,
                    outcome.status,
                )
            else:
                logger.error(
                    "%s: Error while setting characteristic %s",
                    client_addr,
                    write.char.display_name,
                    exc_info=outcome.error,
                )
            write.result[HAP_REPR_STATUS] = outcome.status
            self._restore_write_value(write)
            return
        # Complete the applied write: read back bookkeeping, response, notify.
        response = outcome.value
        if write.private_write:
            response = _settle_private_write(
                write.char, write.previous_value, write.value, response
            )
        if response is not None:
            # A response of a connection that closed meanwhile is dropped by
            # the store: the response of a request is answered once, to the
            # writing connection.
            self._keep_response(
                write.result,
                write.char,
                response,
                write.write_response,
                context.connection,
            )
        write.char.client_update_finished(
            write.previous_value, context.connection, notify=not write.private_write
        )

    @staticmethod
    def _restore_write_value(write: _DelegateWrite) -> None:
        """Put back what a refused write changed."""
        if write.char.value != write.previous_value:
            write.char.value = write.previous_value

    def _veto_writes(
        self,
        request: _WriteRequest,
        services: tuple[Service, ...],
        status: int,
    ) -> None:
        """Refuse the writes of ``services``: restore, tell nobody, answer status."""
        kept: list[_DelegateWrite] = []
        for write in request.writes:
            if write.service not in services:
                kept.append(write)
                continue
            write.result[HAP_REPR_STATUS] = status
            self._restore_write_value(write)
        request.writes = kept

    def _collect_delegate_write(
        self,
        request: _WriteRequest,
        client_addr: tuple[str, int],
        char: Characteristic,
        service: Service,
        value: Any,
        previous_value: Any,
        write_response: bool,
        private_write: bool,
    ) -> dict[str, Any]:
        """Store a write and collect it for its delegates, return its result.

        The written service gets a pre-check whatever the characteristic
        itself uses, so the pre-check sees every value of the request.
        The value is validated and stored now: the delegates apply it once
        the pre-checks accepted, and a write they refuse is restored. An
        explicit null answers invalid, like the legacy path - nothing is
        applied and no callback runs.
        """
        request.pre_checks.setdefault(service, {})[char] = value
        result: dict[str, Any] = {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS}
        if value is None:
            result[HAP_REPR_STATUS] = HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
            return result
        try:
            previous_value = char._store_client_update(value)
        except Exception:
            logger.exception(
                "%s: Error while setting characteristic %s to %s",
                client_addr,
                char.display_name,
                value,
            )
            result[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
            return result
        value = char.value
        if private_write:
            # The request of a readable write response or read back
            # characteristic is not shared, whatever the delegates do: it
            # never lives in the value while they run, they receive it as
            # their argument, and the finish settles what they answer, see
            # ``_settle_private_write``.
            char.value = previous_value
        request.writes.append(
            _DelegateWrite(
                char,
                value,
                previous_value,
                result,
                write_response,
                private_write,
                service,
            )
        )
        return result

    def _keep_response(
        self,
        result: dict[str, Any],
        char: Characteristic,
        response: HAPValue,
        write_response: bool,
        connection: HAPConnectionHandle | None,
    ) -> None:
        """Keep what a setter returned as a write response or read back value."""
        if write_response:
            result[HAP_REPR_VALUE] = self._store_write_response(
                char, response, connection
            )
        elif (
            char.read_back_response
            and HAP_PERMISSION_READ in char.properties[PROP_PERMISSIONS]
        ):
            self._store_write_response(char, response, connection)

    def _take_prepared_write(self, pid: Any, connection: HAPConnectionHandle) -> bool:
        """Consume the prepared write ``pid`` and return whether it is still valid."""
        if not _is_hap_int(pid):
            # Controller input. A list or dict is not even hashable.
            return False
        expires_at = self.prepared_writes.get(connection, {}).pop(pid, None)
        return expires_at is not None and time.monotonic() <= expires_at

    def _store_write_response(
        self,
        char: Characteristic,
        response: HAPValue,
        connection: HAPConnectionHandle | None,
    ) -> HAPValue:
        """Keep the write response and return what to send.

        The written value of a write response characteristic, such as an SDP
        offer, is a request. What the accessory answered is its state. If the
        characteristic is readable, only the connection that wrote reads the
        response back, until it closes, and it wins over the getter and a later
        ``set_value``. A setter that answers after the connection closed does
        not get its response kept - the store drops it, see ``_is_open`` -
        whatever it returns. Other connections read the characteristic's own
        value, which the write leaves as it was. A write without ``r`` stays
        readable for everyone and notifies ``ev`` subscribers as any write
        does, unless the characteristic has ``read_back_response``, which
        keeps the response the same way without a write response.
        """
        readable = HAP_PERMISSION_READ in char.properties[PROP_PERMISSIONS]
        try:
            valid: HAPValue = char.to_valid_value(response)
        except ValueError:
            logger.debug(
                "%s: not keeping the write response, it is invalid: %s",
                char.display_name,
                response,
            )
            return response
        if readable:
            if connection is not None and self._is_open(connection):
                self._write_responses.setdefault(connection, {})[char] = valid
        else:
            char.value = valid
        return valid

    def prepare(
        self,
        prepare_query: dict[str, Any],
        client_addr: tuple[str, int],
        *,
        connection: HAPConnectionHandle | None = None,
    ) -> dict[str, int]:
        """Called from ``HAPServerHandler`` when iOS wants to prepare a write.

        :param prepare_query: A prepare query. For example:

        .. code-block:: python

           {
              "ttl": 10000, # in milliseconds
              "pid": 12345678,
           }

        :type prepare_query: dict

        :param client_addr: The client that prepares the write.
        :type client_addr: tuple <str, int>

        :param connection: The connection the prepare came over, see
            :class:`~pyhap.connection.HAPConnectionHandle`. ``None`` for a
            call without one: it acts as the virtual connection of
            ``client_addr``, see :meth:`_connection_of`.
        :type connection: HAPConnectionHandle or None
        """
        connection = connection or self._connection_of(client_addr)
        if not isinstance(prepare_query, dict):
            return {HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST}
        ttl = prepare_query.get(HAP_REPR_TTL)
        pid = prepare_query.get(HAP_REPR_PID)
        if not (_is_hap_int(ttl) and _is_hap_int(pid)):
            return {HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST}
        try:
            now = time.monotonic()
            pending = self.prepared_writes.setdefault(connection, {})
            # Prune expired entries and cap the number of pending writes so a
            # paired client cannot grow prepared_writes without bound by
            # registering endless unique pids with arbitrary TTLs.
            for stale_pid in [
                prepared_pid
                for prepared_pid, expires_at in pending.items()
                if expires_at < now
            ]:
                del pending[stale_pid]
            if len(pending) >= MAX_PREPARED_WRITES_PER_CLIENT:
                del pending[min(pending, key=pending.__getitem__)]
            pending[pid] = now + min(ttl / 1000, MAX_PREPARED_WRITE_TTL_SECONDS)
        except OverflowError:
            return {HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST}

        return {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS}

    def signal_handler(self, _signal: Any, _frame: Any) -> None:
        """Stops the AccessoryDriver for a given signal.

        An AccessoryDriver can be registered as a signal handler with this method. For
        example, you can register it for a KeyboardInterrupt as follows:
        >>> import signal
        >>> signal.signal(signal.SIGINT, anAccDriver.signal_handler)

        Now, when the user hits Ctrl+C, the driver will stop its accessory, the
        HAP server and everything else that needs stopping and will exit gracefully.
        """
        try:
            self.stop()
        except Exception as e:
            logger.error("Could not stop AccessoryDriver because of error: %s", e)
            raise

    def _notify(
        self,
        queries: list[CharacteristicWrite],
        client_addr: tuple[str, int],
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
    ) -> set[tuple[int, int]]:
        """Notify the driver that the client has subscribed or unsubscribed.

        Characteristics without the notify permission are left alone; their
        ``(aid, iid)`` pairs are returned so the request can report
        -70406 for them. A characteristic with a delegate also gets its
        ``on_subscribe`` / ``on_unsubscribe`` hook, on the transition: a
        request that subscribes an already subscribed controller, or
        unsubscribes one that is not, runs no hook.
        """
        assert self.accessory is not None  # noqa: S101
        not_notifiable: set[tuple[int, int]] = set()
        for query in queries:
            if HAP_PERMISSION_NOTIFY not in query:
                continue
            aid = query[HAP_REPR_AID]
            iid = query[HAP_REPR_IID]
            ev = query[HAP_PERMISSION_NOTIFY]

            char = self.accessory.get_characteristic(aid, iid)
            if (
                char is not None
                and HAP_PERMISSION_NOTIFY not in char.properties[PROP_PERMISSIONS]
            ):
                not_notifiable.add((aid, iid))
                continue

            char_topic = get_topic(aid, iid)
            was_subscribed = connection in self.topics.get(char_topic, ())
            action = "Subscribed" if ev else "Unsubscribed"
            logger.debug("%s client %s to topic %s", action, client_addr, char_topic)
            self.async_subscribe_client_topic(connection, char_topic, ev)
            if char is not None and ev != was_subscribed:
                self._dispatch_subscription(
                    char, ev, client_addr, client_uuid, connection
                )
        return not_notifiable

    def _dispatch_subscription(
        self,
        characteristic: Characteristic,
        subscribe: bool,
        client_addr: tuple[str, int] | None,
        client_uuid: UUID | None,
        connection: HAPConnectionHandle,
    ) -> None:
        """Run a characteristic's ``on_subscribe`` or ``on_unsubscribe``, isolated.

        The hook runs as a job without a deadline: the request, or the
        connection close, that caused it never waits for it. Its exception
        is logged, and the subscription itself has already registered by
        the time the hook runs.
        """
        context = self._build_request_context(
            client_addr, client_uuid, connection, None
        )
        hook, args = characteristic.subscription_hook(subscribe, context)
        self.add_job(
            self._run_isolated_hook,
            hook,
            args,
            "on_subscribe" if subscribe else "on_unsubscribe",
            client_addr,
        )
