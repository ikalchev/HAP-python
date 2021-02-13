"""Module for the IIDManager class."""
import logging

logger = logging.getLogger(__name__)


class IIDManager:
    """Maintains a mapping between Service/Characteristic objects and IIDs."""

    def __init__(self):
        """Initialize an empty instance."""
        self.counter = 0
        self.iids = {}
        self.objs = {}

    def assign(self, obj):
        """Assign an IID to given object. Print warning if already assigned.

        :param obj: The object that will be assigned an IID.
        :type obj: Service or Characteristic
        """
        if obj in self.iids:
            logger.warning(
                "The given Service or Characteristic with UUID %s already "
                "has an assigned IID %s, ignoring.",
                obj.type_id,
                self.iids[obj],
            )
            return

        self.counter += 1
        self.iids[obj] = self.counter
        self.objs[self.counter] = obj

    def get_obj(self, iid):
        """Get the object that is assigned the given IID."""
        return self.objs.get(iid)

    def get_iid(self, obj):
        """Get the IID assigned to the given object."""
        return self.iids.get(obj)

    def remove_obj(self, obj):
        """Remove an object from the IID list."""
        iid = self.iids.pop(obj, None)
        if iid is None:
            logger.error("Object %s not found.", obj)
            return None
        del self.objs[iid]
        return iid

    def remove_iid(self, iid):
        """Remove an object with an IID from the IID list."""
        obj = self.objs.pop(iid, None)
        if obj is None:
            logger.error("IID %s not found.", iid)
            return None
        del self.iids[obj]
        return obj
