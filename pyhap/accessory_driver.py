import logging
import socket
import hashlib
import time
import threading
import json
import pickle

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
        self.topics = set()

        self.accessory.set_broker(self)
        self.mdns_service_info = None
        self.srp_verifier = None
        self.run_sentinel = None
        self.accessory_thread = None

    def publish(self, data):
        """Publishes an event to the client.

        The publishing occurs only if the current client is subscribed to the topic for
        the aid and iid contained in the data.

        @param data: The data to publish. It must at least contain the keys "aid" and
            "iid".
        @type data: dict
        """
        if not get_topic(data["aid"], data["iid"]) in self.topics:
            return

        data = {
            "characteristics": [data]
        }
        bytedata = json.dumps(data).encode("utf-8")
        pushed = self.http_server.push_event(bytedata)
        if not pushed:
            logger.debug("Could not send event, probably stale socket.")
            self.topics.clear()

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
        state and clear the subscription topics.

        @param client_uuid: The client uuid.
        @type client_uuid: uuid.UUID
        """
        logger.info("Unpairing client '%s'.", client_uuid)
        self.accessory.remove_paired_client(client_uuid)
        self.persist()
        self.update_advertisment()
        self.topics.clear()

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

    def set_characteristics(self, chars_query):
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
                if cq["ev"]:
                    self.topics.add(char_topic)
                else:
                    self.topics.discard(char_topic)

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
        logger.debug("Setting run sentinel, stopping accessory")
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
