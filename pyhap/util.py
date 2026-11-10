import asyncio
import base64
from collections.abc import Awaitable, Callable
import functools
import inspect
import random
import socket
from typing import Any, cast
from uuid import UUID

import orjson

from .const import BASE_UUID

ALPHANUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
HEX_DIGITS = "0123456789ABCDEF"
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


rand = random.SystemRandom()


def callback(func: Any) -> Callable[..., Any]:
    """Decorator for non blocking functions."""
    func._pyhap_callback = True
    return cast(Callable[..., Any], func)


def is_callback(func: Any) -> Any:
    """Check if function is callback."""
    return "_pyhap_callback" in getattr(func, "__dict__", {})


def iscoro(func: Any) -> Any:
    """Check if the function is a coroutine or if the function is a
    ``functools.partial``, check the wrapped function for the same.
    """
    if isinstance(func, functools.partial):
        func = func.func
    return inspect.iscoroutinefunction(func)


def get_local_address() -> str:
    """Grabs the local IP address using a socket.

    :return: Local IP Address in IPv4 format.
    :rtype: str
    """
    # TODO: try not to talk 8888 for this
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
    finally:
        s.close()
    return str(addr)


def long_to_bytes(n: Any) -> bytes:
    """Convert a ``long int`` to ``bytes``

    :param n: Long Integer
    :type n: int

    :return: ``long int`` in ``bytes`` format.
    :rtype: bytes
    """
    if isinstance(n, int) and n >= 0:
        # Round the bit count up to whole bytes; shifting by 3 divides by 8.
        # A zero bit count keeps zero encoded as empty bytes.
        return n.to_bytes((n.bit_length() + 7) >> 3, "big")

    # Preserve bitwise behavior for subclasses and other numeric objects.
    byteList = []
    x = 0
    off = 0
    while x != n:
        b = (n >> off) & 0xFF
        byteList.append(b)
        x = x | (b << off)
        off += 8
    byteList.reverse()
    return bytes(byteList)


def generate_mac() -> str:
    """Generates a fake mac address used in broadcast.

    :return: MAC address in format XX:XX:XX:XX:XX:XX
    :rtype: str
    """
    return "{}{}:{}{}:{}{}:{}{}:{}{}:{}{}".format(
        *(rand.choice(HEX_DIGITS) for _ in range(12))
    )


def generate_setup_id() -> str:
    """Generates a random Setup ID for an ``Accessory`` or ``Bridge``.

    Used in QR codes and the setup hash.

    :return: 4 digit alphanumeric code.
    :rtype: str
    """
    return "".join([rand.choice(ALPHANUM) for i in range(4)])


def generate_pincode() -> bytes:
    """Generates a random pincode.

    :return: pincode in format ``xxx-xx-xxx``
    :rtype: bytearray
    """
    return "{}{}{}-{}{}-{}{}{}".format(*(rand.randint(0, 9) for i in range(8))).encode(
        "ascii"
    )


def to_base64_str(bytes_input: Any) -> str:
    return base64.b64encode(bytes_input).decode("utf-8")


def base64_to_bytes(str_input: Any) -> bytes:
    return base64.b64decode(str_input.encode("utf-8"))


def byte_bool(boolv: Any) -> bytes:
    return b"\x01" if boolv else b"\x00"


async def event_wait(event: asyncio.Event, timeout: float) -> bool:
    """Wait for the given event to be set or for the timeout to expire.

    :param event: The event to wait for.
    :type event: asyncio.Event

    :param timeout: The timeout for which to wait, in seconds.
    :type timeout: float

    :return: ``event.is_set()``
    :rtype: bool
    """
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
    except TimeoutError:
        pass
    return event.is_set()


@functools.lru_cache(maxsize=2048)
def uuid_to_hap_type(uuid: UUID | str) -> str:
    """Convert a UUID to a HAP type."""
    long_type = str(uuid).upper()
    if not long_type.endswith(BASE_UUID):
        return long_type
    return long_type.split("-", 1)[0].lstrip("0")


@functools.lru_cache(maxsize=2048)
def hap_type_to_uuid(hap_type: str) -> UUID:
    """Convert a HAP type to a UUID."""
    if "-" in hap_type:
        return UUID(hap_type)
    return UUID("0" * (8 - len(hap_type)) + hap_type + BASE_UUID)


def to_hap_json(dump_obj: Any) -> Any:
    """Convert an object to HAP json."""
    return orjson.dumps(dump_obj)


def to_sorted_hap_json(dump_obj: Any) -> Any:
    """Convert an object to sorted HAP json."""
    return orjson.dumps(
        dump_obj,
        option=orjson.OPT_SORT_KEYS,
    )


def from_hap_json(json_str: Any) -> Any:
    """Convert json to an object.

    Accepts a ``str`` or UTF-8 ``bytes`` (``orjson.loads`` handles both
    without an intermediate decode).
    """
    return orjson.loads(json_str)


def async_create_background_task(func: Awaitable[Any]) -> asyncio.Task[Any]:
    """Create a background task and add it to the set of background tasks."""
    task = asyncio.ensure_future(func)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
