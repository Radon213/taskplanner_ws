from __future__ import annotations

import inspect
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from or_digital_twin.node import ORDigitalTwinNode
from procedure_spec import load_bundle
from std_msgs.msg import String
from surgical_msgs.msg import VLMHealth, VLMResult


def _prepare_result_handler(node: ORDigitalTwinNode) -> None:
    node._mayo_retrieve_stability = {}
    node._mayo_reuse_stability = {}
    node._publish_world_state_if_dirty = lambda: None
    node._publish_world_state = lambda: None
    node._stamp = lambda: None


def test_gesture_free_v6_result_reaches_tool_prediction_reducer() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        spec=load_bundle(spec_dir),
        state=SimpleNamespace(predicted_tool=""),
    )
    node._stamp_sec = lambda _stamp: 12.0
    _prepare_result_handler(node)
    observed: dict[str, object] = {}
    node._handle_vlm_tool_prediction = (
        lambda payload, msg, now_sec, received_sec: observed.update(
            payload=payload,
            schema_version=msg.schema_version,
            now_sec=now_sec,
            received_sec=received_sec,
        )
    )

    msg = VLMResult()
    msg.schema_version = "6"
    msg.raw_json = json.dumps(
        {
            "v": "6",
            "phase": [["P03", 0.9]],
            "tool": [["T02", 0.9]],
            "intent": ["none", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "bed_robot_arm_group": None,
        }
    )
    msg.phase_ids = ["P03"]
    msg.phase_confidences = [0.9]

    node._on_vlm_result(msg)

    assert observed["schema_version"] == "6"
    assert observed["now_sec"] == 12.0
    assert observed["received_sec"] == 12.0
    assert observed["payload"]["tool"] == [["T02", 0.9]]


def test_vlm_hand_fields_are_rejected_before_tool_reducer() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        state=SimpleNamespace(predicted_tool=""),
    )
    node._stamp_sec = lambda _stamp: 12.0
    _prepare_result_handler(node)
    decisions: list[dict] = []
    node._publish_reducer_decision_event = lambda **kwargs: decisions.append(kwargs)
    node._handle_vlm_tool_prediction = lambda *_args: (_ for _ in ()).throw(
        AssertionError("forbidden VLM hand payload reached the tool reducer")
    )

    msg = VLMResult()
    msg.source = "legacy_vlm:test"
    msg.schema_version = "4"
    msg.raw_json = json.dumps(
        {
            "v": "4",
            "phase": [["P03", 0.9]],
            "tool": [["T02", 0.9]],
            "intent": ["none", "", 0.0],
            "gesture": ["request_tool", "", "open_receive", 0.99],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "bed_robot_arm_group": None,
        }
    )

    node._on_vlm_result(msg)

    assert decisions[-1]["accepted"] is False
    assert decisions[-1]["reason"] == "vlm_hand_fields_forbidden"


def test_v4_ranked_tool_rows_are_preserved_for_fusion() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)

    assert node._vlm_tool_rows(
        {
            "v": "4",
            "tool": [["T02", 0.91], ["T04", 0.63]],
        }
    ) == [["T02", 0.91], ["T04", 0.63]]


@pytest.mark.parametrize("schema_version", ["5", "6"])
@pytest.mark.parametrize(
    "rows",
    [
        [["T02", 0.91]],
        [["T02", 0.91], ["T04", 0.63]],
        [["T02", 0.91], ["T04", 0.63], ["T07", 0.46]],
    ],
)
def test_v5_v6_ranked_tool_rows_are_preserved_for_fusion(
    schema_version: str,
    rows: list[list],
) -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)

    assert node._vlm_tool_rows(
        {
            "v": schema_version,
            "tool": rows,
        }
    ) == rows


@pytest.mark.parametrize("schema_version", ["5", "6"])
def test_v5_v6_ranked_tool_rows_ignore_malformed_confidence(
    schema_version: str,
) -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)

    assert node._vlm_tool_rows(
        {
            "v": schema_version,
            "tool": [
                ["T02", 0.91],
                ["T04", [0.63]],
                ["T07", "not-a-number"],
                ["T09", float("nan")],
            ],
        }
    ) == [["T02", 0.91]]


def test_reducer_preserves_deterministic_top_three_but_rank_one_owns_control() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._tool_predict_stability = {}
    node._tool_predict_evidence_threshold = 0.5
    node._tool_predict_threshold = 0.55
    node._tool_predict_stability_sec = 0.30
    state = SimpleNamespace(
        predicted_tool="",
        predicted_tool_confidence=0.0,
        predicted_tool_stability_sec=0.0,
        ranked_tool_predictions=[],
    )
    node._twin = SimpleNamespace(state=state)
    node._tool_prediction_sample_status = lambda **_kwargs: "accepted"
    node._fused_tool_prediction = lambda *_args: (
        "T02",
        0.91,
        {
            "fused": {"T07": 0.61, "T04": 0.73, "T09": 0.49, "T02": 0.91},
            "selected_duration_sec": 3.4,
        },
    )
    node._clear_stale_tool_prediction = lambda _now: None
    node._publish_reducer_decision_event = lambda **_kwargs: None
    node._publish_event = lambda *_args, **_kwargs: None

    node._handle_vlm_tool_prediction(
        {"v": "4", "tool": [["T07", 0.61], ["T02", 0.91], ["T04", 0.73]]},
        SimpleNamespace(source="real_vlm:test"),
        10.0,
        10.1,
    )

    assert state.predicted_tool == "T02"
    assert state.predicted_tool_confidence == 0.91
    assert state.predicted_tool_stability_sec == 3.4
    assert [row.instrument_id for row in state.ranked_tool_predictions] == [
        "T02",
        "T04",
        "T07",
    ]
    assert [row.rank for row in state.ranked_tool_predictions] == [1, 2, 3]
    assert sum(row.confidence for row in state.ranked_tool_predictions) == 1.0
    assert sum(
        round(row.confidence * 100) for row in state.ranked_tool_predictions
    ) == 100
    assert [row.stability_sec for row in state.ranked_tool_predictions] == [
        3.4,
        0.0,
        0.0,
    ]


def test_vlm_result_is_accepted_while_optional_perception_is_disabled() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._vlm_mode = "real"
    node._perception_health_seen = True
    node._perception_enabled = False
    node._input_source_status_by_id = {}
    node._visual_admission_by_channel = {}
    node._visual_runtime_epoch_floor = 0
    node._vlm_evidence_blocked = False
    health = VLMHealth()
    health.connected = True
    health.healthy = True
    node._vlm_health_by_topic = {
        "/vlm/health": (health, time.monotonic())
    }
    state = SimpleNamespace(predicted_tool="", safety_flags=[])
    node._twin = SimpleNamespace(
        spec=load_bundle(spec_dir),
        state=state,
        set_safety_flag=lambda flag, enabled: (
            state.safety_flags.append(flag)
            if enabled and flag not in state.safety_flags
            else state.safety_flags.remove(flag)
            if not enabled and flag in state.safety_flags
            else None
        ),
    )
    node._stamp_sec = lambda _stamp: 12.0
    _prepare_result_handler(node)
    observed: list[dict] = []
    node._handle_vlm_tool_prediction = (
        lambda payload, _msg, _now_sec, _received_sec: observed.append(payload)
    )

    msg = VLMResult()
    msg.source = "real_vlm:test"
    msg.source_epoch = 101
    msg.source_sequence = 1
    msg.correlation_id = "vlm-101-1-test"
    msg.schema_version = "4"
    msg.raw_json = json.dumps(
        {
            "v": "4",
            "phase": [["P03", 0.9]],
            "tool": [["T02", 0.9]],
            "intent": ["none", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "bed_robot_arm_group": None,
        }
    )

    node._on_vlm_result(msg)

    assert len(observed) == 1
    assert observed[0]["tool"] == [["T02", 0.9]]


def test_visual_admission_rejects_duplicate_and_previous_epoch() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._visual_admission_by_channel = {}
    node._visual_runtime_epoch_floor = 100
    node._stamp_sec = lambda stamp: (
        float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
    )

    current = VLMResult()
    current.source_epoch = 101
    current.source_sequence = 3
    current.correlation_id = "vlm-101-3-current"
    current.stamp.sec = 10

    assert node._admit_visual_evidence(
        current,
        channel="vlm_result",
        source="real_vlm:test",
        require_epoch=True,
    )
    assert not node._admit_visual_evidence(
        current,
        channel="vlm_result",
        source="real_vlm:test",
        require_epoch=True,
    )

    previous = VLMResult()
    previous.source_epoch = 100
    previous.source_sequence = 99
    previous.correlation_id = "vlm-100-99-stale"
    previous.stamp.sec = 11

    assert not node._admit_visual_evidence(
        previous,
        channel="vlm_result",
        source="real_vlm:test",
        require_epoch=True,
    )


def test_disabling_optional_detector_does_not_clear_vlm_phase_evidence() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    calls: list[str] = []
    node._twin = SimpleNamespace(
        clear_object_detection_evidence=lambda: calls.append("detector"),
        clear_perception_evidence=lambda: calls.append("all"),
    )
    node._run_time_based_maintenance = lambda: None
    node._world_maintenance_signature = lambda: ("unchanged",)
    node._last_world_emit_signature = ("unchanged",)
    node._emit_world_state = lambda: None

    msg = String()
    msg.data = json.dumps(
        {
            "schema": "taskplanner.rfdetr_health.v1",
            "enabled": False,
        }
    )

    node._on_perception_health(msg)

    assert calls == ["detector"]


def test_prediction_evidence_duration_is_separate_from_action_readiness() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    tracker: dict[str, dict] = {}

    stable, first_duration = node._update_stability(
        tracker,
        tool_id="T02",
        confidence=0.65,
        threshold=0.5,
        stability_sec=3.0,
        now_sec=10.0,
    )
    stable, second_duration = node._update_stability(
        tracker,
        tool_id="T02",
        confidence=0.65,
        threshold=0.5,
        stability_sec=3.0,
        now_sec=10.8,
    )

    assert stable is False
    assert first_duration == 0.0
    assert second_duration == pytest.approx(0.8)
    source = (
        inspect.getsource(ORDigitalTwinNode._fused_tool_prediction)
        + inspect.getsource(ORDigitalTwinNode._handle_vlm_tool_prediction)
    )
    assert "threshold=self._tool_predict_evidence_threshold" in source
    assert "confidence >= self._tool_predict_threshold" in source


def test_prediction_stability_does_not_run_backward() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    tracker: dict[str, dict] = {}

    node._update_stability(
        tracker,
        tool_id="T02",
        confidence=0.8,
        threshold=0.5,
        stability_sec=0.7,
        now_sec=10.0,
    )
    _stable, duration = node._update_stability(
        tracker,
        tool_id="T02",
        confidence=0.8,
        threshold=0.5,
        stability_sec=0.7,
        now_sec=9.5,
    )

    assert duration == 0.0
    assert tracker["T02"]["last_seen"] == 10.0


def test_prediction_expiry_uses_result_receipt_time_not_capture_time() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._vlm_evidence_max_gap_sec = 2.5
    node._tool_predict_stability = {}
    node._twin = SimpleNamespace(
        state=SimpleNamespace(
            predicted_tool="T02",
            predicted_tool_confidence=0.85,
            predicted_tool_stability_sec=3.2,
            ranked_tool_predictions=[SimpleNamespace(instrument_id="T02")],
        )
    )

    node._update_stability(
        node._tool_predict_stability,
        tool_id="T02",
        confidence=0.85,
        threshold=0.5,
        stability_sec=3.0,
        now_sec=10.0,
        received_sec=13.0,
    )

    node._clear_stale_tool_prediction(13.2)
    assert node._twin.state.predicted_tool == "T02"

    node._clear_stale_tool_prediction(15.6)
    assert node._twin.state.predicted_tool == ""
    assert node._twin.state.ranked_tool_predictions == []


def _prediction_fusion_node(
    *,
    prior_rows: list[list],
    available: set[str],
    prior_evidence: dict | None = None,
) -> ORDigitalTwinNode:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    spec = load_bundle(spec_dir)
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._tool_predict_stability = {}
    node._tool_predict_evidence_threshold = 0.5
    node._tool_predict_stability_sec = 0.30
    node._prior_scorer = SimpleNamespace(
        score=lambda _evidence: {
            "tool": prior_rows,
            "evidence": prior_evidence or {},
        }
    )
    node._runtime_prior_evidence = lambda: {}
    node._handover_ngram_prior = None
    node._twin = SimpleNamespace(
        spec=spec,
        state=SimpleNamespace(predicted_tool=""),
        get_available_instruments=lambda: sorted(available),
        _instances_for_type=lambda tool_id: (
            [
                SimpleNamespace(
                    lifecycle_stage=(
                        "home_rack"
                        if tool_id in available
                        else "surgeon_owned"
                    )
                )
            ]
            if any(instrument.id == tool_id for instrument in spec.bundle.instruments)
            else []
        ),
        get_instrument_state=lambda tool_id: SimpleNamespace(
            lifecycle_stage=(
                "home_rack" if tool_id in available else "surgeon_owned"
            )
        ),
    )
    return node


def test_procedure_prior_fills_only_currently_eligible_candidates() -> None:
    node = _prediction_fusion_node(
        prior_rows=[["T02", 1.0]],
        available={"T01", "T02"},
    )

    tool_id, confidence, detail = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.8]]},
        10.0,
    )

    assert tool_id == "T01"
    assert confidence == pytest.approx(0.8)
    assert detail["fused"]["T02"] == pytest.approx(0.15)
    assert detail["eligible_candidates"] == ["T01", "T02"]


def test_validated_procedure_path_can_create_reversible_preparation_candidate() -> None:
    node = _prediction_fusion_node(
        prior_rows=[["T02", 1.0]],
        available={"T01", "T02"},
        prior_evidence={
            "procedure_path_forecast": {
                "tool": "T02",
                "confidence": 0.86,
                "history": ["T02", "T02"],
                "history_source": "validated_requests",
                "match_length": 2,
            }
        },
    )

    tool_id, confidence, detail = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.72]]},
        10.0,
    )

    assert tool_id == "T02"
    assert confidence == pytest.approx(0.86)
    assert detail["path_available"] is True
    assert detail["procedure_path_forecast"]["match_length"] == 2


def test_strong_current_visual_forecast_can_override_procedure_path() -> None:
    node = _prediction_fusion_node(
        prior_rows=[["T02", 1.0]],
        available={"T01", "T02"},
        prior_evidence={
            "procedure_path_forecast": {
                "tool": "T02",
                "confidence": 0.86,
                "history": ["T02", "T02"],
                "history_source": "validated_requests",
                "match_length": 2,
            }
        },
    )

    tool_id, confidence, _ = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.97]]},
        10.0,
    )

    assert tool_id == "T01"
    assert confidence == pytest.approx(0.97)


def test_unavailable_vlm_candidate_is_removed_before_final_ranking() -> None:
    node = _prediction_fusion_node(
        prior_rows=[["T02", 1.0]],
        available={"T02"},
    )

    tool_id, confidence, detail = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.9]]},
        10.0,
    )

    assert tool_id == "T02"
    assert confidence == pytest.approx(0.15)
    assert detail["candidate_lifecycles"] == {"T02": ["home_rack"]}
    assert detail["fused"] == {"T02": pytest.approx(0.15)}
    assert "T01" in detail["filtered_candidates"]
    assert detail["degraded"] is True


def test_mayo_recovery_is_not_an_implicit_final_handover_candidate() -> None:
    node = _prediction_fusion_node(
        prior_rows=[["T02", 1.0]],
        available={"T01", "T02"},
    )
    node._twin._instances_for_type = lambda tool_id: [
        SimpleNamespace(
            lifecycle_stage=(
                "mayo_recovery" if tool_id == "T01" else "home_rack"
            )
        )
    ]

    tool_id, _confidence, detail = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.95]]},
        10.0,
    )

    assert tool_id == "T02"
    assert detail["eligible_candidates"] == ["T02"]
    assert "T01" in detail["filtered_candidates"]


def test_frozen_ngram_prior_fills_exact_system_top_three_with_unit_mass() -> None:
    node = _prediction_fusion_node(
        prior_rows=[],
        available={"T02", "T04", "T07", "T08"},
    )
    node._handover_ngram_prior = SimpleNamespace(
        predict=lambda **_kwargs: {
            "id": "frozen-0704-test",
            "match": "global",
            "support": 10,
            "candidates": [["T04", 0.6], ["T07", 0.3], ["T08", 0.1]],
        }
    )

    tool_id, confidence, detail = node._fused_tool_prediction(
        {"v": "4", "tool": [["T02", 0.8]]},
        10.0,
    )

    assert tool_id == "T02"
    assert confidence == pytest.approx(0.8)
    assert [row[0] for row in detail["ranked_distribution"]] == [
        "T02",
        "T04",
        "T07",
    ]
    assert sum(row[1] for row in detail["ranked_distribution"]) == 1.0
    assert sum(
        round(row[1] * 100) for row in detail["ranked_distribution"]
    ) == 100
    assert detail["ngram"] == {"T04": 0.6, "T07": 0.3, "T08": 0.1}
    assert detail["degraded"] is False


def test_prediction_continuity_resets_when_top_candidate_changes() -> None:
    node = _prediction_fusion_node(
        prior_rows=[],
        available={"T01", "T02"},
    )

    node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.9], ["T02", 0.7]]},
        10.0,
    )
    _, _, stable_t01 = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.9], ["T02", 0.7]]},
        11.0,
    )
    node._fused_tool_prediction(
        {"v": "4", "tool": [["T02", 0.9], ["T01", 0.7]]},
        12.0,
    )
    selected, _, restarted_t01 = node._fused_tool_prediction(
        {"v": "4", "tool": [["T01", 0.9], ["T02", 0.7]]},
        13.0,
    )

    assert stable_t01["selected_duration_sec"] == pytest.approx(1.0)
    assert selected == "T01"
    assert restarted_t01["selected_duration_sec"] == 0.0
    assert set(node._tool_predict_stability) == {"T01"}


def test_tool_prediction_temporal_sample_rejects_duplicate_and_older_frame() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._tool_prediction_last_sample_by_source = {}
    payload = {"v": "4", "tool": [["T01", 0.8]]}

    assert node._tool_prediction_sample_status(
        source="real_vlm:source_frame_live",
        now_sec=10.0,
        payload=payload,
    ) == "accepted"
    assert node._tool_prediction_sample_status(
        source="real_vlm:source_frame_live",
        now_sec=10.0,
        payload={"v": "4", "tool": [["T02", 0.9]]},
    ) == "duplicate_tool_prediction_observation"
    assert node._tool_prediction_sample_status(
        source="real_vlm:source_frame_live",
        now_sec=9.5,
        payload=payload,
    ) == "stale_out_of_order_tool_prediction"
    assert node._tool_prediction_sample_status(
        source="real_vlm:speech",
        now_sec=10.0,
        payload=payload,
    ) == "accepted"
