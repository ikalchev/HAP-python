"""Reading and updating the accessory's persisted state file (``accessory.state``)."""

from __future__ import annotations

import os
import tempfile
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from ..const import HAP_PERMISSIONS
from ..encoder import AccessoryEncoder
from ..state import State
from .identity import ClientIdentity


def load_accessory_state(path: str | os.PathLike[str]) -> State:
    """Load an accessory state file into a :class:`pyhap.state.State`, without a driver.

    The state file is the ``AccessoryDriver``'s ``accessory.state``. Note it
    contains the accessory's *private* key — handle it accordingly.

    :raise OSError: If the file does not exist or cannot be read.
    :raise ValueError: If the file is malformed.
    """
    target = os.path.expanduser(os.fspath(path))
    # Placeholder address; load_into overwrites all persisted fields.
    state = State(address="127.0.0.1")
    try:
        with open(target, encoding="utf-8") as file_handle:
            AccessoryEncoder().load_into(file_handle, state)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Not an accessory state file: {target}") from error
    return state


def save_accessory_state(path: str | os.PathLike[str], state: State) -> None:
    """Write ``state`` to its state file, atomically and 0600.

    A tempfile created with 0600 permissions in the same directory is swapped
    in with ``os.replace``, like the driver's own persist.
    """
    target = os.path.expanduser(os.fspath(path))
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


def load_accessory_ltpk(path: str | os.PathLike[str]) -> bytes:
    """Return the accessory's raw long-term public key (LTPK) from its state file.

    Use this to move just the accessory's public key to another machine, e.g.
    where a client runs: the state file also contains the accessory's
    *private* key, so prefer exporting the public key alone over copying the
    whole file.

    :raise ValueError: If the file is not an accessory state file.
    :raise OSError: If the file does not exist or cannot be read.
    """
    state = load_accessory_state(path)
    return state.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def add_client_pairing(
    path: str | os.PathLike[str],
    client: ClientIdentity | str | UUID,
    client_public_key: bytes | None = None,
    *,
    permissions: bytes = HAP_PERMISSIONS.ADMIN,
) -> None:
    """Register a client as paired in an accessory state file, offline.

    Loads the state file with :meth:`pyhap.encoder.AccessoryEncoder.load_into`,
    adds the client via :meth:`pyhap.state.State.add_paired_client`, and writes
    the file back atomically (0600 tempfile swapped in with ``os.replace``).
    No driver needs to run.

    Do this with the accessory **stopped**: a running driver keeps its state
    in memory and would overwrite this edit on its next persist. (While the
    driver runs, ``AccessoryDriver.pair(...)`` does the same in-process.)
    After the accessory restarts with the updated file, the client can
    pair-verify against it.

    :param path: The accessory's ``accessory.state`` file.
    :param client: A :class:`ClientIdentity`, or a client UUID (str/UUID) which
        then requires ``client_public_key``.
    :param client_public_key: The client's raw long-term public key, when
        ``client`` is a UUID.
    :param permissions: The pairing's permissions, admin by default.
    :raise OSError: If the file does not exist or cannot be read.
    :raise ValueError: If the file is malformed, the UUID is invalid or the
        public key is not a valid Ed25519 key.
    """
    if isinstance(client, ClientIdentity):
        client_uuid = UUID(client.uuid)
        public_key = client.public_key_bytes
    else:
        client_uuid = UUID(str(client))
        if client_public_key is None:
            raise ValueError(
                "client_public_key is required when client is not a ClientIdentity"
            )
        public_key = client_public_key
    ed25519.Ed25519PublicKey.from_public_bytes(public_key)  # validate the key
    state = load_accessory_state(path)
    state.add_paired_client(str(client_uuid).encode("utf-8"), public_key, permissions)
    save_accessory_state(path, state)


def remove_client_pairing(
    path: str | os.PathLike[str], client_uuid: str | UUID
) -> None:
    """Remove a paired client from an accessory state file, offline.

    Same offline caveat as :func:`add_client_pairing`. Note that
    :meth:`pyhap.state.State.remove_paired_client` removes *all* pairings when
    the last admin is removed ("new home" semantics) — check the paired clients
    first if that matters.

    :raise OSError: If the file does not exist or cannot be read.
    :raise ValueError: If the UUID is invalid or not paired in the file.
    """
    uuid_obj = UUID(str(client_uuid))
    state = load_accessory_state(path)
    if uuid_obj not in state.paired_clients:
        raise ValueError(f"{uuid_obj} is not a paired client in {path}")
    state.remove_paired_client(uuid_obj)
    save_accessory_state(path, state)
