"""Tests for the HAPCrypto."""


from pyhap import hap_crypto


def test_round_trip():
    """Test we can roundtrip data by using the same cipher info."""
    plaintext = b"bobdata1232" * 1000
    key = b"mykeydsfdsfdsfsdfdsfsdf"

    crypto = hap_crypto.HAPCrypto(key)
    # Switch the cipher info to the same to allow
    # round trip
    crypto.OUT_CIPHER_INFO = crypto.IN_CIPHER_INFO
    crypto.reset(key)

    encrypted = bytearray(crypto.encrypt(plaintext))

    # Receive no data
    assert crypto.decrypt() == b""

    # Receive not a whole block
    crypto.receive_data(encrypted[:50])
    assert crypto.decrypt() == b""

    del encrypted[:50]
    # Receive the rest of the block
    crypto.receive_data(encrypted)

    decrypted = crypto.decrypt()

    assert decrypted == plaintext
