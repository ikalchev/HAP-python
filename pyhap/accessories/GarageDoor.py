import time
import logging

from pyhap.accessory import Accessory, Category
from pyhap import loader

OPEN = 0        # Valid for target and current states
CLOSED = 1      # Valid for target and current states
OPENING = 2     # Valid only for current state
CLOSING = 3     # Valid only for current state
STOPPED = 4     # Valid only for current state

logger = logging.getLogger(__file__)


class GarageDoor(Accessory):
    """
    A GarageDoorOpener Accessory.

    You will probably want to subclass this to get the behaviour that is
    specific to your garage door system. A couple of subclasses follow that
    show different ways to model things.
    """
    category = Category.GARAGE_DOOR_OPENER

    def __init__(self, *args, **kwargs):
        gpio_pins = kwargs.pop('gpio_pins', {})
        super().__init__(*args, **kwargs)

        garage_door = self.get_service("GarageDoorOpener")
        self.current_state = garage_door.get_characteristic("CurrentDoorState")
        self.target_state = garage_door.get_characteristic("TargetDoorState")
        self.target_state.setter_callback = self.set_target

        self.setup_gpio(**gpio_pins)

    def _set_services(self):
        super()._set_services()
        self.add_service(loader.get_serv_loader().get('GarageDoorOpener'))

    # Hooks: things that you will probably need to implement yourself.
    def set_target(self, value):
        """
        Defines the behaviour that should happen when a HomeKit trigger event
        is sent. This will have already set the target_state to the opposite to
        what it was before (ie, if the door was already open, or opening,
        then the target_state will now be closed). Keep in mind that target_state
        only has OPEN/CLOSED, so HomeKit is not able to model "stopped in the middle"
        very well.
        """
        pass

    def setup_gpio(self, **pins):
        """
        Override the things that need to happen when the system starts up, and
        pins need configuring.
        """
        pass

    def door_is_open(self):
        logger.info("Open")
        self.current_state.set_value(OPEN)
        self.target_state.value = OPEN
        self.target_state.notify()

    def door_is_opening(self):
        logger.info("Opening")
        self.current_state.set_value(OPENING)
        self.target_state.value = OPEN
        self.target_state.notify()
        # self.start_stopped_timer()

    def door_is_closed(self):
        logger.info("Closed")
        self.current_state.set_value(CLOSED)
        self.target_state.value = CLOSED
        self.target_state.notify()

    def door_is_closing(self):
        logger.info("Closing")
        self.current_state.set_value(CLOSING)
        self.target_state.value = CLOSED
        self.target_state.notify()
        # self.start_stopped_timer()


class TwoSwitchGarageDoor(GarageDoor):
    def get_accessory_information(self):
        """
        In reality, this should be a consumer subclass, it's just here to
        show how it can be done.
        """
        return {
            'Manufacturer': 'B & D',
            'Model': 'Controll-A-Door MPC2',
        }

    def run(self):
        """
        This uses gpiozero, which means we can use the really nice event
        handlers it provides. We then just need to detect what the current
        state is at startup, and set that in HomeKit.
        """
        self.top_limit.when_pressed = self.door_is_open
        self.top_limit.when_released = self.door_is_closing
        self.bottom_limit.when_pressed = self.door_is_closed
        self.bottom_limit.when_released = self.door_is_opening

        if self.top_limit.is_pressed:
            self.door_is_open()
        elif self.bottom_limit.is_pressed:
            self.door_is_closed()

    def setup_gpio(self, **pins):
        """
        """
        from gpiozero import Button, LED
        self.relay = LED(pins['relay'])  # 4
        self.top_limit = Button(pins['top_limit'])
        self.bottom_limit = Button(pins['bottom_limit'])

    def set_target(self, value):
        """
        This garage door system has a single button that triggers different
        behaviour depending upon the current state of the system. This mostly
        matches with the behaviour from HomeKit, with one exception: when the
        door is currently opening or closing, a single press halts the door,
        and then a second press reverses the direction.

        The HomeKit assumption is that a single press reverses the direction,
        so we need to stop, wait for a second, and then re-trigger.
        """
        if self.current_state.value in (OPENING, CLOSING):
            self.trigger_button()
            time.sleep(1)
        self.trigger_button()

    def trigger_button(self):
        logger.info('Button pressed')
        self.relay.on()
        time.sleep(1)
        self.relay.off()
        logger.info('Button released')
