"""Experimental HKSV service definitions and authenticated HDS setup.

No service is installed automatically. The caller must provide real media
capabilities and attach these services only after the recording path is ready.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from pyhap.characteristic import Characteristic
from pyhap.service import Service
from pyhap.util import hap_type_to_uuid


def tlv(*items: tuple[int, bytes]) -> bytes:
    """Encode TLV8 including long-value continuation."""
    out = bytearray()
    for tag, value in items:
        if not 0 <= tag <= 255:
            raise ValueError("invalid TLV tag")
        for offset in range(0, max(len(value), 1), 255):
            chunk = value[offset : offset + 255]
            out.extend((tag, len(chunk)))
            out.extend(chunk)
    return bytes(out)


def parse_tlv(data: bytes) -> dict[int, bytes]:
    """Parse singleton TLV fields, rejecting ambiguous repeated fields."""
    result: dict[int, bytes] = {}
    last_tag = -1
    last_length = -1
    i = 0
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError("truncated TLV header")
        tag, length = data[i : i + 2]
        i += 2
        if i + length > len(data):
            raise ValueError("truncated TLV value")
        if tag in result:
            if tag != last_tag or last_length != 255:
                raise ValueError("duplicate TLV field")
            result[tag] += data[i : i + length]
        else:
            result[tag] = data[i : i + length]
        i += length
        last_tag, last_length = tag, length
    return result


def b64(data: bytes) -> str:
    """Encode bytes as a HomeKit TLV characteristic value."""
    return base64.b64encode(data).decode("ascii")


def unb64(value: str) -> bytes:
    """Decode a strictly formatted TLV characteristic value."""
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as err:
        raise ValueError("invalid base64 TLV") from err


def _u(value: int, size: int) -> bytes:
    return value.to_bytes(size, "little")


@dataclass(frozen=True)
class RecordingCapabilities:
    """Only describe formats actually produced by the configured recorder."""

    width: int
    height: int
    fps: int
    profile: int
    level: int
    audio_codec: int
    audio_sample_rate: int
    prebuffer_ms: int = 4000
    fragment_ms: int = 4000
    event_triggers: int = 1
    audio_channels: int = 1
    audio_bitrate_mode: int = 0
    resolutions: tuple[tuple[int, int, int], ...] = ()
    profiles: tuple[int, ...] = ()
    levels: tuple[int, ...] = ()

    def supported(self) -> tuple[str, str, str]:
        """Build the advertised recording, video and audio TLVs."""
        if (
            self.prebuffer_ms < 4000
            or self.fragment_ms <= 0
            or not 0 < self.width <= 65535
            or not 0 < self.height <= 65535
            or not 0 < self.fps <= 255
        ):
            raise ValueError("invalid recording capabilities")
        container = tlv((1, b"\0"), (2, tlv((1, _u(self.fragment_ms, 4)))))
        recording = tlv(
            (1, _u(self.prebuffer_ms, 4)),
            (2, _u(self.event_triggers, 8)),
            (3, container),
        )
        resolutions = self.resolutions or ((self.width, self.height, self.fps),)
        profiles = self.profiles or (self.profile,)
        levels = self.levels or (self.level,)
        if not resolutions or any(
            not 0 < width <= 65535 or not 0 < height <= 65535 or not 0 < fps <= 255
            for width, height, fps in resolutions
        ):
            raise ValueError("invalid recording resolutions")
        if any(not 0 <= profile <= 2 for profile in profiles) or any(
            not 0 <= level <= 2 for level in levels
        ):
            raise ValueError("invalid recording profile or level")

        def tlv_list(tag: int, entries: tuple[bytes, ...]) -> bytes:
            return b"\x00\x00".join(tlv((tag, entry)) for entry in entries)

        params = tlv_list(1, tuple(_u(p, 1) for p in profiles)) + tlv_list(
            2, tuple(_u(level, 1) for level in levels)
        )
        attrs = tuple(
            tlv((1, _u(width, 2)), (2, _u(height, 2)), (3, _u(fps, 1)))
            for width, height, fps in resolutions
        )
        video_config = tlv((1, b"\0"), (2, params)) + tlv_list(3, attrs)
        video = tlv((1, video_config))
        audio_params = tlv(
            (1, _u(self.audio_channels, 1)),
            (2, _u(self.audio_bitrate_mode, 1)),
            (3, _u(self.audio_sample_rate, 1)),
        )
        audio = tlv((1, tlv((1, _u(self.audio_codec, 1)), (2, audio_params))))
        return b64(recording), b64(video), b64(audio)


def _characteristic(
    name: str, type_id: str, fmt: str, perms: list[str], value: Any
) -> Characteristic:
    char = Characteristic(
        name, hap_type_to_uuid(type_id), {"Format": fmt, "Permissions": perms}
    )
    char.set_value(value, should_notify=False)
    return char


def _service(name: str, type_id: str, chars: list[Characteristic]) -> Service:
    service = Service(hap_type_to_uuid(type_id), name)
    service.add_characteristic(*chars)
    return service


class HKSVServices:
    """Build but do not attach camera services.

    The caller owns persistence, validation of selected recording configuration,
    camera operating state, and media startup. No output is advertised merely by
    constructing this object.
    """

    def __init__(
        self, hds: Any, capabilities: RecordingCapabilities, validate_selected: Any
    ):
        """Construct detached services with authenticated setup callbacks."""
        self.hds = hds
        self.validate_selected = validate_selected
        self._responses: dict[object, str] = {}
        recording, video, audio = capabilities.supported()
        rw = ["pr", "pw", "ev"]
        self.active = _characteristic("Active", "B0", "uint8", rw, 0)
        self.audio_active = _characteristic(
            "Recording Audio Active", "226", "uint8", rw, 0
        )
        self.selected = _characteristic(
            "Selected Camera Recording Configuration", "209", "tlv8", rw, ""
        )
        self.selected.session_setter_callback = self._set_selected_session
        self.camera_active = _characteristic(
            "HomeKit Camera Active", "21B", "uint8", rw, 1
        )
        self.event_snapshots = _characteristic(
            "Event Snapshots Active", "223", "uint8", rw, 1
        )
        self.periodic_snapshots = _characteristic(
            "Periodic Snapshots Active", "225", "uint8", rw, 1
        )
        self.setup = _characteristic(
            "Setup Data Stream Transport", "131", "tlv8", ["pr", "pw", "wr"], ""
        )
        # A patched HAP-python must add these slots and provide authenticated context.
        if not hasattr(self.setup, "session_setter_callback") or not hasattr(
            self.setup, "session_getter_callback"
        ):
            raise RuntimeError(
                "HAP-python authenticated session callbacks are required"
            )
        self.setup.session_setter_callback = self._setup_transport
        self.setup.session_getter_callback = self._get_transport
        self.recording = _service(
            "Camera Recording Management",
            "204",
            [
                self.active,
                self.audio_active,
                self.selected,
                _characteristic(
                    "Supported Camera Recording Configuration",
                    "205",
                    "tlv8",
                    ["pr", "ev"],
                    recording,
                ),
                _characteristic(
                    "Supported Video Recording Configuration",
                    "206",
                    "tlv8",
                    ["pr", "ev"],
                    video,
                ),
                _characteristic(
                    "Supported Audio Recording Configuration",
                    "207",
                    "tlv8",
                    ["pr", "ev"],
                    audio,
                ),
            ],
        )
        self.operating = _service(
            "Camera Operating Mode",
            "21A",
            [self.camera_active, self.event_snapshots, self.periodic_snapshots],
        )
        self.transport = _service(
            "Data Stream Transport Management",
            "129",
            [
                self.setup,
                _characteristic(
                    "Supported Data Stream Transport Configuration",
                    "130",
                    "tlv8",
                    ["pr"],
                    b64(tlv((1, tlv((1, b"\0"))))),
                ),
                _characteristic("Version", "37", "string", ["pr"], "1.0"),
            ],
        )

    def _set_selected_session(self, value: str, session: Any) -> None:
        # Validate before modifying the characteristic value.
        self.validate_selected(unb64(value))
        self.selected.set_value(value)

    def _setup_transport(self, value: str, session: Any) -> str:
        if session.closed or session.shared_key is None:
            raise ValueError("HDS setup requires an open authenticated session")
        fields = parse_tlv(unb64(value))
        if (
            set(fields) != {1, 2, 3}
            or fields[1] != b"\0"
            or fields[2] != b"\0"
            or len(fields[3]) != 32
        ):
            raise ValueError("invalid HDS setup request")
        port, salt = self.hds.prepare_session(session.shared_key, fields[3], session)
        response = b64(tlv((1, b"\0"), (2, tlv((1, _u(port, 2)))), (3, salt)))
        if session not in self._responses:
            session.add_close_callback(lambda: self._close_session(session))
        self._responses[session] = response
        return response

    def _get_transport(self, session: Any) -> str:
        return self._responses.get(session, "")

    def _close_session(self, session: Any) -> None:
        self._responses.pop(session, None)
        self.hds.close_owner(session)


def parse_selected(data: bytes, capabilities: RecordingCapabilities) -> dict[str, int]:
    """Parse and validate selected values against the advertised single profile."""

    def field(fields: dict[int, bytes], tag: int, length: int) -> int:
        value = fields.get(tag)
        if value is None or len(value) != length:
            raise ValueError("missing or invalid selected configuration field")
        return int.from_bytes(value, "little")

    outer = parse_tlv(data)
    if set(outer) != {1, 2, 3}:
        raise ValueError("invalid selected configuration")
    recording, video, audio = (parse_tlv(outer[i]) for i in (1, 2, 3))
    prebuffer = field(recording, 1, 4)
    events = field(recording, 2, 8)
    container = parse_tlv(recording[3])
    fragment = field(parse_tlv(container[2]), 1, 4)
    params, attrs = parse_tlv(video[2]), parse_tlv(video[3])
    audio_params = parse_tlv(audio[2])
    selected = {
        "prebuffer_ms": prebuffer,
        "event_triggers": events,
        "fragment_ms": fragment,
        "width": field(attrs, 1, 2),
        "height": field(attrs, 2, 2),
        "fps": field(attrs, 3, 1),
        "profile": field(params, 1, 1),
        "level": field(params, 2, 1),
        "video_bitrate_kbps": field(params, 3, 4),
        "iframe_interval_ms": field(params, 4, 4),
        "audio_codec": field(audio, 1, 1),
        "audio_channels": field(audio_params, 1, 1),
        "audio_bitrate_mode": field(audio_params, 2, 1),
        "audio_sample_rate": field(audio_params, 3, 1),
        "audio_bitrate_kbps": field(audio_params, 4, 4),
    }
    if (
        field(container, 1, 1) != 0
        or field(video, 1, 1) != 0
        or not 0 < prebuffer <= capabilities.prebuffer_ms
        or not events
        or events & ~capabilities.event_triggers
        or fragment != capabilities.fragment_ms
        or (selected["width"], selected["height"], selected["fps"])
        not in (
            capabilities.resolutions
            or ((capabilities.width, capabilities.height, capabilities.fps),)
        )
        or selected["profile"] not in (capabilities.profiles or (capabilities.profile,))
        or selected["level"] not in (capabilities.levels or (capabilities.level,))
        or any(
            selected[k] != getattr(capabilities, k)
            for k in (
                "audio_codec",
                "audio_channels",
                "audio_bitrate_mode",
                "audio_sample_rate",
            )
        )
        or not 1 <= selected["video_bitrate_kbps"] <= 10000
        or not 1 <= selected["iframe_interval_ms"] <= 4000
        or not 1 <= selected["audio_bitrate_kbps"] <= 512
    ):
        raise ValueError("unsupported selected recording configuration")
    return selected
