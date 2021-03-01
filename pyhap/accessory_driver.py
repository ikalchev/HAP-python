"""AccessoryDriver - glues together the HAP Server, accessories and mDNS advertising.

Sending updates to clients

The process of sending value changes to clients happens in two parts - on one hand, the
value change is indicated by a Characteristic and, on the other, that change is sent to a
client. To begin, typically, something in the Accessory's run method will do
set_value(foo, notify=True) on one of its contained Characteristic. This in turn will
create a HAP representation of the change and publish it to the Accessory. This will
then add some more information and eventually the value change will reach the
AccessoryDriver (all this happens through the publish() interface). The AccessoryDriver
will then check if there is a client that subscribed for events from this exact
Characteristic from this exact Accessory (remember, it could be a Bridge with more than
one Accessory in it). If so, a task is created to send the event to the subscribers. This
terminates the call chain and concludes the publishing process from the Characteristic,
the Characteristic does not block waiting for the actual send to happen.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import functools
import hashlib
import tempfile
import json
import logging
import os
import socket
import sys
import threading
import re

from zeroconf import ServiceInfo, Zeroconf

from pyhap import util
from pyhap.accessory import get_topic
from pyhap.characteristic import CharacteristicError
from pyhap.const import (
    MAX_CONFIG_VERSION,
    HAP_PERMISSION_NOTIFY,
    HAP_REPR_ACCS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
    STANDALONE_AID,
)
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_server import HAPServer
from pyhap.hsrp import Server as SrpServer
from pyhap.loader import Loader
from pyhap.params import get_srp_context
from pyhap.state import State
from .util import callback

logger = logging.getLogger(__name__)

CHAR_STAT_OK = 0
SERVICE_COMMUNICATION_FAILURE = -70402
SERVICE_CALLBACK = "callback"
SERVICE_CHARS = "chars"
SERVICE_IIDS = "iids"
HAP_SERVICE_TYPE = "_hap._tcp.local."
VALID_MDNS_REGEX = re.compile(r"[^A-Za-z0-9\-]+")


def is_callback(func):
    """Check if function is callback."""
    return "_pyhap_callback" in getattr(func, "__dict__", {})


def iscoro(func):
    """Check if the function is a coroutine or if the function is a ``functools.partial``,
    check the wrapped function for the same.
    """
    if isinstance(func, functools.partial):
        func = func.func
    return asyncio.iscoroutinefunction(func)


class AccessoryMDNSServiceInfo(ServiceInfo):
    """A mDNS service info representation of an accessory."""

    def __init__(self, accessory, state):
        self.accessory = accessory
        self.state = state

        adv_data = self._get_advert_data()
        # Append part of MAC address to prevent name conflicts
        name = "{} {}.{}".format(
            self._valid_name(),
            self.state.mac[-8:].replace(":", ""),
            HAP_SERVICE_TYPE,
        )
        super().__init__(
            HAP_SERVICE_TYPE,
            name=name,
            port=self.state.port,
            weight=0,
            priority=0,
            properties=adv_data,
            addresses=[socket.inet_aton(self.state.address)],
        )

    def _valid_name(self):
        return re.sub(VALID_MDNS_REGEX, " ", self.accessory.display_name).strip()

    def _setup_hash(self):
        setup_hash_material = self.state.setup_id + self.state.mac
        temp_hash = hashlib.sha512()
        temp_hash.update(setup_hash_material.encode())
        return base64.b64encode(temp_hash.digest()[:4]).decode()

    def _get_advert_data(self):
        """Generate advertisement data from the accessory."""
        return {
            "md": self._valid_name(),
            "pv": "1.0",
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
    """
    An AccessoryDriver mediates between incoming requests from the HAPServer and
    the Accessory.

    The driver starts and stops the HAPServer, the mDNS advertisements and responds
    to events from the HAPServer.
    """

    def __init__(
        self,
        *,
        address=None,
        port=51234,
        persist_file="accessory.state",
        pincode=None,
        encoder=None,
        loader=None,
        loop=None,
        mac=None,
        listen_address=None,
        advertised_address=None,
        interface_choice=None,
        zeroconf_instance=None
    ):
        """
        Initialize a new AccessoryDriver object.

        :param pincode: The pincode that HAP clients must prove they know in order
            to pair with this `Accessory`. Defaults to None, in which case a random
            pincode is generated. The pincode has the format "xxx-xx-xxx", where x is
            a digit.
        :type pincode: bytearray

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

        :param advertised_address: The address of the HAPServer announced via mDNS.
            This can be used to announce an external address from behind a NAT.
            If not given, the value of the address parameter will be used.
        :type advertised_address: str

        :param interface_choice: The zeroconf interfaces to listen on.
        :type InterfacesType: [InterfaceChoice.Default, InterfaceChoice.All]

        :param zeroconf_instance: A Zeroconf instance. When running multiple accessories or
            bridges a single zeroconf instance can be shared to avoid the overhead
            of processing the same data multiple times.
        """
        if loop is None:
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()

            executor_opts = {"max_workers": None}
            if sys.version_info >= (3, 6):
                executor_opts["thread_name_prefix"] = "SyncWorker"

            self.executor = ThreadPoolExecutor(**executor_opts)
            loop.set_default_executor(self.executor)
            self.tid = threading.current_thread()
        else:
            self.tid = threading.main_thread()
            self.executor = None

        self.loop = loop

        self.accessory = None
        if zeroconf_instance is not None:
            self.advertiser = zeroconf_instance
        elif interface_choice is not None:
            self.advertiser = Zeroconf(interfaces=interface_choice)
        else:
            self.advertiser = Zeroconf()
        self.persist_file = os.path.expanduser(persist_file)
        self.encoder = encoder or AccessoryEncoder()
        self.topics = {}  # topic: set of (address, port) of subscribed clients
        self.loader = loader or Loader()
        self.aio_stop_event = asyncio.Event(loop=loop)
        self.stop_event = threading.Event()

        self.safe_mode = False

        self.mdns_service_info = None
        self.srp_verifier = None

        address = address or util.get_local_address()
        advertised_address = advertised_address or address
        self.state = State(
            address=advertised_address, mac=mac, pincode=pincode, port=port
        )

        listen_address = listen_address or address
        network_tuple = (listen_address, self.state.port)
        self.http_server = HAPServer(network_tuple, self)

    def start(self):
        """Start the event loop and call `start_service`.

        Pyhap will be stopped gracefully on a KeyBoardInterrupt.
        """
        try:
            logger.info("Starting the event loop")
            if threading.current_thread() is threading.main_thread():
                logger.debug("Setting child watcher")
                watcher = asyncio.SafeChildWatcher()
                watcher.attach_loop(self.loop)
                asyncio.set_child_watcher(watcher)
            else:
                logger.debug(
                    "Not setting a child watcher. Set one if "
                    "subprocesses will be started outside the main thread."
                )
            self.add_job(self.start_service)
            self.loop.run_forever()
        except KeyboardInterrupt:
            logger.debug("Got a KeyboardInterrupt, stopping driver")
            self.loop.call_soon_threadsafe(self.loop.create_task, self.async_stop())
            self.loop.run_forever()
        finally:
            self.loop.close()
            logger.info("Closed the event loop")

    def start_service(self):
        """Starts the accessory.

        - Call the accessory's run method.
        - Start handling accessory events.
        - Start the HAP server.
        - Publish a mDNS advertisement.
        - Print the setup QR code if the accessory is not paired.

        All of the above are started in separate threads. Accessory thread is set as
        daemon.
        """
        if self.accessory is None:
            raise ValueError(
                "You must assign an accessory to the driver, "
                "before you can start it."
            )
        logger.info(
            "Starting accessory %s on address %s, port %s.",
            self.accessory.display_name,
            self.state.address,
            self.state.port,
        )

        # Start listening for requests
        logger.debug("Starting server.")
        self.add_job(self.http_server.async_start, self.loop)

        # Advertise the accessory as a mDNS service.
        logger.debug("Starting mDNS.")
        self.mdns_service_info = AccessoryMDNSServiceInfo(self.accessory, self.state)
        self.advertiser.register_service(self.mdns_service_info)

        # Print accessory setup message
        if not self.state.paired:
            self.accessory.setup_message()

        # Start the accessory so it can do stuff.
        logger.debug("Starting accessory %s", self.accessory.display_name)
        self.add_job(self.accessory.run)
        logger.debug(
            "AccessoryDriver for %s started successfully", self.accessory.display_name
        )

    def stop(self):
        """Method to stop pyhap."""
        self.loop.call_soon_threadsafe(self.loop.create_task, self.async_stop())

    async def async_stop(self):
        """Stops the AccessoryDriver and shutdown all remaining tasks."""
        await self.async_add_job(self._do_stop)
        logger.debug("Stopping HAP server and event sending")

        self.aio_stop_event.set()

        self.http_server.async_stop()

        logger.info(
            "Stopping accessory %s on address %s, port %s.",
            self.accessory.display_name,
            self.state.address,
            self.state.port,
        )

        await self.async_add_job(self.accessory.stop)

        logger.debug(
            "AccessoryDriver for %s stopped successfully", self.accessory.display_name
        )

        # Executor=None means a loop wasn't passed in
        if self.executor is not None:
            logger.debug("Shutdown executors")
            self.executor.shutdown()
            self.loop.stop()

        logger.debug("Stop completed")

    def _do_stop(self):
        """Stop the mDNS and set the stop event."""
        logger.debug("Setting stop events, stopping accessory")
        self.stop_event.set()

        logger.debug("Stopping mDNS advertising for %s", self.accessory.display_name)
        self.advertiser.unregister_service(self.mdns_service_info)
        self.advertiser.close()

    def add_job(self, target, *args):
        """Add job to executor pool."""
        if target is None:
            raise ValueError("Don't call add_job with None.")
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    @util.callback
    def async_add_job(self, target, *args):
        """Add job from within the event loop."""
        task = None

        if asyncio.iscoroutine(target):
            task = self.loop.create_task(target)
        elif is_callback(target):
            self.loop.call_soon(target, *args)
        elif iscoro(target):
            task = self.loop.create_task(target(*args))
        else:
            task = self.loop.run_in_executor(None, target, *args)

        return task

    def add_accessory(self, accessory):
        """Add top level accessory to driver."""
        self.accessory = accessory
        if accessory.aid is None:
            accessory.aid = STANDALONE_AID
        elif accessory.aid != STANDALONE_AID:
            raise ValueError("Top-level accessory must have the AID == 1.")
        if os.path.exists(self.persist_file):
            logger.info("Loading Accessory state from `%s`", self.persist_file)
            self.load()
        else:
            logger.info("Storing Accessory state in `%s`", self.persist_file)
            self.persist()

    @util.callback
    def async_subscribe_client_topic(self, client, topic, subscribe=True):
        """(Un)Subscribe the given client from the given topic.

        This method must be run in the event loop.

        :param client: A client (address, port) tuple that should be subscribed.
        :type client: tuple <str, int>

        :param topic: The topic to which to subscribe.
        :type topic: str

        :param subscribe: Whether to subscribe or unsubscribe the client. Both subscribing
            an already subscribed client and unsubscribing a client that is not subscribed
            do nothing.
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

    def publish(self, data, sender_client_addr=None):
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

        data = {HAP_REPR_CHARS: [data]}
        bytedata = json.dumps(data).encode()

        if threading.current_thread() == self.tid:
            self.async_send_event(topic, bytedata, sender_client_addr)
            return

        self.loop.call_soon_threadsafe(
            self.async_send_event, topic, bytedata, sender_client_addr
        )

    def async_send_event(self, topic, bytedata, sender_client_addr):
        """Send an event to a client.

        Must be called in the event loop
        """
        if self.aio_stop_event.is_set():
            return

        subscribed_clients = self.topics.get(topic, [])
        logger.debug(
            "Send event: topic(%s), data(%s), sender_client_addr(%s)",
            topic,
            bytedata,
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
            logger.debug("Sending event to client: %s", client_addr)
            pushed = self.http_server.push_event(bytedata, client_addr)
            if not pushed:
                logger.debug(
                    "Could not send event to %s, probably stale socket.", client_addr
                )
                unsubs.append(client_addr)
                # Maybe consider removing the client_addr from every topic?

        for client_addr in unsubs:
            self.async_subscribe_client_topic(client_addr, topic, False)

    def config_changed(self):
        """Notify the driver that the accessory's configuration has changed.

        Persists the accessory, so that the new configuration is available on
        restart. Also, updates the mDNS advertisement, so that iOS clients know they need
        to fetch new data.
        """
        self.state.config_version += 1
        if self.state.config_version > MAX_CONFIG_VERSION:
            self.state.config_version = 1
        self.persist()
        self.update_advertisement()

    def update_advertisement(self):
        """Updates the mDNS service info for the accessory."""
        logger.debug("Updating mDNS advertisement")
        self.mdns_service_info = AccessoryMDNSServiceInfo(self.accessory, self.state)
        self.advertiser.update_service(self.mdns_service_info)

    @callback
    def async_persist(self):
        """Saves the state of the accessory.

        Must be run in the event loop.
        """
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(loop.run_in_executor(None, self.persist))

    def persist(self):
        """Saves the state of the accessory.

        Must run in executor.
        """
        tmp_filename = None
        try:
            temp_dir = os.path.dirname(self.persist_file)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=temp_dir, delete=False
            ) as file_handle:
                tmp_filename = file_handle.name
                self.encoder.persist(file_handle, self.state)
            os.replace(tmp_filename, self.persist_file)
        finally:
            if tmp_filename and os.path.exists(tmp_filename):
                os.remove(tmp_filename)

    def load(self):
        """Load the persist file.

        Must run in executor.
        """
        with open(self.persist_file, "r") as file_handle:
            self.encoder.load_into(file_handle, self.state)

    @callback
    def pair(self, client_uuid, client_public):
        """Called when a client has paired with the accessory.

        Persist the new accessory state.

        :param client_uuid: The client uuid.
        :type client_uuid: uuid.UUID

        :param client_public: The client's public key.
        :type client_public: bytes

        :return: Whether the pairing is successful.
        :rtype: bool
        """
        logger.info("Paired with %s.", client_uuid)
        self.state.add_paired_client(client_uuid, client_public)
        self.async_persist()
        return True

    @callback
    def unpair(self, client_uuid):
        """Removes the paired client from the accessory.

        Persist the new accessory state.

        :param client_uuid: The client uuid.
        :type client_uuid: uuid.UUID
        """
        logger.info("Unpairing client %s.", client_uuid)
        self.state.remove_paired_client(client_uuid)
        self.async_persist()

    def finish_pair(self):
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

    def setup_srp_verifier(self):
        """Create an SRP verifier for the accessory's info."""
        # TODO: Move the below hard-coded values somewhere nice.
        ctx = get_srp_context(3072, hashlib.sha512, 16)
        verifier = SrpServer(ctx, b"Pair-Setup", self.state.pincode)
        self.srp_verifier = verifier

    def get_accessories(self):
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
        hap_rep = self.accessory.to_HAP()
        if not isinstance(hap_rep, list):
            hap_rep = [
                hap_rep,
            ]
        logger.debug("Get accessories response: %s", hap_rep)
        return {HAP_REPR_ACCS: hap_rep}

    def get_characteristics(self, char_ids):
        """Returns values for the required characteristics.

        :param char_ids: A list of characteristic "paths", e.g. "1.2" is aid 1, iid 2.
        :type char_ids: list<str>

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
        chars = []
        for aid_iid in char_ids:
            aid, iid = (int(i) for i in aid_iid.split("."))
            rep = {
                HAP_REPR_AID: aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
            }

            try:
                if aid == STANDALONE_AID:
                    char = self.accessory.iid_manager.get_obj(iid)
                    available = True
                else:
                    acc = self.accessory.accessories.get(aid)
                    if acc is None:
                        continue
                    available = acc.available
                    char = acc.iid_manager.get_obj(iid)

                if available:
                    rep[HAP_REPR_VALUE] = char.get_value()
                    rep[HAP_REPR_STATUS] = CHAR_STAT_OK
            except CharacteristicError:
                logger.error("Error getting value for characteristic %s.", id)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Unexpected error getting value for characteristic %s.", id
                )

            chars.append(rep)
        logger.debug("Get chars response: %s", chars)
        return {HAP_REPR_CHARS: chars}

    def set_characteristics(self, chars_query, client_addr):
        """Called from ``HAPServerHandler`` when iOS configures the characteristics.

        :param chars_query: A configuration query. For example:

        .. code-block:: python

           {
              "characteristics": [{
                 "aid": 1,
                 "iid": 2,
                 "value": False, # Value to set
                 "ev": True # (Un)subscribe for events from this characteristics.
              }]
           }

        :type chars_query: dict
        """
        # TODO: Add support for chars that do no support notifications.
        accessory_callbacks = {}
        setter_results = {}
        had_error = False

        for cq in chars_query[HAP_REPR_CHARS]:
            aid, iid = cq[HAP_REPR_AID], cq[HAP_REPR_IID]
            setter_results.setdefault(aid, {})
            char = self.accessory.get_characteristic(aid, iid)

            if HAP_PERMISSION_NOTIFY in cq:
                char_topic = get_topic(aid, iid)
                logger.debug(
                    "Subscribed client %s to topic %s", client_addr, char_topic
                )
                self.async_subscribe_client_topic(
                    client_addr, char_topic, cq[HAP_PERMISSION_NOTIFY]
                )

            if HAP_REPR_VALUE not in cq:
                continue

            value = cq[HAP_REPR_VALUE]

            try:
                char.client_update_value(value, client_addr)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "%s: Error while setting characteristic %s to %s",
                    client_addr,
                    char.display_name,
                    value,
                )
                setter_results[aid][iid] = SERVICE_COMMUNICATION_FAILURE
                had_error = True
            else:
                setter_results[aid][iid] = CHAR_STAT_OK

            # For some services we want to send all the char value
            # changes at once.  This resolves an issue where we send
            # ON and then BRIGHTNESS and the light would go to 100%
            # and then dim to the brightness because each callback
            # would only send one char at a time.
            if not char.service or not char.service.setter_callback:
                continue

            services = accessory_callbacks.setdefault(aid, {})

            if char.service.display_name not in services:
                services[char.service.display_name] = {
                    SERVICE_CALLBACK: char.service.setter_callback,
                    SERVICE_CHARS: {},
                    SERVICE_IIDS: [],
                }

            service_data = services[char.service.display_name]
            service_data[SERVICE_CHARS][char.display_name] = value
            service_data[SERVICE_IIDS].append(iid)

        for aid, services in accessory_callbacks.items():
            for service_name, service_data in services.items():
                try:
                    service_data[SERVICE_CALLBACK](service_data[SERVICE_CHARS])
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "%s: Error while setting characteristics to %s for the %s service",
                        service_data[SERVICE_CHARS],
                        client_addr,
                        service_name,
                    )
                    set_result = SERVICE_COMMUNICATION_FAILURE
                    had_error = True
                else:
                    set_result = CHAR_STAT_OK

                for iid in service_data[SERVICE_IIDS]:
                    setter_results[aid][iid] = set_result

        if not had_error:
            return None

        return {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: aid,
                    HAP_REPR_IID: iid,
                    HAP_REPR_STATUS: status,
                }
                for aid, iid_status in setter_results.items()
                for iid, status in iid_status.items()
            ]
        }

    def signal_handler(self, _signal, _frame):
        """Stops the AccessoryDriver for a given signal.

        An AccessoryDriver can be registered as a signal handler with this method. For
        example, you can register it for a KeyboardInterrupt as follows:
        >>> import signal
        >>> signal.signal(signal.SIGINT, anAccDriver.signal_handler)

        Now, when the user hits Ctrl+C, the driver will stop its accessory, the HAP server
        and everything else that needs stopping and will exit gracefully.
        """
        try:
            self.stop()
        except Exception as e:
            logger.error("Could not stop AccessoryDriver because of error: %s", e)
            raise
