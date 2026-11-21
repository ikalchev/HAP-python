"""Reading the accessory's persisted state file (``accessory.state``)."""

from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.asymmetric import ed25519


def load_accessory_ltpk(path: str | os.PathLike[str]) -> bytes:
    """Return the accessory's raw long-term public key (LTPK) from its state file.

    The state file is the ``AccessoryDriver``'s ``accessory.state``.

    Use this to move just the accessory's public key to another machine, e.g.
    where a client runs: the state file also contains the accessory's
    *private* key, so prefer exporting the public key alone (via this
    function or the ``"public_key"`` hex field) over copying the whole file.

    :raise ValueError: If the file is not an accessory state file or the key
        is malformed.
    :raise OSError: If the file does not exist or cannot be read.
    """
    target = os.path.expanduser(os.fspath(path))
    try:
        with open(target, encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        ltpk = bytes.fromhex(data["public_key"])
        ed25519.Ed25519PublicKey.from_public_bytes(ltpk)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"Not an accessory state file (no valid 'public_key'): {target}"
        ) from error
    return ltpk
