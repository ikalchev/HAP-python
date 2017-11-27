"""An accessory that listens to HTTP requests containing characteristic updates.

A more simple device/implementation can just HTTP POST data as:
{   <service_name>: {
        <characteristic_name>: value
    }
}
Then this accessory takes care of communicating this update to any HAP clients.
"""
import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader

logger = logging.getLogger(__name__)


class HapHttpHandler(BaseHTTPRequestHandler):
    """
    Handles POST requests and passes characteristic value updates to an HttpAccessory.

    The POST request should contain json data with the format:
    {   <service_name>: {
            <characteristic_name>: value,
        }
    }

    Example:
    {   "TemperatureSensor" : {
            "CurrentTemperature": 20
        }
    }
    """

    def __init__(self, httpAccessory, sock, client_addr, server):
        """
        Creates a handler that passes updates to the given HttpAccessory.
        """
        self.httpAccessory = httpAccessory
        super(HapHttpHandler, self).__init__(sock, client_addr, server)

    def respond_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", 0)
        self.end_headers()
        self.close_connection = 1

    def respond_err(self):
        self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", 0)
        self.end_headers()
        self.close_connection = 1

    def do_POST(self):
        """
        Read the payload as json and update the state of the httpAccessory.
        """
        length = int(self.headers["Content-Length"])
        try:
            # The below decode is necessary only for python <3.6, because loads prior 3.6
            # doesn't know bytes/bytearray.
            content = self.rfile.read(length).decode("utf-8")
            data = json.loads(content)
        except Exception as e:
            logger.error("Bad POST request; Error was: %s", str(e))
            self.respond_err()
        else:
            self.httpAccessory.update_state(data)
            self.respond_ok()


'''TODO: should make it possible to init with {"aid" : [services]}
or {"addr": [services]} etc., so that this accessory can bridge several
other. In this way remote devices can use a simple interface and this module will do
the hap magic.
'''


class Http(Accessory):

    category = Category.OTHER

    def __init__(self, address, hapServices, *args, **kwargs):
        self.hapServices = hapServices
        super(Http, self).__init__(*args, **kwargs)
        self._set_server(address)

    def _set_server(self, address):
        self.server = HTTPServer(address, lambda *a: HapHttpHandler(self, *a))
        self.serverThread = threading.Thread(target=self.server.serve_forever)

    def _set_services(self):
        super(Http, self)._set_services()
        ldr = loader.get_serv_loader()
        for s in self.hapServices:
            self.add_service(ldr.get(s))

    def __getstate__(self):
        state = super(Http, self).__getstate__()
        state["server"] = None
        state["serverThread"] = None
        state["address"] = self.server.server_address
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._set_server(state["address"])

    def update_state(self, data):
        for service, charData in data.items():
            serviceObj = self.get_service(service)
            for char, value in charData.items():
                charObj = serviceObj.get_characteristic(char)
                charObj.set_value(value)

    def stop(self):
        super(Http, self).stop()
        self.server.shutdown()
        self.server.server_close()

    def run(self):
        self.serverThread.start()
