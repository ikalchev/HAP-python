"""A fake fan that does nothing but to demonstrate optional characteristics."""
import logging

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_FAN
import pyhap.loader as loader

logger = logging.getLogger(__name__)


class FakeFan(Accessory):
    """A fake fan accessory that logs changes to its rotation speed and direction."""

    category = CATEGORY_FAN

    def set_rotation_speed(self, value):
        logger.debug("Rotation speed changed: %s", value)

    def set_rotation_direction(self, value):
        logger.debug("Rotation direction changed: %s", value)

    def _set_services(self):
        """Add the fan service. Also add optional characteristics to it."""
        super()._set_services()
        fan_service = loader.get_serv_loader().get_service("Fan")
        # NOTE: Don't forget that all characteristics must be added to the service before
        # adding the service to the accessory, so that it can assign IIDs to all.

        # Add the optional RotationSpeed characteristic to the Fan
        rotation_speed_char = loader.get_char_loader() \
            .get_char("RotationSpeed")
        fan_service.add_characteristic(rotation_speed_char)
        rotation_speed_char.setter_callback = self.set_rotation_speed

        # Add the optional RotationSpeed characteristic to the Fan
        rotation_dir_char = loader.get_char_loader() \
            .get_char("RotationDirection")
        fan_service.add_characteristic(rotation_dir_char)
        rotation_dir_char.setter_callback = self.set_rotation_direction

        self.add_service(fan_service)
