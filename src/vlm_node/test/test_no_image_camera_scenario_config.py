"""Focused retained ScenarioStore coverage for the blank-image producer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from procedure_spec import (
    compute_bundle_config_revision,
    load_bundle,
    scenario_config_payload,
)
from procedure_spec.scenario_consumer import ScenarioConfigConsumerBinding
from vlm_node.no_image_camera import NoImageCameraNode


def _scenario_node(*, spec_root: Path | None = None):
    spec_root = spec_root or (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    initial = spec_root / "thyroidectomy"
    node = NoImageCameraNode.__new__(NoImageCameraNode)
    node._spec_dir = str(initial.resolve())
    node._scenario_config = ScenarioConfigConsumerBinding.from_spec_dir(initial)
    node._scenario_state_received = False
    node._scenario_running = False
    node._scenario_execution_state = ""
    node._scenario_initial_idle = True
    node._tool_display_names = {
        instrument.id: instrument.display_name
        for instrument in load_bundle(initial).bundle.instruments
    }
    applied: list[str] = []
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(
        warning=warnings.append,
        info=lambda _message: None,
    )

    def set_parameters(parameters):
        candidate = str(parameters[0].value)
        node._spec_dir = candidate
        node._tool_display_names = {
            instrument.id: instrument.display_name
            for instrument in load_bundle(candidate).bundle.instruments
        }
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


def test_blank_image_source_rehydrates_scenario_at_initial_idle() -> None:
    node, spec_root, applied, warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == [str(candidate.resolve())]
    assert node._scenario_config.revision == compute_bundle_config_revision(candidate)
    assert node._scenario_config.pending_snapshot() is None
    assert warnings == []


def test_blank_image_source_defers_scenario_until_paused() -> None:
    node, spec_root, applied, _warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"
    node._scenario_state_received = True
    node._scenario_running = True
    node._scenario_execution_state = "running"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == []
    assert node._scenario_config.pending_snapshot() is not None

    node._on_simulation_state(
        SimpleNamespace(running=True, execution_state="paused")
    )

    assert applied == [str(candidate.resolve())]
    assert node._scenario_config.pending_snapshot() is None


def test_blank_image_source_rejects_external_scenario_path() -> None:
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
    assert node._scenario_config.revision == ""
    assert node._scenario_config.pending_snapshot() is None
    assert warnings and "outside the fixed root" in warnings[-1]


def test_blank_image_source_rejects_authored_edit_while_waiting_for_pause(
    tmp_path,
) -> None:
    source_root = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    copied_root = tmp_path / "specs"
    import shutil

    shutil.copytree(source_root, copied_root)
    node, spec_root, applied, warnings = _scenario_node(spec_root=copied_root)
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
    node._on_simulation_state(SimpleNamespace(running=True, execution_state="paused"))

    assert applied == []
    assert node._scenario_config.revision == ""
    assert node._scenario_config.pending_snapshot() is None
    assert warnings and "revision does not match" in warnings[-1]


def test_blank_image_source_rejects_runtime_rebind_of_scenario_config_topic() -> None:
    node = NoImageCameraNode.__new__(NoImageCameraNode)

    result = node._on_parameters_changed(
        [SimpleNamespace(name="scenario_config_topic", value="/other/scenario_config")]
    )

    assert result.successful is False
    assert "process-lifetime" in result.reason
