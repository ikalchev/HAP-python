"""
Tests for pyhap.encoder
"""
import tempfile
import uuid

import pytest
import ed25519

from pyhap.util import generate_mac, tohex
from pyhap.accessory import Accessory
import pyhap.encoder as encoder


class TestAccessoryEncoder(object):
    """
    Tests for AccessoryEncoder.
    """

    def test_persist_and_load(self):
        """Stores an Accessory and then loads the stored state into another
        Accessory. Tests if the two accessories have the same property values.
        """
        mac = generate_mac()
        _pk, sample_client_pk = ed25519.create_keypair()
        acc = Accessory("Test Accessory", mac=mac)
        acc.add_paired_client(uuid.uuid1(),
                              sample_client_pk.to_bytes())

        acc_loaded = Accessory("Loaded Test Accessory")
        acc_loaded.config_version += 2  # change the default state.
        enc = encoder.AccessoryEncoder()
        with tempfile.TemporaryFile(mode="r+") as fp:
            enc.persist(fp, acc)
            fp.seek(0)
            enc.load_into(fp, acc_loaded)

        assert acc.mac == acc_loaded.mac
        assert acc.private_key == acc_loaded.private_key
        assert acc.public_key == acc_loaded.public_key
        assert acc.config_version == acc_loaded.config_version
        assert acc.paired_clients == acc_loaded.paired_clients
