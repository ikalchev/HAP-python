"""This module implements the HAP events."""

from typing import Any

from .const import HAP_REPR_CHARS
from .util import to_hap_json

EVENT_MSG_STUB = (
    b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\nContent-Length: "
)


def create_hap_event(data: list[dict[str, Any]]) -> bytes:
    """Creates a HAP HTTP EVENT response for the given data.

    @param data: Payload of the request.
    @type data: list
    """
    bytesdata = to_hap_json({HAP_REPR_CHARS: data})
    return b"".join(
        (EVENT_MSG_STUB, str(len(bytesdata)).encode("utf-8"), b"\r\n" * 2, bytesdata)
    )
