from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import run_ninfer_eval as evaluator  # noqa: E402
from run_ninfer_eval import (  # noqa: E402
    FROZEN_DIAGNOSTIC_LOCK_SCHEMA,
    binary_metrics,
    canonical_json,
    is_recoverable_transport_error,
    request_model,
    safe_content,
    validate_input_contract,
    validate_variant_split,
)
from prompt_contract import build_messages  # noqa: E402


def _row(target_decision: str, target_tool: str, prediction: dict | None) -> dict:
    return {
        "target": {"decision": target_decision, "tool_id": target_tool, "regime": "anticipatory"},
        "prediction": prediction,
        "latency_sec": 0.1,
        "error": "" if prediction else "invalid",
    }


def test_exact_top1_does_not_credit_wrong_tool_as_true_positive() -> None:
    rows = [
        _row(
            "handover",
            "bovie",
            {"decision": "handover", "tool_id": "adson_forceps", "confidence": 0.9, "uncertainty": 0.1},
        ),
        _row(
            "none",
            "",
            {"decision": "none", "tool_id": "", "confidence": 0.9, "uncertainty": 0.1},
        ),
    ]
    score = binary_metrics(rows, 0.65)
    assert score["exact_top1_correct"] == 0
    assert score["tp"] == 0
    assert score["fp"] == 1
    assert score["fn"] == 1
    assert score["tn"] == 1
    assert score["wrong_tool_count"] == 1
    assert score["false_positive_on_none"] == 0
    assert score["specificity"] == 1.0
    assert score["balanced_accuracy"] == 0.5
    assert score["count"] == 2
    assert score["accuracy"] == 0.5


def test_request_payload_explicitly_disables_thinking(monkeypatch) -> None:
    sent: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(request, timeout):
        sent["body"] = request.data
        sent["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    messages = build_messages(
        variant="baseline_v0",
        frame_offsets_sec=(-6.0, -3.0, 0.0),
        public_asr=(),
        images=(
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
        ),
    )
    response, _raw, error = request_model(
        base_url="http://127.0.0.1:8080",
        model="qwen3.6-35b-a3b",
        messages=messages,
        temperature=0.0,
        top_p=1.0,
        seed=0,
        max_tokens=128,
        enable_thinking=False,
        timeout_sec=5.0,
    )
    assert error == ""
    assert response is not None
    assert b'"enable_thinking":false' in sent["body"]
    assert b'"reasoning_effort":"none"' in sent["body"]
    payload = json.loads(sent["body"])
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["role"] == "system"


def test_only_worker_availability_errors_trigger_a_single_retry_policy() -> None:
    assert is_recoverable_transport_error("http_502:worker closed")
    assert is_recoverable_transport_error("http_503:unavailable")
    assert is_recoverable_transport_error("transport:timed out")
    assert not is_recoverable_transport_error("http_400:bad request")
    assert not is_recoverable_transport_error("response_json:invalid")


def test_content_block_response_is_normalized_before_strict_json_parse() -> None:
    response = {"choices": [{"message": {"content": [{"text": "{"}, {"text": "}"}]}}]}
    assert safe_content(response) == "{}"


def test_v3_variants_are_calibration_only() -> None:
    validate_variant_split("optimized_v3", "development_calibration")
    validate_variant_split("optimized_v3_diagnostic", "development_calibration")
    with pytest.raises(Exception, match="calibration-only"):
        validate_variant_split("optimized_v3", "development_challenge")
    with pytest.raises(Exception, match="calibration-only"):
        validate_variant_split("optimized_v3_diagnostic", "final_holdout")


def test_timestamped_variant_rejects_plain_manifest_before_model_request() -> None:
    plain = [{"example_id": "x", "public_context": {"asr": [], "asr_input_format": "plain"}}]
    timed = [
        {
            "example_id": "x",
            "public_context": {"asr": [], "asr_input_format": "timestamped_relative"},
        }
    ]
    validate_input_contract(plain, "baseline_v0")
    validate_input_contract(timed, "optimized_v3")
    with pytest.raises(Exception, match="requires timestamped_relative"):
        validate_input_contract(plain, "optimized_v3")
    with pytest.raises(Exception, match="requires plain"):
        validate_input_contract(timed, "baseline_v0")


def test_frozen_failed_candidate_requires_exact_manifest_prompt_and_output(tmp_path, monkeypatch) -> None:
    """The sole post-calibration exception cannot become a generic v3 override."""

    monkeypatch.setattr(evaluator, "RUNS_ROOT", tmp_path)
    lock_dir = tmp_path / "lock"
    benchmark_dir = tmp_path / "benchmark"
    lock_dir.mkdir()
    benchmark_dir.mkdir()
    input_row = {
        "example_id": "ntf:example",
        "public_context": {"asr": [], "asr_input_format": "timestamped_relative"},
    }
    labels_row = {"example_id": "ntf:example", "split": "development_challenge", "target": {}}
    inputs_path = benchmark_dir / "inputs.jsonl"
    labels_path = benchmark_dir / "labels.jsonl"
    inputs_path.write_text(json.dumps(input_row) + "\n", encoding="utf-8")
    labels_path.write_text(json.dumps(labels_row) + "\n", encoding="utf-8")
    system, developer = evaluator.prompts("optimized_v3")
    config = {
        "variant": "optimized_v3",
        "model": "qwen3.6-35b-a3b",
        "input_contract": "timestamped_relative_asr",
        "output_contract": "deployable_four_key",
        "prompt_sha256": {
            "system": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "developer": hashlib.sha256(developer.encode("utf-8")).hexdigest(),
        },
        "generation": {
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": 0,
            "max_tokens": 128,
            "enable_thinking": False,
            "threshold": 0.65,
        },
        "execution_guard": {
            "batch_size": 1,
            "automatic_transport_retry": False,
            "manager_reload_before_each_batch": True,
            "manager_loaded_vision_check": True,
            "direct_worker_catalog_check": True,
        },
    }
    output_dir = tmp_path / "authorized-output"
    lock = {
        "schema": FROZEN_DIAGNOSTIC_LOCK_SCHEMA,
        "candidate_status": "failed_candidate_diagnostic",
        "deployment_status": "non_deployable",
        "frozen_config": config,
        "frozen_config_sha256": hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest(),
        "source_calibration_run": {"run_dir": "calibration"},
        "suitability": {"status": "fail"},
        "evaluation_targets": {
            "development_challenge": {
                "output_dir": str(output_dir),
                "inputs_sha256": evaluator.sha256_file(inputs_path),
                "labels_sha256": evaluator.sha256_file(labels_path),
                "selected_example_ids": ["ntf:example"],
                "example_count": 1,
            }
        },
    }
    lock_path = lock_dir / "failed_candidate_diagnostic.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    args = argparse.Namespace(
        variant="optimized_v3",
        split="development_challenge",
        model="qwen3.6-35b-a3b",
        temperature=0.0,
        top_p=1.0,
        seed=0,
        max_tokens=128,
        enable_thinking=False,
        threshold=0.65,
        batch_size=1,
        overwrite=False,
    )
    proof = evaluator.validate_frozen_candidate_diagnostic(
        lock_path=lock_path,
        args=args,
        output_dir=output_dir,
        inputs_path=inputs_path,
        labels_path=labels_path,
        selected=[input_row],
    )
    assert proof["candidate_status"] == "failed_candidate_diagnostic"
    assert proof["deployment_status"] == "non_deployable"
    with pytest.raises(Exception, match="output directory differs"):
        evaluator.validate_frozen_candidate_diagnostic(
            lock_path=lock_path,
            args=args,
            output_dir=tmp_path / "unauthorized-output",
            inputs_path=inputs_path,
            labels_path=labels_path,
            selected=[input_row],
        )
    args.overwrite = True
    with pytest.raises(Exception, match="forbids overwrite"):
        evaluator.validate_frozen_candidate_diagnostic(
            lock_path=lock_path,
            args=args,
            output_dir=output_dir,
            inputs_path=inputs_path,
            labels_path=labels_path,
            selected=[input_row],
        )
