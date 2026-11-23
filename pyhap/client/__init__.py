"""A HAP client (controller) for driving a HAP accessory over the real protocol.

The client speaks the same protocol as a real iOS controller:

* ``POST /pair-verify`` with X25519/Ed25519/HKDF and TLV payloads, based on a
  pre-paired long-term identity (:class:`ClientIdentity`).
* The encrypted transport (ChaCha20-Poly1305 in 1024 byte blocks) via
  :class:`pyhap.hap_crypto.HAPCrypto` in ``client_side`` mode.
* Plain HTTP/1.1 requests for characteristic reads/writes, subscriptions and
  ``GET /accessories``, plus parsing of interleaved ``EVENT/1.0`` push frames
  on the same connection.

It is used by the benchmark suite (``benchmarks/run.py``) and by the
integration tests in ``tests/integration/`` to exercise a running
:class:`pyhap.accessory_driver.AccessoryDriver` end to end::

    # A controller whose identity is persisted at controller.key: loaded
    # back on later runs, created and saved on the first. Pass
    # accessory_ltpk=... or accessory_state="accessory.state" once to pin
    # the accessory's key for verification (on another machine, point
    # accessory_state at a copy of the accessory's state file).
    client = HAPClient.new_persistent(filepath="controller.key")
    driver.pair(
        client.identity.uuid_bytes,
        client.identity.public_key_bytes,
        HAP_PERMISSIONS.ADMIN,
    )
    # ... start the driver, then:
    await client.connect("127.0.0.1", port)
    frame = await client.get_accessories()
"""

from ..const import RESPONSE_TIMEOUT
from .controller import HAPClient
from .identity import ClientIdentity
from .protocol import Frame, parse_frame
from .state import (
    add_client_pairing,
    load_accessory_ltpk,
    load_accessory_state,
    remove_client_pairing,
    save_accessory_state,
)

__all__ = [
    "RESPONSE_TIMEOUT",
    "ClientIdentity",
    "Frame",
    "HAPClient",
    "add_client_pairing",
    "load_accessory_ltpk",
    "load_accessory_state",
    "parse_frame",
    "remove_client_pairing",
    "save_accessory_state",
]
