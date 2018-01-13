"""Provides a switch accessory that executes sudo shutdown -h.

This allows you to halt a Raspberry Pi and then plug it off safely.

NOTE: For this to work, you need to allow passwordless /sbin/shutdown to
whichever user is running HAP-python. For example, you can do:
$ sudo visudo
$ # add the line "hap-user ALL=NOPASSWD: /sbin/shutdown"
"""
import os
import logging

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader

logger = logging.getLogger(__name__)


class ShutdownSwitch(Accessory):
    """A switch accessory that executes sudo shutdown."""

    category = Category.SWITCH

    def __init__(self, *args, **kwargs):
        """Initialise and set a shutdown callback to the On characteristic."""
        super(ShutdownSwitch, self).__init__(*args, **kwargs)
        on_char = self.get_service("Switch")\
                      .get_characteristic("On")
        on_char.setter_callback = self.execute_shutdown

    def _set_services(self):
        """Add the Switch service."""
        super(ShutdownSwitch, self)._set_services()
        service_loader = loader.get_serv_loader()
        self.add_service(service_loader.get("Switch"))

    def execute_shutdown(self, _value):
        """Execute shutdown -h."""
        logger.info("Executing shutdown command.")
        os.system("sudo shutdown -h now")
