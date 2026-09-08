"""Focused regression coverage for the DT-owned n-gram tool policy.

The file keeps its historic name because it guards the same VLM ingress: VLM
schema rows are still accepted for observability, but no longer affect the
automatic prepare/recover policy.
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from or_digital_twin.models import ActiveRobotTask
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import (
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_SURGEON_OWNED,
    ORDigitalTwin,
)
from procedure_spec import load_bundle, load_frozen_handover_ngram_prior
from surgical_msgs.msg import TwinEvent


SPEC_DIR = (
    Path(__file__).parents[2]
    / "procedure_spec"
    / "procedure_spec"
    / "specs"
    / "thyroidectomy_demo"
)


def _running_twin(phase_id: str = "P03") -> ORDigitalTwin:
    twin = ORDigitalTwin(load_bundle(SPEC_DIR))
    twin.set_initial_phase(phase_id)
    twin.state.running = True
    twin.state.execution_state = "running"
    twin.state.procedure_run_id = "test-run"
    twin.state.cam4_mayo_hand_present = False
    return twin


def _node(
    twin: ORDigitalTwin,
    *,
    history: tuple[str, ...] = (),
    prepare_threshold: float = 0.125,
    recovery_threshold: float = 0.391,
    recovery_enabled_tools: frozenset[str] = frozenset({"T02", "T08"}),
) -> ORDigitalTwinNode:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = twin
    node._handover_ngram_prior = load_frozen_handover_ngram_prior(
        twin.spec,
        SPEC_DIR,
    )
    node._ngram_prepare_probability_threshold = prepare_threshold
    node._ngram_recovery_probability_threshold = recovery_threshold
    node._ngram_recovery_enabled_tools = recovery_enabled_tools
    node._ngram_policy_stability_sec = 0.30
    node._ngram_preparation_stability = {}
    node._ngram_recovery_stability = {}
    node._completed_handover_tools_by_phase = {
        twin.state.filtered_phase: set(history)
    }
    node._runtime_prior_evidence = lambda: {
        "current_phase": twin.state.filtered_phase,
        "completed_handovers": [
            {"tool": tool_id} for tool_id in history
        ],
    }
    return node


def _state(twin: ORDigitalTwin, tool_id: str):
    return next(
        state
        for state in twin.instrument_states.values()
        if state.instrument_id == tool_id
    )


def _controller_handover_event(
    *,
    final_state: str,
    command_id: str,
    stamp_sec: int,
) -> TwinEvent:
    event = TwinEvent()
    event.event_type = "ToolHandoverCompleted"
    event.instrument_id = "T02"
    event.instance_id = "T02#1"
    event.status = "completed"
    event.source_location_type = "tray"
    event.source_location_id = "tray"
    event.target_location_type = "surgeon"
    event.target_location_id = "surgeon"
    event.stamp.sec = stamp_sec
    event.detail_json = json.dumps(
        {
            "authoritative_controller_completion": True,
            "command_id": command_id,
            "controller_final_state": final_state,
            "controller_source_location": "tray",
            "controller_target_location": "surgeon",
            "controller_projection_step": "handover_completed",
            "controller_projection_index": 1,
            "controller_projection_count": 1,
            "request_generation": 0,
        }
    )
    return event


def _enable_skill_event_test_outputs(node: ORDigitalTwinNode) -> None:
    node._completed_handover_history = deque(maxlen=12)
    node._completed_handover_tools_by_phase = {}
    node._current_run_skill_task_ids = set()
    node._stamp_sec = lambda stamp: float(stamp.sec) + (
        float(stamp.nanosec) / 1_000_000_000.0
    )
    node._augment_event_detail = lambda _event_type, detail, **_kwargs: detail
    node._event_pub = SimpleNamespace(publish=lambda _message: None)
    node._simulation_event_pub = SimpleNamespace(
        publish=lambda _message: None
    )
    node._record_important_event = lambda _message: None
    node._publish_world_state = lambda: None


def test_frozen_0704_ngram_exposes_all_four_tool_probabilities() -> None:
    node = _node(_running_twin())

    tool_id, probability, detail = node._ngram_tool_prediction()

    assert tool_id == "T02"
    assert probability == pytest.approx(0.667)
    assert detail["match"] == "phase+last3"
    assert detail["support"] == 15
    assert detail["ngram"] == {
        "T02": pytest.approx(0.667),
        "T04": 0.0,
        "T07": pytest.approx(0.333),
        "T08": 0.0,
    }
    assert [tool_id for tool_id, _ in detail["ranked_distribution"]] == [
        "T02",
        "T07",
        "T04",
        "T08",
    ]


def test_vlm_tool_rows_cannot_override_the_ngram_policy() -> None:
    twin = _running_twin()
    node = _node(twin)

    node._handle_vlm_tool_prediction(
        {"v": "6", "tool": [["T04", 1.0], ["T08", 0.99]]},
        SimpleNamespace(source="real_vlm:test"),
        10.0,
        10.0,
    )

    assert twin.state.predicted_tool == "T02"
    assert twin.state.predicted_tool_confidence == pytest.approx(0.667)
    assert [row.instrument_id for row in twin.state.ranked_tool_predictions] == [
        "T02",
        "T07",
        "T04",
        "T08",
    ]


def test_direct_hand_fallback_keeps_rank_one_below_autonomous_threshold() -> None:
    twin = _running_twin()
    node = _node(twin, prepare_threshold=0.80)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.4)

    assert twin.state.predicted_tool == "T02"
    assert twin.state.predicted_tool_confidence == pytest.approx(0.667)
    assert twin.state.autonomous_preparation_ready is False
    assert len(twin.state.ranked_tool_predictions) == 4


def test_dt_authorizes_preparation_only_after_its_own_ngram_dwell() -> None:
    twin = _running_twin()
    node = _node(twin)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    assert twin.state.autonomous_preparation_ready is False
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert twin.state.predicted_tool == "T02"
    assert twin.state.predicted_tool_stability_sec == pytest.approx(0.31)
    assert twin.state.autonomous_preparation_ready is True


def test_starting_and_paused_wall_time_do_not_count_as_ngram_dwell() -> None:
    twin = _running_twin()
    node = _node(twin)
    twin.state.running = False
    twin.state.execution_state = "starting"

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=70.0)
    assert twin.state.predicted_tool_stability_sec == 0.0
    assert twin.state.autonomous_preparation_ready is False

    twin.state.running = True
    twin.state.execution_state = "running"
    node._refresh_ngram_tool_policy(now_sec=70.1)
    assert twin.state.predicted_tool_stability_sec == 0.0
    node._refresh_ngram_tool_policy(now_sec=70.41)
    assert twin.state.predicted_tool_stability_sec == pytest.approx(0.31)
    assert twin.state.autonomous_preparation_ready is True


def test_inactive_run_clears_policy_tags_without_relocating_a_mayo_tool() -> None:
    twin = _running_twin()
    node = _node(twin)
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=0.93,
    )
    recorded = twin.record_mayo_policy_evidence(
        instrument_id=mosquito.instance_id,
        evidence_type="recover",
        confidence=0.84,
        stability_sec=1.2,
        source="ngram_test",
        proposal_id="test:inactive-policy-clear",
        stamp_sec=10.0,
    )
    assert recorded is not None

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.4)
    assert twin.state.ranked_tool_predictions
    assert mosquito.mayo_recovery_confidence > 0.0
    lifecycle_before_stop = mosquito.lifecycle_stage

    twin.state.running = False
    twin.state.execution_state = "idle"
    twin.state.procedure_run_id = ""
    node._refresh_ngram_tool_policy(now_sec=11.0)

    assert twin.state.predicted_tool == ""
    assert twin.state.ranked_tool_predictions == []
    assert twin.state.autonomous_preparation_ready is False
    assert mosquito.location_type == "mayo_stand"
    assert mosquito.location_id == "mayo_stand"
    assert mosquito.lifecycle_stage == lifecycle_before_stop
    assert mosquito.mayo_reuse_confidence == 0.0
    assert mosquito.mayo_recovery_confidence == 0.0
    assert mosquito.mayo_evidence_source == ""


def test_pause_clears_recovery_dwell_before_resume() -> None:
    twin = _running_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, prepare_threshold=0.80)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    twin.state.execution_state = "paused"
    node._refresh_ngram_tool_policy(now_sec=30.0)
    twin.state.execution_state = "running"
    node._refresh_ngram_tool_policy(now_sec=30.1)
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    node._refresh_ngram_tool_policy(now_sec=30.41)
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY


def test_ngram_selects_next_available_tool_but_keeps_raw_distribution_visible() -> None:
    twin = _running_twin()
    unavailable_top = _state(twin, "T02")
    twin._set_lifecycle(
        unavailable_top,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin)

    node._refresh_ngram_tool_policy(now_sec=10.0)

    assert twin.state.predicted_tool == "T07"
    assert twin.state.predicted_tool_confidence == pytest.approx(0.333)
    assert twin.state.ranked_tool_predictions[0].instrument_id == "T02"
    assert twin.state.ranked_tool_predictions[0].confidence == pytest.approx(0.667)


def test_low_ngram_probability_queues_mayo_recovery_after_dwell() -> None:
    twin = _running_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, prepare_threshold=0.80)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY
    assert twin.state.active_recovery_tool_instances == [mosquito.instance_id]


def test_voice_completion_recovery_uses_frozen_targets_not_ngram_allowlist() -> None:
    twin = _running_twin()
    adson = _state(twin, "T02")
    bovie = _state(twin, "T04")
    for state in (adson, bovie):
        twin._set_lifecycle(
            state,
            LIFECYCLE_MAYO_REUSE,
            location_type="mayo_stand",
            location_id="mayo_stand",
            confidence=1.0,
        )
    twin._begin_completion_cleanup()
    node = _node(twin, recovery_enabled_tools=frozenset({"T04"}))

    assert node._refresh_ngram_mayo_recovery(
        scores={"T04": 0.0},
        has_ngram_evidence=True,
        preparation_tool_id="T04",
        now_sec=10.0,
    )
    assert adson.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY
    assert bovie.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert twin.state.active_recovery_tool_instances == [adson.instance_id]


def test_autonomous_mayo_recovery_stays_blocked_by_action_or_cam4_hand() -> None:
    twin = _running_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, prepare_threshold=0.80)

    twin.state.active_robot_task = ActiveRobotTask(task_id="active-delivery")
    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE

    twin.state.active_robot_task = None
    twin.state.cam4_mayo_hand_present = True
    node._refresh_ngram_tool_policy(now_sec=10.62)
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_stronger_recovery_probability_wins_over_preparation() -> None:
    twin = _running_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert twin.state.predicted_tool == "T02"
    assert twin.state.autonomous_preparation_ready is False
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY
    assert twin.state.active_recovery_tool_instances == [mosquito.instance_id]


def test_equal_probability_strength_keeps_preparation_first() -> None:
    twin = _running_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, history=("T02", "T02"))

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert twin.state.predicted_tool == "T04"
    assert twin.state.predicted_tool_confidence == pytest.approx(1.0)
    assert twin.state.autonomous_preparation_ready is True
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert twin.state.active_recovery_tool_instances == []


def test_cabled_bovie_and_bipolar_recovery_can_be_reenabled() -> None:
    twin = _running_twin()
    bovie = _state(twin, "T04")
    twin._set_lifecycle(
        bovie,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, prepare_threshold=0.80)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)
    assert bovie.lifecycle_stage == LIFECYCLE_MAYO_REUSE

    node._ngram_recovery_enabled_tools = frozenset({"T02", "T04", "T08"})
    node._ngram_recovery_stability.clear()
    node._refresh_ngram_tool_policy(now_sec=10.62)
    node._refresh_ngram_tool_policy(now_sec=10.93)
    assert bovie.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY


def test_recovery_enabled_tools_parameter_is_live_and_validated() -> None:
    node = _node(_running_twin())
    node._ngram_recovery_stability = {
        "T08#1": {"first_seen": 1.0, "last_seen": 1.4}
    }

    result = node._on_parameters_changed(
        [
            SimpleNamespace(
                name="ngram_recovery_enabled_tools",
                value=["T02", "T04", "T07", "T08"],
            )
        ]
    )

    assert result.successful is True
    assert node._ngram_recovery_enabled_tools == frozenset(
        {"T02", "T04", "T07", "T08"}
    )
    assert node._ngram_recovery_stability == {}

    rejected = node._on_parameters_changed(
        [
            SimpleNamespace(
                name="ngram_recovery_enabled_tools",
                value=["T02", "T99"],
            )
        ]
    )
    assert rejected.successful is False
    assert "T99" in rejected.reason


def test_regenerated_0704_ngram_can_prepare_mosquito() -> None:
    twin = _running_twin("P05")
    node = _node(twin, history=("T02", "T07"))

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert twin.state.predicted_tool == "T08"
    assert twin.state.predicted_tool_confidence == pytest.approx(1.0)
    assert twin.state.autonomous_preparation_ready is True


def test_future_use_compatibility_field_cannot_mark_requestable_t08_unused() -> None:
    twin = _running_twin()

    assert twin.procedure_future_use_expected("T08") is True
    twin.state.execution_state = "completed"
    twin.state.running = False
    assert twin.procedure_future_use_expected("T08") is False


def test_p03_completed_adson_on_mayo_is_not_an_automatic_repeat_target() -> None:
    twin = _running_twin()
    adson = _state(twin, "T02")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, history=("T02",))

    tool_id, probability, detail = node._ngram_tool_prediction()

    assert detail["ngram"]["T02"] == pytest.approx(0.625)
    assert detail["automatic_repeat_handover_exclusions"] == ["T02"]
    assert "T02" not in detail["eligible_candidates"]
    assert [
        candidate_id
        for candidate_id, _score in detail["ranked_distribution"]
    ] == ["T04", "T07", "T08"]
    assert tool_id == "T04"
    assert probability == pytest.approx(0.375)

    node._refresh_ngram_tool_policy(now_sec=10.0)
    node._refresh_ngram_tool_policy(now_sec=10.31)

    assert twin.state.predicted_tool == "T04"
    assert [
        belief.instrument_id for belief in twin.state.ranked_tool_predictions
    ] == ["T04", "T07", "T08"]
    assert adson.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert twin.state.active_recovery_tool_instances == []


def test_only_committed_handover_belief_arms_repeat_exclusion() -> None:
    twin = _running_twin()
    twin.state.procedure_run_id = "run-test"
    node = _node(twin)
    _enable_skill_event_test_outputs(node)
    node._stamp = lambda: SimpleNamespace(sec=11, nanosec=0)
    node._publish_reducer_decision_event = lambda **_kwargs: None
    node._publish_event = lambda *_args, **_kwargs: None

    node._on_skill_event(
        _controller_handover_event(
            final_state="failed",
            command_id="rejected-adson",
            stamp_sec=10,
        )
    )

    assert list(node._completed_handover_history) == []
    assert node._completed_handover_tools_by_phase == {}

    node._on_skill_event(
        _controller_handover_event(
            final_state="completed",
            command_id="accepted-adson",
            stamp_sec=11,
        )
    )

    # The controller receipt is auditable evidence, not a second location
    # owner. Repeat exclusion starts only when the tracker commits surgeon
    # custody and that projection is accepted by the Twin.
    assert list(node._completed_handover_history) == []
    node._on_tool_beliefs(
        SimpleNamespace(
            observation_only=True,
            procedure_id=twin.state.procedure_id,
            procedure_run_id="run-test",
            header=SimpleNamespace(
                stamp=SimpleNamespace(sec=12, nanosec=0)
            ),
            tools=[
                SimpleNamespace(
                    track_id="T02#1",
                    instrument_id="T02",
                    instance_id="T02#1",
                    committed_location_id="surgeon",
                    committed_location_probability=0.96,
                    existence_probability=1.0,
                    evidence_sources=["skill:completed"],
                )
            ],
        )
    )

    assert [
        row["tool"] for row in node._completed_handover_history
    ] == ["T02"]
    assert node._completed_handover_tools_by_phase == {"P03": {"T02"}}


def test_mayo_repeat_stays_voice_only_after_robot_picks_up_adson() -> None:
    twin = _running_twin()
    adson = _state(twin, "T02")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, history=("T02",))
    assert node._automatic_repeat_handover_exclusions() == frozenset(
        {"T02"}
    )

    pickup = TwinEvent()
    pickup.event_type = "RobotGraspedTool"
    pickup.instrument_id = "T02"
    pickup.instance_id = adson.instance_id
    pickup.source_location_type = "mayo_stand"
    pickup.source_location_id = "mayo_stand"
    pickup.target_location_type = "robot_right_hand"
    pickup.target_location_id = "robot_right_hand"
    pickup.confidence = 1.0
    pickup.stamp.sec = 12
    twin.apply_event(pickup)

    assert adson.lifecycle_stage == "prepositioned_right"
    assert twin.state.right_hand_tool == "T02"
    # The current-Mayo exclusion naturally clears after pickup, but its Mayo
    # provenance carries the voice-only rule through the right-hand state.
    assert node._automatic_repeat_handover_exclusions() == frozenset()
    assert node._repeat_handover_requires_explicit_voice("T02") is True


def test_explicit_voice_can_repeat_completed_p03_adson_from_mayo() -> None:
    twin = _running_twin()
    adson = _state(twin, "T02")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.normalize_for_publish()
    node = _node(twin, history=("T02",))

    node._refresh_ngram_tool_policy(now_sec=10.0)
    assert twin.state.predicted_tool != "T02"

    assert twin.update_resolved_voice_tool_handover("T02") == "T02"
    assert twin.state.explicit_request_tool == "T02"
    assert twin.state.surgeon_request_instance_id == adson.instance_id
    assert twin.explicit_request_voice_backed() is True
    assert twin.handover_allowed() is True


def test_p03_adson_repeat_exclusion_requires_history_and_mayo_return() -> None:
    first_twin = _running_twin()
    first_node = _node(first_twin)
    _tool_id, _probability, first_detail = first_node._ngram_tool_prediction()
    assert first_detail["automatic_repeat_handover_exclusions"] == []
    assert "T02" in first_detail["eligible_candidates"]

    rack_twin = _running_twin()
    rack_node = _node(rack_twin, history=("T02",))
    _tool_id, _probability, rack_detail = rack_node._ngram_tool_prediction()
    assert rack_detail["automatic_repeat_handover_exclusions"] == []
    assert "T02" in rack_detail["eligible_candidates"]

    older_history_twin = _running_twin()
    older_history_adson = _state(older_history_twin, "T02")
    older_history_twin._set_lifecycle(
        older_history_adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    older_history_twin.normalize_for_publish()
    older_history_node = _node(
        older_history_twin,
        history=("T02", "T04", "T07", "T04"),
    )
    _tool_id, _probability, older_detail = (
        older_history_node._ngram_tool_prediction()
    )
    assert older_detail["automatic_repeat_handover_exclusions"] == ["T02"]
    assert "T02" not in older_detail["eligible_candidates"]

    p04_twin = _running_twin("P04")
    p04_adson = _state(p04_twin, "T02")
    p04_twin._set_lifecycle(
        p04_adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    p04_twin.normalize_for_publish()
    p04_node = _node(p04_twin, history=("T02",))
    _tool_id, _probability, p04_detail = p04_node._ngram_tool_prediction()
    assert p04_detail["automatic_repeat_handover_exclusions"] == []
    assert "T02" in p04_detail["eligible_candidates"]
