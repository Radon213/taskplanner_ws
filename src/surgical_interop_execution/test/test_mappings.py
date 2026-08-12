from dataclasses import asdict

import pytest

from surgical_interop_execution.mappings import (
    ADJUSTMENT_MULTI,
    ADJUSTMENT_SINGLE,
    MAX_RETRACTION_DISTANCE_MM,
    DispatchLedger,
    GROUP_RETRACTION,
    OPERATION_CHANGE_END_EFFECTOR,
    OPERATION_RELEASE_RETRACTION,
    OPERATION_RETRACTION,
    PUBLIC_TOOL_LOCATIONS,
    InternalGroupCommand,
    InternalSkillCommand,
    MappingFailure,
    RetractionAdjustmentRequest,
    ToolChangeRequest,
    map_group_command,
    map_skill_to_tool_handover,
    public_instrument_instance_id,
)


def _skill(
    action: str = "pick_up_and_handover", **overrides
) -> InternalSkillCommand:
    values = {
        "command_id": "skill-1",
        "action": action,
        "instrument_id": "T04",
        "instrument_instance_id": "T04#1",
        "source_location_type": "tray_slot",
        "source_location_id": "tray-a-2",
        "target_location_type": "handover_zone",
        "target_location_id": "surgeon_receive_zone",
        "arm": "right",
        "request_generation": 11,
        "rationale": "private planner explanation",
        "target_owner": "surgeon",
        "cleaning_required": True,
        "mode": "explicit_request",
    }
    values.update(overrides)
    return InternalSkillCommand(**values)


@pytest.mark.parametrize(
    "action",
    ["pick_up_and_handover", "tool_handover"],
)
def test_tray_handover_aliases_map_to_tray_to_surgeon(action):
    request = map_skill_to_tool_handover(
        _skill(action),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.command_id == "skill-1"
    assert request.instrument_id == "Bovie surgical cautery"
    assert request.instrument_instance_id == "Bovie surgical cautery#1"
    assert request.source_location == "tray"
    assert request.target_location == "surgeon"


@pytest.mark.parametrize("action", ["direct_handover", "predicted_tool_handover"])
def test_prepared_handover_aliases_map_to_robot_to_surgeon(action):
    request = map_skill_to_tool_handover(
        _skill(action),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.source_location == "robot"
    assert request.target_location == "surgeon"


def test_unused_prepared_tool_maps_to_robot_to_tray():
    request = map_skill_to_tool_handover(
        _skill("return_unused_preposition"),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.source_location == "robot"
    assert request.target_location == "tray"


@pytest.mark.parametrize("action", ["retrieve_from_mayo", "tool_retrieve"])
def test_retrieve_aliases_map_to_mayo_to_tray(action):
    request = map_skill_to_tool_handover(
        _skill(action),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.source_location == "mayo"
    assert request.target_location == "tray"


@pytest.mark.parametrize("action", ["predict_tool", "prepare_tool", "tool_predict"])
def test_prepare_aliases_use_the_same_action_with_tray_to_robot(action):
    request = map_skill_to_tool_handover(
        _skill(action),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.command_id == "skill-1"
    assert request.instrument_id == "Bovie surgical cautery"
    assert request.instrument_instance_id == "Bovie surgical cautery#1"
    assert request.source_location == "tray"
    assert request.target_location == "robot"
    assert "arm" not in asdict(request)


@pytest.mark.parametrize("action", ["predict_tool", "prepare_tool", "tool_predict"])
def test_prepare_aliases_map_mayo_reuse_to_robot(action):
    request = map_skill_to_tool_handover(
        _skill(
            action,
            source_location_type="mayo_reuse_zone",
            source_location_id="mayo_stand",
        ),
        instrument_name="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
    )
    assert request.source_location == "mayo"
    assert request.target_location == "robot"


@pytest.mark.parametrize(
    ("source_location_type", "source_location_id"),
    [
        ("surgical_field", "field"),
        ("mayo_recovery_zone", "mayo_recovery_zone"),
        ("tray_slot", "mayo_stand"),
    ],
)
def test_prepare_rejects_unsafe_or_ambiguous_internal_sources(
    source_location_type, source_location_id
):
    with pytest.raises(MappingFailure, match="invalid_prepare_source_location"):
        map_skill_to_tool_handover(
            _skill(
                "predict_tool",
                source_location_type=source_location_type,
                source_location_id=source_location_id,
            ),
            instrument_name="Bovie surgical cautery",
            instrument_instance_id="Bovie surgical cautery#1",
        )


def test_public_tool_locations_are_fixed_to_four_values():
    assert PUBLIC_TOOL_LOCATIONS == {"tray", "mayo", "robot", "surgeon"}


def test_handover_request_redacts_internal_policy_fields():
    public_fields = asdict(
        map_skill_to_tool_handover(
            _skill(),
            instrument_name="Bovie surgical cautery",
            instrument_instance_id="Bovie surgical cautery#1",
        )
    )
    assert public_fields == {
        "command_id": "skill-1",
        "instrument_id": "Bovie surgical cautery",
        "instrument_instance_id": "Bovie surgical cautery#1",
        "source_location": "tray",
        "target_location": "surgeon",
    }
    assert "rationale" not in public_fields
    assert "mode" not in public_fields
    assert "target_owner" not in public_fields
    assert "cleaning_required" not in public_fields
    assert "request_generation" not in public_fields
    assert "arm" not in public_fields
    assert "source_location_id" not in public_fields
    assert "target_location_id" not in public_fields


@pytest.mark.parametrize(
    "action",
    [
        "retrieve_from_hand",
        "pick_up_from_mayo_and_handover",
        "put_down_and_handover",
        "replace_and_handover",
    ],
)
def test_non_transfer_skill_is_rejected_with_a_stable_code(action):
    with pytest.raises(MappingFailure, match="unsupported_skill_action"):
        map_skill_to_tool_handover(
            _skill(action),
            instrument_name="Bovie surgical cautery",
            instrument_instance_id="Bovie surgical cautery#1",
        )


def test_tool_transfer_requires_a_resolved_public_instrument_identity():
    with pytest.raises(MappingFailure, match="invalid_tool_transfer_command"):
        map_skill_to_tool_handover(
            _skill(),
            instrument_name="",
            instrument_instance_id="",
        )


def test_private_catalog_codes_are_rejected_from_the_public_goal():
    with pytest.raises(MappingFailure, match="private_instrument_code"):
        map_skill_to_tool_handover(
            _skill(),
            instrument_name="T04",
            instrument_instance_id="T04#1",
        )


def test_public_instance_id_replaces_the_private_catalog_prefix():
    assert public_instrument_instance_id(
        internal_instrument_id="T04",
        internal_instance_id="T04#1",
        instrument_name="Bovie surgical cautery",
    ) == "Bovie surgical cautery#1"


def _group(operation: str, **overrides) -> InternalGroupCommand:
    values = {
        "request_id": "request-1",
        "command_id": "group-1",
        "group_id": GROUP_RETRACTION,
        "operation": operation,
        "arm_id": "",
        "target_tool_id": "",
        "adjustment_mode": "single",
        "target_retractor_id": "left_malleable",
        "direction_frame": "surgeon_view",
        "direction": "left",
        "axis": "none",
        "distance_mm": 8.0,
        "end_effector_profile": "",
        "distance_origin": "private_origin",
        "raw_distance_text": "move left eight millimetres",
        "rationale": "private planner explanation",
        "confidence": 0.95,
    }
    values.update(overrides)
    return InternalGroupCommand(**values)


def test_retraction_move_preserves_only_physical_motion_fields():
    request = map_group_command(
        _group(OPERATION_RETRACTION)
    )
    assert request == RetractionAdjustmentRequest(
        command_id="group-1",
        adjustment_mode=ADJUSTMENT_SINGLE,
        target_retractor_id="left_malleable",
        direction_frame="surgeon_view",
        direction="left",
        axis="none",
        distance_mm=8.0,
    )
    assert asdict(request) == {
        "command_id": "group-1",
        "adjustment_mode": "single",
        "target_retractor_id": "left_malleable",
        "direction_frame": "surgeon_view",
        "direction": "left",
        "axis": "none",
        "distance_mm": 8.0,
    }


def test_multi_adjustment_maps_axis_without_guessing_direction():
    request = map_group_command(
        _group(
            OPERATION_RETRACTION,
            adjustment_mode="multi",
            target_retractor_id="both_malleable",
            direction="none",
            axis="up_down",
        )
    )
    assert request == RetractionAdjustmentRequest(
        command_id="group-1",
        adjustment_mode=ADJUSTMENT_MULTI,
        target_retractor_id="both_malleable",
        direction_frame="surgeon_view",
        direction="none",
        axis="up_down",
        distance_mm=8.0,
    )


def test_end_effector_change_maps_to_document_tool_change_service():
    request = map_group_command(
        _group(
            OPERATION_CHANGE_END_EFFECTOR,
            arm_id="arm_2",
            target_tool_id="army_navy_retractor",
            direction="",
            distance_mm=0.0,
        )
    )
    assert request == ToolChangeRequest(
        command_id="group-1",
        arm_id="arm_2",
        target_tool_id="army_navy_retractor",
    )


def test_suction_group_is_explicitly_rejected_at_public_boundary():
    with pytest.raises(MappingFailure, match="suction_arm_removed"):
        map_group_command(
            _group(
                OPERATION_RETRACTION,
                group_id="suction",
            )
        )


def test_release_is_not_in_the_new_public_contract():
    with pytest.raises(MappingFailure, match="unsupported_retraction_operation"):
        map_group_command(
            _group(
                OPERATION_RELEASE_RETRACTION,
                direction="",
                distance_mm=0.0,
            )
        )


def test_single_adjustment_requires_an_explicit_document_target():
    with pytest.raises(MappingFailure, match="invalid_target_retractor"):
        map_group_command(_group(OPERATION_RETRACTION, target_retractor_id=""))


def test_tool_change_rejects_unknown_tool_and_arm_values():
    with pytest.raises(MappingFailure, match="invalid_target_tool"):
        map_group_command(
            _group(
                OPERATION_CHANGE_END_EFFECTOR,
                arm_id="arm_1",
                target_tool_id="wide_retractor",
                direction="",
                distance_mm=0.0,
            )
        )
    with pytest.raises(MappingFailure, match="invalid_arm_id"):
        map_group_command(
            _group(
                OPERATION_CHANGE_END_EFFECTOR,
                arm_id="left_arm",
                target_tool_id="thyroid_retractor",
                direction="",
                distance_mm=0.0,
            )
        )


def test_retraction_requires_a_positive_finite_distance():
    with pytest.raises(MappingFailure, match="invalid_retraction_distance"):
        map_group_command(
            _group(
                OPERATION_RETRACTION,
                distance_mm=0.0,
            )
        )


def test_retraction_distance_is_bounded_by_configured_public_limit():
    assert MAX_RETRACTION_DISTANCE_MM == 30.0
    assert map_group_command(
        _group(
            OPERATION_RETRACTION,
            distance_mm=MAX_RETRACTION_DISTANCE_MM,
            target_retractor_id="right_malleable",
        )
    ).distance_mm == 30.0
    with pytest.raises(MappingFailure, match="invalid_retraction_distance"):
        map_group_command(
            _group(
                OPERATION_RETRACTION,
                distance_mm=MAX_RETRACTION_DISTANCE_MM + 0.1,
                target_retractor_id="right_malleable",
            )
        )


def test_dispatch_ledger_suppresses_a_repeated_command_id():
    ledger = DispatchLedger(max_entries=4)
    assert ledger.reserve("command-1")
    assert not ledger.reserve("command-1")
    assert ledger.reserve("command-2")


def test_dispatch_ledger_suppresses_a_reissued_explicit_request_generation():
    ledger = DispatchLedger(max_entries=4)
    assert ledger.reserve("command-1", explicit_request_generation=12)
    assert not ledger.reserve("command-2", explicit_request_generation=12)
    assert ledger.reserve("command-3", explicit_request_generation=13)


def test_dispatch_ledger_is_bounded_and_evicts_old_ids():
    ledger = DispatchLedger(max_entries=2)
    assert ledger.reserve("command-1")
    assert ledger.reserve("command-2")
    assert ledger.reserve("command-3")
    assert ledger.reserve("command-1")
