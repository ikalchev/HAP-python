"""The mDNS advertisement of an accessory.

When the driver starts (and whenever the configuration version or pairing
state changes), it registers an :class:`AccessoryMDNSServiceInfo` with
zeroconf so HomeKit controllers can discover the accessory on the network.
The advertisement carries the accessory's identity - service name, host
name, port, addresses - and the TXT record data (``c#``, ``sf``, ``sh``,
...) that tells a controller whether to re-fetch the accessory's
description.
"""

import base64
import hashlib
import re
from typing import Any

from zeroconf import ServiceInfo

from pyhap.const import HAP_PROTOCOL_SHORT_VERSION
from pyhap.state import State

HAP_SERVICE_TYPE = "_hap._tcp.local."
VALID_MDNS_REGEX = re.compile(r"[^A-Za-z0-9\-]+")
LEADING_TRAILING_SPACE_DASH = re.compile(r"^[ -]+|[ -]+$")
DASH_REGEX = re.compile(r"[-]+")


class AccessoryMDNSServiceInfo(ServiceInfo):
    """A mDNS service info representation of an accessory.

    Carries the accessory's identity (name, host, MAC-based setup hash) and
    the advertisement data HomeKit reads to decide whether to (re-)discover
    and (re-)fetch the accessory: the configuration version ``c#``, the
    pairing state ``sf`` and the setup hash ``sh``.
    """

    def __init__(
        self, accessory: Any, state: State, zeroconf_server: Any = None
    ) -> None:
        """Build the service info from the accessory and its persisted state.

        :param accessory: The accessory to advertise.
        :param state: The driver's state, source of the port, MAC, addresses
            and advertisement flags.
        :param zeroconf_server: The server (host) name to advertise. Defaults
            to one derived from the accessory's display name and MAC.
        """
        self.accessory = accessory
        self.state: State = state

        adv_data = self._get_advert_data()
        valid_name = self._valid_name()
        short_mac = self.state.mac[-8:].replace(":", "")
        # Append part of MAC address to prevent name conflicts
        name = f"{valid_name} {short_mac}.{HAP_SERVICE_TYPE}"
        valid_host_name = self._valid_host_name()
        server = zeroconf_server or f"{valid_host_name}-{short_mac}.local."
        super().__init__(
            HAP_SERVICE_TYPE,
            name=name,
            server=server,
            port=self.state.port,
            weight=0,
            priority=0,
            properties=adv_data,
            parsed_addresses=self.state.addresses,
        )

    def _valid_name(self) -> str:
        """The accessory's display name, sanitized for the mDNS service name."""
        return re.sub(
            LEADING_TRAILING_SPACE_DASH,
            "",
            re.sub(VALID_MDNS_REGEX, " ", self.accessory.display_name),
        )

    def _valid_host_name(self) -> str:
        """The display name as a DNS-safe host name (no leading/trailing dashes)."""
        return re.sub(
            DASH_REGEX,
            "-",
            re.sub(VALID_MDNS_REGEX, " ", self.accessory.display_name)
            .strip()
            .replace(" ", "-")
            .strip("-"),
        )

    def _setup_hash(self) -> str:
        """The base64 setup hash advertised as ``sh``, from setup id and MAC."""
        setup_hash_material = self.state.setup_id + self.state.mac
        temp_hash = hashlib.sha512()
        temp_hash.update(setup_hash_material.encode())
        return base64.b64encode(temp_hash.digest()[:4]).decode()

    def _get_advert_data(self) -> dict[str, str]:
        """Generate the mDNS TXT advertisement data from the accessory and state.

        The keys are the HAP advertisement fields: ``md`` (model name),
        ``pv`` (protocol version), ``id`` (MAC), ``c#`` (configuration
        version), ``s#`` (state), ``ff`` (flags), ``ci`` (category), ``sf``
        (discoverable while unpaired) and ``sh`` (setup hash).
        """
        return {
            "md": self._valid_name(),
            "pv": HAP_PROTOCOL_SHORT_VERSION,
            "id": self.state.mac,
            # represents the 'configuration version' of an Accessory.
            # Increasing this 'version number' signals iOS devices to
            # re-fetch accessories data.
            "c#": str(self.state.config_version),
            "s#": "1",  # 'accessory state'
            "ff": "0",
            "ci": str(self.accessory.category),
            # 'sf == 1' means "discoverable by HomeKit iOS clients"
            "sf": "0" if self.state.paired else "1",
            "sh": self._setup_hash(),
        }
