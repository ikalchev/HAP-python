"""This module provides HttpAccessory - an accessory that
allows remote devices to provide HAP services by sending POST
requests.
"""
import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiohttp import web

from pyhap.accessory import Bridge, Category

logger = logging.getLogger(__name__)


class HttpBridgeHandler(web.Application):
    """Handles requests and passes value updates to an HttpAccessory.

    The POST request should contain json data with the format:
    {   "aid": <aid>,
        "services": {
            <service>: {
                <characteristic>: <value>
            }
        }
    }

    Example:
    {   "aid": 2,
        "services": {
            "TemperatureSensor" : {
                "CurrentTemperature": 20
            }
        }
    }
    """

    def __init__(self, http_accessory):
        """Create a handler that passes updates to the given HttpAccessory.
        """
        super().__init__()

        self.http_accessory = http_accessory
        self.add_routes([web.post('/', self.post_handler)])

    async def post_handler(self, request):
        try:
            data = await request.json()
            await self.http_accessory.update_state(data)
        except Exception as e:
            logger.error("Bad POST request; Error was: %s", str(e))
            return web.Response(text="Bad POST", status=400)

        return web.Response(text="OK")


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

    category = Category.OTHER

    def __init__(self, *args, address, **kwargs):
        """Initialise and add the given services.

        @param address: The address-port on which to listen for requests.
        @type address: tuple(str, int)

        @param accessories:
        """
        super().__init__(*args, **kwargs)

        self.address = address

    async def update_state(self, data):
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
        aid = data["aid"]
        logger.debug("Got update from accessory with aid: %d", aid)
        accessory = self.accessories[aid]
        service_data = data["services"]
        for service, char_data in service_data.items():
            service_obj = accessory.get_service(service)
            for char, value in char_data.items():
                char_obj = service_obj.get_characteristic(char)
                char_obj.set_value(value)

    async def run(self, stop_event, loop=None):
        """Start the server - can listen for requests.
        """
        logger.debug("Starting HTTP bridge server.")
        app = HttpBridgeHandler(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.address[0], self.address[1])
        await site.start()

        await stop_event.wait()
        await runner.cleanup()
