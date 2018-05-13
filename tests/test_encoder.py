"""Tests for pyhap.encoder."""
import tempfile
import uuid

import ed25519

from pyhap.util import generate_mac
from pyhap.config import Config
import pyhap.encoder as encoder


class TestAccessoryEncoder(object):
    """Tests for AccessoryEncoder."""

    def test_persist_and_load(self):
        """Stores an Accessory and then loads the stored state into another
        Accessory. Tests if the two accessories have the same property values.
        """
        mac = generate_mac()
        _pk, sample_client_pk = ed25519.create_keypair()
        config = Config(loop=None, mac=mac)
        config.add_paired_client(uuid.uuid1(), sample_client_pk.to_bytes())

        config_loaded = Config(loop=None)
        config_loaded.config_version += 2  # change the default state.
        enc = encoder.AccessoryEncoder()
        with tempfile.TemporaryFile(mode="r+") as fp:
            enc.persist(fp, config)
            fp.seek(0)
            enc.load_into(fp, config_loaded)

        assert config.mac == config_loaded.mac
        assert config.private_key == config_loaded.private_key
        assert config.public_key == config_loaded.public_key
        assert config.config_version == config_loaded.config_version
        assert config.paired_clients == config_loaded.paired_clients
