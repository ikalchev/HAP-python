"""A fake fan that does nothing but to demonstrate optional characteristics."""
import logging

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_FAN

logger = logging.getLogger(__name__)


class FakeFan(Accessory):
    """A fake fan accessory that logs changes to its rotation speed and direction."""

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
        logger.debug("Rotation speed changed: %s", value)

    def set_rotation_direction(self, value):
        logger.debug("Rotation direction changed: %s", value)
