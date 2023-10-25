"""Module for the IIDManager class."""
import logging
from typing import TYPE_CHECKING, Dict, Optional, Union

if TYPE_CHECKING:
    from .characteristic import Characteristic
    from .service import Service

    ServiceOrCharType = Union[Service, Characteristic]

logger = logging.getLogger(__name__)


class IIDManager:
    """Maintains a mapping between Service/Characteristic objects and IIDs."""

    def __init__(self) -> None:
        """Initialize an empty instance."""
        self.counter = 0
        self.iids: Dict["ServiceOrCharType", int] = {}
        self.objs: Dict[int, "ServiceOrCharType"] = {}

    def assign(self, obj: "ServiceOrCharType") -> None:
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

        iid = self.get_iid_for_obj(obj)
        self.iids[obj] = iid
        self.objs[iid] = obj

    def get_iid_for_obj(self, obj: "ServiceOrCharType") -> int:
        """Get the IID for the given object.

        Override this method to provide custom IID assignment.
        """
        self.counter += 1
        return self.counter

    def get_obj(self, iid: int) -> "ServiceOrCharType":
        """Get the object that is assigned the given IID."""
        return self.objs.get(iid)

    def get_iid(self, obj: "ServiceOrCharType") -> int:
        """Get the IID assigned to the given object."""
        return self.iids.get(obj)

    def remove_obj(self, obj: "ServiceOrCharType") -> Optional[int]:
        """Remove an object from the IID list."""
        iid = self.iids.pop(obj, None)
        if iid is None:
            logger.error("Object %s not found.", obj)
            return None
        del self.objs[iid]
        return iid

    def remove_iid(self, iid: int) -> Optional["ServiceOrCharType"]:
        """Remove an object with an IID from the IID list."""
        obj = self.objs.pop(iid, None)
        if obj is None:
            logger.error("IID %s not found.", iid)
            return None
        del self.iids[obj]
        return obj
