from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import PhaseEvidence


def test_strong_allowed_next_phase_is_not_vetoed_by_current_phase_prior() -> None:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        spec=spec,
        state=SimpleNamespace(filtered_phase="P03"),
    )
    node._prior_scorer = SimpleNamespace(
        score=lambda _evidence: {
            "phase": [["P03", 1.0], ["P04", 0.2]],
        }
    )
    node._runtime_prior_evidence = lambda: {}
    node._publish_reducer_decision_event = lambda **_kwargs: None
    node._stamp_sec = lambda _stamp: 0.0

    evidence = PhaseEvidence()
    evidence.source = "real_vlm:test"
    evidence.phase_ids = ["P04", "P03"]
    evidence.phase_confidences = [0.95, 0.2]
    evidence.uncertainty = 0.1

    fused = node._fuse_phase_evidence(evidence)

    assert fused.phase_ids[0] == "P04"
    assert fused.phase_confidences[0] >= 0.8


def test_strong_later_phase_is_preserved_for_sequential_catch_up() -> None:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    twin = ORDigitalTwin(spec)
    twin.set_initial_phase("P03")
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = twin
    node._prior_scorer = SimpleNamespace(
        score=lambda _evidence: {
            "phase": [["P03", 1.0], ["P04", 0.1]],
        }
    )
    node._runtime_prior_evidence = lambda: {}
    node._publish_reducer_decision_event = lambda **_kwargs: None
    node._stamp_sec = lambda _stamp: 0.0

    evidence = PhaseEvidence()
    evidence.source = "real_vlm:test"
    evidence.phase_ids = ["P05", "P03"]
    evidence.phase_confidences = [0.85, 0.2]
    evidence.uncertainty = 0.1

    fused = node._fuse_phase_evidence(evidence)

    assert fused.phase_ids[0] == "P05"
    assert fused.phase_confidences[0] >= 0.8
