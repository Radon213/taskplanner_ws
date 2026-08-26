from __future__ import annotations

from pathlib import Path

from or_digital_twin.node import ORDigitalTwinNode
from procedure_spec import load_bundle


def _spec_root() -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )


def test_bundle_metadata_exposes_display_details_and_distinct_defaults() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    demo = node._bundle_metadata_payload(
        load_bundle(_spec_root() / "thyroidectomy_demo")
    )
    standard = node._bundle_metadata_payload(
        load_bundle(_spec_root() / "thyroidectomy")
    )

    assert demo["default_phase_id"] == "P03"
    assert demo["display_name"] == "Thyroidectomy"
    assert demo["display_name_ko"] == "갑상선절제술(시연)"
    assert demo["target_site"] == "Right Lobectomy"
    assert demo["target_site_ko"] == "Right Lobectomy"
    assert demo["approach"] == "Open"
    assert demo["approach_ko"] == "Open"
    assert [
        (
            instrument["id"],
            instrument["home_location_type"],
            instrument["home_location_id"],
        )
        for instrument in demo["instruments"]
    ] == [
        ("T02", "tray_slot", "main_tray_slot_1"),
        ("T03", "tray_slot", "main_tray_slot_2"),
        ("T04", "tray_slot", "main_tray_slot_3"),
        ("T07", "tray_slot", "main_tray_slot_4"),
    ]
    assert standard["default_phase_id"] == "P01"
    assert standard["display_name"] == "Thyroidectomy"
    assert standard["display_name_ko"] == "Thyroidectomy"
    assert standard["target_site"] == "Right Lobectomy"
    assert standard["target_site_ko"] == "Right Lobectomy"
    assert standard["approach"] == "Open"
    assert standard["approach_ko"] == "Open"
