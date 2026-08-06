from __future__ import annotations

from pathlib import Path

import pytest

from tools.real_surgery_annotation.check_information_boundary import (
    FORBIDDEN_RUNTIME_REFERENCES,
    check_boundary,
)


@pytest.mark.parametrize(
    ("reference_kind", "reference"),
    list(FORBIDDEN_RUNTIME_REFERENCES.items()),
)
def test_each_evaluation_reference_is_rejected_from_runtime_roots(
    tmp_path: Path,
    reference_kind: str,
    reference: str,
) -> None:
    runtime_file = tmp_path / "src" / "consumer.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text(
        f'REFERENCE = "{reference}"\n',
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is False
    assert report["schema"].endswith(".v2")
    assert report["violations"] == [
        {
            "path": "src/consumer.py",
            "line": 1,
            "text": f'REFERENCE = "{reference}"',
            "matches": [
                {
                    "reference_kind": reference_kind,
                    "reference": reference,
                }
            ],
        }
    ]


def test_annotation_tools_are_outside_runtime_scan(tmp_path: Path) -> None:
    tool_file = (
        tmp_path
        / "tools"
        / "real_surgery_annotation"
        / "offline_evaluator.py"
    )
    tool_file.parent.mkdir(parents=True)
    tool_file.write_text(
        'REFERENCE = "interaction_events.dt_reference.final.v2.jsonl"\n',
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is True
    assert report["checked_file_count"] == 0
    assert report["violations"] == []


def test_multiple_references_on_one_line_are_all_reported(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config" / "runtime.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "source: annotations/observable_tool_events/cases/"
        "0704_7/evaluation_masks.v1.json\n",
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is False
    assert [
        match["reference_kind"]
        for match in report["violations"][0]["matches"]
    ] == ["flat_case_root", "evaluation_masks"]


def test_runtime_test_files_are_not_scanned_as_deployed_consumers(
    tmp_path: Path,
) -> None:
    test_file = (
        tmp_path
        / "src"
        / "shadow_evaluation"
        / "test"
        / "test_ground_truth_timeline.py"
    )
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'REFERENCE = "interaction_events.observed.final.v2.jsonl"\n',
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is True
    assert report["checked_file_count"] == 0
    assert report["violations"] == []


def test_shadow_display_adapter_has_narrow_reference_allowlist(
    tmp_path: Path,
) -> None:
    adapter = (
        tmp_path
        / "src"
        / "shadow_evaluation"
        / "shadow_evaluation"
        / "interactive_replay_controller.py"
    )
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        'REFERENCE = "interaction_events.observed.final.v2.jsonl"\n',
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is True
    assert report["violations"] == []
    assert report["evaluation_display_references"][0]["matches"][0][
        "reference_kind"
    ] == "observed_final"


def test_shadow_display_allowlist_does_not_apply_to_other_runtime_nodes(
    tmp_path: Path,
) -> None:
    consumer = (
        tmp_path
        / "src"
        / "or_digital_twin"
        / "or_digital_twin"
        / "node.py"
    )
    consumer.parent.mkdir(parents=True)
    consumer.write_text(
        'REFERENCE = "interaction_events.observed.final.v2.jsonl"\n',
        encoding="utf-8",
    )

    report = check_boundary(tmp_path)

    assert report["ok"] is False
    assert report["violations"][0]["path"].endswith(
        "or_digital_twin/or_digital_twin/node.py"
    )
