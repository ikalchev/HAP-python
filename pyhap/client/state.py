"""Reading and updating the accessory's persisted state file (``accessory.state``)."""

from __future__ import annotations

import json
import os
import tempfile

from cryptography.hazmat.primitives.asymmetric import ed25519

from ..const import HAP_PERMISSIONS
from ..encoder import AccessoryEncoder
from ..state import State
from .identity import ClientIdentity


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


def add_client_pairing(
    path: str | os.PathLike[str],
    identity: ClientIdentity,
    permissions: bytes = HAP_PERMISSIONS.ADMIN,
) -> None:
    """Register ``identity`` as a paired client in an accessory state file, offline.

    Loads the ``AccessoryDriver`` state file at ``path`` with
    :meth:`pyhap.encoder.AccessoryEncoder.load_into`, adds the client via
    :meth:`pyhap.state.State.add_paired_client`, and writes the file back
    atomically (a 0600 tempfile swapped in with ``os.replace``, like the
    driver does). No driver needs to run.

    Do this with the accessory **stopped**: a running driver keeps its state
    in memory and would overwrite this edit on its next persist. (While the
    driver runs, ``AccessoryDriver.pair(...)`` does the same in-process.)
    After the accessory restarts with the updated file, a client with this
    identity can pair-verify against it.

    :param path: The accessory's ``accessory.state`` file.
    :param identity: The client identity to pair (UUID + long-term public key).
    :param permissions: The pairing's permissions, admin by default.
    :raise OSError: If the file does not exist or cannot be read.
    :raise ValueError: If the file is malformed or the UUID is invalid.
    """
    target = os.path.expanduser(os.fspath(path))
    state = State(address="127.0.0.1")  # placeholders; load_into overwrites them
    with open(target, encoding="utf-8") as file_handle:
        AccessoryEncoder().load_into(file_handle, state)
    state.add_paired_client(identity.uuid_bytes, identity.public_key_bytes, permissions)
    directory = os.path.dirname(target) or "."
    tmp_filename = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False, encoding="utf-8"
        ) as file_handle:
            tmp_filename = file_handle.name
            AccessoryEncoder().persist(file_handle, state)
        os.replace(tmp_filename, target)
    finally:
        if tmp_filename is not None and os.path.exists(tmp_filename):
            os.remove(tmp_filename)
