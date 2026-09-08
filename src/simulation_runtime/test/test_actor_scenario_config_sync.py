"""Focused ScenarioStore observer coverage for simulation-input actors."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from procedure_spec import (
    compute_bundle_config_revision,
    load_bundle,
    scenario_config_payload,
)
from procedure_spec.scenario_consumer import ScenarioConfigConsumerBinding
from simulation_runtime.llm_surgeon_actor import LLMSurgeonActorNode
from simulation_runtime.mock_surgeon import MockSurgeonNode
from simulation_runtime.surgeon_actor import SurgeonActorNode


ACTOR_TYPES = (
    MockSurgeonNode,
    SurgeonActorNode,
    LLMSurgeonActorNode,
)


def _scenario_node(node_type, *, spec_root: Path | None = None):
    spec_root = spec_root or (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    initial = spec_root / "thyroidectomy"
    node = node_type.__new__(node_type)
    node._spec_dir = str(initial.resolve())
    node._spec = load_bundle(initial)
    node._scenario_config = ScenarioConfigConsumerBinding.from_spec_dir(initial)
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


@pytest.mark.parametrize("node_type", ACTOR_TYPES)
def test_actor_rehydrates_selected_scenario_at_initial_idle(node_type) -> None:
    node, spec_root, applied, warnings = _scenario_node(node_type)
    candidate = spec_root / "nephrectomy"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == [str(candidate.resolve())]
    assert node._spec.procedure_id == "nephrectomy"
    assert node._scenario_config.revision == compute_bundle_config_revision(candidate)
    assert node._scenario_config.pending_snapshot() is None
    assert warnings == []


@pytest.mark.parametrize("node_type", ACTOR_TYPES)
def test_actor_defers_revision_until_authoritative_pause(node_type) -> None:
    node, spec_root, applied, _warnings = _scenario_node(node_type)
    candidate = spec_root / "nephrectomy"
    node._scenario_state_received = True
    node._scenario_running = True
    node._scenario_execution_state = "running"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == []
    assert node._scenario_config.pending_snapshot() is not None

    state = SimpleNamespace(running=True, execution_state="paused")
    if node_type is LLMSurgeonActorNode:
        # The LLM actor intentionally remains independent of SimulationState;
        # its existing lifecycle-control input supplies its local boundary.
        node._scenario_running = True
        node._scenario_execution_state = "paused"
        node._apply_pending_scenario_config_if_safe()
    else:
        if node_type is MockSurgeonNode:
            state.instrument_states = []
        node._on_simulation_state(state)

    assert applied == [str(candidate.resolve())]
    assert node._spec.procedure_id == "nephrectomy"
    assert node._scenario_config.pending_snapshot() is None


@pytest.mark.parametrize("node_type", ACTOR_TYPES)
def test_actor_rejects_external_scenario_path_and_keeps_last_good(node_type) -> None:
    node, _spec_root, applied, warnings = _scenario_node(node_type)

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
    assert node._scenario_config.revision == ""
    assert node._scenario_config.pending_snapshot() is None
    assert warnings and "outside the fixed root" in warnings[-1]


@pytest.mark.parametrize("node_type", ACTOR_TYPES)
def test_actor_rejects_authored_edit_while_waiting_for_pause(
    tmp_path, node_type
) -> None:
    """The delayed local swap must not reload newer bytes under an old digest."""

    source_root = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    copied_root = tmp_path / "specs"
    import shutil

    shutil.copytree(source_root, copied_root)
    node, spec_root, applied, warnings = _scenario_node(
        node_type, spec_root=copied_root
    )
    candidate = spec_root / "nephrectomy"
    node._scenario_state_received = True
    node._scenario_running = True
    node._scenario_execution_state = "running"

    node._on_scenario_config(_scenario_message(candidate))
    assert node._scenario_config.pending_snapshot() is not None

    prompt = candidate / "vlm_procedure_prompt.yaml"
    prompt.write_text(
        prompt.read_text(encoding="utf-8") + "\n# edited during pause wait\n",
        encoding="utf-8",
    )

    if node_type is LLMSurgeonActorNode:
        node._scenario_execution_state = "paused"
        node._apply_pending_scenario_config_if_safe()
    else:
        state = SimpleNamespace(running=True, execution_state="paused")
        if node_type is MockSurgeonNode:
            state.instrument_states = []
        node._on_simulation_state(state)

    assert applied == []
    assert node._spec.procedure_id == "thyroidectomy"
    assert node._scenario_config.revision == ""
    assert node._scenario_config.pending_snapshot() is None
    assert warnings and "revision does not match" in warnings[-1]


@pytest.mark.parametrize("node_type", ACTOR_TYPES)
def test_actor_rejects_runtime_rebind_of_scenario_config_topic(node_type) -> None:
    node = node_type.__new__(node_type)

    result = node._on_parameters_changed(
        [SimpleNamespace(name="scenario_config_topic", value="/other/scenario_config")]
    )

    assert result.successful is False
    assert "process-lifetime" in result.reason
