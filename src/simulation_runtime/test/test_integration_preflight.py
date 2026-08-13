from types import SimpleNamespace

import simulation_runtime.integration_preflight as preflight_module
from simulation_runtime.integration_preflight import (
    IntegrationPreflightNode,
    evaluate_readiness,
    expected_contract_for_bundle,
    validate_bed_robot_status_layout,
)


def _snapshot(**overrides):
    values = {
        "sentence_publisher_count": 1,
        "require_sentence_publisher": True,
        "tool_handover_server_ready": True,
        "tool_change_service_ready": True,
        "require_tool_change_service": True,
        "retraction_adjustment_server_ready": True,
        "require_retraction_adjustment_server": True,
        "bed_robot_arm_status_valid": True,
        "bed_robot_arm_status_age_sec": 0.1,
        "bed_robot_arm_status_max_age_sec": 3.0,
        "require_bed_robot_arm_status": True,
        "require_perception": False,
        "rfdetr_health": None,
        "rfdetr_age_sec": -1.0,
        "perception_max_age_sec": 3.0,
    }
    values.update(overrides)
    return evaluate_readiness(**values)


def test_sentence_only_runtime_can_start_without_perception() -> None:
    result = _snapshot()
    assert result["ready"] is True
    assert result["missing"] == []
    assert result["checks"] == {
        "contract_configuration": True,
        "surgeon_sentence_publisher": True,
        "tool_handover_action_server": True,
        "tool_change_service": True,
        "retraction_adjustment_action_server": True,
        "bed_robot_arm_status": True,
        "perception_input": True,
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


def test_missing_tool_change_service_fails_closed() -> None:
    result = _snapshot(tool_change_service_ready=False)
    assert result["ready"] is False
    assert result["missing"] == ["tool_change_service"]


def test_missing_retraction_adjustment_action_server_fails_closed() -> None:
    result = _snapshot(retraction_adjustment_server_ready=False)
    assert result["ready"] is False
    assert result["missing"] == ["retraction_adjustment_action_server"]


def test_missing_bed_robot_arm_status_fails_closed() -> None:
    result = _snapshot(bed_robot_arm_status_valid=False)
    assert result["ready"] is False
    assert result["missing"] == ["bed_robot_arm_status"]


def test_stale_bed_robot_arm_status_fails_closed() -> None:
    result = _snapshot(bed_robot_arm_status_age_sec=5.0)
    assert result["ready"] is False
    assert result["missing"] == ["bed_robot_arm_status"]


def test_thyroid_contract_does_not_require_nephrectomy_action() -> None:
    result = _snapshot(
        require_retraction_adjustment_server=False,
        retraction_adjustment_server_ready=False,
    )
    assert result["ready"] is True


def test_nephrectomy_contract_does_not_require_tool_change_service() -> None:
    result = _snapshot(
        require_tool_change_service=False,
        tool_change_service_ready=False,
    )
    assert result["ready"] is True


def test_procedure_without_bed_robot_contract_requires_neither_endpoint() -> None:
    result = _snapshot(
        require_tool_change_service=False,
        tool_change_service_ready=False,
        require_retraction_adjustment_server=False,
        retraction_adjustment_server_ready=False,
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


def test_contract_configuration_mismatch_fails_closed() -> None:
    result = _snapshot(contract_configuration_valid=False)

    assert result["ready"] is False
    assert result["missing"] == ["contract_configuration"]


def test_expected_contract_tracks_bundle_not_launch_default() -> None:
    assert expected_contract_for_bundle("thyroidectomy") == (
        "thyroidectomy",
        True,
        False,
        True,
    )
    assert expected_contract_for_bundle("thyroidectomy_demo") == (
        "thyroidectomy",
        True,
        False,
        True,
    )
    assert expected_contract_for_bundle("nephrectomy") == (
        "nephrectomy",
        False,
        True,
        True,
    )
    assert expected_contract_for_bundle("inguinal_hernia_repair") == (
        "",
        False,
        False,
        False,
    )


def _parameter(name: str, value):
    return SimpleNamespace(name=name, value=value)


def _preflight_contract_state() -> IntegrationPreflightNode:
    node = IntegrationPreflightNode.__new__(IntegrationPreflightNode)
    node._active_bundle = "thyroidectomy"
    node._procedure_type = "thyroidectomy"
    node._require_tool_change_service = True
    node._require_retraction_adjustment_server = False
    node._require_bed_robot_arm_status = True
    node._contract_transitioning = False
    node._bed_robot_status_valid = True
    node._bed_robot_status_received_monotonic = 10.0
    node._bed_robot_status_source_stamp_sec = 9.0
    node._bed_robot_status_revision = 4
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
    node._latest_cv_contract_status = None
    node._latest_cv_contract_monotonic = 0.0
    node._bed_robot_arm_status_max_age_sec = 3.0
    node._tool_handover_client = SimpleNamespace(server_is_ready=lambda: True)
    node._tool_change_client = SimpleNamespace(service_is_ready=lambda: True)
    node._retraction_client = SimpleNamespace(server_is_ready=lambda: True)
    node.count_publishers = lambda _topic: 1
    return node


def test_contract_transition_closes_readiness_and_invalidates_status() -> None:
    node = _preflight_contract_state()

    result = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "thyroidectomy_demo"),
            _parameter("procedure_type", "thyroidectomy"),
            _parameter("require_tool_change_service", True),
            _parameter("require_retraction_adjustment_server", False),
            _parameter("require_bed_robot_arm_status", True),
            _parameter("contract_transitioning", True),
        ]
    )

    assert result.successful is True
    assert node._contract_configuration_valid() is False
    assert node._bed_robot_status_valid is False
    assert node._bed_robot_status_received_monotonic == 0.0
    assert node._bed_robot_status_source_stamp_sec == 0.0
    assert node._bed_robot_status_revision is None


def test_contract_update_rejects_bundle_requirement_mismatch_atomically() -> None:
    node = _preflight_contract_state()

    result = node._on_contract_parameters_changed(
        [
            _parameter("active_bundle", "nephrectomy"),
            _parameter("procedure_type", "thyroidectomy"),
            _parameter("require_tool_change_service", True),
            _parameter("require_retraction_adjustment_server", False),
            _parameter("require_bed_robot_arm_status", True),
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
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=123_000_000_000)
    )
    monkeypatch.setattr(preflight_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(preflight_module.time, "monotonic", lambda: 50.0)

    snapshot = node._snapshot()

    assert snapshot["ready"] is True
    assert snapshot["checks"]["bed_robot_arm_status"] is True
    assert snapshot["details"]["bed_robot_arm_status_age_sec"] == 0.75
    assert snapshot["stamp_sec"] == 123.0
