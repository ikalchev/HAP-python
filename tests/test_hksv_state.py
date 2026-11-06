"""Tests for persisting the HKSV recording state across restarts."""

import asyncio
import io
import json
from unittest.mock import MagicMock, patch
import uuid

import pytest

from pyhap import hksv_recording as hr
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.encoder import AccessoryEncoder
from pyhap.state import State
from pyhap.util import base64_to_bytes, to_base64_str

CAMERA_AID = 2
SELECTED_CHAR = ("CameraRecordingManagement", "SelectedCameraRecordingConfiguration")

SELECTED = to_base64_str(
    hr.SelectedRecordingConfiguration(
        recording=hr.SupportedRecordingConfiguration(
            prebuffer_length_ms=4000, event_triggers=hr.EventTrigger.MOTION
        ),
        video=hr.VideoCodecConfiguration(
            codec_type=hr.VideoCodecType.H264,
            profile=0,
            level=0,
            bitrate_kbps=2000,
            iframe_interval_ms=4000,
            attributes=[hr.VideoAttributes(1920, 1080, 30)],
        ),
        audio=hr.AudioCodecConfiguration(),
    ).encode()
)

# Values that differ from the defaults, as the hub would write them.
WRITES = {
    ("CameraRecordingManagement", "Active"): 1,
    ("CameraRecordingManagement", "RecordingAudioActive"): 1,
    ("CameraOperatingMode", "HomeKitCameraActive"): 0,
    ("CameraOperatingMode", "EventSnapshotsActive"): 0,
    ("CameraOperatingMode", "PeriodicSnapshotsActive"): 0,
    SELECTED_CHAR: SELECTED,
}
DEFAULTS = {
    ("CameraRecordingManagement", "Active"): 0,
    ("CameraRecordingManagement", "RecordingAudioActive"): 0,
    ("CameraOperatingMode", "HomeKitCameraActive"): 1,
    ("CameraOperatingMode", "EventSnapshotsActive"): 1,
    ("CameraOperatingMode", "PeriodicSnapshotsActive"): 1,
    SELECTED_CHAR: "",
}


def _options(resolutions=((1920, 1080, 30),)):
    return {
        "video": {
            "codec": {"profiles": [], "levels": []},
            "resolutions": [list(res) for res in resolutions],
        },
        "audio": {"codecs": [{"type": "OPUS", "samplerate": 24}]},
        "address": "127.0.0.1",
        "recording": True,
    }


@pytest.fixture(name="driver")
async def driver_fixture(tmp_path):
    with patch("pyhap.accessory_driver.AccessoryDriver.async_persist"):
        yield AccessoryDriver(
            loop=asyncio.get_running_loop(),
            listen_address="127.0.0.1",
            persist_file=str(tmp_path / "accessory.state"),
        )


async def _drain():
    # add_job hops through call_soon_threadsafe, then call_soon.
    for _ in range(2):
        await asyncio.sleep(0)


def _char(camera, service, name):
    return camera.get_service(service).get_characteristic(name)


async def _write_recording_state(camera):
    """Simulate the hub enabling recording through HAP writes."""
    for (service, name), value in WRITES.items():
        _char(camera, service, name).client_update_value(value)
    await _drain()


def _assert_state(camera, expected, selected):
    for (service, name), value in expected.items():
        assert _char(camera, service, name).value == value, name
    assert camera.selected_recording_configuration == (
        hr.SelectedRecordingConfiguration.decode(base64_to_bytes(SELECTED))
        if selected
        else None
    )


async def test_hub_burst_is_saved_once(driver):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    camera.recording_state_changed = MagicMock()
    # An app setter must not stop persistence.
    app_setter = MagicMock()
    _char(camera, "CameraRecordingManagement", "Active").setter_callback = app_setter

    for (service, name), value in WRITES.items():
        _char(camera, service, name).client_update_value(value)
    assert driver.async_persist.call_count == 0
    await _drain()

    app_setter.assert_called_once_with(1)
    assert driver.async_persist.call_count == 1
    camera.recording_state_changed.assert_called_once_with()
    stored = driver.state.accessory_states[CAMERA_AID]["recording"]
    assert {name: stored[name] for _, name in WRITES} == {
        name: value for (_, name), value in WRITES.items()
    }

    # An unchanged write saves nothing, a later change is saved again.
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(1)
    await _drain()
    assert driver.async_persist.call_count == 1
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(0)
    await _drain()
    assert driver.async_persist.call_count == 2
    assert driver.state.accessory_states[CAMERA_AID]["recording"]["Active"] == 0


async def test_other_characteristics_are_not_stored(driver):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    camera.set_motion_detected(True)
    # Same name as the recording Active, on the streaming service.
    _char(camera, "CameraRTPStreamManagement", "Active").client_update_value(0)
    await _drain()
    assert driver.async_persist.call_count == 0
    assert not any(driver.state.accessory_states.values())


async def test_camera_rebuilt_before_save_restores_change(driver):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    for (service, name), value in WRITES.items():
        _char(camera, service, name).client_update_value(value)

    rebuilt = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(rebuilt, WRITES, selected=True)


async def test_restore_on_construction(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    saves = driver.async_persist.call_count

    # Same aid, e.g. an accessory swapped in place on reload.
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(camera, WRITES, selected=True)
    # Restoring does not notify, so nothing is stored or saved again.
    await _drain()
    assert driver.async_persist.call_count == saves
    other = Camera(_options(), driver, "Other", aid=CAMERA_AID + 1)
    _assert_state(other, DEFAULTS, selected=False)


async def test_stale_selected_configuration_dropped(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))

    camera = Camera(
        _options(resolutions=((1280, 720, 30),)), driver, "Cam", aid=CAMERA_AID
    )
    _assert_state(camera, {**WRITES, SELECTED_CHAR: ""}, selected=False)


@pytest.mark.parametrize("value", [None, 7, "1", float("inf"), float("nan")])
async def test_invalid_stored_value_keeps_its_default(driver, caplog, value):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    driver.state.accessory_states[CAMERA_AID]["recording"]["Active"] = value

    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    active = ("CameraRecordingManagement", "Active")
    _assert_state(camera, {**WRITES, active: DEFAULTS[active]}, selected=True)
    assert f"Ignoring stored Active of aid {CAMERA_AID}" in caplog.text


async def test_infinity_from_state_file_keeps_default(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    buffer = io.StringIO()
    AccessoryEncoder.persist(buffer, driver.state)
    text = buffer.getvalue().replace('"Active": 1', '"Active": Infinity')
    AccessoryEncoder.load_into(io.StringIO(text), driver.state)
    assert driver.state.accessory_states[CAMERA_AID]["recording"]["Active"] == float(
        "inf"
    )

    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    assert _char(camera, "CameraRecordingManagement", "Active").value == 0


@pytest.mark.parametrize(
    "selected", ["not base64!", to_base64_str(b"\x01\x02\x01"), 123]
)
async def test_invalid_stored_selection_is_dropped(driver, caplog, selected):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    stored = driver.state.accessory_states[CAMERA_AID]["recording"]
    stored["SelectedCameraRecordingConfiguration"] = selected

    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(camera, {**WRITES, SELECTED_CHAR: ""}, selected=False)
    message = f"Ignoring stored recording selection of aid {CAMERA_AID}"
    assert message in caplog.text


async def test_no_selection_restores_without_warning(driver, caplog):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(1)

    Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    assert "Ignoring" not in caplog.text


async def test_restored_selection_survives_a_later_write(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    restored = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _char(restored, "CameraOperatingMode", "HomeKitCameraActive").client_update_value(1)

    rebuilt = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    hk_active = ("CameraOperatingMode", "HomeKitCameraActive")
    _assert_state(rebuilt, {**WRITES, hk_active: 1}, selected=True)


async def test_reset_drops_the_stored_state(driver):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    await _write_recording_state(camera)
    camera.recording_state_changed = MagicMock()
    saves = driver.async_persist.call_count

    camera.reset_accessory_state()
    _assert_state(camera, DEFAULTS, selected=False)
    assert "recording" not in driver.state.accessory_states[CAMERA_AID]
    await _drain()
    assert driver.async_persist.call_count == saves + 1
    camera.recording_state_changed.assert_called_once_with()
    rebuilt = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(rebuilt, DEFAULTS, selected=False)


async def test_rejected_selection_write_is_not_stored(driver):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    await _write_recording_state(camera)
    # pyhap sets the value before the setter rejects it.
    with pytest.raises(KeyError):
        _char(camera, *SELECTED_CHAR).client_update_value(to_base64_str(b"\xff\x00"))
    _char(camera, "CameraOperatingMode", "HomeKitCameraActive").client_update_value(1)

    rebuilt = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    hk_active = ("CameraOperatingMode", "HomeKitCameraActive")
    _assert_state(rebuilt, {**WRITES, hk_active: 1}, selected=True)


async def test_closed_loop_does_not_fail_the_write(driver, caplog):
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    loop = driver.loop
    driver.loop = MagicMock()
    driver.loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(1)
    assert driver.state.accessory_states[CAMERA_AID]["recording"]["Active"] == 1
    assert "Could not schedule saving the recording state" in caplog.text

    # The next change schedules the save again.
    driver.loop = loop
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(0)
    await _drain()
    assert driver.async_persist.call_count == 1


async def test_construction_does_not_call_overridable_restore(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))

    class AppCamera(Camera):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.restored = []

        def restore_accessory_state(self) -> None:
            super().restore_accessory_state()
            self.restored.append(True)

    camera = AppCamera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(camera, WRITES, selected=True)
    camera.restore_accessory_state()
    assert camera.restored == [True]


@pytest.mark.parametrize("stored", [None, [], "x"])
async def test_non_dict_stored_state_is_ignored(driver, stored):
    driver.state.accessory_states[CAMERA_AID] = {"recording": stored}
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(camera, DEFAULTS, selected=False)


async def test_encoder_round_trip(driver):
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=CAMERA_AID))
    buffer = io.StringIO()
    AccessoryEncoder.persist(buffer, driver.state)
    buffer.seek(0)

    state = State()
    AccessoryEncoder.load_into(buffer, state)
    assert state.accessory_states == driver.state.accessory_states


def test_empty_accessory_state_is_not_written(driver):
    assert driver.accessory_state(CAMERA_AID) == {}
    driver.accessory_state(CAMERA_AID + 1)["recording"] = {"Active": 1}
    buffer = io.StringIO()
    AccessoryEncoder.persist(buffer, driver.state)
    stored = json.loads(buffer.getvalue())["accessory_states"]
    assert stored == {str(CAMERA_AID + 1): {"recording": {"Active": 1}}}


@pytest.mark.parametrize("method", ["restore_accessory_state", "reset_accessory_state"])
async def test_bridge_continues_after_failing_accessory(driver, method, caplog):
    bridge = Bridge(driver, "Bridge")
    broken = Camera(_options(), driver, "Broken", aid=CAMERA_AID)
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID + 1)
    bridge.add_accessory(broken)
    bridge.add_accessory(camera)
    setattr(broken, method, MagicMock(side_effect=RuntimeError("boom")))
    setattr(camera, method, MagicMock())

    getattr(bridge, method)()
    getattr(camera, method).assert_called_once_with()
    assert "Broken" in caplog.text
    assert "boom" in caplog.text


@pytest.mark.parametrize(
    "accessory_states",
    [
        "missing",
        None,
        [],
        {
            "x": {"recording": {}},
            "\u00b2": {},
            "2": None,
            "-1": {},
            "3": {"recording": {}},
        },
    ],
)
def test_old_or_damaged_state_file(accessory_states):
    buffer = io.StringIO()
    AccessoryEncoder.persist(buffer, State())
    loaded = json.loads(buffer.getvalue())
    if accessory_states == "missing":
        del loaded["accessory_states"]
    else:
        loaded["accessory_states"] = accessory_states

    state = State()
    state.accessory_states = {CAMERA_AID: {"recording": {"Active": 1}}}
    AccessoryEncoder.load_into(io.StringIO(json.dumps(loaded)), state)
    expected = {3: {"recording": {}}} if isinstance(accessory_states, dict) else {}
    assert state.accessory_states == expected


@pytest.mark.parametrize("bridged", [False, True])
async def test_add_accessory_restores_after_load(driver, bridged):
    aid = CAMERA_AID if bridged else 1
    await _write_recording_state(Camera(_options(), driver, "Cam", aid=aid))
    driver.persist()
    driver.state.accessory_states.clear()

    # Built before the driver loads its state file, as in standalone use.
    camera = Camera(_options(), driver, "Cam")
    if bridged:
        bridge = Bridge(driver, "Bridge")
        bridge.add_accessory(camera)
        driver.add_accessory(bridge)
    else:
        driver.add_accessory(camera)
    assert camera.aid == aid
    _assert_state(camera, WRITES, selected=True)


@pytest.mark.parametrize("bridged", [False, True])
async def test_last_unpair_resets_recording_state(driver, bridged):
    clients = [uuid.uuid4(), uuid.uuid4()]
    for client in clients:
        driver.state.add_paired_client(str(client).encode(), b"\x00" * 32, b"\x01")
    camera = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    if bridged:
        driver.accessory = Bridge(driver, "Bridge")
        driver.accessory.add_accessory(camera)
    else:
        driver.accessory = camera
    await _write_recording_state(camera)
    # Entry of an accessory that is not built, e.g. a removed camera.
    driver.state.accessory_states[99] = {"recording": {"Active": 1}}

    driver.unpair(clients[0])
    _assert_state(camera, WRITES, selected=True)
    assert 99 in driver.state.accessory_states

    camera.recording_state_changed = MagicMock()
    driver.unpair(clients[1])
    _assert_state(camera, DEFAULTS, selected=False)
    await _drain()
    camera.recording_state_changed.assert_called_once_with()
    # The defaults are not stored again, the file holds no recording state.
    assert not any(driver.state.accessory_states.values())
    # A new pairing starts from the defaults, also after a restart.
    restored = Camera(_options(), driver, "Cam", aid=CAMERA_AID)
    _assert_state(restored, DEFAULTS, selected=False)
    # The new home's first write does not bring the old selection back.
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(1)
    stored = driver.state.accessory_states[CAMERA_AID]["recording"]
    assert stored["SelectedCameraRecordingConfiguration"] is None


async def test_last_unpair_saves_when_reset_fails(driver, caplog):
    client = uuid.uuid4()
    driver.state.add_paired_client(str(client).encode(), b"\x00" * 32, b"\x01")
    driver.accessory = Camera(_options(), driver, "Cam", aid=1)
    driver.accessory.reset_accessory_state = MagicMock(side_effect=RuntimeError("boom"))

    driver.unpair(client)
    assert driver.async_persist.call_count == 1
    assert "boom" in caplog.text


async def test_camera_without_aid_stores_nothing(driver):
    camera = Camera(_options(), driver, "Cam")
    await _write_recording_state(camera)
    camera.reset_accessory_state()
    assert not driver.state.accessory_states


async def test_last_unpair_without_recording_saves_once(driver):
    client = uuid.uuid4()
    driver.state.add_paired_client(str(client).encode(), b"\x00" * 32, b"\x01")
    options = _options()
    del options["recording"]
    camera = Camera(options, driver, "Cam", aid=CAMERA_AID)
    camera.recording_state_changed = MagicMock()
    driver.accessory = camera

    driver.unpair(client)
    await _drain()
    # Only unpair's own save.
    assert driver.async_persist.call_count == 1
    camera.recording_state_changed.assert_not_called()
