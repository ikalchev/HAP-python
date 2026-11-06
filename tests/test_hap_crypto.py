"""Tests for the HAPCrypto."""

from cryptography.exceptions import InvalidTag
import pytest

from pyhap import hap_crypto


def test_accessory_client_round_trip():
    """Test an accessory and a client-side instance round trip both directions."""
    plaintext = b"bobdata1232" * 1000
    key = b"mykeydsfdsfdsfsdfdsfsdf"

    accessory = hap_crypto.HAPCrypto(key)
    client = hap_crypto.HAPCrypto(key, client_side=True)

    # Client -> accessory direction
    encrypted = bytes(b"".join(client.encrypt(plaintext)))
    accessory.receive_data(encrypted)
    assert accessory.decrypt() == plaintext

    # Accessory -> client direction
    encrypted = bytes(b"".join(accessory.encrypt(plaintext)))
    client.receive_data(encrypted)
    assert client.decrypt() == plaintext


def test_client_side_swaps_cipher_direction():
    """Test client_side instances use the opposite cipher keys of the default."""
    plaintext = b"data"
    key = b"mykeydsfdsfdsfsdfdsfsdf"

    # Two instances on the same side cannot decrypt each other, as they
    # encrypt with the outgoing key and expect the incoming key.
    encrypted = bytes(b"".join(hap_crypto.HAPCrypto(key).encrypt(plaintext)))
    accessory_b = hap_crypto.HAPCrypto(key)
    accessory_b.receive_data(encrypted)
    with pytest.raises(InvalidTag):
        accessory_b.decrypt()

    encrypted = bytes(
        b"".join(hap_crypto.HAPCrypto(key, client_side=True).encrypt(plaintext))
    )
    client_b = hap_crypto.HAPCrypto(key, client_side=True)
    client_b.receive_data(encrypted)
    with pytest.raises(InvalidTag):
        client_b.decrypt()

    # An accessory and a client_side instance derive complementary keys,
    # so they can talk to each other (both directions are covered by
    # test_accessory_client_round_trip).
    accessory_a = hap_crypto.HAPCrypto(key)
    client_a = hap_crypto.HAPCrypto(key, client_side=True)
    encrypted = bytes(b"".join(accessory_a.encrypt(plaintext)))
    client_a.receive_data(encrypted)
    assert client_a.decrypt() == plaintext


def test_round_trip():
    """Test we can roundtrip data by using the same cipher info."""
    plaintext = b"bobdata1232" * 1000
    key = b"mykeydsfdsfdsfsdfdsfsdf"

    crypto = hap_crypto.HAPCrypto(key)
    # Switch the cipher info to the same to allow
    # round trip
    crypto.OUT_CIPHER_INFO = crypto.IN_CIPHER_INFO
    crypto.reset(key)

    encrypted = bytearray(b"".join(crypto.encrypt(plaintext)))

    # Receive no data
    assert crypto.decrypt() == b""

    # Receive not a whole block
    crypto.receive_data(bytes(encrypted[:50]))
    assert crypto.decrypt() == b""

    del encrypted[:50]
    # Receive the rest of the block
    crypto.receive_data(bytes(encrypted))

    decrypted = crypto.decrypt()

    assert decrypted == plaintext
