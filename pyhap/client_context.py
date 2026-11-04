"""Context of the HAP client session a characteristic write came from."""

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True, slots=True)
class ClientContext:
    """The HAP client session a characteristic write came from.

    Passed to ``Characteristic.setter_callback_with_context``. ``shared_key`` is
    the pair-verify session key of the connection, or ``None`` when the
    connection is unknown or not yet encrypted.
    """

    address: Optional[Tuple[str, int]]
    shared_key: Optional[bytes] = field(default=None, repr=False, compare=False)
