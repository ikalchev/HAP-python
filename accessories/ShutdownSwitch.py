"""Provides a switch accessory that executes sudo shutdown -h.

This allows you to halt a Raspberry Pi and then plug it off safely.

NOTE: For this to work, you need to allow passwordless /sbin/shutdown to
whichever user is running HAP-python. For example, you can do:
$ sudo visudo
$ # add the line "hap-user ALL=NOPASSWD: /sbin/shutdown"
"""
import os
import logging

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SWITCH

logger = logging.getLogger(__name__)


class ShutdownSwitch(Accessory):
    """A switch accessory that executes sudo shutdown."""

    category = CATEGORY_SWITCH

    def __init__(self, *args, **kwargs):
        """Initialize and set a shutdown callback to the On characteristic."""
        super().__init__(*args, **kwargs)

        serv_switch = self.add_preload_service('Switch')
        self.char_on = serv_switch.configure_char(
            'On', setter_callback=self.execute_shutdown)

    def execute_shutdown(self, _value):
        """Execute shutdown -h."""
        logger.info("Executing shutdown command.")
        os.system("sudo shutdown -h now")
