from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


MODULE_PATH = Path(__file__).with_name("render_mayo_review.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("mayo_review_renderer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_failure_reasons_keep_contract_and_arrival_errors_distinct():
    record = {
        "input": {"mode": "arrival"},
        "request_error": "",
        "score": {"valid_json": True, "contract_valid": False, "exact": False},
    }
    assert module._failure_reasons(record) == ["contract", "arrival-mismatch"]


def test_failure_sheet_is_written_from_post_inference_images(tmp_path):
    image = np.zeros((80, 160, 3), dtype=np.uint8)
    destination = tmp_path / "failure_review_sheet.jpg"
    module._write_failure_sheet([image], destination)
    assert destination.is_file()
    assert destination.stat().st_size > 0


def test_manifest_identity_excludes_runtime_only_validation_details():
    row = {
        "label": "CAM4_MAYO",
        "mime_type": "image/jpeg",
        "preprocessor": "letterbox",
        "source": {"sha256": "source"},
        "normalized": {"sha256": "normalized"},
        "geometry": {"padding_px": {"top_px": 1}},
        "codec": {"format": "jpeg"},
        "runtime_integrity": {"passed": True},
    }
    assert module._manifest_identity([row]) == [
        {
            "label": "CAM4_MAYO",
            "mime_type": "image/jpeg",
            "preprocessor": "letterbox",
            "source": {"sha256": "source"},
            "normalized": {"sha256": "normalized"},
            "geometry": {"padding_px": {"top_px": 1}},
            "codec": {"format": "jpeg"},
        }
    ]
