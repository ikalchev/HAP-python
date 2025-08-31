"""Basic Hue integration
Allows control of incompatible Accessories like Osram Lightify Plugs (Osram Smart+)

I have no idea if this is a good implementation, but it works for me :D
"""
import logging
import signal
import certifi
import requests
import urllib3

from pyhap.accessory import Bridge, Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB

logging.basicConfig(level=logging.INFO)

http = urllib3.PoolManager(
    cert_reqs='CERT_REQUIRED',
    ca_certs=certifi.where())

# no auto discovery yet, see the getting started guide on how to get the secret
# https://www.developers.meethue.com/documentation/getting-started
hue_ip = ""
hue_secret = ""


class LightBulb(Accessory):

    category = CATEGORY_LIGHTBULB

    def __init__(self, *args, id, **kwargs):
        super().__init__(*args, **kwargs)

        serv_light = self.add_preload_service('Lightbulb')
        self.char_on = serv_light.configure_char(
            'On', setter_callback=self.set_bulb)

        self.id = id

    def __setstate__(self, state):
        self.__dict__.update(state)

    @Accessory.run_at_interval(10)
    def run(self):
        r = requests.get("http://{}/api/{}/lights/{}".format(hue_ip, hue_secret, self.id))
        r.raise_for_status()
        self.char_on.set_value(r.json()['state']['on'])
        logging.info("get status for light {}".format(self.id))

    def set_bulb(self, value):
        logging.info("setting state: {}".format(value))
        if value:
            r = requests.put("http://{}/api/{}/lights/{}/state".format(hue_ip, hue_secret, self.id), json={"on": True})
            r.raise_for_status()
        else:
            r = requests.put("http://{}/api/{}/lights/{}/state".format(hue_ip, hue_secret, self.id), json={"on": False})
            r.raise_for_status()
        logging.info(r.content)


def get_bridge():
    """Call this method to get a Bridge instead of a standalone accessory."""
    bridge = Bridge(display_name='Bridge')

    # discover hue lights
    r = requests.get("http://{}/api/{}/lights".format(hue_ip, hue_secret))
    r.raise_for_status()

    for key, value in r.json().items():
        id = key
        name = value["name"]
        logging.info("Found light: {} - {}".format(id, name))
        light = LightBulb(display_name=name, id=id)
        bridge.add_accessory(light)

    return bridge


acc = get_bridge()
# Start the accessory on port 51826
driver = AccessoryDriver(acc, port=51826)
# We want KeyboardInterrupts and SIGTERM (kill) to be handled by the driver itself,
# so that it can gracefully stop the accessory, server and advertising.
signal.signal(signal.SIGINT, driver.signal_handler)
signal.signal(signal.SIGTERM, driver.signal_handler)
# Start it!
driver.start()
