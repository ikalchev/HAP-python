import time
import threading
import pyhap.loader as loader
import RPi.GPIO as GPIO
from pyhap.accessory import Accessory, Category


class DoorSwitch(object):
    """Simple door switch for GPIO.
    This is NOT a HAP Door Switch!!
    """
    _last_callback = 0

    def __init__(self, pin, callback, bounce_time=1500, edge=GPIO.BOTH,
                 closed_state=GPIO.LOW, pull_up_down=GPIO.PUD_UP):
        """Initialize a Switch object.

        @param pin: GPIO (BCM) pin for the switch.
        @type pin: int

        @param callback: callback function on event detect
        @type callback: function

        @param bounce_time: amount of time (in milliseconds) to delay before another
        callback event is valid after a valid event is called.
        @type bounce_time: integer

        @param edge: GPIO edge to detect.
        @type edge: GPIO.RISING, GPIO.FALLING, or GPIO.BOTH

        @param closed_state: GPIO state when door is closed.
        @type closed_state: GPIO.LOW or GPIO.HIGH

        @param pull_up_down: pull up or down resistor to use
        @type pull_up_down: GPIO.PUD_UP or GPIO.PUD_DOWN
        """
        self.pin = pin
        self.closed_state = closed_state
        self.bounce_time = bounce_time
        self._threaded_callback = callback
        if GPIO.getmode() is None:
            GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=pull_up_down)
        GPIO.add_event_detect(self.pin, edge)
        GPIO.add_event_callback(self.pin, self._internal_callback)

    def _debounce(self):
        """Simple software debounce method.
        """
        current_time = time.time() * 1000
        if self._last_callback:
            if (current_time - self._last_callback) > self.bounce_time:
                self._last_callback = current_time
                return True
            else:
                return False
        self._last_callback = current_time
        return True

    def _internal_callback(self, channel):
        """Callback used for the GPIO library.
        Fires of another thread to work around
        the latent debounce bug in the GPIO library.
        You probably shouldn't override this.

        @param channel: Not used, pin provided on init is used.
        """
        if self._debounce():
            threading.Thread(target=self._threaded_callback).start()

    @property
    def state(self):
        """Integer returned state.

        @return: The Integer value of the switch's state.
        @rtype: int
        """
        if GPIO.input(self.pin) == self.closed_state:
            return 1
        else:
            return 0

    @property
    def is_closed(self):
        """Bool returned state.

        @return: The Boolean value of the switch's state.
        @rtype: bool
        """
        return self.state == 1


class Relay(object):
    """Simple Relay object for easier relay handling.
    """

    def __init__(self, pin, open_state=GPIO.HIGH):
        """Initialize a Relay object.

        @param pin: GPIO pin for the relay
        @type pin: int

        @param open_state: GPIO state for the relay to be open.
        @type open_state: GPIO.LOW or GPIO.HIGH
        """
        self.pin = pin
        self.open_state = open_state
        if GPIO.getmode() is None:
            GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.pin, GPIO.OUT)
        GPIO.output(self.pin, self.open_state)  # Set open state immediately to not toggle relay.

    def toggle(self, duration=500):
        """Toggle the relay for a specified time.

        @param duration: Time to activate relay for (in milliseconds).
        @type duration: int
        """
        GPIO.output(self.pin, not self.open_state)
        time.sleep(duration * 0.001)
        GPIO.output(self.pin, self.open_state)


class GarageDoor(Accessory):
    category = Category.GARAGE_DOOR_OPENER

    def __init__(self, relay_pin, switch_pin, *args,
                 relay_open_state=GPIO.HIGH, switch_bounce_time=1500,
                 switch_edge=GPIO.BOTH, switch_closed_state=GPIO.LOW,
                 switch_pull_up_down=GPIO.PUD_UP, **kwargs):
        self._bounce_time = switch_bounce_time
        self.relay = Relay(relay_pin, relay_open_state)
        self.switch = DoorSwitch(switch_pin, self.door_state_changed, switch_bounce_time,
                                 switch_edge, switch_closed_state, switch_pull_up_down)
        super(GarageDoor, self).__init__(*args, **kwargs)

    def door_state_changed(self):
        """This is the method to override to handle switch callback logic, for instance, hooking to a REST API
        Generally, in a one door switch setup, you would only use this to set Open and Closed states.
        It is best advised to call super after your logic.
        """
        time.sleep(self.switch.bounce_time * 0.001)  # Read switch after bounce time elapses.
        state = self.switch.state  # Read state once from GPIO
        if state != self.current_state_characteristic.value:  # Check if states don't match
            self.target_state_characteristic.set_value(state)  # Use characteristic for callback

    def _request_handler(self, new_state):
        """This is the method to override to handle HomeKit Requests
        Use self.target_state.characteristic.set_value to access this.

        @note: Current States - 0: Open, 1: Closed, 2: Opening, 3: Closing, 4: Stopped
        @note: Target States - 0: Open Door, 1: Close Door
        """
        if new_state:  # If closing
            self.current_state_characteristic.set_value(3)  # Closing
            if self.switch.is_closed:  # Check for CLOSED
                time.sleep(3)  # Add latency for socket delays.
                self.current_state_characteristic.set_value(1)  # CLOSED
            else:  # Not CLOSED
                self.relay.toggle()  # Toggle Relay to put door in motion.
        else:  # If opening
            self.current_state_characteristic.set_value(2)  # Opening
            if not self.switch.is_closed:  # Check for OPEN
                time.sleep(3)  # Add latency for socket delays.
                self.current_state_characteristic.set_value(0)  # OPEN
            else:  # Not OPEN
                self.relay.toggle()  # Toggle Relay to put door in motion.

    def _set_services(self):
        super(GarageDoor, self)._set_services()
        garage_door_service = loader.get_serv_loader().get('GarageDoorOpener')
        self.add_service(garage_door_service)
        self.target_state_characteristic = garage_door_service.get_characteristic('TargetDoorState')
        self.current_state_characteristic = garage_door_service.get_characteristic('CurrentDoorState')
        self.target_state_characteristic.setter_callback = self._request_handler

        self.current_state_characteristic.set_value(4)  # Set Stopped.
        self._request_handler(self.switch.state)  # Set initial state

    def stop(self):
        super(GarageDoor, self).stop()
        GPIO.cleanup()
