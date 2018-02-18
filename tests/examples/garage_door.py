"""An example of how to setup and start an Accessory.

This is:
1. Create the Accessory object you want.
2. Add it to an AccessoryDriver, which will advertise it on the local network,
    setup a server to answer client queries, etc.
"""
import logging
import signal

from pyhap.accessories.GarageDoor import TwoSwitchGarageDoor
from pyhap.accessory_driver import AccessoryDriver

logging.basicConfig(level=logging.INFO)


def get_accessory():
    """Call this method to get a standalone Accessory."""
    acc = TwoSwitchGarageDoor("GarageDoor", gpio_pins={
        'relay': 17,
        'top_limit': 4,
        'bottom_limit': 3,
    })
    return acc


acc = get_accessory()  # Change to get_bridge() if you want to run a Bridge.

# Start the accessory on port 51826
driver = AccessoryDriver(acc, port=51826, persist_file='garage_door.state')
# We want KeyboardInterrupts and SIGTERM (kill) to be handled by the driver itself,
# so that it can gracefully stop the accessory, server and advertising.
signal.signal(signal.SIGINT, driver.signal_handler)
signal.signal(signal.SIGTERM, driver.signal_handler)
# Start it!
driver.start()
