"""This module implements the HAP events."""

from .const import HAP_REPR_CHARS
from .util import to_hap_json

EVENT_MSG_STUB = (
    b"EVENT/1.0 200 OK\r\n"
    b"Content-Type: application/hap+json\r\n"
    b"Content-Length: "
)


def create_hap_event(data):
    """Creates a HAP HTTP EVENT response for the given data.

    @param data: Payload of the request.
    @type data: bytes
    """
    bytesdata = to_hap_json({HAP_REPR_CHARS: data})
    return (
        EVENT_MSG_STUB + str(len(bytesdata)).encode("utf-8") + b"\r\n" * 2 + bytesdata
    )
