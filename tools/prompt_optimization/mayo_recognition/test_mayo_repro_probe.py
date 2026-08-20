from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).with_name("mayo_repro_probe.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("mayo_repro_probe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_prior_request_hash_lookup_reads_only_model_input_record(tmp_path):
    halted = tmp_path / "halted.json"
    halted.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "input": {
                            "sample_id": "0704_5-initial-crop-03",
                            "request_sha256": "request-hash",
                        },
                        "evaluation_reference": "must_not_be_read_by_lookup",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert module._find_prior_request_sha256(halted, "0704_5-initial-crop-03") == "request-hash"


def test_prior_request_hash_lookup_rejects_missing_input_hash(tmp_path):
    halted = tmp_path / "halted.json"
    halted.write_text(json.dumps({"records": []}), encoding="utf-8")
    with pytest.raises(module.evaluator.EvaluationError, match="no input hash"):
        module._find_prior_request_sha256(halted, "0704_5-initial-crop-03")


def test_unknown_model_has_no_worker_process_snapshot():
    assert module.worker_process_snapshot("definitely-not-a-ninfer-model") == []


def test_non_image_request_hash_changes_only_when_non_image_fields_change():
    body_a = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "same task"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
                ],
            }
        ]
    }
    body_b = json.loads(json.dumps(body_a))
    body_b["messages"][0]["content"][1]["image_url"]["url"] = "data:image/jpeg;base64,BBB"
    assert module._non_image_request_sha256(body_a) == module._non_image_request_sha256(body_b)
    body_b["messages"][0]["content"][0]["text"] = "different task"
    assert module._non_image_request_sha256(body_a) != module._non_image_request_sha256(body_b)
