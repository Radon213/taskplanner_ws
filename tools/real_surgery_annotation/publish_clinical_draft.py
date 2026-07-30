#!/usr/bin/env python3
"""Publish and validate case-local two-field clinical AI drafts.

The candidate JSONL is authored separately after full-video review.  This
publisher derives every source descriptor from the current observable-label
manifest, canonical CAM4 timeline, cached review proxy, and RF-DETR overlay.
It never treats voice, interaction labels, Phase, or detector output as
clinical ground truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .clinical_review_store import ClinicalReviewStore
from .finalize_interaction_review import encode_json, publish_create_only


CLINICAL_CANDIDATE_NAME = "clinical_candidates.codex_5_6_sol.v2.jsonl"
CLINICAL_MANIFEST_NAME = "clinical_manifest.v2.json"
CLINICAL_ACTIONS_NAME = "clinical_review_actions.v2.jsonl"
CLINICAL_REFERENCE_NAME = "clinical_reference.final.v2.jsonl"
RFDETR_AUTHORITY = "ai_inference_reference_not_ground_truth"


class ClinicalDraftPublishError(ValueError):
    """A clinical draft cannot be published without losing provenance."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClinicalDraftPublishError(
            f"{label} JSON을 읽을 수 없습니다: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ClinicalDraftPublishError(f"{label}은 JSON 객체여야 합니다.")
    return value


def _jsonl_count(path: Path) -> int:
    count = 0
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ClinicalDraftPublishError(
                        f"{path}:{line_number}: JSONL 형식 오류"
                    ) from exc
                if not isinstance(value, dict):
                    raise ClinicalDraftPublishError(
                        f"{path}:{line_number}: JSON 객체가 필요합니다."
                    )
                count += 1
    except OSError as exc:
        raise ClinicalDraftPublishError(
            f"candidate JSONL을 읽을 수 없습니다: {path}"
        ) from exc
    if count < 1:
        raise ClinicalDraftPublishError("clinical candidate가 비어 있습니다.")
    return count


def _relative_file(
    *,
    base_dir: Path,
    relative: Any,
    allowed_root: Path,
    label: str,
) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ClinicalDraftPublishError(f"{label} 경로가 없습니다.")
    value = Path(relative)
    if value.is_absolute():
        raise ClinicalDraftPublishError(f"{label}은 상대 경로여야 합니다.")
    path = (base_dir / value).resolve()
    root = allowed_root.resolve()
    if path != root and root not in path.parents:
        raise ClinicalDraftPublishError(f"{label} 경로가 허용 범위를 벗어났습니다.")
    if not path.is_file():
        raise ClinicalDraftPublishError(f"{label} 파일이 없습니다: {path}")
    return path


def _context_descriptor(
    *,
    clinical_case_dir: Path,
    path: Path,
    role: str,
    authority: str | None = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "file": os.path.relpath(path.resolve(), clinical_case_dir.resolve()),
        "role": role,
        "sha256": _sha256_file(path),
    }
    if authority is not None:
        descriptor["authority"] = authority
    return descriptor


def build_manifest(
    *,
    repo_root: Path,
    case_id: str,
    generated_at: str,
    model: str = "Codex 5.6 sol",
    review_media_root: Path = Path(
        "/home/arl/.cache/taskplanner_annotation"
    ),
) -> dict[str, Any]:
    """Derive a clinical manifest from current canonical case descriptors."""

    repo_root = repo_root.resolve()
    observable_root = repo_root / "annotations/observable_tool_events"
    observable_case_dir = observable_root / "cases" / case_id
    clinical_case_dir = (
        repo_root / "annotations/clinical_video/cases" / case_id
    )
    candidate_path = clinical_case_dir / CLINICAL_CANDIDATE_NAME
    if not candidate_path.is_file():
        raise ClinicalDraftPublishError(
            f"candidate 파일이 없습니다: {candidate_path}"
        )
    candidate_count = _jsonl_count(candidate_path)

    observable_manifest_path = observable_case_dir / "annotation_manifest.json"
    observable_manifest = _load_json(
        observable_manifest_path,
        label="observable manifest",
    )
    if observable_manifest.get("case_id") != case_id:
        raise ClinicalDraftPublishError("observable manifest case_id가 다릅니다.")
    evaluation = observable_manifest.get("evaluation_reference")
    speech = observable_manifest.get("speech_timeline")
    minimal = observable_manifest.get("minimal_interaction_annotation")
    if not all(isinstance(value, dict) for value in (evaluation, speech, minimal)):
        raise ClinicalDraftPublishError(
            "observable manifest의 evaluation/speech/minimal 선언이 없습니다."
        )

    timeline_path = _relative_file(
        base_dir=observable_case_dir,
        relative=minimal.get("timeline_file"),
        allowed_root=observable_case_dir,
        label="CAM4 timeline",
    )
    timeline = _load_json(timeline_path, label="CAM4 timeline")
    timestamps = timeline.get("timestamps_sec")
    frame_count = timeline.get("frame_count")
    if (
        timeline.get("case_id") != case_id
        or not isinstance(timestamps, list)
        or frame_count != len(timestamps)
        or not timestamps
    ):
        raise ClinicalDraftPublishError("CAM4 timeline 구조가 올바르지 않습니다.")

    expected_timeline_sha256 = minimal.get("timeline_sha256")
    if _sha256_file(timeline_path) != expected_timeline_sha256:
        raise ClinicalDraftPublishError("CAM4 timeline SHA-256이 다릅니다.")

    descriptors: list[dict[str, Any]] = []
    voice_path = _relative_file(
        base_dir=observable_case_dir,
        relative=speech.get("file"),
        allowed_root=observable_case_dir,
        label="voice timeline",
    )
    if _sha256_file(voice_path) != speech.get("sha256"):
        raise ClinicalDraftPublishError("voice timeline SHA-256이 다릅니다.")
    descriptors.append(
        _context_descriptor(
            clinical_case_dir=clinical_case_dir,
            path=voice_path,
            role="context_only_not_ground_truth",
        )
    )

    reference_specs = (
        ("observed_reference", "context_only_not_clinical_ground_truth"),
        ("dt_reference", "context_only_not_clinical_ground_truth"),
        ("phase_reference", "context_only_not_clinical_ground_truth"),
    )
    for key, role in reference_specs:
        descriptor = evaluation.get(key)
        if not isinstance(descriptor, dict):
            raise ClinicalDraftPublishError(f"{key} descriptor가 없습니다.")
        path = _relative_file(
            base_dir=observable_case_dir,
            relative=descriptor.get("file"),
            allowed_root=observable_case_dir,
            label=key,
        )
        if _sha256_file(path) != descriptor.get("sha256"):
            raise ClinicalDraftPublishError(f"{key} SHA-256이 다릅니다.")
        descriptors.append(
            _context_descriptor(
                clinical_case_dir=clinical_case_dir,
                path=path,
                role=role,
            )
        )

    reaudit = evaluation.get("assistant_reaudit")
    if reaudit is not None:
        if not isinstance(reaudit, dict):
            raise ClinicalDraftPublishError(
                "assistant_reaudit descriptor가 올바르지 않습니다."
            )
        reaudit_path = _relative_file(
            base_dir=observable_case_dir,
            relative=reaudit.get("file"),
            allowed_root=observable_case_dir,
            label="assistant reaudit",
        )
        if _sha256_file(reaudit_path) != reaudit.get("sha256"):
            raise ClinicalDraftPublishError(
                "assistant reaudit SHA-256이 다릅니다."
            )
        descriptors.append(
            _context_descriptor(
                clinical_case_dir=clinical_case_dir,
                path=reaudit_path,
                role="context_only_not_clinical_ground_truth",
            )
        )

    overlay_path = (
        repo_root
        / "tools/real_surgery_annotation/web_interaction_review/"
        "rfdetr_overlays"
        / f"{case_id}.json"
    )
    overlay = _load_json(overlay_path, label="RF-DETR overlay")
    if (
        overlay.get("case_id") != case_id
        or overlay.get("authority") != RFDETR_AUTHORITY
        or overlay.get("frame_count") != frame_count
    ):
        raise ClinicalDraftPublishError("RF-DETR overlay case/frame/authority 불일치")
    descriptors.append(
        _context_descriptor(
            clinical_case_dir=clinical_case_dir,
            path=overlay_path,
            role=RFDETR_AUTHORITY,
            authority=RFDETR_AUTHORITY,
        )
    )

    review_media_manifest_path = (
        review_media_root
        / case_id
        / "review_corrected.mp4.manifest.json"
    )
    review_media_manifest = _load_json(
        review_media_manifest_path,
        label="review media manifest",
    )
    output = review_media_manifest.get("output")
    probe = output.get("media_probe") if isinstance(output, dict) else None
    video = probe.get("video") if isinstance(probe, dict) else None
    if (
        review_media_manifest.get("case_id") != case_id
        or not isinstance(output, dict)
        or not isinstance(probe, dict)
        or not isinstance(video, dict)
    ):
        raise ClinicalDraftPublishError("review media manifest 구조 불일치")
    review_media_path = Path(str(output.get("path", ""))).resolve()
    try:
        media_frame_count = int(video.get("nb_frames"))
        width = int(video["width"])
        height = int(video["height"])
        container_end_sec = float(probe["container_duration_sec"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ClinicalDraftPublishError(
            "review media frame/resolution/duration 값이 올바르지 않습니다."
        ) from exc
    if (
        not review_media_path.is_file()
        or _sha256_file(review_media_path) != output.get("sha256")
        or media_frame_count != frame_count
    ):
        raise ClinicalDraftPublishError("review media 파일/SHA/frame 불일치")
    video_end_sec = float(timeline["end_sec"])
    review_media: dict[str, Any] = {
        "file": str(review_media_path),
        "sha256": _sha256_file(review_media_path),
        "resolution": f"{width}x{height}",
        "flir_crop": f"crop={width // 2}:{height}:{width // 2}:0",
        "video_frame_count": frame_count,
        "video_end_sec": video_end_sec,
        "container_end_sec": container_end_sec,
    }
    if container_end_sec > video_end_sec + 1e-6:
        review_media["audio_only_tail_sec"] = [
            video_end_sec,
            container_end_sec,
        ]

    report_path = (
        repo_root
        / "output/clinical_annotation"
        / case_id
        / "multimodal_review.v1.md"
    )
    if report_path.is_file():
        descriptors.append(
            _context_descriptor(
                clinical_case_dir=clinical_case_dir,
                path=report_path,
                role="audit_report",
            )
        )

    return {
        "schema": "taskplanner.clinical_video_manifest.v2",
        "case_id": case_id,
        "authority": "ai_draft",
        "candidate_file": CLINICAL_CANDIDATE_NAME,
        "candidate_sha256": _sha256_file(candidate_path),
        "candidate_count": candidate_count,
        "content_fields": ["observation", "interpretation"],
        "review_actions_file": CLINICAL_ACTIONS_NAME,
        "review_actions_present": False,
        "final_reference_file": CLINICAL_REFERENCE_NAME,
        "final_reference_present": False,
        "review_status": "needs_surgeon_review",
        "source_timeline": {
            "file": os.path.relpath(
                timeline_path.resolve(),
                clinical_case_dir.resolve(),
            ),
            "sha256": _sha256_file(timeline_path),
            "frame_count": frame_count,
            "start_sec": float(timeline["start_sec"]),
            "end_sec": video_end_sec,
            "gaps": timeline.get("gaps", []),
        },
        "review_media": review_media,
        "context_sources": descriptors,
        "provenance": {
            "generator": "Codex",
            "model": model,
            "generated_at": generated_at,
            "authority": "ai_draft",
            "review_status": "needs_surgeon_review",
            "method": (
                "full-video multimodal review using FLIR/CAM views, causal "
                "voice context, reviewed interaction labels, provisional "
                "functional Phase, and RF-DETR as non-ground-truth reference"
            ),
            "observable_interpretation_boundary": (
                "observation contains visible facts and visibility limits; "
                "interpretation states the most likely clinically meaningful "
                "tissue, action, and tissue response while confidence and "
                "AI-draft provenance carry residual uncertainty"
            ),
            "rfdetr_boundary": (
                "AI detections are tool-name reference only and never "
                "automatic ground truth"
            ),
        },
    }


def publish_case(
    *,
    repo_root: Path,
    case_id: str,
    generated_at: str,
    model: str = "Codex 5.6 sol",
    review_media_root: Path = Path(
        "/home/arl/.cache/taskplanner_annotation"
    ),
) -> dict[str, Any]:
    """Create a manifest once, then load all candidates fail-closed."""

    repo_root = repo_root.resolve()
    clinical_case_dir = (
        repo_root / "annotations/clinical_video/cases" / case_id
    )
    clinical_case_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = clinical_case_dir / CLINICAL_MANIFEST_NAME
    requested_manifest = build_manifest(
        repo_root=repo_root,
        case_id=case_id,
        generated_at=generated_at,
        model=model,
        review_media_root=review_media_root,
    )
    already_published = manifest_path.is_file()
    if already_published:
        existing_manifest = _load_json(
            manifest_path,
            label="existing clinical manifest",
        )
        provenance = existing_manifest.get("provenance")
        if not isinstance(provenance, dict):
            raise ClinicalDraftPublishError(
                f"{case_id}: 기존 clinical manifest provenance가 없습니다."
            )
        manifest = build_manifest(
            repo_root=repo_root,
            case_id=case_id,
            generated_at=str(provenance.get("generated_at", "")),
            model=str(provenance.get("model", "")),
            review_media_root=review_media_root,
        )
        if manifest_path.read_bytes() != encode_json(manifest):
            raise ClinicalDraftPublishError(
                f"{case_id}: create-only clinical manifest가 이미 다릅니다."
            )
    else:
        manifest = requested_manifest
        manifest_data = encode_json(manifest)
        publish_create_only({manifest_path: manifest_data})

    timeline_path = (
        clinical_case_dir / str(manifest["source_timeline"]["file"])
    )
    timeline_path = timeline_path.resolve()
    timeline = _load_json(timeline_path, label="CAM4 timeline")
    try:
        store = ClinicalReviewStore(
            case_dir=clinical_case_dir,
            case_id=case_id,
            source_timeline=timeline,
            source_timeline_path=timeline_path,
            manifest_path=manifest_path,
        )
        candidates = store.candidates()
        state = store.state()
    except Exception:
        if not already_published:
            manifest_path.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "case_id": case_id,
        "already_published": already_published,
        "candidate_count": len(candidates),
        "candidate_sha256": manifest["candidate_sha256"],
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "clinical_state_schema": state["schema"],
        "candidate_source_status": state["candidate_source"]["status"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--model", default="Codex 5.6 sol")
    parser.add_argument(
        "--review-media-root",
        type=Path,
        default=Path("/home/arl/.cache/taskplanner_annotation"),
    )
    parser.add_argument(
        "--generated-at",
        default=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
    )
    parser.add_argument("case_ids", nargs="+")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        results = [
            publish_case(
                repo_root=args.repo_root,
                case_id=case_id,
                generated_at=args.generated_at,
                model=args.model,
                review_media_root=args.review_media_root,
            )
            for case_id in args.case_ids
        ]
    except (ClinicalDraftPublishError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
