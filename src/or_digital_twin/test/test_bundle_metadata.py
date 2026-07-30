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


def test_bundle_metadata_exposes_demo_default_without_changing_thyroidectomy() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    demo = node._bundle_metadata_payload(
        load_bundle(_spec_root() / "thyroidectomy_demo")
    )
    standard = node._bundle_metadata_payload(
        load_bundle(_spec_root() / "thyroidectomy")
    )

    assert demo["default_phase_id"] == "P03"
    assert standard["default_phase_id"] == "P01"
