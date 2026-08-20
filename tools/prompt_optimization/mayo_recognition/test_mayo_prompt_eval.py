from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
import cv2
import numpy as np


MODULE_PATH = Path(__file__).with_name("mayo_prompt_eval.py")
SPEC = importlib.util.spec_from_file_location("mayo_prompt_eval", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
EVENTS = (
    WORKSPACE_ROOT
    / "annotations/observable_tool_events/cases/0704_5/tool_events.final.v1.jsonl"
)


def test_0704_5_calibration_uses_initial_labels_and_early_temporal_arrivals():
    samples = module.make_calibration_samples(module.load_events(EVENTS))
    assert [sample.mode for sample in samples].count("inventory") == 1
    assert [sample.mode for sample in samples].count("crop") == 11
    assert [sample.mode for sample in samples].count("arrival") == 2
    assert samples[0].expected == {
        "adson_forceps": 2,
        "allis_forceps": 2,
        "army_navy_retractor": 2,
        "bipolar_forceps": 1,
        "kocher_retractor": 2,
        "mosquito_forceps": 1,
        "scalpel": 1,
    }


def test_frozen_arrival_rule_is_time_separated_and_has_no_target_in_context():
    samples = module.make_frozen_arrival_samples(module.load_events(EVENTS))
    assert len(samples) == 5
    assert [sample.sample_id for sample in samples] == [
        f"0704_5-challenge-arrival-{event_id}"
        for event_id in module.FROZEN_CHALLENGE_EVENT_IDS
    ]
    for sample in samples:
        assert sample.mode == "arrival"
        assert sample.frame_indices[0] < sample.frame_indices[1]
        context = module.request_context_for(sample)
        module.assert_request_is_label_free(sample, context)
        assert "expected" not in json.dumps(context, sort_keys=True)
        assert "source_frame_idx" not in json.dumps(context, sort_keys=True)
        assert "event_id" not in json.dumps(context, sort_keys=True)


def test_request_body_keeps_reference_outside_model_messages():
    sample = module.Sample(
        sample_id="test-arrival",
        mode="arrival",
        frame_indices=(10, 14),
        expected="bovie",
    )
    body, context, _prompt = module.build_request_body(
        sample=sample,
        variant="optimized",
        images=[
            ("CAM4_BEFORE", b"before", "image/jpeg"),
            ("CAM4_AFTER", b"after", "image/jpeg"),
        ],
        model_id="qwen3.6-35b-a3b",
    )
    serialized_context = json.dumps(context, sort_keys=True)
    serialized_body = json.dumps(body, sort_keys=True)
    assert '"expected"' not in serialized_context
    assert '"expected"' not in serialized_body
    assert '"event_id"' not in serialized_body
    assert "CAM4_BEFORE" in serialized_body
    assert "CAM4_AFTER" in serialized_body


def test_json_parser_accepts_fenced_object_and_rejects_plain_text():
    assert module.parse_model_json('```json\n{"tool_id":"bovie","confidence":0.9}\n```') == {
        "tool_id": "bovie",
        "confidence": 0.9,
    }
    assert module.parse_model_json("the image is unclear") is None


def test_arrival_score_penalizes_extra_tool_predictions():
    sample = module.Sample(
        sample_id="test-arrival",
        mode="arrival",
        frame_indices=(10, 14),
        expected="bovie",
    )
    score = module.score_sample(
        sample,
        {"newly_on_mayo": [["bovie", 0.9], ["bipolar_forceps", 0.5]], "abstain": False},
    )
    assert score["target_recalled"] is True
    assert score["exact"] is False
    assert score["false_positives"] == ["bipolar_forceps"]


def test_output_contract_requires_explicit_abstain_key():
    sample = module.Sample(
        sample_id="test-inventory",
        mode="inventory",
        frame_indices=(0,),
        expected={"scalpel": 1},
    )
    assert module.output_contract_valid(
        sample,
        {"visible": [["scalpel", 1, 0.9]]},
    ) is False
    assert module.output_contract_valid(
        sample,
        {"visible": [["scalpel", 1, 0.9]], "abstain": False},
    ) is True


def test_summary_reports_semantic_and_contract_accepted_arrival_metrics_separately():
    summary = module.summarize(
        [
            {
                "mode": "arrival",
                "valid_json": True,
                "contract_valid": False,
                "target_recalled": True,
                "exact": True,
                "false_positives": [],
                "transport_error": False,
                "not_inferred": False,
            }
        ]
    )
    arrival = summary["arrival"]
    assert arrival["target_recall"] == 1.0
    assert arrival["exact_match"] == 1.0
    assert arrival["accepted_target_recall"] == 0.0
    assert arrival["accepted_exact_match"] == 0.0


def test_loaded_vision_model_health_is_accepted_but_unloaded_is_not():
    loaded = module.parse_model_health(
        {
            "data": [
                {
                    "id": "qwen3.6-35b-a3b",
                    "loaded": True,
                    "load_state": "loaded",
                    "capability": "vision",
                }
            ]
        },
        model_id="qwen3.6-35b-a3b",
    )
    assert loaded["model_loaded"] is True
    assert loaded["capability"] == "vision"
    unloaded = module.parse_model_health(
        {"data": [{"id": "qwen3.6-35b-a3b", "loaded": False, "load_state": "unloaded"}]},
        model_id="qwen3.6-35b-a3b",
    )
    assert unloaded["model_loaded"] is False


def test_direct_worker_readiness_requires_the_requested_model_id():
    ready = module.parse_direct_worker_readiness(
        {"data": [{"id": "qwen3.6-35b-a3b"}]},
        model_id="qwen3.6-35b-a3b",
    )
    assert ready == {"catalog_valid": True, "worker_model_present": True}
    missing = module.parse_direct_worker_readiness(
        {"data": [{"id": "another-model"}]},
        model_id="qwen3.6-35b-a3b",
    )
    assert missing["worker_model_present"] is False


def test_fresh_batch_hard_caps_inference_posts_without_network_calls():
    session = module.NInferEvalSession(
        base_url="http://manager.invalid",
        worker_base_url="http://worker.invalid",
        api_key="",
        model_id="qwen3.6-35b-a3b",
        timeout_sec=1.0,
        lifecycle_timeout_sec=1.0,
        lock_path=Path("/tmp/taskplanner-mayo-unit-test.lock"),
        batch_size=3,
    )

    class FakeRequests:
        class RequestException(Exception):
            pass

        def __init__(self):
            self.calls = 0

        def request(self, **_kwargs):
            self.calls += 1
            return object()

    fake = FakeRequests()
    session._requests = fake
    session._fresh_reload = lambda: {"status": "ready"}
    session.check_health = lambda _reason: {"model_loaded": True}
    session.check_direct_worker = lambda _reason: {"worker_ready": True}
    with session.fresh_batch(batch_index=1, sample_ids=["a", "b", "c"]):
        for _ in range(3):
            response, _latency = session._post_once({"test": True})
            assert response is not None
        with pytest.raises(module.EvaluationError, match="budget exhausted"):
            session._post_once({"would": "be a fourth POST"})
    assert fake.calls == 3
    assert session.batch_history[0]["inference_http_request_count"] == 3
    assert session.batch_history[0]["status"] == "completed"


def test_parse_args_rejects_more_than_three_requests_per_fresh_worker_batch():
    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--suite",
                "frozen_arrival",
                "--variant",
                "baseline",
                "--batch-size",
                "4",
            ]
        )


def test_optimized_v2_adds_review_driven_ring_and_bovie_guards():
    prompt = module.prompt_for("inventory", "optimized_v2")
    assert "cannot be Adson" in prompt
    assert "actual electrosurgical pencil/probe body" in prompt
    assert '"abstain"' in prompt


def test_optimized_v4_implements_the_approved_contract_and_abstention_guards():
    prompt = module.prompt_for("crop", "optimized_v4")
    assert module.prompt_version_for("optimized_v4") == "mayo-recognition-v4"
    assert "Contract self-check before emitting" in prompt
    assert "central body and working end" in prompt
    assert "cannot be Adson or bipolar forceps" in prompt
    assert "broad/serrated grasping jaws" in prompt
    assert "substantial retractor working end" in prompt
    assert "Return exactly one JSON object" in prompt
    assert "event_id" not in prompt
    assert "source_frame_idx" not in prompt


def _synthetic_jpeg(width: int = 267, height: int = 154) -> bytes:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (10, 90, 210)
    image[:, width // 2 :] = (190, 40, 8)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert ok
    return bytes(encoded)


def test_normalized_preprocess_manifest_covers_source_hash_target_hash_and_geometry():
    source = _synthetic_jpeg()
    images, manifest = module.preprocess_images_for_request(
        [("CAM4_MAYO", source, "image/jpeg")],
        image_preprocess=module.IMAGE_PREPROCESS_LETTERBOX_512_Q95,
    )
    assert len(images) == 1
    assert len(manifest) == 1
    row = manifest[0]
    assert row["source"]["sha256"] != row["normalized"]["sha256"]
    assert row["normalized"]["width_px"] == 512
    assert row["normalized"]["height_px"] == 512
    assert row["geometry"]["padding_bgr"] == [0, 0, 0]
    assert row["runtime_integrity"]["passed"] is True


def test_normalized_profile_is_calibration_baseline_complete_and_fresh_only():
    args = module.parse_args(
        [
            "--suite",
            "calibration",
            "--variant",
            "baseline",
            "--image-preprocess",
            "letterbox_512_q95",
            "--run-normalizer-unit-tests",
            "--batch-size",
            "1",
            "--retries",
            "0",
        ]
    )
    assert args.score_only_if_complete is True
    v4_args = module.parse_args(
        [
            "--suite",
            "calibration",
            "--variant",
            "optimized_v4",
            "--image-preprocess",
            "letterbox_512_q95",
            "--run-normalizer-unit-tests",
            "--batch-size",
            "1",
            "--retries",
            "0",
        ]
    )
    assert v4_args.score_only_if_complete is True
    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--suite",
                "frozen_arrival",
                "--variant",
                "baseline",
                "--image-preprocess",
                "letterbox_512_q95",
                "--run-normalizer-unit-tests",
                "--batch-size",
                "1",
            ]
        )


def test_frozen_v4_requires_a_locked_selection_argument_and_rejects_other_variants(tmp_path):
    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--suite",
                "frozen_arrival",
                "--variant",
                "optimized_v4",
                "--image-preprocess",
                "letterbox_512_q95",
                "--run-normalizer-unit-tests",
                "--batch-size",
                "1",
            ]
        )
    args = module.parse_args(
        [
            "--suite",
            "frozen_arrival",
            "--variant",
            "optimized_v4",
            "--image-preprocess",
            "letterbox_512_q95",
            "--run-normalizer-unit-tests",
            "--batch-size",
            "1",
            "--retries",
            "0",
            "--frozen-selection",
            str(tmp_path / "selection.json"),
        ]
    )
    assert args.score_only_if_complete is True
    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--suite",
                "frozen_arrival",
                "--variant",
                "optimized_v4",
                "--image-preprocess",
                "letterbox_512_q95",
                "--run-normalizer-unit-tests",
                "--batch-size",
                "1",
                "--retries",
                "0",
                "--frozen-selection",
                str(tmp_path / "selection.json"),
                "--dry-run",
            ]
        )


def test_frozen_selection_validator_locks_prompt_preprocess_threshold_and_samples(tmp_path):
    samples = [
        module.Sample(
            sample_id=f"0704_5-challenge-arrival-{event_id}",
            mode="arrival",
            frame_indices=(index, index + 1),
            expected="",
        )
        for index, event_id in enumerate(module.FROZEN_CHALLENGE_EVENT_IDS)
    ]
    event_hash = "event-reference-hash"
    frozen_config = module.frozen_config_for(
        model_id="qwen3.6-35b-a3b",
        event_reference_sha256=event_hash,
        samples=samples,
    )
    selection = {
        "schema": "taskplanner.mayo_frozen_selection.v1",
        "selection_status": "locked",
        "selection_id": "unit-test-selection",
        "frozen_config": frozen_config,
    }
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(selection), encoding="utf-8")
    validated = module.validate_frozen_selection(
        selection_path=path,
        model_id="qwen3.6-35b-a3b",
        event_reference_sha256=event_hash,
        samples=samples,
    )
    assert validated["status"] == "validated_locked_selection"
    selection["frozen_config"]["threshold_policy"]["confidence_threshold"] = 0.8
    path.write_text(json.dumps(selection), encoding="utf-8")
    with pytest.raises(module.EvaluationError, match="does not match"):
        module.validate_frozen_selection(
            selection_path=path,
            model_id="qwen3.6-35b-a3b",
            event_reference_sha256=event_hash,
            samples=samples,
        )
    assert module.NO_THRESHOLD_POLICY["confidence_threshold"] is None


def test_normalizer_unit_contract_runs_without_live_network_access():
    report = module.run_normalizer_unit_tests(
        image_preprocess=module.IMAGE_PREPROCESS_LETTERBOX_512_Q95,
        run_pytest=False,
    )
    assert report["status"] == "passed"
    assert report["synthetic_unit_contract"]["passed"] is True
