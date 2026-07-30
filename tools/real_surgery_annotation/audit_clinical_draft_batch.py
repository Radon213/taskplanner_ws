#!/usr/bin/env python3
"""Fail-closed data-quality audit for two-field clinical AI drafts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .clinical_review_store import ClinicalReviewStore


DEFAULT_CASES = tuple(f"0704_{index}" for index in range(7, 18))
CLINICAL_CANDIDATE_NAME = "clinical_candidates.codex_5_6_sol.v2.jsonl"
CLINICAL_MANIFEST_NAME = "clinical_manifest.v2.json"
RFDETR_AUTHORITY = "ai_inference_reference_not_ground_truth"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path}:{line_number}: JSON 객체가 필요합니다."
                )
            records.append(value)
    return records


def _resolve_inside(
    *,
    base: Path,
    relative: Any,
    allowed_root: Path,
    label: str,
) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} 경로가 없습니다.")
    value = Path(relative)
    if value.is_absolute():
        raise ValueError(f"{label}은 상대 경로여야 합니다.")
    path = (base / value).resolve()
    root = allowed_root.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{label} 경로가 repo 밖입니다: {path}")
    if not path.is_file():
        raise ValueError(f"{label} 파일이 없습니다: {path}")
    return path


def _phase_counts(
    *,
    phases: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for index, phase in enumerate(phases):
        phase_id = str(phase.get("phase_id", ""))
        start = int(phase["source_frame_idx"])
        end = (
            int(phases[index + 1]["source_frame_idx"])
            if index + 1 < len(phases)
            else None
        )
        counts[phase_id] = sum(
            int(candidate["anchor_source_frame_idx"]) >= start
            and (
                end is None
                or int(candidate["anchor_source_frame_idx"]) < end
            )
            for candidate in candidates
        )
    return counts


def _evidence_union_sec(candidates: list[dict[str, Any]]) -> float:
    intervals = sorted(
        (
            float(candidate["evidence_start_sec"]),
            float(candidate["evidence_end_sec"]),
        )
        for candidate in candidates
    )
    if not intervals:
        return 0.0
    total = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def audit_case(repo_root: Path, case_id: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    clinical_case_dir = (
        repo_root / "annotations/clinical_video/cases" / case_id
    )
    observable_case_dir = (
        repo_root / "annotations/observable_tool_events/cases" / case_id
    )
    manifest_path = clinical_case_dir / CLINICAL_MANIFEST_NAME
    result: dict[str, Any] = {
        "case_id": case_id,
        "manifest": str(manifest_path.relative_to(repo_root)),
        "errors": errors,
        "warnings": warnings,
    }
    try:
        manifest = load_json(manifest_path)
        observable_manifest = load_json(
            observable_case_dir / "annotation_manifest.json"
        )
        source_timeline = manifest.get("source_timeline")
        if not isinstance(source_timeline, dict):
            raise ValueError("source_timeline descriptor가 없습니다.")
        timeline_path = _resolve_inside(
            base=clinical_case_dir,
            relative=source_timeline.get("file"),
            allowed_root=repo_root,
            label="source timeline",
        )
        if sha256_file(timeline_path) != source_timeline.get("sha256"):
            errors.append("source timeline SHA-256 mismatch")
        timeline = load_json(timeline_path)
        store = ClinicalReviewStore(
            case_dir=clinical_case_dir,
            case_id=case_id,
            source_timeline=timeline,
            source_timeline_path=timeline_path,
            manifest_path=manifest_path,
        )
        candidates = store.candidates()
        state = store.state()
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        result["ok"] = False
        return result

    candidate_count = len(candidates)
    if not 12 <= candidate_count <= 24:
        errors.append(
            f"임상 candidate 수가 권장 범위 12..24 밖입니다: {candidate_count}"
        )
    if state["candidate_source"]["status"] != "ready":
        errors.append(
            "candidate source status가 ready가 아닙니다: "
            f"{state['candidate_source']['status']}"
        )
    if state["progress"]["reviewed"] != 0:
        warnings.append(
            "사람 검수 action이 이미 존재합니다; candidate-only batch가 아닙니다."
        )

    anchors = [
        int(candidate["anchor_source_frame_idx"]) for candidate in candidates
    ]
    if anchors != sorted(anchors) or len(set(anchors)) != len(anchors):
        errors.append("clinical anchors가 엄격한 오름차순/고유값이 아닙니다.")
    combined_narratives = [
        (
            str(candidate["observation"]).strip(),
            str(candidate["interpretation"]).strip(),
        )
        for candidate in candidates
    ]
    if len(set(combined_narratives)) != len(combined_narratives):
        errors.append("중복 observation/interpretation 쌍이 있습니다.")
    if any(
        observation == interpretation
        for observation, interpretation in combined_narratives
    ):
        errors.append("observation과 interpretation이 같은 candidate가 있습니다.")

    now = datetime.now(timezone.utc)
    future_ids: list[str] = []
    for candidate in candidates:
        if candidate.get("review_status") != "needs_surgeon_review":
            errors.append(
                f"{candidate.get('annotation_id')}: AI draft review_status 오류"
            )
        provenance = candidate.get("provenance", {})
        if provenance.get("authority") != "ai_draft":
            errors.append(
                f"{candidate.get('annotation_id')}: provenance authority 오류"
            )
        generated_at = datetime.fromisoformat(
            str(provenance["generated_at"]).replace("Z", "+00:00")
        )
        if generated_at.astimezone(timezone.utc) > now:
            future_ids.append(str(candidate["annotation_id"]))
    if future_ids:
        errors.append(f"미래 generated_at candidate: {future_ids}")

    context_sources = manifest.get("context_sources")
    if not isinstance(context_sources, list):
        errors.append("context_sources가 배열이 아닙니다.")
        context_sources = []
    context_files: dict[Path, dict[str, Any]] = {}
    for index, descriptor in enumerate(context_sources):
        try:
            if not isinstance(descriptor, dict):
                raise ValueError("descriptor가 객체가 아닙니다.")
            path = _resolve_inside(
                base=clinical_case_dir,
                relative=descriptor.get("file"),
                allowed_root=repo_root,
                label=f"context_sources[{index}]",
            )
            if sha256_file(path) != descriptor.get("sha256"):
                errors.append(
                    f"context SHA-256 mismatch: {path.relative_to(repo_root)}"
                )
            context_files[path] = descriptor
        except Exception as exc:
            errors.append(f"context_sources[{index}]: {exc}")

    evaluation = observable_manifest.get("evaluation_reference", {})
    for key in ("observed_reference", "dt_reference", "phase_reference"):
        descriptor = evaluation.get(key)
        if not isinstance(descriptor, dict):
            errors.append(f"observable manifest {key}가 없습니다.")
            continue
        source_path = (
            observable_case_dir / str(descriptor.get("file", ""))
        ).resolve()
        clinical_descriptor = context_files.get(source_path)
        if clinical_descriptor is None:
            errors.append(f"clinical context에 current {key}가 없습니다.")
        elif clinical_descriptor.get("sha256") != descriptor.get("sha256"):
            errors.append(f"clinical/current {key} SHA-256가 다릅니다.")

    speech = observable_manifest.get("speech_timeline", {})
    voice_path = (
        observable_case_dir / str(speech.get("file", ""))
    ).resolve()
    if context_files.get(voice_path, {}).get("sha256") != speech.get("sha256"):
        errors.append("clinical context에 current voice timeline이 없습니다.")

    rfdetr_sources = [
        descriptor
        for descriptor in context_sources
        if isinstance(descriptor, dict)
        and descriptor.get("role") == RFDETR_AUTHORITY
        and descriptor.get("authority") == RFDETR_AUTHORITY
    ]
    if len(rfdetr_sources) != 1:
        errors.append(
            f"RF-DETR non-GT context descriptor가 정확히 1개가 아닙니다: "
            f"{len(rfdetr_sources)}"
        )
    audit_reports = [
        descriptor
        for descriptor in context_sources
        if isinstance(descriptor, dict)
        and descriptor.get("role") == "audit_report"
    ]
    if len(audit_reports) != 1:
        errors.append(
            f"clinical audit report descriptor가 정확히 1개가 아닙니다: "
            f"{len(audit_reports)}"
        )

    review_media = manifest.get("review_media")
    if not isinstance(review_media, dict):
        errors.append("review_media descriptor가 없습니다.")
    else:
        media_path = Path(str(review_media.get("file", ""))).resolve()
        if not media_path.is_file():
            errors.append(f"review media가 없습니다: {media_path}")
        elif sha256_file(media_path) != review_media.get("sha256"):
            errors.append("review media SHA-256 mismatch")
        if review_media.get("video_frame_count") != timeline.get("frame_count"):
            errors.append("review media/timeline frame_count mismatch")

    phase_descriptor = evaluation.get("phase_reference", {})
    phase_path = (
        observable_case_dir / str(phase_descriptor.get("file", ""))
    ).resolve()
    phases = load_jsonl(phase_path)
    expected_phase_ids = ["P03", "P04", "P05", "P06"]
    actual_phase_ids = [str(phase.get("phase_id")) for phase in phases]
    if actual_phase_ids != expected_phase_ids:
        errors.append(
            f"functional Phase 순서가 {expected_phase_ids}가 아닙니다: "
            f"{actual_phase_ids}"
        )
    phase_candidate_counts = _phase_counts(
        phases=phases,
        candidates=candidates,
    )
    empty_phases = [
        phase_id
        for phase_id in expected_phase_ids
        if phase_candidate_counts.get(phase_id, 0) == 0
    ]
    if empty_phases:
        errors.append(f"clinical anchor가 없는 Phase: {empty_phases}")

    timestamps = [float(value) for value in timeline["timestamps_sec"]]
    anchor_times = [
        float(candidate["anchor_sec"]) for candidate in candidates
    ]
    edge_and_anchor_gaps = (
        [anchor_times[0] - timestamps[0]]
        + [
            right - left
            for left, right in zip(anchor_times, anchor_times[1:])
        ]
        + [timestamps[-1] - anchor_times[-1]]
        if anchor_times
        else [timestamps[-1] - timestamps[0]]
    )
    max_anchor_gap_sec = max(edge_and_anchor_gaps)
    if max_anchor_gap_sec > 20.0:
        errors.append(
            f"clinical anchor 최대 공백이 20초를 초과합니다: "
            f"{max_anchor_gap_sec:.3f}s"
        )

    evidence_union_sec = _evidence_union_sec(candidates)
    visual_span_sec = timestamps[-1] - timestamps[0]
    result.update(
        {
            "candidate_count": candidate_count,
            "candidate_sha256": sha256_file(
                clinical_case_dir / CLINICAL_CANDIDATE_NAME
            ),
            "manifest_sha256": sha256_file(manifest_path),
            "visual_frame_count": len(timestamps),
            "visual_span_sec": visual_span_sec,
            "evidence_union_sec": evidence_union_sec,
            "evidence_union_percent": (
                round(evidence_union_sec * 100.0 / visual_span_sec, 1)
                if visual_span_sec > 0
                else 100.0
            ),
            "max_anchor_gap_sec": round(max_anchor_gap_sec, 6),
            "phase_candidate_counts": phase_candidate_counts,
            "source_view_counts": dict(
                sorted(
                    Counter(
                        view
                        for candidate in candidates
                        for view in candidate["source_views"]
                    ).items()
                )
            ),
            "observation_confidence_counts": dict(
                sorted(
                    Counter(
                        str(candidate["confidence"]["observation"])
                        for candidate in candidates
                    ).items()
                )
            ),
            "interpretation_confidence_counts": dict(
                sorted(
                    Counter(
                        str(candidate["confidence"]["interpretation"])
                        for candidate in candidates
                    ).items()
                )
            ),
        }
    )
    result["ok"] = not errors
    return result


def audit_batch(repo_root: Path, case_ids: Iterable[str]) -> dict[str, Any]:
    cases = [audit_case(repo_root, case_id) for case_id in case_ids]
    return {
        "schema": "taskplanner.clinical_draft_batch_audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authority": "deterministic_read_only_validation",
        "cases": cases,
        "counts": {
            "case_count": len(cases),
            "passed_case_count": sum(case["ok"] for case in cases),
            "failed_case_count": sum(not case["ok"] for case in cases),
            "candidate_count": sum(
                int(case.get("candidate_count", 0)) for case in cases
            ),
        },
        "ok": all(case["ok"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        default=list(DEFAULT_CASES),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_batch(args.repo_root.resolve(), args.cases)
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        if output.exists():
            raise SystemExit(f"create-only output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
