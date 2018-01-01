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
terminates the call chain and concludes the publishing process from the Characteristic.

When the AccessoryDriver is started, it spawns an event dispatch thread. The purpose of
this thread is to get events from the event queue and send them to subscribed clients.
Whenever a send fails, the client is unsubscribed, as it is assumed that the client left
or went to sleep before telling us. This concludes the publishing process from the
AccessoryDriver.
"""
import logging
import socket
import hashlib
import time
import threading
import json
import pickle
import queue

from zeroconf import ServiceInfo, Zeroconf

from pyhap.accessory import get_topic
from pyhap.characteristic import CharacteristicError
from pyhap.params import get_srp_context
from pyhap.hsrp import Server as SrpServer
from pyhap.hap_server import HAPServer
import pyhap.util as util

logger = logging.getLogger(__name__)


class AccessoryMDNSServiceInfo(ServiceInfo):
    """A mDNS service info representation of an accessory."""

    def __init__(self, accessory, address, port):
        self.accessory = accessory
        hname = socket.gethostname()
        pubname = hname + "." if hname.endswith(".local") else hname + ".local."

        adv_data = self._get_advert_data()
        super(AccessoryMDNSServiceInfo, self).__init__(
             "_hap._tcp.local.",
             self.accessory.display_name + "._hap._tcp.local.",
             socket.inet_aton(address),
             port,
             0,
             0,
             adv_data,
             pubname)

    def _get_advert_data(self):
        """Generate advertisment data from the accessory."""
        adv_data = {
            "md": self.accessory.display_name,
            "pv": "1.0",
            "id": self.accessory.mac,
            # represents the "configuration version" of an Accessory.
            # Increasing this "version number" signals iOS devices to
            # re-fetch accessories data.
            "c#": str(self.accessory.config_version),
            "s#": "1",  # "accessory state"
            "ff": "0",
            "ci": str(self.accessory.category),
            # "sf == 1" means "discoverable by HomeKit iOS clients"
            "sf": "0" if self.accessory.paired else "1"
        }

        return adv_data


class HAP_CONSTANTS:
    CHAR_STAT_OK = 0
    SERVICE_COMMUNICATION_FAILURE = -70402


class AccessoryDriver(object):
    """
    An AccessoryDriver mediates between incoming requests from the HAPServer and
    the Accessory.

    The driver starts and stops the HAPServer, the mDNS advertisements and responds
    to events from the HAPServer.
    """

    NUM_EVENTS_BEFORE_STATS = 100

    def __init__(self, accessory, port, address=None, persist_file="accessory.pickle"):
        """
        @param accessory: The Accessory to be managed by this driver.
        @type accessory: Accessory

        @param port: The local port on which the accessory will be accessible.
            In other words, this is the port of the HAPServer.
        @type port: int

        @param address: The local address on which the accessory will be accessible.
            In other words, this is the address of the HAPServer. If not given, the
            driver will try to select an address.
        @type address: str

        @param persist_file: The file name in which the state of the accessory
            will be persisted.
        @type persist_file: str
        """
        self.address = address or util.get_local_address()
        self.http_server = HAPServer((self.address, port), self)
        self.http_server_thread = None
        self.accessory = accessory
        self.advertiser = Zeroconf()
        self.port = port
        self.persist_file = persist_file
        self.topics = {}  # topic: set of (address, port) of subscribed clients
        self.topic_lock = threading.Lock()  # for exclusive access to the topics
        self.event_queue = queue.Queue()  # (topic, bytes)
        self.send_event_thread = None  # the event dispatch thread
        self.sent_events = 0
        self.accumulated_qsize = 0

        self.accessory.set_broker(self)
        self.mdns_service_info = None
        self.srp_verifier = None
        self.run_sentinel = None
        self.accessory_thread = None

    def subscribe_client_topic(self, client, topic, subscribe=True):
        """(Un)Subscribe the given client from the given topic, thread-safe.

        @param client: A client (address, port) tuple that should be subscribed.
        @type client: tuple <str, int>

        @param topic: The topic to which to subscribe.
        @type topic: str

        @param subscribe: Whether to subscribe or unsubscribe the client. Both subscribing
            an already subscribed client and unsubscribing a client that is not subscribed
            do nothing.
        @type subscribe: bool
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

    def publish(self, data):
        """Publishes an event to the client.

        The publishing occurs only if the current client is subscribed to the topic for
        the aid and iid contained in the data.

        @param data: The data to publish. It must at least contain the keys "aid" and
            "iid".
        @type data: dict
        """
        topic = get_topic(data["aid"], data["iid"])
        if topic not in self.topics:
            return

        data = {"characteristics": [data]}
        bytedata = json.dumps(data).encode()
        self.event_queue.put((topic, bytedata))

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
        while not self.run_sentinel.is_set():
            # Maybe consider having a pool of worker threads, each performing a send in
            # order to increase throughput.
            topic, bytedata = self.event_queue.get()
            subscribed_clients = self.topics.get(topic, [])
            for client_addr in subscribed_clients.copy():
                pushed = self.http_server.push_event(bytedata, client_addr)
                if not pushed:
                    logger.debug("Could not send event to %s, probably stale socket.",
                                 client_addr)
                    # Maybe consider removing the client_addr from every topic?
                    self.subscribe_client_topic(client_addr, topic, False)
            self.event_queue.task_done()
            self.sent_events += 1
            self.accumulated_qsize += self.event_queue.qsize()

            if self.sent_events > self.NUM_EVENTS_BEFORE_STATS:
                logger.debug("Average queue size for the past %s events: %.2f",
                             self.sent_events, self.accumulated_qsize / self.sent_events)
                self.sent_events = 0
                self.accumulated_qsize = 0

    def update_advertisment(self):
        """Updates the mDNS service info for the accessory."""
        self.advertiser.unregister_service(self.mdns_service_info)
        self.mdns_service_info = AccessoryMDNSServiceInfo(self.accessory,
                                                          self.address,
                                                          self.port)
        time.sleep(0.1)  # Doing it right away can cause crashes.
        self.advertiser.register_service(self.mdns_service_info)

    def persist(self):
        """Saves the state of the accessory."""
        with open(self.persist_file, "wb") as f:
            pickle.dump(self.accessory, f)

    def pair(self, client_uuid, client_public):
        """Called when a client has paired with the accessory.

        Updates the accessory with the paired client and updates the mDNS service. Also,
        persist the new state.

        @param client_uuid: The client uuid.
        @type client_uuid: uuid.UUID

        @param client_public: The client's public key.
        @type client_public: bytes

        @return: Whether the pairing is successful.
        @rtype: bool
        """
        logger.info("Paired with %s.", client_uuid)
        self.accessory.add_paired_client(client_uuid, client_public)
        self.persist()
        self.update_advertisment()
        return True

    def unpair(self, client_uuid):
        """Removes the paired client from the accessory.

        Updates the accessory and updates the mDNS service. Persist the new accessory
        state.

        @param client_uuid: The client uuid.
        @type client_uuid: uuid.UUID
        """
        logger.info("Unpairing client '%s'.", client_uuid)
        self.accessory.remove_paired_client(client_uuid)
        self.persist()
        self.update_advertisment()

    def setup_srp_verifier(self):
        """Create an SRP verifier for the accessory's info."""
        # TODO: Move the below hard-coded values somewhere nice.
        ctx = get_srp_context(3072, hashlib.sha512, 16)
        verifier = SrpServer(ctx, b"Pair-Setup", self.accessory.pincode)
        self.srp_verifier = verifier

    def get_accessories(self):
        """Returns the accessory in HAP format.

        @return: An example HAP representation is:
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
        @rtype: data
        """
        hap_rep = self.accessory.to_HAP()
        if not isinstance(hap_rep, list):
            hap_rep = [hap_rep, ]
        return {"accessories": hap_rep}

    def get_characteristics(self, char_ids):
        """Returns values for the required characteristics.

        @param char_ids: A list of characteristic "paths", e.g. "1.2" is aid 1, iid 2.
        @type char_ids: list<str>

        @return: Status success for each required characteristic. For example:
         {
            "characteristics: [{
               "aid": 1,
               "iid": 2,
               "status" 0
            }]
         }
        @rtype: dict
        """
        chars = []
        for id in char_ids:
            aid, iid = (int(i) for i in id.split("."))
            rep = {"aid": aid, "iid": iid}
            char = self.accessory.get_characteristic(aid, iid)
            try:
                rep["value"] = char.get_value()
                rep["status"] = HAP_CONSTANTS.CHAR_STAT_OK
            except CharacteristicError:
                logger.error("Error getting value for characteristic %s.", id)
                rep["status"] = HAP_CONSTANTS.SERVICE_COMMUNICATION_FAILURE

            chars.append(rep)
        return {"characteristics": chars}

    def set_characteristics(self, chars_query, client_addr):
        """Configures the given characteristics.

        @param chars_query: A configuration query. For example:
         {
            "characteristics": [{
               "aid": 1,
               "iid": 2,
               "value": False, # Value to set
               "ev": True # (Un)subscribe for events from this charactertics.
            }]
         }
        @type chars_query: dict

        @return: Response status for each characteristic. For example:
         {
            "characteristics": [{
               "aid": 1,
               "iid": 2,
               "status": 0,
            }]
         }
        @rtype: dict
        """
        chars_query = chars_query["characteristics"]
        chars_response = []
        for cq in chars_query:
            aid, iid = cq["aid"], cq["iid"]
            char = self.accessory.get_characteristic(aid, iid)

            if "ev" in cq:
                char_topic = get_topic(aid, iid)
                self.subscribe_client_topic(client_addr, char_topic, cq["ev"])

            response = {
                "aid": aid,
                "iid": iid,
                "status": HAP_CONSTANTS.CHAR_STAT_OK,
            }
            if "value" in cq:
                # TODO: status needs to be based on success of set_value
                char.set_value(cq["value"], should_notify=False)
                if "r" in cq:
                    response["value"] = char.value

            chars_response.append(response)
        return {"characteristics": chars_response}

    def start(self):
        """Starts the accessory.

        - Start the HAP server.
        - Publish a mDNS advertisment.
        - Call the accessory's run method.

        All of the above are started in separate threads. Accessory thread is set as
        daemon.
        """
        logger.info("Starting accessory '%s' on address '%s', port '%s'.",
                    self.accessory.display_name, self.address, self.port)

        # Start the accessory so it can do stuff.
        self.run_sentinel = threading.Event()
        self.accessory.set_sentinel(self.run_sentinel)
        self.accessory_thread = threading.Thread(target=self.accessory.run)
        self.accessory_thread.start()

        # Start sending events to clients. This is done in a daemon thread, because:
        # - if the queue is blocked waiting on an empty queue, then there is nothing left
        #   for clean up.
        # - if the queue is currently sending an event to the client, then, when it has
        #   finished, it will check the run sentinel, see that it is set and break the
        #   loop. Alternatively, the server's server_close method will shutdown and close
        #   the socket, while sending is in progress, which will result abort the sending.
        self.send_event_thread = threading.Thread(daemon=True, target=self.send_events)
        self.send_event_thread.start()

        # Start listening for requests
        self.http_server_thread = threading.Thread(target=self.http_server.serve_forever)
        self.http_server_thread.start()

        # Advertise the accessory as a mDNS service.
        self.mdns_service_info = AccessoryMDNSServiceInfo(self.accessory,
                                                          self.address,
                                                          self.port)
        self.advertiser.register_service(self.mdns_service_info)

    def stop(self):
        """Stop the accessory."""
        logger.info("Stoping accessory '%s' on address %s, port %s.",
                    self.accessory.display_name, self.address, self.port)
        logger.debug("Setting run sentinel, stopping accessory and event sending")
        self.run_sentinel.set()
        self.accessory.stop()
        self.accessory_thread.join()

        logger.debug("Stopping mDNS advertising")
        self.advertiser.unregister_service(self.mdns_service_info)
        self.advertiser.close()

        logger.debug("Stopping HAP server")
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_server_thread.join()
        logger.debug("AccessoryDriver stopped successfully")

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
