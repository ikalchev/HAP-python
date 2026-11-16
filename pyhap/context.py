"""The contexts passed to delegate and lifecycle calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from .connection import HAPConnectionHandle

if TYPE_CHECKING:
    from .characteristic import Characteristic
    from .service import Service


@dataclass(frozen=True)
class Context:
    """What a delegate call knows about the controller it serves.

    Contexts are the input side of the callback interface: new input data
    arrives as new fields of this class, so a callback's signature never
    changes - watch this class instead of callback parameters. Instances
    are frozen; a callback must not try to change one.

    :ivar client_addr: The controller's ``(address, port)``, ``None``
        without a connection.
    :ivar client_uuid: The identity of the paired controller, ``None``
        before pair-verify completes.
    :ivar connection: The stable identity of the connection, see
        :class:`~pyhap.connection.HAPConnectionHandle`.
    :ivar deadline: Monotonic time; the driver answers ``-70408`` for a
        callback that is not done by it.

    Contexts identify their target by the object itself, a
    :class:`~pyhap.characteristic.Characteristic` or a
    :class:`~pyhap.service.Service`, never by an id: iids are the wire
    protocol's identity, assigned by the driver at publish time and
    meaningless outside a running driver, while a delegate is wired at
    construction time, before iids exist. The object is both the
    identity for dispatch and the handle to what a callback needs -
    display name, stored value, properties, the owning service.
    """

    client_addr: tuple[str, int] | None
    client_uuid: UUID | None
    connection: HAPConnectionHandle
    deadline: float


@dataclass(frozen=True)
class CharReadContext(Context):
    """The context of a read, e.g. of ``on_get``.

    :ivar characteristic: The characteristic being read.
    """

    characteristic: Characteristic


@dataclass(frozen=True)
class CharWriteContext(Context):
    """The context of a write of one characteristic, e.g. of ``on_set``.

    :ivar characteristic: The characteristic being written.
    :ivar is_admin: Whether the writing controller has admin privileges.
    """

    characteristic: Characteristic
    is_admin: bool


@dataclass(frozen=True)
class ServiceWriteContext(Context):
    """The context of a grouped write of a service, its ``on_set`` gate.

    :ivar service: The service whose characteristics are written.
    :ivar is_admin: Whether the writing controller has admin privileges.
    """

    service: Service
    is_admin: bool
