"""Command line interface for offline controller management.

Run as ``python -m pyhap.client <command> --help``. The subcommands create
persistent controller identities and register/deregister them in an
accessory's state file without a running driver, e.g. to run the client on a
different machine than the accessory.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
import sys
from typing import Any
from uuid import UUID

from ..const import CLIENT_PROP_PERMS, HAP_PERMISSIONS
from .identity import ClientIdentity
from .state import (
    add_client_pairing,
    load_accessory_state,
    remove_client_pairing,
)

_RUNNING_ACCESSORY_WARNING = (
    "Note: if the accessory is currently running, stop it and start it again "
    "so it loads the updated state file - a running driver may overwrite this "
    "change on its next persist (while it runs, use AccessoryDriver.pair() / "
    "AccessoryDriver.unpair() in-process instead)."
)


def _permissions_name(perms: int) -> str:
    """The human-readable name of a pairing's permission byte."""
    return "admin" if perms & 0x01 else "user"


def _permissions_bytes(name: Any) -> bytes:
    """The wire permission byte for an "admin"/"user" name."""
    if name == "admin":
        return HAP_PERMISSIONS.ADMIN
    if name == "user":
        return HAP_PERMISSIONS.USER
    raise ValueError(f"unknown permissions {name!r} (choose 'admin' or 'user')")


def _cmd_new(args: argparse.Namespace) -> int:
    """Create a new persistent controller identity file."""
    path = os.path.expanduser(args.file)
    if os.path.exists(path) and not args.force:
        raise ValueError(f"{path} already exists (use --force to overwrite it)")
    print(f'Creating new HAP controller in file "{path}"...')
    identity = ClientIdentity.generate()
    identity.save(path)
    print("Done!")
    print()
    print(
        "On the machine where your accessory is running create a file "
        "(e.g. client.json) with the following contents:"
    )
    print()
    print(
        json.dumps(
            {
                "uuid": identity.uuid,
                "public_key": identity.public_key_bytes.hex(),
            },
            indent=4,
        )
    )
    print()
    print(
        "and run `python -m pyhap.client register -f client.json "
        "-a <accessory state file>`."
    )
    print()
    print(
        "Alternatively, check the pyhap.client documentation on how to work "
        "directly with the accessory state file instead."
    )
    print()
    print(
        "Once the new HAP controller is registered, it will be able to "
        "connect and control the accessory."
    )
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    """Register a controller in an accessory state file."""
    register_path = os.path.expanduser(args.file)
    with open(register_path, encoding="utf-8") as file_handle:
        data = json.load(file_handle)
    try:
        client_uuid = data["uuid"]
        public_key = bytes.fromhex(data["public_key"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"{register_path} is not a controller file (expected 'uuid' and "
            f"'public_key'): {error}"
        ) from error
    permissions = _permissions_bytes(
        args.permissions or data.get("permissions") or "admin"
    )
    add_client_pairing(
        args.accessory_state, client_uuid, public_key, permissions=permissions
    )
    print(
        f"Registered HAP controller {client_uuid} "
        f'({_permissions_name(ord(permissions))}) in "{args.accessory_state}".'
    )
    print(_RUNNING_ACCESSORY_WARNING)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """List paired clients of an accessory state file as ``<uuid>: <permissions>``."""
    state = load_accessory_state(args.accessory_state)
    if not state.paired_clients:
        print("No paired clients.")
        return 0
    for client_uuid in sorted(state.paired_clients):
        perms = state.client_properties.get(client_uuid, {}).get(CLIENT_PROP_PERMS, 0)
        print(f"{client_uuid}: {_permissions_name(perms)}")
    return 0


def _cmd_deregister(args: argparse.Namespace) -> int:
    """Remove a paired client from an accessory state file, guarded by --yes."""
    uuid_obj = UUID(args.uuid)
    state = load_accessory_state(args.accessory_state)
    if uuid_obj not in state.paired_clients:
        raise ValueError(f"{uuid_obj} is not a paired client in {args.accessory_state}")
    is_admin = state.is_admin(uuid_obj)
    other_admins = any(
        state.is_admin(other) for other in state.paired_clients if other != uuid_obj
    )
    if (is_admin or len(state.paired_clients) == 1) and not args.yes:
        reason = (
            "removing the last admin removes ALL pairings"
            if is_admin and not other_admins
            else "removing an admin pairing"
        )
        raise ValueError(f"refusing to deregister {uuid_obj} without --yes ({reason})")
    remove_client_pairing(args.accessory_state, uuid_obj)
    print(f'Deregistered HAP controller {uuid_obj} from "{args.accessory_state}".')
    print(_RUNNING_ACCESSORY_WARNING)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m pyhap.client",
        description=(
            "Create HAP controller identities and manage accessory pairings "
            "offline (no driver needed)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser(
        "new", help="create a new persistent HAP controller identity file"
    )
    new_parser.add_argument(
        "-f",
        "--file",
        required=True,
        help="controller file to create; stays on this machine (holds the private key)",
    )
    new_parser.add_argument(
        "--force", action="store_true", help="overwrite the file if it exists"
    )
    new_parser.set_defaults(func=_cmd_new)

    register_parser = subparsers.add_parser(
        "register", help="register a controller in an accessory state file"
    )
    register_parser.add_argument(
        "-f",
        "--file",
        required=True,
        help="the JSON file printed by 'new', or a full client.state",
    )
    register_parser.add_argument(
        "-a",
        "--accessory-state",
        required=True,
        help="the accessory's accessory.state file",
    )
    register_parser.add_argument(
        "-p",
        "--permissions",
        choices=["admin", "user"],
        default=None,
        help="pairing permissions (default: admin)",
    )
    register_parser.set_defaults(func=_cmd_register)

    list_parser = subparsers.add_parser(
        "list", help="list the paired clients of an accessory state file"
    )
    list_parser.add_argument("-a", "--accessory-state", required=True)
    list_parser.set_defaults(func=_cmd_list)

    deregister_parser = subparsers.add_parser(
        "deregister", help="remove a paired client from an accessory state file"
    )
    deregister_parser.add_argument("-a", "--accessory-state", required=True)
    deregister_parser.add_argument("uuid", help="the client UUID to remove")
    deregister_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm removing an admin or the last remaining pairing",
    )
    deregister_parser.set_defaults(func=_cmd_deregister)

    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
