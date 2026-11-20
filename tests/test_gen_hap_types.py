"""Tests for the catalog generator in scripts/gen_hap_types.py."""

import pytest

from scripts.gen_hap_types import ServiceCatalog, ServiceEntry, generate_services

UUID = "0000008C-0000-1000-8000-0026BB765291"
CHARS = (
    "CurrentPosition",
    "HoldPosition",
    "Name",
    "ObstructionDetected",
    "PositionState",
    "TargetPosition",
)
REQUIRED = ["CurrentPosition", "TargetPosition", "PositionState"]


def _service(required: list[str], optional: list[str]) -> ServiceEntry:
    return {
        "OptionalCharacteristics": optional,
        "RequiredCharacteristics": required,
        "UUID": UUID,
    }


EXISTING = {"WindowCovering": _service(REQUIRED, ["Name"])}


def _generate(
    required: list[str],
    optional: list[str],
    existing: ServiceCatalog,
    description: str = "Window Covering",
) -> ServiceCatalog:
    meta = {
        "window-covering": {
            "Characteristics": {"Optional": optional, "Required": required},
            "DefaultDescription": description,
            "ShortUUID": "0000008C",
        }
    }
    return generate_services(
        meta, {c: {} for c in CHARS}, {}, {}, {c: c for c in CHARS}, existing
    )


@pytest.mark.parametrize(
    ("apple_required", "apple_optional", "optional"),
    [
        pytest.param(
            ["CurrentPosition", "PositionState", "TargetPosition"],
            ["Name"],
            ["Name"],
            id="reordered",
        ),
        pytest.param(
            ["CurrentPosition", "TargetPosition", "PositionState", "Name"],
            [],
            ["Name"],
            id="promoted",
        ),
        pytest.param(
            [
                "ObstructionDetected",
                "CurrentPosition",
                "TargetPosition",
                "PositionState",
            ],
            ["Name"],
            ["Name", "ObstructionDetected"],
            id="new-required",
        ),
        pytest.param(
            ["CurrentPosition", "PositionState"],
            ["Name", "TargetPosition"],
            ["Name"],
            id="demoted",
        ),
    ],
)
def test_generate_services_keeps_required(
    apple_required: list[str], apple_optional: list[str], optional: list[str]
) -> None:
    """Test an existing service keeps its required list and order."""
    catalog = _generate(apple_required, apple_optional, EXISTING)
    assert catalog == {"WindowCovering": _service(REQUIRED, optional)}


def test_generate_services_renamed_service() -> None:
    """Test a service the metadata renames keeps its name and required list."""
    apple_required = ["CurrentPosition", "PositionState", "TargetPosition"]
    catalog = _generate(apple_required, ["Name"], EXISTING, "Window Coverings")
    assert catalog == {"WindowCovering": _service(REQUIRED, ["Name"])}


def test_generate_services_new_service() -> None:
    """Test a new service takes Apple's lists as they are."""
    catalog = _generate(["PositionState", "CurrentPosition"], ["Name"], {})
    expected = _service(["PositionState", "CurrentPosition"], ["Name"])
    assert catalog == {"WindowCovering": expected}


@pytest.mark.parametrize(
    ("apple_required", "report"),
    [
        pytest.param(REQUIRED, "", id="same"),
        pytest.param(
            [*REQUIRED, "Name"],
            "  ! service WindowCovering: keeping required ['CurrentPosition', "
            "'TargetPosition', 'PositionState'] (metadata says ['CurrentPosition', "
            "'TargetPosition', 'PositionState', 'Name'])\n",
            id="promoted",
        ),
    ],
)
def test_generate_services_reports_required(
    capsys: pytest.CaptureFixture[str], apple_required: list[str], report: str
) -> None:
    """Test a required list the metadata lists differently is reported."""
    _generate(apple_required, [], EXISTING)
    assert capsys.readouterr().err == report
