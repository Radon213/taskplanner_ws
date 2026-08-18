from pathlib import Path

import pytest

from tools.real_surgery_annotation.artifact_path_contract import (
    resolve_repo_artifact_identity,
)
from tools.real_surgery_annotation.audit_marlin_policy02_coverage import (
    DEFAULT_CASES,
    build_report,
)
from tools.real_surgery_annotation.run_marlin2_proposals import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[2]


def _relocation_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    repo_root = tmp_path / "current"
    relative = Path("annotations/observable_tool_events/cases/demo/artifact.bin")
    expected = repo_root / relative
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"canonical artifact")
    declared = tmp_path / "historical" / relative
    return repo_root, expected, declared, sha256_file(expected)


def test_relocated_repo_artifact_requires_full_identity(tmp_path: Path) -> None:
    repo_root, expected, declared, digest = _relocation_fixture(tmp_path)

    assert resolve_repo_artifact_identity(
        str(declared),
        expected_path=expected,
        repo_root=repo_root,
        expected_sha256=digest,
        label="fixture",
    ) == expected.resolve()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("hash", "SHA-256 mismatch"),
        ("outside", "outside the repository"),
        ("suffix", "repository-relative artifact identity"),
        ("conflict", "conflicting path"),
        ("symlink", "conflicting path"),
        ("symlink_parent", "conflicting path"),
    ],
)
def test_relocated_repo_artifact_rejects_ambiguous_identity(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    repo_root, expected, declared, digest = _relocation_fixture(tmp_path)
    if mutation == "hash":
        digest = "0" * 64
    elif mutation == "outside":
        expected = tmp_path / "outside.bin"
        expected.write_bytes(b"canonical artifact")
    elif mutation == "suffix":
        declared = declared.parent / "other.bin"
    elif mutation == "conflict":
        declared.parent.mkdir(parents=True)
        declared.write_bytes(b"conflicting artifact")
    elif mutation == "symlink":
        declared.parent.mkdir(parents=True)
        declared.symlink_to(expected)
    elif mutation == "symlink_parent":
        historical_root = declared.parents[4]
        historical_root.parent.mkdir(parents=True, exist_ok=True)
        historical_root.symlink_to(repo_root)

    with pytest.raises(ValueError, match=message):
        resolve_repo_artifact_identity(
            str(declared),
            expected_path=expected,
            repo_root=repo_root,
            expected_sha256=digest,
            label="fixture",
        )


def test_actual_full_scan_clip_union_covers_every_observable_segment() -> None:
    report = build_report(REPO_ROOT, DEFAULT_CASES)

    assert report["ok"] is True
    assert report["counts"] == {
        "case_count": 11,
        "passed_case_count": 11,
        "failed_case_count": 0,
        "scan_run_count": 15,
        "completed_anchor_count": 115,
    }
    by_case = {item["case_id"]: item for item in report["cases"]}
    assert by_case["0704_8"]["run_count"] == 3
    assert by_case["0704_14"]["run_count"] == 3
    for case in by_case.values():
        assert case["ok"] is True
        assert case["coverage"]["observable_coverage_ratio"] == 1.0
        assert case["uncovered_intervals"] == []
