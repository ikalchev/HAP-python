"""A fake fan that does nothing but to demonstrate optional characteristics."""
import logging

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader

logger = logging.getLogger(__name__)


class FakeFan(Accessory):
    """A fake fan accessory that logs changes to its rotation speed and direction."""

    category = Category.FAN

    def set_rotation_speed(self, value):
        logger.debug("Rotation speed changed: %s", value)

    def set_rotation_direction(self, value):
        logger.debug("Rotation direction changed: %s", value)

    def _set_services(self):
        """Add the fan service. Also add optional characteristics to it."""
        super(FakeFan, self)._set_services()
        service_loader = loader.get_serv_loader()
        fan_service = service_loader.get("Fan")
        # NOTE: Don't forget that all characteristics must be added to the service before
        # adding the service to the accessory, so that it can assign IIDs to all.

        # Add the optional RotationSpeed characteristic to the Fan
        rotation_speed_char = loader.get_char_loader().get("RotationSpeed")
        fan_service.add_opt_characteristic(rotation_speed_char)
        rotation_speed_char.setter_callback = self.set_rotation_speed

        # Add the optional RotationSpeed characteristic to the Fan
        rotation_dir_char = loader.get_char_loader().get("RotationDirection")
        fan_service.add_opt_characteristic(rotation_dir_char)
        rotation_dir_char.setter_callback = self.set_rotation_direction

        self.add_service(fan_service)
