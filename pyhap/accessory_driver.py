"""AccessoryDriver - glues together the HAP Server, accessories and mDNS advertising.

Sending updates to clients

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
import base64
from collections import defaultdict
from collections.abc import Awaitable, Coroutine
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import ipaddress
import logging
import os
import re
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
from pyhap.const import (
    HAP_PERMISSION_NOTIFY,
    HAP_PERMISSION_READ,
    HAP_PERMISSION_TIMED_WRITE,
    HAP_PERMISSION_WRITE,
    HAP_PERMISSION_WRITE_RESPONSE,
    HAP_PROTOCOL_SHORT_VERSION,
    HAP_REPR_ACCS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_PID,
    HAP_REPR_STATUS,
    HAP_REPR_TTL,
    HAP_REPR_VALUE,
    HAP_REPR_WRITE_RESPONSE,
    STANDALONE_AID,
)
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_server import HAPServer
from pyhap.hsrp import Server as SrpServer
from pyhap.loader import Loader
from pyhap.params import get_srp_context
from pyhap.service import Service
from pyhap.state import State

from .const import HAP_SERVER_STATUS
from .util import callback

logger = logging.getLogger(__name__)

SERVICE_CALLBACK = "callback"
SERVICE_CHARS = "chars"
SERVICE_IIDS = "iids"
HAP_SERVICE_TYPE = "_hap._tcp.local."
VALID_MDNS_REGEX = re.compile(r"[^A-Za-z0-9\-]+")
LEADING_TRAILING_SPACE_DASH = re.compile(r"^[ -]+|[ -]+$")
DASH_REGEX = re.compile(r"[-]+")
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


def _wrap_char_setter(
    char: Any, value: Any, client_addr: Any, notify: bool = True
) -> tuple[int, Any]:
    """Process an characteristic setter callback trapping and logging all exceptions."""
    try:
        # The driver finishes the update of a coroutine setter once it is done.
        response = char.client_update_value(
            value, client_addr, notify=notify, defer_notify=True
        )
    except Exception:
        logger.exception(
            "%s: Error while setting characteristic %s to %s",
            client_addr,
            char.display_name,
            value,
        )
        return HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE, None
    return HAP_SERVER_STATUS.SUCCESS, response


def _close_unsupported_coroutine(result: Any, name: str) -> None:
    """Close a coroutine an accessory or service setter returned.

    Only characteristic setters may be coroutine functions.
    """
    if inspect.iscoroutine(result):
        result.close()
        logger.warning(
            "%s: only characteristic setters may be coroutine functions", name
        )


def _wrap_acc_setter(acc: Any, updates_by_service: Any, client_addr: Any) -> Any:
    """Process an accessory setter callback trapping and logging all exceptions."""
    try:
        _close_unsupported_coroutine(
            acc.setter_callback(updates_by_service), acc.display_name
        )
    except Exception:
        logger.exception(
            "%s: Error while setting characteristics to %s for the %s accessory",
            updates_by_service,
            client_addr,
            acc,
        )
        return HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    return HAP_SERVER_STATUS.SUCCESS


def _wrap_service_setter(service: Any, chars: Any, client_addr: Any) -> Any:
    """Process a service setter callback trapping and logging all exceptions."""
    # Ideally this would pass the chars as is without converting
    # them to the display_name, but that would break existing
    # consumers of the data.
    service_chars = {char.display_name: value for char, value in chars.items()}
    try:
        _close_unsupported_coroutine(
            service.setter_callback(service_chars), service.display_name
        )
    except Exception:
        logger.exception(
            "%s: Error while setting characteristics to %s for the %s service",
            service_chars,
            client_addr,
            service.display_name,
        )
        return HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    return HAP_SERVER_STATUS.SUCCESS


class _PendingWrite(NamedTuple):
    """A characteristic write whose setter returned a coroutine."""

    result: dict[str, Any]  # The result entry to complete.
    coro: Coroutine[Any, Any, Any]
    write_response: bool  # Whether the controller asked for a write response.
    char: Characteristic
    previous_value: Any  # The value before the write, to tell if it changed.
    notify: bool  # Whether subscribers learn the value once the setter is done.


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


class AccessoryMDNSServiceInfo(ServiceInfo):
    """A mDNS service info representation of an accessory."""

    def __init__(
        self, accessory: Any, state: State, zeroconf_server: Any = None
    ) -> None:
        self.accessory = accessory
        self.state: State = state

        adv_data = self._get_advert_data()
        valid_name = self._valid_name()
        short_mac = self.state.mac[-8:].replace(":", "")
        # Append part of MAC address to prevent name conflicts
        name = f"{valid_name} {short_mac}.{HAP_SERVICE_TYPE}"
        valid_host_name = self._valid_host_name()
        server = zeroconf_server or f"{valid_host_name}-{short_mac}.local."
        super().__init__(
            HAP_SERVICE_TYPE,
            name=name,
            server=server,
            port=self.state.port,
            weight=0,
            priority=0,
            properties=adv_data,
            parsed_addresses=self.state.addresses,
        )

    def _valid_name(self) -> str:
        return re.sub(
            LEADING_TRAILING_SPACE_DASH,
            "",
            re.sub(VALID_MDNS_REGEX, " ", self.accessory.display_name),
        )

    def _valid_host_name(self) -> str:
        return re.sub(
            DASH_REGEX,
            "-",
            re.sub(VALID_MDNS_REGEX, " ", self.accessory.display_name)
            .strip()
            .replace(" ", "-")
            .strip("-"),
        )

    def _setup_hash(self) -> str:
        setup_hash_material = self.state.setup_id + self.state.mac
        temp_hash = hashlib.sha512()
        temp_hash.update(setup_hash_material.encode())
        return base64.b64encode(temp_hash.digest()[:4]).decode()

    def _get_advert_data(self) -> dict[str, str]:
        """Generate advertisement data from the accessory."""
        return {
            "md": self._valid_name(),
            "pv": HAP_PROTOCOL_SHORT_VERSION,
            "id": self.state.mac,
            # represents the 'configuration version' of an Accessory.
            # Increasing this 'version number' signals iOS devices to
            # re-fetch accessories data.
            "c#": str(self.state.config_version),
            "s#": "1",  # 'accessory state'
            "ff": "0",
            "ci": str(self.accessory.category),
            # 'sf == 1' means "discoverable by HomeKit iOS clients"
            "sf": "0" if self.state.paired else "1",
            "sh": self._setup_hash(),
        }


class AccessoryDriver:
    """An AccessoryDriver mediates between incoming requests from the HAPServer and
    the Accessory.

    The driver starts and stops the HAPServer, the mDNS advertisements and responds
    to events from the HAPServer.
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
        self.topics = {}  # topic: set of (address, port) of subscribed clients
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
        self.prepared_writes = {}
        # Write responses of readable characteristics, per client.
        self._write_responses: dict[
            tuple[str, int], dict[Characteristic, HAPValue]
        ] = {}

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
            self.loop.close()
            logger.info("Closed the event loop")

    def start_service(self) -> None:
        """Start the service."""
        self._validate_start()
        self.add_job(self.async_start)

    def _validate_start(self) -> None:
        """Validate we can start."""
        if self.accessory is None:
            raise ValueError(
                "You must assign an accessory to the driver, before you can start it."
            )

    async def async_start(self) -> None:
        """Starts the accessory.

        - Call the accessory's run method.
        - Start handling accessory events.
        - Start the HAP server.
        - Publish a mDNS advertisement.
        - Print the setup QR code if the accessory is not paired.

        All of the above are started in separate threads. Accessory thread is set as
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
            await self.advertiser.async_register_service(
                self.mdns_service_info, cooperating_responders=True
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
        """Method to stop pyhap."""
        self.add_job(self.async_stop)

    async def async_stop(self) -> None:
        """Stops the AccessoryDriver and shutdown all remaining tasks."""
        self.stop_event.set()
        accessory = self.accessory
        assert accessory is not None  # noqa: S101
        assert self.aio_stop_event is not None  # noqa: S101
        logger.debug("Stopping HAP server and event sending")
        if self.advertise:
            assert self.advertiser is not None  # noqa: S101
            assert self.mdns_service_info is not None  # noqa: S101
            logger.debug("Stopping mDNS advertising for %s", accessory.display_name)
            await self.advertiser.async_unregister_service(self.mdns_service_info)
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
        """Add job to executor pool."""
        if target is None:
            raise ValueError("Don't call add_job with None.")
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    @util.callback
    def async_add_job(self, target: Any, *args: Any) -> Any:
        """Add job from within the event loop."""
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
        """Add top level accessory to driver."""
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
        self, client: Any, topic: str, subscribe: bool = True
    ) -> None:
        """(Un)Subscribe the given client from the given topic.

        This method must be run in the event loop.

        :param client: A client (address, port) tuple that should be subscribed.
        :type client: tuple <str, int>

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

    def connection_lost(self, client: Any) -> None:
        """Called when a connection is lost to a client.

        This method must be run in the event loop.

        :param client: A client (address, port) tuple that should be unsubscribed.
        :type client: tuple <str, int>
        """
        client_topics = []
        for topic, subscribed_clients in self.topics.items():
            if client in subscribed_clients:
                # Make a copy to avoid changing
                # self.topics during iteration
                client_topics.append(topic)

        for topic in client_topics:
            self.async_subscribe_client_topic(client, topic, subscribe=False)
        self.prepared_writes.pop(client, None)
        # Deferred, so a write response that is already on its way is dropped too.
        self.loop.call_soon(self._write_responses.pop, client, None)
        accessory = self.accessory
        if accessory is None:
            return
        try:
            accessory.client_disconnected(client)
        except Exception:
            logger.exception(
                "%s failed to handle a closed connection", accessory.display_name
            )

    def publish(
        self,
        data: dict[str, Any],
        sender_client_addr: Any = None,
        immediate: bool = False,
    ) -> None:
        """Publishes an event to the client.

        The publishing occurs only if the current client is subscribed to the topic for
        the aid and iid contained in the data.

        :param data: The data to publish. It must at least contain the keys "aid" and
            "iid".
        :type data: dict
        """
        topic = get_topic(data[HAP_REPR_AID], data[HAP_REPR_IID])
        if topic not in self.topics:
            return

        if threading.current_thread() == self.tid:
            self.async_send_event(topic, data, sender_client_addr, immediate)
            return

        self.loop.call_soon_threadsafe(
            self.async_send_event, topic, data, sender_client_addr, immediate
        )

    def async_send_event(
        self,
        topic: str,
        data: dict[str, Any],
        sender_client_addr: Any,
        immediate: bool,
    ) -> None:
        """Send an event to a client.

        Must be called in the event loop
        """
        assert self.aio_stop_event is not None  # noqa: S101
        if self.aio_stop_event.is_set():
            return

        subscribed_clients = self.topics.get(topic, [])
        logger.debug(
            "Send event: topic(%s), data(%s), sender_client_addr(%s)",
            topic,
            data,
            sender_client_addr,
        )
        unsubs = []
        for client_addr in subscribed_clients:
            if sender_client_addr and sender_client_addr == client_addr:
                logger.debug(
                    "Skip sending event to client since "
                    "its the client that made the characteristic change: %s",
                    client_addr,
                )
                continue
            logger.debug(
                "Sending event to client: %s, immediate: %s", client_addr, immediate
            )
            pushed = self.http_server.push_event(data, client_addr, immediate)
            if not pushed:
                logger.debug(
                    "Could not send event to %s, probably stale socket.", client_addr
                )
                unsubs.append(client_addr)
                # Maybe consider removing the client_addr from every topic?

        for client_addr in unsubs:
            self.async_subscribe_client_topic(client_addr, topic, False)

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
        """Updates the mDNS service info for the accessory."""
        self.loop.call_soon_threadsafe(self.async_update_advertisement)

    @callback
    def async_update_advertisement(self) -> None:
        """Updates the mDNS service info for the accessory from the event loop."""
        logger.debug("Updating mDNS advertisement")
        if not self.advertise:
            logger.debug("Not updating mDNS advertisement: advertising is disabled")
            return
        assert self.advertiser is not None  # noqa: S101
        self.mdns_service_info = AccessoryMDNSServiceInfo(
            self.accessory, self.state, self.zeroconf_server
        )
        util.async_create_background_task(
            self.advertiser.async_update_service(self.mdns_service_info)
        )

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
        """Saves the state of the accessory.

        Must run in executor.
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
        """Load the persist file.

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
        """Create an SRP verifier for the accessory's info."""
        # TODO: Move the below hard-coded values somewhere nice.
        ctx = get_srp_context(3072, hashlib.sha512, 16)
        verifier = SrpServer(ctx, b"Pair-Setup", self.state.pincode)
        self._srp_verifier = verifier

    @property
    def accessories_hash(self) -> str:
        """Hash the get_accessories response to track configuration changes."""
        # We pass include_value=False to avoid including the value
        # of the characteristics in the hash. This is because the
        # value of the characteristics is not used by iOS to determine
        # if the accessory configuration has changed. It only uses the
        # characteristics metadata. If we included the value in the hash
        # then iOS would think the accessory configuration has changed
        # every time a characteristic value changed.
        return hashlib.sha512(
            util.to_sorted_hap_json(self.get_accessories(include_value=False))
        ).hexdigest()

    def get_accessories(self, include_value: bool = True) -> dict[str, Any]:
        """Returns the accessory in HAP format.

        :return: An example HAP representation is:

        .. code-block:: python

           {
              "accessories": [
                 "aid": 1,
                 "services": [
                    "iid": 1,
                    "type": ...,
                    "characteristics": [{
                       "iid": 2,
                       "type": ...,
                       "description": "CurrentTemperature",
                       ...
                    }]
                 ]
              ]
           }

        :rtype: dict
        """
        assert self.accessory is not None  # noqa: S101
        hap_rep = self.accessory.to_HAP(include_value=include_value)
        if not isinstance(hap_rep, list):
            hap_rep = [
                hap_rep,
            ]
        logger.debug("Get accessories response: %s", hap_rep)
        return {HAP_REPR_ACCS: hap_rep}

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
        """
        assert self.accessory is not None  # noqa: S101
        write_responses = (
            self._write_responses.get(client_addr, {}) if client_addr else {}
        )
        chars = []
        for aid_iid in char_ids:
            aid, iid = (int(i) for i in aid_iid.split("."))
            rep = {
                HAP_REPR_AID: aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            }

            try:
                if aid == STANDALONE_AID:
                    char = self.accessory.iid_manager.get_obj(iid)
                    available = self.accessory.available
                else:
                    # Accessory IIDs other than the standalone one only exist
                    # on bridged accessories; anything else is unknown and
                    # keeps the per-characteristic failure status.
                    bridge = self.accessory
                    if isinstance(bridge, Bridge):
                        acc = bridge.accessories.get(aid)
                        if acc is None:
                            continue
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
                        rep[HAP_REPR_VALUE] = (
                            write_responses[char]
                            if char in write_responses
                            else char.get_value()
                        )
                        rep[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
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
        logger.debug("Get chars response: %s", chars)
        return {HAP_REPR_CHARS: chars}

    def derive_session_key(
        self, client_addr: tuple[str, int] | None, salt: bytes, info: bytes
    ) -> bytes | None:
        """Derive a key from the pair-verify session key of ``client_addr``.

        For transports set up from the HAP session, e.g. HomeKit Data Stream.
        Never returns the session key itself, see
        :meth:`HAPServerProtocol.derive_key`. Returns ``None`` when the
        connection is unknown or not encrypted.
        """
        if client_addr is None:
            return None
        connection = self.http_server.connections.get(client_addr)
        if connection is None:
            return None
        return connection.derive_key(salt, info)

    def _local_address(
        self, client_addr: tuple[str, int] | None, ipv6: bool
    ) -> str | None:
        """The accessory's address on a controller's connection.

        Returns None without such a connection or if its address is not of
        the asked IP version. An IPv4 address mapped into IPv6 counts as
        IPv4, a scope ID is dropped.
        """
        connection = (
            self.http_server.connections.get(client_addr) if client_addr else None
        )
        transport = connection.transport if connection else None
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
    ) -> dict[str, Any] | None:
        """Called from ``HAPServerHandler`` when iOS configures the characteristics.

        Returns ``None`` when every write succeeded without a write response,
        otherwise the per-characteristic results.

        A characteristic setter may be a coroutine function. Writes from a
        controller wait for it before they are answered. Called directly, this
        method only schedules the coroutine and does not wait for it. Either
        way subscribers learn the value once the coroutine succeeded, and it
        should use its argument, not the characteristic's value, which a write
        response request has already put back. Accessory and service setters
        must not be coroutine functions. A coroutine they return is closed
        with a warning.

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
        """
        results, pending = self._apply_characteristic_writes(
            chars_query, client_addr, client_uuid
        )
        for write in pending:
            self.add_job(self._finish_pending_write(write, client_addr))
        return _set_characteristics_response(results)

    async def _finish_pending_write(
        self, write: _PendingWrite, client_addr: tuple[str, int]
    ) -> None:
        """Run a coroutine setter of a direct call, then tell subscribers."""
        try:
            await write.coro
        except Exception:
            logger.exception(
                "%s: Error while setting characteristic %s",
                client_addr,
                write.char.display_name,
            )
            return
        write.char.client_update_finished(
            write.previous_value, client_addr, notify=write.notify
        )

    def _set_characteristics_for_controller(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        timeout: float,
        *,
        client_uuid: UUID | None = None,
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]:
        """Apply a controller's write, waiting for coroutine setters.

        Returns an awaitable of the response if a characteristic setter is a
        coroutine function. Accessory and service setters run before those
        coroutines start. A setter still running after ``timeout`` seconds is
        cancelled and its characteristic answers -70408. Cancelling the
        awaitable, e.g. because the connection closed, cancels the setters.
        """
        results, pending = self._apply_characteristic_writes(
            chars_query, client_addr, client_uuid
        )
        if pending:
            return self._async_finish_set_characteristics(
                results, pending, client_addr, timeout
            )
        return _set_characteristics_response(results)

    def _apply_characteristic_writes(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        client_uuid: UUID | None,
    ) -> tuple[dict[int, dict[int, dict[str, Any]]], list[_PendingWrite]]:
        """Run the setters of a write and collect the per-characteristic results."""
        pending: list[_PendingWrite] = []
        try:
            return self._run_characteristic_writes(
                chars_query, client_addr, client_uuid, pending
            )
        except BaseException:
            # A malformed later entry must not leave a setter coroutine
            # un-awaited.
            for write in pending:
                write.coro.close()
            raise

    def _run_characteristic_writes(
        self,
        chars_query: dict[str, Any],
        client_addr: tuple[str, int],
        client_uuid: UUID | None,
        pending: list[_PendingWrite],
    ) -> tuple[dict[int, dict[int, dict[str, Any]]], list[_PendingWrite]]:
        queries: list[CharacteristicWrite] = chars_query[HAP_REPR_CHARS]

        timed_write = HAP_REPR_PID in chars_query
        if timed_write and not self._take_prepared_write(
            chars_query[HAP_REPR_PID], client_addr
        ):
            # The spec requires a timed write that was never prepared, was
            # already used or has expired to be ignored as a whole.
            ignored: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
            for query in queries:
                ignored[query[HAP_REPR_AID]][query[HAP_REPR_IID]] = {
                    HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
                }
            return ignored, []

        not_notifiable = self._notify(queries, client_addr)

        updates_by_accessories_services: dict[
            Accessory, dict[Service, dict[Characteristic, Any]]
        ] = defaultdict(lambda: defaultdict(dict))
        results: dict[int, dict[int, dict[str, Any]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        char_to_iid: dict[Characteristic, int] = {}

        primary_accessory = self.accessory
        assert primary_accessory is not None  # noqa: S101
        primary_aid = primary_accessory.aid

        for aid, iid in not_notifiable:
            results[aid][iid] = {
                HAP_REPR_STATUS: HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED
            }

        is_admin = self.state.is_admin(client_uuid)
        for query in queries:
            aid = query[HAP_REPR_AID]
            iid = query[HAP_REPR_IID]
            if (aid, iid) in not_notifiable:
                continue
            if HAP_REPR_VALUE not in query:
                # Listed with status 0 if the response has other results.
                results[aid][iid] = {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS}
                continue
            value = query.get(HAP_REPR_VALUE)

            if aid == primary_aid:
                acc = primary_accessory
            else:
                # Accessory IIDs other than the primary one only exist on
                # bridged accessories.
                bridge = self.accessory
                if not isinstance(bridge, Bridge):
                    continue
                acc = bridge.accessories.get(aid)
            if acc is None:
                continue
            char = acc.get_characteristic(aid, iid)
            if char is None:
                continue

            # Privileges are checked before the permissions, so a controller
            # that may not write the characteristic learns nothing else.
            # Also for an explicit null, so no setter sees the characteristic.
            if not is_admin and ADMIN_ONLY_WRITE in char.properties.get(
                PROP_ADMIN_ONLY_ACCESS, ()
            ):
                results[aid][iid] = {
                    HAP_REPR_STATUS: HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES
                }
                continue

            if (
                value is not None
                and HAP_PERMISSION_WRITE not in char.properties[PROP_PERMISSIONS]
            ):
                results[aid][iid] = {
                    HAP_REPR_STATUS: HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC
                }
                continue

            if (
                value is not None
                and not timed_write
                and HAP_PERMISSION_TIMED_WRITE in char.properties[PROP_PERMISSIONS]
            ):
                if self.enforce_timed_writes:
                    results[aid][iid] = {
                        HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
                    }
                    continue
                logger.debug(
                    "%s: %s requires a timed write but was written without one",
                    client_addr,
                    char.display_name,
                )

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
                self._write_responses.get(client_addr, {}).pop(char, None)
            private_write = (
                (write_response or char.read_back_response)
                and value is not None
                and HAP_PERMISSION_READ in char.properties[PROP_PERMISSIONS]
            )
            previous_value = char.value

            set_result = HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
            set_result_value = None

            if value is not None:
                set_result, set_result_value = _wrap_char_setter(
                    char, value, client_addr, notify=not private_write
                )
            # A write with ``r`` to a readable characteristic is a request,
            # e.g. a Setup Endpoints offer with the writer's SRTP keys. Only
            # the writer reads the answer back, see _store_write_response.
            # client_update_value stored the request as the value, so put
            # back the former one unless the setter set a value of its own.
            # Whatever the setter of a read back characteristic leaves in the
            # value is an answer for the writer only, e.g. the Setup Endpoints
            # answer of a subclass that calls super() and returns nothing.
            # Neither the former value of a refused write nor the request the
            # setter left alone is an answer.
            if private_write:
                own_value = char.value
                if char.read_back_response:
                    if set_result_value is None and own_value not in (
                        previous_value,
                        _stored_value(char, value),
                    ):
                        set_result_value = own_value
                    char.value = previous_value
                elif own_value == _stored_value(char, value):
                    char.value = previous_value

            result: dict[str, Any] = {HAP_REPR_STATUS: set_result}
            if inspect.iscoroutine(set_result_value):
                pending.append(
                    _PendingWrite(
                        result,
                        set_result_value,
                        write_response,
                        char,
                        previous_value,
                        not private_write,
                    )
                )
            elif set_result_value is not None:
                self._keep_response(
                    result, char, set_result_value, write_response, client_addr
                )

            results[aid][iid] = result
            char_to_iid[char] = iid
            service = char.service
            assert service is not None  # noqa: S101
            updates_by_accessories_services[acc][service][char] = value

        # Proccess accessory and service level setter callbacks
        for acc, updates_by_service in updates_by_accessories_services.items():
            aid = acc.aid
            assert aid is not None  # noqa: S101
            aid_results = results[aid]

            # Accessory level setter callbacks
            acc_set_result = None
            if acc.setter_callback:
                acc_set_result = _wrap_acc_setter(acc, updates_by_service, client_addr)

            # Service level setter callbacks
            for service, chars in updates_by_service.items():
                char_set_result = None
                if service.setter_callback:
                    char_set_result = _wrap_service_setter(service, chars, client_addr)
                set_result = char_set_result or acc_set_result

                if not set_result:
                    continue

                for char in chars:
                    aid_results[char_to_iid[char]][HAP_REPR_STATUS] = set_result

        return results, pending

    async def _async_finish_set_characteristics(
        self,
        results: dict[int, dict[int, dict[str, Any]]],
        pending: list[_PendingWrite],
        client_addr: tuple[str, int],
        timeout: float,
    ) -> dict[str, Any] | None:
        """Wait for the coroutine setters, then build the response."""
        tasks = {asyncio.ensure_future(write.coro): write for write in pending}
        try:
            done, not_done = await asyncio.wait(tasks, timeout=timeout)
        except asyncio.CancelledError:
            for task, write in tasks.items():
                if task.done():
                    self._finish_done_write(task, write, client_addr)
                    continue
                task.cancel()
                task.add_done_callback(_retrieve_late_result)
            raise
        for task in not_done:
            task.cancel()
            task.add_done_callback(_retrieve_late_result)
            write = tasks[task]
            logger.warning(
                "%s: Timed out setting characteristic %s",
                client_addr,
                write.char.display_name,
            )
            write.result[HAP_REPR_STATUS] = HAP_SERVER_STATUS.OPERATION_TIMED_OUT
        for task in done:
            write = tasks[task]
            try:
                outcome = task.result()
            except (Exception, asyncio.CancelledError):
                logger.exception(
                    "%s: Error while setting characteristic %s",
                    client_addr,
                    write.char.display_name,
                )
                write.result[HAP_REPR_STATUS] = (
                    HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
                )
                continue
            write.char.client_update_finished(
                write.previous_value, client_addr, notify=write.notify
            )
            if outcome is not None:
                # A response queued when the connection closes is dropped by
                # the deferred cleanup in connection_lost.
                self._keep_response(
                    write.result, write.char, outcome, write.write_response, client_addr
                )
        return _set_characteristics_response(results)

    @staticmethod
    def _finish_done_write(
        task: asyncio.Future[Any], write: _PendingWrite, client_addr: tuple[str, int]
    ) -> None:
        """Tell subscribers about a setter done before the write was cancelled."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            logger.error(
                "%s: Error while setting characteristic %s",
                client_addr,
                write.char.display_name,
                exc_info=err,
            )
            return
        write.char.client_update_finished(
            write.previous_value, client_addr, notify=write.notify
        )

    def _keep_response(
        self,
        result: dict[str, Any],
        char: Characteristic,
        response: HAPValue,
        write_response: bool,
        client_addr: tuple[str, int] | None,
    ) -> None:
        """Keep what a setter returned as a write response or read back value."""
        if write_response:
            result[HAP_REPR_VALUE] = self._store_write_response(
                char, response, client_addr
            )
        elif (
            char.read_back_response
            and HAP_PERMISSION_READ in char.properties[PROP_PERMISSIONS]
        ):
            self._store_write_response(char, response, client_addr)

    def _take_prepared_write(self, pid: Any, client_addr: tuple[str, int]) -> bool:
        """Consume the prepared write ``pid`` and return whether it is still valid."""
        if not _is_hap_int(pid):
            # Controller input. A list or dict is not even hashable.
            return False
        expires_at = self.prepared_writes.get(client_addr, {}).pop(pid, None)
        return expires_at is not None and time.monotonic() <= expires_at

    def _store_write_response(
        self,
        char: Characteristic,
        response: HAPValue,
        client_addr: tuple[str, int] | None,
    ) -> HAPValue:
        """Keep the write response and return what to send.

        The written value of a write response characteristic, such as an SDP
        offer, is a request. What the accessory answered is its state. If the
        characteristic is readable, only the connection that wrote reads the
        response back, until it closes, and it wins over the getter and a later
        ``set_value``. Other connections read the characteristic's own value,
        which the write leaves as it was. A write without ``r`` stays readable
        for everyone and notifies ``ev`` subscribers as any write does, unless
        the characteristic has ``read_back_response``, which keeps the response
        the same way without a write response.
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
            if client_addr is not None:
                self._write_responses.setdefault(client_addr, {})[char] = valid
        else:
            char.value = valid
        return valid

    def prepare(
        self, prepare_query: dict[str, Any], client_addr: tuple[str, int]
    ) -> dict[str, int]:
        """Called from ``HAPServerHandler`` when iOS wants to prepare a write.

        :param prepare_query: A prepare query. For example:

        .. code-block:: python

           {
              "ttl": 10000, # in milliseconds
              "pid": 12345678,
           }

        :type prepare_query: dict
        """
        if not isinstance(prepare_query, dict):
            return {HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST}
        ttl = prepare_query.get(HAP_REPR_TTL)
        pid = prepare_query.get(HAP_REPR_PID)
        if not (_is_hap_int(ttl) and _is_hap_int(pid)):
            return {HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST}
        try:
            now = time.monotonic()
            pending = self.prepared_writes.setdefault(client_addr, {})
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
                del pending[min(pending, key=pending.get)]
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
        self, queries: list[CharacteristicWrite], client_addr: tuple[str, int]
    ) -> set[tuple[int, int]]:
        """Notify the driver that the client has subscribed or unsubscribed.

        Characteristics without the notify permission are left alone; their
        ``(aid, iid)`` pairs are returned so the request can report
        -70406 for them.
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
            action = "Subscribed" if ev else "Unsubscribed"
            logger.debug("%s client %s to topic %s", action, client_addr, char_topic)
            self.async_subscribe_client_topic(client_addr, char_topic, ev)
        return not_notifiable
