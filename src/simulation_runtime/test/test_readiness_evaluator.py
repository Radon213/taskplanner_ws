import ast
from copy import deepcopy
from pathlib import Path

from simulation_runtime import readiness_evaluator
from simulation_runtime.readiness_evaluator import (
    evaluate_readiness,
    readiness_service_message,
)


def _external_typed_inputs(**overrides):
    values = {
        "sentence_publisher_count": 1,
        "require_sentence_publisher": True,
        "tool_handover_server_ready": True,
        "require_tool_handover_action_server": True,
        "retraction_service_ready": True,
        "require_retraction_service": True,
        "bed_robot_arm_status_valid": True,
        "bed_robot_arm_status_age_sec": 0.1,
        "bed_robot_arm_status_max_age_sec": 3.0,
        "require_bed_robot_arm_status": False,
        "require_perception": True,
        "rfdetr_health": None,
        "rfdetr_age_sec": -1.0,
        "perception_max_age_sec": 3.0,
        "perception_backend": "external",
        "cv_contract_status": {
            "schema": "taskplanner.cv_external_contract.v1",
            "readiness_state": "READY",
            "ready_for_external_evidence": True,
        },
        "cv_contract_age_sec": 0.1,
        "require_rfdetr_tool_observations": True,
        "rfdetr_cam3_tool_observations_valid": True,
        "rfdetr_cam3_tool_observations_age_sec": 0.1,
        "rfdetr_cam4_tool_observations_valid": False,
        "rfdetr_cam4_tool_observations_age_sec": 0.2,
        "rfdetr_cam4_tool_observations_reason": (
            "rfdetr_cam4_tool_observations_provenance_invalid"
        ),
    }
    values.update(overrides)
    return values


def test_readiness_evaluator_has_no_ros_or_runtime_state_imports() -> None:
    source = Path(readiness_evaluator.__file__).read_text(encoding="utf-8")
    imports = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])

    assert imports == {"__future__", "typing"}


def test_external_typed_readiness_snapshot_is_fixed_and_fail_closed() -> None:
    snapshot = evaluate_readiness(**_external_typed_inputs())

    assert snapshot == {
        "schema": "taskplanner.integration_readiness.v1",
        "ready": False,
        "checks": {
            "contract_configuration": True,
            "surgeon_sentence_publisher": True,
            "tool_handover_action_server": True,
            "retraction_command_service": True,
            "asr_runtime_status": True,
            "perception_input": False,
            "rfdetr_cam3_tool_observations": True,
            "rfdetr_cam4_tool_observations": False,
        },
        "missing": [
            "perception_input",
            "rfdetr_cam4_tool_observations",
        ],
        "checklist": [
            {
                "id": "contract_configuration",
                "required": True,
                "status": "pass",
                "reason": "ready",
                "detail": "Ready",
            },
            {
                "id": "surgeon_sentence_publisher",
                "required": True,
                "status": "pass",
                "reason": "ready",
                "detail": "Ready",
            },
            {
                "id": "tool_handover_action_server",
                "required": True,
                "status": "pass",
                "reason": "ready",
                "detail": "Ready",
            },
            {
                "id": "retraction_command_service",
                "required": True,
                "status": "pass",
                "reason": "ready",
                "detail": "Ready",
            },
            {
                "id": "asr_runtime_status",
                "required": False,
                "status": "pass",
                "reason": "not_required",
                "detail": "Not required for the active procedure",
            },
            {
                "id": "perception_input",
                "required": True,
                "status": "pending",
                "reason": "typed_rfdetr_tool_observations_not_ready",
                "detail": "Waiting for both typed CAM3/CAM4 RF-DETR frames",
            },
            {
                "id": "rfdetr_cam3_tool_observations",
                "required": True,
                "status": "pass",
                "reason": "ready",
                "detail": "Ready",
            },
            {
                "id": "rfdetr_cam4_tool_observations",
                "required": True,
                "status": "fail",
                "reason": "rfdetr_cam4_tool_observations_provenance_invalid",
                "detail": "CAM4 RF-DETR provenance is invalid",
            },
        ],
        "details": {
            "sentence_publisher_count": 1,
            "rfdetr_age_sec": -1.0,
            "robot_endpoint_source": "external",
            "retraction_endpoint_source": "external",
            "retraction_state_machine_suppressed": False,
            "asr_runtime_status_age_sec": -1.0,
            "rfdetr_cam3_tool_observations_age_sec": 0.1,
            "rfdetr_cam4_tool_observations_age_sec": 0.2,
            "perception_backend": "external",
            "typed_rfdetr_tool_location_ready": False,
            "perception_evidence_source": "typed_rfdetr_tool_observations",
            "cv_contract_state": "READY",
            "cv_contract_age_sec": 0.1,
        },
    }
    assert readiness_service_message(snapshot) == (
        "integration not ready: perception_input, "
        "rfdetr_cam4_tool_observations; "
        "perception_input=pending:typed_rfdetr_tool_observations_not_ready; "
        "rfdetr_cam4_tool_observations=fail:"
        "rfdetr_cam4_tool_observations_provenance_invalid"
    )


def test_external_health_cannot_replace_either_required_typed_view() -> None:
    values = _external_typed_inputs(
        rfdetr_health={
            "provider": "pnu_hand_blood",
            "connected": True,
            "status": "ready",
            "semantic_ready": True,
            "cam4_aligned": True,
        },
        rfdetr_age_sec=0.1,
    )
    before = deepcopy(values)

    first = evaluate_readiness(**values)
    second = evaluate_readiness(**values)

    assert first == second
    assert values == before
    assert first["ready"] is False
    assert first["missing"] == [
        "perception_input",
        "rfdetr_cam4_tool_observations",
    ]


def test_both_required_typed_views_admit_the_external_lease() -> None:
    snapshot = evaluate_readiness(
        **_external_typed_inputs(
            rfdetr_cam4_tool_observations_valid=True,
            rfdetr_cam4_tool_observations_reason="",
            rfdetr_source_valid=False,
            rfdetr_source_reason="rfdetr_health_missing",
        )
    )

    assert snapshot["ready"] is True
    assert snapshot["missing"] == []
    assert snapshot["details"]["typed_rfdetr_tool_location_ready"] is True
    assert snapshot["details"]["perception_evidence_source"] == (
        "typed_rfdetr_tool_observations"
    )


def test_typed_view_failure_does_not_blame_legacy_aggregate_health() -> None:
    snapshot = evaluate_readiness(
        **_external_typed_inputs(
            rfdetr_source_valid=False,
            rfdetr_source_reason="rfdetr_health_missing",
        )
    )

    perception = next(
        row for row in snapshot["checklist"] if row["id"] == "perception_input"
    )
    assert perception["reason"] == "typed_rfdetr_tool_observations_not_ready"


def _required_bed_robot_status_snapshot(**overrides):
    return evaluate_readiness(
        **_external_typed_inputs(
            require_bed_robot_arm_status=True,
            rfdetr_cam4_tool_observations_valid=True,
            rfdetr_cam4_tool_observations_reason="",
            **overrides,
        )
    )


def test_required_bed_robot_status_missing_fails_closed() -> None:
    snapshot = _required_bed_robot_status_snapshot(
        bed_robot_arm_status_valid=False,
        bed_robot_arm_status_age_sec=-1.0,
    )

    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["bed_robot_arm_status"]
    row = next(
        item
        for item in snapshot["checklist"]
        if item["id"] == "bed_robot_arm_status"
    )
    assert row == {
        "id": "bed_robot_arm_status",
        "required": True,
        "status": "pending",
        "reason": "bed_robot_arm_status_missing",
        "detail": "Waiting for bed-robot arm status",
    }


def test_required_bed_robot_status_stale_fails_closed() -> None:
    snapshot = _required_bed_robot_status_snapshot(
        bed_robot_arm_status_valid=True,
        bed_robot_arm_status_age_sec=3.001,
    )

    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["bed_robot_arm_status"]
    row = next(
        item
        for item in snapshot["checklist"]
        if item["id"] == "bed_robot_arm_status"
    )
    assert row["status"] == "pending"
    assert row["reason"] == "bed_robot_arm_status_stale"
    assert snapshot["details"]["bed_robot_arm_status_age_sec"] == 3.001


def test_required_bed_robot_status_invalid_fails_closed() -> None:
    snapshot = _required_bed_robot_status_snapshot(
        bed_robot_arm_status_valid=False,
        bed_robot_arm_status_age_sec=0.1,
    )

    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["bed_robot_arm_status"]
    row = next(
        item
        for item in snapshot["checklist"]
        if item["id"] == "bed_robot_arm_status"
    )
    assert row["status"] == "fail"
    assert row["reason"] == "bed_robot_arm_status_invalid"


def test_integration_preflight_preserves_existing_public_imports() -> None:
    facade_path = Path(readiness_evaluator.__file__).with_name(
        "integration_preflight.py"
    )
    tree = ast.parse(facade_path.read_text(encoding="utf-8"))
    reexported = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "readiness_evaluator"
            and node.level == 1
        ):
            reexported.update(alias.asname or alias.name for alias in node.names)

    assert {
        "build_readiness_checklist",
        "evaluate_readiness",
        "readiness_service_message",
    } <= reexported
