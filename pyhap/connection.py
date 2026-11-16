"""The identity of a controller connection."""

from __future__ import annotations


class HAPConnectionHandle:
    """The stable identity of one controller connection.

    Contexts pass this to delegate and lifecycle calls, so a callback can
    tie what it does to the connection that caused it, e.g. a camera may
    let only the connection that set a session up control it. Beyond
    ``client_addr`` it is opaque on purpose: the address of a connection
    is unique only while it lives, a later connection may reuse the same
    ``(address, port)`` pair, so equality and hashing are by the
    connection's identity - two handles are equal only for the same
    connection. Anything kept per connection, a write response, a camera
    session, cannot leak to a connection that reused the address of a
    closed one.

    The driver's per-connection state - event subscriptions, prepared
    writes, write responses and the server's connections - is keyed by
    the handle, so nothing of a closed connection reaches a later one,
    and an answer that arrives only after its connection closed is
    dropped instead of kept. A direct call without a connection acts as
    a virtual handle of its client address, see
    ``AccessoryDriver._connection_of``.
    """

    __slots__ = ("_client_addr", "_generation")

    def __init__(self, client_addr: tuple[str, int] | None, generation: int) -> None:
        """Initialise with the connection's address and its generation.

        :param client_addr: The connection's ``(address, port)``, or
            ``None`` for a call without a connection, e.g. a
            programmatic write through the legacy API. A controller's
            request always has one.
        :param generation: A driver-internal number that differs between
            two connections with the same address; part of what makes
            the handle unique, but not part of its interface.
        """
        self._client_addr = client_addr
        self._generation = generation

    @property
    def client_addr(self) -> tuple[str, int] | None:
        """The connection's ``(address, port)``, ``None`` without one."""
        return self._client_addr

    def __eq__(self, other: object) -> bool:
        """Equal only for the same connection, never for a reused address."""
        if not isinstance(other, HAPConnectionHandle):
            return NotImplemented
        return self._key == other._key

    def __ne__(self, other: object) -> bool:
        """Not equal for anything but the same connection."""
        result = self.__eq__(other)
        if result is NotImplemented:
            return NotImplemented
        return not result

    @property
    def _key(self) -> tuple[tuple[str, int] | None, int]:
        """What identifies the handle, also for hashing."""
        return (self._client_addr, self._generation)

    def __hash__(self) -> int:
        """Hash by the same key :meth:`__eq__` compares."""
        return hash(self._key)

    def __repr__(self) -> str:
        """Show the connection's address and identity."""
        return f"{self.__class__.__name__}({self._client_addr!r}, {self._generation})"
