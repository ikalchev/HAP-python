"""The contexts passed to delegate and lifecycle calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from .connection import HAPConnectionHandle

if TYPE_CHECKING:
    from .characteristic import Characteristic


@dataclass(frozen=True)
class CharContext:
    """What a characteristic callback knows about the controller it serves.

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
    """

    client_addr: tuple[str, int] | None
    client_uuid: UUID | None
    connection: HAPConnectionHandle
    deadline: float


@dataclass(frozen=True)
class CharReadContext(CharContext):
    """The context of a read, e.g. of ``on_get``.

    :ivar characteristic: The characteristic being read.
    """

    characteristic: Characteristic


@dataclass(frozen=True)
class CharWriteContext(CharContext):
    """The context of a write, e.g. of ``on_set``.

    :ivar characteristic: The characteristic being written, or ``None``
        for a grouped call that carries several, e.g. a service's or an
        accessory's ``on_set``.
    :ivar is_admin: Whether the writing controller has admin privileges.
    """

    characteristic: Characteristic | None
    is_admin: bool
