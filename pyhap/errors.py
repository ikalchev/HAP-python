"""Errors raised by accessory callbacks to answer a HAP request."""

from .const import HAP_SERVER_STATUS

# The statuses a callback may answer with, as HAP status codes. The rest are
# reserved for the accessory driver: privileges and read-only, write-only and
# notification permissions are enforced before any callback runs, and a timed
# out callback is answered by the driver's watchdog. Constructing a
# HapStatusError with anything else fails fast with a ValueError, so a wrong
# status is caught at its source instead of silently altering a request.
CALLBACK_STATUSES = frozenset(
    {
        HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
        HAP_SERVER_STATUS.RESOURCE_BUSY,
        HAP_SERVER_STATUS.OUT_OF_RESOURCE,
        HAP_SERVER_STATUS.RESOURCE_DOES_NOT_EXIST,
        HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST,
        HAP_SERVER_STATUS.INSUFFICIENT_AUTHORIZATION,
    }
)


class HapStatusError(Exception):
    """Raise from a callback to answer its request with a HAP status.

    A characteristic's setter or getter callback may raise this instead of
    failing generically, and the controller receives the status for that
    characteristic in the HAP response - so the Home app can tell a refusal
    apart from a communication failure. Every other exception of a callback
    keeps answering ``-70402`` (service communication failure).

    For example, the setter of a lock whose motor is busy::

        from pyhap.const import HAP_SERVER_STATUS
        from pyhap.errors import HapStatusError

        def set_lock_target_state(value):
            if not lock.is_ready():
                raise HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
            lock.set_state(value)

    A callback may answer with one of these statuses:

    * ``-70402`` service communication failure - the default for any other
      exception, raise it only to be explicit.
    * ``-70403`` resource busy - the device refuses the request, try again.
    * ``-70407`` out of resource - e.g. no free streaming session.
    * ``-70409`` resource does not exist - e.g. an unknown session id.
    * ``-70410`` invalid value in a request - a value that passed the
      format checks but is semantically wrong.
    * ``-70411`` insufficient authorization - the accessory refuses this
      controller.

    The other HAP statuses are reserved for the accessory driver and cannot
    be raised: privileges and permissions are enforced before a callback
    runs, and a callback that is too slow is answered by the driver itself.

    :param status: One of the statuses above, as a
        :class:`~pyhap.const.HAP_SERVER_STATUS` value.
    :raises ValueError: If ``status`` is reserved for the driver or unknown.
    """

    __slots__ = ("status",)

    def __init__(self, status: int) -> None:
        """Initialise with the HAP status to answer with."""
        if status not in CALLBACK_STATUSES:
            raise ValueError(
                f"HAP status {status} is reserved for the accessory driver or"
                " unknown; a callback may answer with one of:"
                f" {', '.join(str(code) for code in sorted(CALLBACK_STATUSES))}"
            )
        super().__init__(f"HAP status {status}")
        self.status = status
