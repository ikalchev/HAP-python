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
one Accessory in it). If so, the event is put in a FIFO queue - the event queue. This
terminates the call chain and concludes the publishing process from the Characteristic,
the Characteristic does not block waiting for the actual send to happen.

When the AccessoryDriver is started, it spawns an event dispatch thread. The purpose of
this thread is to get events from the event queue and send them to subscribed clients.
Whenever a send fails, the client is unsubscripted, as it is assumed that the client left
or went to sleep before telling us. This concludes the publishing process from the
AccessoryDriver.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import os
import logging
import socket
import hashlib
import base64
import sys
import time
import threading
import json
import queue

from zeroconf import ServiceInfo, Zeroconf

from pyhap.accessory import get_topic
from pyhap.characteristic import CharacteristicError
from pyhap.const import (
    STANDALONE_AID, HAP_PERMISSION_NOTIFY, HAP_REPR_ACCS, HAP_REPR_AID,
    HAP_REPR_CHARS, HAP_REPR_IID, HAP_REPR_STATUS, HAP_REPR_VALUE)
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_server import HAPServer
from pyhap.hsrp import Server as SrpServer
from pyhap.loader import Loader
from pyhap.params import get_srp_context
from pyhap.state import State
from pyhap import util

logger = logging.getLogger(__name__)

CHAR_STAT_OK = 0
SERVICE_COMMUNICATION_FAILURE = -70402
SERVICE_CALLBACK = 0
SERVICE_CALLBACK_DATA = 1


def callback(func):
    """Decorator for non blocking functions."""
    setattr(func, '_pyhap_callback', True)
    return func


def is_callback(func):
    """Check if function is callback."""
    return '_pyhap_callback' in getattr(func, '__dict__', {})


def iscoro(func):
    """Check if the function is a coroutine or if the function is a ``functools.patial``,
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
        super().__init__(
            '_hap._tcp.local.',
            self.accessory.display_name + '._hap._tcp.local.',
            socket.inet_aton(self.state.address), self.state.port,
            0, 0, adv_data)

    def _setup_hash(self):
        setup_hash_material = self.state.setup_id + self.state.mac
        temp_hash = hashlib.sha512()
        temp_hash.update(setup_hash_material.encode())
        return base64.b64encode(temp_hash.digest()[:4])

    def _get_advert_data(self):
        """Generate advertisement data from the accessory."""
        return {
            'md': self.accessory.display_name,
            'pv': '1.0',
            'id': self.state.mac,
            # represents the 'configuration version' of an Accessory.
            # Increasing this 'version number' signals iOS devices to
            # re-fetch accessories data.
            'c#': str(self.state.config_version),
            's#': '1',  # 'accessory state'
            'ff': '0',
            'ci': str(self.accessory.category),
            # 'sf == 1' means "discoverable by HomeKit iOS clients"
            'sf': '0' if self.state.paired else '1',
            'sh': self._setup_hash()
        }


class AccessoryDriver:
    """
    An AccessoryDriver mediates between incoming requests from the HAPServer and
    the Accessory.

    The driver starts and stops the HAPServer, the mDNS advertisements and responds
    to events from the HAPServer.
    """

    NUM_EVENTS_BEFORE_STATS = 100
    """Number of HAP send events to be processed before reporting statistics on
    the event queue length."""

    def __init__(self, *, address=None, port=51234,
                 persist_file='accessory.state', pincode=None,
                 encoder=None, loader=None, loop=None, mac=None,
                 listen_address=None, advertised_address=None):
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
        """
        if sys.platform == 'win32':
            self.loop = loop or asyncio.ProactorEventLoop()
        else:
            self.loop = loop or asyncio.new_event_loop()

        executor_opts = {'max_workers': None}
        if sys.version_info >= (3, 6):
            executor_opts['thread_name_prefix'] = 'SyncWorker'

        self.executor = ThreadPoolExecutor(**executor_opts)
        self.loop.set_default_executor(self.executor)

        self.accessory = None
        self.http_server_thread = None
        self.advertiser = Zeroconf()
        self.persist_file = os.path.expanduser(persist_file)
        self.encoder = encoder or AccessoryEncoder()
        self.topics = {}  # topic: set of (address, port) of subscribed clients
        self.topic_lock = threading.Lock()  # for exclusive access to the topics
        self.loader = loader or Loader()
        self.aio_stop_event = asyncio.Event(loop=self.loop)
        self.stop_event = threading.Event()
        self.event_queue = queue.Queue()  # (topic, bytes)
        self.send_event_thread = None  # the event dispatch thread
        self.sent_events = 0
        self.accumulated_qsize = 0

        self.safe_mode = False

        self.mdns_service_info = None
        self.srp_verifier = None

        address = address or util.get_local_address()
        advertised_address = advertised_address or address
        self.state = State(address=advertised_address, mac=mac, pincode=pincode, port=port)

        listen_address = listen_address or address
        network_tuple = (listen_address, self.state.port)
        self.http_server = HAPServer(network_tuple, self)

    def start(self):
        """Start the event loop and call `_do_start`.

        Pyhap will be stopped gracefully on a KeyBoardInterrupt.
        """
        try:
            logger.info('Starting the event loop')
            if threading.current_thread() is threading.main_thread():
                logger.debug('Setting child watcher')
                watcher = asyncio.SafeChildWatcher()
                watcher.attach_loop(self.loop)
                asyncio.set_child_watcher(watcher)
            else:
                logger.debug('Not setting a child watcher. Set one if '
                             'subprocesses will be started outside the main thread.')
            self.add_job(self._do_start)
            self.loop.run_forever()
        except KeyboardInterrupt:
            logger.debug('Got a KeyboardInterrupt, stopping driver')
            self.loop.call_soon_threadsafe(
                self.loop.create_task, self.async_stop())
            self.loop.run_forever()
        finally:
            self.loop.close()
            logger.info('Closed the event loop')

    def _do_start(self):
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
            raise ValueError("You must assign an accessory to the driver, "
                             "before you can start it.")
        logger.info('Starting accessory %s on address %s, port %s.',
                    self.accessory.display_name, self.state.address,
                    self.state.port)

        # Start sending events to clients. This is done in a daemon thread, because:
        # - if the queue is blocked waiting on an empty queue, then there is nothing left
        #   for clean up.
        # - if the queue is currently sending an event to the client, then, when it has
        #   finished, it will check the run sentinel, see that it is set and break the
        #   loop. Alternatively, the server's server_close method will shutdown and close
        #   the socket, while sending is in progress, which will result abort the sending.
        logger.debug('Starting event thread.')
        self.send_event_thread = threading.Thread(daemon=True, target=self.send_events)
        self.send_event_thread.start()

        # Start listening for requests
        logger.debug('Starting server.')
        self.http_server_thread = threading.Thread(target=self.http_server.serve_forever)
        self.http_server_thread.start()

        # Advertise the accessory as a mDNS service.
        logger.debug('Starting mDNS.')
        self.mdns_service_info = AccessoryMDNSServiceInfo(
            self.accessory, self.state)
        self.advertiser.register_service(self.mdns_service_info)

        # Print accessory setup message
        if not self.state.paired:
            self.accessory.setup_message()

        # Start the accessory so it can do stuff.
        logger.debug('Starting accessory.')
        self.add_job(self.accessory.run)
        logger.debug('AccessoryDriver started successfully')

    def stop(self):
        """Method to stop pyhap."""
        self.loop.call_soon_threadsafe(
            self.loop.create_task, self.async_stop())

    async def async_stop(self):
        """Stops the AccessoryDriver and shutdown all remaining tasks."""
        await self.async_add_job(self._do_stop)
        logger.debug('Shutdown executors')
        self.executor.shutdown()
        self.loop.stop()
        logger.debug('Stop completed')

    def _do_stop(self):
        """Stop the accessory.

        1. Set the run sentinel.
        2. Call the stop method of the Accessory and wait for its thread to finish.
        3. Stop mDNS advertising.
        4. Stop HAP server.
        """
        # TODO: This should happen in a different order - mDNS, server, accessory. Need
        # to ensure that sending with a closed server will not crash the app.
        logger.info("Stopping accessory %s on address %s, port %s.",
                    self.accessory.display_name, self.state.address,
                    self.state.port)
        logger.debug("Setting stop events, stopping accessory and event sending")
        self.stop_event.set()
        self.loop.call_soon_threadsafe(self.aio_stop_event.set)
        self.add_job(self.accessory.stop)

        logger.debug("Stopping mDNS advertising")
        self.advertiser.unregister_service(self.mdns_service_info)
        self.advertiser.close()

        logger.debug("Stopping HAP server")
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_server_thread.join()

        logger.debug("AccessoryDriver stopped successfully")

    def add_job(self, target, *args):
        """Add job to executor pool."""
        if target is None:
            raise ValueError("Don't call add_job with None.")
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    @callback
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

    @callback
    def async_run_job(self, target, *args):
        """Run job from within the event loop.

        In contract to `async_add_job`, `callbacks` get called immediately.
        """
        if not asyncio.iscoroutine(target) and is_callback(target):
            target(*args)
        else:
            self.async_add_job(target, *args)

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

    def subscribe_client_topic(self, client, topic, subscribe=True):
        """(Un)Subscribe the given client from the given topic, thread-safe.

        :param client: A client (address, port) tuple that should be subscribed.
        :type client: tuple <str, int>

        :param topic: The topic to which to subscribe.
        :type topic: str

        :param subscribe: Whether to subscribe or unsubscribe the client. Both subscribing
            an already subscribed client and unsubscribing a client that is not subscribed
            do nothing.
        :type subscribe: bool
        """
        with self.topic_lock:
            if subscribe:
                subscribed_clients = self.topics.get(topic)
                if subscribed_clients is None:
                    subscribed_clients = set()
                    self.topics[topic] = subscribed_clients
                subscribed_clients.add(client)
            else:
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
        self.event_queue.put((topic, bytedata, sender_client_addr))

    def send_events(self):
        """Start sending events from the queue to clients.

        This continues until self.run_sentinel is set. The method logs the average
        queue size for the past NUM_EVENTS_BEFORE_STATS. Enable debug logging to see this
        information.

        Whenever sending an event fails (i.e. HAPServer.push_event returns False), the
        intended client is removed from the set of subscribed clients for the topic.

        @note: This method blocks on Queue.get, waiting for something to come. Thus, if
        this is not run in a daemon thread or it is run on the main thread, the app will
        hang.
        """
        while not self.loop.is_closed():
            # Maybe consider having a pool of worker threads, each performing a send in
            # order to increase throughput.
            #
            # Clients that made the characteristic change are NOT susposed to get events
            # about the characteristic change as it can cause an HTTP disconnect and violates
            # the HAP spec
            #
            topic, bytedata, sender_client_addr = self.event_queue.get()
            subscribed_clients = self.topics.get(topic, [])
            logger.debug('Send event: topic(%s), data(%s), sender_client_addr(%s)', topic, bytedata, sender_client_addr)
            for client_addr in subscribed_clients.copy():
                if sender_client_addr and sender_client_addr == client_addr:
                    logger.debug('Skip sending event to client since its the client that made the characteristic change: %s', client_addr)
                    continue
                logger.debug('Sending event to client: %s', client_addr)
                pushed = self.http_server.push_event(bytedata, client_addr)
                if not pushed:
                    logger.debug('Could not send event to %s, probably stale socket.',
                                 client_addr)
                    # Maybe consider removing the client_addr from every topic?
                    self.subscribe_client_topic(client_addr, topic, False)
            self.event_queue.task_done()
            self.sent_events += 1
            self.accumulated_qsize += self.event_queue.qsize()

            if self.sent_events > self.NUM_EVENTS_BEFORE_STATS:
                logger.debug('Average queue size for the past %s events: %.2f',
                             self.sent_events, self.accumulated_qsize / self.sent_events)
                self.sent_events = 0
                self.accumulated_qsize = 0

    def config_changed(self):
        """Notify the driver that the accessory's configuration has changed.

        Persists the accessory, so that the new configuration is available on
        restart. Also, updates the mDNS advertisement, so that iOS clients know they need
        to fetch new data.
        """
        self.state.config_version += 1
        self.persist()
        self.update_advertisement()

    def update_advertisement(self):
        """Updates the mDNS service info for the accessory."""
        self.advertiser.unregister_service(self.mdns_service_info)
        self.mdns_service_info = AccessoryMDNSServiceInfo(
            self.accessory, self.state)
        time.sleep(0.1)  # Doing it right away can cause crashes.
        self.advertiser.register_service(self.mdns_service_info)

    def persist(self):
        """Saves the state of the accessory."""
        with open(self.persist_file, 'w') as fp:
            self.encoder.persist(fp, self.state)

    def load(self):
        """ """
        with open(self.persist_file, 'r') as fp:
            self.encoder.load_into(fp, self.state)

    def pair(self, client_uuid, client_public):
        """Called when a client has paired with the accessory.

        Updates the accessory with the paired client and updates the mDNS service. Also,
        persist the new state.

        :param client_uuid: The client uuid.
        :type client_uuid: uuid.UUID

        :param client_public: The client's public key.
        :type client_public: bytes

        :return: Whether the pairing is successful.
        :rtype: bool
        """
        # TODO: Adding a client is a change in the acc. configuration. Then, should we
        # let the accessory call config_changed, which will persist and update mDNS?
        # See also unpair.
        logger.info("Paired with %s.", client_uuid)
        self.state.add_paired_client(client_uuid, client_public)
        self.persist()
        # Safe mode added to avoid error during pairing, see
        # https://github.com/home-assistant/home-assistant/issues/14567
        if not self.safe_mode:
            self.update_advertisement()
        return True

    def unpair(self, client_uuid):
        """Removes the paired client from the accessory.

        Updates the accessory and updates the mDNS service. Persist the new accessory
        state.

        :param client_uuid: The client uuid.
        :type client_uuid: uuid.UUID
        """
        logger.info("Unpairing client %s.", client_uuid)
        self.state.remove_paired_client(client_uuid)
        self.persist()
        if not self.safe_mode:
            self.update_advertisement()

    def setup_srp_verifier(self):
        """Create an SRP verifier for the accessory's info."""
        # TODO: Move the below hard-coded values somewhere nice.
        ctx = get_srp_context(3072, hashlib.sha512, 16)
        verifier = SrpServer(ctx, b'Pair-Setup', self.state.pincode)
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
            hap_rep = [hap_rep, ]
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
        for id in char_ids:
            aid, iid = (int(i) for i in id.split('.'))
            rep = {HAP_REPR_AID: aid, HAP_REPR_IID: iid}
            char = self.accessory.get_characteristic(aid, iid)
            try:
                rep[HAP_REPR_VALUE] = char.get_value()
                rep[HAP_REPR_STATUS] = CHAR_STAT_OK
            except CharacteristicError:
                logger.error("Error getting value for characteristic %s.", id)
                rep[HAP_REPR_STATUS] = SERVICE_COMMUNICATION_FAILURE

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
        service_callbacks = {}
        for cq in chars_query[HAP_REPR_CHARS]:
            aid, iid = cq[HAP_REPR_AID], cq[HAP_REPR_IID]
            char = self.accessory.get_characteristic(aid, iid)

            if HAP_PERMISSION_NOTIFY in cq:
                char_topic = get_topic(aid, iid)
                logger.debug('Subscribed client %s to topic %s',
                             client_addr, char_topic)
                self.subscribe_client_topic(
                    client_addr, char_topic, cq[HAP_PERMISSION_NOTIFY])

            if HAP_REPR_VALUE in cq:
                # TODO: status needs to be based on success of set_value
                char.client_update_value(cq[HAP_REPR_VALUE], client_addr)
                # For some services we want to send all the char value
                # changes at once.  This resolves an issue where we send
                # ON and then BRIGHTNESS and the light would go to 100%
                # and then dim to the brightness because each callback
                # would only send one char at a time.
                service = char.service

                if service and service.setter_callback:
                    service_callbacks.setdefault(
                        service.display_name,
                        [service.setter_callback, {}]
                    )
                    service_callbacks[service.display_name][
                        SERVICE_CALLBACK_DATA
                    ][char.display_name] = cq[HAP_REPR_VALUE]

        for service_name in service_callbacks:
            service_callbacks[service_name][SERVICE_CALLBACK](
                service_callbacks[service_name][SERVICE_CALLBACK_DATA]
            )

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
