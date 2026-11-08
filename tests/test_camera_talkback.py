"""Tests for pyhap.camera_talkback."""

import ipaddress

from pyhap.camera_talkback import is_allowed_sender


def test_is_allowed_sender_matches_controller() -> None:
    """Verify packets from the negotiated controller address are accepted."""
    assert is_allowed_sender(("192.168.1.114", 5000), "192.168.1.114")
    assert is_allowed_sender(("::1", 5000), "::1")
    assert is_allowed_sender(("::1", 5000), "0:0:0:0:0:0:0:1")
    assert is_allowed_sender(
        ("192.168.1.114", 5000),
        ipaddress.ip_address("192.168.1.114"),  # ty: ignore[invalid-argument-type]
    )


def test_is_allowed_sender_rejects_other_sources() -> None:
    """Verify packets from any other source are rejected."""
    assert not is_allowed_sender(("10.0.0.1", 5000), "192.168.1.114")
    assert not is_allowed_sender(("::1", 5000), "192.168.1.114")
    assert not is_allowed_sender(("::2", 5000), "0:0:0:0:0:0:0:1")
    assert not is_allowed_sender(
        ("10.0.0.1", 5000),
        ipaddress.ip_address("::1"),  # ty: ignore[invalid-argument-type]
    )


def test_is_allowed_sender_without_controller_address() -> None:
    """Verify None keeps the legacy behavior of accepting any source."""
    assert is_allowed_sender(("10.0.0.1", 5000), None)


def test_is_allowed_sender_non_ip_falls_back_to_string() -> None:
    """Verify a non-IP controller address falls back to string comparison."""
    assert is_allowed_sender(("homekit.local", 5000), "homekit.local")
    assert not is_allowed_sender(("other.local", 5000), "homekit.local")
