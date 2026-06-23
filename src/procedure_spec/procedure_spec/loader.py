"""Loader entrypoints for surgical procedure bundles."""

from __future__ import annotations

from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory

from .models import (
    ActionGuardPolicy,
    HumanoidPolicy,
    InitialPlacement,
    InstrumentSpec,
    MockObservation,
    MockPerceptionScenario,
    MockPerceptionStage,
    MockPhaseHypothesis,
    MockSurgeonGesture,
    MockSurgeonScenario,
    MockSurgeonStage,
    PhaseGuardPolicy,
    PhaseSpec,
    ProcedureBundle,
    SceneLocation,
    SimulationAnchor,
    SimulationEntity,
)
from .prompt_bundle import build_raw_bundle_from_prompt, has_procedure_prompt
from .query_api import ProcedureSpec
from .validator import validate_bundle_paths, validate_raw_bundle

LEGACY_PROMPT_CONFLICT_FILES = (
    "procedure.yaml",
    "instruments.yaml",
    "scene_layout.yaml",
    "policy.yaml",
    "simulation_layout.yaml",
    "mock_surgeon.yaml",
    "mock_perception.yaml",
)


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _read_optional_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    return _read_yaml(path)


def _deep_merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _instrument_requestable(instrument: dict) -> bool:
    ui_payload = instrument.get("ui", {})
    ui_requestable = ui_payload.get("requestable", True) if isinstance(ui_payload, dict) else True
    return bool(instrument.get("requestable", ui_requestable))


def _reject_prompt_legacy_conflicts(bundle_path: Path) -> None:
    conflicts = [name for name in LEGACY_PROMPT_CONFLICT_FILES if (bundle_path / name).is_file()]
    if conflicts:
        joined = ", ".join(conflicts)
        raise ValueError(
            f"{bundle_path} uses a procedure prompt YAML; remove legacy bundle files: {joined}"
        )


def get_default_spec_dir() -> Path:
    share = Path(get_package_share_directory("procedure_spec"))
    return share / "specs" / "thyroidectomy"


def load_bundle(bundle_dir: str | Path | None = None) -> ProcedureSpec:
    bundle_path = Path(bundle_dir) if bundle_dir else get_default_spec_dir()
    common_catalog_path = bundle_path.parent / "display_catalog.yaml"
    bundle_catalog_path = bundle_path / "display_catalog.yaml"
    display_catalog = _deep_merge_dicts(
        _read_optional_yaml(common_catalog_path),
        _read_optional_yaml(bundle_catalog_path),
    )
    if has_procedure_prompt(bundle_path):
        _reject_prompt_legacy_conflicts(bundle_path)
        raw_bundle = build_raw_bundle_from_prompt(bundle_path, display_catalog)
    else:
        validate_bundle_paths(bundle_path)
        raw_bundle = {
            "procedure": _read_yaml(bundle_path / "procedure.yaml"),
            "instruments": _read_yaml(bundle_path / "instruments.yaml"),
            "scene_layout": _read_yaml(bundle_path / "scene_layout.yaml"),
            "policy": _read_yaml(bundle_path / "policy.yaml"),
            "simulation_layout": _read_yaml(bundle_path / "simulation_layout.yaml"),
            "mock_surgeon": _read_yaml(bundle_path / "mock_surgeon.yaml"),
            "display_catalog": display_catalog,
        }
        mock_perception_path = bundle_path / "mock_perception.yaml"
        if mock_perception_path.is_file():
            raw_bundle["mock_perception"] = _read_yaml(mock_perception_path)
    validate_raw_bundle(raw_bundle)

    procedure = raw_bundle["procedure"]
    instruments = raw_bundle["instruments"]
    scene_layout = raw_bundle["scene_layout"]
    policy = raw_bundle["policy"]
    simulation_layout = raw_bundle["simulation_layout"]
    mock_perception = raw_bundle.get("mock_perception", {})
    mock_surgeon = raw_bundle["mock_surgeon"]

    bundle = ProcedureBundle(
        procedure_id=str(procedure["procedure_id"]),
        procedure_display_name=str(procedure.get("procedure_display_name", procedure["procedure_id"])),
        procedure_display_name_ko=str(
            procedure.get("procedure_display_name_ko", procedure.get("procedure_display_name", procedure["procedure_id"]))
        ),
        normal_phase_ids=[str(item) for item in procedure.get("normal_phase_ids", [])],
        interrupt_phase_ids=[str(item) for item in procedure.get("interrupt_phase_ids", [])],
        phases=[
            PhaseSpec(
                id=str(phase["id"]),
                display_name=str(phase.get("display_name", phase["id"])),
                display_name_ko=str(phase.get("display_name_ko", phase.get("display_name", phase["id"]))),
                possible_next=[str(item) for item in phase["possible_next"]],
                expected_instruments=[str(item) for item in phase["expected_instruments"]],
                min_duration_sec=float(phase.get("min_duration_sec", 0.0)),
            )
            for phase in procedure["phases"]
        ],
        instruments=[
            InstrumentSpec(
                id=str(instrument["id"]),
                display_name=str(instrument.get("display_name", instrument["id"])),
                display_name_ko=str(instrument.get("display_name_ko", instrument.get("display_name", instrument["id"]))),
                aliases=[str(alias) for alias in instrument.get("aliases", [])],
                category=str(instrument["category"]),
                requestable=_instrument_requestable(instrument),
                role=str(instrument.get("role", "")),
                handover_profile=str(instrument["handover_profile"]),
            )
            for instrument in instruments["instruments"]
        ],
        display_catalog=display_catalog,
        locations=[
            SceneLocation(id=str(location["id"]), type=str(location["type"]))
            for location in scene_layout["locations"]
        ],
        initial_placements=[
            InitialPlacement(
                instrument_id=str(placement["instrument_id"]),
                location_id=str(placement["location_id"]),
            )
            for placement in scene_layout["initial_instrument_placement"]
        ],
        phase_guard=PhaseGuardPolicy(
            min_confidence_to_keep=float(policy["phase_guard"]["min_confidence_to_keep"]),
            min_confidence_to_switch=float(policy["phase_guard"]["min_confidence_to_switch"]),
            smoothing_window=int(policy["phase_guard"]["smoothing_window"]),
            min_dwell_time_sec=float(policy["phase_guard"]["min_dwell_time_sec"]),
            allow_unknown_phase=bool(policy["phase_guard"]["allow_unknown_phase"]),
        ),
        action_guard=ActionGuardPolicy(
            block_handover_when_phase_uncertain=bool(
                policy["action_guard"]["block_handover_when_phase_uncertain"]
            ),
            require_multi_evidence_for_handover=bool(
                policy["action_guard"]["require_multi_evidence_for_handover"]
            ),
            allow_prepositioning_when_uncertain=bool(
                policy["action_guard"]["allow_prepositioning_when_uncertain"]
            ),
            explicit_request_priority=bool(policy["action_guard"]["explicit_request_priority"]),
        ),
        humanoid_policy=HumanoidPolicy(
            handover_arm=str(policy["humanoid_policy"]["handover_arm"]),
            recovery_arm=str(policy["humanoid_policy"]["recovery_arm"]),
            require_cleaning_after_surgeon_use=bool(
                policy["humanoid_policy"]["require_cleaning_after_surgeon_use"]
            ),
            allow_anticipatory_hold=bool(policy["humanoid_policy"]["allow_anticipatory_hold"]),
            voice_override_preempts_preposition=bool(
                policy["humanoid_policy"]["voice_override_preempts_preposition"]
            ),
            direct_return_to_rack_for_unused_prepositioned_tool=bool(
                policy["humanoid_policy"]["direct_return_to_rack_for_unused_prepositioned_tool"]
            ),
        ),
        simulation_entities=[
            SimulationEntity(
                id=str(entity["id"]),
                type=str(entity["type"]),
                x=float(entity["x"]),
                y=float(entity["y"]),
                width=float(entity.get("width", 0.0)),
                height=float(entity.get("height", 0.0)),
                label=str(entity.get("label", "")),
            )
            for entity in simulation_layout["entities"]
        ],
        simulation_anchors=[
            SimulationAnchor(
                id=str(anchor["id"]),
                attached_to=str(anchor["attached_to"]),
                x=float(anchor["x"]),
                y=float(anchor["y"]),
                label=str(anchor.get("label", "")),
            )
            for anchor in simulation_layout["anchors"]
        ],
        mock_perception=MockPerceptionScenario(
            period_sec=float(mock_perception.get("period_sec", 1.0)),
            stages=[
                MockPerceptionStage(
                    name=str(stage["name"]),
                    duration_ticks=int(stage["duration_ticks"]),
                    phase_hypotheses=[
                        MockPhaseHypothesis(
                            phase_id=str(hypothesis["phase_id"]),
                            confidence=float(hypothesis["confidence"]),
                        )
                        for hypothesis in stage.get("phase_hypotheses", [])
                    ],
                    observations=[
                        MockObservation(
                            instrument_id=str(observation["instrument_id"]),
                            location_id=str(observation["location_id"]),
                            location_type=str(observation["location_type"]),
                            confidence=float(observation["confidence"]),
                            visible=bool(observation.get("visible", True)),
                        )
                        for observation in stage.get("observations", [])
                    ],
                    surgeon_gesture=MockSurgeonGesture(
                        event_type=str(stage["surgeon_gesture"]["event_type"]),
                        requested_tool=str(stage["surgeon_gesture"].get("requested_tool", "")),
                        hand_pose=str(stage["surgeon_gesture"].get("hand_pose", "")),
                        confidence=float(stage["surgeon_gesture"].get("confidence", 0.0)),
                        note=str(stage["surgeon_gesture"].get("note", "")),
                    )
                    if stage.get("surgeon_gesture")
                    else None,
                    scene_summary=str(stage.get("scene_summary", "")),
                    uncertainty=float(stage.get("uncertainty", 0.0)),
                    explicit_request=str(stage.get("explicit_request", "")),
                )
                for stage in mock_perception.get("stages", [])
            ],
        )
        if mock_perception
        else None,
        mock_surgeon=MockSurgeonScenario(
            period_sec=float(mock_surgeon.get("period_sec", 1.0)),
            stages=[
                MockSurgeonStage(
                    name=str(stage["name"]),
                    phase_id=str(stage["phase_id"]),
                    duration_ticks=int(stage["duration_ticks"]),
                    event_type=str(stage["event_type"]),
                    intent=str(stage.get("intent", "")),
                    requested_tool=str(stage.get("requested_tool", "")),
                    voice_text=str(stage.get("voice_text", "")),
                    ready_for_handover=bool(stage.get("ready_for_handover", False)),
                    ready_for_retrieval=bool(stage.get("ready_for_retrieval", False)),
                    scene_note=str(stage.get("scene_note", "")),
                )
                for stage in mock_surgeon.get("stages", [])
            ],
        ),
    )
    return ProcedureSpec(bundle)
