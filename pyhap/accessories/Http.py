"""This module provides HttpAccessory - an accessory that
allows remote devices to provide HAP services by sending POST
requests.
"""
import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyhap.accessory import Bridge
from pyhap.const import CATEGORY_OTHER

logger = logging.getLogger(__name__)


class HttpBridgeHandler(BaseHTTPRequestHandler):
    """Handles requests and passes value updates to an HttpAccessory.

    The POST request should contain json data with the format:
    {   "aid": <aid>
        "services": {
            <service>: {
                <characteristic>: value,
            }
        }
    }

    Example:
    {   "aid": 2
        "services": {
            TemperatureSensor" : {
                "CurrentTemperature": 20
            }
        }
    }
    """

    def __init__(self, http_accessory, sock, client_addr, server):
        """Create a handler that passes updates to the given HttpAccessory.
        """
        self.http_accessory = http_accessory
        super().__init__(sock, client_addr, server)

    def respond_ok(self):
        """Reply with code 200 (OK) and close the connection.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", 0)
        self.end_headers()
        self.close_connection = 1

    def respond_err(self):
        """Reply with code 400 and close the connection.
        """
        self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", 0)
        self.end_headers()
        self.close_connection = 1

    def do_POST(self):
        """Read the payload as json and update the state of the accessory.
        """
        length = int(self.headers["Content-Length"])
        try:
            # The below decode is necessary only for python <3.6, because loads prior 3.6
            # doesn't know bytes/bytearray.
            content = self.rfile.read(length).decode('utf-8')
            data = json.loads(content)
        except Exception as e:
            logger.error("Bad POST request; Error was: %s", str(e))
            self.respond_err()
        else:
            self.http_accessory.update_state(data)
            self.respond_ok()


class HttpBridge(Bridge):
    """An accessory that listens to HTTP requests containing characteristic updates.

    Simple devices/implementations can just HTTP POST data as:

    {
        <aid>: int,
        "services": {
            <service1>: {
                <characteristic1>: value
                ...
            }
            ...
        }
    }

    Then this accessory takes care of communicating this update to any HAP clients.

    The way you configure a HttpBridge is by adding Accessory objects. You can specify
    the Accessory AIDs, which will be needed when making POST requests. In the
    example below, we add three accessories to a HTTP Bridge:
    >>> # get loaders
    >>> service_loader = loader.get_serv_loader()
    >>> char_loader = loader.get_char_loader()
    >>>
    >>> # Create an accessory with the temperature sensor service.
    >>> temperature_acc_1 = Accessory("temp1")
    >>> temperature_acc_1.add_service(service_loader.get("TemperatureSensor"))
    >>>
    >>> # Create an accessory with the temperature sensor service.
    >>> # Also, add an optional characteristic Name to the service.
    >>> temperature_acc_2 = Accessory("temp2")
    >>> temp_service = service_loader.get("TemperatureSensor")
    >>> temp_service.add_characteristic(char_loader.get("StatusLowBattery"))
    >>> temperature_acc_2.add_service(temp_service)
    >>>
    >>> # Create a lightbulb accessory.
    >>> light_bulb_acc = Accessory("bulb")
    >>> light_bulb_acc.add_service(service_loader.get("Lightbulb"))
    >>>
    >>> # Finally, create the HTTP Bridge and add all accessories to it.
    >>> http_bridge = HttpBridge("HTTP Bridge", address=("", 51111))
    >>> for accessory in (temperature_acc_1, temperature_acc_2, light_bulb_acc):
    ...     http_bridge.add_accessory(accessory)
    >>>
    >>> # add to an accessory driver and start as usual

    After the above you can HTTP POST updates to the local address at port 51111.
    """

    category = CATEGORY_OTHER

    def __init__(self, address, *args, **kwargs):
        """Initialise and add the given services.

        @param address: The address-port on which to listen for requests.
        @type address: tuple(str, int)

        @param accessories:
        """
        super().__init__(*args, **kwargs)

        # For exclusive access to updates. Slight overkill...
        self.update_lock = None
        self.server_thread = None
        self._set_server(address)

    def _set_server(self, address):
        """Set up a HTTPServer to listen on the given address.
        """
        self.server = HTTPServer(address, lambda *a: HttpBridgeHandler(self, *a))
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.update_lock = threading.Lock()

    def __getstate__(self):
        """Return the state of this instance, less the server and server thread.

        Also add the server address. All this is because we cannot pickle such
        objects and to allow to recover the server using the address.
        """
        state = super().__getstate__()
        state['server'] = None
        state['server_thread'] = None
        state['update_lock'] = None
        state['address'] = self.server.server_address
        return state

    def __setstate__(self, state):
        """Load the state  and set up the server with the address in the state.
        """
        self.__dict__.update(state)
        self._set_server(state['address'])

    def update_state(self, data):
        """Update the characteristics from the received data.

        Expected to be called from HapHttpHandler. Updates are thread-safe.

        @param data: A dict of values that should be set, e.g.:
            {
                <aid>: int,
                <service1 name> : {
                    <characteristic1 name>: value
                    ...
                }
                ...
            }
        @type data: dict
        """
        aid = data['aid']
        logger.debug("Got update from accessory with aid: %d", aid)
        accessory = self.accessories[aid]
        service_data = data['services']
        for service, char_data in service_data.items():
            service_obj = accessory.get_service(service)
            for char, value in char_data.items():
                char_obj = service_obj.get_characteristic(char)
                with self.update_lock:
                    char_obj.set_value(value)

    def stop(self):
        """Stop the server.
        """
        super().stop()
        logger.debug("Stopping HTTP bridge server.")
        self.server.shutdown()
        self.server.server_close()

    def run(self):
        """Start the server - can listen for requests.
        """
        logger.debug("Starting HTTP bridge server.")
        self.server_thread.start()
