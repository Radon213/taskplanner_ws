from __future__ import annotations

import sys
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import build_runtime_ngram_prior as runtime_prior  # noqa: E402


def test_runtime_ngram_artifact_is_reproducible_from_the_fixed_calibration_split() -> None:
    rendered = runtime_prior.render_payload(runtime_prior.build_payload())
    artifact = (
        TASK_DIR.parents[2]
        / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
        / "tool_handover_ngram_prior.yaml"
    )

    assert artifact.read_text(encoding="utf-8") == rendered
    assert "0704_" not in rendered
    assert "time_sec" not in rendered
    assert "case_id" not in rendered


def test_runtime_ngram_builder_excludes_unsupported_tool_targets_without_joining_history() -> None:
    payload = runtime_prior.build_payload()
    metadata = payload["metadata"]

    assert metadata["eligible_transition_count"] == 101
    assert metadata["unsupported_transition_count"] == 7
    assert metadata["history_boundary"] == "unsupported_or_unknown_handover_resets_suffix"
    assert payload["target"].endswith("regardless_of_elapsed_time")
