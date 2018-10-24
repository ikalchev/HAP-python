"""Starts a fake fan, lightbulb, garage door and a TemperatureSensor
"""
import logging
import signal
import random
import time

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (CATEGORY_FAN,
                         CATEGORY_LIGHTBULB,
                         CATEGORY_GARAGE_DOOR_OPENER,
                         CATEGORY_WINDOW_COVERING,
                         CATEGORY_SENSOR)

'''
# ### CATEGORY values ###
# Category is a hint to iOS clients about what "type" of Accessory this
# represents, for UI only.
CATEGORY_OTHER = 1
CATEGORY_BRIDGE = 2
CATEGORY_FAN = 3
CATEGORY_GARAGE_DOOR_OPENER = 4
CATEGORY_LIGHTBULB = 5
CATEGORY_DOOR_LOCK = 6
CATEGORY_OUTLET = 7
CATEGORY_SWITCH = 8
CATEGORY_THERMOSTAT = 9
CATEGORY_SENSOR = 10
CATEGORY_ALARM_SYSTEM = 11
CATEGORY_DOOR = 12
CATEGORY_WINDOW = 13
CATEGORY_WINDOW_COVERING = 14
CATEGORY_PROGRAMMABLE_SWITCH = 15
CATEGORY_RANGE_EXTENDER = 16
CATEGORY_CAMERA = 17
CATEGORY_VIDEO_DOOR_BELL = 18
CATEGORY_AIR_PURIFIER = 19
CATEGORY_HEATER = 20
CATEGORY_AIR_CONDITIONER = 21
CATEGORY_HUMIDIFIER = 22
CATEGORY_DEHUMIDIFIER = 23
CATEGORY_SPEAKER = 26
CATEGORY_SPRINKLER = 28
CATEGORY_FAUCET = 29
CATEGORY_SHOWER_HEAD = 30
'''

logging.basicConfig(level=logging.INFO, format="[%(module)s] %(message)s")

class TemperatureSensor(Accessory):
    """Fake Temperature sensor, measuring every 3 seconds."""

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_temp = self.add_preload_service('TemperatureSensor')
        self.char_temp = serv_temp.configure_char('CurrentTemperature')

    @Accessory.run_at_interval(3)
    async def run(self):
        self.char_temp.set_value(random.randint(18, 26))


class FakeFan(Accessory):
    """Fake Fan, only logs whatever the client set."""

    category = CATEGORY_FAN

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Add the fan service. Also add optional characteristics to it.
        serv_fan = self.add_preload_service(
            'Fan', chars=['RotationSpeed', 'RotationDirection'])

        self.char_rotation_speed = serv_fan.configure_char(
            'RotationSpeed', setter_callback=self.set_rotation_speed)
        self.char_rotation_direction = serv_fan.configure_char(
            'RotationDirection', setter_callback=self.set_rotation_direction)

    def set_rotation_speed(self, value):
        logging.debug("Rotation speed changed: %s", value)

    def set_rotation_direction(self, value):
        logging.debug("Rotation direction changed: %s", value)

class LightBulb(Accessory):
    """Fake lightbulb, logs what the client sets."""

    category = CATEGORY_LIGHTBULB

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_light = self.add_preload_service('Lightbulb')
        self.char_on = serv_light.configure_char(
            'On', setter_callback=self.set_bulb)

    def set_bulb(self, value):
        logging.info("Bulb value: %s", value)

class GarageDoor(Accessory):
    """Fake garage door."""

    category = CATEGORY_GARAGE_DOOR_OPENER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_preload_service('GarageDoorOpener')\
            .configure_char(
                'TargetDoorState', setter_callback=self.change_state)

    def change_state(self, value):
        logging.info("GDC value: %s", value)
        self.get_service('GarageDoorOpener')\
            .get_characteristic('CurrentDoorState')\
            .set_value(value)


class WindowCovering(Accessory):
    """Fake WindowCovering."""

    category = CATEGORY_WINDOW_COVERING

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        """Quick assign basic accessory information."""
        self.set_info_service(firmware_revision=2,  manufacturer="Brand",
                model="Shutter", serial_number="0123456789")
        
        # Add the WindowCovering service. Also add optional characteristics to it.
        serv_cover = self.add_preload_service(
            'WindowCovering', chars=['CurrentPosition', 'TargetPosition', 'PositionState', 'ObstructionDetected', 'HoldPosition'])

        self.char_target_pos = serv_cover.configure_char(
            'TargetPosition', setter_callback=self.set_target_position)
            
        self.char_state = serv_cover.configure_char(
            'PositionState', setter_callback=self.set_position_state)
            
        self.char_cur_pos = serv_cover.configure_char(
            'CurrentPosition', setter_callback=self.set_current_position)

    def set_target_position(self, value):
        logging.info("WindowCovering TargetPosition value: %s", value)
        self.get_service('WindowCovering')\
            .get_characteristic('TargetPosition')\
            .set_value(value)

        time.sleep(5)   # Delays for 5 seconds closing the shutter.

        logging.info("WindowCovering CurrentPosition value: %s", value)
        self.get_service('WindowCovering')\
            .get_characteristic('CurrentPosition')\
            .set_value(value)

    # The value property of PositionState must be one of the following:
    # Characteristic.PositionState.DECREASING = 0;
    # Characteristic.PositionState.INCREASING = 1;
    # Characteristic.PositionState.STOPPED = 2; 
    def set_position_state(self, value):
        logging.info("WindowCovering PositionState value: %s", value)
        self.get_service('PositionState')\
            .get_characteristic('PositionState')\
            .set_value(value)

    def set_current_position(self, value):
        logging.info("WindowCovering CurrentPosition value: %s", value)
        self.get_service('WindowCovering')\
            .get_characteristic('CurrentPosition')\
            .set_value(value)
    
def get_bridge(driver):
    bridge = Bridge(driver, 'Bridge')
    bridge.set_info_service(firmware_revision=1,  manufacturer="Brand",
                model="model", serial_number="0123456789")
    #bridge.add_accessory(LightBulb(driver, 'Lightbulb'))
    #bridge.add_accessory(FakeFan(driver, 'Big Fan'))
    #bridge.add_accessory(GarageDoor(driver, 'Garage'))
    bridge.add_accessory(WindowCovering(driver, 'Shutter'))
    #bridge.add_accessory(TemperatureSensor(driver, 'Sensor'))
    
    return bridge

driver = AccessoryDriver(port=51800, persist_file='shutter.state')
driver.add_accessory(accessory=get_bridge(driver))
signal.signal(signal.SIGTERM, driver.signal_handler)
driver.start()
