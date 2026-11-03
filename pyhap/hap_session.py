"""Authenticated, connection-scoped HAP session context."""

import logging
from typing import Callable, List, Optional
from uuid import UUID

_LOGGER = logging.getLogger(__name__)


class HAPSession:
    """Context for one successfully verified controller connection.

    The shared key is sensitive and must not be logged or persisted.
    Close callbacks run once, on the protocol event loop.
    """

    def __init__(self, client_uuid: UUID, shared_key: bytes) -> None:
        self.client_uuid = client_uuid
        self.shared_key: Optional[bytes] = shared_key
        self._close_callbacks: List[Callable[[], None]] = []
        self.closed = False

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Register a zero-argument callback; invoke immediately if closed."""
        if self.closed:
            callback()
        else:
            self._close_callbacks.append(callback)

    def close(self) -> None:
        """Invalidate the session and notify its dependents exactly once."""
        if self.closed:
            return
        self.closed = True
        self.shared_key = None
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("HAP session close callback failed")
