"""Tests for the HAPServerHandler."""


import json
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import UUID

import pytest

from pyhap import hap_handler, tlv
from pyhap.accessory import Accessory, Bridge
from pyhap.characteristic import CharacteristicError
from pyhap.const import HAP_PERMISSIONS

CLIENT_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")
CLIENT2_UUID = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c2")

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
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
    handler.client_uuid = CLIENT_UUID
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
        hap_handler.HAP_TLV_TAGS.USERNAME: str(CLIENT_UUID).encode("utf8").upper(),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY: PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS: hap_handler.HAP_PERMISSIONS.ADMIN,
    }


def test_add_pairing_admin(driver):
    """Verify an encrypted add pairing request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    handler.client_uuid = CLIENT_UUID
    assert driver.state.paired is False
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.USERNAME,
        str(CLIENT2_UUID).encode("utf-8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS,
        hap_handler.HAP_PERMISSIONS.ADMIN,
    )
    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
    }
    assert driver.state.paired is True
    assert CLIENT2_UUID in driver.state.paired_clients
    assert driver.state.is_admin(CLIENT2_UUID)


def test_add_pairing_user(driver):
    """Verify an encrypted add pairing request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    handler.client_uuid = CLIENT_UUID
    assert driver.state.paired is False
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.USERNAME,
        str(CLIENT2_UUID).encode("utf-8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS,
        hap_handler.HAP_PERMISSIONS.USER,
    )
    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
    }
    assert driver.state.paired is True
    assert CLIENT2_UUID in driver.state.paired_clients
    assert not driver.state.is_admin(CLIENT2_UUID)

    # Verify upgrade to admin
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.USERNAME,
        str(CLIENT2_UUID).encode("utf-8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS,
        hap_handler.HAP_PERMISSIONS.ADMIN,
    )
    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
    }
    assert driver.state.paired is True
    assert CLIENT2_UUID in driver.state.paired_clients
    assert driver.state.is_admin(CLIENT2_UUID)

    # Verify downgrade to normal user
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
        hap_handler.HAP_TLV_STATES.M3,
        hap_handler.HAP_TLV_TAGS.USERNAME,
        str(CLIENT2_UUID).encode("utf-8"),
        hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
        PUBLIC_KEY,
        hap_handler.HAP_TLV_TAGS.PERMISSIONS,
        hap_handler.HAP_PERMISSIONS.USER,
    )
    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
    }
    assert driver.state.paired is True
    assert CLIENT2_UUID in driver.state.paired_clients
    assert not driver.state.is_admin(CLIENT2_UUID)


def test_remove_pairing(driver):
    """Verify an encrypted remove pairing request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    handler.client_uuid = CLIENT_UUID

    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
    driver.pair(CLIENT2_UUID, PUBLIC_KEY, HAP_PERMISSIONS.USER)

    assert driver.state.paired is True
    assert CLIENT_UUID in driver.state.paired_clients

    for _ in range(2):
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.request_body = tlv.encode(
            hap_handler.HAP_TLV_TAGS.REQUEST_TYPE,
            hap_handler.HAP_TLV_STATES.M4,
            hap_handler.HAP_TLV_TAGS.USERNAME,
            str(CLIENT2_UUID).encode("utf-8"),
            hap_handler.HAP_TLV_TAGS.PUBLIC_KEY,
            PUBLIC_KEY,
        )
        handler.handle_pairings()
        assert tlv.decode(response.body) == {
            hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2
        }
        assert CLIENT2_UUID not in driver.state.paired_clients
        assert driver.state.paired is True

    # Now remove the last admin
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


def test_non_admin_pairings_request(driver):
    """Verify only admins can access pairings."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    handler.client_uuid = CLIENT_UUID

    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.USER)
    assert CLIENT_UUID in driver.state.paired_clients

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = tlv.encode(
        hap_handler.HAP_TLV_TAGS.REQUEST_TYPE, hap_handler.HAP_TLV_STATES.M5
    )

    handler.handle_pairings()
    assert tlv.decode(response.body) == {
        hap_handler.HAP_TLV_TAGS.SEQUENCE_NUM: hap_handler.HAP_TLV_STATES.M2,
        hap_handler.HAP_TLV_TAGS.ERROR_CODE: hap_handler.HAP_TLV_ERRORS.AUTHENTICATION,
    }


def test_invalid_pairings_request(driver):
    """Verify an encrypted invalid pairings request."""
    driver.add_accessory(Accessory(driver, "TestAcc"))

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True
    handler.client_uuid = CLIENT_UUID

    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)
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
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":10,"ev":true}]}'
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
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":10,"ev":true}]}'
    handler.handle_set_characteristics()

    assert response.status_code == 204
    assert response.body == b""


def test_handle_set_handle_set_characteristics_encrypted_pid_missing_prepare(driver):
    """Verify an encrypted set_characteristics with a missing prepare."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = (
        b'{"pid":123,"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    )
    handler.handle_set_characteristics()

    assert response.status_code == 207
    assert b"-70410" in response.body


def test_handle_set_handle_set_characteristics_encrypted_with_prepare(driver):
    """Verify an encrypted set_characteristics with a prepare."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":5000}'
    handler.handle_prepare()

    assert response.status_code == 200
    assert response.body == b'{"status":0}'

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = (
        b'{"pid":123,"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    )
    handler.handle_set_characteristics()

    assert response.status_code == 204
    assert response.body == b""


def test_handle_set_handle_set_characteristics_encrypted_with_multiple_prepare(driver):
    """Verify an encrypted set_characteristics with multiple prepares."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":0}'
    handler.handle_prepare()

    # Second prepare should overwrite the first
    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":5000}'
    handler.handle_prepare()

    assert response.status_code == 200
    assert response.body == b'{"status":0}'

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = (
        b'{"pid":123,"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    )
    handler.handle_set_characteristics()

    assert response.status_code == 204
    assert response.body == b""


def test_handle_set_handle_encrypted_with_invalid_prepare(driver):
    """Verify an encrypted set_characteristics with a prepare missing the ttl."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123}'
    handler.handle_prepare()

    assert response.status_code == 200
    assert response.body == b'{"status":-70410}'


def test_handle_set_handle_set_characteristics_encrypted_with_expired_ttl(driver):
    """Verify an encrypted set_characteristics with a prepare expired."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":0}'
    handler.handle_prepare()

    assert response.status_code == 200
    assert response.body == b'{"status":0}'

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = (
        b'{"pid":123,"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    )
    handler.handle_set_characteristics()

    assert response.status_code == 207
    assert b"-70410" in response.body


def test_handle_set_handle_set_characteristics_encrypted_with_wrong_pid(driver):
    """Verify an encrypted set_characteristics with wrong pid."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = True

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":5000}'
    handler.handle_prepare()

    assert response.status_code == 200
    assert response.body == b'{"status":0}'

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = (
        b'{"pid":456,"characteristics":[{"aid":1,"iid":9,"ev":true}]}'
    )
    handler.handle_set_characteristics()

    assert response.status_code == 207
    assert b"-70410" in response.body


def test_handle_set_handle_prepare_not_encrypted(driver):
    """Verify an non-encrypted set_characteristics with a prepare."""
    acc = Accessory(driver, "TestAcc", aid=1)
    assert acc.aid == 1
    service = acc.driver.loader.get_service("GarageDoorOpener")
    acc.add_service(service)
    driver.add_accessory(acc)

    handler = hap_handler.HAPServerHandler(driver, "peername")
    handler.is_encrypted = False

    response = hap_handler.HAPResponse()
    handler.response = response
    handler.request_body = b'{"pid":123,"ttl":5000}'
    handler.handle_prepare()

    assert response.status_code == 401


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
    handler.request_body = b'{"characteristics":[{"aid":1,"iid":11,"value":1}]}'
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
    driver.pair(CLIENT_UUID, PUBLIC_KEY, HAP_PERMISSIONS.ADMIN)

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
    handler.path = "/characteristics?id=1.11"
    handler.parsed_url = urlparse(handler.path)

    handler.handle_get_characteristics()

    assert response.status_code == 200
    decoded_response = json.loads(response.body.decode())
    assert "characteristics" in decoded_response
    assert "status" not in decoded_response["characteristics"][0]
    assert b'"value":0' in response.body

    with patch.object(acc.iid_manager, "get_obj", side_effect=CharacteristicError):
        response = hap_handler.HAPResponse()
        handler.response = response
        handler.path = "/characteristics?id=1.10"
        handler.handle_get_characteristics()

    assert response.status_code == 207
    decoded_response = json.loads(response.body.decode())
    assert "characteristics" in decoded_response
    assert "status" in decoded_response["characteristics"][0]
    assert decoded_response["characteristics"][0]["status"] == -70402


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
