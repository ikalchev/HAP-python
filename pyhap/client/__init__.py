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

    identity = ClientIdentity.generate(0)
    driver.pair(identity.uuid_bytes, identity.public_key_bytes, HAP_PERMISSIONS.ADMIN)
    # ... start the driver, then:
    client = HAPClient(identity, accessory_ltpk_raw_bytes)
    await client.connect("127.0.0.1", port)
    frame = await client.get_accessories()

To reconnect as the same controller after a restart, persist the identity
yourself (UUID + private key, written 0600) and load it back::

    identity.save("controller.key")
    identity = ClientIdentity.load("controller.key")
"""

from ..const import RESPONSE_TIMEOUT
from .controller import HAPClient
from .identity import ClientIdentity
from .protocol import Frame, parse_frame

__all__ = [
    "RESPONSE_TIMEOUT",
    "ClientIdentity",
    "Frame",
    "HAPClient",
    "parse_frame",
]
