# An Accessory for viewing/controlling the status of a Mac display.
import subprocess

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SWITCH


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

    category = CATEGORY_SWITCH

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_switch = self.add_preload_service('Switch')
        self.display = serv_switch.configure_char(
            'On', setter_callback=self.set_display)

    @Accessory.run_at_interval(1)
    def run(self):
        # We can't just use .set_value(state), because that will
        # trigger our listener.
        state = get_display_state()
        if self.display.value != state:
            self.display.value = state
            self.display.notify()

    def set_display(self, state):
        if get_display_state() != state:
            set_display_state(state)
