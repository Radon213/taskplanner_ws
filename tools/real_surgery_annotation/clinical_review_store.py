"""Append-only clinical-video review storage.

The clinical layer is deliberately separate from the interaction/DT review
store.  AI candidate JSONL is immutable input, human review actions are an
append-only audit log, and any reference JSONL is a reproducible derivative of
the latest action for each candidate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CLINICAL_ANNOTATION_SCHEMA = "taskplanner.clinical_video_annotation.v2"
CLINICAL_MANIFEST_SCHEMA = "taskplanner.clinical_video_manifest.v2"
CLINICAL_REVIEW_ACTION_SCHEMA = "taskplanner.clinical_review_action.v2"
CLINICAL_REVIEW_STATE_SCHEMA = "taskplanner.clinical_review_state.v2"
CLINICAL_REFERENCE_SCHEMA = "taskplanner.clinical_reference.v2"
CLINICAL_REVIEW_STATUSES = ("confirmed", "ambiguous", "rejected")
CLINICAL_CANDIDATE_REVIEW_STATUS = "needs_surgeon_review"
CLINICAL_NARRATIVE_FIELDS = ("observation", "interpretation")
CLINICAL_SOURCE_VIEWS = ("cam1", "cam2", "cam3", "cam4", "flir")
CLINICAL_CANDIDATE_AUTHORITIES = frozenset(("ai_draft",))
CLINICAL_RESULT_AUTHORITY = "human_reviewed_ai_draft_not_automatic_ground_truth"
CLINICAL_SENTENCE_END_PATTERN = re.compile(
    r"""[.!?](?:["'”’)\]]+)?(?=\s|$)"""
)
CLINICAL_SCHEMA_DIR = (
    Path(__file__).resolve().parents[2]
    / "annotations"
    / "clinical_video"
    / "schema"
)
CLINICAL_ANNOTATION_SCHEMA_PATH = (
    CLINICAL_SCHEMA_DIR / "clinical_video_annotation.v2.schema.json"
)
CLINICAL_REVIEW_ACTION_SCHEMA_PATH = (
    CLINICAL_SCHEMA_DIR / "clinical_review_action.v2.schema.json"
)
CLINICAL_MANIFEST_SCHEMA_PATH = (
    CLINICAL_SCHEMA_DIR / "clinical_manifest.v2.schema.json"
)


class ClinicalInputError(Exception):
    """A clinical source artifact or user-editable field is invalid."""


class ClinicalConflictError(Exception):
    """The candidate or optimistic review revision no longer matches."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_non_finite_json(value: Any, *, location: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ClinicalInputError(
            f"{location}: NaN/Infinity는 유효한 JSON 숫자가 아닙니다."
        )
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_non_finite_json(
                nested,
                location=f"{location}.{key}",
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_non_finite_json(
                nested,
                location=f"{location}[{index}]",
            )


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClinicalInputError(f"{path}: JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise ClinicalInputError(f"{path}: JSON 객체가 필요합니다.")
    _reject_non_finite_json(value, location=str(path))
    return value


def _load_jsonl(
    path: Path,
    *,
    missing_ok: bool = False,
) -> list[dict[str, Any]]:
    if not path.exists():
        if missing_ok:
            return []
        raise ClinicalInputError(f"파일이 없습니다: {path}")
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ClinicalInputError(
                        f"{path}:{line_number}: JSONL 형식 오류: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise ClinicalInputError(
                        f"{path}:{line_number}: JSON 객체 레코드가 필요합니다."
                    )
                _reject_non_finite_json(
                    value,
                    location=f"{path}:{line_number}",
                )
                records.append(value)
    except OSError as exc:
        raise ClinicalInputError(
            f"{path}: JSONL을 읽을 수 없습니다: {exc}"
        ) from exc
    return records


class ClinicalReviewStore:
    """Validate immutable AI drafts and append explicit human review actions."""

    def __init__(
        self,
        *,
        case_dir: Path,
        case_id: str,
        source_timeline: Mapping[str, Any] | None = None,
        source_timeline_path: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise ClinicalInputError("임상 case_id 형식이 올바르지 않습니다.")
        self.case_id = case_id
        self.case_dir = case_dir.resolve()
        if self.case_dir.name != case_id:
            raise ClinicalInputError(
                "임상 case 디렉터리 이름과 case_id가 다릅니다."
            )
        self.manifest_path = (
            manifest_path.resolve()
            if manifest_path is not None
            else self.case_dir / "clinical_manifest.v2.json"
        )
        if self.manifest_path.parent != self.case_dir:
            raise ClinicalInputError("임상 manifest가 case 디렉터리를 벗어났습니다.")
        self.source_timeline_path = (
            source_timeline_path.resolve()
            if source_timeline_path is not None
            else None
        )
        self.source_timeline = (
            dict(source_timeline) if source_timeline is not None else None
        )
        annotation_schema = _load_json_object(
            CLINICAL_ANNOTATION_SCHEMA_PATH
        )
        try:
            Draft202012Validator.check_schema(annotation_schema)
        except Exception as exc:
            raise ClinicalInputError(
                "임상 annotation JSON Schema가 올바르지 않습니다: "
                f"{exc}"
            ) from exc
        self.annotation_validator = Draft202012Validator(
            annotation_schema,
            format_checker=FormatChecker(),
        )
        action_schema = _load_json_object(
            CLINICAL_REVIEW_ACTION_SCHEMA_PATH
        )
        try:
            Draft202012Validator.check_schema(action_schema)
        except Exception as exc:
            raise ClinicalInputError(
                "임상 action JSON Schema가 올바르지 않습니다: "
                f"{exc}"
            ) from exc
        self.action_validator = Draft202012Validator(
            action_schema,
            format_checker=FormatChecker(),
        )
        manifest_schema = _load_json_object(
            CLINICAL_MANIFEST_SCHEMA_PATH
        )
        try:
            Draft202012Validator.check_schema(manifest_schema)
        except Exception as exc:
            raise ClinicalInputError(
                "임상 manifest JSON Schema가 올바르지 않습니다: "
                f"{exc}"
            ) from exc
        self.manifest_validator = Draft202012Validator(
            manifest_schema,
            format_checker=FormatChecker(),
        )
        self.timestamps: list[float] | None = None
        self.gaps: list[dict[str, Any]] = []
        self._validate_source_timeline()
        self.lock = threading.RLock()

        self.manifest: dict[str, Any] | None = None
        self.manifest_sha256: str | None = None
        self.candidates_path = (
            self.case_dir / "clinical_candidates.codex_5_6_sol.v2.jsonl"
        )
        self.actions_path = self.case_dir / "clinical_review_actions.v2.jsonl"
        self.reference_path = (
            self.case_dir / "clinical_reference.final.v2.jsonl"
        )
        self.expected_candidate_source_sha256: str | None = None
        self.expected_candidate_count: int | None = None
        self._load_manifest_snapshot()

    @property
    def available(self) -> bool:
        return self.manifest is not None

    @property
    def candidate_source_status(self) -> str:
        if not self.available:
            return "missing"
        if not self.candidates_path.is_file():
            return "invalid"
        if self.candidates_path.stat().st_size == 0:
            return "empty"
        return "ready"

    def _validate_source_timeline(self) -> None:
        if self.source_timeline is None:
            return
        timeline_case_id = self.source_timeline.get("case_id")
        if timeline_case_id != self.case_id:
            raise ClinicalInputError("임상 source timeline case_id가 다릅니다.")
        raw_timestamps = self.source_timeline.get("timestamps_sec")
        if not isinstance(raw_timestamps, list) or not raw_timestamps:
            raise ClinicalInputError(
                "임상 source timeline timestamps_sec가 비어 있습니다."
            )
        timestamps: list[float] = []
        for index, value in enumerate(raw_timestamps):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ClinicalInputError(
                    f"임상 source timeline frame {index} 시각이 숫자가 아닙니다."
                )
            timestamp = float(value)
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ClinicalInputError(
                    f"임상 source timeline frame {index} 시각이 잘못됐습니다."
                )
            timestamps.append(timestamp)
        if any(
            right <= left
            for left, right in zip(timestamps, timestamps[1:])
        ):
            raise ClinicalInputError(
                "임상 source timeline이 엄격한 오름차순이 아닙니다."
            )
        if self.source_timeline.get("frame_count") != len(timestamps):
            raise ClinicalInputError(
                "임상 source timeline frame_count가 timestamps와 다릅니다."
            )
        raw_gaps = self.source_timeline.get("gaps", [])
        if not isinstance(raw_gaps, list):
            raise ClinicalInputError("임상 source timeline gaps가 배열이 아닙니다.")
        gaps: list[dict[str, Any]] = []
        for index, value in enumerate(raw_gaps, 1):
            if not isinstance(value, dict):
                raise ClinicalInputError(
                    f"임상 source timeline gap {index}가 객체가 아닙니다."
                )
            before = value.get("before_frame_idx")
            after = value.get("after_frame_idx")
            if (
                isinstance(before, bool)
                or not isinstance(before, int)
                or isinstance(after, bool)
                or not isinstance(after, int)
                or before < 0
                or after != before + 1
                or after >= len(timestamps)
            ):
                raise ClinicalInputError(
                    f"임상 source timeline gap {index} 범위가 잘못됐습니다."
                )
            gaps.append(dict(value))
        self.timestamps = timestamps
        self.gaps = gaps

    def _manifest_local_file(
        self,
        value: Any,
        *,
        expected_name: str | None,
        label: str,
    ) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ClinicalInputError(f"임상 manifest {label} 경로가 없습니다.")
        relative = Path(value)
        if (
            relative.is_absolute()
            or len(relative.parts) != 1
            or relative.name != value
        ):
            raise ClinicalInputError(
                f"임상 manifest {label}은 case-local 파일이어야 합니다."
            )
        if expected_name is not None and value != expected_name:
            raise ClinicalInputError(
                f"임상 manifest {label} 파일명이 올바르지 않습니다."
            )
        resolved = (self.case_dir / relative).resolve()
        if resolved.parent != self.case_dir:
            raise ClinicalInputError(
                f"임상 manifest {label} 경로가 case를 벗어났습니다."
            )
        return resolved

    def _load_manifest_snapshot(self) -> None:
        if not self.manifest_path.exists():
            if (
                self.candidates_path.exists()
                or self.actions_path.exists()
                or self.reference_path.exists()
            ):
                raise ClinicalInputError(
                    "임상 데이터 파일은 있지만 clinical manifest가 없습니다."
                )
            return
        manifest = _load_json_object(self.manifest_path)
        self._validate_schema(
            self.manifest_validator,
            manifest,
            location=str(self.manifest_path),
            label="manifest",
        )
        if manifest.get("schema") != CLINICAL_MANIFEST_SCHEMA:
            raise ClinicalInputError("지원하지 않는 clinical manifest schema입니다.")
        if manifest.get("case_id") != self.case_id:
            raise ClinicalInputError("clinical manifest case_id가 다릅니다.")

        candidate_file = manifest.get("candidate_file")
        if candidate_file is None:
            artifacts = manifest.get("artifacts")
            candidate_descriptor = (
                artifacts.get("candidates")
                if isinstance(artifacts, dict)
                else None
            )
            if isinstance(candidate_descriptor, dict):
                candidate_file = candidate_descriptor.get("file")
                candidate_sha256 = candidate_descriptor.get("sha256")
                candidate_count = candidate_descriptor.get("record_count")
            else:
                candidate_sha256 = None
                candidate_count = None
        else:
            candidate_sha256 = manifest.get("candidate_sha256")
            candidate_count = manifest.get("candidate_count")

        candidates_path = self._manifest_local_file(
            candidate_file,
            expected_name="clinical_candidates.codex_5_6_sol.v2.jsonl",
            label="candidate",
        )
        if not candidates_path.is_file():
            raise ClinicalInputError(
                f"임상 candidate 파일이 없습니다: {candidates_path}"
            )
        if not isinstance(candidate_sha256, str) or not SHA256_PATTERN.fullmatch(
            candidate_sha256
        ):
            raise ClinicalInputError("clinical manifest candidate SHA-256이 잘못됐습니다.")
        actual_candidate_sha256 = sha256_file(candidates_path)
        if actual_candidate_sha256 != candidate_sha256:
            raise ClinicalInputError(
                "clinical manifest와 candidate 파일 SHA-256이 다릅니다."
            )
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < 1
        ):
            raise ClinicalInputError(
                "clinical manifest candidate_count가 올바르지 않습니다."
            )

        actions_file = manifest.get(
            "review_actions_file",
            "clinical_review_actions.v2.jsonl",
        )
        reference_file = manifest.get(
            "final_reference_file",
            "clinical_reference.final.v2.jsonl",
        )
        actions_path = self._manifest_local_file(
            actions_file,
            expected_name="clinical_review_actions.v2.jsonl",
            label="review action",
        )
        reference_path = self._manifest_local_file(
            reference_file,
            expected_name="clinical_reference.final.v2.jsonl",
            label="final reference",
        )
        authority = manifest.get("authority")
        if authority is not None and authority != "ai_draft":
            raise ClinicalInputError(
                "clinical manifest authority는 ai_draft여야 합니다."
            )
        source_timeline_descriptor = manifest.get("source_timeline")
        if source_timeline_descriptor is not None:
            if (
                not isinstance(source_timeline_descriptor, dict)
                or not isinstance(
                    source_timeline_descriptor.get("file"),
                    str,
                )
                or not str(source_timeline_descriptor["file"]).strip()
                or not isinstance(
                    source_timeline_descriptor.get("sha256"),
                    str,
                )
                or not SHA256_PATTERN.fullmatch(
                    str(source_timeline_descriptor["sha256"])
                )
            ):
                raise ClinicalInputError(
                    "clinical manifest source_timeline descriptor가 "
                    "올바르지 않습니다."
                )
            if self.source_timeline_path is None:
                raise ClinicalInputError(
                    "clinical manifest source_timeline을 검증할 경로가 없습니다."
                )
            declared_timeline_path = (
                self.case_dir / source_timeline_descriptor["file"]
            ).resolve()
            if declared_timeline_path != self.source_timeline_path:
                raise ClinicalInputError(
                    "clinical manifest source_timeline 경로가 canonical "
                    "timeline과 다릅니다."
                )
            if (
                not self.source_timeline_path.is_file()
                or sha256_file(self.source_timeline_path)
                != source_timeline_descriptor["sha256"]
            ):
                raise ClinicalInputError(
                    "clinical manifest source_timeline SHA-256이 canonical "
                    "timeline과 다릅니다."
                )
        context_sources = manifest.get("context_sources", [])
        if not isinstance(context_sources, list):
            raise ClinicalInputError(
                "clinical manifest context_sources가 배열이 아닙니다."
            )
        for index, descriptor in enumerate(context_sources, 1):
            if not isinstance(descriptor, dict):
                raise ClinicalInputError(
                    f"clinical manifest context source {index}가 객체가 아닙니다."
                )
            expected_sha256 = descriptor.get("sha256")
            if expected_sha256 is None:
                continue
            source_file = descriptor.get("file")
            if not isinstance(source_file, str) or not source_file.strip():
                raise ClinicalInputError(
                    f"clinical manifest context source {index} 경로가 없습니다."
                )
            relative_source_path = Path(source_file)
            if relative_source_path.is_absolute():
                raise ClinicalInputError(
                    f"clinical manifest context source {index}는 상대 "
                    "경로여야 합니다."
                )
            declared_source_path = self.case_dir / relative_source_path
            resolved_source_path = declared_source_path.resolve()
            if (
                not resolved_source_path.is_file()
                or declared_source_path.is_symlink()
                or sha256_file(resolved_source_path) != expected_sha256
            ):
                raise ClinicalInputError(
                    f"clinical manifest context source {index} SHA-256이 "
                    "실제 파일과 다릅니다."
                )

        self.manifest = manifest
        self.manifest_sha256 = sha256_file(self.manifest_path)
        self.candidates_path = candidates_path
        self.actions_path = actions_path
        self.reference_path = reference_path
        self.expected_candidate_source_sha256 = candidate_sha256
        self.expected_candidate_count = candidate_count

    @staticmethod
    def _finite_number(value: Any, *, location: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ClinicalInputError(f"{location}: 숫자가 필요합니다.")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ClinicalInputError(f"{location}: 0 이상의 유한한 값이 필요합니다.")
        return result

    @staticmethod
    def _narrative(value: Any, *, location: str) -> str:
        if not isinstance(value, str):
            raise ClinicalInputError(f"{location}: 문자열이 필요합니다.")
        normalized = " ".join(value.split())
        if not normalized:
            raise ClinicalInputError(f"{location}: 한 문장 이상 입력해야 합니다.")
        if len(normalized) > 600:
            raise ClinicalInputError(f"{location}: 600자를 넘을 수 없습니다.")
        sentence_ends = list(
            CLINICAL_SENTENCE_END_PATTERN.finditer(normalized)
        )
        if (
            not 1 <= len(sentence_ends) <= 2
            or sentence_ends[-1].end() != len(normalized)
        ):
            raise ClinicalInputError(
                f"{location}: 마침표, 물음표 또는 느낌표로 끝나는 "
                "1~2문장이어야 합니다."
            )
        return normalized

    def _canonical_frame_time(
        self,
        frame_value: Any,
        time_value: Any,
        *,
        location: str,
    ) -> tuple[int, float]:
        if isinstance(frame_value, bool) or not isinstance(frame_value, int):
            raise ClinicalInputError(f"{location}: source frame index가 정수가 아닙니다.")
        if frame_value < 0:
            raise ClinicalInputError(f"{location}: source frame index가 음수입니다.")
        time_sec = self._finite_number(time_value, location=f"{location} time")
        if self.timestamps is not None:
            if frame_value >= len(self.timestamps):
                raise ClinicalInputError(
                    f"{location}: source frame index가 timeline 범위 밖입니다."
                )
            canonical_time = self.timestamps[frame_value]
            if abs(time_sec - canonical_time) > 5e-10:
                raise ClinicalInputError(
                    f"{location}: timestamp가 canonical timeline과 다릅니다."
                )
            time_sec = canonical_time
        return frame_value, time_sec

    @staticmethod
    def _validate_aware_datetime(value: str, *, location: str) -> None:
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError as exc:
            raise ClinicalInputError(
                f"{location}: ISO 8601 date-time이 올바르지 않습니다."
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ClinicalInputError(
                f"{location}: timezone이 포함된 date-time이 필요합니다."
            )

    @staticmethod
    def _validate_schema(
        validator: Draft202012Validator,
        record: Mapping[str, Any],
        *,
        location: str,
        label: str,
    ) -> None:
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                error.message,
            ),
        )
        if not errors:
            return
        first = errors[0]
        field_path = ".".join(str(part) for part in first.absolute_path)
        field_label = field_path or "$"
        raise ClinicalInputError(
            f"{location}: {label} JSON Schema 위반 "
            f"({field_label}): {first.message}"
        )

    def _validated_annotation(
        self,
        source: Any,
        *,
        location: str,
        candidate: bool,
        review_status: str | None = None,
        original_candidate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(source, dict):
            raise ClinicalInputError(f"{location}: annotation 객체가 필요합니다.")
        _reject_non_finite_json(source, location=location)
        try:
            record = json.loads(canonical_json(source))
        except (TypeError, ValueError) as exc:
            raise ClinicalInputError(
                f"{location}: 표준 JSON으로 정규화할 수 없습니다."
            ) from exc
        record.pop("_clinical_review", None)
        record.pop("_review_ui", None)
        record.pop("clinical_review", None)
        if record.get("schema") != CLINICAL_ANNOTATION_SCHEMA:
            raise ClinicalInputError(
                f"{location}: 지원하지 않는 clinical annotation schema입니다."
            )
        if record.get("case_id") != self.case_id:
            raise ClinicalInputError(f"{location}: case_id가 다릅니다.")
        annotation_id = record.get("annotation_id")
        if (
            not isinstance(annotation_id, str)
            or not re.fullmatch(
                rf"{re.escape(self.case_id)}-CLV2-[0-9]{{4,}}",
                annotation_id,
            )
        ):
            raise ClinicalInputError(
                f"{location}: annotation_id가 case_id와 일치하지 않습니다."
            )
        anchor_idx, anchor_sec = self._canonical_frame_time(
            record.get("anchor_source_frame_idx"),
            record.get("anchor_sec"),
            location=f"{location} anchor",
        )
        evidence_start_idx, evidence_start_sec = self._canonical_frame_time(
            record.get("evidence_start_source_frame_idx"),
            record.get("evidence_start_sec"),
            location=f"{location} evidence start",
        )
        evidence_end_idx, evidence_end_sec = self._canonical_frame_time(
            record.get("evidence_end_source_frame_idx"),
            record.get("evidence_end_sec"),
            location=f"{location} evidence end",
        )
        if not evidence_start_idx <= anchor_idx <= evidence_end_idx:
            raise ClinicalInputError(
                f"{location}: evidence start ≤ anchor ≤ end가 아닙니다."
            )
        if not evidence_start_sec <= anchor_sec <= evidence_end_sec:
            raise ClinicalInputError(
                f"{location}: evidence 시간 순서가 올바르지 않습니다."
            )
        for gap in self.gaps:
            if (
                evidence_start_idx <= int(gap["before_frame_idx"])
                and evidence_end_idx >= int(gap["after_frame_idx"])
            ):
                raise ClinicalInputError(
                    f"{location}: evidence window가 영상 gap을 가로지릅니다."
                )
        record["anchor_source_frame_idx"] = anchor_idx
        record["anchor_sec"] = anchor_sec
        record["evidence_start_source_frame_idx"] = evidence_start_idx
        record["evidence_start_sec"] = evidence_start_sec
        record["evidence_end_source_frame_idx"] = evidence_end_idx
        record["evidence_end_sec"] = evidence_end_sec

        for narrative_field in CLINICAL_NARRATIVE_FIELDS:
            record[narrative_field] = self._narrative(
                record.get(narrative_field),
                location=f"{location}: {narrative_field}",
            )

        supersedes = record.get("supersedes_annotation_ids")
        if supersedes is not None and (
            not isinstance(supersedes, list)
            or not supersedes
            or any(
                not isinstance(annotation_id, str)
                or not re.fullmatch(
                    rf"{re.escape(self.case_id)}-CL[0-9]{{4,}}",
                    annotation_id,
                )
                for annotation_id in supersedes
            )
            or len(set(supersedes)) != len(supersedes)
        ):
            raise ClinicalInputError(
                f"{location}: supersedes_annotation_ids는 현재 case의 "
                "중복 없는 v1 annotation ID 배열이어야 합니다."
            )

        source_views = record.get("source_views")
        if (
            not isinstance(source_views, list)
            or not source_views
            or any(view not in CLINICAL_SOURCE_VIEWS for view in source_views)
            or len(set(source_views)) != len(source_views)
        ):
            raise ClinicalInputError(
                f"{location}: source_views에는 지원되는 view가 중복 없이 "
                "하나 이상 포함돼야 합니다."
            )
        record["source_views"] = list(source_views)

        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise ClinicalInputError(f"{location}: provenance 객체가 필요합니다.")
        for field in ("generator", "model", "generated_at", "authority"):
            if not isinstance(provenance.get(field), str) or not str(
                provenance[field]
            ).strip():
                raise ClinicalInputError(
                    f"{location}: provenance.{field}가 없습니다."
                )
        if provenance.get("authority") not in CLINICAL_CANDIDATE_AUTHORITIES:
            raise ClinicalInputError(
                f"{location}: provenance.authority는 ai_draft여야 합니다."
            )
        source_annotation_ids = provenance.get("source_annotation_ids")
        if source_annotation_ids is not None and (
            not isinstance(source_annotation_ids, list)
            or not source_annotation_ids
            or any(
                not isinstance(source_annotation_id, str)
                or not re.fullmatch(
                    rf"{re.escape(self.case_id)}-CL[0-9]{{4,}}",
                    source_annotation_id,
                )
                for source_annotation_id in source_annotation_ids
            )
            or len(set(source_annotation_ids)) != len(source_annotation_ids)
        ):
            raise ClinicalInputError(
                f"{location}: provenance.source_annotation_ids는 현재 case의 "
                "중복 없는 v1 annotation ID 배열이어야 합니다."
            )
        self._validate_aware_datetime(
            str(provenance["generated_at"]),
            location=f"{location}: provenance.generated_at",
        )

        expected_status = (
            CLINICAL_CANDIDATE_REVIEW_STATUS
            if candidate
            else review_status
        )
        if expected_status is None:
            raise ClinicalInputError(f"{location}: review status가 없습니다.")
        if candidate and record.get("review_status") != expected_status:
            raise ClinicalInputError(
                f"{location}: AI candidate는 needs_surgeon_review여야 합니다."
            )
        if not candidate:
            record["review_status"] = expected_status

        self._validate_schema(
            self.annotation_validator,
            record,
            location=location,
            label="annotation",
        )

        if original_candidate is not None:
            for immutable_field in (
                "schema",
                "annotation_id",
                "case_id",
                "anchor_source_frame_idx",
                "anchor_sec",
                "evidence_start_source_frame_idx",
                "evidence_end_source_frame_idx",
                "evidence_start_sec",
                "evidence_end_sec",
                "confidence",
                "source_views",
                "supersedes_annotation_ids",
                "provenance",
            ):
                if record.get(immutable_field) != original_candidate.get(
                    immutable_field
                ):
                    raise ClinicalInputError(
                        f"{location}: {immutable_field}는 AI candidate에서 "
                        "변경할 수 없습니다."
                    )
        return record

    def candidates(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        assert self.manifest_sha256 is not None
        if (
            not self.manifest_path.is_file()
            or self.manifest_path.is_symlink()
            or sha256_file(self.manifest_path) != self.manifest_sha256
        ):
            raise ClinicalConflictError(
                "clinical manifest가 server snapshot 이후 변경되었습니다."
            )
        assert self.expected_candidate_source_sha256 is not None
        if (
            not self.candidates_path.is_file()
            or self.candidates_path.is_symlink()
        ):
            raise ClinicalInputError("임상 candidate 파일이 사라졌습니다.")
        actual_sha256 = sha256_file(self.candidates_path)
        if actual_sha256 != self.expected_candidate_source_sha256:
            raise ClinicalConflictError(
                "임상 candidate 파일이 manifest snapshot 이후 변경되었습니다."
            )
        records = _load_jsonl(self.candidates_path)
        if len(records) != self.expected_candidate_count:
            raise ClinicalInputError(
                "임상 candidate record 수가 manifest와 다릅니다."
            )
        clean_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_key: tuple[int, str] | None = None
        for index, source in enumerate(records, 1):
            record = self._validated_annotation(
                source,
                location=f"{self.candidates_path}:{index}",
                candidate=True,
            )
            if source != record:
                raise ClinicalInputError(
                    f"{self.candidates_path}:{index}: candidate가 canonical "
                    "형식이 아닙니다."
                )
            annotation_id = str(record["annotation_id"])
            if annotation_id in seen_ids:
                raise ClinicalInputError(
                    f"{self.candidates_path}:{index}: annotation_id 중복"
                )
            seen_ids.add(annotation_id)
            sort_key = (
                int(record["anchor_source_frame_idx"]),
                annotation_id,
            )
            if previous_key is not None and sort_key < previous_key:
                raise ClinicalInputError(
                    "임상 candidate가 canonical 시간 순서가 아닙니다."
                )
            previous_key = sort_key
            clean_records.append(record)
        return clean_records

    @staticmethod
    def _semantic_request(
        *,
        annotation_id: str,
        candidate_sha256: str,
        supersedes_action_id: str | None,
        client_request_id: str | None,
        review_status: str,
        reviewer_id: str,
        reviewer_role: str,
        notes: str,
        adjudicated_annotation: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "annotation_id": annotation_id,
            "candidate_sha256": candidate_sha256,
            "supersedes_action_id": supersedes_action_id,
            "client_request_id": client_request_id,
            "review_status": review_status,
            "reviewer_id": reviewer_id,
            "reviewer_role": reviewer_role,
            "notes": notes,
            "adjudicated_annotation": dict(adjudicated_annotation),
        }

    def actions(
        self,
        *,
        candidates: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.available:
            if self.actions_path.exists():
                raise ClinicalInputError(
                    "임상 manifest 없이 review action이 존재합니다."
                )
            return []
        candidate_records = list(candidates) if candidates is not None else self.candidates()
        candidates_by_id = {
            str(candidate["annotation_id"]): candidate
            for candidate in candidate_records
        }
        candidate_hashes = {
            annotation_id: sha256_value(candidate)
            for annotation_id, candidate in candidates_by_id.items()
        }
        if self.actions_path.is_symlink():
            raise ClinicalInputError(
                "clinical review action log는 symlink일 수 없습니다."
            )
        records = _load_jsonl(self.actions_path, missing_ok=True)
        seen_action_ids: set[str] = set()
        seen_client_request_ids: set[str] = set()
        active_by_annotation: dict[str, str] = {}
        clean_records: list[dict[str, Any]] = []
        for index, source in enumerate(records, 1):
            location = f"{self.actions_path}:{index}"
            record = json.loads(canonical_json(source))
            self._validate_schema(
                self.action_validator,
                record,
                location=location,
                label="action",
            )
            if record.get("schema") != CLINICAL_REVIEW_ACTION_SCHEMA:
                raise ClinicalInputError(
                    f"{location}: 지원하지 않는 clinical action schema"
                )
            if record.get("case_id") != self.case_id:
                raise ClinicalInputError(f"{location}: case_id 불일치")
            action_id = record.get("action_id")
            expected_action_id = f"{self.case_id}-CLH{index:04d}"
            if action_id != expected_action_id or action_id in seen_action_ids:
                raise ClinicalInputError(
                    f"{location}: action_id가 순차적이지 않거나 중복입니다."
                )
            seen_action_ids.add(action_id)
            annotation_id = record.get("annotation_id")
            candidate = candidates_by_id.get(str(annotation_id))
            if candidate is None:
                raise ClinicalInputError(
                    f"{location}: 알 수 없는 annotation_id입니다."
                )
            expected_candidate_sha256 = candidate_hashes[str(annotation_id)]
            if record.get("candidate_sha256") != expected_candidate_sha256:
                raise ClinicalInputError(
                    f"{location}: candidate SHA-256이 다릅니다."
                )
            review_status = record.get("review_status")
            if review_status not in CLINICAL_REVIEW_STATUSES:
                raise ClinicalInputError(
                    f"{location}: review_status가 올바르지 않습니다."
                )
            supersedes = record.get("supersedes_action_id")
            if supersedes is not None and (
                not isinstance(supersedes, str) or not supersedes
            ):
                raise ClinicalInputError(
                    f"{location}: supersedes_action_id가 올바르지 않습니다."
                )
            if supersedes != active_by_annotation.get(str(annotation_id)):
                raise ClinicalInputError(
                    f"{location}: supersedes_action_id가 현재 판정과 다릅니다."
                )

            client_request_id = record.get("client_request_id")
            if client_request_id is not None:
                if (
                    not isinstance(client_request_id, str)
                    or not client_request_id.strip()
                    or client_request_id in seen_client_request_ids
                ):
                    raise ClinicalInputError(
                        f"{location}: client_request_id가 없거나 중복입니다."
                    )
                seen_client_request_ids.add(client_request_id)

            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "human"
                or not isinstance(review.get("reviewer_id"), str)
                or not str(review["reviewer_id"]).strip()
                or review.get("reviewer_role")
                not in ("clinical_reviewer", "clinician", "surgeon")
                or not isinstance(review.get("reviewed_at"), str)
                or not str(review["reviewed_at"]).strip()
                or not isinstance(review.get("notes"), str)
            ):
                raise ClinicalInputError(
                    f"{location}: human clinical review provenance가 잘못됐습니다."
                )
            self._validate_aware_datetime(
                str(review["reviewed_at"]),
                location=f"{location}: review.reviewed_at",
            )
            if record.get("resulting_authority") != CLINICAL_RESULT_AUTHORITY:
                raise ClinicalInputError(
                    f"{location}: resulting authority가 잘못됐습니다."
                )
            adjudicated = self._validated_annotation(
                record.get("adjudicated_annotation"),
                location=f"{location} adjudicated_annotation",
                candidate=False,
                review_status=str(review_status),
                original_candidate=candidate,
            )
            if record.get("adjudicated_annotation") != adjudicated:
                raise ClinicalInputError(
                    f"{location}: adjudicated annotation이 canonical하지 않습니다."
                )
            semantic = self._semantic_request(
                annotation_id=str(annotation_id),
                candidate_sha256=expected_candidate_sha256,
                supersedes_action_id=supersedes,
                client_request_id=client_request_id,
                review_status=str(review_status),
                reviewer_id=str(review["reviewer_id"]).strip(),
                reviewer_role=str(review["reviewer_role"]),
                notes=str(review["notes"]).strip(),
                adjudicated_annotation=adjudicated,
            )
            if record.get("request_sha256") != sha256_value(semantic):
                raise ClinicalInputError(
                    f"{location}: request SHA-256이 다릅니다."
                )
            active_by_annotation[str(annotation_id)] = str(action_id)
            clean_records.append(record)
        return clean_records

    def _resolved_actions(
        self,
        *,
        candidates: Sequence[dict[str, Any]],
        actions: Sequence[dict[str, Any]],
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, list[dict[str, Any]]],
    ]:
        effective: dict[str, dict[str, Any]] = {}
        history = {
            str(candidate["annotation_id"]): []
            for candidate in candidates
        }
        for action in actions:
            annotation_id = str(action["annotation_id"])
            history[annotation_id].append(action)
            effective[annotation_id] = action
        return effective, history

    def revision(
        self,
        *,
        actions: Sequence[dict[str, Any]] | None = None,
    ) -> str:
        action_records = list(actions) if actions is not None else self.actions()
        return sha256_value(
            {
                "schema": CLINICAL_REVIEW_STATE_SCHEMA,
                "case_id": self.case_id,
                "manifest_sha256": self.manifest_sha256,
                "candidate_source_sha256": (
                    self.expected_candidate_source_sha256
                ),
                "actions": action_records,
            }
        )

    @staticmethod
    def _derived_records(
        *,
        candidates: Sequence[dict[str, Any]],
        effective: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for candidate in candidates:
            annotation_id = str(candidate["annotation_id"])
            action = effective.get(annotation_id)
            if action is None or action["review_status"] == "rejected":
                continue
            record = json.loads(
                canonical_json(action["adjudicated_annotation"])
            )
            record["clinical_review"] = {
                "action_id": action["action_id"],
                "candidate_sha256": action["candidate_sha256"],
                "review_status": action["review_status"],
                "resulting_authority": action["resulting_authority"],
                **action["review"],
            }
            records.append(record)
        return records

    @staticmethod
    def _reference_bytes(records: Sequence[dict[str, Any]]) -> bytes:
        return "".join(canonical_json(record) + "\n" for record in records).encode(
            "utf-8"
        )

    def _reference_state(
        self,
        *,
        candidates: Sequence[dict[str, Any]],
        effective: Mapping[str, dict[str, Any]],
    ) -> dict[str, Any]:
        complete = bool(candidates) and len(effective) == len(candidates)
        records = self._derived_records(
            candidates=candidates,
            effective=effective,
        )
        expected_bytes = self._reference_bytes(records)
        expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
        materialized = self.reference_path.is_file()
        if self.reference_path.is_symlink():
            raise ClinicalInputError(
                "clinical final reference는 symlink일 수 없습니다."
            )
        if materialized:
            actual = self.reference_path.read_bytes()
            if not complete:
                raise ClinicalInputError(
                    "검수 미완료 상태에 stale clinical final reference가 있습니다."
                )
            if actual != expected_bytes:
                raise ClinicalInputError(
                    "clinical final reference가 append-only action에서 파생된 "
                    "내용과 다릅니다."
                )
        return {
            "schema": CLINICAL_REFERENCE_SCHEMA,
            "ready": complete and materialized,
            "review_complete": complete,
            "materialized": materialized,
            "file": self.reference_path.name,
            "record_count": len(records),
            "sha256": expected_sha256 if complete else None,
            "records": records if complete else [],
            "preview_records": records,
            "excludes_rejected": True,
            "authority": CLINICAL_RESULT_AUTHORITY,
        }

    def state(self) -> dict[str, Any]:
        if not self.available:
            return {
                "ok": True,
                "schema": CLINICAL_REVIEW_STATE_SCHEMA,
                "case_id": self.case_id,
                "available": False,
                "revision": self.revision(actions=[]),
                "manifest": None,
                "manifest_sha256": None,
                "candidate_source": {
                    "status": "missing",
                    "file": self.candidates_path.name,
                    "sha256": None,
                    "record_count": 0,
                },
                "candidate_source_sha256": None,
                "candidates": [],
                "review_actions": [],
                "effective_reviews": {},
                "action_history": {},
                "progress": {
                    "total": 0,
                    "reviewed": 0,
                    "unreviewed": 0,
                    "confirmed": 0,
                    "ambiguous": 0,
                    "rejected": 0,
                    "percent": 0.0,
                },
                "reference": {
                    "schema": CLINICAL_REFERENCE_SCHEMA,
                    "ready": False,
                    "review_complete": False,
                    "materialized": False,
                    "file": self.reference_path.name,
                    "record_count": 0,
                    "sha256": None,
                    "records": [],
                    "preview_records": [],
                    "excludes_rejected": True,
                    "authority": CLINICAL_RESULT_AUTHORITY,
                },
                "policy": self._policy(write_api_enabled=False),
            }

        with self.lock:
            candidates = self.candidates()
            actions = self.actions(candidates=candidates)
            effective, history = self._resolved_actions(
                candidates=candidates,
                actions=actions,
            )
            decorated_candidates: list[dict[str, Any]] = []
            for candidate in candidates:
                annotation_id = str(candidate["annotation_id"])
                candidate_sha256 = sha256_value(candidate)
                clean = json.loads(canonical_json(candidate))
                review_meta = {
                    "candidate_sha256": candidate_sha256,
                    "effective_action": effective.get(annotation_id),
                    "action_history": history[annotation_id],
                }
                clean["_clinical_review"] = review_meta
                clean["_review_ui"] = {
                    "candidate_sha256": candidate_sha256,
                    "effective_action_id": (
                        effective[annotation_id]["action_id"]
                        if annotation_id in effective
                        else None
                    ),
                }
                decorated_candidates.append(clean)
            counts = {
                status: sum(
                    action["review_status"] == status
                    for action in effective.values()
                )
                for status in CLINICAL_REVIEW_STATUSES
            }
            reviewed = len(effective)
            total = len(candidates)
            if (
                total
                and reviewed == total
                and not self.reference_path.exists()
                and not self.reference_path.is_symlink()
            ):
                # The append-only action log is authoritative and fsynced before
                # the derived reference is replaced.  Recover the narrow crash
                # window where all actions survived but the derivative did not.
                self._materialize_reference_if_complete()
            reference = self._reference_state(
                candidates=candidates,
                effective=effective,
            )
            return {
                "ok": True,
                "schema": CLINICAL_REVIEW_STATE_SCHEMA,
                "case_id": self.case_id,
                "available": True,
                "revision": self.revision(actions=actions),
                "manifest": self.manifest,
                "manifest_sha256": self.manifest_sha256,
                "candidate_source": {
                    "status": self.candidate_source_status,
                    "file": self.candidates_path.name,
                    "sha256": self.expected_candidate_source_sha256,
                    "record_count": total,
                },
                "candidate_source_sha256": (
                    self.expected_candidate_source_sha256
                ),
                "candidates": decorated_candidates,
                "review_actions": actions,
                "effective_reviews": effective,
                "action_history": history,
                "progress": {
                    "total": total,
                    "reviewed": reviewed,
                    "unreviewed": total - reviewed,
                    **counts,
                    "percent": (
                        round(reviewed * 100.0 / total, 1)
                        if total
                        else 0.0
                    ),
                },
                "reference": reference,
                "policy": self._policy(write_api_enabled=True),
            }

    @staticmethod
    def _policy(*, write_api_enabled: bool) -> dict[str, Any]:
        return {
            "write_api_enabled": write_api_enabled,
            "candidate_files_immutable": True,
            "candidate_sha256_required": True,
            "optimistic_revision_required": True,
            "append_only_actions": True,
            "review_statuses": list(CLINICAL_REVIEW_STATUSES),
            "annotation_schema": CLINICAL_ANNOTATION_SCHEMA,
            "editable_annotation_fields": list(CLINICAL_NARRATIVE_FIELDS),
            "interpretation_requires_human_review": True,
            "annotation_kind_enabled": False,
            "separate_unobservable_type": False,
            "candidate_authority": "ai_draft",
            "confirmed_is_automatic_ground_truth": False,
            "reference_is_separately_derived": True,
        }

    def _materialize_reference_if_complete(self) -> None:
        candidates = self.candidates()
        actions = self.actions(candidates=candidates)
        effective, _history = self._resolved_actions(
            candidates=candidates,
            actions=actions,
        )
        if not candidates or len(effective) != len(candidates):
            return
        records = self._derived_records(
            candidates=candidates,
            effective=effective,
        )
        data = self._reference_bytes(records)
        self.reference_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.reference_path.name}.",
            suffix=".tmp",
            dir=self.reference_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o640)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise ClinicalInputError(
                        "clinical final reference를 전부 기록하지 못했습니다."
                    )
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, self.reference_path)
            directory_descriptor = os.open(
                self.reference_path.parent,
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path.exists():
                temporary_path.unlink()

    def save_action(self, payload: Any) -> dict[str, Any]:
        if not self.available:
            raise ClinicalInputError(
                f"{self.case_id}에는 임상 candidate가 없어 저장할 수 없습니다."
            )
        if not isinstance(payload, dict):
            raise ClinicalInputError("요청 본문은 JSON 객체여야 합니다.")
        if payload.get("case_id") not in (None, self.case_id):
            raise ClinicalInputError("임상 action case_id가 선택한 case와 다릅니다.")
        annotation_id = str(
            payload.get("annotation_id")
            or payload.get("candidate_id")
            or ""
        ).strip()
        if not annotation_id:
            raise ClinicalInputError("검수할 annotation_id가 필요합니다.")
        candidate_sha256 = str(payload.get("candidate_sha256", "")).strip()
        review_status = str(payload.get("review_status", "")).strip()
        if review_status not in CLINICAL_REVIEW_STATUSES:
            raise ClinicalInputError("임상 검토 결과가 올바르지 않습니다.")
        reviewer_id = str(payload.get("reviewer_id", "")).strip()
        if not reviewer_id:
            raise ClinicalInputError("임상 검토자 ID를 입력해 주세요.")
        reviewer_role = str(
            payload.get("reviewer_role", "clinical_reviewer")
        ).strip()
        if reviewer_role not in ("clinical_reviewer", "clinician", "surgeon"):
            raise ClinicalInputError("임상 검토자 역할이 올바르지 않습니다.")
        notes = str(payload.get("notes", "")).strip()
        if len(notes) > 20_000:
            raise ClinicalInputError("임상 검토 메모가 너무 깁니다.")
        expected_revision = str(payload.get("revision", "")).strip()
        supersedes_value = payload.get("supersedes_action_id")
        supersedes_action_id = (
            str(supersedes_value).strip()
            if supersedes_value is not None
            else None
        )
        if supersedes_action_id == "":
            supersedes_action_id = None
        client_value = payload.get("client_request_id")
        client_request_id = (
            str(client_value).strip() if client_value is not None else None
        )
        if client_request_id == "":
            client_request_id = None
        if client_request_id is not None and len(client_request_id) > 200:
            raise ClinicalInputError("client_request_id가 너무 깁니다.")

        self.actions_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with self.lock:
            try:
                descriptor = os.open(self.actions_path, flags, 0o640)
            except OSError as exc:
                raise ClinicalInputError(
                    f"clinical action log를 열 수 없습니다: {exc}"
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                candidates = self.candidates()
                candidates_by_id = {
                    str(candidate["annotation_id"]): candidate
                    for candidate in candidates
                }
                candidate = candidates_by_id.get(annotation_id)
                if candidate is None:
                    raise ClinicalInputError(
                        "선택한 임상 annotation_id가 없습니다."
                    )
                expected_candidate_sha256 = sha256_value(candidate)
                if candidate_sha256 != expected_candidate_sha256:
                    raise ClinicalConflictError(
                        "임상 candidate 내용이 바뀌었습니다. 새로고침 후 "
                        "다시 검토해 주세요."
                    )
                adjudicated_source = payload.get("adjudicated_annotation")
                if adjudicated_source is None:
                    adjudicated_source = candidate
                adjudicated = self._validated_annotation(
                    adjudicated_source,
                    location="임상 action adjudicated_annotation",
                    candidate=False,
                    review_status=review_status,
                    original_candidate=candidate,
                )
                actions = self.actions(candidates=candidates)
                effective, _history = self._resolved_actions(
                    candidates=candidates,
                    actions=actions,
                )
                semantic = self._semantic_request(
                    annotation_id=annotation_id,
                    candidate_sha256=expected_candidate_sha256,
                    supersedes_action_id=supersedes_action_id,
                    client_request_id=client_request_id,
                    review_status=review_status,
                    reviewer_id=reviewer_id,
                    reviewer_role=reviewer_role,
                    notes=notes,
                    adjudicated_annotation=adjudicated,
                )
                request_sha256 = sha256_value(semantic)

                if client_request_id is not None:
                    for existing in actions:
                        if (
                            existing.get("client_request_id")
                            != client_request_id
                        ):
                            continue
                        if existing.get("request_sha256") == request_sha256:
                            self._materialize_reference_if_complete()
                            return {
                                "ok": True,
                                "idempotent": True,
                                "action": existing,
                                "state": self.state(),
                            }
                        raise ClinicalConflictError(
                            "같은 client_request_id에 다른 임상 action이 "
                            "이미 기록되어 있습니다."
                        )
                for existing in actions:
                    if existing.get("request_sha256") == request_sha256:
                        self._materialize_reference_if_complete()
                        return {
                            "ok": True,
                            "idempotent": True,
                            "action": existing,
                            "state": self.state(),
                        }

                current = effective.get(annotation_id)
                current_action_id = (
                    str(current["action_id"]) if current is not None else None
                )
                if supersedes_action_id != current_action_id:
                    raise ClinicalConflictError(
                        "임상 정정 대상이 최신 판정과 다릅니다. 새로고침 후 "
                        "다시 검토해 주세요."
                    )
                if expected_revision != self.revision(actions=actions):
                    raise ClinicalConflictError(
                        "다른 임상 판정이 먼저 추가되었습니다. 새로고침 후 "
                        "다시 검토해 주세요."
                    )

                action = {
                    "schema": CLINICAL_REVIEW_ACTION_SCHEMA,
                    "case_id": self.case_id,
                    "action_id": (
                        f"{self.case_id}-CLH{len(actions) + 1:04d}"
                    ),
                    "annotation_id": annotation_id,
                    "candidate_sha256": expected_candidate_sha256,
                    "supersedes_action_id": supersedes_action_id,
                    "client_request_id": client_request_id,
                    "request_sha256": request_sha256,
                    "review_status": review_status,
                    "resulting_authority": CLINICAL_RESULT_AUTHORITY,
                    "adjudicated_annotation": adjudicated,
                    "review": {
                        "reviewer_kind": "human",
                        "reviewer_id": reviewer_id,
                        "reviewer_role": reviewer_role,
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "notes": notes,
                    },
                }
                data = (canonical_json(action) + "\n").encode("utf-8")
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise ClinicalInputError(
                            "clinical action log에 전체 레코드를 기록하지 "
                            "못했습니다."
                        )
                    offset += written
                os.fsync(descriptor)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

            self._materialize_reference_if_complete()
            return {
                "ok": True,
                "idempotent": False,
                "action": action,
                "state": self.state(),
            }
