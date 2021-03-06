"""Tests for the HAPServerHandler."""


from unittest.mock import patch
from uuid import UUID

import pytest

from pyhap import hap_handler
from pyhap.accessory import Accessory, Bridge
from pyhap.characteristic import CharacteristicError
import pyhap.tlv as tlv

CLIENT_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")
PUBLIC_KEY = b"\x99\x98d%\x8c\xf6h\x06\xfa\x85\x9f\x90\x82\xf2\xe8\x18\x9f\xf8\xc75\x1f>~\xc32\xc1OC\x13\xbfH\xad"


def test_response():
    """Test object creation of HAPResponse."""
    response = hap_handler.HAPResponse()
    assert response.status_code == 500
    assert "500" in str(response)


def test_list_pairings_unencrypted(driver):
    """Verify an unencrypted list pairings request fails."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE, hap_handler.HAP_TLV_STATES.M5
    )
    handler.handle_pairings()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }


def test_list_pairings(driver):
    """Verify an encrypted list pairings request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE, hap_handler.HAP_TLV_STATES.M5
    )
    handler.handle_pairings()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2,
        hap_handler.HAP_TLV_TAGS.USERNAME: str(CLIENT_UUID).encode("utf8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY: PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS: hap_handler.HAP_PERMISSIONS.ADMIN,
    }


def test_add_pairing(driver):
    """Verify an encrypted add pairing request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.USERNAME,
        str(CLIENT_UUID).encode("utf-8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS,
        hap_handler.HAP_PERMISSIONS.ADMIN,
    )
    assert driver.state.paired is False

    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
    }
    assert driver.state.paired is True
    assert CLIENT_UUID in driver.state.paired_clients


def test_remove_pairing(driver):
    """Verify an encrypted remove pairing request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert driver.state.paired is True
    assert CLIENT_UUID in driver.state.paired_clients

    for _ in range(2):
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.request_body = tlv.encode(
            hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
            hap_handler.HAP_TLV_STATES.M4,
            hap_handler.HAP_TLV_TAGS.USERNAME,
            str(CLIENT_UUID).encode("utf-8"),
            hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
            PUBLIC_KEY,
        )
        handler.handle_pairings()
        assert tlv.decode(response.body) == {
            hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
        }
        assert CLIENT_UUID not in driver.state.paired_clients
        assert driver.state.paired is False


def test_invalid_pairings_request(driver):
    """Verify an encrypted invalid pairings request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE, hap_handler.HAP_TLV_STATES.M6
    )

    with pytest.raises(ValueError):
        handler.handle_pairings()


def test_pair_verify_one(driver):
    """Verify an unencrypted pair verify one."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M1,
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
    )
    handler.handle_pair_verify()

    tlv_objects = tlv.decode(response.body)

    assert (
        tlv_objects[hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM]
        == hap_handler.HAP_TLV_STATES.M2
    )


def test_pair_verify_one_not_paired(driver):
    """Verify an unencrypted pair verify one."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M1,
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
    )
    handler.handle_pair_verify()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }


def test_pair_verify_two_invaild_state(driver):
    """Verify an unencrypted pair verify two."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M1,
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
    )
    handler.handle_pair_verify()

    tlv_objects = tlv.decode(response.body)

    assert (
        tlv_objects[hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM]
        == hap_handler.HAP_TLV_STATES.M2
    )

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.ENCRYPTED_DATA,
        b"invalid",
    )
    handler.handle_pair_verify()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M4,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }


def test_invalid_pairing_request(driver):
    """Verify an unencrypted pair verify with an invalid sequence fails."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M6,
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
    )
    with pytest.raises(ValueError):
        handler.handle_pair_verify()


def test_handle_set_handle_set_characteristics_unencrypted(driver):
    """Verify an unencrypted set_characteristics."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    handler.handle_set_characteristics()

    assert response.status_code == 401


def test_handle_set_handle_set_characteristics_encrypted(driver):
    """Verify an encrypted set_characteristics."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    handler.handle_set_characteristics()

    assert response.status_code == 204
    assert response.body == b""


def test_handle_set_handle_set_characteristics_encrypted_with_exception(driver):
    """Verify an encrypted set_characteristics."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1

    def _mock_failure(*_):
        raise ValueError

    service = acc.driver.loader.get_service("GarageDoorOpener")
    service.setter_callback = _mock_failure
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":9,"value":1}]}'
    handler.handle_set_characteristics()

    assert response.status_code == 207
    assert b"-70402" in response.body


def test_handle_snapshot_encrypted_non_existant_accessory(driver):
    """Verify an encrypted snapshot with non-existant accessory."""
    bridge = Bridge(driver, "Test Bridge")
    driver.add_accessory(bridge)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"image-height":360,"resource-type":"image","image-width":640,"aid":1411620844}'
    with pytest.raises(ValueError):
        handler.handle_resource()


def test_attempt_to_pair_when_already_paired(driver):
    """Verify we respond with unavailable if already paired."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    driver.pair(
        CLIENT_UUID,
        PUBLIC_KEY,
    )

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M1,
    )
    handler.handle_pairing()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.UNAVAILABLE,
    }


def test_handle_get_characteristics_encrypted(driver):
    """Verify an encrypted get_characteristics."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.path = "/characteristics?id=1.9"
    handler.handle_get_characteristics()

    assert response.status_code == 207
    assert b'"value": 0' in response.body

    with patch.object(acc.iid_manager, "get_obj", side_effect=CharacteristicError):
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.path = "/characteristics?id=1.9"
        handler.handle_get_characteristics()

    assert response.status_code == 207
    assert b"-70402" in response.body


def test_invalid_pairing_two(driver):
    """Verify we respond with error with invalid request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.ENCRYPTED_DATA,
        b"",
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        b"",
        hap_handler.HAP_TLV_TAGS.PASSWORD_PROOF,
        b"",
    )
    handler.accessory_handler.setup_srp_verifier()
    handler.handle_pairing()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M4,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }


def test_invalid_pairing_three(driver):
    """Verify we respond with error with invalid request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM,
        hap_handler.HAP_TLV_STATES.M5,
        hap_handler.HAP_TLV_TAGS.ENCRYPTED_DATA,
        b"",
    )
    handler.accessory_handler.setup_srp_verifier()
    handler.accessory_handler.srp_verifier.set_A(b"")
    handler.handle_pairing()

    tlv_objects = tlv.decode(response.body)

    assert tlv_objects == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M6,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }
