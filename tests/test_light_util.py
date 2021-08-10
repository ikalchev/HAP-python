"""Test for pyhap.light_util."""

from pyhap.light_util import mireds_to_hue_sat


def test_mireds_to_hue_sat():
    """Test mireds_to_hue_sat."""
    assert mireds_to_hue_sat(99) == (19, 222.1)
    assert mireds_to_hue_sat(100) == (19, 222.1)
    assert mireds_to_hue_sat(100.5) == (19, 222.1)
    assert mireds_to_hue_sat(500) == (64.1, 28.3)
    assert mireds_to_hue_sat(500.5) == (64.1, 28.3)
