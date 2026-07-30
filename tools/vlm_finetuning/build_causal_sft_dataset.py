#!/usr/bin/env python3
"""Build a causal, manifest-bound surgical VLM SFT dataset.

The builder intentionally separates annotation authority from task rendering:

* observable, DT, provisional Phase, voice, and exact frame timeline files are
  resolved only through ``annotation_manifest.json``;
* clinical candidates are resolved only through ``clinical_manifest.v2.json``;
* every referenced digest is checked before any example is emitted;
* only media and public transcript rows available at ``causal_cutoff_sec`` are
  placed in model input;
* prediction targets may be after the cutoff only for
  ``next_physical_tool``;
* canonical timestamp geometry, rather than ``frame / fps``, drives sampling;
* a sequence that crosses a declared visual gap is rejected.

The generated image files come from the overlay-free, one-frame-per-canonical-
frame ``review_cam4.mp4`` and ``review_flir.mp4`` proxies.  Their complete
decoded frame count and source hashes are validated before use.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


MASTER_SCHEMA = "taskplanner.causal_vlm_sft_example.v1"
UNSLOTH_SCHEMA = "taskplanner.unsloth_vision_messages.v1"
AUDIT_SCHEMA = "taskplanner.causal_vlm_sft_audit.v1"
FOLDS_SCHEMA = "taskplanner.case_group_cv_folds.v1"
DEFAULT_CASES = tuple(f"0704_{index}" for index in range(6, 18))
DEFAULT_SEED = 20260729
DEFAULT_FOLD_COUNT = 4
DEFAULT_HELD_OUT_FOLD = 0
NEXT_TOOL_HORIZON_SEC = 5.0
NEXT_TOOL_LOOKBACK_SEC = 6.0
EPSILON_SEC = 1e-6

TASK_ORDER = {
    "tool_presence_at_transfer": 0,
    "tool_presence_pseudo": 1,
    "request_intent": 2,
    "current_phase": 3,
    "next_physical_tool": 4,
    "clinical_observation_interpretation": 5,
}

PHASE_LABELS = {
    "P03": "고정 견인 전 중앙 수술야 박리",
    "P04": "고정 견인 배치 및 노출 확립",
    "P05": "견인 유지 하 표적 조직 조작",
    "P06": "국소 표적 제어 및 처치",
}

SYSTEM_PROMPT = (
    "당신은 갑상샘 수술 영상을 분석하는 VLM이다. 제공된 프레임과 질문에 "
    "명시된 시점까지 이용 가능한 ASR만 사용한다. 절대 시각이나 수술 진행률을 "
    "추측 단서로 사용하지 말고, 요구된 키를 가진 간결한 JSON만 출력한다."
)

TOOL_SPEECH_ALIASES = {
    "adson_forceps": ("adson", "애드슨", "아드손", "앳슨"),
    "bipolar_forceps": ("bipolar", "바이폴라"),
    "allis_forceps": ("allis", "알리스"),
    "kocher_retractor": (
        "kocher",
        "코처",
        "thyroid retractor",
        "갑상선 리트랙터",
    ),
    "bovie": ("bovie", "보비", "보위"),
    "army_navy_retractor": (
        "army navy",
        "army-navy",
        "아미 네이비",
        "아미네이비",
    ),
    "senn_miller_retractor": ("senn", "센 밀러", "센밀러"),
    "mosquito_forceps": ("mosquito", "모스키토", "모스키토우"),
    "harmonic_shears": ("harmonic", "하모닉"),
    "yankauer_suction": (
        "yankauer",
        "yankeur",
        "양카우어",
        "얀카우어",
        "석션",
    ),
    "scalpel": ("scalpel", "메스"),
}


class BuildError(RuntimeError):
    """A fail-closed dataset build or validation error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot load JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise BuildError(
                        f"expected JSON object: {path}:{line_number}"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot load JSONL {path}: {exc}") from exc
    return rows


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())


def _resolve_repo_file(
    *,
    repo_root: Path,
    base: Path,
    relative: Any,
    label: str,
) -> Path:
    if not isinstance(relative, str) or not relative:
        raise BuildError(f"{label}: missing relative file path")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise BuildError(f"{label}: source annotation path must be relative")
    resolved = (base / candidate).resolve()
    root = repo_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise BuildError(f"{label}: path escapes repository: {resolved}")
    if not resolved.is_file():
        raise BuildError(f"{label}: file does not exist: {resolved}")
    return resolved


def _resolve_bound_descriptor(
    *,
    repo_root: Path,
    base: Path,
    descriptor: Any,
    label: str,
) -> tuple[Path, str]:
    if not isinstance(descriptor, dict):
        raise BuildError(f"{label}: descriptor must be an object")
    path = _resolve_repo_file(
        repo_root=repo_root,
        base=base,
        relative=descriptor.get("file"),
        label=label,
    )
    expected = descriptor.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected
    ):
        raise BuildError(f"{label}: invalid or missing SHA-256")
    actual = sha256_file(path)
    if actual != expected:
        raise BuildError(
            f"{label}: SHA-256 mismatch: expected={expected}, actual={actual}"
        )
    return path, actual


def _validate_timeline(
    timeline: Mapping[str, Any],
    *,
    case_id: str,
    path: Path,
) -> tuple[tuple[float, ...], tuple[dict[str, Any], ...]]:
    if timeline.get("schema") != "taskplanner.video_frame_timeline.v1":
        raise BuildError(f"{case_id}: unexpected timeline schema: {path}")
    if timeline.get("case_id") != case_id:
        raise BuildError(f"{case_id}: timeline case_id mismatch")
    timestamps_raw = timeline.get("timestamps_sec")
    if not isinstance(timestamps_raw, list) or not timestamps_raw:
        raise BuildError(f"{case_id}: timeline timestamps are empty")
    timestamps = tuple(float(value) for value in timestamps_raw)
    if int(timeline.get("frame_count", -1)) != len(timestamps):
        raise BuildError(f"{case_id}: timeline frame_count mismatch")
    if any(
        current <= previous
        for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise BuildError(f"{case_id}: timeline timestamps are not increasing")
    if abs(float(timeline.get("start_sec", math.nan)) - timestamps[0]) > EPSILON_SEC:
        raise BuildError(f"{case_id}: timeline start_sec mismatch")
    if abs(float(timeline.get("end_sec", math.nan)) - timestamps[-1]) > EPSILON_SEC:
        raise BuildError(f"{case_id}: timeline end_sec mismatch")

    gaps_raw = timeline.get("gaps", [])
    if not isinstance(gaps_raw, list):
        raise BuildError(f"{case_id}: timeline gaps must be a list")
    gaps: list[dict[str, Any]] = []
    previous_after = -1
    for raw in gaps_raw:
        if not isinstance(raw, dict):
            raise BuildError(f"{case_id}: malformed timeline gap")
        before = int(raw.get("before_frame_idx", -1))
        after = int(raw.get("after_frame_idx", -1))
        if after != before + 1 or before <= previous_after:
            raise BuildError(f"{case_id}: invalid timeline gap frame geometry")
        if not 0 <= before < after < len(timestamps):
            raise BuildError(f"{case_id}: timeline gap outside frame range")
        before_time = float(raw.get("before_time_sec", math.nan))
        after_time = float(raw.get("after_time_sec", math.nan))
        if (
            abs(before_time - timestamps[before]) > EPSILON_SEC
            or abs(after_time - timestamps[after]) > EPSILON_SEC
        ):
            raise BuildError(f"{case_id}: timeline gap timestamp mismatch")
        gaps.append(dict(raw))
        previous_after = after
    return timestamps, tuple(gaps)


def _event_frame_time(
    event: Mapping[str, Any],
    timestamps: Sequence[float],
    *,
    label: str,
    frame_field: str = "source_frame_idx",
    time_field: str = "time_sec",
) -> tuple[int, float]:
    frame = int(event.get(frame_field, -1))
    if not 0 <= frame < len(timestamps):
        raise BuildError(f"{label}: frame outside canonical timeline")
    value = float(event.get(time_field, math.nan))
    if abs(value - timestamps[frame]) > EPSILON_SEC:
        raise BuildError(
            f"{label}: time/frame mismatch: {value} != {timestamps[frame]}"
        )
    return frame, value


@dataclass(frozen=True)
class CaseSources:
    repo_root: Path
    case_id: str
    observable_case_dir: Path
    clinical_case_dir: Path
    annotation_manifest_path: Path
    annotation_manifest: dict[str, Any]
    clinical_manifest_path: Path
    clinical_manifest: dict[str, Any]
    timeline_path: Path
    timeline: dict[str, Any]
    timestamps: tuple[float, ...]
    gaps: tuple[dict[str, Any], ...]
    observed_path: Path
    observed: tuple[dict[str, Any], ...]
    dt_path: Path
    dt: tuple[dict[str, Any], ...]
    phase_path: Path
    phases: tuple[dict[str, Any], ...]
    voice_path: Path
    voices: tuple[dict[str, Any], ...]
    clinical_path: Path
    clinical: tuple[dict[str, Any], ...]
    bindings: dict[str, dict[str, str]]
    snapshot_id: str


def load_case_sources(repo_root: Path, case_id: str) -> CaseSources:
    """Resolve and validate all current case sources through their manifests."""

    repo_root = repo_root.resolve()
    observable_case_dir = (
        repo_root / "annotations/observable_tool_events/cases" / case_id
    )
    clinical_case_dir = (
        repo_root / "annotations/clinical_video/cases" / case_id
    )
    annotation_manifest_path = (
        observable_case_dir / "annotation_manifest.json"
    )
    clinical_manifest_path = clinical_case_dir / "clinical_manifest.v2.json"
    if not annotation_manifest_path.is_file():
        raise BuildError(f"{case_id}: annotation_manifest.json is missing")
    if not clinical_manifest_path.is_file():
        raise BuildError(f"{case_id}: clinical_manifest.v2.json is missing")

    annotation_manifest = load_json(annotation_manifest_path)
    clinical_manifest = load_json(clinical_manifest_path)
    if annotation_manifest.get("case_id") != case_id:
        raise BuildError(f"{case_id}: annotation manifest case_id mismatch")
    if clinical_manifest.get("case_id") != case_id:
        raise BuildError(f"{case_id}: clinical manifest case_id mismatch")

    evaluation = annotation_manifest.get("evaluation_reference")
    if not isinstance(evaluation, dict) or evaluation.get("complete") is not True:
        raise BuildError(f"{case_id}: evaluation_reference is not complete")
    observed_path, observed_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=observable_case_dir,
        descriptor=evaluation.get("observed_reference"),
        label=f"{case_id} observed reference",
    )
    dt_path, dt_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=observable_case_dir,
        descriptor=evaluation.get("dt_reference"),
        label=f"{case_id} DT reference",
    )
    phase_path, phase_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=observable_case_dir,
        descriptor=evaluation.get("phase_reference"),
        label=f"{case_id} Phase reference",
    )
    voice_path, voice_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=observable_case_dir,
        descriptor=annotation_manifest.get("speech_timeline"),
        label=f"{case_id} voice timeline",
    )

    minimal = annotation_manifest.get("minimal_interaction_annotation")
    if not isinstance(minimal, dict):
        raise BuildError(f"{case_id}: minimal_interaction_annotation is missing")
    timeline_descriptor = {
        "file": minimal.get("timeline_file"),
        "sha256": minimal.get("timeline_sha256"),
    }
    timeline_path, timeline_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=observable_case_dir,
        descriptor=timeline_descriptor,
        label=f"{case_id} canonical frame timeline",
    )
    timeline = load_json(timeline_path)
    timestamps, gaps = _validate_timeline(
        timeline,
        case_id=case_id,
        path=timeline_path,
    )

    source_timeline = clinical_manifest.get("source_timeline")
    clinical_timeline_path, clinical_timeline_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=clinical_case_dir,
        descriptor=source_timeline,
        label=f"{case_id} clinical source timeline",
    )
    if (
        clinical_timeline_path != timeline_path
        or clinical_timeline_sha != timeline_sha
    ):
        raise BuildError(
            f"{case_id}: clinical and observable manifests bind different timelines"
        )

    candidate_descriptor = {
        "file": clinical_manifest.get("candidate_file"),
        "sha256": clinical_manifest.get("candidate_sha256"),
    }
    clinical_path, clinical_sha = _resolve_bound_descriptor(
        repo_root=repo_root,
        base=clinical_case_dir,
        descriptor=candidate_descriptor,
        label=f"{case_id} clinical candidates",
    )

    observed = tuple(load_jsonl(observed_path))
    dt = tuple(load_jsonl(dt_path))
    phases = tuple(load_jsonl(phase_path))
    voices = tuple(load_jsonl(voice_path))
    clinical = tuple(load_jsonl(clinical_path))

    expected_counts = (
        (
            "observed",
            len(observed),
            evaluation["observed_reference"].get("confirmed_event_count"),
        ),
        (
            "DT",
            len(dt),
            evaluation["dt_reference"].get("confirmed_event_count"),
        ),
        (
            "Phase",
            len(phases),
            evaluation["phase_reference"].get("event_count"),
        ),
        (
            "voice",
            len(voices),
            annotation_manifest["speech_timeline"].get("event_count"),
        ),
        (
            "clinical",
            len(clinical),
            clinical_manifest.get("candidate_count"),
        ),
    )
    for label, actual, expected in expected_counts:
        if actual != int(expected):
            raise BuildError(
                f"{case_id}: {label} count mismatch: {actual} != {expected}"
            )

    for collection_name, collection in (
        ("observed", observed),
        ("DT", dt),
        ("Phase", phases),
    ):
        ids: set[str] = set()
        for event in collection:
            event_id = str(event.get("event_id", ""))
            if not event_id or event_id in ids:
                raise BuildError(
                    f"{case_id}: duplicate/missing {collection_name} event_id"
                )
            ids.add(event_id)
            if event.get("case_id") != case_id:
                raise BuildError(
                    f"{case_id}: {collection_name} event case mismatch"
                )
            _event_frame_time(
                event,
                timestamps,
                label=f"{case_id} {collection_name} {event_id}",
            )
            if "end_source_frame_idx" in event:
                _event_frame_time(
                    event,
                    timestamps,
                    label=f"{case_id} {collection_name} {event_id} end",
                    frame_field="end_source_frame_idx",
                    time_field="end_sec",
                )

    previous_availability = -math.inf
    for voice in voices:
        if voice.get("case_id") != case_id:
            raise BuildError(f"{case_id}: voice case mismatch")
        available = float(voice.get("available_sec", math.nan))
        end_sec = float(voice.get("end_sec", math.nan))
        time_sec = float(voice.get("time_sec", math.nan))
        if (
            not math.isfinite(available)
            or available + EPSILON_SEC < end_sec
            or available + EPSILON_SEC < time_sec
            or available + EPSILON_SEC < previous_availability
        ):
            raise BuildError(f"{case_id}: invalid voice availability geometry")
        previous_availability = available

    previous_anchor = -1
    for candidate in clinical:
        annotation_id = str(candidate.get("annotation_id", ""))
        if candidate.get("case_id") != case_id or not annotation_id:
            raise BuildError(f"{case_id}: invalid clinical candidate identity")
        anchor, anchor_time = _event_frame_time(
            candidate,
            timestamps,
            label=f"{case_id} clinical {annotation_id} anchor",
            frame_field="anchor_source_frame_idx",
            time_field="anchor_sec",
        )
        start, start_time = _event_frame_time(
            candidate,
            timestamps,
            label=f"{case_id} clinical {annotation_id} evidence start",
            frame_field="evidence_start_source_frame_idx",
            time_field="evidence_start_sec",
        )
        end, end_time = _event_frame_time(
            candidate,
            timestamps,
            label=f"{case_id} clinical {annotation_id} evidence end",
            frame_field="evidence_end_source_frame_idx",
            time_field="evidence_end_sec",
        )
        if not start <= anchor <= end or not start_time <= anchor_time <= end_time:
            raise BuildError(f"{case_id}: clinical evidence does not contain anchor")
        if anchor <= previous_anchor:
            raise BuildError(f"{case_id}: clinical anchors are not strictly ordered")
        previous_anchor = anchor

    bindings = {
        "annotation_manifest": {
            "path": _repo_relative(repo_root, annotation_manifest_path),
            "sha256": sha256_file(annotation_manifest_path),
        },
        "clinical_manifest": {
            "path": _repo_relative(repo_root, clinical_manifest_path),
            "sha256": sha256_file(clinical_manifest_path),
        },
        "timeline": {
            "path": _repo_relative(repo_root, timeline_path),
            "sha256": timeline_sha,
        },
        "observed": {
            "path": _repo_relative(repo_root, observed_path),
            "sha256": observed_sha,
        },
        "dt": {
            "path": _repo_relative(repo_root, dt_path),
            "sha256": dt_sha,
        },
        "phase": {
            "path": _repo_relative(repo_root, phase_path),
            "sha256": phase_sha,
        },
        "voice": {
            "path": _repo_relative(repo_root, voice_path),
            "sha256": voice_sha,
        },
        "clinical": {
            "path": _repo_relative(repo_root, clinical_path),
            "sha256": clinical_sha,
        },
    }
    snapshot_id = sha256_bytes(canonical_json(bindings).encode("utf-8"))

    return CaseSources(
        repo_root=repo_root,
        case_id=case_id,
        observable_case_dir=observable_case_dir,
        clinical_case_dir=clinical_case_dir,
        annotation_manifest_path=annotation_manifest_path,
        annotation_manifest=annotation_manifest,
        clinical_manifest_path=clinical_manifest_path,
        clinical_manifest=clinical_manifest,
        timeline_path=timeline_path,
        timeline=timeline,
        timestamps=timestamps,
        gaps=gaps,
        observed_path=observed_path,
        observed=observed,
        dt_path=dt_path,
        dt=dt,
        phase_path=phase_path,
        phases=phases,
        voice_path=voice_path,
        voices=voices,
        clinical_path=clinical_path,
        clinical=clinical,
        bindings=bindings,
        snapshot_id=snapshot_id,
    )


def frame_segment_bounds(
    frame_index: int,
    *,
    frame_count: int,
    gaps: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    if not 0 <= frame_index < frame_count:
        raise BuildError(f"frame outside timeline: {frame_index}")
    start = 0
    end = frame_count - 1
    for gap in gaps:
        before = int(gap["before_frame_idx"])
        after = int(gap["after_frame_idx"])
        if frame_index <= before:
            end = min(end, before)
            break
        start = max(start, after)
    return start, end


def crosses_gap(
    start_frame: int,
    end_frame: int,
    gaps: Sequence[Mapping[str, Any]],
) -> bool:
    if start_frame > end_frame:
        start_frame, end_frame = end_frame, start_frame
    return any(
        start_frame <= int(gap["before_frame_idx"])
        and end_frame >= int(gap["after_frame_idx"])
        for gap in gaps
    )


def _nearest_frame_in_bounds(
    timestamps: Sequence[float],
    target_sec: float,
    *,
    lower: int,
    upper: int,
) -> int:
    position = bisect.bisect_left(timestamps, target_sec, lower, upper + 1)
    candidates = []
    if lower <= position <= upper:
        candidates.append(position)
    if lower <= position - 1 <= upper:
        candidates.append(position - 1)
    if not candidates:
        return lower if position <= lower else upper
    return min(candidates, key=lambda index: (abs(timestamps[index] - target_sec), index))


def sample_causal_frames(
    sources: CaseSources,
    *,
    cutoff_frame: int,
    start_frame: int,
    count: int,
    view: str,
) -> list[dict[str, Any]]:
    """Sample 2-4 chronological frames without crossing a visual gap."""

    if not 2 <= count <= 4:
        raise BuildError("media frame count must be in 2..4")
    segment_start, segment_end = frame_segment_bounds(
        cutoff_frame,
        frame_count=len(sources.timestamps),
        gaps=sources.gaps,
    )
    if cutoff_frame > segment_end:
        raise BuildError("cutoff frame outside its visual segment")
    start_frame = max(start_frame, segment_start)
    if start_frame > cutoff_frame:
        start_frame = cutoff_frame
    if crosses_gap(start_frame, cutoff_frame, sources.gaps):
        raise BuildError(
            f"{sources.case_id}: requested media window crosses a gap"
        )

    start_time = sources.timestamps[start_frame]
    cutoff_time = sources.timestamps[cutoff_frame]
    if count == 1:
        targets = [cutoff_time]
    else:
        targets = [
            start_time + (cutoff_time - start_time) * index / (count - 1)
            for index in range(count)
        ]
    indices = {
        _nearest_frame_in_bounds(
            sources.timestamps,
            target,
            lower=start_frame,
            upper=cutoff_frame,
        )
        for target in targets
    }
    indices.add(cutoff_frame)
    cursor = cutoff_frame - 1
    while len(indices) < min(count, cutoff_frame - start_frame + 1):
        if cursor < start_frame:
            break
        indices.add(cursor)
        cursor -= 1
    ordered = sorted(indices)
    if len(ordered) > count:
        ordered = ordered[-count:]
    if len(ordered) < 2:
        if cutoff_frame > segment_start:
            ordered.insert(0, cutoff_frame - 1)
        elif cutoff_frame < segment_end:
            # This frame remains before the declared cutoff only when the
            # caller chose a cutoff frame earlier than cutoff time.  Callers
            # always bind cutoff time to this exact frame, so do not use it.
            raise BuildError(
                f"{sources.case_id}: cannot form a two-frame causal sequence"
            )
        else:
            raise BuildError(
                f"{sources.case_id}: visual segment contains only one frame"
            )

    return [
        {
            "view": view,
            "source_frame_idx": index,
            "time_sec": sources.timestamps[index],
            "relative_sec": sources.timestamps[index] - cutoff_time,
            "path": None,
        }
        for index in ordered
    ]


def _voice_context(
    sources: CaseSources,
    *,
    cutoff_sec: float,
    window_start_sec: float,
    maximum: int = 6,
) -> list[dict[str, Any]]:
    eligible = [
        {
            "event_id": str(voice["event_id"]),
            "text": str(voice.get("text", "")).strip(),
            "available_sec": float(voice["available_sec"]),
        }
        for voice in sources.voices
        if float(voice["available_sec"]) <= cutoff_sec + EPSILON_SEC
        and float(voice["available_sec"]) >= window_start_sec - 3.0
    ]
    return eligible[-maximum:]


def _authority_for_event(
    event: Mapping[str, Any],
    *,
    derived: bool = False,
) -> dict[str, Any]:
    review = event.get("review")
    review = review if isinstance(review, dict) else {}
    origin = str(event.get("label_origin", ""))
    reviewer_kind = str(review.get("reviewer_kind", ""))
    if origin == "human_video_review" or reviewer_kind == "human":
        tier = "reviewed_human"
        label = "human exact-frame review"
    elif review.get("authorized_by") and reviewer_kind == "ai_assistant":
        tier = "silver_user_authorized_ai_assistant"
        label = "user-authorized AI exact-frame adjudication"
    else:
        tier = "silver_unreviewed_or_other"
        label = "non-human sparse event annotation"
    if derived:
        tier = f"derived_from_{tier}"
        label = f"deterministically derived from {label}"
    return {
        "tier": tier,
        "label": label,
        "review_status": event.get("review_status"),
        "label_origin": event.get("label_origin"),
    }


def _base_row(
    sources: CaseSources,
    *,
    example_id: str,
    task_type: str,
    cutoff_sec: float,
    window_start_sec: float,
    media: list[dict[str, Any]],
    target: dict[str, Any],
    authority: dict[str, Any],
    source_ids: Sequence[str],
    prediction_horizon_sec: float | None = None,
    quality_extra: Mapping[str, Any] | None = None,
    include_voice: bool = True,
) -> dict[str, Any]:
    if task_type not in TASK_ORDER:
        raise BuildError(f"unknown task_type: {task_type}")
    if not media:
        raise BuildError(f"{example_id}: media is empty")
    max_media_time = max(float(item["time_sec"]) for item in media)
    if max_media_time > cutoff_sec + EPSILON_SEC:
        raise BuildError(f"{example_id}: media leaks past causal cutoff")
    voices = (
        _voice_context(
            sources,
            cutoff_sec=cutoff_sec,
            window_start_sec=window_start_sec,
        )
        if include_voice
        else []
    )
    if any(
        float(voice["available_sec"]) > cutoff_sec + EPSILON_SEC
        for voice in voices
    ):
        raise BuildError(f"{example_id}: voice leaks past causal cutoff")
    start_frame = min(int(item["source_frame_idx"]) for item in media)
    end_frame = max(int(item["source_frame_idx"]) for item in media)
    gap_safe = not crosses_gap(start_frame, end_frame, sources.gaps)
    if not gap_safe:
        raise BuildError(f"{example_id}: media sequence crosses a declared gap")
    quality = {
        "gap_safe": True,
        "no_future_input": True,
        "absolute_time_hidden_from_prompt": True,
    }
    if quality_extra:
        quality.update(quality_extra)
    time: dict[str, Any] = {
        "causal_cutoff_sec": cutoff_sec,
        "window_start_sec": window_start_sec,
        "window_end_sec": cutoff_sec,
    }
    if prediction_horizon_sec is not None:
        time["prediction_horizon_sec"] = prediction_horizon_sec
    return {
        "schema": MASTER_SCHEMA,
        "example_id": example_id,
        "case_id": sources.case_id,
        "split_group_id": f"case:{sources.case_id}",
        "task_type": task_type,
        "time": time,
        "media": media,
        "causal_context": {
            "voice": voices,
            "prior_events": [],
        },
        "target": target,
        "authority": {
            **authority,
            "source_ids": list(source_ids),
        },
        "source_bindings": sources.bindings,
        "source_snapshot_id": sources.snapshot_id,
        "quality": quality,
    }


def _event_start_end_frames(
    event: Mapping[str, Any],
) -> tuple[int, int]:
    start = int(event["source_frame_idx"])
    end = int(event.get("end_source_frame_idx", start))
    return start, end


def _build_tool_presence_rows(sources: CaseSources) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in sources.observed:
        if event.get("event_type") != "tool_transfer":
            continue
        tool = event.get("tool")
        if not isinstance(tool, str) or not tool:
            raise BuildError(f"{event.get('event_id')}: transfer tool is missing")
        frame, cutoff = _event_frame_time(
            event,
            sources.timestamps,
            label=str(event.get("event_id")),
        )
        segment_start, _ = frame_segment_bounds(
            frame,
            frame_count=len(sources.timestamps),
            gaps=sources.gaps,
        )
        start = _nearest_frame_in_bounds(
            sources.timestamps,
            cutoff - 0.8,
            lower=segment_start,
            upper=frame,
        )
        media = sample_causal_frames(
            sources,
            cutoff_frame=frame,
            start_frame=start,
            count=3,
            view="cam4",
        )
        event_id = str(event["event_id"])
        rows.append(
            _base_row(
                sources,
                example_id=(
                    f"{sources.case_id}:tool_presence_at_transfer:{event_id}"
                ),
                task_type="tool_presence_at_transfer",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[start],
                media=media,
                target={
                    "event": "physical_tool_transfer",
                    "tool": tool,
                    "from": event.get("from"),
                    "to": event.get("to"),
                    "exhaustive_visible_tool_inventory": False,
                },
                authority=_authority_for_event(event),
                source_ids=[event_id],
                quality_extra={
                    "supervision_scope": "sparse_positive_transfer_event_only",
                    "absence_labels_available": False,
                    "tool_name_voice_leakage_blocked": True,
                },
                include_voice=False,
            )
        )
    return rows


def _build_request_rows(sources: CaseSources) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in sources.observed:
        if event.get("event_type") != "implicit_tool_request":
            continue
        if event.get("requested_tool") not in (None, ""):
            raise BuildError(
                f"{event.get('event_id')}: implicit request must not backfill tool"
            )
        start_frame, end_frame = _event_start_end_frames(event)
        _, cutoff = _event_frame_time(
            event,
            sources.timestamps,
            label=str(event.get("event_id")),
            frame_field="end_source_frame_idx",
            time_field="end_sec",
        )
        segment_start, _ = frame_segment_bounds(
            end_frame,
            frame_count=len(sources.timestamps),
            gaps=sources.gaps,
        )
        lookback_start = _nearest_frame_in_bounds(
            sources.timestamps,
            cutoff - max(1.0, cutoff - sources.timestamps[start_frame]),
            lower=segment_start,
            upper=end_frame,
        )
        media = sample_causal_frames(
            sources,
            cutoff_frame=end_frame,
            start_frame=lookback_start,
            count=3,
            view="cam4",
        )
        event_id = str(event["event_id"])
        rows.append(
            _base_row(
                sources,
                example_id=f"{sources.case_id}:request_intent:{event_id}",
                task_type="request_intent",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[lookback_start],
                media=media,
                target={
                    "event": "implicit_tool_request",
                    "intent": "receive_unspecified_tool",
                    "requested_tool": None,
                    "tool_identity_inferred_from_later_transfer": False,
                },
                authority=_authority_for_event(event),
                source_ids=[event_id],
                quality_extra={
                    "supervision_scope": "strict_empty_open_palm_interval",
                    "future_tool_backfill_forbidden": True,
                },
            )
        )
    return rows


def _phase_interval_frames(
    phases: Sequence[Mapping[str, Any]],
    frame_count: int,
) -> list[tuple[Mapping[str, Any], int, int]]:
    ordered = sorted(phases, key=lambda event: int(event["source_frame_idx"]))
    intervals = []
    previous_start = -1
    for index, phase in enumerate(ordered):
        start = int(phase["source_frame_idx"])
        if start <= previous_start:
            raise BuildError("Phase starts are not strictly increasing")
        end = (
            int(ordered[index + 1]["source_frame_idx"]) - 1
            if index + 1 < len(ordered)
            else frame_count - 1
        )
        if end < start:
            raise BuildError("empty Phase interval")
        intervals.append((phase, start, end))
        previous_start = start
    return intervals


def _build_phase_rows(sources: CaseSources) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    intervals = _phase_interval_frames(
        sources.phases, len(sources.timestamps)
    )
    for phase_index, (phase, start, end) in enumerate(intervals):
        phase_id = str(phase.get("phase_id", ""))
        if phase_id not in PHASE_LABELS:
            raise BuildError(f"{sources.case_id}: unknown Phase {phase_id}")
        phase_event_id = str(phase["event_id"])
        phase_authority = {
            "tier": "provisional_ai_phase_not_scoring_ground_truth",
            "label": "provisional ambiguous functional-state context",
            "review_status": phase.get("review_status"),
            "scoring_role": "context_only_not_ground_truth",
        }

        # Two stable interior anchors reduce temporal-prior learning while
        # keeping every case/Phase group represented.
        for interior_number, fraction in enumerate((1 / 3, 2 / 3), 1):
            cutoff_frame = round(start + (end - start) * fraction)
            cutoff_frame = min(max(cutoff_frame, start), end)
            segment_start, _ = frame_segment_bounds(
                cutoff_frame,
                frame_count=len(sources.timestamps),
                gaps=sources.gaps,
            )
            desired_start = _nearest_frame_in_bounds(
                sources.timestamps,
                sources.timestamps[cutoff_frame] - 6.0,
                lower=segment_start,
                upper=cutoff_frame,
            )
            media = sample_causal_frames(
                sources,
                cutoff_frame=cutoff_frame,
                start_frame=desired_start,
                count=4,
                view="flir",
            )
            cutoff = sources.timestamps[cutoff_frame]
            rows.append(
                _base_row(
                    sources,
                    example_id=(
                        f"{sources.case_id}:current_phase:{phase_event_id}:"
                        f"interior-{interior_number}"
                    ),
                    task_type="current_phase",
                    cutoff_sec=cutoff,
                    window_start_sec=sources.timestamps[desired_start],
                    media=media,
                    target={
                        "phase_id": phase_id,
                        "phase_name_ko": PHASE_LABELS[phase_id],
                        "state": "interior",
                        "transition_from": None,
                        "transition_to": None,
                    },
                    authority=phase_authority,
                    source_ids=[phase_event_id],
                    quality_extra={
                        "phase_scoring_eligible": False,
                        "phase_status": "provisional_ambiguous",
                    },
                )
            )

        if phase_index == 0:
            continue
        cutoff_frame = start
        cutoff = sources.timestamps[cutoff_frame]
        segment_start, _ = frame_segment_bounds(
            cutoff_frame,
            frame_count=len(sources.timestamps),
            gaps=sources.gaps,
        )
        desired_start = _nearest_frame_in_bounds(
            sources.timestamps,
            cutoff - 3.0,
            lower=segment_start,
            upper=cutoff_frame,
        )
        media = sample_causal_frames(
            sources,
            cutoff_frame=cutoff_frame,
            start_frame=desired_start,
            count=4,
            view="flir",
        )
        previous_phase = str(intervals[phase_index - 1][0]["phase_id"])
        rows.append(
            _base_row(
                sources,
                example_id=(
                    f"{sources.case_id}:current_phase:{phase_event_id}:transition"
                ),
                task_type="current_phase",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[desired_start],
                media=media,
                target={
                    "phase_id": phase_id,
                    "phase_name_ko": PHASE_LABELS[phase_id],
                    "state": "transition",
                    "transition_from": previous_phase,
                    "transition_to": phase_id,
                },
                authority=phase_authority,
                source_ids=[phase_event_id],
                quality_extra={
                    "phase_scoring_eligible": False,
                    "phase_status": "provisional_ambiguous",
                },
            )
        )
    return rows


def _surgeon_direction_transfers(
    sources: CaseSources,
) -> list[dict[str, Any]]:
    return sorted(
        [
            dict(event)
            for event in sources.dt
            if event.get("event_type") == "tool_transfer"
            and event.get("from") == "scrub_nurse"
            and event.get("to") == "surgeon"
            and isinstance(event.get("tool"), str)
            and bool(event.get("tool"))
        ],
        key=lambda event: (float(event["time_sec"]), str(event["event_id"])),
    )


def _requests_before(
    sources: CaseSources,
    cutoff_sec: float,
) -> list[dict[str, Any]]:
    return sorted(
        [
            dict(event)
            for event in sources.dt
            if event.get("event_type") == "implicit_tool_request"
            and float(event.get("end_sec", event["time_sec"]))
            <= cutoff_sec + EPSILON_SEC
        ],
        key=lambda event: (
            float(event.get("end_sec", event["time_sec"])),
            str(event["event_id"]),
        ),
    )


def _first_future_transfer(
    transfers: Sequence[Mapping[str, Any]],
    *,
    cutoff_sec: float,
    horizon_sec: float,
) -> Mapping[str, Any] | None:
    eligible = [
        event
        for event in transfers
        if float(event["time_sec"]) > cutoff_sec + EPSILON_SEC
        and float(event["time_sec"]) <= cutoff_sec + horizon_sec + EPSILON_SEC
    ]
    return eligible[0] if eligible else None


def _window_crosses_gap_time(
    sources: CaseSources,
    start_sec: float,
    end_sec: float,
) -> bool:
    return any(
        start_sec <= float(gap["before_time_sec"]) + EPSILON_SEC
        and end_sec >= float(gap["after_time_sec"]) - EPSILON_SEC
        for gap in sources.gaps
    )


def _prediction_regime(
    *,
    tool: str,
    voices: Sequence[Mapping[str, Any]],
    causal_request_id: str | None,
) -> str:
    aliases = TOOL_SPEECH_ALIASES.get(tool, ())
    combined = " ".join(str(voice.get("text", "")).casefold() for voice in voices)
    if any(alias.casefold() in combined for alias in aliases):
        return "explicit_voice"
    if causal_request_id:
        return "implicit_request"
    return "anticipatory_context"


def _build_next_tool_rows(
    sources: CaseSources,
    *,
    rng: random.Random,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    transfers = _surgeon_direction_transfers(sources)
    positive_cutoff_frames: set[int] = set()
    for transfer_index, transfer in enumerate(transfers):
        transfer_frame, transfer_time = _event_frame_time(
            transfer,
            sources.timestamps,
            label=str(transfer["event_id"]),
        )
        segment_start, _ = frame_segment_bounds(
            transfer_frame,
            frame_count=len(sources.timestamps),
            gaps=sources.gaps,
        )
        segment_start_sec = sources.timestamps[segment_start]
        requests = [
            event
            for event in _requests_before(sources, transfer_time)
            if transfer_time
            - float(event.get("end_sec", event["time_sec"]))
            <= NEXT_TOOL_HORIZON_SEC + EPSILON_SEC
            and float(event.get("end_sec", event["time_sec"]))
            >= segment_start_sec
        ]
        causal_request = requests[-1] if requests else None
        proposed_cutoff = (
            float(
                causal_request.get("end_sec", causal_request["time_sec"])
            )
            if causal_request
            else transfer_time - 2.0
        )
        if transfer_index:
            previous_transfer = transfers[transfer_index - 1]
            previous_time = float(previous_transfer["time_sec"])
            if previous_time >= segment_start_sec:
                # A later transfer needs a cutoff at or after the previous
                # transfer, otherwise the earlier transfer is necessarily the
                # first future target.
                proposed_cutoff = max(proposed_cutoff, previous_time)
        proposed_cutoff = max(proposed_cutoff, segment_start_sec)
        cutoff_frame = bisect.bisect_right(
            sources.timestamps,
            proposed_cutoff + EPSILON_SEC,
            segment_start,
            transfer_frame,
        ) - 1
        cutoff_frame = max(cutoff_frame, segment_start)
        cutoff = sources.timestamps[cutoff_frame]
        if cutoff_frame in positive_cutoff_frames:
            continue
        if _window_crosses_gap_time(
            sources,
            cutoff,
            transfer_time,
        ):
            continue
        first = _first_future_transfer(
            transfers,
            cutoff_sec=cutoff,
            horizon_sec=NEXT_TOOL_HORIZON_SEC,
        )
        if first is None or first["event_id"] != transfer["event_id"]:
            continue
        positive_cutoff_frames.add(cutoff_frame)
        lookback_frame = _nearest_frame_in_bounds(
            sources.timestamps,
            cutoff - NEXT_TOOL_LOOKBACK_SEC,
            lower=segment_start,
            upper=cutoff_frame,
        )
        media = sample_causal_frames(
            sources,
            cutoff_frame=cutoff_frame,
            start_frame=lookback_frame,
            count=4,
            view="cam4",
        )
        causal_request_id = (
            str(causal_request["event_id"]) if causal_request else None
        )
        preliminary_voices = _voice_context(
            sources,
            cutoff_sec=cutoff,
            window_start_sec=sources.timestamps[lookback_frame],
        )
        target_tool = str(transfer["tool"])
        source_ids = [str(transfer["event_id"])]
        if causal_request_id:
            source_ids.append(causal_request_id)
        rows.append(
            _base_row(
                sources,
                example_id=(
                    f"{sources.case_id}:next_physical_tool:"
                    f"{transfer['event_id']}"
                ),
                task_type="next_physical_tool",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[lookback_frame],
                media=media,
                target={
                    "next_transfer_tool": target_tool,
                    "event": "scrub_nurse_to_surgeon",
                    "target_event_id": transfer["event_id"],
                    "target_time_sec": transfer_time,
                    "basis": "first_physical_transfer_within_horizon",
                    "prediction_regime": _prediction_regime(
                        tool=target_tool,
                        voices=preliminary_voices,
                        causal_request_id=causal_request_id,
                    ),
                    "causal_request_event_id": causal_request_id,
                    "request_tool_backfilled": False,
                },
                authority=_authority_for_event(transfer, derived=True),
                source_ids=source_ids,
                prediction_horizon_sec=NEXT_TOOL_HORIZON_SEC,
                quality_extra={
                    "future_target_allowed": True,
                    "target_is_first_future_transfer": True,
                    "direction": "scrub_nurse_to_surgeon",
                },
            )
        )

    # Add deterministic hard negatives.  The ratio yields roughly 29% `none`
    # examples without inventing absence labels at arbitrary dense frames.
    desired_none = round(len(rows) * 0.4)
    candidate_frames: list[int] = []
    step_sec = 3.0
    cursor = sources.timestamps[0] + NEXT_TOOL_LOOKBACK_SEC
    visual_end = sources.timestamps[-1]
    while cursor + NEXT_TOOL_HORIZON_SEC <= visual_end + EPSILON_SEC:
        frame = bisect.bisect_right(sources.timestamps, cursor + EPSILON_SEC) - 1
        if frame >= 0:
            candidate_frames.append(frame)
        cursor += step_sec
    candidate_frames = sorted(set(candidate_frames))
    rng.shuffle(candidate_frames)
    accepted_none: list[int] = []
    for cutoff_frame in candidate_frames:
        if len(accepted_none) >= desired_none:
            break
        cutoff = sources.timestamps[cutoff_frame]
        if any(
            abs(cutoff - sources.timestamps[other]) < NEXT_TOOL_HORIZON_SEC
            for other in accepted_none
        ):
            continue
        first = _first_future_transfer(
            transfers,
            cutoff_sec=cutoff,
            horizon_sec=NEXT_TOOL_HORIZON_SEC,
        )
        if first is not None:
            continue
        segment_start, segment_end = frame_segment_bounds(
            cutoff_frame,
            frame_count=len(sources.timestamps),
            gaps=sources.gaps,
        )
        horizon_end = min(cutoff + NEXT_TOOL_HORIZON_SEC, visual_end)
        horizon_frame = bisect.bisect_right(
            sources.timestamps,
            horizon_end + EPSILON_SEC,
            cutoff_frame,
            segment_end + 1,
        ) - 1
        if horizon_frame < cutoff_frame:
            continue
        if _window_crosses_gap_time(sources, cutoff, horizon_end):
            continue
        lookback_frame = _nearest_frame_in_bounds(
            sources.timestamps,
            cutoff - NEXT_TOOL_LOOKBACK_SEC,
            lower=segment_start,
            upper=cutoff_frame,
        )
        if crosses_gap(lookback_frame, horizon_frame, sources.gaps):
            continue
        media = sample_causal_frames(
            sources,
            cutoff_frame=cutoff_frame,
            start_frame=lookback_frame,
            count=4,
            view="cam4",
        )
        accepted_none.append(cutoff_frame)
        rows.append(
            _base_row(
                sources,
                example_id=(
                    f"{sources.case_id}:next_physical_tool:none:"
                    f"f{cutoff_frame:06d}"
                ),
                task_type="next_physical_tool",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[lookback_frame],
                media=media,
                target={
                    "next_transfer_tool": "none",
                    "event": "none",
                    "target_event_id": None,
                    "target_time_sec": None,
                    "basis": "no_physical_transfer_within_horizon",
                    "prediction_regime": "negative_horizon",
                    "causal_request_event_id": None,
                    "request_tool_backfilled": False,
                },
                authority={
                    "tier": "derived_from_complete_dt_reference",
                    "label": "deterministic horizon-negative from complete DT reference",
                    "review_status": "derived",
                    "scoring_role": "training_silver",
                },
                source_ids=[],
                prediction_horizon_sec=NEXT_TOOL_HORIZON_SEC,
                quality_extra={
                    "future_target_allowed": True,
                    "target_is_first_future_transfer": True,
                    "direction": "scrub_nurse_to_surgeon",
                },
            )
        )
    return rows


RFDETR_CLASS_TO_TOOL = {
    "Adson forceps": "adson_forceps",
    "Bipolar Cautery": "bipolar_forceps",
    "Bipolar cautery": "bipolar_forceps",
    "Bovie surgical cautery": "bovie",
    "Allis forceps": "allis_forceps",
    "Allis clamp forceps": "allis_forceps",
    "Army navy retractor": "army_navy_retractor",
    "Mosquito forceps": "mosquito_forceps",
    "Yankauer suction": "yankauer_suction",
    "Thyroid retractor": "kocher_retractor",
}


def _bbox_iou(left: Sequence[Any], right: Sequence[Any]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    lx1, ly1, lx2, ly2 = (float(value) for value in left)
    rx1, ry1, rx2, ry2 = (float(value) for value in right)
    intersection_width = max(0.0, min(lx2, rx2) - max(lx1, rx1))
    intersection_height = max(0.0, min(ly2, ry2) - max(ly1, ry1))
    intersection = intersection_width * intersection_height
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _load_rfdetr_bundle(
    sources: CaseSources,
) -> tuple[Path, str, dict[str, Any]]:
    context_sources = sources.clinical_manifest.get("context_sources")
    if not isinstance(context_sources, list):
        raise BuildError(f"{sources.case_id}: clinical context_sources missing")
    descriptor = next(
        (
            item
            for item in context_sources
            if isinstance(item, dict)
            and item.get("role") == "ai_inference_reference_not_ground_truth"
            and str(item.get("file", "")).endswith(".json")
        ),
        None,
    )
    if descriptor is None:
        raise BuildError(
            f"{sources.case_id}: manifest-bound RF-DETR context is missing"
        )
    path, digest = _resolve_bound_descriptor(
        repo_root=sources.repo_root,
        base=sources.clinical_case_dir,
        descriptor=descriptor,
        label=f"{sources.case_id} RF-DETR pseudo-label source",
    )
    bundle = load_json(path)
    if (
        bundle.get("schema") != "taskplanner.rfdetr_overlay_bundle.v1"
        or bundle.get("case_id") != sources.case_id
        or bundle.get("authority") != "ai_inference_reference_not_ground_truth"
        or int(bundle.get("frame_count", -1)) != len(sources.timestamps)
        or bundle.get("frame_index_mapping")
        != "source_frame_idx_one_to_one_zero_based"
    ):
        raise BuildError(f"{sources.case_id}: invalid RF-DETR overlay bundle")
    return path, digest, bundle


def _rfdetr_consistent_candidates(
    sources: CaseSources,
) -> list[dict[str, Any]]:
    """Return high-confidence trailing-5-frame pseudo-label candidates."""

    path, digest, bundle = _load_rfdetr_bundle(sources)
    candidates: list[dict[str, Any]] = []
    views = bundle.get("views")
    if not isinstance(views, dict):
        raise BuildError(f"{sources.case_id}: RF-DETR views missing")
    for view in ("cam4", "flir"):
        view_bundle = views.get(view)
        if not isinstance(view_bundle, dict):
            raise BuildError(f"{sources.case_id}: RF-DETR {view} missing")
        frames = view_bundle.get("frames")
        if not isinstance(frames, list) or len(frames) != len(sources.timestamps):
            raise BuildError(
                f"{sources.case_id}: RF-DETR {view} frame count mismatch"
            )
        for frame_index in range(4, len(frames)):
            current = frames[frame_index]
            if not isinstance(current, list):
                raise BuildError(
                    f"{sources.case_id}: malformed RF-DETR frame {frame_index}"
                )
            for detection in current:
                if not isinstance(detection, dict):
                    continue
                class_name = str(detection.get("class_name", ""))
                tool = RFDETR_CLASS_TO_TOOL.get(class_name)
                confidence = float(detection.get("confidence", 0.0))
                bbox = detection.get("bbox_xyxy")
                if tool is None or confidence < 0.9 or not isinstance(bbox, list):
                    continue
                # Exclude known detector identity collisions at effectively the
                # same box, even when the conflicting class has lower score.
                has_conflict = any(
                    isinstance(other, dict)
                    and other is not detection
                    and RFDETR_CLASS_TO_TOOL.get(
                        str(other.get("class_name", ""))
                    )
                    not in (None, tool)
                    and isinstance(other.get("bbox_xyxy"), list)
                    and _bbox_iou(bbox, other["bbox_xyxy"]) >= 0.8
                    for other in current
                )
                if has_conflict:
                    continue
                support_frames: list[int] = []
                support_confidences: list[float] = []
                for support_index in range(frame_index - 4, frame_index + 1):
                    support = frames[support_index]
                    if not isinstance(support, list):
                        continue
                    matches = [
                        item
                        for item in support
                        if isinstance(item, dict)
                        and str(item.get("class_name", "")) == class_name
                        and float(item.get("confidence", 0.0)) >= 0.9
                        and isinstance(item.get("bbox_xyxy"), list)
                        and _bbox_iou(bbox, item["bbox_xyxy"]) >= 0.5
                    ]
                    if matches:
                        best = max(
                            matches,
                            key=lambda item: float(item["confidence"]),
                        )
                        support_frames.append(support_index)
                        support_confidences.append(float(best["confidence"]))
                if len(support_frames) < 3:
                    continue
                if crosses_gap(frame_index - 4, frame_index, sources.gaps):
                    continue
                candidates.append(
                    {
                        "case_id": sources.case_id,
                        "view": view,
                        "frame_index": frame_index,
                        "tool": tool,
                        "class_name": class_name,
                        "confidence": confidence,
                        "bbox_xyxy": [float(value) for value in bbox],
                        "support_frames": support_frames,
                        "support_count": len(support_frames),
                        "minimum_support_confidence": min(
                            support_confidences
                        ),
                        "source_path": path,
                        "source_sha256": digest,
                    }
                )
    return candidates


def _select_balanced_rfdetr_candidates(
    sources_by_case: Mapping[str, CaseSources],
    *,
    train_cases: set[str],
    seed: int,
    maximum_per_tool: int = 24,
) -> list[dict[str, Any]]:
    all_candidates: list[dict[str, Any]] = []
    for case_id in sorted(train_cases):
        sources = sources_by_case[case_id]
        all_candidates.extend(_rfdetr_consistent_candidates(sources))

    # A single-image, single-target SFT row cannot safely express multiple
    # pseudo tool identities for identical media.  Even spatially separate
    # detections would yield contradictory QA duplicates, so retain only the
    # highest-confidence candidate at each exact case/view/frame.
    best_by_media: dict[tuple[str, str, int], dict[str, Any]] = {}
    for candidate in all_candidates:
        key = (
            str(candidate["case_id"]),
            str(candidate["view"]),
            int(candidate["frame_index"]),
        )
        previous = best_by_media.get(key)
        if previous is None or (
            float(candidate["confidence"]),
            int(candidate["support_count"]),
            str(candidate["tool"]),
        ) > (
            float(previous["confidence"]),
            int(previous["support_count"]),
            str(previous["tool"]),
        ):
            best_by_media[key] = candidate

    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in best_by_media.values():
        by_tool[candidate["tool"]].append(candidate)
    selected: list[dict[str, Any]] = []
    for tool, candidates in sorted(by_tool.items()):
        tool_seed = int(
            sha256_bytes(f"{seed}:rfdetr:{tool}".encode("utf-8"))[:16], 16
        )
        rng = random.Random(tool_seed)
        ordered = sorted(
            candidates,
            key=lambda item: (
                item["case_id"],
                item["view"],
                item["frame_index"],
                -item["confidence"],
            ),
        )
        rng.shuffle(ordered)
        accepted: list[dict[str, Any]] = []
        # Avoid turning one stable detection run into many near-duplicates.
        for candidate in ordered:
            if any(
                prior["case_id"] == candidate["case_id"]
                and prior["view"] == candidate["view"]
                and abs(prior["frame_index"] - candidate["frame_index"]) < 15
                for prior in accepted
            ):
                continue
            accepted.append(candidate)
            if len(accepted) >= maximum_per_tool:
                break
        selected.extend(accepted)
    return sorted(
        selected,
        key=lambda item: (
            item["case_id"],
            item["tool"],
            item["view"],
            item["frame_index"],
        ),
    )


def _build_rfdetr_pseudo_rows(
    sources_by_case: Mapping[str, CaseSources],
    *,
    train_cases: set[str],
    seed: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in _select_balanced_rfdetr_candidates(
        sources_by_case,
        train_cases=train_cases,
        seed=seed,
    ):
        sources = sources_by_case[candidate["case_id"]]
        frame = int(candidate["frame_index"])
        media = sample_causal_frames(
            sources,
            cutoff_frame=frame,
            start_frame=frame - 4,
            count=3,
            view=str(candidate["view"]),
        )
        source_binding = {
            "path": _repo_relative(
                sources.repo_root, Path(candidate["source_path"])
            ),
            "sha256": candidate["source_sha256"],
        }
        row = _base_row(
            sources,
            example_id=(
                f"{sources.case_id}:tool_presence_pseudo:"
                f"{candidate['view']}:{candidate['tool']}:f{frame:06d}"
            ),
            task_type="tool_presence_pseudo",
            cutoff_sec=sources.timestamps[frame],
            window_start_sec=sources.timestamps[frame - 4],
            media=media,
            target={
                "event": "detector_pseudo_presence",
                "tool": candidate["tool"],
                "view": candidate["view"],
                "exhaustive_visible_tool_inventory": False,
            },
            authority={
                "tier": "pseudo",
                "label": (
                    "RF-DETR confidence>=0.90, temporal>=3/5, "
                    "no conflicting overlapping tool class"
                ),
                "review_status": "not_human_reviewed",
                "label_origin": "rfdetr_ai_inference",
            },
            source_ids=[
                (
                    f"rfdetr:{candidate['view']}:"
                    f"frame:{candidate['frame_index']}"
                )
            ],
            quality_extra={
                "supervision_scope": "sparse_positive_pseudo_presence_only",
                "absence_labels_available": False,
                "pseudo_label_train_only": True,
                "rfdetr_confidence": candidate["confidence"],
                "rfdetr_support_count_in_trailing_5": candidate[
                    "support_count"
                ],
                "rfdetr_minimum_support_confidence": candidate[
                    "minimum_support_confidence"
                ],
                "rfdetr_conflicting_bbox_class": False,
                "tool_name_voice_leakage_blocked": True,
                "scoring_eligible": False,
            },
            include_voice=False,
        )
        row["source_bindings"] = {
            **row["source_bindings"],
            "rfdetr_pseudo_source": source_binding,
        }
        row["source_snapshot_id"] = sha256_bytes(
            canonical_json(row["source_bindings"]).encode("utf-8")
        )
        rows.append(row)
    return rows


def _build_clinical_rows(sources: CaseSources) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in sources.clinical:
        annotation_id = str(candidate["annotation_id"])
        start_frame = int(candidate["evidence_start_source_frame_idx"])
        end_frame = int(candidate["evidence_end_source_frame_idx"])
        if crosses_gap(start_frame, end_frame, sources.gaps):
            raise BuildError(
                f"{annotation_id}: clinical evidence crosses a visual gap"
            )
        media = sample_causal_frames(
            sources,
            cutoff_frame=end_frame,
            start_frame=start_frame,
            count=4,
            view="flir",
        )
        cutoff = sources.timestamps[end_frame]
        provenance = candidate.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        rows.append(
            _base_row(
                sources,
                example_id=(
                    f"{sources.case_id}:"
                    f"clinical_observation_interpretation:{annotation_id}"
                ),
                task_type="clinical_observation_interpretation",
                cutoff_sec=cutoff,
                window_start_sec=sources.timestamps[start_frame],
                media=media,
                target={
                    "observation": str(candidate["observation"]).strip(),
                    "interpretation": str(candidate["interpretation"]).strip(),
                    "confidence": candidate.get("confidence"),
                },
                authority={
                    "tier": "silver_ai_draft_needs_surgeon_review",
                    "label": "AI clinical draft pending surgeon review",
                    "review_status": candidate.get("review_status"),
                    "label_origin": provenance.get("authority"),
                },
                source_ids=[annotation_id],
                quality_extra={
                    "clinical_scoring_eligible": False,
                    "target_uses_full_declared_evidence_window": True,
                    "rfdetr_is_ground_truth": False,
                },
            )
        )
    return rows


def build_case_rows(
    sources: CaseSources,
    *,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Render all defensible task rows for one manifest snapshot."""

    case_seed = int(
        sha256_bytes(f"{seed}:{sources.case_id}".encode("utf-8"))[:16], 16
    )
    rng = random.Random(case_seed)
    rows = [
        *_build_tool_presence_rows(sources),
        *_build_request_rows(sources),
        *_build_phase_rows(sources),
        *_build_next_tool_rows(sources, rng=rng),
        *_build_clinical_rows(sources),
    ]
    rows.sort(
        key=lambda row: (
            TASK_ORDER[row["task_type"]],
            float(row["time"]["causal_cutoff_sec"]),
            row["example_id"],
        )
    )
    ids = [row["example_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise BuildError(f"{sources.case_id}: duplicate generated example_id")
    return rows


def assign_case_folds(
    cases: Sequence[str],
    *,
    seed: int,
    fold_count: int,
) -> dict[str, int]:
    if fold_count < 3:
        raise BuildError("fold_count must be at least 3")
    if len(cases) < fold_count:
        raise BuildError("number of cases must be at least fold_count")
    if len(set(cases)) != len(cases):
        raise BuildError("duplicate case IDs")
    shuffled = sorted(cases)
    random.Random(seed).shuffle(shuffled)
    return {
        case_id: position % fold_count
        for position, case_id in enumerate(shuffled)
    }


def split_role(
    fold_id: int,
    *,
    held_out_fold: int,
    fold_count: int,
) -> str:
    if fold_id == held_out_fold:
        return "test"
    if fold_id == (held_out_fold + 1) % fold_count:
        return "validation"
    return "train"


def build_folds_document(
    cases: Sequence[str],
    *,
    seed: int,
    fold_count: int,
    selected_held_out_fold: int,
) -> dict[str, Any]:
    assignment = assign_case_folds(cases, seed=seed, fold_count=fold_count)
    folds = {
        f"fold_{index}": sorted(
            case_id
            for case_id, assigned in assignment.items()
            if assigned == index
        )
        for index in range(fold_count)
    }
    partitions = {}
    for held_out in range(fold_count):
        partitions[f"held_out_fold_{held_out}"] = {
            role: sorted(
                case_id
                for case_id, assigned in assignment.items()
                if split_role(
                    assigned,
                    held_out_fold=held_out,
                    fold_count=fold_count,
                )
                == role
            )
            for role in ("train", "validation", "test")
        }
    return {
        "schema": FOLDS_SCHEMA,
        "group_key": "case_id",
        "seed": seed,
        "fold_count": fold_count,
        "selected_held_out_fold": selected_held_out_fold,
        "assignment": {
            case_id: f"fold_{fold_id}"
            for case_id, fold_id in sorted(assignment.items())
        },
        "folds": folds,
        "cv_partitions": partitions,
        "notes": [
            "All frames, cameras, crops, prompts, and targets from one case stay in one fold.",
            "These same-campaign cases are development/calibration data, not an external generalization test.",
        ],
    }


def _load_multiview_media(
    sources: CaseSources,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache_root = Path(
        os.environ.get(
            "TASKPLANNER_ANNOTATION_CACHE",
            str(Path.home() / ".cache/taskplanner_annotation"),
        )
    ).expanduser()
    cache_dir = cache_root / sources.case_id
    manifest_path = cache_dir / "review_multiview.manifest.json"
    if not manifest_path.is_file():
        raise BuildError(
            f"{sources.case_id}: review_multiview.manifest.json is missing"
        )
    manifest = load_json(manifest_path)
    if (
        manifest.get("schema")
        != "taskplanner.review_multiview_proxy_manifest.v1"
        or manifest.get("case_id") != sources.case_id
    ):
        raise BuildError(f"{sources.case_id}: invalid multiview manifest")
    inputs = manifest.get("inputs")
    timeline_input = inputs.get("timeline") if isinstance(inputs, dict) else None
    if (
        not isinstance(timeline_input, dict)
        or timeline_input.get("sha256")
        != sources.bindings["timeline"]["sha256"]
        or Path(str(timeline_input.get("path", ""))).resolve()
        != sources.timeline_path.resolve()
    ):
        raise BuildError(
            f"{sources.case_id}: multiview proxy is bound to another timeline"
        )
    timeline = manifest.get("timeline")
    proxy_gaps = timeline.get("gaps", []) if isinstance(timeline, dict) else []
    gap_geometry_matches = (
        isinstance(proxy_gaps, list)
        and len(proxy_gaps) == len(sources.gaps)
        and all(
            int(proxy.get("before_frame_idx", -1))
            == int(source["before_frame_idx"])
            and int(proxy.get("after_frame_idx", -1))
            == int(source["after_frame_idx"])
            and abs(
                float(proxy.get("before_time_sec", math.nan))
                - float(source["before_time_sec"])
            )
            <= EPSILON_SEC
            and abs(
                float(proxy.get("after_time_sec", math.nan))
                - float(source["after_time_sec"])
            )
            <= EPSILON_SEC
            for proxy, source in zip(proxy_gaps, sources.gaps, strict=True)
            if isinstance(proxy, dict)
        )
    )
    if (
        not isinstance(timeline, dict)
        or int(timeline.get("frame_count", -1)) != len(sources.timestamps)
        or not gap_geometry_matches
    ):
        raise BuildError(
            f"{sources.case_id}: multiview timeline geometry mismatch"
        )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise BuildError(f"{sources.case_id}: multiview outputs are missing")
    selected: dict[str, dict[str, Any]] = {}
    for view in ("cam4", "flir"):
        descriptor = outputs.get(view)
        if not isinstance(descriptor, dict):
            raise BuildError(f"{sources.case_id}: missing {view} proxy")
        media_path = Path(str(descriptor.get("path", ""))).resolve()
        if not media_path.is_file():
            raise BuildError(
                f"{sources.case_id}: {view} proxy file is missing"
            )
        expected_sha = descriptor.get("sha256")
        actual_sha = sha256_file(media_path)
        if actual_sha != expected_sha:
            raise BuildError(f"{sources.case_id}: {view} proxy SHA mismatch")
        probe = descriptor.get("media_probe")
        if (
            not isinstance(probe, dict)
            or int(probe.get("frame_count", -1)) != len(sources.timestamps)
        ):
            raise BuildError(
                f"{sources.case_id}: {view} proxy frame count mismatch"
            )
        selected[view] = {
            "path": str(media_path),
            "sha256": actual_sha,
            "declared_frame_count": len(sources.timestamps),
        }
    return selected, {
        "path": str(manifest_path.resolve()),
        "sha256": sha256_file(manifest_path),
    }


def materialize_images(
    rows: Sequence[MutableMapping[str, Any]],
    sources_by_case: Mapping[str, CaseSources],
    *,
    output_dir: Path,
    jpeg_quality: int = 92,
) -> dict[str, Any]:
    """Decode complete overlay-free proxies and save only requested frames."""

    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise BuildError("OpenCV is required to materialize frame images") from exc

    requested: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in rows:
        for media in row["media"]:
            requested[(row["case_id"], media["view"])].add(
                int(media["source_frame_idx"])
            )

    media_bindings: dict[str, Any] = {}
    image_paths: dict[tuple[str, str, int], str] = {}
    for case_id in sorted(sources_by_case):
        sources = sources_by_case[case_id]
        proxies, proxy_manifest = _load_multiview_media(sources)
        case_report: dict[str, Any] = {
            "proxy_manifest": proxy_manifest,
            "views": {},
        }
        for view in ("cam4", "flir"):
            needed = requested.get((case_id, view), set())
            if not needed:
                continue
            proxy = proxies[view]
            capture = cv2.VideoCapture(proxy["path"])
            if not capture.isOpened():
                raise BuildError(f"{case_id}: cannot open {view} proxy")
            declared_by_decoder = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            if declared_by_decoder != len(sources.timestamps):
                capture.release()
                raise BuildError(
                    f"{case_id}: decoder reports {declared_by_decoder} {view} "
                    f"frames, expected {len(sources.timestamps)}"
                )
            view_dir = output_dir / "images" / case_id / view
            view_dir.mkdir(parents=True, exist_ok=True)
            decoded_count = 0
            written_count = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frame_index = decoded_count
                if frame_index in needed:
                    image_path = (
                        view_dir / f"frame_{frame_index:06d}.jpg"
                    ).resolve()
                    encode_ok, encoded = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
                    )
                    if not encode_ok:
                        capture.release()
                        raise BuildError(
                            f"{case_id}: failed to encode {view} frame "
                            f"{frame_index}"
                        )
                    image_path.write_bytes(encoded.tobytes())
                    image_paths[(case_id, view, frame_index)] = str(image_path)
                    written_count += 1
                decoded_count += 1
            capture.release()
            if decoded_count != len(sources.timestamps):
                raise BuildError(
                    f"{case_id}: fully decoded {decoded_count} {view} frames, "
                    f"expected {len(sources.timestamps)}"
                )
            missing = sorted(needed - {
                frame
                for cid, candidate_view, frame in image_paths
                if cid == case_id and candidate_view == view
            })
            if missing:
                raise BuildError(
                    f"{case_id}: {view} requested frames were not decoded: "
                    f"{missing[:10]}"
                )
            case_report["views"][view] = {
                **proxy,
                "decoded_frame_count": decoded_count,
                "requested_unique_frame_count": len(needed),
                "written_unique_frame_count": written_count,
            }
        media_bindings[case_id] = case_report

    for row in rows:
        for media in row["media"]:
            key = (
                row["case_id"],
                media["view"],
                int(media["source_frame_idx"]),
            )
            path = image_paths.get(key)
            if path is None or not Path(path).is_file():
                raise BuildError(f"{row['example_id']}: materialized image missing")
            media["path"] = path
    return {
        "case_media": media_bindings,
        "unique_image_count": len(image_paths),
        "all_proxy_frames_fully_decoded": True,
    }


def _voice_prompt(voices: Sequence[Mapping[str, Any]]) -> str:
    if not voices:
        return "이 시점까지 이용 가능한 ASR: 없음"
    lines = ["이 시점까지 이용 가능한 ASR:"]
    lines.extend(f"- {voice['text']}" for voice in voices)
    return "\n".join(lines)


def _task_prompt(row: Mapping[str, Any]) -> str:
    task = row["task_type"]
    voices = row["causal_context"]["voice"]
    voice_section = _voice_prompt(voices)
    canonical_tools = ", ".join(sorted(TOOL_SPEECH_ALIASES))
    if task == "tool_presence_at_transfer":
        return (
            "시간 순서의 CAM4 프레임은 물리적 도구 전달 시점까지를 보여준다. "
            "이 실물 인계 사건에서 관찰되는 도구와 제공자·수령자를 판별하라. "
            "화면에 보이는 모든 도구 목록을 추정하지 말라. "
            f"canonical tool ID: {canonical_tools}.\n{voice_section}\n"
            "출력 키: event, tool, from, to, "
            "exhaustive_visible_tool_inventory"
        )
    if task == "tool_presence_pseudo":
        return (
            "시간 순서의 오버레이 없는 수술 영상 프레임에서 반복해서 보이는 "
            "하나의 도구를 canonical ID로 판별하라. 이 질문은 화면의 모든 "
            "도구 목록이나 부재를 묻지 않는다. "
            f"canonical tool ID: {canonical_tools}.\n"
            "출력 키: event, tool, view, "
            "exhaustive_visible_tool_inventory"
        )
    if task == "request_intent":
        return (
            "시간 순서의 CAM4 프레임에서 집도의 손짓을 판별하라. 빈 손바닥 "
            "요청 뒤 실제 전달된 도구를 소급해 요청 도구로 쓰지 말라.\n"
            f"{voice_section}\n"
            "출력 키: event, intent, requested_tool, "
            "tool_identity_inferred_from_later_transfer"
        )
    if task == "current_phase":
        phase_text = "; ".join(
            f"{phase_id}={name}" for phase_id, name in PHASE_LABELS.items()
        )
        return (
            "시간 순서의 FLIR 수술야 프레임에서 현재 기능적 수술 단계를 "
            "판별하라. 단일 도구 이름이 아니라 지속되는 견인 구조와 "
            "도구-조직 상호작용을 근거로 한다. "
            f"단계 정의: {phase_text}.\n{voice_section}\n"
            "출력 키: phase_id, phase_name_ko, state, transition_from, "
            "transition_to"
        )
    if task == "next_physical_tool":
        horizon = row["time"]["prediction_horizon_sec"]
        return (
            "시간 순서의 CAM4 프레임과 현재 시점까지 완료된 ASR을 바탕으로 "
            f"앞으로 {horizon:g}초 이내 scrub_nurse에서 surgeon으로 실제 "
            "전달될 첫 도구를 예측하라. 해당 전달이 없으면 "
            "next_transfer_tool과 event를 모두 none으로 답하라. 무언 요청의 "
            "도구를 미래 전달에서 역으로 채우지 말라. "
            f"canonical tool ID: {canonical_tools}.\n{voice_section}\n"
            "출력 키: next_transfer_tool, event, basis"
        )
    if task == "clinical_observation_interpretation":
        return (
            "시간 순서의 FLIR 프레임으로 제공된 최근 수술 구간을 임상적으로 "
            "분석하라. observation에는 직접 보이는 도구·조직·행위·시야·"
            "조직 반응을 1~2문장으로 쓰고, interpretation에는 문맥상 가장 "
            "가능성 높은 임상 의미를 1~2문장으로 쓰라. 판독 제한은 별도 "
            "클래스가 아니라 observation 문장에 포함한다.\n"
            f"{voice_section}\n"
            "출력 키: observation, interpretation, confidence"
        )
    raise BuildError(f"unsupported task prompt: {task}")


def _assistant_target(row: Mapping[str, Any]) -> dict[str, Any]:
    target = dict(row["target"])
    if row["task_type"] == "next_physical_tool":
        return {
            key: target[key]
            for key in ("next_transfer_tool", "event", "basis")
        }
    return target


def render_unsloth_row(row: Mapping[str, Any]) -> dict[str, Any]:
    image_content = []
    for media in row["media"]:
        path = media.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise BuildError(
                f"{row['example_id']}: absolute materialized image path required"
            )
        image_content.append({"type": "image", "image": path})
    user_content = [
        *image_content,
        {"type": "text", "text": _task_prompt(row)},
    ]
    assistant_text = canonical_json(_assistant_target(row))
    return {
        "schema": UNSLOTH_SCHEMA,
        "example_id": row["example_id"],
        "case_id": row["case_id"],
        "task_type": row["task_type"],
        "prediction_regime": row["target"].get("prediction_regime"),
        "split_group_id": row["split_group_id"],
        "fold_id": row["split"]["fold_id"],
        "split": row["split"]["role"],
        "authority": row["authority"]["tier"],
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": SYSTEM_PROMPT}],
            },
            {"role": "user", "content": user_content},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        ],
    }


def validate_rows(
    rows: Sequence[Mapping[str, Any]],
    sources_by_case: Mapping[str, CaseSources],
    *,
    require_images: bool,
) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    group_roles: dict[str, set[str]] = defaultdict(set)
    transfers_by_case = {
        case_id: _surgeon_direction_transfers(sources)
        for case_id, sources in sources_by_case.items()
    }
    for row in rows:
        example_id = str(row.get("example_id", ""))
        if not example_id or example_id in ids:
            errors.append(f"duplicate/missing example_id: {example_id}")
        ids.add(example_id)
        case_id = str(row.get("case_id", ""))
        sources = sources_by_case.get(case_id)
        if sources is None:
            errors.append(f"{example_id}: unknown case")
            continue
        if row.get("schema") != MASTER_SCHEMA:
            errors.append(f"{example_id}: wrong schema")
        expected_snapshot = (
            sha256_bytes(
                canonical_json(row.get("source_bindings", {})).encode("utf-8")
            )
            if row.get("task_type") == "tool_presence_pseudo"
            else sources.snapshot_id
        )
        if row.get("source_snapshot_id") != expected_snapshot:
            errors.append(f"{example_id}: stale source snapshot")
        split = row.get("split")
        if not isinstance(split, dict):
            errors.append(f"{example_id}: split is missing")
        else:
            group_roles[str(row.get("split_group_id"))].add(
                str(split.get("role"))
            )
        cutoff = float(row["time"]["causal_cutoff_sec"])
        media = row.get("media", [])
        if not 2 <= len(media) <= 4:
            errors.append(f"{example_id}: media count is not 2..4")
        if any(float(item["time_sec"]) > cutoff + EPSILON_SEC for item in media):
            errors.append(f"{example_id}: future media leakage")
        if any(
            float(voice["available_sec"]) > cutoff + EPSILON_SEC
            for voice in row["causal_context"]["voice"]
        ):
            errors.append(f"{example_id}: future voice leakage")
        frame_indices = [int(item["source_frame_idx"]) for item in media]
        if frame_indices != sorted(frame_indices):
            errors.append(f"{example_id}: media is not chronological")
        if frame_indices and crosses_gap(
            min(frame_indices), max(frame_indices), sources.gaps
        ):
            errors.append(f"{example_id}: media crosses gap")
        if require_images:
            for item in media:
                path = item.get("path")
                if (
                    not isinstance(path, str)
                    or not Path(path).is_absolute()
                    or not Path(path).is_file()
                ):
                    errors.append(f"{example_id}: image is not materialized")
        if row["task_type"] in (
            "tool_presence_at_transfer",
            "tool_presence_pseudo",
        ):
            target = row["target"]
            if target.get("exhaustive_visible_tool_inventory") is not False:
                errors.append(f"{example_id}: exhaustive tool claim")
            if row["quality"].get("absence_labels_available") is not False:
                errors.append(f"{example_id}: invalid tool absence authority")
            if row["causal_context"]["voice"]:
                errors.append(f"{example_id}: tool-name voice leakage")
        if row["task_type"] == "tool_presence_pseudo":
            if row["authority"].get("tier") != "pseudo":
                errors.append(f"{example_id}: pseudo authority promoted")
            if row.get("split", {}).get("role") != "train":
                errors.append(f"{example_id}: pseudo label outside train split")
            if row["quality"].get("pseudo_label_train_only") is not True:
                errors.append(f"{example_id}: missing train-only pseudo guard")
        if row["task_type"] == "request_intent":
            if row["target"].get("requested_tool") is not None:
                errors.append(f"{example_id}: implicit request tool backfilled")
        if row["task_type"] == "current_phase":
            if row["authority"].get("tier") != (
                "provisional_ai_phase_not_scoring_ground_truth"
            ):
                errors.append(f"{example_id}: Phase authority promoted")
        if row["task_type"] == "clinical_observation_interpretation":
            if row["authority"].get("tier") != (
                "silver_ai_draft_needs_surgeon_review"
            ):
                errors.append(f"{example_id}: clinical authority promoted")
        if row["task_type"] == "next_physical_tool":
            horizon = float(row["time"]["prediction_horizon_sec"])
            first = _first_future_transfer(
                transfers_by_case[case_id],
                cutoff_sec=cutoff,
                horizon_sec=horizon,
            )
            target_event = row["target"].get("target_event_id")
            if first is None and target_event is not None:
                errors.append(f"{example_id}: future target does not exist")
            if first is not None and target_event != first["event_id"]:
                errors.append(f"{example_id}: target is not first transfer")
            if first is None and row["target"].get("next_transfer_tool") != "none":
                errors.append(f"{example_id}: negative target is not none")
            if (
                first is not None
                and row["target"].get("next_transfer_tool") != first["tool"]
            ):
                errors.append(f"{example_id}: next tool mismatch")
    for group, roles in group_roles.items():
        if len(roles) != 1:
            errors.append(f"{group}: split group crosses roles: {sorted(roles)}")
    return errors


def _counter_dict(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted((str(key), count) for key, count in Counter(values).items()))


def _audit_document(
    *,
    rows: Sequence[Mapping[str, Any]],
    sources_by_case: Mapping[str, CaseSources],
    folds: Mapping[str, Any],
    media_report: Mapping[str, Any] | None,
    errors: Sequence[str],
    output_hashes: Mapping[str, str],
    materialize_images_enabled: bool,
) -> dict[str, Any]:
    task_counts = _counter_dict(row["task_type"] for row in rows)
    case_counts = _counter_dict(row["case_id"] for row in rows)
    split_counts = _counter_dict(row["split"]["role"] for row in rows)
    authority_counts = _counter_dict(
        row["authority"]["tier"] for row in rows
    )
    next_rows = [
        row for row in rows if row["task_type"] == "next_physical_tool"
    ]
    next_target_counts = _counter_dict(
        row["target"]["next_transfer_tool"] for row in next_rows
    )
    phase_counts = _counter_dict(
        row["target"]["phase_id"]
        for row in rows
        if row["task_type"] == "current_phase"
    )
    pseudo_tool_counts = _counter_dict(
        row["target"]["tool"]
        for row in rows
        if row["task_type"] == "tool_presence_pseudo"
    )
    return {
        "schema": AUDIT_SCHEMA,
        "ok": not errors,
        "errors": list(errors),
        "warnings": [
            "Phase labels are provisional ambiguous context, not scoring ground truth.",
            "Clinical observation/interpretation labels are AI drafts awaiting surgeon review.",
            "Tool-presence supervision is sparse positive transfer-event supervision and has no exhaustive absence labels.",
            "0704_6 through 0704_17 are one development/calibration campaign, not an external generalization test.",
        ],
        "grain": "one causal task-specific QA example",
        "case_count": len(sources_by_case),
        "example_count": len(rows),
        "counts": {
            "by_task": task_counts,
            "by_case": case_counts,
            "by_split": split_counts,
            "by_authority": authority_counts,
            "next_tool_targets": next_target_counts,
            "phase_targets": phase_counts,
            "pseudo_tool_targets": pseudo_tool_counts,
        },
        "quality_checks": {
            "manifest_hash_binding": "PASS" if not errors else "FAIL",
            "case_group_split_integrity": "PASS" if not errors else "FAIL",
            "no_future_media_or_voice": "PASS" if not errors else "FAIL",
            "visual_gap_safety": "PASS" if not errors else "FAIL",
            "first_future_transfer_semantics": "PASS" if not errors else "FAIL",
            "authority_not_promoted": "PASS" if not errors else "FAIL",
            "decoded_frame_count_equals_timeline": (
                "PASS"
                if materialize_images_enabled and not errors
                else "NOT_RUN"
                if not materialize_images_enabled
                else "FAIL"
            ),
        },
        "source_snapshots": {
            case_id: {
                "snapshot_id": sources.snapshot_id,
                "bindings": sources.bindings,
                "frame_count": len(sources.timestamps),
                "gap_count": len(sources.gaps),
            }
            for case_id, sources in sorted(sources_by_case.items())
        },
        "folds": folds,
        "media": media_report,
        "output_sha256": dict(sorted(output_hashes.items())),
    }


def build_dataset(
    *,
    repo_root: Path,
    output_dir: Path,
    cases: Sequence[str] = DEFAULT_CASES,
    seed: int = DEFAULT_SEED,
    fold_count: int = DEFAULT_FOLD_COUNT,
    held_out_fold: int = DEFAULT_HELD_OUT_FOLD,
    materialize: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build master, folds, Unsloth messages, and a fail-closed audit."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if not 0 <= held_out_fold < fold_count:
        raise BuildError("held_out_fold is outside fold range")
    marker_files = (
        output_dir / "master.jsonl",
        output_dir / "folds.json",
        output_dir / "unsloth_messages.jsonl",
        output_dir / "audit.json",
    )
    if any(path.exists() for path in marker_files) and not overwrite:
        raise BuildError(
            f"output already exists; use --overwrite for this exact directory: "
            f"{output_dir}"
        )
    if overwrite and output_dir.exists():
        # Remove only artifacts owned by this builder, never the directory as a
        # whole.  This preserves unrelated user files.
        for path in marker_files:
            if path.is_file():
                path.unlink()
        images_dir = output_dir / "images"
        if images_dir.is_dir():
            shutil.rmtree(images_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sources_by_case = {
        case_id: load_case_sources(repo_root, case_id)
        for case_id in cases
    }
    folds = build_folds_document(
        cases,
        seed=seed,
        fold_count=fold_count,
        selected_held_out_fold=held_out_fold,
    )
    assignment = {
        case_id: int(str(fold_name).removeprefix("fold_"))
        for case_id, fold_name in folds["assignment"].items()
    }
    rows: list[dict[str, Any]] = []
    for case_id in cases:
        case_rows = build_case_rows(sources_by_case[case_id], seed=seed)
        fold_id = assignment[case_id]
        role = split_role(
            fold_id,
            held_out_fold=held_out_fold,
            fold_count=fold_count,
        )
        for row in case_rows:
            row["split"] = {
                "fold_id": f"fold_{fold_id}",
                "role": role,
                "held_out_fold": f"fold_{held_out_fold}",
            }
        rows.extend(case_rows)
    train_cases = {
        case_id
        for case_id, fold_id in assignment.items()
        if split_role(
            fold_id,
            held_out_fold=held_out_fold,
            fold_count=fold_count,
        )
        == "train"
    }
    pseudo_rows = _build_rfdetr_pseudo_rows(
        sources_by_case,
        train_cases=train_cases,
        seed=seed,
    )
    for row in pseudo_rows:
        fold_id = assignment[row["case_id"]]
        role = split_role(
            fold_id,
            held_out_fold=held_out_fold,
            fold_count=fold_count,
        )
        if role != "train":
            raise BuildError(
                f"{row['example_id']}: pseudo label escaped train split"
            )
        row["split"] = {
            "fold_id": f"fold_{fold_id}",
            "role": role,
            "held_out_fold": f"fold_{held_out_fold}",
        }
    rows.extend(pseudo_rows)
    rows.sort(
        key=lambda row: (
            row["case_id"],
            TASK_ORDER[row["task_type"]],
            float(row["time"]["causal_cutoff_sec"]),
            row["example_id"],
        )
    )

    media_report = None
    if materialize:
        media_report = materialize_images(
            rows,
            sources_by_case,
            output_dir=output_dir,
        )

    pre_errors = validate_rows(
        rows,
        sources_by_case,
        require_images=materialize,
    )
    if pre_errors:
        raise BuildError(
            "dataset validation failed before write:\n- "
            + "\n- ".join(pre_errors[:30])
        )

    master_path = output_dir / "master.jsonl"
    folds_path = output_dir / "folds.json"
    unsloth_path = output_dir / "unsloth_messages.jsonl"
    write_jsonl(master_path, rows)
    write_json(folds_path, folds)
    if materialize:
        unsloth_rows = [render_unsloth_row(row) for row in rows]
        write_jsonl(unsloth_path, unsloth_rows)

    output_hashes = {
        "master.jsonl": sha256_file(master_path),
        "folds.json": sha256_file(folds_path),
    }
    if materialize:
        output_hashes["unsloth_messages.jsonl"] = sha256_file(unsloth_path)

    errors = validate_rows(
        rows,
        sources_by_case,
        require_images=materialize,
    )
    audit = _audit_document(
        rows=rows,
        sources_by_case=sources_by_case,
        folds=folds,
        media_report=media_report,
        errors=errors,
        output_hashes=output_hashes,
        materialize_images_enabled=materialize,
    )
    audit_path = output_dir / "audit.json"
    write_json(audit_path, audit)
    if errors:
        raise BuildError(
            "dataset validation failed:\n- " + "\n- ".join(errors[:30])
        )
    return audit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    default_repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=default_repo)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--cases",
        nargs="+",
        default=list(DEFAULT_CASES),
        help="Case IDs; every camera/prompt derivative remains case-grouped.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--fold-count", type=int, default=DEFAULT_FOLD_COUNT)
    parser.add_argument(
        "--held-out-fold", type=int, default=DEFAULT_HELD_OUT_FOLD
    )
    parser.add_argument(
        "--materialize-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Fully decode overlay-free CAM4/FLIR proxies and emit absolute "
            "JPEG paths plus Unsloth messages (default: enabled)."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only artifacts owned by this builder in output-dir.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    audit = build_dataset(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        cases=tuple(args.cases),
        seed=args.seed,
        fold_count=args.fold_count,
        held_out_fold=args.held_out_fold,
        materialize=args.materialize_images,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "ok": audit["ok"],
                "output_dir": str(args.output_dir.resolve()),
                "example_count": audit["example_count"],
                "counts": audit["counts"],
                "output_sha256": audit["output_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
