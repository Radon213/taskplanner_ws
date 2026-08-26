import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from procedure_spec import load_bundle

import simulation_runtime.integration_preflight as preflight_module
from simulation_runtime.integration_preflight import (
    IntegrationPreflightNode,
    evaluate_asr_runtime_status,
    evaluate_readiness,
    expected_contract_for_bundle,
    rfdetr_tool_observation_source_stamp,
    requires_rfdetr_tool_location_context,
    validate_bed_robot_status_layout,
)


def _snapshot(**overrides):
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
        "require_perception": False,
        "rfdetr_health": None,
        "rfdetr_age_sec": -1.0,
        "perception_max_age_sec": 3.0,
    }
    values.update(overrides)
    return evaluate_readiness(**values)


def _spec_dir(bundle_name: str) -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / bundle_name
    )


def _runtime_requirements(bundle_name: str):
    return load_bundle(
        _spec_dir(bundle_name)
    ).get_scenario_runtime_requirements()


def test_sentence_only_runtime_can_start_without_perception() -> None:
    result = _snapshot()
    assert result["ready"] is True
    assert result["missing"] == []
    assert result["checks"] == {
        "contract_configuration": True,
        "surgeon_sentence_publisher": True,
        "tool_handover_action_server": True,
        "retraction_command_service": True,
        "asr_runtime_status": True,
        "perception_input": True,
        "rfdetr_cam3_tool_observations": True,
        "rfdetr_cam4_tool_observations": True,
    }


def test_sentence_publisher_can_be_optional() -> None:
    result = _snapshot(
        require_sentence_publisher=False,
        sentence_publisher_count=0,
    )
    assert result["ready"] is True
    assert result["checks"]["surgeon_sentence_publisher"] is True


def test_missing_sentence_publisher_fails_closed() -> None:
    result = _snapshot(sentence_publisher_count=0)
    assert result["ready"] is False
    assert result["missing"] == ["surgeon_sentence_publisher"]


def test_missing_tool_handover_action_server_fails_closed() -> None:
    result = _snapshot(tool_handover_server_ready=False)
    assert result["ready"] is False
    assert result["missing"] == ["tool_handover_action_server"]


def test_voice_retraction_only_runtime_does_not_require_tool_action() -> None:
    result = _snapshot(
        require_tool_handover_action_server=False,
        tool_handover_server_ready=False,
    )

    assert result["ready"] is True
    assert result["checks"]["tool_handover_action_server"] is True


def test_missing_retraction_command_service_fails_closed() -> None:
    result = _snapshot(retraction_service_ready=False)
    assert result["ready"] is False
    assert result["missing"] == ["retraction_command_service"]


def _rfdetr_tool_frame(
    *,
    view: str = "cam_3",
    stamp_sec: int = 999,
    stamp_nanosec: int = 900_000_000,
    instances: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=stamp_sec, nanosec=stamp_nanosec)
        ),
        sequence=7,
        schema_version="pnu.tool_observation_2d.v1",
        observation_id="frame-7",
        view=view,
        image_width=1280,
        image_height=720,
        model_version="cam3-rfdetr-tool-v1",
        ontology_version="tools-v1",
        instances=[] if instances is None else instances,
    )


def test_typed_empty_rfdetr_frame_is_valid_no_detection_evidence() -> None:
    source_stamp_sec, reason = rfdetr_tool_observation_source_stamp(
        _rfdetr_tool_frame(instances=[]),
        expected_view="cam_3",
    )

    assert reason == ""
    assert source_stamp_sec == pytest.approx(999.9)


def test_typed_rfdetr_frame_must_match_explicit_model_pin() -> None:
    _stamp, reason = rfdetr_tool_observation_source_stamp(
        _rfdetr_tool_frame(instances=[]),
        expected_view="cam_3",
        expected_model_version="cam4-rfdetr-seg-small-regular-resume-e13-best",
    )

    assert reason == "rfdetr_tool_observations_model_version_mismatch"


def test_exact_pin_admits_checkpoint_derived_model_label() -> None:
    frame = _rfdetr_tool_frame(instances=[])
    frame.model_version = "local-xlarge-best-checkpoint-sha256-2c982736"

    source_stamp_sec, reason = rfdetr_tool_observation_source_stamp(
        frame,
        expected_view="cam_3",
        expected_model_version=frame.model_version,
    )

    assert reason == ""
    assert source_stamp_sec == pytest.approx(999.9)


@pytest.mark.parametrize(
    "model_version",
    (
        "local-xlarge-best-checkpoint-sha256-2c982736",
        "provider-vNext-2026-09",
    ),
)
def test_unpinned_provider_accepts_nonempty_model_versions(
    model_version: str,
) -> None:
    frame = _rfdetr_tool_frame(instances=[])
    frame.model_version = model_version

    source_stamp_sec, reason = rfdetr_tool_observation_source_stamp(
        frame,
        expected_view="cam_3",
    )

    assert reason == ""
    assert source_stamp_sec == pytest.approx(999.9)


def test_unpinned_provider_still_requires_model_version_provenance() -> None:
    frame = _rfdetr_tool_frame(instances=[])
    frame.model_version = ""

    _stamp, reason = rfdetr_tool_observation_source_stamp(
        frame,
        expected_view="cam_3",
    )

    assert reason == "rfdetr_tool_observations_provenance_invalid"


def test_typed_rfdetr_location_requirement_follows_runtime_requirements() -> None:
    assert requires_rfdetr_tool_location_context("thyroidectomy_demo") is True
    assert requires_rfdetr_tool_location_context("THYROIDECTOMY_DEMO") is True
    assert requires_rfdetr_tool_location_context("thyroidectomy") is False
    assert requires_rfdetr_tool_location_context("nephrectomy") is False
    assert requires_rfdetr_tool_location_context(
        replace(
            _runtime_requirements("nephrectomy"),
            rfdetr_tool_observations_required=True,
        )
    ) is True


def test_demo_selection_activates_perception_health_gate() -> None:
    node = _ready_snapshot_node()
    assert node._active_requires_perception() is False

    node._active_bundle = "thyroidectomy_demo"
    node._spec_dir = str(_spec_dir("thyroidectomy_demo"))
    node._scenario_runtime_requirements = _runtime_requirements(
        "thyroidectomy_demo"
    )
    assert node._active_requires_perception() is True

    node._active_bundle = "thyroidectomy"
    node._spec_dir = str(_spec_dir("thyroidectomy"))
    node._scenario_runtime_requirements = _runtime_requirements("thyroidectomy")
    node._require_perception = True
    assert node._active_requires_perception() is True


def test_typed_rfdetr_frame_rejects_wrong_view_or_malformed_instance() -> None:
    _stamp, reason = rfdetr_tool_observation_source_stamp(
        _rfdetr_tool_frame(view="cam_4"),
        expected_view="cam_3",
    )
    assert reason == "rfdetr_tool_observations_view_mismatch"

    malformed = SimpleNamespace(
        frame_local_instance_id=1,
        canonical_class_id=1,
        model_class_index=1,
        class_name="Scalpel",
        class_confidence=0.9,
        bbox_xyxy_px=[10.0, 10.0, 10.0, 20.0],
        observation_point_depth_valid=False,
    )
    _stamp, reason = rfdetr_tool_observation_source_stamp(
        _rfdetr_tool_frame(instances=[malformed]),
        expected_view="cam_3",
    )
    assert reason == "rfdetr_tool_observations_instance_invalid"


def test_required_typed_rfdetr_views_fail_closed_independently_of_health() -> None:
    result = _snapshot(
        require_rfdetr_tool_observations=True,
        rfdetr_cam3_tool_observations_valid=False,
        rfdetr_cam3_tool_observations_reason=(
            "rfdetr_cam3_tool_observations_missing"
        ),
        rfdetr_cam4_tool_observations_valid=False,
        rfdetr_cam4_tool_observations_reason=(
            "rfdetr_cam4_tool_observations_missing"
        ),
    )

    assert result["ready"] is False
    assert result["missing"] == [
        "rfdetr_cam3_tool_observations",
        "rfdetr_cam4_tool_observations",
    ]
    checklist = {item["id"]: item for item in result["checklist"]}
    assert checklist["rfdetr_cam3_tool_observations"] == {
        "id": "rfdetr_cam3_tool_observations",
        "required": True,
        "status": "pending",
        "reason": "rfdetr_cam3_tool_observations_missing",
        "detail": "Waiting for a fresh typed CAM3 RF-DETR frame",
    }


def test_fresh_typed_rfdetr_views_admit_empty_detector_frames() -> None:
    result = _snapshot(
        require_rfdetr_tool_observations=True,
        rfdetr_cam3_tool_observations_valid=True,
        rfdetr_cam3_tool_observations_age_sec=0.1,
        rfdetr_cam4_tool_observations_valid=True,
        rfdetr_cam4_tool_observations_age_sec=0.1,
    )

    assert result["ready"] is True
    assert result["checks"]["rfdetr_cam3_tool_observations"] is True
    assert result["checks"]["rfdetr_cam4_tool_observations"] is True


def test_demo_typed_views_satisfy_vlm_perception_without_overlay_bridge_frame() -> None:
    """The demo's VLM uses typed boxes, not a detector-rendered FLIR frame."""

    result = _snapshot(
        require_perception=True,
        require_rfdetr_tool_observations=True,
        rfdetr_cam3_tool_observations_valid=True,
        rfdetr_cam3_tool_observations_age_sec=0.1,
        rfdetr_cam4_tool_observations_valid=True,
        rfdetr_cam4_tool_observations_age_sec=0.1,
        rfdetr_health={
            "schema": "pnu.rfdetr_health.v2",
            "model_ready": True,
            "state": "waiting_for_frame",
            "cam4_rgb_ready": True,
        },
        rfdetr_age_sec=0.1,
    )

    assert result["ready"] is True
    assert result["checks"]["perception_input"] is True
    assert result["details"]["typed_rfdetr_tool_location_ready"] is True
    assert result["details"]["perception_evidence_source"] == (
        "typed_rfdetr_tool_observations"
    )


def test_external_typed_views_are_the_complete_production_perception_lease() -> None:
    result = _snapshot(
        require_perception=True,
        perception_backend="external",
        require_rfdetr_tool_observations=True,
        rfdetr_cam3_tool_observations_valid=True,
        rfdetr_cam3_tool_observations_age_sec=0.1,
        rfdetr_cam4_tool_observations_valid=True,
        rfdetr_cam4_tool_observations_age_sec=0.1,
        rfdetr_health=None,
        rfdetr_age_sec=-1.0,
        cv_contract_status=None,
        cv_contract_age_sec=-1.0,
    )

    assert result["ready"] is True
    assert result["checks"]["perception_input"] is True
    assert result["details"]["perception_evidence_source"] == (
        "typed_rfdetr_tool_observations"
    )


def test_typed_rfdetr_callback_rejects_replayed_empty_frame(monkeypatch) -> None:
    node = _ready_snapshot_node()
    node._active_bundle = "thyroidectomy_demo"
    node._spec_dir = str(_spec_dir("thyroidectomy_demo"))
    node._scenario_runtime_requirements = _runtime_requirements(
        "thyroidectomy_demo"
    )
    node._require_rfdetr_tool_observations = True
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)
    frame = _rfdetr_tool_frame(instances=[])

    node._on_rfdetr_tool_observations(frame, expected_view="cam_3")
    valid, age_sec, reason = node._rfdetr_tool_observation_readiness(
        view="cam_3"
    )
    assert valid is True
    assert age_sec == pytest.approx(0.1)
    assert reason == ""

    node._on_rfdetr_tool_observations(frame, expected_view="cam_3")
    valid, _age_sec, reason = node._rfdetr_tool_observation_readiness(
        view="cam_3"
    )
    assert valid is False
    assert reason == "rfdetr_cam3_tool_observations_source_stamp_not_monotonic"


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        ({}, "asr_runtime_status_source_stamp_missing"),
        (
            {
                "schema": "taskplanner.asr.status.v1",
                "stamp_sec": 100.0,
                "asr": {
                    "available": False,
                    "connected": False,
                    "state": "UNAVAILABLE",
                    "device_status": "MISSING",
                },
            },
            "asr_runtime_unavailable",
        ),
        (
            {
                "schema": "taskplanner.asr.status.v1",
                "stamp_sec": 90.0,
                "asr": {
                    "available": True,
                    "connected": True,
                    "state": "IDLE",
                    "device_status": "READY",
                },
            },
            "asr_runtime_status_source_stamp_stale",
        ),
    ],
)
def test_live_asr_runtime_status_rejects_absent_unavailable_or_stale_payload(
    payload: dict[str, object], expected_reason: str
) -> None:
    valid, _age_sec, reason = evaluate_asr_runtime_status(
        payload,
        now_sec=100.0,
        max_age_sec=3.0,
        future_tolerance_sec=0.5,
    )

    assert valid is False
    assert reason == expected_reason


def test_live_asr_runtime_status_accepts_fresh_ready_microphone() -> None:
    valid, age_sec, reason = evaluate_asr_runtime_status(
        {
            "schema": "taskplanner.asr.status.v1",
            "stamp_sec": 99.9,
            "asr": {
                "available": True,
                "connected": False,
                "state": "STOPPED",
                "device_status": "READY",
            },
        },
        now_sec=100.0,
        max_age_sec=3.0,
        future_tolerance_sec=0.5,
    )

    assert valid is True
    assert age_sec == pytest.approx(0.1)
    assert reason == ""


def test_required_live_asr_status_blocks_readiness_independently_of_publisher() -> None:
    result = _snapshot(
        require_asr_runtime_status=True,
        asr_runtime_status_valid=False,
    )

    assert result["ready"] is False
    assert result["missing"] == ["asr_runtime_status"]


def test_controller_contract_does_not_gate_integration_start() -> None:
    result = _snapshot(
        require_controller_contract=True,
        controller_contract_valid=False,
        controller_contract_age_sec=-1.0,
    )

    assert result["ready"] is True
    assert result["missing"] == []
    assert "controller_contract" not in result["checks"]


def test_controller_contract_has_no_integration_start_checklist_row() -> None:
    result = _snapshot(
        require_controller_contract=True,
        controller_contract_valid=False,
        controller_contract_reasons=("controller_contract_missing",),
    )

    checklist = {item["id"]: item for item in result["checklist"]}
    assert "controller_contract" not in checklist
    assert result["ready"] is True


def test_service_only_retraction_does_not_require_bed_robot_arm_status() -> None:
    result = _snapshot(bed_robot_arm_status_valid=False)
    assert result["ready"] is True
    assert "bed_robot_arm_status" not in result["checks"]
    assert all(
        item["id"] != "bed_robot_arm_status" for item in result["checklist"]
    )


def test_service_only_retraction_does_not_require_fresh_bed_robot_status() -> None:
    result = _snapshot(bed_robot_arm_status_age_sec=5.0)
    assert result["ready"] is True
    assert "bed_robot_arm_status" not in result["checks"]


def test_procedure_without_bed_robot_contract_does_not_require_service() -> None:
    result = _snapshot(
        require_retraction_service=False,
        retraction_service_ready=False,
        require_bed_robot_arm_status=False,
        bed_robot_arm_status_valid=False,
        bed_robot_arm_status_age_sec=-1.0,
    )
    assert result["ready"] is True


def test_bed_robot_status_layout_matches_documented_procedure() -> None:
    thyroid = [
        SimpleNamespace(
            arm_id="arm_1",
            role="retraction",
            role_instance_id="army_navy",
            state="standby",
            direct_teach_active=False,
        )
    ]
    assert validate_bed_robot_status_layout("thyroidectomy", thyroid)
    assert not validate_bed_robot_status_layout("nephrectomy", thyroid)

    inguinal = [
        SimpleNamespace(
            arm_id="arm_1",
            role="retraction",
            role_instance_id="left_army_navy",
            state="standby",
            direct_teach_active=False,
        ),
        SimpleNamespace(
            arm_id="arm_2",
            role="retraction",
            role_instance_id="right_army_navy",
            state="standby",
            direct_teach_active=False,
        ),
    ]
    assert validate_bed_robot_status_layout("inguinal_hernia_repair", inguinal)
    assert not validate_bed_robot_status_layout("nephrectomy", inguinal)

    invalid = [SimpleNamespace(**{**vars(thyroid[0]), "role": "suction"})]
    assert not validate_bed_robot_status_layout("thyroidectomy", invalid)


def test_real_vlm_runtime_requires_fresh_aligned_perception() -> None:
    result = _snapshot(
        require_perception=True,
        rfdetr_health={
            "connected": True,
            "status": "ready",
            "cam4_aligned": True,
        },
        rfdetr_age_sec=0.2,
    )
    assert result["ready"] is True

    stale = _snapshot(
        require_perception=True,
        rfdetr_health={
            "connected": True,
            "status": "ready",
            "cam4_aligned": True,
        },
        rfdetr_age_sec=5.0,
    )
    assert stale["ready"] is False
    assert stale["missing"] == ["perception_input"]


def test_external_perception_stays_fail_closed_until_adapter_is_explicitly_ready() -> None:
    result = _snapshot(
        require_perception=True,
        perception_backend="external",
        cv_contract_status={
            "schema": "taskplanner.cv_external_contract.v1",
            "readiness_state": "PENDING_EXTERNAL_IDL_AND_ADAPTER",
            "ready_for_external_evidence": False,
        },
        cv_contract_age_sec=0.1,
    )
    assert result["ready"] is False
    assert result["missing"] == ["perception_input"]
    assert result["details"]["cv_contract_state"] == (
        "PENDING_EXTERNAL_IDL_AND_ADAPTER"
    )


def test_external_perception_requires_fresh_explicit_adapter_authorization() -> None:
    result = _snapshot(
        require_perception=True,
        perception_backend="external",
        cv_contract_status={
            "schema": "taskplanner.cv_external_contract.v1",
            "readiness_state": "READY",
            "ready_for_external_evidence": True,
        },
        cv_contract_age_sec=0.1,
    )
    assert result["ready"] is True


def test_external_pnu_accepts_fresh_executed_empty_tool_result() -> None:
    result = _snapshot(
        require_perception=True,
        perception_backend="external",
        rfdetr_health={
            "provider": "pnu_hand_blood",
            "connected": True,
            "status": "ready",
            "semantic_ready": True,
            "cam4_aligned": True,
            "empty_detection_result": True,
            "detection_count": 0,
        },
        rfdetr_age_sec=0.2,
    )
    assert result["ready"] is True
    assert result["details"]["perception_evidence_source"] == (
        "pnu_bridge_health"
    )
    assert result["details"]["empty_detection_result"] is True


def test_external_pnu_can_require_metric_depth_readiness_independently() -> None:
    health = {
        "provider": "pnu_hand_blood",
        "connected": True,
        "status": "ready",
        "semantic_ready": True,
        "cam4_aligned": True,
        "metric_3d_ready": False,
        "metric_3d_reasons": ["depth_scale_unvalidated"],
    }
    rejected = _snapshot(
        require_perception=True,
        require_metric_3d=True,
        perception_backend="external",
        rfdetr_health=health,
        rfdetr_age_sec=0.2,
    )
    assert rejected["ready"] is False
    assert rejected["missing"] == ["perception_input"]
    assert rejected["details"]["metric_3d_required"] is True
    assert rejected["details"]["metric_3d_reasons"] == [
        "depth_scale_unvalidated"
    ]

    health["metric_3d_ready"] = True
    health["metric_3d_reasons"] = []
    accepted = _snapshot(
        require_perception=True,
        require_metric_3d=True,
        perception_backend="external",
        rfdetr_health=health,
        rfdetr_age_sec=0.2,
    )
    assert accepted["ready"] is True
    assert accepted["details"]["metric_3d_ready"] is True


@pytest.mark.parametrize(
    ("health_override", "age_sec"),
    [
        ({"status": "partial_ready", "semantic_ready": False}, 0.2),
        ({"connected": False}, 0.2),
        ({}, 5.0),
    ],
)
def test_external_pnu_rejects_partial_disconnected_or_stale_results(
    health_override: dict[str, object], age_sec: float
) -> None:
    health = {
        "provider": "pnu_hand_blood",
        "connected": True,
        "status": "ready",
        "semantic_ready": True,
        "cam4_aligned": True,
        "empty_detection_result": False,
    }
    health.update(health_override)
    result = _snapshot(
        require_perception=True,
        perception_backend="external",
        rfdetr_health=health,
        rfdetr_age_sec=age_sec,
        cv_contract_status={
            "schema": "taskplanner.cv_external_contract.v1",
            "readiness_state": "READY",
            "ready_for_external_evidence": True,
        },
        cv_contract_age_sec=0.1,
    )
    assert result["ready"] is False
    assert result["missing"] == ["perception_input"]


def test_contract_configuration_mismatch_fails_closed() -> None:
    result = _snapshot(contract_configuration_valid=False)

    assert result["ready"] is False
    assert result["missing"] == ["contract_configuration"]


def test_virtual_endpoint_source_rejects_public_controller_endpoint_names() -> None:
    assert not preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="virtual",
        tool_handover_action_name="/surgery/tool_handover",
        retraction_service_name="/integration/virtual/surgery/retraction/command",
        require_bed_robot_arm_status=False,
    )
    assert not preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="virtual",
        tool_handover_action_name="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/surgery/retraction/command",
        require_bed_robot_arm_status=False,
    )
    assert preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="virtual",
        tool_handover_action_name="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/integration/virtual/surgery/retraction/command",
        require_bed_robot_arm_status=False,
    )
    assert not preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="external",
        tool_handover_action_name="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/surgery/retraction/command",
        require_bed_robot_arm_status=False,
    )
    assert preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="virtual",
        tool_handover_action_name="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/integration/virtual/surgery/retraction/command",
        require_bed_robot_arm_status=False,
        # Contract-topic routing is diagnostic only and no longer changes
        # integration-start endpoint admission.
        controller_contract_topic="/surgery/controller_contract",
        require_controller_contract=True,
    )


def test_retraction_state_machine_suppression_rejects_external_source() -> None:
    assert not preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="external",
        tool_handover_action_name="/surgery/tool_handover",
        retraction_service_name="/surgery/retraction/command",
        require_bed_robot_arm_status=False,
        retraction_state_machine_suppressed=True,
    )


@pytest.mark.parametrize(
    "active_bundle",
    ("thyroidectomy_demo", "inguinal_hernia_repair_demo"),
)
def test_scenario_policy_can_disable_external_retraction_workflow_ordering(
    active_bundle: str,
) -> None:
    assert preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="external",
        retraction_endpoint_source="external",
        tool_handover_action_name="/surgery/tool_handover",
        retraction_service_name="/surgery/retraction/command",
        require_bed_robot_arm_status=False,
        retraction_state_machine_suppressed=True,
        active_bundle=active_bundle,
    )


def test_external_workflow_choice_is_not_limited_to_demo_bundle_names() -> None:
    runtime = replace(
        _runtime_requirements("nephrectomy"),
        retraction_workflow_state_enforced=False,
    )

    assert preflight_module._is_valid_robot_endpoint_configuration(
        robot_endpoint_source="external",
        retraction_endpoint_source="external",
        tool_handover_action_name="/surgery/tool_handover",
        retraction_service_name="/surgery/retraction/command",
        require_bed_robot_arm_status=False,
        retraction_state_machine_suppressed=True,
        active_bundle="nephrectomy",
        scenario_runtime_requirements=runtime,
    )


def test_expected_contract_tracks_bundle_not_launch_default() -> None:
    assert expected_contract_for_bundle("thyroidectomy") == (
        "thyroidectomy",
        True,
        True,
        False,
    )
    assert expected_contract_for_bundle("thyroidectomy_demo") == (
        "thyroidectomy",
        True,
        True,
        False,
    )
    assert expected_contract_for_bundle("nephrectomy") == (
        "nephrectomy",
        True,
        True,
        False,
    )
    assert expected_contract_for_bundle("inguinal_hernia_repair_demo") == (
        "inguinal_hernia_repair",
        False,
        True,
        False,
    )
    assert expected_contract_for_bundle("inguinal_hernia_repair") == (
        "",
        True,
        False,
        False,
    )


def _parameter(name: str, value):
    return SimpleNamespace(name=name, value=value)


def _preflight_contract_state() -> IntegrationPreflightNode:
    node = IntegrationPreflightNode.__new__(IntegrationPreflightNode)
    node._active_bundle = "thyroidectomy"
    node._procedure_type = "thyroidectomy"
    node._require_tool_handover_action_server = True
    node._require_retraction_service = True
    node._require_bed_robot_arm_status = False
    node._robot_endpoint_source = "external"
    node._retraction_state_machine_suppressed = False
    node._require_rfdetr_tool_observations = False
    node._require_controller_contract = False
    node._controller_contract_topic = "/surgery/controller_contract"
    node._expected_controller_contract_id = "eir-nuc-tool-handover.real.v1"
    node._expected_capability_policy_id = "eir-nuc-tool-handover.v1"
    node._require_physical_stop_confirmation = False
    node._controller_contract_max_age_sec = 3.0
    node._spec_dir = str(_spec_dir("thyroidectomy"))
    node._scenario_runtime_requirements = _runtime_requirements("thyroidectomy")
    node._tool_handover_action_name = "/surgery/tool_handover"
    node._retraction_service_name = "/surgery/retraction/command"
    node._contract_transitioning = False
    node._bed_robot_status_valid = True
    node._bed_robot_status_received_monotonic = 10.0
    node._bed_robot_status_source_stamp_sec = 9.0
    node._bed_robot_status_revision = 4
    node._sentence_topic = "/sensors/surgeon/sentence"
    node._speech_source_topic = node._sentence_topic
    node._asr_runtime_status_topic = "/input/asr/runtime_status"
    node._require_asr_runtime_status = False
    node._asr_runtime_status_max_age_sec = 3.0
    node._asr_runtime_status_source_future_tolerance_sec = 0.5
    node._latest_asr_runtime_status = None
    node._latest_asr_runtime_status_monotonic = 0.0
    node._latest_asr_runtime_status_source_error = ""
    node._last_accepted_asr_runtime_status_source_stamp_sec = 0.0
    node._perception_source_future_tolerance_sec = 0.5
    node._latest_rfdetr_source_error = ""
    node._last_accepted_rfdetr_source_stamp_sec = 0.0
    node._latest_cv_contract_source_error = ""
    node._last_accepted_cv_contract_source_stamp_sec = 0.0
    node._latest_controller_contract = None
    node._latest_controller_contract_monotonic = 0.0
    node._latest_controller_contract_source_error = ""
    node._last_accepted_controller_contract_source_stamp_sec = 0.0
    return node


def _thyroid_heartbeat(*, stamp_sec: float, revision: int):
    whole_seconds = int(stamp_sec)
    nanoseconds = int(round((stamp_sec - whole_seconds) * 1e9))
    return SimpleNamespace(
        stamp=SimpleNamespace(sec=whole_seconds, nanosec=nanoseconds),
        revision=revision,
        procedure_type="thyroidectomy",
        arms=[
            SimpleNamespace(
                arm_id="arm_1",
                role="retraction",
                role_instance_id="army_navy",
                state="standby",
                direct_teach_active=False,
            )
        ],
    )


def _ready_snapshot_node() -> IntegrationPreflightNode:
    node = _preflight_contract_state()
    node._sentence_topic = "/sensors/surgeon/sentence"
    node._require_sentence_publisher = True
    node._require_perception = False
    node._perception_backend = "local"
    node._perception_max_age_sec = 3.0
    node._latest_rfdetr_health = None
    node._latest_rfdetr_monotonic = 0.0
    node._latest_rfdetr_source_error = ""
    node._latest_cv_contract_status = None
    node._latest_cv_contract_monotonic = 0.0
    node._latest_cv_contract_source_error = ""
    node._bed_robot_arm_status_max_age_sec = 3.0
    node._tool_handover_client = SimpleNamespace(server_is_ready=lambda: True)
    node._retraction_client = SimpleNamespace(service_is_ready=lambda: True)
    node.count_publishers = lambda _topic: 1
    return node


def test_execution_route_preflight_ack_requires_exact_revision_and_init_barrier() -> None:
    node = IntegrationPreflightNode.__new__(IntegrationPreflightNode)
    node._route_state_selected_source = "virtual"
    node._route_state_revision = 7
    node._route_state_initialization_revision = 12
    node._route_state_initialization_state = "initializing"
    node._route_state_initialized = False

    def acknowledge(require_initialized: bool):
        return node._handle_execution_route_preflight_ack(
            SimpleNamespace(
                operation="execution_route_preflight_ack",
                payload_json=json.dumps(
                    {
                        "source": "virtual",
                        "revision": 7,
                        "initialization_revision": 12,
                        "require_initialized": require_initialized,
                    }
                ),
            ),
            SimpleNamespace(),
        )

    assert acknowledge(False).accepted is True
    assert acknowledge(True).accepted is False

    node._route_state_initialization_state = "initialized"
    node._route_state_initialized = True
    accepted = acknowledge(True)
    assert accepted.accepted is True
    assert json.loads(accepted.result_json) == {
        "initialization_revision": 12,
        "initialization_state": "initialized",
        "initialized": True,
        "revision": 7,
        "schema": "taskplanner.execution_route_preflight_ack.v1",
        "selected_source": "virtual",
        "retraction_source": "virtual",
    }

    mismatch = node._handle_execution_route_preflight_ack(
        SimpleNamespace(
            operation="execution_route_preflight_ack",
            payload_json='{"source":"virtual","revision":8,"initialization_revision":12,"require_initialized":true}',
        ),
        SimpleNamespace(),
    )
    assert mismatch.accepted is False


def test_route_without_controller_contract_metadata_can_initialize_and_ack() -> None:
    node = IntegrationPreflightNode.__new__(IntegrationPreflightNode)
    node._virtual_tool_handover_action_name = (
        "/integration/virtual/surgery/tool_handover"
    )
    node._external_tool_handover_action_name = "/surgery/tool_handover"
    node._virtual_retraction_service_name = (
        "/integration/virtual/surgery/retraction/command"
    )
    node._external_retraction_service_name = "/surgery/retraction/command"
    node._external_require_physical_stop_confirmation = True
    node._route_state_revision = -1
    node._route_state_initialization_revision = -1
    node._route_state_selected_source = ""
    node._route_state_retraction_source = ""
    node._route_state_initialization_state = ""
    node._route_state_initialized = False
    applied = []
    warnings = []
    node.get_logger = lambda: SimpleNamespace(warning=warnings.append)
    node._apply_execution_route_source = (
        lambda source, *, retraction_source, invalidate: applied.append(
            (source, retraction_source, invalidate)
        )
    )
    node._on_execution_route_state(
        SimpleNamespace(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_route_state.v1",
                    "revision": 8,
                    "initialization_revision": 13,
                    "selected_source": "virtual",
                    "run_endpoint_source": "",
                    "retraction_source": "virtual",
                    "run_retraction_source": "",
                    "initialization_state": "initialized",
                    "tool_handover_endpoint": (
                        "/integration/virtual/surgery/tool_handover"
                    ),
                    "retraction_service_name": (
                        "/integration/virtual/surgery/retraction/command"
                    ),
                    "require_bed_robot_status": False,
                    "require_physical_stop_confirmation": False,
                    "retraction_state_machine_suppressed": True,
                }
            )
        )
    )

    response = node._handle_execution_route_preflight_ack(
        SimpleNamespace(
            operation="execution_route_preflight_ack",
            payload_json=json.dumps(
                {
                    "source": "virtual",
                    "retraction_source": "virtual",
                    "revision": 8,
                    "initialization_revision": 13,
                    "require_initialized": True,
                }
            ),
        ),
        SimpleNamespace(),
    )

    assert warnings == []
    assert applied == [("virtual", "virtual", True)]
    assert node._route_state_initialized is True
    assert response.accepted is True


def test_same_route_revision_refreshes_scenario_retraction_suppression() -> None:
    """A stopped bundle switch still needs matching bridge authority."""

    node = _ready_snapshot_node()
    node._enable_runtime_route_control = True
    node._retraction_endpoint_source = "external"
    node._external_tool_handover_action_name = "/surgery/tool_handover"
    node._virtual_tool_handover_action_name = (
        "/integration/virtual/surgery/tool_handover"
    )
    node._external_retraction_service_name = "/surgery/retraction/command"
    node._virtual_retraction_service_name = (
        "/integration/virtual/surgery/retraction/command"
    )
    node._external_require_physical_stop_confirmation = False
    node._route_state_revision = 8
    node._route_state_initialization_revision = 13
    node._route_state_selected_source = "external"
    node._route_state_retraction_source = "external"
    node._route_state_initialization_state = "initialized"
    node._route_state_initialized = True
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(warning=warnings.append)

    def publish_route(*, suppressed: bool) -> None:
        node._on_execution_route_state(
            SimpleNamespace(
                data=json.dumps(
                    {
                        "schema": "taskplanner.execution_route_state.v1",
                        "revision": 8,
                        "initialization_revision": 13,
                        "selected_source": "external",
                        "run_endpoint_source": "",
                        "retraction_source": "external",
                        "run_retraction_source": "",
                        "initialization_state": "initialized",
                        "tool_handover_endpoint": "/surgery/tool_handover",
                        "retraction_service_name": (
                            "/surgery/retraction/command"
                        ),
                        "require_bed_robot_status": False,
                        "require_physical_stop_confirmation": False,
                        "retraction_state_machine_suppressed": suppressed,
                    }
                )
            )
        )

    assert node._snapshot()["ready"] is True

    # The inguinal demo chooses not to enforce Taskplanner's local workflow
    # ordering.  Merely changing the stopped scenario closes readiness: the
    # preflight node still needs the bridge's matching authoritative route
    # projection even though its route revision and sources do not change.
    selected = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "inguinal_hernia_repair_demo"),
            _parameter(
                "spec_dir",
                str(_spec_dir("inguinal_hernia_repair_demo")),
            ),
            _parameter("procedure_type", "inguinal_hernia_repair"),
            _parameter("require_tool_handover_action_server", False),
            _parameter("require_retraction_service", True),
            _parameter("require_bed_robot_arm_status", False),
            _parameter("contract_transitioning", False),
        ]
    )
    assert selected.successful is True
    assert node._snapshot()["ready"] is False

    publish_route(suppressed=True)
    assert warnings == []
    assert node._route_state_revision == 8
    assert node._route_state_initialization_revision == 13
    assert node._retraction_state_machine_suppressed is True
    assert node._snapshot()["ready"] is True

    # Switching back at the exact same route revision must fail closed on the
    # old suppression value, then recover when the bridge publishes the value
    # matching the newly active enforced-workflow scenario.
    selected = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "thyroidectomy"),
            _parameter("spec_dir", str(_spec_dir("thyroidectomy"))),
            _parameter("procedure_type", "thyroidectomy"),
            _parameter("require_tool_handover_action_server", True),
            _parameter("require_retraction_service", True),
            _parameter("require_bed_robot_arm_status", False),
            _parameter("contract_transitioning", False),
        ]
    )
    assert selected.successful is True
    assert node._snapshot()["ready"] is False

    publish_route(suppressed=True)
    assert node._route_state_initialized is False
    assert node._snapshot()["ready"] is False

    publish_route(suppressed=False)
    assert node._route_state_revision == 8
    assert node._route_state_initialization_revision == 13
    assert node._route_state_initialized is True
    assert node._retraction_state_machine_suppressed is False
    assert node._snapshot()["ready"] is True


def _asr_runtime_status(*, stamp_sec: float, available: bool = True) -> dict:
    return {
        "schema": "taskplanner.asr.status.v1",
        "stamp_sec": stamp_sec,
        "asr": {
            "available": available,
            "connected": True,
            "state": "IDLE",
            "device_status": "READY",
        },
    }


def _rfdetr_health_v2(*, stamp_sec: int, stamp_nanosec: int = 0) -> dict:
    return {
        "schema": "pnu.rfdetr_health.v2",
        "stamp_sec": stamp_sec,
        "stamp_nanosec": stamp_nanosec,
        "state": "ready",
        "model_ready": True,
        "cam4_rgb_ready": True,
    }


def _cv_contract_status(*, stamp_sec: float) -> dict:
    return {
        "schema": "taskplanner.cv_external_contract.v1",
        "stamp_sec": stamp_sec,
        "readiness_state": "READY",
        "ready_for_external_evidence": True,
    }


def test_live_asr_replay_disarms_fresh_preflight_status(monkeypatch) -> None:
    node = _ready_snapshot_node()
    node._require_asr_runtime_status = True
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)
    payload = _asr_runtime_status(stamp_sec=999.9)

    node._on_asr_runtime_status(SimpleNamespace(data=json.dumps(payload)))
    assert node._snapshot()["ready"] is True

    node._on_asr_runtime_status(SimpleNamespace(data=json.dumps(payload)))
    snapshot = node._snapshot()
    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["asr_runtime_status"]
    assert snapshot["details"]["asr_runtime_status_reason"] == (
        "asr_runtime_status_source_stamp_not_monotonic"
    )


def test_live_perception_v2_source_stamp_requires_fresh_monotonic_evidence(
    monkeypatch,
) -> None:
    node = _ready_snapshot_node()
    node._require_perception = True
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)
    payload = _rfdetr_health_v2(stamp_sec=999, stamp_nanosec=900_000_000)

    node._on_rfdetr_health(SimpleNamespace(data=json.dumps(payload)))
    assert node._snapshot()["ready"] is True

    node._on_rfdetr_health(SimpleNamespace(data=json.dumps(payload)))
    snapshot = node._snapshot()
    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["perception_input"]
    assert snapshot["details"]["rfdetr_source_reason"] == (
        "rfdetr_health_source_stamp_not_monotonic"
    )


def test_live_perception_rejects_stale_source_even_when_receipt_is_fresh(
    monkeypatch,
) -> None:
    node = _ready_snapshot_node()
    node._require_perception = True
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)

    node._on_rfdetr_health(
        SimpleNamespace(data=json.dumps(_rfdetr_health_v2(stamp_sec=990)))
    )

    snapshot = node._snapshot()
    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["perception_input"]
    assert snapshot["details"]["rfdetr_source_reason"] == (
        "rfdetr_health_source_stamp_stale"
    )


def test_external_cv_contract_replay_cannot_extend_perception_admission(
    monkeypatch,
) -> None:
    node = _ready_snapshot_node()
    node._require_perception = True
    node._perception_backend = "external"
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)
    payload = _cv_contract_status(stamp_sec=999.9)

    node._on_cv_contract_status(SimpleNamespace(data=json.dumps(payload)))
    assert node._snapshot()["ready"] is True

    node._on_cv_contract_status(SimpleNamespace(data=json.dumps(payload)))
    snapshot = node._snapshot()
    assert snapshot["ready"] is False
    assert snapshot["missing"] == ["perception_input"]
    assert snapshot["details"]["cv_contract_source_reason"] == (
        "cv_contract_status_source_stamp_not_monotonic"
    )


def test_contract_transition_closes_readiness_and_invalidates_status() -> None:
    node = _preflight_contract_state()
    node._latest_rfdetr_tool_observations = {
        "cam_3": {"source_stamp_sec": 10.0},
        "cam_4": {"source_stamp_sec": 10.0},
    }
    node._latest_rfdetr_tool_observations_monotonic = {
        "cam_3": 10.0,
        "cam_4": 10.0,
    }
    node._latest_rfdetr_tool_observations_source_error = {
        "cam_3": "old_error",
        "cam_4": "old_error",
    }
    node._last_accepted_rfdetr_tool_observations_source_stamp_sec = {
        "cam_3": 10.0,
        "cam_4": 10.0,
    }
    node._latest_rfdetr_vlm_alignment = {"cam_3": "aligned"}
    node._latest_rfdetr_vlm_alignment_monotonic = 10.0
    node._latest_rfdetr_vlm_alignment_error = "old_error"

    result = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "thyroidectomy_demo"),
            _parameter("spec_dir", str(_spec_dir("thyroidectomy_demo"))),
            _parameter("procedure_type", "thyroidectomy"),
            _parameter("require_retraction_service", True),
            _parameter("require_bed_robot_arm_status", False),
            _parameter("contract_transitioning", True),
        ]
    )

    assert result.successful is True
    assert node._contract_configuration_valid() is False
    assert node._bed_robot_status_valid is False
    assert node._bed_robot_status_received_monotonic == 0.0
    assert node._bed_robot_status_source_stamp_sec == 0.0
    assert node._bed_robot_status_revision is None
    assert node._latest_rfdetr_tool_observations == {
        "cam_3": None,
        "cam_4": None,
    }
    assert node._latest_rfdetr_tool_observations_monotonic == {
        "cam_3": 0.0,
        "cam_4": 0.0,
    }
    assert node._latest_rfdetr_vlm_alignment == {}
    assert node._latest_rfdetr_vlm_alignment_monotonic == 0.0


def test_contract_update_rejects_bundle_requirement_mismatch_atomically() -> None:
    node = _preflight_contract_state()

    result = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "nephrectomy"),
            _parameter("spec_dir", str(_spec_dir("nephrectomy"))),
            _parameter("procedure_type", "thyroidectomy"),
            _parameter("require_retraction_service", True),
            _parameter("require_bed_robot_arm_status", False),
        ]
    )

    assert result.successful is False
    assert "contract mismatch" in result.reason
    assert node._active_bundle == "thyroidectomy"
    assert node._procedure_type == "thyroidectomy"
    assert node._bed_robot_status_valid is True


def test_bed_robot_status_receive_uses_wall_clock_not_replay_clock(
    monkeypatch,
) -> None:
    node = _preflight_contract_state()
    node._invalidate_bed_robot_status()
    node.get_clock = lambda: (_ for _ in ()).throw(
        AssertionError("heartbeat receive must not consult the replay clock")
    )
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 42.0)

    node._on_bed_robot_arm_status(
        _thyroid_heartbeat(stamp_sec=999.75, revision=1)
    )

    assert node._bed_robot_status_valid is True
    assert node._bed_robot_status_received_monotonic == 42.0
    assert node._bed_robot_status_source_stamp_sec == 999.75
    assert node._bed_robot_status_revision == 1


def test_bed_robot_status_rejects_identical_stamp_with_newer_revision(
    monkeypatch,
) -> None:
    node = _preflight_contract_state()
    node._bed_robot_status_source_stamp_sec = 999.75
    node._bed_robot_status_revision = 4
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)

    node._on_bed_robot_arm_status(
        _thyroid_heartbeat(stamp_sec=999.75, revision=5)
    )

    assert node._bed_robot_status_valid is False
    assert node._bed_robot_status_source_stamp_sec == 999.75
    assert node._bed_robot_status_revision == 4


def test_bed_robot_status_snapshot_age_uses_wall_clock(
    monkeypatch,
) -> None:
    node = _ready_snapshot_node()
    node._bed_robot_status_received_monotonic = 49.5
    node._bed_robot_status_source_stamp_sec = 999.25
    node.get_clock = lambda: (_ for _ in ()).throw(
        AssertionError("readiness lease must not use replay clock")
    )
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)

    snapshot = node._snapshot()

    assert snapshot["ready"] is True
    assert "bed_robot_arm_status" not in snapshot["checks"]
    assert "bed_robot_arm_status_age_sec" not in snapshot["details"]
    assert snapshot["stamp_sec"] == 1_000.0


def test_required_bed_robot_status_snapshot_uses_observed_lease(
    monkeypatch,
) -> None:
    node = _ready_snapshot_node()
    node._require_bed_robot_arm_status = True
    node._scenario_runtime_requirements = replace(
        node._scenario_runtime_requirements,
        bed_robot_status_required=True,
    )
    node._bed_robot_status_valid = True
    node._bed_robot_status_received_monotonic = 49.5
    node._bed_robot_status_source_stamp_sec = 999.25
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)

    fresh = node._snapshot()

    assert fresh["ready"] is True
    assert fresh["checks"]["bed_robot_arm_status"] is True
    assert fresh["details"]["bed_robot_arm_status_age_sec"] == 0.75

    node._bed_robot_status_valid = False
    invalid = node._snapshot()

    assert invalid["ready"] is False
    assert invalid["missing"] == ["bed_robot_arm_status"]
    row = next(
        item
        for item in invalid["checklist"]
        if item["id"] == "bed_robot_arm_status"
    )
    assert row["reason"] == "bed_robot_arm_status_invalid"
