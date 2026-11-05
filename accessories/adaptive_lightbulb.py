"""This virtual lightbulb implements the bare minimum needed for HomeKit
    controller to recognize it as having AdaptiveLightning
"""
import logging
import signal
import random
import tlv8
import base64

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (CATEGORY_LIGHTBULB,
                         HAP_REPR_IID)
from pyhap.loader import get_loader

# Define tlv8 Keys and Values
SUPPORTED_TRANSITION_CONFIGURATION = 1
CHARACTERISTIC_IID = 1
TRANSITION_TYPE = 2

BRIGHTNESS = 1
COLOR_TEMPERATURE = 2

logging.basicConfig(level=logging.DEBUG, format="[%(module)s] %(message)s")

def bytes_to_base64_string(value: bytes) -> str:
   return base64.b64encode(value).decode('ASCII')

class LightBulb(Accessory):
    """Fake lightbulb, logs what the client sets."""

    category = CATEGORY_LIGHTBULB

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_light = self.add_preload_service('Lightbulb', [
            # The names here refer to the Characteristic name defined
            # in characteristic.json
            "Brightness",
            "ColorTemperature",
            "ActiveTransitionCount",
            "TransitionControl",
            "SupportedTransitionConfiguration"])

        self.char_on = serv_light.configure_char(
            'On', setter_callback=self.set_on)
        self.char_br = serv_light.configure_char(
            'Brightness', setter_callback=self.set_brightness)
        self.char_ct = serv_light.configure_char(
            'ColorTemperature', setter_callback=self.set_ct, value=140)

        # Via this structure we advertise to the controller that we are
        # capable of autonomous transitions between states on brightness
        # and color temperature.
        supported_transitions = [tlv8.Entry(SUPPORTED_TRANSITION_CONFIGURATION, [
                    tlv8.Entry(CHARACTERISTIC_IID, self.char_br.to_HAP()[HAP_REPR_IID]),
                    tlv8.Entry(TRANSITION_TYPE, BRIGHTNESS),
                    tlv8.Entry(CHARACTERISTIC_IID, self.char_ct.to_HAP()[HAP_REPR_IID]),
                    tlv8.Entry(TRANSITION_TYPE, COLOR_TEMPERATURE)
                ])]

        bytes_data = tlv8.encode(supported_transitions)
        b64str = bytes_to_base64_string(bytes_data)

        self.char_atc = serv_light.configure_char(
            'ActiveTransitionCount', setter_callback=self.set_atc)
        self.char_tc = serv_light.configure_char(
            'TransitionControl', setter_callback=self.set_tc)
        self.char_stc = serv_light.configure_char(
            'SupportedTransitionConfiguration',
            value=b64str)

    def set_on(self, value):
        logging.info("Write On State: %s", value)

    def set_ct(self, value):
        logging.info("Bulb color temp: %s", value)

    def set_atc(self, value):
        logging.info("Write to ActiveTransactionCount: %s", value)

    def set_tc(self, value):
        logging.info("Write to TransitionControl: %s", value)

    def set_brightness(self, value):
        logging.info("Bulb brightness: %s", value)

driver = AccessoryDriver(port=51826, persist_file='adaptive_lightbulb.state')
driver.add_accessory(accessory=LightBulb(driver, 'Lightbulb'))
signal.signal(signal.SIGTERM, driver.signal_handler)
driver.start()

