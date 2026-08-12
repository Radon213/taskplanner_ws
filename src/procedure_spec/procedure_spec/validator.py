"""Validation helpers for the procedure bundle."""

from __future__ import annotations

from pathlib import Path

from .bed_robot_arm_group import (
    BED_ROBOT_ARM_GROUP_IDS,
    DISTANCE_ORIGINS,
    RETRACTION_DIRECTIONS,
)


REQUIRED_FILES = (
    "procedure.yaml",
    "instruments.yaml",
    "scene_layout.yaml",
    "policy.yaml",
    "simulation_layout.yaml",
    "mock_surgeon.yaml",
)

REQUIRED_SIMULATION_ENTITY_TYPES = {
    "humanoid",
    "surgeon",
    "surgical_bed",
    "instrument_rack",
    "cleaner_station",
}

REQUIRED_SIMULATION_ANCHORS = {
    "robot_right_hand",
    "robot_left_hand",
    "surgeon_receive_zone",
    "surgeon_return_zone",
    "cleaner_slot",
}

ALLOWED_SURGEON_EVENTS = {
    "request_tool",
    "return_tool",
    "voice_request",
    "extend_hand_for_handover",
    "extend_hand_for_retrieval",
    "cancel_request",
}

ALLOWED_SURGEON_GESTURE_EVENTS = {
    "request_tool",
    "return_tool",
}

REQUIRED_DISPLAY_CATALOG_SECTIONS = {
    "lifecycle",
    "actions",
    "transitions",
    "intents",
    "events",
}

REQUIRED_LIFECYCLE_DISPLAY_KEYS = {
    "home_rack",
    "returned_home",
    "prepositioned_right",
    "surgeon_owned",
    "mayo_reuse",
    "mayo_recovery",
    "recovering_left",
    "cleaning_left",
    "cleaned_left",
}

REQUIRED_ACTION_DISPLAY_KEYS = {
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
    "retrieve_from_hand",
    "retrieve_from_mayo",
    "predict_tool",
    "tool_handover",
    "tool_retrieve",
    "tool_predict",
    "predicted_tool_handover",
    "replace_and_handover",
}

REQUIRED_TRANSITION_DISPLAY_KEYS = {
    "recover_left",
    "clean_left",
    "return_home",
    "return_unused_preposition",
}

REQUIRED_EVENT_DISPLAY_KEYS = {
    "SurgeonActorEventObserved",
    "VoiceTranscriptObserved",
    "SurgeonRequestObserved",
    "RobotTaskStarted",
    "RobotTaskCompleted",
    "RobotGraspedTool",
    "ToolPrepared",
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolCleaningCompleted",
    "ToolReturnedToTray",
    "VLMProposalAccepted",
    "VLMProposalRejected",
    "VLMProposalIgnored",
    "ProcedureCompleted",
}

ALLOWED_BED_ROBOT_ARM_OPERATIONS = {
    "retraction",
    "change_end_effector",
}

BED_ROBOT_ARM_IDS = {"arm_1", "arm_2"}
BED_ROBOT_ARM_TARGET_TOOLS = {"thyroid_retractor", "army_navy_retractor"}
BED_ROBOT_ARM_SINGLE_TARGETS = {"left_malleable", "right_malleable"}
BED_ROBOT_ARM_MULTI_TARGET = "both_malleable"


class SpecValidationError(ValueError):
    """Raised when a procedure specification bundle is invalid."""


def validate_bundle_paths(bundle_dir: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (bundle_dir / name).is_file()]
    if missing:
        raise SpecValidationError(
            f"Bundle directory '{bundle_dir}' is missing required files: {', '.join(missing)}"
        )


def _require_mapping(payload: object, label: str) -> dict:
    if not isinstance(payload, dict):
        raise SpecValidationError(f"{label} must be a YAML mapping.")
    return payload


def _require_list(payload: object, label: str) -> list:
    if not isinstance(payload, list):
        raise SpecValidationError(f"{label} must be a YAML list.")
    return payload


def _validate_display_catalog(catalog: object) -> None:
    catalog_map = _require_mapping(catalog, "display_catalog.yaml")
    missing_sections = sorted(REQUIRED_DISPLAY_CATALOG_SECTIONS - set(catalog_map))
    if missing_sections:
        raise SpecValidationError(
            "display_catalog.yaml is missing required sections: " + ", ".join(missing_sections)
        )

    required_by_section = {
        "lifecycle": REQUIRED_LIFECYCLE_DISPLAY_KEYS,
        "actions": REQUIRED_ACTION_DISPLAY_KEYS,
        "transitions": REQUIRED_TRANSITION_DISPLAY_KEYS,
        "events": REQUIRED_EVENT_DISPLAY_KEYS,
    }
    for section, required_keys in required_by_section.items():
        section_map = _require_mapping(catalog_map.get(section), f"display_catalog.yaml {section}")
        missing_keys = sorted(required_keys - set(section_map))
        if missing_keys:
            raise SpecValidationError(
                f"display_catalog.yaml section '{section}' is missing required keys: "
                + ", ".join(missing_keys)
            )
        for key, entry in section_map.items():
            entry_map = _require_mapping(entry, f"display_catalog.yaml {section}.{key}")
            if not entry_map.get("display_name"):
                raise SpecValidationError(f"display_catalog.yaml {section}.{key} requires display_name.")
            if not entry_map.get("display_name_ko"):
                raise SpecValidationError(f"display_catalog.yaml {section}.{key} requires display_name_ko.")


def _validate_bed_robot_arm_groups(payload: object, phase_ids: set[str]) -> None:
    if payload is None or payload == {}:
        return
    config = _require_mapping(payload, "bed_robot_arm_groups")

    directions = [str(item) for item in _require_list(
        config.get("direction_enum"), "bed_robot_arm_groups direction_enum"
    )]
    if set(directions) != set(RETRACTION_DIRECTIONS) or len(directions) != len(RETRACTION_DIRECTIONS):
        raise SpecValidationError(
            "bed_robot_arm_groups direction_enum must contain exactly: "
            + ", ".join(RETRACTION_DIRECTIONS)
        )

    distance_policy = _require_mapping(
        config.get("distance_policy"), "bed_robot_arm_groups distance_policy"
    )
    forbidden_inference_keys = {
        "default_distance_mm",
        "qualitative_min_mm",
        "qualitative_max_mm",
        "qualitative_integer_mm",
        "qualitative_anchors",
        "unitless_numeric_unit",
    }
    forbidden_present = sorted(forbidden_inference_keys.intersection(distance_policy))
    if forbidden_present:
        raise SpecValidationError(
            "bed_robot_arm_groups distance policy must not define inferred/default fields: "
            + ", ".join(forbidden_present)
        )
    if float(distance_policy.get("max_distance_mm", 0.0)) != 30.0:
        raise SpecValidationError("bed_robot_arm_groups max_distance_mm must be 30.")
    if [str(item) for item in distance_policy.get("precedence", [])] != list(DISTANCE_ORIGINS):
        raise SpecValidationError(
            "bed_robot_arm_groups distance precedence must be explicit_with_unit."
        )
    if float(distance_policy.get("cm_to_mm_multiplier", 0.0)) != 10.0:
        raise SpecValidationError("bed_robot_arm_groups cm_to_mm_multiplier must be 10.")
    if distance_policy.get("require_explicit_unit") is not True:
        raise SpecValidationError("bed_robot_arm_groups distances must require an explicit unit.")
    if distance_policy.get("clamp_explicit_values") is not False:
        raise SpecValidationError("bed_robot_arm_groups explicit distances must not be clamped.")
    if set(str(item) for item in distance_policy.get("distance_origins", [])) != set(DISTANCE_ORIGINS):
        raise SpecValidationError(
            "bed_robot_arm_groups distance_origins must contain exactly: "
            + ", ".join(DISTANCE_ORIGINS)
        )

    groups = _require_mapping(config.get("groups"), "bed_robot_arm_groups groups")
    if set(groups) != set(BED_ROBOT_ARM_GROUP_IDS):
        raise SpecValidationError(
            "bed_robot_arm_groups groups must define exactly retraction."
        )
    allowed_by_group: dict[str, set[str]] = {}
    enabled_groups: set[str] = set()
    for group_id, raw_group in groups.items():
        group = _require_mapping(raw_group, f"bed_robot_arm_groups groups.{group_id}")
        if not isinstance(group.get("enabled"), bool):
            raise SpecValidationError(
                f"bed_robot_arm_groups groups.{group_id}.enabled must be boolean."
            )
        if bool(group["enabled"]):
            enabled_groups.add(str(group_id))
        operations = {
            str(item)
            for item in _require_list(
                group.get("allowed_operations"),
                f"bed_robot_arm_groups groups.{group_id}.allowed_operations",
            )
        }
        unsupported = sorted(operations - ALLOWED_BED_ROBOT_ARM_OPERATIONS)
        if unsupported:
            raise SpecValidationError(
                f"bed_robot_arm_groups group '{group_id}' has unsupported operations: "
                + ", ".join(unsupported)
            )
        allowed_by_group[str(group_id)] = operations
        if (
            bool(group["enabled"])
            and "change_end_effector" in operations
            and not str(group.get("initial_end_effector_profile", "")).strip()
        ):
            raise SpecValidationError(
                f"tool-changing bed_robot_arm_groups group '{group_id}' requires initial_end_effector_profile."
            )

    cue_ids: set[str] = set()
    for raw_cue in _require_list(config.get("cues"), "bed_robot_arm_groups cues"):
        cue = _require_mapping(raw_cue, "bed_robot_arm_groups cue")
        cue_id = str(cue.get("id", "")).strip()
        phase_id = str(cue.get("phase_id", "")).strip()
        group_id = str(cue.get("group_id", "")).strip()
        operation = str(cue.get("operation", "")).strip()
        if not cue_id or cue_id in cue_ids:
            raise SpecValidationError(
                f"bed_robot_arm_groups cue id '{cue_id}' must be non-empty and unique."
            )
        cue_ids.add(cue_id)
        if phase_id not in phase_ids:
            raise SpecValidationError(
                f"bed_robot_arm_groups cue '{cue_id}' references unknown phase '{phase_id}'."
            )
        if group_id not in enabled_groups:
            raise SpecValidationError(
                f"bed_robot_arm_groups cue '{cue_id}' references disabled group '{group_id}'."
            )
        if operation not in allowed_by_group[group_id]:
            raise SpecValidationError(
                f"bed_robot_arm_groups cue '{cue_id}' operation '{operation}' is not allowed for {group_id}."
            )
        utterances = [
            str(item).strip()
            for item in _require_list(
                cue.get("utterances"), f"bed_robot_arm_groups cue '{cue_id}' utterances"
            )
        ]
        if not utterances or any(not item for item in utterances):
            raise SpecValidationError(
                f"bed_robot_arm_groups cue '{cue_id}' requires non-empty utterances."
            )
        cue_directions = [
            str(item)
            for item in _require_list(
                cue.get("directions", []), f"bed_robot_arm_groups cue '{cue_id}' directions"
            )
        ]
        unsupported_directions = sorted(set(cue_directions) - set(RETRACTION_DIRECTIONS))
        if unsupported_directions:
            raise SpecValidationError(
                f"bed_robot_arm_groups cue '{cue_id}' has unsupported directions: "
                + ", ".join(unsupported_directions)
            )
        if operation == "retraction":
            if not cue_directions:
                raise SpecValidationError(
                    f"bed_robot_arm_groups adjustment cue '{cue_id}' requires directions."
                )
            adjustment_mode = str(cue.get("adjustment_mode", "")).strip()
            target_retractor_id = str(cue.get("target_retractor_id", "")).strip()
            direction_frame = str(cue.get("direction_frame", "")).strip()
            if direction_frame != "surgeon_view":
                raise SpecValidationError(
                    f"bed_robot_arm_groups adjustment cue '{cue_id}' requires direction_frame surgeon_view."
                )
            if adjustment_mode == "single":
                if target_retractor_id not in BED_ROBOT_ARM_SINGLE_TARGETS:
                    raise SpecValidationError(
                        f"bed_robot_arm_groups single cue '{cue_id}' requires left_malleable or right_malleable."
                    )
                if not set(cue_directions) <= {"UP", "DOWN", "LEFT", "RIGHT"}:
                    raise SpecValidationError(
                        f"bed_robot_arm_groups single cue '{cue_id}' supports cardinal directions only."
                    )
            elif adjustment_mode == "multi":
                if target_retractor_id != BED_ROBOT_ARM_MULTI_TARGET:
                    raise SpecValidationError(
                        f"bed_robot_arm_groups multi cue '{cue_id}' requires both_malleable."
                    )
                if not set(cue_directions) <= {"LEFT_RIGHT", "UP_DOWN"}:
                    raise SpecValidationError(
                        f"bed_robot_arm_groups multi cue '{cue_id}' supports LEFT_RIGHT or UP_DOWN only."
                    )
            else:
                raise SpecValidationError(
                    f"bed_robot_arm_groups adjustment cue '{cue_id}' requires single or multi mode."
                )
            if "default_distance_mm" in cue:
                raise SpecValidationError(
                    f"bed_robot_arm_groups cue '{cue_id}' must not define a default distance."
                )

    transition_ids: set[str] = set()
    transitions = _require_list(
        config.get("end_effector_transitions", []),
        "bed_robot_arm_groups end_effector_transitions",
    )
    for raw_transition in transitions:
        transition = _require_mapping(raw_transition, "bed_robot_arm_groups end-effector transition")
        transition_id = str(transition.get("id", "")).strip()
        phase_id = str(transition.get("phase_id", "")).strip()
        group_id = str(transition.get("group_id", "")).strip()
        if not transition_id or transition_id in transition_ids:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition id '{transition_id}' must be non-empty and unique."
            )
        transition_ids.add(transition_id)
        if phase_id not in phase_ids:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' references unknown phase '{phase_id}'."
            )
        if group_id not in enabled_groups:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' references disabled group '{group_id}'."
            )
        if "change_end_effector" not in allowed_by_group[group_id]:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' requires change_end_effector permission."
            )
        if not str(transition.get("from_profile", "")).strip() or not str(
            transition.get("to_profile", "")
        ).strip():
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' requires from_profile and to_profile."
            )
        arm_id = str(transition.get("arm_id", "")).strip()
        target_tool_id = str(transition.get("target_tool_id", "")).strip()
        if arm_id not in BED_ROBOT_ARM_IDS:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' requires arm_id arm_1 or arm_2."
            )
        if target_tool_id not in BED_ROBOT_ARM_TARGET_TOOLS:
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' has unsupported target_tool_id."
            )
        if target_tool_id != str(transition.get("to_profile", "")).strip():
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' target_tool_id must match to_profile."
            )
        utterances = _require_list(
            transition.get("utterances"),
            f"bed_robot_arm_groups transition '{transition_id}' utterances",
        )
        if not utterances or any(not str(item).strip() for item in utterances):
            raise SpecValidationError(
                f"bed_robot_arm_groups transition '{transition_id}' requires non-empty utterances."
            )



def validate_raw_bundle(raw_bundle: dict[str, object]) -> None:
    procedure = _require_mapping(raw_bundle["procedure"], "procedure.yaml")
    instruments = _require_mapping(raw_bundle["instruments"], "instruments.yaml")
    scene_layout = _require_mapping(raw_bundle["scene_layout"], "scene_layout.yaml")
    policy = _require_mapping(raw_bundle["policy"], "policy.yaml")
    simulation_layout = _require_mapping(raw_bundle["simulation_layout"], "simulation_layout.yaml")
    mock_surgeon = _require_mapping(raw_bundle["mock_surgeon"], "mock_surgeon.yaml")
    display_catalog = raw_bundle.get("display_catalog", {})
    _validate_display_catalog(display_catalog)

    if not procedure.get("procedure_id"):
        raise SpecValidationError("procedure.yaml requires procedure_id.")

    phases = _require_list(procedure.get("phases"), "procedure.yaml phases")
    if not phases:
        raise SpecValidationError("procedure.yaml must define at least one phase.")
    phase_ids: set[str] = set()
    for phase in phases:
        phase_map = _require_mapping(phase, "phase entry")
        phase_id = str(phase_map.get("id", "")).strip()
        if not phase_id:
            raise SpecValidationError("Each phase requires a non-empty id.")
        if phase_id in phase_ids:
            raise SpecValidationError(f"Duplicate phase id '{phase_id}'.")
        phase_ids.add(phase_id)
        _require_list(phase_map.get("possible_next"), f"phase '{phase_id}' possible_next")
        _require_list(phase_map.get("expected_instruments"), f"phase '{phase_id}' expected_instruments")
        _require_list(
            phase_map.get("field_deployed_instruments", []),
            f"phase '{phase_id}' field_deployed_instruments",
        )
    default_phase_id = str(procedure.get("default_phase_id", "") or "").strip()
    if default_phase_id and default_phase_id not in phase_ids:
        raise SpecValidationError(
            f"procedure default_phase_id references unknown phase '{default_phase_id}'."
        )

    instrument_entries = _require_list(instruments.get("instruments"), "instruments.yaml instruments")
    if not instrument_entries:
        raise SpecValidationError("instruments.yaml must define at least one instrument.")
    instrument_ids: set[str] = set()
    inventory_counts: dict[str, int] = {}
    for instrument in instrument_entries:
        instrument_map = _require_mapping(instrument, "instrument entry")
        instrument_id = str(instrument_map.get("id", "")).strip()
        if not instrument_id:
            raise SpecValidationError("Each instrument requires a non-empty id.")
        if instrument_id in instrument_ids:
            raise SpecValidationError(f"Duplicate instrument id '{instrument_id}'.")
        instrument_ids.add(instrument_id)
        _require_list(instrument_map.get("aliases", []), f"instrument '{instrument_id}' aliases")
        inventory_count = instrument_map.get("inventory_count", 1)
        if (
            isinstance(inventory_count, bool)
            or not isinstance(inventory_count, int)
            or inventory_count <= 0
        ):
            raise SpecValidationError(
                f"instrument '{instrument_id}' inventory_count must be a positive integer."
            )
        inventory_counts[instrument_id] = inventory_count
        if "requestable" in instrument_map and not isinstance(instrument_map["requestable"], bool):
            raise SpecValidationError(f"instrument '{instrument_id}' requestable must be boolean.")
        if not instrument_map.get("category"):
            raise SpecValidationError(f"instrument '{instrument_id}' requires category.")
        if not instrument_map.get("handover_profile"):
            raise SpecValidationError(f"instrument '{instrument_id}' requires handover_profile.")

    for phase in phases:
        phase_map = _require_mapping(phase, "phase entry")
        phase_id = str(phase_map.get("id", "")).strip()
        for next_phase in phase_map.get("possible_next", []):
            if str(next_phase) not in phase_ids:
                raise SpecValidationError(
                    f"phase '{phase_id}' references unknown next phase '{next_phase}'."
                )
        for instrument_id in phase_map.get("expected_instruments", []):
            if str(instrument_id) not in instrument_ids:
                raise SpecValidationError(
                    f"phase '{phase_id}' references unknown instrument '{instrument_id}'."
                )
        for instrument_id in phase_map.get("field_deployed_instruments", []):
            if str(instrument_id) not in instrument_ids:
                raise SpecValidationError(
                    f"phase '{phase_id}' references unknown field-deployed "
                    f"instrument '{instrument_id}'."
                )

    _validate_bed_robot_arm_groups(raw_bundle.get("bed_robot_arm_groups"), phase_ids)

    location_entries = _require_list(scene_layout.get("locations"), "scene_layout.yaml locations")
    location_ids: set[str] = set()
    location_types: dict[str, str] = {}
    for location in location_entries:
        location_map = _require_mapping(location, "scene location entry")
        location_id = str(location_map.get("id", "")).strip()
        if not location_id:
            raise SpecValidationError("Each scene location requires a non-empty id.")
        if location_id in location_ids:
            raise SpecValidationError(f"Duplicate location id '{location_id}'.")
        location_ids.add(location_id)
        location_type = str(location_map.get("type", "")).strip()
        if not location_type:
            raise SpecValidationError(f"location '{location_id}' requires type.")
        location_types[location_id] = location_type

    placements = _require_list(
        scene_layout.get("initial_instrument_placement"),
        "scene_layout.yaml initial_instrument_placement",
    )
    placed_instruments: set[str] = set()
    for placement in placements:
        placement_map = _require_mapping(placement, "initial placement entry")
        instrument_id = str(placement_map.get("instrument_id", "")).strip()
        location_id = str(placement_map.get("location_id", "")).strip()
        if instrument_id not in instrument_ids:
            raise SpecValidationError(
                f"Initial placement references unknown instrument '{instrument_id}'."
            )
        if location_id not in location_ids:
            raise SpecValidationError(
                f"Initial placement references unknown location '{location_id}'."
            )
        if instrument_id in placed_instruments:
            raise SpecValidationError(
                f"Instrument '{instrument_id}' has more than one initial placement."
            )
        placed_instruments.add(instrument_id)

    missing_placements = sorted(instrument_ids.difference(placed_instruments))
    if missing_placements:
        raise SpecValidationError(
            "scene_layout.yaml must provide an initial placement for every instrument: "
            + ", ".join(missing_placements)
        )

    initial_states = _require_list(
        scene_layout.get("initial_instrument_states", []),
        "scene_layout.yaml initial_instrument_states",
    )
    initial_state_instances: set[str] = set()
    supported_initial_lifecycles = {
        "home_rack",
        "returned_home",
        "surgeon_owned",
        "mayo_reuse",
        "mayo_recovery",
        "prepositioned_right",
        "recovering_left",
        "cleaning_left",
        "cleaned_left",
    }
    for raw_state in initial_states:
        state = _require_mapping(raw_state, "initial instrument state")
        instrument_id = str(state.get("instrument_id", "")).strip()
        instance_id = str(state.get("instance_id", "")).strip()
        location_id = str(state.get("location_id", "")).strip()
        lifecycle_stage = str(state.get("lifecycle_stage", "")).strip()
        if instrument_id not in instrument_ids:
            raise SpecValidationError(
                f"Initial instrument state references unknown instrument '{instrument_id}'."
            )
        expected_prefix = f"{instrument_id}#"
        if not instance_id.startswith(expected_prefix):
            raise SpecValidationError(
                f"Initial instrument state instance '{instance_id}' must start with "
                f"'{expected_prefix}'."
            )
        try:
            instance_index = int(instance_id.removeprefix(expected_prefix))
        except ValueError as exc:
            raise SpecValidationError(
                f"Initial instrument state instance '{instance_id}' has an invalid index."
            ) from exc
        if not 1 <= instance_index <= inventory_counts[instrument_id]:
            raise SpecValidationError(
                f"Initial instrument state instance '{instance_id}' exceeds inventory "
                f"count {inventory_counts[instrument_id]}."
            )
        if instance_id in initial_state_instances:
            raise SpecValidationError(
                f"Duplicate initial instrument state for '{instance_id}'."
            )
        initial_state_instances.add(instance_id)
        if location_id not in location_ids:
            raise SpecValidationError(
                f"Initial instrument state references unknown location '{location_id}'."
            )
        if lifecycle_stage not in supported_initial_lifecycles:
            raise SpecValidationError(
                f"Initial instrument state for '{instance_id}' has unsupported lifecycle "
                f"'{lifecycle_stage}'."
            )
        try:
            confidence = float(state.get("confidence", 1.0))
        except (TypeError, ValueError) as exc:
            raise SpecValidationError(
                f"Initial instrument state for '{instance_id}' confidence must be numeric."
            ) from exc
        if not 0.0 <= confidence <= 1.0:
            raise SpecValidationError(
                f"Initial instrument state for '{instance_id}' confidence must be between 0 and 1."
            )
        if (
            lifecycle_stage == "surgeon_owned"
            and location_types[location_id]
            not in {"surgeon_hand", "surgical_field", "bed_fixed_tool", "return_zone"}
        ):
            raise SpecValidationError(
                f"Initial surgeon-owned instrument '{instance_id}' must use a surgeon "
                "or surgical-field location."
            )

    phase_guard = _require_mapping(policy.get("phase_guard"), "policy.yaml phase_guard")
    action_guard = _require_mapping(policy.get("action_guard"), "policy.yaml action_guard")
    humanoid_policy = _require_mapping(policy.get("humanoid_policy"), "policy.yaml humanoid_policy")

    for key in (
        "min_confidence_to_keep",
        "min_confidence_to_switch",
        "smoothing_window",
        "min_dwell_time_sec",
        "allow_unknown_phase",
    ):
        if key not in phase_guard:
            raise SpecValidationError(f"policy.yaml phase_guard requires '{key}'.")
    if "min_evidence_duration_sec" in phase_guard:
        try:
            min_evidence_duration_sec = float(
                phase_guard["min_evidence_duration_sec"]
            )
        except (TypeError, ValueError) as exc:
            raise SpecValidationError(
                "policy.yaml phase_guard min_evidence_duration_sec must be numeric."
            ) from exc
        if min_evidence_duration_sec < 0.0:
            raise SpecValidationError(
                "policy.yaml phase_guard min_evidence_duration_sec must be non-negative."
            )

    for key in (
        "block_handover_when_phase_uncertain",
        "require_multi_evidence_for_handover",
        "allow_prepositioning_when_uncertain",
        "explicit_request_priority",
    ):
        if key not in action_guard:
            raise SpecValidationError(f"policy.yaml action_guard requires '{key}'.")

    for key in (
        "handover_arm",
        "recovery_arm",
        "require_cleaning_after_surgeon_use",
        "allow_anticipatory_hold",
        "voice_override_preempts_preposition",
        "direct_return_to_rack_for_unused_prepositioned_tool",
    ):
        if key not in humanoid_policy:
            raise SpecValidationError(f"policy.yaml humanoid_policy requires '{key}'.")

    if str(humanoid_policy["handover_arm"]) not in {"left", "right"}:
        raise SpecValidationError("policy.yaml humanoid_policy.handover_arm must be 'left' or 'right'.")
    if str(humanoid_policy["recovery_arm"]) not in {"left", "right"}:
        raise SpecValidationError("policy.yaml humanoid_policy.recovery_arm must be 'left' or 'right'.")

    simulation_entities = _require_list(simulation_layout.get("entities"), "simulation_layout.yaml entities")
    if not simulation_entities:
        raise SpecValidationError("simulation_layout.yaml must define at least one entity.")
    entity_ids: set[str] = set()
    entity_types: set[str] = set()
    for entity in simulation_entities:
        entity_map = _require_mapping(entity, "simulation entity")
        entity_id = str(entity_map.get("id", "")).strip()
        entity_type = str(entity_map.get("type", "")).strip()
        if not entity_id:
            raise SpecValidationError("Each simulation entity requires a non-empty id.")
        if entity_id in entity_ids:
            raise SpecValidationError(f"Duplicate simulation entity id '{entity_id}'.")
        if not entity_type:
            raise SpecValidationError(f"simulation entity '{entity_id}' requires type.")
        entity_ids.add(entity_id)
        entity_types.add(entity_type)
        for key in ("x", "y"):
            if key not in entity_map:
                raise SpecValidationError(f"simulation entity '{entity_id}' requires '{key}'.")

    missing_entity_types = sorted(REQUIRED_SIMULATION_ENTITY_TYPES.difference(entity_types))
    if missing_entity_types:
        raise SpecValidationError(
            "simulation_layout.yaml is missing required entity types: " + ", ".join(missing_entity_types)
        )

    simulation_anchors = _require_list(simulation_layout.get("anchors"), "simulation_layout.yaml anchors")
    if not simulation_anchors:
        raise SpecValidationError("simulation_layout.yaml must define anchors.")
    anchor_ids: set[str] = set()
    for anchor in simulation_anchors:
        anchor_map = _require_mapping(anchor, "simulation anchor")
        anchor_id = str(anchor_map.get("id", "")).strip()
        attached_to = str(anchor_map.get("attached_to", "")).strip()
        if not anchor_id:
            raise SpecValidationError("Each simulation anchor requires a non-empty id.")
        if anchor_id in anchor_ids:
            raise SpecValidationError(f"Duplicate simulation anchor id '{anchor_id}'.")
        if attached_to not in entity_ids:
            raise SpecValidationError(
                f"simulation anchor '{anchor_id}' references unknown entity '{attached_to}'."
            )
        for key in ("x", "y"):
            if key not in anchor_map:
                raise SpecValidationError(f"simulation anchor '{anchor_id}' requires '{key}'.")
        anchor_ids.add(anchor_id)

    missing_anchor_ids = sorted(REQUIRED_SIMULATION_ANCHORS.difference(anchor_ids))
    if missing_anchor_ids:
        raise SpecValidationError(
            "simulation_layout.yaml is missing required anchors: " + ", ".join(missing_anchor_ids)
        )

    mock_perception = raw_bundle.get("mock_perception")
    if mock_perception is not None:
        mock_map = _require_mapping(mock_perception, "mock_perception.yaml")
        period_sec = float(mock_map.get("period_sec", 1.0))
        if period_sec <= 0.0:
            raise SpecValidationError("mock_perception.yaml period_sec must be greater than 0.")

        stages = _require_list(mock_map.get("stages"), "mock_perception.yaml stages")
        if not stages:
            raise SpecValidationError("mock_perception.yaml must define at least one stage.")

        stage_names: set[str] = set()
        for stage in stages:
            stage_map = _require_mapping(stage, "mock perception stage")
            stage_name = str(stage_map.get("name", "")).strip()
            if not stage_name:
                raise SpecValidationError("Each mock perception stage requires a non-empty name.")
            if stage_name in stage_names:
                raise SpecValidationError(f"Duplicate mock perception stage '{stage_name}'.")
            stage_names.add(stage_name)

            if int(stage_map.get("duration_ticks", 0)) <= 0:
                raise SpecValidationError(
                    f"mock perception stage '{stage_name}' requires duration_ticks > 0."
                )
            if not str(stage_map.get("scene_summary", "")).strip():
                raise SpecValidationError(
                    f"mock perception stage '{stage_name}' requires scene_summary."
                )

            uncertainty = float(stage_map.get("uncertainty", 0.0))
            if uncertainty < 0.0 or uncertainty > 1.0:
                raise SpecValidationError(
                    f"mock perception stage '{stage_name}' uncertainty must be between 0 and 1."
                )

            phase_hypotheses = _require_list(
                stage_map.get("phase_hypotheses"),
                f"mock perception stage '{stage_name}' phase_hypotheses",
            )
            if not phase_hypotheses:
                raise SpecValidationError(
                    f"mock perception stage '{stage_name}' must define at least one phase hypothesis."
                )
            for hypothesis in phase_hypotheses:
                hypothesis_map = _require_mapping(hypothesis, "mock phase hypothesis")
                phase_id = str(hypothesis_map.get("phase_id", "")).strip()
                if phase_id not in phase_ids:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' references unknown phase '{phase_id}'."
                    )
                confidence = float(hypothesis_map.get("confidence", -1.0))
                if confidence < 0.0 or confidence > 1.0:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' phase confidence must be between 0 and 1."
                    )

            observations = _require_list(
                stage_map.get("observations"),
                f"mock perception stage '{stage_name}' observations",
            )
            for observation in observations:
                observation_map = _require_mapping(observation, "mock observation")
                instrument_id = str(observation_map.get("instrument_id", "")).strip()
                location_id = str(observation_map.get("location_id", "")).strip()
                location_type = str(observation_map.get("location_type", "")).strip()
                if instrument_id not in instrument_ids:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' references unknown instrument '{instrument_id}'."
                    )
                if location_id not in location_ids:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' references unknown location '{location_id}'."
                    )
                if not location_type:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' requires location_type for each observation."
                    )
                if location_types.get(location_id) != location_type:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' location '{location_id}' expects type "
                        f"'{location_types.get(location_id)}' but got '{location_type}'."
                    )
                confidence = float(observation_map.get("confidence", -1.0))
                if confidence < 0.0 or confidence > 1.0:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' observation confidence must be between 0 and 1."
                    )

            surgeon_gesture = stage_map.get("surgeon_gesture")
            if surgeon_gesture is not None:
                gesture_map = _require_mapping(
                    surgeon_gesture,
                    f"mock perception stage '{stage_name}' surgeon_gesture",
                )
                event_type = str(gesture_map.get("event_type", "")).strip()
                requested_tool = str(gesture_map.get("requested_tool", "")).strip()
                hand_pose = str(gesture_map.get("hand_pose", "")).strip()
                confidence = float(gesture_map.get("confidence", -1.0))
                if event_type not in ALLOWED_SURGEON_GESTURE_EVENTS:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' uses unsupported surgeon_gesture event_type '{event_type}'."
                    )
                if requested_tool not in instrument_ids:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' surgeon_gesture references unknown instrument '{requested_tool}'."
                    )
                if not hand_pose:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' surgeon_gesture requires hand_pose."
                    )
                if confidence < 0.0 or confidence > 1.0:
                    raise SpecValidationError(
                        f"mock perception stage '{stage_name}' surgeon_gesture confidence must be between 0 and 1."
                    )

    surgeon_period_sec = float(mock_surgeon.get("period_sec", 1.0))
    if surgeon_period_sec <= 0.0:
        raise SpecValidationError("mock_surgeon.yaml period_sec must be greater than 0.")
    surgeon_stages = _require_list(mock_surgeon.get("stages"), "mock_surgeon.yaml stages")
    if not surgeon_stages:
        raise SpecValidationError("mock_surgeon.yaml must define at least one stage.")
    surgeon_stage_names: set[str] = set()
    for stage in surgeon_stages:
        stage_map = _require_mapping(stage, "mock surgeon stage")
        stage_name = str(stage_map.get("name", "")).strip()
        phase_id = str(stage_map.get("phase_id", "")).strip()
        event_type = str(stage_map.get("event_type", "")).strip()
        if not stage_name:
            raise SpecValidationError("Each mock surgeon stage requires a non-empty name.")
        if stage_name in surgeon_stage_names:
            raise SpecValidationError(f"Duplicate mock surgeon stage '{stage_name}'.")
        surgeon_stage_names.add(stage_name)
        if phase_id not in phase_ids:
            raise SpecValidationError(
                f"mock surgeon stage '{stage_name}' references unknown phase '{phase_id}'."
            )
        if event_type not in ALLOWED_SURGEON_EVENTS:
            raise SpecValidationError(
                f"mock surgeon stage '{stage_name}' uses unsupported event_type '{event_type}'."
            )
        if int(stage_map.get("duration_ticks", 0)) <= 0:
            raise SpecValidationError(
                f"mock surgeon stage '{stage_name}' requires duration_ticks > 0."
            )
        requested_tool = str(stage_map.get("requested_tool", "")).strip()
        if requested_tool and requested_tool not in instrument_ids:
            raise SpecValidationError(
                f"mock surgeon stage '{stage_name}' references unknown instrument '{requested_tool}'."
            )
        if event_type == "voice_request" and not str(stage_map.get("voice_text", "")).strip():
            raise SpecValidationError(
                f"mock surgeon stage '{stage_name}' requires voice_text for voice_request."
            )
