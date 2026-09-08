from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from procedure_spec import (
    compute_bundle_config_revision,
    load_bundle,
    scenario_config_payload,
)
from vlm_node.real_vlm import RealVLMNode


def _scenario_node() -> tuple[RealVLMNode, Path, list[str], list[str]]:
    spec_root = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    initial = spec_root / "thyroidectomy"
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec_dir = str(initial.resolve())
    node._spec = load_bundle(initial)
    node._scenario_config_root = spec_root
    node._scenario_config_revision = ""
    node._pending_scenario_config = None
    node._scenario_state_received = False
    node._scenario_running = False
    node._scenario_execution_state = ""
    node._scenario_initial_idle = True
    node._active = False
    applied: list[str] = []
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(
        warning=warnings.append,
        info=lambda _message: None,
    )

    def set_parameters(parameters):
        candidate = str(parameters[0].value)
        node._spec_dir = candidate
        node._spec = load_bundle(candidate)
        applied.append(candidate)
        return SimpleNamespace(successful=True, reason="")

    node.set_parameters_atomically = set_parameters
    return node, spec_root, applied, warnings


def _scenario_message(spec_dir: Path, *, revision: str | None = None):
    resolved = spec_dir.resolve()
    return SimpleNamespace(
        data=json.dumps(
            scenario_config_payload(
                bundle_name=resolved.name,
                spec_dir=str(resolved),
                revision=revision or compute_bundle_config_revision(resolved),
            )
        )
    )


def test_vlm_rehydrates_selected_scenario_at_initial_idle() -> None:
    node, spec_root, applied, warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == [str(candidate.resolve())]
    assert node._spec.procedure_id == "nephrectomy"
    assert node._scenario_config_revision == compute_bundle_config_revision(candidate)
    assert node._pending_scenario_config is None
    assert warnings == []


def test_vlm_stages_scenario_revision_while_running_then_applies_when_paused() -> None:
    node, spec_root, applied, _warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"
    node._scenario_state_received = True
    node._scenario_running = True
    node._scenario_execution_state = "running"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == []
    assert node._pending_scenario_config is not None

    node._last_simulation_bundle = ""
    node._reset_public_evidence = lambda: None
    node._activate_lifecycle = lambda _phase: None
    node._stop_lifecycle = lambda: None
    node._track_authoritative_phase = lambda: None
    node._publish_context_summaries = lambda: None
    node._on_simulation(
        SimpleNamespace(
            active_bundle="thyroidectomy",
            running=True,
            execution_state="paused",
            filtered_phase="",
        )
    )

    assert applied == [str(candidate.resolve())]
    assert node._spec.procedure_id == "nephrectomy"
    assert node._pending_scenario_config is None


def test_vlm_rejects_external_scenario_path_and_keeps_last_good_binding() -> None:
    node, _spec_root, applied, warnings = _scenario_node()

    node._on_scenario_config(
        SimpleNamespace(
            data=json.dumps(
                scenario_config_payload(
                    bundle_name="outside",
                    spec_dir="/tmp/outside",
                    revision="sha256:outside",
                )
            )
        )
    )

    assert applied == []
    assert node._spec.procedure_id == "thyroidectomy"
    assert node._scenario_config_revision == ""
    assert node._pending_scenario_config is None
    assert warnings and "outside the fixed root" in warnings[-1]
