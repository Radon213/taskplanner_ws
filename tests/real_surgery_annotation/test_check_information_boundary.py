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
