# code for connecting relay to homebridge, losely based on the on/off screen function in this repo. 
# can also be used to control other GPIO based devices
# Usage: 
# relay parameters: GPIO pin, timer, reverse on/off, starting state
# bridge.add_accessory(RelaySwitch(38, 0, 0, 1, driver, 'Name', ))
# can also be used with switch characteristic with minor changes. 
# feel free to improve

def _gpio_setup(pin):
    if GPIO.getmode() is None:
        GPIO.setmode(GPIO.BOARD)
    GPIO.setup(pin, GPIO.OUT)


def set_gpio_state(pin, state, reverse):
    if state:
        if reverse:
            GPIO.output(pin, 1)
        else:
            GPIO.output(pin, 0)
    else:
        if reverse:
            GPIO.output(pin, 0)
        else:
            GPIO.output(pin, 1)
    #logging.info("Setting pin: %s to state: %s", pin, state)


def get_gpio_state(pin, reverse):
    if GPIO.input(pin):
        if reverse:
            return int(1)
        else:
            return int(0)
    else:
        if reverse:
            return int(0)
        else:
            return int(1)


class RelaySwitch(Accessory):
    category = CATEGORY_OUTLET

    def __init__(self, pin_number, counter, reverse, startstate, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.pin_number = pin_number
        self.counter = counter
        self.reverse = reverse
        self.startstate = startstate

        _gpio_setup(self.pin_number)

        serv_switch = self.add_preload_service('Outlet')
        self.relay_on = serv_switch.configure_char(
            'On', setter_callback=self.set_relay)

        self.relay_in_use = serv_switch.configure_char(
            'OutletInUse', setter_callback=self.get_relay_in_use)

        self.timer = 1

        self.set_relay(startstate)

    @Accessory.run_at_interval(1)
    def run(self):
        state = get_gpio_state(self.pin_number, self.reverse)

        if self.relay_on.value != state:
            self.relay_on.value = state
            self.relay_on.notify()
            self.relay_in_use.notify()

        oldstate = 1

        if state != oldstate:
            self.timer = 1
            oldstate == state

        if self.timer == self.counter:
            set_gpio_state(self.pin_number, 0, self.reverse)
            self.timer = 1

        self.timer = self.timer + 1
        #logging.info("counter %s state is %s", self.timer, state)


    def set_relay(self, state):
        if get_gpio_state(self.pin_number, self.reverse) != state:
            if state:
                set_gpio_state(self.pin_number, 1, self.reverse)
            else:
                set_gpio_state(self.pin_number, 0, self.reverse)

    def get_relay_in_use(self, state):
        return True

