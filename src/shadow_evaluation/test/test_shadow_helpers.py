import json
from copy import deepcopy

from builtin_interfaces.msg import Time
from surgical_msgs.msg import VLMHealth

from shadow_evaluation.recorded_transcript_adapter import (
    CLAMPED_AVAILABILITY_POLICY,
    EXPLICIT_AVAILABILITY_POLICY,
    IMMEDIATE_AVAILABILITY_POLICY,
    LEGACY_AVAILABILITY_POLICY,
    TranscriptReleaseBuffer,
    parse_transcript_payload,
    replay_context_requires_transcript_reset,
    resolve_available_sec,
    transcript_history_json,
    time_msg,
)
from shadow_evaluation.interactive_replay_controller import (
    ElasticReplayGate,
    FIELD_IMAGE_COMPATIBILITY_TOPIC,
    NORMALIZED_BBOX_TOPIC,
    NORMALIZED_CAMERA_TOPICS,
    NORMALIZED_SEGMENTATION_TOPIC,
    _is_no_input_vlm_health,
    advance_replay_elapsed,
    advance_replay_source_time,
    public_replay_topic_routes,
)
from shadow_evaluation.reference_reconciler import (
    load_runtime_tool_map,
    map_reference_location,
)
from shadow_evaluation.shadow_skill_sink import classify_shadow_command
from shadow_evaluation.shadow_skill_sink import classify_shadow_command_attempt
from shadow_evaluation.shadow_skill_sink import (
    completed_preposition_instrument_instances,
)
from shadow_evaluation.shadow_skill_sink import counterfactual_event_matches_world
from shadow_evaluation.shadow_skill_sink import (
    counterfactual_event_world_fingerprint,
)
from shadow_evaluation.shadow_skill_sink import counterfactual_event_payloads
from shadow_evaluation.shadow_skill_sink import (
    counterfactual_terminal_matches_world,
)
from shadow_evaluation.shadow_skill_sink import (
    counterfactual_task_boundary_payload,
)
from shadow_evaluation.shadow_skill_sink import (
    departed_prepositioned_instrument_instances,
)
from shadow_evaluation.shadow_skill_sink import PendingShadowAction
from shadow_evaluation.shadow_skill_sink import (
    newly_prepositioned_instrument_instances,
)
from shadow_evaluation.shadow_skill_sink import returned_home_instrument_instances
from shadow_evaluation.shadow_skill_sink import semantic_command_key
from shadow_evaluation.shadow_skill_sink import SemanticCommandLedger


def _world(
    *,
    running=True,
    execution_state=None,
    lifecycle="home_rack",
    contaminated=False,
    additional_instance_assumed=False,
    left_hand_tool="",
    cleaner_busy=False,
):
    return {
        "running": running,
        "execution_state": (
            execution_state
            if execution_state is not None
            else "running"
            if running
            else "idle"
        ),
        "active_robot_task_id": "",
        "left_hand_tool": left_hand_tool,
        "cleaner_busy": cleaner_busy,
        "filtered_phase": "P01",
        "surgeon_request_generation": 1,
        "surgeon_request_additional_instance_assumed": (
            additional_instance_assumed
        ),
        "instrument_states": [
            {
                "instrument_id": "bovie",
                "lifecycle_stage": lifecycle,
                "owner": "none",
                "contaminated": contaminated,
                "home_location_type": "instrument_rack",
                "home_location_id": "rack_bovie",
            }
        ],
    }


def test_recorded_transcript_extracts_only_public_text_and_timing():
    parsed = parse_transcript_payload(
        '{"start_sec":4.66,"end_sec":5.27,"text":"Adson","hidden_phase":"P99"}'
    )
    assert parsed == {
        "text": "Adson",
        "start_sec": 4.66,
        "end_sec": 5.27,
        "available_sec": None,
        "source_wav": "",
        "schema": "",
    }


def test_plain_transcript_is_supported_without_fabricated_timing():
    parsed = parse_transcript_payload("Bovie")
    assert parsed["text"] == "Bovie"
    assert parsed["start_sec"] is None
    assert parsed["end_sec"] is None
    assert parsed["available_sec"] is None
    assert resolve_available_sec(
        parsed,
        arrival_sec=7.25,
    ) == (
        7.25,
        IMMEDIATE_AVAILABILITY_POLICY,
    )


def test_transcript_history_contains_only_public_utterance_fields():
    payload = json.loads(
        transcript_history_json(
            "0704_6",
            [
                {
                    "utterance_id": "u1",
                    "text": "Adson",
                    "start_stamp": {"sec": 3, "nanosec": 0},
                    "end_stamp": {"sec": 4, "nanosec": 0},
                    "speaker_role": "surgeon",
                }
            ],
            run_id="0704_6-run-a",
        )
    )

    assert payload["schema"] == "taskplanner.shadow_transcript_history.v1"
    assert payload["case_id"] == "0704_6"
    assert payload["run_id"] == "0704_6-run-a"
    assert payload["utterances"][0]["text"] == "Adson"
    assert "phase" not in payload["utterances"][0]


def test_transcript_context_resets_atomically_on_case_switch() -> None:
    assert replay_context_requires_transcript_reset(
        current_case_id="0704_6",
        current_run_id="run-a",
        last_source_time_sec=42.0,
        next_case_id="0704_7",
        next_run_id="run-b",
        next_source_time_sec=0.0,
    )
    assert replay_context_requires_transcript_reset(
        current_case_id="0704_7",
        current_run_id="run-b",
        last_source_time_sec=42.0,
        next_case_id="0704_7",
        next_run_id="run-b",
        next_source_time_sec=0.0,
    )
    assert not replay_context_requires_transcript_reset(
        current_case_id="0704_7",
        current_run_id="run-b",
        last_source_time_sec=42.0,
        next_case_id="0704_7",
        next_run_id="run-b",
        next_source_time_sec=42.1,
    )


def test_v2_transcript_uses_explicit_causal_availability():
    parsed = parse_transcript_payload(
        '{"schema":"taskplanner.observable_voice_point.v2",'
        '"time_sec":4.66,"end_sec":5.27,"available_sec":5.5,'
        '"text":"Adson"}'
    )
    assert parsed["start_sec"] == 4.66
    assert resolve_available_sec(
        parsed,
        arrival_sec=4.66,
    ) == (
        5.5,
        EXPLICIT_AVAILABILITY_POLICY,
    )


def test_legacy_transcript_defers_complete_text_to_utterance_end():
    parsed = parse_transcript_payload(
        '{"start_sec":4.66,"end_sec":5.27,"text":"Adson"}'
    )
    assert resolve_available_sec(
        parsed,
        arrival_sec=4.66,
    ) == (
        5.27,
        LEGACY_AVAILABILITY_POLICY,
    )


def test_invalid_early_available_time_is_safely_clamped_to_end():
    parsed = parse_transcript_payload(
        '{"schema":"taskplanner.observable_voice_point.v2",'
        '"time_sec":4.66,"end_sec":5.27,"available_sec":4.7,'
        '"text":"Adson"}'
    )
    assert resolve_available_sec(
        parsed,
        arrival_sec=4.66,
    ) == (
        5.27,
        CLAMPED_AVAILABILITY_POLICY,
    )


def test_transcript_release_buffer_does_not_burst_future_items_at_start():
    buffer = TranscriptReleaseBuffer()
    fallback = Time()
    rows = [
        (5.72, "u1", "case start"),
        (8.44, "u2", "Adson"),
        (11.22, "u3", "Adson 하나 더"),
        (14.16, "u4", "Bovie"),
        (27.0, "u5", "air suction"),
    ]
    for available_sec, utterance_id, text in rows:
        assert buffer.add(
            available_ns=round(available_sec * 1_000_000_000),
            utterance_id=utterance_id,
            text=text,
            start=time_msg(available_sec - 0.5, fallback),
            end=time_msg(available_sec, fallback),
            availability_policy=EXPLICIT_AVAILABILITY_POLICY,
        )

    assert buffer.pop_due(0) == []
    assert buffer.pop_due(5_719_999_999) == []
    assert [row.utterance_id for row in buffer.pop_due(5_720_000_000)] == [
        "u1"
    ]
    assert [row.utterance_id for row in buffer.pop_due(8_440_000_000)] == [
        "u2"
    ]
    assert buffer.pop_due(8_440_000_000) == []

    buffer.reset()
    assert buffer.pop_due(30_000_000_000) == []
    assert buffer.add(
        available_ns=5_720_000_000,
        utterance_id="u1",
        text="case start",
        start=fallback,
        end=fallback,
        availability_policy=EXPLICIT_AVAILABILITY_POLICY,
    )


def test_pause_and_stop_freeze_elapsed_and_source_until_resume():
    elapsed, last_tick, delta = advance_replay_elapsed(
        4.0,
        10.0,
        12.5,
        active=True,
    )
    assert elapsed == 6.5
    assert last_tick == 12.5
    assert delta == 0.25

    frozen_elapsed, last_tick, _ = advance_replay_elapsed(
        elapsed,
        last_tick,
        30.0,
        active=False,
    )
    assert frozen_elapsed == 6.5
    assert advance_replay_source_time(
        8.0,
        17.5,
        1.0,
        100.0,
        advancing=False,
    ) == 8.0

    resumed_elapsed, _, _ = advance_replay_elapsed(
        frozen_elapsed,
        last_tick,
        31.0,
        active=True,
    )
    assert resumed_elapsed == 7.5
    assert advance_replay_source_time(
        8.0,
        0.25,
        1.0,
        100.0,
        advancing=True,
    ) == 8.25


def test_public_replay_routes_only_raw_cameras_and_public_perception_json():
    image_routes, json_routes = public_replay_topic_routes(
        source_cam1_topic="/recorded/cam1",
        source_cam2_topic="/recorded/cam2",
        source_cam3_topic="/recorded/cam3",
        source_cam4_topic="/recorded/cam4",
        source_flir_topic="/recorded/flir",
        source_bbox_topic="/recorded/bboxes",
        source_segmentation_topic="/recorded/segmentation",
    )

    assert image_routes == {
        "/recorded/cam1": (NORMALIZED_CAMERA_TOPICS["cam1"],),
        "/recorded/cam2": (NORMALIZED_CAMERA_TOPICS["cam2"],),
        "/recorded/cam3": (NORMALIZED_CAMERA_TOPICS["cam3"],),
        "/recorded/cam4": (
            NORMALIZED_CAMERA_TOPICS["cam4"],
            FIELD_IMAGE_COMPATIBILITY_TOPIC,
        ),
        "/recorded/flir": (NORMALIZED_CAMERA_TOPICS["flir"],),
    }
    assert json_routes == {
        "/recorded/bboxes": NORMALIZED_BBOX_TOPIC,
        "/recorded/segmentation": NORMALIZED_SEGMENTATION_TOPIC,
    }
    all_source_topics = {*image_routes, *json_routes}
    assert not any(
        token in topic
        for topic in all_source_topics
        for token in ("ground_truth", "evaluation", "annotation")
    )


def test_time_conversion_is_nanosecond_stable():
    fallback = Time(sec=9, nanosec=8)
    assert time_msg(4.66, fallback).sec == 4
    assert time_msg(4.66, fallback).nanosec == 660_000_000
    assert time_msg(None, fallback) is fallback


def test_elastic_replay_gate_counts_only_public_image_slots():
    gate = ElasticReplayGate(vlm_period_sec=1.0, max_pending_vlm=1)

    assert gate.expected_vlm_count(
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=0,
    ) == 0
    assert gate.expected_vlm_count(
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=40,
    ) == 3
    assert gate.pending_vlm_count(
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=40,
        completed_vlm_count=2,
    ) == 1


def test_elastic_replay_gate_observes_backlog_without_clock_coupling():
    gate = ElasticReplayGate(vlm_period_sec=1.0, max_pending_vlm=1)

    backlog = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=40,
        completed_vlm_count=1,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=True,
        vlm_grace_elapsed=True,
    )
    assert backlog.hold_reason == ""
    assert backlog.playback_rate_factor == 1.0
    assert backlog.vlm_lag_sec > 0.0
    assert gate.hold_reason(
        mode="elastic_demo",
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=40,
        completed_vlm_count=3,
        active_skill_count=1,
        vlm_ready=True,
        vlm_grace_elapsed=True,
    ) == "skill_execution"
    assert gate.hold_reason(
        mode="realtime_1x",
        source_time_sec=3.4,
        image_duration_sec=10.0,
        published_image_count=40,
        completed_vlm_count=0,
        active_skill_count=1,
        vlm_ready=False,
        vlm_grace_elapsed=True,
    ) == ""


def test_elastic_replay_gate_does_not_require_vlm_after_images_end():
    gate = ElasticReplayGate(vlm_period_sec=1.0, max_pending_vlm=1)

    assert gate.hold_reason(
        mode="elastic_demo",
        source_time_sec=12.0,
        image_duration_sec=10.0,
        published_image_count=150,
        completed_vlm_count=10,
        active_skill_count=0,
        vlm_ready=False,
        vlm_grace_elapsed=True,
    ) == ""


def test_no_fresh_recorded_image_is_not_a_provider_failure():
    health = VLMHealth()
    health.connected = True
    health.healthy = False
    health.last_error = "no fresh field image"
    assert _is_no_input_vlm_health(health)

    health.connected = False
    assert not _is_no_input_vlm_health(health)

    health.connected = True
    health.last_error = "request timed out"
    assert not _is_no_input_vlm_health(health)

    health.last_error = "missing fresh RFDETR-segmented FLIR image"
    assert _is_no_input_vlm_health(health)


def test_shadow_sink_never_admits_command_without_running_world():
    status, reason = classify_shadow_command(
        {"instrument_id": "bovie", "action": "pick_up_and_handover"},
        None,
    )
    assert status == "blocked"
    assert reason == "no_world_state"


def test_semantic_command_ledger_reset_removes_prior_run_state():
    ledger = SemanticCommandLedger()
    ledger.record_admission("handover:T02", "world-a", 10.0)
    assert ledger.previous_fingerprint("handover:T02") == "world-a"
    assert ledger.should_report_deadlock(
        "handover:T02",
        "world-a",
        11.0,
    )

    ledger.reset()

    assert ledger.previous_fingerprint("handover:T02") == ""
    assert ledger.should_report_deadlock(
        "handover:T02",
        "world-a",
        12.0,
    )


def test_rack_return_starts_new_preparation_episode_only_for_returned_instance():
    ledger = SemanticCommandLedger()
    returned_prepare = semantic_command_key(
        {
            "action": "predict_tool",
            "instrument_id": "T02",
            "instance_id": "T02#1",
            "request_generation": 0,
        }
    )
    other_prepare = semantic_command_key(
        {
            "action": "predict_tool",
            "instrument_id": "T02",
            "instance_id": "T02#2",
            "request_generation": 0,
        }
    )
    handover = semantic_command_key(
        {
            "action": "pick_up_and_handover",
            "instrument_id": "T02",
            "instance_id": "T02#1",
            "request_generation": 1,
        }
    )
    for key in (returned_prepare, other_prepare, handover):
        ledger.record_admission(key, "world-a", 10.0)
        assert ledger.should_report_deadlock(key, "world-a", 11.0)

    ledger.forget_preparations("T02", "T02#1")

    assert ledger.previous_fingerprint(returned_prepare) == ""
    assert ledger.previous_fingerprint(other_prepare) == "world-a"
    assert ledger.previous_fingerprint(handover) == "world-a"
    assert ledger.should_report_deadlock(
        returned_prepare,
        "world-a",
        12.0,
    )


def test_new_preparation_episode_readmits_unused_return_for_same_instance():
    ledger = SemanticCommandLedger()
    returned = semantic_command_key(
        {
            "action": "return_unused_preposition",
            "instrument_id": "T01",
            "instance_id": "T01#1",
            "request_generation": 0,
        }
    )
    other_return = semantic_command_key(
        {
            "action": "return_unused_preposition",
            "instrument_id": "T01",
            "instance_id": "T01#2",
            "request_generation": 0,
        }
    )
    ledger.record_admission(returned, "prepositioned-world", 10.0)
    ledger.record_admission(other_return, "other-world", 10.0)

    ledger.begin_preparation_episode("T01", "T01#1")

    assert ledger.previous_fingerprint(returned) == ""
    assert ledger.previous_fingerprint(other_return) == "other-world"


def test_returned_home_transition_is_instance_specific_and_edge_triggered():
    before = {
        "instrument_states": [
            {
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "lifecycle_stage": "prepositioned_right",
            },
            {
                "instrument_id": "T02",
                "instance_id": "T02#2",
                "lifecycle_stage": "home_rack",
            },
        ]
    }
    after = {
        "instrument_states": [
            {
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "lifecycle_stage": "returned_home",
            },
            {
                "instrument_id": "T02",
                "instance_id": "T02#2",
                "lifecycle_stage": "home_rack",
            },
        ]
    }

    assert returned_home_instrument_instances(before, after) == {
        ("T02", "T02#1")
    }
    assert returned_home_instrument_instances(after, after) == set()


def test_consumed_preposition_starts_a_new_prediction_episode():
    before = {
        "instrument_states": [
            {
                "instrument_id": "T04",
                "instance_id": "T04#1",
                "lifecycle_stage": "prepositioned_right",
            },
            {
                "instrument_id": "T07",
                "instance_id": "T07#1",
                "lifecycle_stage": "mayo_reuse",
            },
        ]
    }
    after_handover = {
        "instrument_states": [
            {
                "instrument_id": "T04",
                "instance_id": "T04#1",
                "lifecycle_stage": "surgeon_owned",
            },
            {
                "instrument_id": "T07",
                "instance_id": "T07#1",
                "lifecycle_stage": "mayo_reuse",
            },
        ]
    }

    assert completed_preposition_instrument_instances(
        before,
        after_handover,
    ) == {("T04", "T04#1")}
    assert completed_preposition_instrument_instances(
        after_handover,
        after_handover,
    ) == set()


def test_new_preparation_episode_rearms_return_for_only_that_instance():
    ledger = SemanticCommandLedger()
    returned = semantic_command_key(
        {
            "action": "return_unused_preposition",
            "instrument_id": "T01",
            "instance_id": "T01#1",
            "request_generation": 0,
        }
    )
    other_return = semantic_command_key(
        {
            "action": "return_unused_preposition",
            "instrument_id": "T01",
            "instance_id": "T01#2",
            "request_generation": 0,
        }
    )
    for key in (returned, other_return):
        ledger.record_admission(key, "prepositioned-world", 10.0)

    ledger.forget_returns("T01", "T01#1")

    assert ledger.previous_fingerprint(returned) == ""
    assert (
        ledger.previous_fingerprint(other_return)
        == "prepositioned-world"
    )


def test_newly_prepositioned_transition_is_instance_specific_and_edge_triggered():
    before = {
        "instrument_states": [
            {
                "instrument_id": "T01",
                "instance_id": "T01#1",
                "lifecycle_stage": "returned_home",
            },
            {
                "instrument_id": "T01",
                "instance_id": "T01#2",
                "lifecycle_stage": "prepositioned_right",
            },
        ]
    }
    after = {
        "instrument_states": [
            {
                "instrument_id": "T01",
                "instance_id": "T01#1",
                "lifecycle_stage": "prepositioned_right",
            },
            {
                "instrument_id": "T01",
                "instance_id": "T01#2",
                "lifecycle_stage": "prepositioned_right",
            },
        ]
    }

    assert newly_prepositioned_instrument_instances(before, after) == {
        ("T01", "T01#1")
    }
    assert newly_prepositioned_instrument_instances(after, after) == set()


def test_departed_preposition_starts_a_new_preparation_episode():
    before = {
        "instrument_states": [
            {
                "instrument_id": "T04",
                "instance_id": "T04#1",
                "lifecycle_stage": "prepositioned_right",
            },
            {
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "lifecycle_stage": "home_rack",
            },
        ]
    }
    after = {
        "instrument_states": [
            {
                "instrument_id": "T04",
                "instance_id": "T04#1",
                "lifecycle_stage": "surgeon_owned",
            },
            {
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "lifecycle_stage": "home_rack",
            },
        ]
    }

    assert departed_prepositioned_instrument_instances(before, after) == {
        ("T04", "T04#1")
    }
    assert departed_prepositioned_instrument_instances(after, after) == set()


def test_shadow_sink_applies_physical_handover_guards():
    command = {"instrument_id": "bovie", "action": "pick_up_and_handover"}
    assert classify_shadow_command(command, _world())[0] == "admissible"
    assert classify_shadow_command(
        command,
        _world(lifecycle="surgeon_owned"),
    )[0] == "physically_impossible"
    assert classify_shadow_command(
        command,
        _world(contaminated=True),
    )[0] == "unsafe"
    assert classify_shadow_command(
        command,
        _world(lifecycle="mayo_reuse", contaminated=True),
    )[0] == "admissible"


def test_shadow_sink_allows_reversible_preparation_from_mayo_reuse():
    world = _world(lifecycle="mayo_reuse", contaminated=True)
    state = world["instrument_states"][0]
    state.update(
        instance_id="bovie#1",
        location_id="mayo_reuse_zone",
        location_type="mayo_reuse_zone",
    )
    prepare = {
        "command_id": "prepare-mayo-bovie",
        "action": "predict_tool",
        "instrument_id": "bovie",
        "instance_id": "bovie#1",
        "source_location_id": "mayo_reuse_zone",
        "source_location_type": "mayo_reuse_zone",
        "target_location_id": "robot_right_hand",
        "target_location_type": "robot_right_hand",
    }

    assert classify_shadow_command(prepare, world) == (
        "admissible",
        "shadow_only_no_execution",
    )
    prepared_event = counterfactual_event_payloads(prepare, world)[0]
    assert prepared_event["event_type"] == "ToolPrepared"
    assert prepared_event["source_location_id"] == "mayo_reuse_zone"

    prepared_world = deepcopy(world)
    prepared_state = prepared_world["instrument_states"][0]
    prepared_state.update(
        lifecycle_stage="prepositioned_right",
        owner="robot_right_hand",
        location_id="robot_right_hand",
        location_type="robot_right_hand",
        contaminated=False,
        preposition_origin_location_id="mayo_reuse_zone",
        preposition_origin_location_type="mayo_reuse_zone",
        preposition_origin_lifecycle_stage="mayo_reuse",
    )
    prepared_world["right_hand_tool"] = "bovie"
    prepared_world["right_hand_instance_id"] = "bovie#1"
    release = {
        "command_id": "release-mayo-bovie",
        "action": "return_unused_preposition",
        "instrument_id": "bovie",
        "instance_id": "bovie#1",
        "source_location_id": "robot_right_hand",
        "source_location_type": "robot_right_hand",
        "target_location_id": "mayo_reuse_zone",
        "target_location_type": "mayo_reuse_zone",
    }

    assert classify_shadow_command(release, prepared_world) == (
        "admissible",
        "shadow_only_no_execution",
    )
    returned_event = counterfactual_event_payloads(
        release,
        prepared_world,
    )[0]
    assert returned_event["event_type"] == "UnusedPrepositionReturned"
    assert returned_event["target_location_id"] == "mayo_reuse_zone"
    assert (
        returned_event["detail"]["target_lifecycle_stage"]
        == "mayo_reuse"
    )


def test_shadow_sink_can_label_type_level_instance_assumption_without_weakening_default_guard():
    command = {"instrument_id": "bovie", "action": "pick_up_and_handover"}
    world = _world(lifecycle="surgeon_owned")

    assert classify_shadow_command(command, world)[0] == "physically_impossible"
    assert classify_shadow_command(
        command,
        world,
        allow_type_instance_assumption=True,
    )[0] == "physically_impossible"
    assert classify_shadow_command(
        command,
        _world(
            lifecycle="surgeon_owned",
            additional_instance_assumed=True,
        ),
        allow_type_instance_assumption=True,
    ) == (
        "instance_resolution_assumed",
        "tool_type_already_owned_instance_inventory_unmodeled",
    )


def test_additional_instance_does_not_inherit_representative_instance_contamination():
    command = {"instrument_id": "bovie", "action": "pick_up_and_handover"}
    world = _world(
        lifecycle="surgeon_owned",
        contaminated=True,
        additional_instance_assumed=True,
    )

    assert classify_shadow_command(
        command,
        world,
        allow_type_instance_assumption=True,
    ) == (
        "instance_resolution_assumed",
        "tool_type_already_owned_instance_inventory_unmodeled",
    )


def test_instance_assumption_uses_non_mutating_shadow_completion_event():
    command = {
        "action": "pick_up_and_handover",
        "instrument_id": "bovie",
        "arm": "right",
        "command_id": "cmd-extra",
    }

    events = counterfactual_event_payloads(
        command,
        _world(lifecycle="surgeon_owned"),
        instance_resolution_assumed=True,
    )

    assert [event["event_type"] for event in events] == [
        "ShadowAdditionalToolHandoverCompleted"
    ]
    assert events[0]["detail"]["ground_truth_used"] is False
    assert events[0]["detail"]["shadow_assumption"] == "additional_tool_instance"


def test_shadow_sink_recovery_requires_mayo_state():
    command = {"instrument_id": "bovie", "action": "retrieve_from_mayo"}
    assert classify_shadow_command(command, _world())[0] == "physically_impossible"
    assert classify_shadow_command(
        command,
        _world(lifecycle="mayo_reuse"),
    )[0] == "admissible"
    assert classify_shadow_command(
        command,
        _world(lifecycle="mayo_recovery", contaminated=True),
    )[0] == "admissible"


def test_shadow_sink_recovery_waits_for_left_hand_and_cleaner():
    command = {"instrument_id": "bovie", "action": "retrieve_from_mayo"}

    assert classify_shadow_command(
        command,
        _world(lifecycle="mayo_recovery", left_hand_tool="adson"),
    ) == ("blocked", "robot_left_hand_busy:adson")
    assert classify_shadow_command(
        command,
        _world(lifecycle="mayo_recovery", cleaner_busy=True),
    ) == ("blocked", "cleaner_busy")


def test_shadow_sink_allows_only_cleanup_actions_while_finishing():
    finishing_world = _world(
        execution_state="finishing",
        lifecycle="mayo_recovery",
    )

    assert classify_shadow_command(
        {"instrument_id": "bovie", "action": "retrieve_from_mayo"},
        finishing_world,
    ) == ("admissible", "shadow_only_no_execution")
    assert classify_shadow_command(
        {"instrument_id": "bovie", "action": "pick_up_and_handover"},
        finishing_world,
    ) == ("blocked", "runtime_finishing_cleanup_only")


def test_shadow_sink_marks_repeat_without_world_change_as_deadlock():
    command = {"instrument_id": "bovie", "action": "pick_up_and_handover"}
    first = classify_shadow_command_attempt(command, _world())
    repeated = classify_shadow_command_attempt(
        command,
        _world(),
        previous_admissible_fingerprint=first[2],
    )

    assert first[:2] == ("admissible", "shadow_only_no_execution")
    assert repeated[:2] == (
        "deadlock_rejected",
        "same_semantic_command_and_generation_without_world_state_change",
    )


def test_shadow_sink_readmits_semantic_command_after_world_change():
    command = {"instrument_id": "bovie", "action": "pick_up_and_handover"}
    first = classify_shadow_command_attempt(command, _world())
    changed = _world()
    changed["filtered_phase"] = "P02"
    repeated = classify_shadow_command_attempt(
        command,
        changed,
        previous_admissible_fingerprint=first[2],
    )

    assert repeated[:2] == ("admissible", "shadow_only_no_execution")


def test_shadow_sink_readmits_identical_request_after_generation_change():
    command = {
        "instrument_id": "bovie",
        "action": "pick_up_and_handover",
        "request_generation": 1,
    }
    first = classify_shadow_command_attempt(command, _world())
    changed = _world()
    changed["surgeon_request_generation"] = 2
    repeated = classify_shadow_command_attempt(
        {**command, "request_generation": 2},
        changed,
        previous_admissible_fingerprint=first[2],
    )

    assert repeated[:2] == ("admissible", "shadow_only_no_execution")


def _put_down_world():
    return {
        "procedure_id": "thyroidectomy_demo",
        "running": True,
        "execution_state": "running",
        "active_robot_task_id": "",
        "active_robot_task_type": "",
        "active_robot_task_tool_id": "",
        "filtered_phase": "P03",
        "surgeon_request_tool": "T07",
        "surgeon_request_generation": 7,
        "right_hand_tool": "T02",
        "right_hand_instance_id": "T02#1",
        "left_hand_tool": "",
        "cleaner_busy": False,
        "instrument_states": [
            {
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "lifecycle_stage": "prepositioned_right",
                "owner": "robot_right_hand",
                "location_id": "robot_right_hand",
                "location_type": "robot_right_hand",
                "home_location_id": "main_tray_slot_2",
                "home_location_type": "tray_slot",
                "contaminated": False,
            },
            {
                "instrument_id": "T07",
                "instance_id": "T07#1",
                "lifecycle_stage": "home_rack",
                "owner": "none",
                "location_id": "main_tray_slot_7",
                "location_type": "tray_slot",
                "home_location_id": "main_tray_slot_7",
                "home_location_type": "tray_slot",
                "contaminated": False,
            },
        ],
    }


def test_put_down_and_handover_returns_t02_before_grasping_t07():
    command = {
        "command_id": "cmd-put-down-t02-for-t07",
        "action": "put_down_and_handover",
        "instrument_id": "T07",
        "instance_id": "T07#1",
        "request_generation": 7,
        "arm": "right",
        "source_location_id": "main_tray_slot_7",
        "source_location_type": "tray_slot",
        "target_location_id": "surgeon_receive_zone",
        "target_location_type": "handover_zone",
        "target_owner": "surgeon",
    }
    world = _put_down_world()

    assert classify_shadow_command(command, world) == (
        "admissible",
        "shadow_only_no_execution",
    )
    events = counterfactual_event_payloads(command, world)

    assert [event["event_type"] for event in events] == [
        "UnusedPrepositionReturned",
        "RobotGraspedTool",
        "ToolHandoverCompleted",
    ]
    assert events[0]["instrument_id"] == "T02"
    assert events[0]["instance_id"] == "T02#1"
    assert events[0]["target_location_id"] == "main_tray_slot_2"
    assert events[1]["instrument_id"] == "T07"
    assert events[1]["instance_id"] == "T07#1"


def test_put_down_and_handover_requires_an_actual_displaced_tool():
    world = _put_down_world()
    world["right_hand_tool"] = ""
    world["right_hand_instance_id"] = ""
    world["instrument_states"][0]["lifecycle_stage"] = "home_rack"
    world["instrument_states"][0]["owner"] = "none"
    command = {
        "action": "put_down_and_handover",
        "instrument_id": "T07",
        "instance_id": "T07#1",
        "request_generation": 7,
    }

    assert classify_shadow_command(command, world) == (
        "blocked",
        "put_down_and_handover_requires_different_right_hand_tool",
    )


def test_put_down_handover_only_completes_after_each_dt_lifecycle_transition():
    command = {
        "command_id": "cmd-lifecycle-ack",
        "action": "put_down_and_handover",
        "instrument_id": "T07",
        "instance_id": "T07#1",
        "request_generation": 7,
        "arm": "right",
        "source_location_id": "main_tray_slot_7",
        "source_location_type": "tray_slot",
        "target_location_id": "surgeon_receive_zone",
        "target_location_type": "handover_zone",
        "target_owner": "surgeon",
    }
    before = _put_down_world()
    events = counterfactual_event_payloads(command, before)
    release, grasp, handover = events
    release_baseline = counterfactual_event_world_fingerprint(release, before)

    assert not counterfactual_event_matches_world(release, before)

    after_release = deepcopy(before)
    t02 = after_release["instrument_states"][0]
    t02.update(
        lifecycle_stage="returned_home",
        owner="none",
        location_id="main_tray_slot_2",
        location_type="tray_slot",
    )
    after_release["right_hand_tool"] = ""
    after_release["right_hand_instance_id"] = ""
    assert counterfactual_event_world_fingerprint(
        release,
        after_release,
    ) != release_baseline
    assert counterfactual_event_matches_world(release, after_release)
    assert not counterfactual_event_matches_world(grasp, after_release)

    after_grasp = deepcopy(after_release)
    t07 = after_grasp["instrument_states"][1]
    t07.update(
        lifecycle_stage="prepositioned_right",
        owner="robot_right_hand",
        location_id="robot_right_hand",
        location_type="robot_right_hand",
    )
    after_grasp["right_hand_tool"] = "T07"
    after_grasp["right_hand_instance_id"] = "T07#1"
    assert counterfactual_event_matches_world(grasp, after_grasp)
    assert not counterfactual_terminal_matches_world(
        command,
        events,
        after_grasp,
    )

    after_handover = deepcopy(after_grasp)
    after_handover["instrument_states"][1].update(
        lifecycle_stage="surgeon_owned",
        owner="surgeon",
        location_id="surgeon_hand",
        location_type="surgeon_hand",
    )
    after_handover["right_hand_tool"] = ""
    after_handover["right_hand_instance_id"] = ""
    assert counterfactual_event_matches_world(handover, after_handover)
    assert counterfactual_terminal_matches_world(
        command,
        events,
        after_handover,
    )


def test_same_generation_deadlock_is_reported_once_across_47_retries():
    command = {
        "action": "put_down_and_handover",
        "instrument_id": "T07",
        "instance_id": "T07#1",
        "request_generation": 7,
        "arm": "right",
    }
    world = _put_down_world()
    semantic_key = semantic_command_key(command)
    ledger = SemanticCommandLedger()
    first = classify_shadow_command_attempt(command, world)
    ledger.record_admission(semantic_key, first[2], 1.0)

    reported = 0
    for attempt in range(47):
        repeated = classify_shadow_command_attempt(
            {**command, "command_id": f"retry-{attempt}"},
            world,
            previous_admissible_fingerprint=ledger.previous_fingerprint(
                semantic_key
            ),
        )
        assert repeated[:2] == (
            "deadlock_rejected",
            "same_semantic_command_and_generation_without_world_state_change",
        )
        reported += int(
            ledger.should_report_deadlock(
                semantic_key,
                repeated[2],
                2.0 + attempt,
            )
        )

    assert reported == 1


def test_semantic_dedupe_is_scoped_by_request_generation_and_instance():
    base = {
        "action": "pick_up_and_handover",
        "instrument_id": "T02",
        "instance_id": "T02#1",
        "request_generation": 1,
        "arm": "right",
    }

    assert semantic_command_key(base) != semantic_command_key(
        {**base, "request_generation": 2}
    )
    assert semantic_command_key(base) != semantic_command_key(
        {**base, "instance_id": "T02#2"}
    )


def test_explicit_unknown_instance_never_falls_back_to_same_tool_type():
    world = _put_down_world()
    command = {
        "action": "pick_up_and_handover",
        "instrument_id": "T07",
        "instance_id": "T07#2",
        "request_generation": 8,
        "arm": "right",
    }

    assert classify_shadow_command(command, world) == (
        "physically_impossible",
        "instrument_not_in_active_bundle",
    )


def test_ros_skill_and_world_instance_field_names_are_preserved():
    world = _put_down_world()
    world["right_hand_tool_instance_id"] = world.pop(
        "right_hand_instance_id"
    )
    command = {
        "action": "put_down_and_handover",
        "instrument_id": "T07",
        "instrument_instance_id": "T07#1",
        "request_generation": 9,
        "arm": "right",
    }

    assert classify_shadow_command(command, world) == (
        "admissible",
        "shadow_only_no_execution",
    )
    events = counterfactual_event_payloads(command, world)
    assert events[-1]["instance_id"] == "T07#1"


def test_counterfactual_handover_feedback_is_gt_independent_and_ordered():
    command = {
        "command_id": "cmd-1",
        "instrument_id": "bovie",
        "action": "pick_up_and_handover",
        "arm": "right",
    }

    events = counterfactual_event_payloads(command, _world())

    assert [event["event_type"] for event in events] == [
        "RobotGraspedTool",
        "ToolHandoverCompleted",
    ]
    assert all(event["detail"]["ground_truth_used"] is False for event in events)
    assert all(
        event["detail"]["physical_execution_attempted"] is False
        for event in events
    )


def test_counterfactual_recovery_feedback_completes_cleaning_chain():
    command = {
        "command_id": "cmd-2",
        "instrument_id": "bovie",
        "action": "retrieve_from_mayo",
        "arm": "left",
    }

    events = counterfactual_event_payloads(
        command,
        _world(lifecycle="mayo_recovery"),
    )

    assert [event["event_type"] for event in events] == [
        "ToolReceivedFromSurgeon",
        "ToolSentToCleaner",
        "ToolCleaningCompleted",
        "ToolReturnedToTray",
    ]


def test_counterfactual_task_boundaries_gate_parallel_bt_commands():
    command = {
        "command_id": "cmd-2",
        "instrument_id": "bovie",
        "action": "retrieve_from_mayo",
        "arm": "left",
        "source_location_id": "mayo_recovery_zone",
        "target_location_id": "rack_bovie",
    }

    started = counterfactual_task_boundary_payload(
        command,
        started=True,
        duration_sec=6.0,
    )
    completed = counterfactual_task_boundary_payload(
        command,
        started=False,
        duration_sec=0.0,
    )

    assert started["event_type"] == "RobotTaskStarted"
    assert started["detail"]["task_id"] == "cmd-2"
    assert started["detail"]["task_type"] == "retrieve_from_mayo"
    assert started["detail"]["duration_sec"] == 6.0
    assert completed["event_type"] == "RobotTaskCompleted"
    assert completed["detail"]["duration_sec"] == 0.0


def test_timed_shadow_recovery_releases_dt_events_in_physical_order():
    command = {
        "command_id": "cmd-recovery",
        "instrument_id": "bovie",
        "action": "retrieve_from_mayo",
        "arm": "left",
    }
    action = PendingShadowAction(
        payload=command,
        admission_status="admissible",
        events=counterfactual_event_payloads(
            command,
            _world(lifecycle="mayo_recovery"),
        ),
        started_at=10.0,
        duration_sec=10.0,
    )

    assert [event["event_type"] for event in action.due_events(12.3)] == [
        "ToolReceivedFromSurgeon"
    ]
    assert [event["event_type"] for event in action.due_events(14.3)] == [
        "ToolSentToCleaner"
    ]
    assert [event["event_type"] for event in action.due_events(17.9)] == [
        "ToolCleaningCompleted"
    ]
    assert [event["event_type"] for event in action.due_events(20.0)] == [
        "ToolReturnedToTray"
    ]
    assert action.complete(20.0)


def test_pending_shadow_action_clock_shift_preserves_paused_progress():
    action = PendingShadowAction(
        payload={
            "command_id": "cmd-paused",
            "instrument_id": "bovie",
            "action": "retrieve_from_mayo",
        },
        admission_status="admissible",
        events=[],
        started_at=10.0,
        duration_sec=10.0,
        awaiting_since=13.0,
    )

    assert action.progress(14.0) == 0.4

    action.shift_clock(6.0)

    assert action.started_at == 16.0
    assert action.awaiting_since == 19.0
    assert action.progress(20.0) == 0.4


def test_reference_mapping_preserves_visible_semantics():
    handover = {
        "tool": {"id": "bovie"},
        "to": {"holder": "operative_recipient", "location": "hand_unspecified"},
    }
    assert map_reference_location(handover, _world()) == (
        "surgeon_hand",
        "surgeon_hand",
        "operative_recipient",
    )
    mayo = {
        "tool": {"id": "bovie"},
        "to": {"holder": "none", "location": "mayo_stand"},
    }
    assert map_reference_location(mayo, _world()) == (
        "mayo_reuse_zone",
        "mayo_reuse_zone",
        "mayo",
    )


def test_reference_mapping_uses_runtime_home_anchor_for_nurse_pickup():
    pickup = {
        "tool": {"id": "bovie"},
        "to": {"holder": "scrub_nurse", "location": "hand_unspecified"},
    }
    assert map_reference_location(pickup, _world()) == (
        "instrument_rack",
        "rack_bovie",
        "runtime_home_anchor",
    )


def test_reference_mapping_can_use_runtime_tool_id():
    pickup = {
        "tool": {"id": "dataset_bovie"},
        "to": {"holder": "scrub_nurse", "location": "hand_unspecified"},
    }
    assert map_reference_location(pickup, _world(), "bovie") == (
        "instrument_rack",
        "rack_bovie",
        "runtime_home_anchor",
    )

def test_flat_reference_mapping_supports_minimal_v5_endpoints():
    handover = {
        "tool": "bovie",
        "from": "scrub_nurse",
        "to": "surgeon",
    }
    assert map_reference_location(handover, _world()) == (
        "surgeon_hand",
        "surgeon_hand",
        "operative_recipient",
    )
    pickup = {
        "tool": "bovie",
        "from": "mayo_stand",
        "to": "scrub_nurse",
    }
    assert map_reference_location(pickup, _world()) == (
        "robot_right_hand",
        "robot_right_hand",
        "humanoid_handover_hand",
    )
    recovery = {
        "tool": "bovie",
        "from": "surgeon",
        "to": "scrub_nurse",
    }
    assert map_reference_location(recovery, _world()) == (
        "robot_left_hand",
        "robot_left_hand",
        "humanoid_recovery_hand",
    )


def test_runtime_tool_map_uses_first_procedure_reference(tmp_path):
    catalog = tmp_path / "tools.yaml"
    catalog.write_text(
        "tools:\n"
        "  - id: scalpel\n"
        "    procedure_refs: [T01]\n"
        "  - id: dataset_only\n"
        "    procedure_refs: []\n",
        encoding="utf-8",
    )
    assert load_runtime_tool_map(catalog) == {"scalpel": "T01"}
