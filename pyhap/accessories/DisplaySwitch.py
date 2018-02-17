# An Accessory for viewing/controlling the status of a Mac display.
import subprocess

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


def get_display_state():
    result = subprocess.check_output(['pmset', '-g', 'powerstate', 'IODisplayWrangler'])
    return int(result.strip().split(b'\n')[-1].split()[1]) >= 4


def set_display_state(state):
    if state:
        subprocess.call(['caffeinate', '-u', '-t', '1'])
    else:
        subprocess.call(['pmset', 'displaysleepnow'])


class DisplaySwitch(Accessory):
    """
    An accessory that will display, and allow setting, the display status
    of the Mac that this code is running on.
    """

    category = Category.SWITCH

    def __init__(self, *args, **kwargs):
        super(DisplaySwitch, self).__init__(*args, **kwargs)

        self.display = self.get_service("Switch")\
                           .get_characteristic("On")
        self.display.setter_callback = self.set_display

    def _set_services(self):
        super(DisplaySwitch, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("Switch"))

    def run(self):
        while not self.run_sentinel.wait(1):
            # We can't just use .set_value(state), because that will
            # trigger our listener.
            state = get_display_state()
            if self.display.value != state:
                self.display.value = state
                self.display.notify()

    def set_display(self, state):
        if get_display_state() != state:
            set_display_state(state)
