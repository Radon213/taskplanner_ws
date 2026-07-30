from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.real_surgery_annotation import generate_policy02_final_batch_summary
from tools.real_surgery_annotation.publish_assistant_case_reference import (
    sha256_file,
)


def _write_summary_inputs(
    root: Path,
    *,
    bundle_revision: str = "current-revision",
) -> tuple[Path, Path, Path, list[Path]]:
    cases: list[dict[str, object]] = []
    coverage_cases: list[dict[str, str]] = []
    manifest_paths: list[Path] = []
    for index in range(7, 18):
        case_id = f"0704_{index}"
        manifest_path = root / "cases" / case_id / "annotation_manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "test_bundle_revision": bundle_revision,
                    "evaluation_reference": {
                        "observed_reference": {
                            "event_type_counts": {
                                "implicit_tool_request": 0,
                                "tool_transfer": 0,
                            }
                        },
                        "dt_reference": {
                            "event_type_counts": {"tool_transfer": 0}
                        },
                        "phase_reference": {"file": "phase.jsonl"},
                        "evaluation_masks": {"file": "masks.json"},
                    },
                }
            ),
            encoding="utf-8",
        )
        manifest_paths.append(manifest_path)
        cases.append(
            {
                "case_id": case_id,
                "ok": True,
                "manifest": str(manifest_path.relative_to(root)),
                "manifest_sha256": sha256_file(manifest_path),
                "bundle_revision": bundle_revision,
                "counts": {
                    "observed": 0,
                    "dt_reference": 0,
                    "gesture_targets": 0,
                    "action_targets": 1,
                    "effective_action_targets": 1,
                    "phase": 4,
                    "voice": 0,
                },
            }
        )
        coverage_cases.append({"case_id": case_id})

    batch_path = root / "batch.json"
    batch_path.write_text(
        json.dumps({"ok": True, "cases": cases}),
        encoding="utf-8",
    )
    coverage_path = root / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "ok": True,
                "cases": coverage_cases,
                "counts": {
                    "scan_run_count": 1,
                    "completed_anchor_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    marlin_path = root / "marlin.json"
    marlin_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "counts": {
                    "failed_or_blocked_count": 0,
                    "completed_count": 1,
                    "job_count": 1,
                },
                "concurrency": {"hard_limit": 2, "max_processes": 2},
                "model": {"id": "model", "revision": "revision"},
            }
        ),
        encoding="utf-8",
    )
    return batch_path, coverage_path, marlin_path, manifest_paths


class _FakeFinalReviewBundle:
    def __init__(self, *, manifest_path: Path) -> None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.manifest = manifest
        self.revision = str(manifest["test_bundle_revision"])


def _build(
    root: Path,
    batch_path: Path,
    coverage_path: Path,
    marlin_path: Path,
) -> str:
    return generate_policy02_final_batch_summary.build_summary(
        repo_root=root,
        batch_audit_path=batch_path,
        coverage_audit_path=coverage_path,
        marlin_batch_path=marlin_path,
    )


def test_summary_accepts_manifest_and_bundle_bound_to_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _write_summary_inputs(tmp_path)
    monkeypatch.setattr(
        generate_policy02_final_batch_summary,
        "FinalReviewBundle",
        _FakeFinalReviewBundle,
    )

    assert "# 0704_07–0704_17" in _build(tmp_path, *inputs[:3])


def test_summary_rejects_manifest_changed_after_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, coverage_path, marlin_path, manifests = (
        _write_summary_inputs(tmp_path)
    )
    monkeypatch.setattr(
        generate_policy02_final_batch_summary,
        "FinalReviewBundle",
        _FakeFinalReviewBundle,
    )
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    manifest["post_audit_mutation"] = True
    manifests[0].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        _build(tmp_path, batch_path, coverage_path, marlin_path)


def test_summary_rejects_bundle_revision_changed_after_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, coverage_path, marlin_path, manifests = (
        _write_summary_inputs(tmp_path)
    )
    monkeypatch.setattr(
        generate_policy02_final_batch_summary,
        "FinalReviewBundle",
        _FakeFinalReviewBundle,
    )
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch["cases"][0]["bundle_revision"] = "stale-revision"
    batch_path.write_text(json.dumps(batch), encoding="utf-8")

    with pytest.raises(ValueError, match="bundle revision mismatch"):
        _build(tmp_path, batch_path, coverage_path, marlin_path)
