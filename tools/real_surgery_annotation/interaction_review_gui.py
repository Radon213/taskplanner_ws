#!/usr/bin/env python3
"""Video-timeline workbench with append-only human review actions.

This workbench is intentionally independent from ``annotation_gui.py`` and the
0704_5 tool-event files. Candidate records and source media are never written.
Legacy decisions remain immutable; corrections and new events are appended to a
separate timeline-action JSONL audit stream.
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import hashlib
import json
import math
import mimetypes
import os
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import yaml

try:
    from .artifact_path_contract import resolve_repo_artifact_identity
    from .clinical_review_store import (
        ClinicalConflictError,
        ClinicalInputError,
        ClinicalReviewStore,
    )
except ImportError:
    from artifact_path_contract import (  # type: ignore[no-redef]
        resolve_repo_artifact_identity,
    )
    from clinical_review_store import (  # type: ignore[no-redef]
        ClinicalConflictError,
        ClinicalInputError,
        ClinicalReviewStore,
    )


CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_TYPES = ("implicit_tool_request", "tool_transfer", "phase_start")
STREAM_EVENT_TYPES = {
    "timeline": EVENT_TYPES,
    "interaction": ("implicit_tool_request", "tool_transfer"),
    "phase": ("phase_start",),
    "request": ("implicit_tool_request",),
    "transfer": ("tool_transfer",),
}
REVIEW_STATUSES = ("confirmed", "ambiguous", "rejected")
LEGACY_TRANSFER_ENDPOINTS = ("mayo_stand", "scrub_nurse", "surgeon")
TRANSFER_ENDPOINTS = (
    *LEGACY_TRANSFER_ENDPOINTS,
    "operative_person_role_unresolved",
)
SOURCE_VIEWS = ("cam1", "cam2", "cam3", "cam4", "flir")
REVIEW_MEDIA_VIEWS = ("cam4", "flir", "cam2", "cam3")
TIMELINE_ACTION_SCHEMA = "taskplanner.timeline_annotation_action.v1"
TIMELINE_ACTION_OPERATIONS = (
    "review_candidate",
    "create_annotation",
    "revise_annotation",
)
EVENT_ID_PREFIXES = {
    "implicit_tool_request": "R",
    "tool_transfer": "T",
    "phase_start": "PH",
}
VIEW_TOPICS = {
    "cam2": "/surgery/cam2/color/image/compressed",
    "cam3": "/surgery/cam3/color/image/compressed",
    "cam4": "/surgery/cam4/color/image/compressed",
    "flir": "/surgery/flir/image/compressed",
}
REVIEW_MODES = ("edit", "final_observed", "final_dt")
FINAL_REVIEW_MODES = frozenset(("final_observed", "final_dt"))
FINAL_EVENT_TYPES = frozenset(("implicit_tool_request", "tool_transfer"))
VOICE_EVENT_SCHEMAS = frozenset(
    (
        "taskplanner.observable_voice_point.v1",
        "taskplanner.observable_voice_point.v2",
    )
)
VOICE_TRACK_AUTHORITY = (
    "source_bag_public_transcript_not_evaluation_ground_truth"
)
VOICE_SCORING_ROLE = "context_only_not_ground_truth"
PHASE_CONTEXT_AUTHORITIES = frozenset(
    (
        "direct_human_review_experimental_not_scoring_reference",
        "direct_human_review_provisional_context_not_scoring_ground_truth",
        (
            "user_authorized_ai_assistant_video_adjudication_"
            "provisional_context_not_scoring_ground_truth"
        ),
    )
)
ASSISTANT_PHASE_CONTEXT_AUTHORITY = (
    "user_authorized_ai_assistant_video_adjudication_"
    "provisional_context_not_scoring_ground_truth"
)
PHASE_CONTEXT_SCORING_ROLE = "context_only_not_ground_truth"
REVIEW_ATTENTION_SCORING_ROLE = "context_only_not_ground_truth"
SOURCE_BAG_COLLECTION_RELOCATIONS = MappingProxyType(
    {
        "0704_멀티모달_ROS2_MCAP_v1.0.0": "0704_rosbag2",
    }
)


class InputError(Exception):
    """A user-editable field or local input artifact is invalid."""


class ConflictError(Exception):
    """The append-only log or optimistic revision no longer matches."""


class FrameError(Exception):
    """An exact source frame cannot be read."""


class CaseNotFoundError(InputError):
    """A syntactically valid case ID is not present in the server catalog."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_path_or_marker(path: Path, marker: bytes) -> bytes:
    if path.is_file():
        data = path.read_bytes()
        return data if data else marker
    return marker


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_source_bag_directory(
    *,
    declared_source_bag: Path,
    case_id: str,
    annotation_manifest: Mapping[str, Any],
) -> Path:
    """Resolve a verified operational bag without rewriting evidence identity.

    The timeline, annotation manifest, and cached review media intentionally
    retain the source path used when their hashes were recorded.  A known
    collection-directory rename may be followed only when the replacement has
    the same case name, MCAP filename, and metadata hash.
    """

    declared = declared_source_bag.resolve()
    source_descriptor = annotation_manifest.get("source_bag")
    if not isinstance(source_descriptor, dict):
        raise InputError("annotation manifest source_bag 정보가 없습니다.")
    manifest_directory = Path(
        str(source_descriptor.get("directory", ""))
    ).resolve()
    if manifest_directory != declared:
        raise InputError("annotation manifest와 timeline의 source bag이 다릅니다.")
    if declared.name != case_id:
        raise InputError(
            "source bag case directory가 timeline case_id와 다릅니다."
        )

    operational = declared
    relocated = False
    if not operational.is_dir():
        collection_directory = declared.parent.parent
        replacement_name = SOURCE_BAG_COLLECTION_RELOCATIONS.get(
            collection_directory.name
        )
        if replacement_name is None or declared.parent.name != "bags":
            raise InputError(f"source bag directory가 없습니다: {declared}")
        operational = (
            collection_directory.parent
            / replacement_name
            / "bags"
            / case_id
        ).resolve()
        relocated = True

    if not operational.is_dir():
        raise InputError(
            "source bag directory와 검증 가능한 이전 위치가 없습니다: "
            f"{declared}"
        )
    if operational.name != case_id:
        raise InputError("이전된 source bag의 case directory가 다릅니다.")

    mcap_name = source_descriptor.get("mcap_file")
    if not isinstance(mcap_name, str) or not mcap_name:
        raise InputError("annotation manifest source MCAP 파일명이 없습니다.")
    mcap_name_path = Path(mcap_name)
    if mcap_name_path.is_absolute() or mcap_name_path.name != mcap_name:
        raise InputError(
            "annotation manifest source MCAP 파일명은 안전한 basename이어야 "
            "합니다."
        )
    expected_mcap_sha256 = source_descriptor.get("mcap_sha256")
    if (
        not isinstance(expected_mcap_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_mcap_sha256) is None
    ):
        raise InputError("annotation manifest MCAP 해시가 올바르지 않습니다.")
    expected_metadata_sha256 = source_descriptor.get("metadata_sha256")
    if (
        not isinstance(expected_metadata_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_metadata_sha256) is None
    ):
        raise InputError("annotation manifest metadata 해시가 올바르지 않습니다.")

    metadata_path = operational / "metadata.yaml"
    mcap_path = (operational / mcap_name_path).resolve()
    if mcap_path.parent != operational.resolve():
        raise InputError("source MCAP 경로가 case directory를 벗어났습니다.")
    if not metadata_path.is_file():
        raise InputError(
            f"이전된 source bag metadata가 없습니다: {metadata_path}"
        )
    if sha256_file(metadata_path) != expected_metadata_sha256:
        raise InputError(
            "이전된 source bag metadata 해시가 annotation manifest와 "
            f"다릅니다: {metadata_path}"
        )
    if not mcap_path.is_file():
        raise InputError(f"이전된 source MCAP 파일이 없습니다: {mcap_path}")
    if mcap_path.stat().st_size <= 0:
        raise InputError(f"이전된 source MCAP 파일이 비어 있습니다: {mcap_path}")
    if relocated and sha256_file(mcap_path) != expected_mcap_sha256:
        raise InputError(
            "이전된 source MCAP 해시가 annotation manifest와 다릅니다: "
            f"{mcap_path}"
        )
    return operational


def parse_single_byte_range(
    value: str | None,
    *,
    size: int,
) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range; return inclusive bounds."""

    if value is None:
        return None
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        raise InputError("지원하지 않는 HTTP Range입니다.")
    spec = value[6:].strip()
    if "-" not in spec:
        raise InputError("HTTP Range 형식이 올바르지 않습니다.")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        try:
            suffix_length = int(end_text)
        except ValueError as exc:
            raise InputError("HTTP suffix Range가 올바르지 않습니다.") from exc
        if suffix_length <= 0:
            raise InputError("HTTP suffix Range가 올바르지 않습니다.")
        start = max(0, size - suffix_length)
        return start, size - 1
    try:
        start = int(start_text)
    except ValueError as exc:
        raise InputError("HTTP Range 시작값이 올바르지 않습니다.") from exc
    if start < 0 or start >= size:
        raise InputError("HTTP Range가 파일 범위 밖입니다.")
    if end_text:
        try:
            end = int(end_text)
        except ValueError as exc:
            raise InputError("HTTP Range 끝값이 올바르지 않습니다.") from exc
        if end < start:
            raise InputError("HTTP Range 순서가 올바르지 않습니다.")
        end = min(end, size - 1)
    else:
        end = size - 1
    return start, end


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{path}: JSON 객체가 필요합니다.")
    return value


def load_jsonl(path: Path, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if missing_ok:
            return []
        raise InputError(f"파일이 없습니다: {path}")
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(
                        f"{path}:{line_number}: JSONL 형식 오류: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise InputError(
                        f"{path}:{line_number}: JSON 객체 레코드가 필요합니다."
                    )
                records.append(value)
    except OSError as exc:
        raise InputError(f"{path}: JSONL을 읽을 수 없습니다: {exc}") from exc
    return records


class FinalReviewBundle:
    """Fail-closed, read-only view of finalized observed and DT references."""

    schema = "taskplanner.final_interaction_review_bundle.v1"

    def __init__(
        self,
        *,
        manifest_path: Path,
        expected_timeline_path: Path | None = None,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        self.case_dir = self.manifest_path.parent
        self.manifest = load_json_object(self.manifest_path)
        if (
            self.manifest.get("schema")
            != "taskplanner.observable_annotation_manifest.v1"
        ):
            raise InputError("지원하지 않는 final manifest schema입니다.")
        self.case_id = str(self.manifest.get("case_id", ""))
        if not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise InputError("final manifest case_id가 올바르지 않습니다.")
        if self.case_dir.name != self.case_id:
            raise InputError(
                "final manifest의 case_id와 case 디렉터리 이름이 다릅니다."
            )

        reference = self.manifest.get("evaluation_reference")
        if not isinstance(reference, dict) or reference.get("complete") is not True:
            raise InputError("완료된 evaluation_reference가 필요합니다.")
        if not isinstance(reference.get("phase_reference_included"), bool):
            raise InputError("phase_reference_included는 boolean이어야 합니다.")
        self.phase_reference_included = bool(
            reference["phase_reference_included"]
        )
        if (
            reference.get("information_boundary")
            != "evaluation_only_never_vlm_reducer_bt_runtime_input"
        ):
            raise InputError("final reference information boundary가 올바르지 않습니다.")
        self.reference = reference

        timeline_info = self.manifest.get("minimal_interaction_annotation")
        if not isinstance(timeline_info, dict):
            raise InputError("minimal_interaction_annotation이 없습니다.")
        timeline_path = self._case_file(
            timeline_info.get("timeline_file"),
            label="timeline",
        )
        if expected_timeline_path is not None and (
            timeline_path != expected_timeline_path.resolve()
        ):
            raise InputError("편집 timeline과 final manifest timeline이 다릅니다.")
        self._require_sha256(
            timeline_path,
            timeline_info.get("timeline_sha256"),
            label="timeline",
        )
        adjudication_descriptor = reference.get("assistant_adjudication")
        if not isinstance(adjudication_descriptor, dict):
            raise InputError("assistant adjudication descriptor가 없습니다.")
        adjudication_path = self._case_file(
            adjudication_descriptor.get("file"),
            label="assistant adjudication",
        )
        adjudication_sha256 = adjudication_descriptor.get("sha256")
        duplicate_adjudication_path = self._case_file(
            timeline_info.get("assistant_adjudication_file"),
            label="minimal assistant adjudication",
        )
        duplicate_adjudication_sha256 = timeline_info.get(
            "assistant_adjudication_sha256"
        )
        if (
            duplicate_adjudication_path != adjudication_path
            or duplicate_adjudication_sha256 != adjudication_sha256
        ):
            raise InputError(
                "assistant adjudication의 중복 manifest 선언이 다릅니다."
            )
        self._require_sha256(
            adjudication_path,
            adjudication_sha256,
            label="assistant adjudication",
        )
        reaudit_descriptor = reference.get("assistant_reaudit")
        if reaudit_descriptor is None:
            reaudit_path: Path | None = None
            reaudit_sha256 = ""
        else:
            if not isinstance(reaudit_descriptor, dict):
                raise InputError("assistant reaudit descriptor가 올바르지 않습니다.")
            reaudit_path = self._case_file(
                reaudit_descriptor.get("file"),
                label="assistant reaudit",
            )
            reaudit_sha256 = str(reaudit_descriptor.get("sha256", ""))
            self._require_sha256(
                reaudit_path,
                reaudit_sha256,
                label="assistant reaudit",
            )
            reaudit_schema_path = self._confined_file(
                self.case_dir,
                reaudit_descriptor.get("schema_file"),
                allowed_root=self.case_dir.parent.parent / "schema",
                label="assistant reaudit schema",
            )
            self._require_sha256(
                reaudit_schema_path,
                reaudit_descriptor.get("schema_sha256"),
                label="assistant reaudit schema",
            )
            if (
                load_json_object(reaudit_schema_path).get("$id")
                != "taskplanner.assistant_annotation_adjudication.v2"
            ):
                raise InputError("assistant reaudit schema ID가 올바르지 않습니다.")
            duplicate_reaudit_file = timeline_info.get(
                "assistant_reaudit_file"
            )
            duplicate_reaudit_sha256 = timeline_info.get(
                "assistant_reaudit_sha256"
            )
            if duplicate_reaudit_file is not None or (
                duplicate_reaudit_sha256 is not None
            ):
                duplicate_reaudit_path = self._case_file(
                    duplicate_reaudit_file,
                    label="minimal assistant reaudit",
                )
                if (
                    duplicate_reaudit_path != reaudit_path
                    or duplicate_reaudit_sha256 != reaudit_sha256
                ):
                    raise InputError(
                        "assistant reaudit의 중복 manifest 선언이 다릅니다."
                    )

        projection_path = self._case_file(
            reference.get("projection_policy_file"),
            label="explicit projection policy",
        )
        projection_sha256 = reference.get("projection_policy_sha256")
        duplicate_projection_file = timeline_info.get(
            "explicit_projection_file"
        )
        legacy_projection_manifest = duplicate_projection_file is None
        if legacy_projection_manifest:
            if projection_path.name not in (
                "dt_projection_policy.v1.json",
                "dt_projection_policy.v2.json",
            ):
                raise InputError(
                    "minimal explicit projection 선언이 없는 legacy "
                    "projection policy만 호환됩니다."
                )
        else:
            duplicate_projection_path = self._case_file(
                duplicate_projection_file,
                label="minimal explicit projection policy",
            )
            duplicate_projection_sha256 = timeline_info.get(
                "explicit_projection_sha256"
            )
            if (
                duplicate_projection_path != projection_path
                or duplicate_projection_sha256 != projection_sha256
            ):
                raise InputError(
                    "explicit projection의 중복 manifest 선언이 다릅니다."
                )
        self._require_sha256(
            projection_path,
            projection_sha256,
            label="explicit projection policy",
        )

        masks_descriptor = reference.get("evaluation_masks")
        if not isinstance(masks_descriptor, dict):
            raise InputError("evaluation masks descriptor가 없습니다.")
        masks_path = self._case_file(
            masks_descriptor.get("file"),
            label="evaluation masks",
        )
        masks_sha256 = masks_descriptor.get("sha256")
        self._require_sha256(
            masks_path,
            masks_sha256,
            label="evaluation masks",
        )

        reconciliation_file = timeline_info.get(
            "policy02_reconciliation_file"
        )
        if reconciliation_file is None:
            if not legacy_projection_manifest:
                raise InputError("Policy02 reconciliation 경로가 없습니다.")
            reconciliation_path: Path | None = None
            reconciliation_sha256 = ""
        else:
            reconciliation_path = self._case_file(
                reconciliation_file,
                label="Policy02 reconciliation",
            )
            reconciliation_sha256 = timeline_info.get(
                "policy02_reconciliation_sha256"
            )
            self._require_sha256(
                reconciliation_path,
                reconciliation_sha256,
                label="Policy02 reconciliation",
            )

        self.timeline_path = timeline_path
        self.adjudication_path = adjudication_path
        self.adjudication_sha256 = str(adjudication_sha256)
        self.reaudit_path = reaudit_path
        self.reaudit_sha256 = reaudit_sha256
        self.projection_policy_path = projection_path
        self.projection_policy_sha256 = str(projection_sha256)
        self.evaluation_masks_path = masks_path
        self.evaluation_masks_sha256 = str(masks_sha256)
        self.reconciliation_path = reconciliation_path
        self.reconciliation_sha256 = str(reconciliation_sha256)
        self.timeline = load_json_object(timeline_path)
        self.timestamps = self._validated_timestamps(self.timeline)
        self.speech_descriptor: dict[str, Any] | None = None
        self.speech_schema_id: str | None = None
        self.speech_events: list[dict[str, Any]] = []
        self._load_speech_context()
        self.phase_descriptor: dict[str, Any] | None = None
        self.phase_catalog: dict[str, Any] | None = None
        self.phase_events: list[dict[str, Any]] = []
        self._load_phase_context()
        if self.phase_reference_included and self.phase_descriptor is None:
            raise InputError(
                "phase reference가 선언됐지만 provisional Phase 문맥이 없습니다."
            )

        observed_info = self._reference_descriptor("observed_reference")
        dt_info = self._reference_descriptor("dt_reference")
        observed_path = self._case_file(
            observed_info.get("file"),
            label="observed reference",
        )
        dt_path = self._case_file(
            dt_info.get("file"),
            label="DT reference",
        )
        self._require_sha256(
            observed_path,
            observed_info.get("sha256"),
            label="observed reference",
        )
        self._require_sha256(
            dt_path,
            dt_info.get("sha256"),
            label="DT reference",
        )

        reports_dir = self.case_dir.parent.parent / "reports"
        report_path = self._confined_file(
            self.case_dir,
            reference.get("projection_report_file"),
            allowed_root=reports_dir,
            label="projection report",
        )
        self._require_sha256(
            report_path,
            reference.get("projection_report_sha256"),
            label="projection report",
        )
        self.observed_path = observed_path
        self.dt_path = dt_path
        self.report_path = report_path
        self.observed = self._validated_layer(
            load_jsonl(observed_path),
            descriptor=observed_info,
            layer="observed",
        )
        self.dt_reference = self._validated_layer(
            load_jsonl(dt_path),
            descriptor=dt_info,
            layer="dt_reference",
        )
        self.report = load_json_object(report_path)
        report_inputs = self.report.get("inputs")
        modern_report_inputs_match = (
            isinstance(report_inputs, dict)
            and report_inputs.get("adjudications")
            == self.adjudication_sha256
            and report_inputs.get("projection")
            == self.projection_policy_sha256
        )
        legacy_report_inputs_match = (
            legacy_projection_manifest
            and isinstance(report_inputs, dict)
            and report_inputs.get("assistant_corrections")
            == self.adjudication_sha256
            and report_inputs.get("projection_policy")
            == self.projection_policy_sha256
            and (
                self.reaudit_path is None
                or report_inputs.get("assistant_reaudit")
                == self.reaudit_sha256
            )
        )
        if not modern_report_inputs_match and not legacy_report_inputs_match:
            raise InputError(
                "projection report의 adjudication/projection 입력 hash가 "
                "manifest와 다릅니다."
            )
        observed_dispositions, dt_dispositions = self._validated_projection()
        self.review_attention = self._validated_review_attention()
        self.observed = self._decorate_events(
            self.observed,
            layer="observed",
            dispositions=observed_dispositions,
        )
        self.dt_reference = self._decorate_events(
            self.dt_reference,
            layer="dt_reference",
            dispositions=dt_dispositions,
        )
        stable_artifacts: list[tuple[Path, str, str]] = [
            (
                self.adjudication_path,
                self.adjudication_sha256,
                "assistant adjudication",
            ),
            (
                self.projection_policy_path,
                self.projection_policy_sha256,
                "explicit projection policy",
            ),
            (
                self.evaluation_masks_path,
                self.evaluation_masks_sha256,
                "evaluation masks",
            ),
        ]
        if self.reconciliation_path is not None:
            stable_artifacts.append(
                (
                self.reconciliation_path,
                self.reconciliation_sha256,
                "Policy02 reconciliation",
                )
            )
        if self.reaudit_path is not None:
            stable_artifacts.append(
                (
                    self.reaudit_path,
                    self.reaudit_sha256,
                    "assistant reaudit",
                )
            )
        for path, digest, label in stable_artifacts:
            self._require_sha256(path, digest, label=label)
        self.revision = hashlib.sha256(
            (
                str(reference.get("adjudication_revision", ""))
                + self.adjudication_sha256
                + self.reaudit_sha256
                + self.projection_policy_sha256
                + self.evaluation_masks_sha256
                + self.reconciliation_sha256
                + str(observed_info["sha256"])
                + str(dt_info["sha256"])
                + str(reference["projection_report_sha256"])
                + (
                    str(self.speech_descriptor["sha256"])
                    if self.speech_descriptor is not None
                    else ""
                )
                + (
                    str(self.phase_descriptor.get("candidate_sha256", ""))
                    + str(
                        self.phase_descriptor.get(
                            "human_decision_sha256",
                            "",
                        )
                    )
                    + str(self.phase_descriptor["procedure_catalog_sha256"])
                    + str(
                        self.phase_descriptor.get(
                            "provisional_reference_sha256",
                            "",
                        )
                    )
                    if self.phase_descriptor is not None
                    else ""
                )
            ).encode("utf-8")
        ).hexdigest()[:16]

    def _load_speech_context(self) -> None:
        descriptor = self.manifest.get("speech_timeline")
        if descriptor is None:
            return
        if not isinstance(descriptor, dict):
            raise InputError("speech_timeline은 객체여야 합니다.")
        if descriptor.get("authority") != VOICE_TRACK_AUTHORITY:
            raise InputError("speech_timeline authority가 올바르지 않습니다.")
        if descriptor.get("scoring_role") != VOICE_SCORING_ROLE:
            raise InputError("speech_timeline scoring_role이 올바르지 않습니다.")
        if descriptor.get("timeline_geometry") != "point_at_source_timestamp":
            raise InputError("speech_timeline geometry가 올바르지 않습니다.")
        if descriptor.get("source_topic") != "/surgery/transcript":
            raise InputError("speech_timeline source_topic이 올바르지 않습니다.")

        speech_path = self._case_file(
            descriptor.get("file"),
            label="speech timeline",
        )
        self._require_sha256(
            speech_path,
            descriptor.get("sha256"),
            label="speech timeline",
        )
        schema_dir = self.case_dir.parent.parent / "schema"
        schema_path = self._confined_file(
            self.case_dir,
            descriptor.get("schema_file"),
            allowed_root=schema_dir,
            label="speech timeline schema",
        )
        self._require_sha256(
            schema_path,
            descriptor.get("schema_sha256"),
            label="speech timeline schema",
        )
        schema_object = load_json_object(schema_path)
        schema_id = str(schema_object.get("$id", ""))
        if schema_id not in VOICE_EVENT_SCHEMAS:
            raise InputError("speech timeline schema ID가 올바르지 않습니다.")
        records = load_jsonl(speech_path)
        if descriptor.get("event_count") != len(records):
            raise InputError("speech timeline manifest event count가 다릅니다.")
        self.speech_events = self._validated_speech_events(
            records,
            schema_id=schema_id,
        )
        self.speech_descriptor = dict(descriptor)
        self.speech_schema_id = schema_id

    def _validated_speech_events(
        self,
        records: list[dict[str, Any]],
        *,
        schema_id: str,
    ) -> list[dict[str, Any]]:
        clean_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_key: tuple[float, int, str] | None = None
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"speech:{index}"
            if "source_frame_idx" in record:
                raise InputError(
                    f"{location}: 음성 원본 시각을 영상 프레임으로 대체할 수 없습니다."
                )
            if record.get("schema") != schema_id:
                raise InputError(f"{location}: schema ID가 올바르지 않습니다.")
            if record.get("case_id") != self.case_id:
                raise InputError(f"{location}: case_id 불일치")
            event_id = str(record.get("event_id", ""))
            if (
                re.fullmatch(
                    rf"{re.escape(self.case_id)}-V[0-9]{{4,}}",
                    event_id,
                )
                is None
                or event_id in seen_ids
            ):
                raise InputError(f"{location}: event_id가 없거나 중복입니다.")
            seen_ids.add(event_id)
            if record.get("event_type") != "voice_utterance":
                raise InputError(f"{location}: voice_utterance가 아닙니다.")
            if record.get("source_topic") != "/surgery/transcript":
                raise InputError(f"{location}: source_topic이 올바르지 않습니다.")
            if record.get("source_authority") != "public_runtime_transcript":
                raise InputError(f"{location}: source_authority가 올바르지 않습니다.")
            if record.get("scoring_role") != VOICE_SCORING_ROLE:
                raise InputError(f"{location}: scoring_role이 올바르지 않습니다.")
            text = record.get("text")
            if not isinstance(text, str) or not text.strip():
                raise InputError(f"{location}: 발화 원문이 비어 있습니다.")
            message_index = record.get("source_message_index")
            if not isinstance(message_index, int) or message_index != index:
                raise InputError(
                    f"{location}: source_message_index가 원본 순서와 다릅니다."
                )
            try:
                time_sec = float(record.get("time_sec"))
                end_sec = float(record.get("end_sec"))
            except (TypeError, ValueError) as exc:
                raise InputError(f"{location}: 시각이 숫자가 아닙니다.") from exc
            if (
                not math.isfinite(time_sec)
                or not math.isfinite(end_sec)
                or time_sec < 0
                or end_sec < time_sec
            ):
                raise InputError(f"{location}: 발화 시각 범위가 올바르지 않습니다.")
            timestamp_ns = record.get("source_record_timestamp_ns")
            if not isinstance(timestamp_ns, int) or timestamp_ns < 0:
                raise InputError(f"{location}: source record timestamp가 잘못됐습니다.")
            source_record_sec = timestamp_ns / 1_000_000_000
            if schema_id == "taskplanner.observable_voice_point.v1":
                if abs(source_record_sec - time_sec) > 5e-10:
                    raise InputError(
                        f"{location}: source record timestamp와 time_sec가 다릅니다."
                    )
                if (
                    "available_sec" in record
                    or "availability_policy" in record
                ):
                    raise InputError(
                        f"{location}: v1 음성에는 availability 필드가 없습니다."
                    )
                available_sec = time_sec
                availability_policy = "legacy_at_source_timestamp"
            else:
                try:
                    available_sec = float(record.get("available_sec"))
                except (TypeError, ValueError) as exc:
                    raise InputError(
                        f"{location}: available_sec가 숫자가 아닙니다."
                    ) from exc
                if (
                    not math.isfinite(available_sec)
                    or available_sec < end_sec
                    or available_sec + 5e-10 < source_record_sec
                    or source_record_sec + 5e-10 < time_sec
                ):
                    raise InputError(
                        f"{location}: complete text availability가 올바르지 않습니다."
                    )
                availability_policy = str(
                    record.get("availability_policy", "")
                )
                if availability_policy != "not_before_utterance_end":
                    raise InputError(
                        f"{location}: availability_policy가 올바르지 않습니다."
                    )
            source_wav = record.get("source_wav")
            if not isinstance(source_wav, str) or not source_wav.strip():
                raise InputError(f"{location}: source_wav가 비어 있습니다.")
            sort_key = (time_sec, message_index, event_id)
            if previous_key is not None and sort_key <= previous_key:
                raise InputError("speech timeline이 원본 시각 순서가 아닙니다.")
            previous_key = sort_key
            nearest = bisect.bisect_left(self.timestamps, time_sec)
            if nearest >= len(self.timestamps):
                nearest = len(self.timestamps) - 1
            elif nearest > 0 and (
                time_sec - self.timestamps[nearest - 1]
                <= self.timestamps[nearest] - time_sec
            ):
                nearest -= 1
            record["_review_ui"] = {
                "read_only": True,
                "timeline_geometry": "point",
                "exact_source_timestamp": True,
                "nearest_source_frame_idx": nearest,
                "complete_text_available_sec": available_sec,
                "availability_policy": availability_policy,
            }
            clean_records.append(record)
        return clean_records

    def _load_phase_context(self) -> None:
        descriptor = self.manifest.get("phase_annotation")
        if descriptor is None:
            return
        if not isinstance(descriptor, dict):
            raise InputError("phase_annotation은 객체여야 합니다.")
        authority = str(descriptor.get("authority", ""))
        if authority not in PHASE_CONTEXT_AUTHORITIES:
            raise InputError("phase_annotation authority가 올바르지 않습니다.")
        if (
            descriptor.get("complete") is not True
            or descriptor.get("review_complete") is not True
            or descriptor.get("scoring_reference_ready") is not False
        ):
            raise InputError(
                "phase_annotation은 검토 완료된 비채점 provisional 문맥이어야 합니다."
            )
        reference_included = descriptor.get(
            "reference_included_in_final_layers"
        )
        if reference_included is not None and reference_included is not True:
            raise InputError(
                "phase provisional reference 포함 상태가 올바르지 않습니다."
            )
        if bool(reference_included) != self.phase_reference_included:
            if reference_included is not None or self.phase_reference_included:
                raise InputError(
                    "evaluation_reference와 phase_annotation의 포함 상태가 다릅니다."
                )

        catalog_path = self._case_file(
            descriptor.get("procedure_catalog_file"),
            label="phase procedure catalog",
        )
        self._require_sha256(
            catalog_path,
            descriptor.get("procedure_catalog_sha256"),
            label="phase procedure catalog",
        )
        self.phase_catalog = self._browser_phase_catalog(catalog_path)
        if (
            descriptor.get("procedure_catalog_runtime_status")
            != "evaluation_only_draft_not_frozen"
        ):
            raise InputError(
                "phase procedure catalog runtime status가 올바르지 않습니다."
            )
        if authority == ASSISTANT_PHASE_CONTEXT_AUTHORITY:
            provisional_path = self._case_file(
                descriptor.get("provisional_reference_file"),
                label="assistant provisional phase reference",
            )
            self._require_sha256(
                provisional_path,
                descriptor.get("provisional_reference_sha256"),
                label="assistant provisional phase reference",
            )
            self.phase_events = (
                self._validated_assistant_provisional_phase_records(
                    load_jsonl(provisional_path),
                    descriptor=descriptor,
                )
            )
            self.phase_descriptor = dict(descriptor)
            return

        candidate_path = self._case_file(
            descriptor.get("candidate_file"),
            label="phase candidate",
        )
        action_path = self._case_file(
            descriptor.get("human_decision_file"),
            label="phase human decision",
        )
        self._require_sha256(
            candidate_path,
            descriptor.get("candidate_sha256"),
            label="phase candidate",
        )
        self._require_sha256(
            action_path,
            descriptor.get("human_decision_sha256"),
            label="phase human decision",
        )
        candidates = self._validated_phase_candidates(
            load_jsonl(candidate_path),
            descriptor=descriptor,
        )
        phase_actions = self._validated_phase_actions(
            load_jsonl(action_path),
            candidates=candidates,
        )
        effective_actions = {
            str(action["annotation_id"]): action for action in phase_actions
        }
        if set(effective_actions) != {
            str(candidate["event_id"]) for candidate in candidates
        }:
            raise InputError(
                "phase review_complete이지만 사람 판정이 없는 후보가 있습니다."
            )

        effective_counts = {
            status: sum(
                action["review_status"] == status
                for action in effective_actions.values()
            )
            for status in REVIEW_STATUSES
        }
        if descriptor.get("effective_review_status_counts") != effective_counts:
            raise InputError(
                "phase_annotation effective_review_status_counts가 다릅니다."
            )

        resolved_events: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_id = str(candidate["event_id"])
            action = effective_actions[candidate_id]
            if action["review_status"] != "ambiguous":
                continue
            fields = action["adjudicated_fields"]
            event = dict(candidate)
            event.update(fields)
            event["review_status"] = "ambiguous"
            event["label_origin"] = None
            event["review"] = action["review"]
            event["_review_ui"] = {
                "candidate_id": candidate_id,
                "candidate_sha256": sha256_value(candidate),
                "effective_decision": action,
                "action_history": [
                    item
                    for item in phase_actions
                    if item["annotation_id"] == candidate_id
                ],
            }
            event["_final_review"] = {
                "layer": "phase_context",
                "read_only": True,
                "context_only": True,
                "provisional": True,
                "scoring_role": PHASE_CONTEXT_SCORING_ROLE,
                "overlay_cue": {"geometry": "interval_from_phase_start"},
            }
            resolved_events.append(event)
        resolved_events = sorted(
            resolved_events,
            key=lambda item: (
                int(item["source_frame_idx"]),
                str(item["event_id"]),
            ),
        )
        provisional_file = descriptor.get("provisional_reference_file")
        provisional_sha256 = descriptor.get("provisional_reference_sha256")
        if provisional_file is None and provisional_sha256 is None:
            self.phase_events = resolved_events
        else:
            provisional_path = self._case_file(
                provisional_file,
                label="provisional phase reference",
            )
            self._require_sha256(
                provisional_path,
                provisional_sha256,
                label="provisional phase reference",
            )
            self.phase_events = self._validated_provisional_phase_records(
                load_jsonl(provisional_path),
                resolved_events=resolved_events,
            )
        self.phase_descriptor = dict(descriptor)

    def _browser_phase_catalog(self, path: Path) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise InputError(
                "phase procedure catalog를 읽을 수 없습니다."
            ) from exc
        if not isinstance(payload, dict):
            raise InputError("phase procedure catalog는 객체여야 합니다.")

        phases: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_phases = payload.get("phases", [])
        if raw_phases is not None and not isinstance(raw_phases, list):
            raise InputError("phase procedure catalog phases는 배열이어야 합니다.")
        observed_key = f"observed_in_{self.case_id}"
        for raw_phase in raw_phases or []:
            if not isinstance(raw_phase, dict):
                continue
            phase_id = str(raw_phase.get("phase_id", "")).strip()
            if (
                re.fullmatch(r"P[0-9]{2,}", phase_id) is None
                or phase_id in seen
            ):
                continue
            seen.add(phase_id)
            phase: dict[str, Any] = {
                "phase_id": phase_id,
                "observed_in_case": bool(raw_phase.get(observed_key, False)),
            }
            for key in (
                "name",
                "name_ko",
                "definition_source",
                "observable_definition",
                "tool_pattern",
                "annotation_note",
            ):
                value = raw_phase.get(key)
                if isinstance(value, str) and value.strip():
                    phase[key] = value.strip()
            phases.append(phase)

        raw_order = payload.get("phase_order", [])
        order = [
            phase_id
            for phase_id in raw_order
            if isinstance(phase_id, str) and phase_id in seen
        ] if isinstance(raw_order, list) else []
        order.extend(
            phase["phase_id"]
            for phase in phases
            if phase["phase_id"] not in order
        )
        return {
            "schema": "taskplanner.phase_catalog_browser_view.v1",
            "case_id": self.case_id,
            "procedure_id": payload.get("procedure_id"),
            "phase_namespace": payload.get("phase_namespace"),
            "authority": payload.get("authority"),
            "runtime_status": payload.get("runtime_status"),
            "phase_order": order,
            "phases": phases,
        }

    def _validated_assistant_provisional_phase_records(
        self,
        records: list[dict[str, Any]],
        *,
        descriptor: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate direct, user-authorized assistant Phase context.

        These records intentionally have no synthetic human candidate/action
        history.  They remain ambiguous, read-only context and contribute
        nothing to interaction scoring.
        """

        if descriptor.get("event_count") != len(records):
            raise InputError(
                "assistant provisional phase reference event count가 다릅니다."
            )
        expected_counts = {
            status: sum(
                record.get("review_status") == status for record in records
            )
            for status in REVIEW_STATUSES
        }
        if descriptor.get("review_status_counts") != expected_counts:
            raise InputError(
                "assistant phase_annotation review_status_counts가 다릅니다."
            )
        authority = descriptor.get("review_authority")
        if not isinstance(authority, dict):
            raise InputError("assistant Phase review_authority가 없습니다.")
        reviewer_ids = authority.get("reviewer_ids")
        authorized_by = authority.get("authorized_by")
        if (
            authority.get("reviewer_kind") != "ai_assistant"
            or not isinstance(reviewer_ids, list)
            or not reviewer_ids
            or any(
                not isinstance(reviewer_id, str) or not reviewer_id.strip()
                for reviewer_id in reviewer_ids
            )
            or len(set(reviewer_ids)) != len(reviewer_ids)
            or not isinstance(authorized_by, str)
            or not authorized_by.strip()
        ):
            raise InputError("assistant Phase review_authority가 올바르지 않습니다.")

        clean_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_key: tuple[int, str] | None = None
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"assistant provisional phase reference:{index}"
            if (
                record.get("schema")
                not in (
                    "taskplanner.observable_interaction_point.v1",
                    "taskplanner.observable_interaction_point.v2",
                )
                or record.get("case_id") != self.case_id
                or record.get("event_type") != "phase_start"
                or record.get("review_status") != "ambiguous"
            ):
                raise InputError(
                    f"{location}: ambiguous phase_start point가 아닙니다."
                )
            event_id = str(record.get("event_id", ""))
            if (
                re.fullmatch(
                    rf"{re.escape(self.case_id)}-PH[0-9]{{4,}}",
                    event_id,
                )
                is None
                or event_id in seen_ids
            ):
                raise InputError(f"{location}: event_id가 없거나 중복입니다.")
            seen_ids.add(event_id)
            frame_idx, time_sec = self._canonical_time(
                record.get("source_frame_idx"),
                location=location,
            )
            self._require_same_time(
                record.get("time_sec"),
                time_sec,
                location=location,
            )
            source_views = self._validated_source_views(
                record.get("source_views"),
                location=location,
            )
            phase_id = record.get("phase_id")
            if not isinstance(phase_id, str) or re.fullmatch(
                r"P[0-9]{2,}",
                phase_id,
            ) is None:
                raise InputError(f"{location}: phase_id가 올바르지 않습니다.")
            if record.get("phase_boundary_kind") not in (
                "clip_initial_state",
                "observed_transition",
                "uncertain_transition",
            ):
                raise InputError(
                    f"{location}: phase_boundary_kind가 올바르지 않습니다."
                )
            if any(field in record for field in ("tool", "from", "to")):
                raise InputError(
                    f"{location}: phase에 tool/location을 지정할 수 없습니다."
                )
            if record.get("label_origin") != "assistant_video_adjudication":
                raise InputError(
                    f"{location}: assistant Phase 출처가 올바르지 않습니다."
                )
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "ai_assistant"
                or review.get("reviewer_id") not in reviewer_ids
                or review.get("authorized_by") != authorized_by
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(
                    f"{location}: assistant review provenance 불일치"
                )
            sort_key = (frame_idx, event_id)
            if previous_key is not None and sort_key < previous_key:
                raise InputError(
                    "assistant provisional phase reference가 canonical 순서가 "
                    "아닙니다."
                )
            previous_key = sort_key
            record["_review_ui"] = {
                "candidate_id": None,
                "candidate_sha256": None,
                "effective_decision": {
                    "review_status": "ambiguous",
                    "review": review,
                },
                "action_history": [],
                "authority": ASSISTANT_PHASE_CONTEXT_AUTHORITY,
            }
            record["_final_review"] = {
                "layer": "phase_context",
                "read_only": True,
                "context_only": True,
                "provisional": True,
                "scoring_role": PHASE_CONTEXT_SCORING_ROLE,
                "overlay_cue": {"geometry": "interval_from_phase_start"},
            }
            record["source_views"] = source_views
            clean_records.append(record)
        return clean_records

    def _validated_provisional_phase_records(
        self,
        records: list[dict[str, Any]],
        *,
        resolved_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(records) != len(resolved_events):
            raise InputError("provisional phase reference event count가 다릅니다.")
        resolved_by_id = {
            str(event["event_id"]): event for event in resolved_events
        }
        clean_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_key: tuple[int, str] | None = None
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"provisional phase reference:{index}"
            if (
                record.get("schema")
                not in (
                    "taskplanner.observable_interaction_point.v1",
                    "taskplanner.observable_interaction_point.v2",
                )
                or record.get("case_id") != self.case_id
                or record.get("event_type") != "phase_start"
                or record.get("review_status") != "ambiguous"
            ):
                raise InputError(
                    f"{location}: ambiguous phase_start point가 아닙니다."
                )
            event_id = str(record.get("event_id", ""))
            resolved = resolved_by_id.get(event_id)
            if resolved is None or event_id in seen_ids:
                raise InputError(f"{location}: 대응하는 사람 판정이 없습니다.")
            seen_ids.add(event_id)
            frame_idx, time_sec = self._canonical_time(
                record.get("source_frame_idx"),
                location=location,
            )
            self._require_same_time(
                record.get("time_sec"),
                time_sec,
                location=location,
            )
            self._validated_source_views(
                record.get("source_views"),
                location=location,
            )
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "human"
                or not str(review.get("reviewer_id", "")).strip()
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(f"{location}: human review provenance 불일치")
            for field in (
                "phase_id",
                "phase_boundary_kind",
                "source_frame_idx",
                "time_sec",
                "source_views",
                "review_status",
                "review",
            ):
                if record.get(field) != resolved.get(field):
                    raise InputError(
                        f"{location}: resolved 사람 판정과 {field}가 다릅니다."
                    )
            if record.get("label_origin") != "human_video_review":
                raise InputError(
                    f"{location}: provisional Phase 출처가 올바르지 않습니다."
                )
            sort_key = (frame_idx, event_id)
            if previous_key is not None and sort_key < previous_key:
                raise InputError(
                    "provisional phase reference가 canonical 순서가 아닙니다."
                )
            previous_key = sort_key
            record["_review_ui"] = resolved["_review_ui"]
            record["_final_review"] = resolved["_final_review"]
            clean_records.append(record)
        if seen_ids != set(resolved_by_id):
            raise InputError("provisional phase reference에 누락 event가 있습니다.")
        return clean_records

    def _validated_phase_candidates(
        self,
        records: list[dict[str, Any]],
        *,
        descriptor: dict[str, Any],
    ) -> list[dict[str, Any]]:
        seen_ids: set[str] = set()
        counts = {
            "ambiguous": 0,
            "confirmed": 0,
            "proposed": 0,
            "rejected": 0,
        }
        previous_key: tuple[int, str] | None = None
        clean_records: list[dict[str, Any]] = []
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"phase candidate:{index}"
            if (
                record.get("schema")
                != "taskplanner.observable_interaction_point.v1"
                or record.get("case_id") != self.case_id
                or record.get("event_type") != "phase_start"
            ):
                raise InputError(f"{location}: phase_start point가 아닙니다.")
            event_id = str(record.get("event_id", ""))
            if (
                re.fullmatch(
                    rf"{re.escape(self.case_id)}-PH[0-9]{{4,}}",
                    event_id,
                )
                is None
                or event_id in seen_ids
            ):
                raise InputError(f"{location}: event_id가 없거나 중복입니다.")
            seen_ids.add(event_id)
            frame_idx, time_sec = self._canonical_time(
                record.get("source_frame_idx"),
                location=location,
            )
            self._require_same_time(
                record.get("time_sec"),
                time_sec,
                location=location,
            )
            phase_id = record.get("phase_id")
            if not isinstance(phase_id, str) or re.fullmatch(
                r"P[0-9]{2,}",
                phase_id,
            ) is None:
                raise InputError(f"{location}: phase_id가 올바르지 않습니다.")
            boundary_kind = record.get("phase_boundary_kind")
            if not isinstance(boundary_kind, str) or not boundary_kind.strip():
                raise InputError(f"{location}: phase boundary kind가 없습니다.")
            self._validated_source_views(
                record.get("source_views"),
                location=location,
            )
            review_status = str(record.get("review_status", ""))
            if review_status not in counts:
                raise InputError(f"{location}: review_status가 올바르지 않습니다.")
            counts[review_status] += 1
            if not str(record.get("label_origin", "")).strip():
                raise InputError(f"{location}: label_origin이 없습니다.")
            sort_key = (frame_idx, event_id)
            if previous_key is not None and sort_key < previous_key:
                raise InputError("phase candidate가 canonical 순서가 아닙니다.")
            previous_key = sort_key
            clean_records.append(record)
        if descriptor.get("review_status_counts") != counts:
            raise InputError("phase_annotation review_status_counts가 다릅니다.")
        return clean_records

    @staticmethod
    def _validated_source_views(value: Any, *, location: str) -> list[str]:
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)
            or any(item not in SOURCE_VIEWS for item in value)
        ):
            raise InputError(f"{location}: source_views가 올바르지 않습니다.")
        canonical = [item for item in SOURCE_VIEWS if item in value]
        if value != canonical:
            raise InputError(f"{location}: source_views 순서가 canonical이 아닙니다.")
        return canonical

    def _validated_phase_fields(
        self,
        value: Any,
        *,
        location: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise InputError(f"{location}: adjudicated_fields 객체가 필요합니다.")
        if value.get("event_type") != "phase_start":
            raise InputError(f"{location}: phase event_type을 변경할 수 없습니다.")
        frame_idx, time_sec = self._canonical_time(
            value.get("source_frame_idx"),
            location=location,
        )
        self._require_same_time(
            value.get("time_sec"),
            time_sec,
            location=location,
        )
        phase_id = value.get("phase_id")
        if not isinstance(phase_id, str) or re.fullmatch(
            r"P[0-9]{2,}",
            phase_id,
        ) is None:
            raise InputError(f"{location}: phase_id가 올바르지 않습니다.")
        source_views = self._validated_source_views(
            value.get("source_views"),
            location=location,
        )
        if any(value.get(field) is not None for field in ("tool", "from", "to")):
            raise InputError(f"{location}: phase에 tool/location을 지정할 수 없습니다.")
        return {
            "event_type": "phase_start",
            "source_frame_idx": frame_idx,
            "time_sec": time_sec,
            "tool": None,
            "from": None,
            "to": None,
            "phase_id": phase_id,
            "source_views": source_views,
        }

    def _validated_phase_actions(
        self,
        records: list[dict[str, Any]],
        *,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates_by_id = {
            str(candidate["event_id"]): candidate for candidate in candidates
        }
        seen_action_ids: set[str] = set()
        active_by_annotation: dict[str, str] = {}
        phase_actions: list[dict[str, Any]] = []
        for index, source_record in enumerate(records, 1):
            candidate_id = str(source_record.get("candidate_id") or "")
            annotation_id = str(source_record.get("annotation_id") or "")
            if (
                candidate_id not in candidates_by_id
                and annotation_id not in candidates_by_id
            ):
                continue
            record = dict(source_record)
            location = f"phase human decision:{index}"
            if (
                record.get("schema") != TIMELINE_ACTION_SCHEMA
                or record.get("case_id") != self.case_id
                or record.get("operation") != "review_candidate"
            ):
                raise InputError(f"{location}: phase review action이 아닙니다.")
            if candidate_id not in candidates_by_id or annotation_id != candidate_id:
                raise InputError(f"{location}: phase candidate 참조가 다릅니다.")
            action_id = str(record.get("action_id", ""))
            if not action_id or action_id in seen_action_ids:
                raise InputError(f"{location}: action_id가 없거나 중복입니다.")
            seen_action_ids.add(action_id)
            candidate = candidates_by_id[candidate_id]
            if record.get("candidate_sha256") != sha256_value(candidate):
                raise InputError(f"{location}: phase candidate digest 불일치")
            supersedes_value = record.get("supersedes_action_id")
            supersedes_action_id = (
                str(supersedes_value).strip()
                if supersedes_value is not None
                else None
            )
            if active_by_annotation.get(annotation_id) != supersedes_action_id:
                raise InputError(
                    f"{location}: supersedes_action_id가 현재 판정과 다릅니다."
                )
            fields = self._validated_phase_fields(
                record.get("adjudicated_fields"),
                location=location,
            )
            if record.get("adjudicated_fields") != fields:
                raise InputError(f"{location}: canonical phase 판정 필드 불일치")
            review_status = str(record.get("review_status", ""))
            if review_status not in REVIEW_STATUSES:
                raise InputError(f"{location}: review_status 불일치")
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "human"
                or not str(review.get("reviewer_id", "")).strip()
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(f"{location}: human review provenance 불일치")
            expected_origin = (
                "human_video_review" if review_status == "confirmed" else None
            )
            if record.get("resulting_label_origin") != expected_origin:
                raise InputError(f"{location}: resulting label origin 불일치")
            client_request_value = record.get("client_request_id")
            client_request_id = (
                str(client_request_value).strip()
                if client_request_value is not None
                else None
            )
            semantic_request = {
                "operation": "review_candidate",
                "annotation_id": annotation_id,
                "candidate_id": candidate_id,
                "candidate_sha256": sha256_value(candidate),
                "supersedes_action_id": supersedes_action_id,
                "client_request_id": client_request_id,
                "review_status": review_status,
                "reviewer_id": str(review["reviewer_id"]).strip(),
                "notes": str(review.get("notes", "")).strip(),
                "adjudicated_fields": fields,
            }
            if record.get("request_sha256") != sha256_value(semantic_request):
                raise InputError(f"{location}: request digest 불일치")
            active_by_annotation[annotation_id] = action_id
            phase_actions.append(record)
        return phase_actions

    def _reference_descriptor(self, key: str) -> dict[str, Any]:
        descriptor = self.reference.get(key)
        if not isinstance(descriptor, dict):
            raise InputError(f"evaluation_reference.{key}가 없습니다.")
        return descriptor

    def _case_file(self, value: Any, *, label: str) -> Path:
        return self._confined_file(
            self.case_dir,
            value,
            allowed_root=self.case_dir,
            label=label,
        )

    @staticmethod
    def _confined_file(
        base_dir: Path,
        value: Any,
        *,
        allowed_root: Path,
        label: str,
    ) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"{label} 경로가 없습니다.")
        relative = Path(value)
        if relative.is_absolute():
            raise InputError(f"{label} 경로는 상대 경로여야 합니다.")
        target = (base_dir / relative).resolve()
        root = allowed_root.resolve()
        if target != root and root not in target.parents:
            raise InputError(f"{label} 경로가 허용된 디렉터리 밖입니다.")
        if not target.is_file():
            raise InputError(f"{label} 파일이 없습니다: {target}")
        return target

    @staticmethod
    def _require_sha256(path: Path, expected: Any, *, label: str) -> None:
        if not isinstance(expected, str) or not re.fullmatch(
            r"[0-9a-f]{64}",
            expected,
        ):
            raise InputError(f"{label} SHA-256이 올바르지 않습니다.")
        actual = sha256_file(path)
        if actual != expected:
            raise InputError(
                f"{label} SHA-256 불일치: expected={expected}, actual={actual}"
            )

    def _validated_timestamps(self, timeline: dict[str, Any]) -> list[float]:
        if timeline.get("case_id") != self.case_id:
            raise InputError("final timeline case_id가 다릅니다.")
        values = timeline.get("timestamps_sec")
        if not isinstance(values, list) or not values:
            raise InputError("final timeline timestamps_sec가 비어 있습니다.")
        try:
            timestamps = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise InputError("final timeline timestamp가 숫자가 아닙니다.") from exc
        if any(not math.isfinite(value) for value in timestamps):
            raise InputError("final timeline timestamp가 유한수가 아닙니다.")
        if any(
            not (right > left)
            for left, right in zip(timestamps, timestamps[1:])
        ):
            raise InputError("final timeline timestamp가 엄격한 오름차순이 아닙니다.")
        if timeline.get("frame_count") != len(timestamps):
            raise InputError("final timeline frame_count가 다릅니다.")
        return timestamps

    def _canonical_time(self, frame_idx: Any, *, location: str) -> tuple[int, float]:
        if not isinstance(frame_idx, int):
            raise InputError(f"{location}: source frame이 정수가 아닙니다.")
        if not 0 <= frame_idx < len(self.timestamps):
            raise InputError(f"{location}: source frame이 timeline 범위 밖입니다.")
        return frame_idx, self.timestamps[frame_idx]

    @staticmethod
    def _require_same_time(actual: Any, expected: float, *, location: str) -> None:
        try:
            numeric = float(actual)
        except (TypeError, ValueError) as exc:
            raise InputError(f"{location}: timestamp가 숫자가 아닙니다.") from exc
        if not math.isfinite(numeric) or abs(numeric - expected) > 5e-10:
            raise InputError(f"{location}: timestamp가 canonical timeline과 다릅니다.")

    def _validated_layer(
        self,
        records: list[dict[str, Any]],
        *,
        descriptor: dict[str, Any],
        layer: str,
    ) -> list[dict[str, Any]]:
        expected_count = descriptor.get("confirmed_event_count")
        if expected_count != len(records):
            raise InputError(f"{layer}: manifest event count가 다릅니다.")
        event_ids: set[str] = set()
        event_type_counts: dict[str, int] = {}
        label_origin_counts: dict[str, int] = {}
        previous_sort_key: tuple[int, str] | None = None
        clean_records: list[dict[str, Any]] = []
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"{layer}:{index}"
            if record.get("case_id") != self.case_id:
                raise InputError(f"{location}: case_id 불일치")
            event_id = str(record.get("event_id", ""))
            if not event_id or event_id in event_ids:
                raise InputError(f"{location}: event_id가 없거나 중복입니다.")
            event_ids.add(event_id)
            event_type = str(record.get("event_type", ""))
            if event_type not in FINAL_EVENT_TYPES:
                raise InputError(f"{location}: 최종 interaction event type이 아닙니다.")
            if record.get("review_status") != "confirmed":
                raise InputError(f"{location}: confirmed event만 허용됩니다.")
            if event_type == "implicit_tool_request":
                if record.get("schema") != (
                    "taskplanner.observable_interaction_interval.v1"
                ):
                    raise InputError(f"{location}: request interval schema 불일치")
                start_idx, start_sec = self._canonical_time(
                    record.get("start_source_frame_idx"),
                    location=location,
                )
                end_idx, end_sec = self._canonical_time(
                    record.get("end_source_frame_idx"),
                    location=location,
                )
                if end_idx < start_idx:
                    raise InputError(f"{location}: request interval 순서가 잘못됐습니다.")
                if record.get("source_frame_idx") != start_idx:
                    raise InputError(f"{location}: request source_frame_idx 불일치")
                self._require_same_time(
                    record.get("time_sec"),
                    start_sec,
                    location=location,
                )
                self._require_same_time(
                    record.get("start_sec"),
                    start_sec,
                    location=location,
                )
                self._require_same_time(
                    record.get("end_sec"),
                    end_sec,
                    location=location,
                )
                sort_frame = start_idx
            else:
                point_schema = record.get("schema")
                if point_schema not in (
                    "taskplanner.observable_interaction_point.v1",
                    "taskplanner.observable_interaction_point.v2",
                ):
                    raise InputError(f"{location}: transfer point schema 불일치")
                frame_idx, time_sec = self._canonical_time(
                    record.get("source_frame_idx"),
                    location=location,
                )
                self._require_same_time(
                    record.get("time_sec"),
                    time_sec,
                    location=location,
                )
                tool = record.get("tool")
                if not isinstance(tool, str) or not TOOL_ID_PATTERN.fullmatch(tool):
                    raise InputError(f"{location}: canonical tool ID가 없습니다.")
                allowed_endpoints = (
                    LEGACY_TRANSFER_ENDPOINTS
                    if point_schema
                    == "taskplanner.observable_interaction_point.v1"
                    else TRANSFER_ENDPOINTS
                )
                if record.get("from") not in allowed_endpoints:
                    raise InputError(f"{location}: from endpoint가 올바르지 않습니다.")
                if record.get("to") not in allowed_endpoints:
                    raise InputError(f"{location}: to endpoint가 올바르지 않습니다.")
                if record.get("from") == record.get("to"):
                    raise InputError(
                        f"{location}: 같은 endpoint 이동은 허용되지 않습니다."
                    )
                presentation = record.get("review_presentation")
                if presentation is not None:
                    if (
                        point_schema
                        != "taskplanner.observable_interaction_point.v2"
                    ):
                        raise InputError(
                            f"{location}: v1 point에 review presentation을 "
                            "지정할 수 없습니다."
                        )
                    record["review_presentation"] = (
                        self._validated_review_presentation(
                            presentation,
                            anchor_frame_idx=frame_idx,
                            location=location,
                        )
                    )
                sort_frame = frame_idx
            sort_key = (sort_frame, event_id)
            if previous_sort_key is not None and sort_key < previous_sort_key:
                raise InputError(f"{layer}: event가 canonical 순서가 아닙니다.")
            previous_sort_key = sort_key
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
            label_origin = str(record.get("label_origin", ""))
            if not label_origin:
                raise InputError(f"{location}: label_origin이 없습니다.")
            label_origin_counts[label_origin] = (
                label_origin_counts.get(label_origin, 0) + 1
            )
            clean_records.append(record)
        if descriptor.get("event_type_counts") != event_type_counts:
            raise InputError(f"{layer}: manifest event_type_counts가 다릅니다.")
        declared_origins = descriptor.get("label_origin_counts")
        if declared_origins is not None and declared_origins != label_origin_counts:
            raise InputError(f"{layer}: manifest label_origin_counts가 다릅니다.")
        return clean_records

    def _validated_review_presentation(
        self,
        value: Any,
        *,
        anchor_frame_idx: int,
        location: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise InputError(f"{location}: review_presentation 객체가 필요합니다.")
        allowed_keys = {
            "observation_ko",
            "interpretation_ko",
            "evidence_start_source_frame_idx",
            "evidence_end_source_frame_idx",
            "source_views",
            "dt_observation_ko",
            "dt_interpretation_ko",
        }
        if set(value) - allowed_keys:
            raise InputError(
                f"{location}: review_presentation에 알 수 없는 필드가 있습니다."
            )
        for key in ("observation_ko", "interpretation_ko"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise InputError(f"{location}: {key} 문장이 필요합니다.")
        for key in ("dt_observation_ko", "dt_interpretation_ko"):
            if key in value and (
                not isinstance(value[key], str) or not value[key].strip()
            ):
                raise InputError(f"{location}: {key} 문장이 올바르지 않습니다.")
        start_idx, _ = self._canonical_time(
            value.get("evidence_start_source_frame_idx"),
            location=location,
        )
        end_idx, _ = self._canonical_time(
            value.get("evidence_end_source_frame_idx"),
            location=location,
        )
        if not start_idx <= anchor_frame_idx <= end_idx:
            raise InputError(
                f"{location}: anchor가 review evidence 범위 밖입니다."
            )
        source_views = self._validated_source_views(
            value.get("source_views"),
            location=location,
        )
        presentation = dict(value)
        presentation["source_views"] = source_views
        return presentation

    def _validated_review_attention(self) -> list[dict[str, Any]]:
        records = self.report.get("review_attention", [])
        if not isinstance(records, list):
            raise InputError("projection report review_attention이 목록이 아닙니다.")
        counts = self.report.get("counts")
        if (
            isinstance(counts, dict)
            and counts.get("review_attention_count", len(records))
            != len(records)
        ):
            raise InputError("projection report review attention count가 다릅니다.")
        if records and self.reaudit_path is None:
            raise InputError(
                "assistant reaudit 선언 없이 review attention이 존재합니다."
            )
        clean_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_key: tuple[int, str] | None = None
        for index, source_record in enumerate(records, 1):
            record = dict(source_record)
            location = f"review attention:{index}"
            if (
                record.get("schema")
                != "taskplanner.assistant_review_attention.v1"
                or record.get("case_id") != self.case_id
                or record.get("event_type") != "tool_transfer"
                or record.get("review_status") != "ambiguous"
                or record.get("label_origin") is not None
                or record.get("scoring_role")
                != REVIEW_ATTENTION_SCORING_ROLE
            ):
                raise InputError(
                    f"{location}: ambiguous review attention record가 아닙니다."
                )
            event_id = str(record.get("event_id", ""))
            if not event_id or event_id in seen_ids:
                raise InputError(f"{location}: event_id가 없거나 중복입니다.")
            seen_ids.add(event_id)
            frame_idx, time_sec = self._canonical_time(
                record.get("source_frame_idx"),
                location=location,
            )
            self._require_same_time(
                record.get("time_sec"),
                time_sec,
                location=location,
            )
            if (
                record.get("from") not in TRANSFER_ENDPOINTS
                or record.get("to") not in TRANSFER_ENDPOINTS
                or record.get("from") == record.get("to")
            ):
                raise InputError(f"{location}: transfer endpoint가 올바르지 않습니다.")
            if (
                not isinstance(record.get("tool"), str)
                or not TOOL_ID_PATTERN.fullmatch(record["tool"])
            ):
                raise InputError(f"{location}: canonical tool ID가 없습니다.")
            record["source_views"] = self._validated_source_views(
                record.get("source_views"),
                location=location,
            )
            record["review_presentation"] = (
                self._validated_review_presentation(
                    record.get("review_presentation"),
                    anchor_frame_idx=frame_idx,
                    location=location,
                )
            )
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "ai_assistant"
                or not str(review.get("reviewer_id", "")).strip()
                or not str(review.get("authorized_by", "")).strip()
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(f"{location}: assistant provenance가 올바르지 않습니다.")
            sort_key = (frame_idx, event_id)
            if previous_key is not None and sort_key < previous_key:
                raise InputError("review attention이 canonical 순서가 아닙니다.")
            previous_key = sort_key
            presentation = record["review_presentation"]
            record["_final_review"] = {
                "layer": "review_attention",
                "read_only": True,
                "context_only": True,
                "review_attention": True,
                "scoring_role": REVIEW_ATTENTION_SCORING_ROLE,
                "overlay_cue": {
                    "geometry": "evidence_interval",
                    "start_source_frame_idx": presentation[
                        "evidence_start_source_frame_idx"
                    ],
                    "end_source_frame_idx": presentation[
                        "evidence_end_source_frame_idx"
                    ],
                },
            }
            clean_records.append(record)
        return clean_records

    def _validated_projection(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        report = self.report
        if report.get("schema") != "taskplanner.dt_interaction_projection_report.v1":
            raise InputError("지원하지 않는 projection report schema입니다.")
        if report.get("case_id") != self.case_id:
            raise InputError("projection report case_id가 다릅니다.")
        if report.get("source_revision") != self.reference.get("source_revision"):
            raise InputError("projection report source revision이 다릅니다.")
        if report.get("adjudication_revision") != self.reference.get(
            "adjudication_revision"
        ):
            raise InputError("projection report adjudication revision이 다릅니다.")
        outputs = report.get("outputs")
        if not isinstance(outputs, dict):
            raise InputError("projection report outputs가 없습니다.")
        counts = report.get("counts")
        if not isinstance(counts, dict):
            raise InputError("projection report counts가 없습니다.")
        if counts.get("observed_confirmed_count") != len(self.observed):
            raise InputError("projection report observed count가 다릅니다.")
        if counts.get("dt_confirmed_count") != len(self.dt_reference):
            raise InputError("projection report DT count가 다릅니다.")
        expected_outputs = {
            "observed": (
                self.observed_path,
                len(self.observed),
                self.reference["observed_reference"]["sha256"],
            ),
            "dt_reference": (
                self.dt_path,
                len(self.dt_reference),
                self.reference["dt_reference"]["sha256"],
            ),
        }
        repo_root = self.case_dir.parent.parent.parent.parent
        for key, (path, count, digest) in expected_outputs.items():
            output = outputs.get(key)
            if not isinstance(output, dict):
                raise InputError(f"projection report {key} output이 다릅니다.")
            try:
                resolve_repo_artifact_identity(
                    output.get("path"),
                    expected_path=path,
                    repo_root=repo_root,
                    expected_sha256=digest,
                    label=f"projection report {key} output",
                )
            except ValueError as exc:
                raise InputError(str(exc)) from exc
            if (
                output.get("record_count") != count
                or output.get("sha256") != digest
            ):
                raise InputError(f"projection report {key} output이 다릅니다.")

        operations = report.get("operations")
        if not isinstance(operations, dict):
            raise InputError("projection report operations가 없습니다.")
        if not isinstance(operations.get("collapsed_returns"), list):
            raise InputError("projection collapsed_returns가 없습니다.")
        observed_ids = {str(item["event_id"]) for item in self.observed}
        dt_ids = {str(item["event_id"]) for item in self.dt_reference}
        observed_by_id = {
            str(item["event_id"]): item for item in self.observed
        }
        dt_by_id = {
            str(item["event_id"]): item for item in self.dt_reference
        }
        observed_dispositions: dict[str, dict[str, Any]] = {}
        dt_dispositions: dict[str, dict[str, Any]] = {}

        mapping = operations.get("source_mapping")
        if not isinstance(mapping, list):
            raise InputError("projection source_mapping이 없습니다.")
        for index, item in enumerate(mapping, 1):
            location = f"projection source_mapping:{index}"
            if not isinstance(item, dict):
                raise InputError(f"{location}: 객체가 필요합니다.")
            operation = str(item.get("operation", ""))
            output_event_id = str(item.get("output_event_id", ""))
            source_event_ids = item.get("source_event_ids")
            if operation not in (
                "identity",
                "collapse_surgeon_scrub_mayo",
                "normalize_unresolved_operative_recipient",
            ):
                raise InputError(f"{location}: 지원하지 않는 operation입니다.")
            if output_event_id not in dt_ids:
                raise InputError(f"{location}: DT output event가 없습니다.")
            if not isinstance(source_event_ids, list) or not source_event_ids:
                raise InputError(f"{location}: source_event_ids가 비어 있습니다.")
            source_ids = [str(value) for value in source_event_ids]
            if any(value not in observed_ids for value in source_ids):
                raise InputError(f"{location}: observed source event가 없습니다.")
            if output_event_id in dt_dispositions:
                raise InputError(f"{location}: DT output mapping이 중복입니다.")
            if operation == "identity":
                if source_ids != [output_event_id]:
                    raise InputError(f"{location}: identity mapping이 올바르지 않습니다.")
                kind = "identity"
                label = "DT 평가에 그대로 포함"
                reason = "관측 이벤트와 DT 평가 이벤트가 동일합니다."
            elif operation == "collapse_surgeon_scrub_mayo":
                if len(source_ids) < 2:
                    raise InputError(f"{location}: collapse source가 부족합니다.")
                kind = "collapsed_output"
                label = "연속 이동을 DT 전이로 축약"
                reason = self._collapsed_reason(
                    operations,
                    source_ids,
                    output_event_id,
                )
            else:
                if source_ids != [output_event_id]:
                    raise InputError(
                        f"{location}: recipient normalization source가 "
                        "올바르지 않습니다."
                    )
                observed = observed_by_id[output_event_id]
                projected = dt_by_id[output_event_id]
                if (
                    observed.get("to")
                    != "operative_person_role_unresolved"
                    or projected.get("to") != "surgeon"
                    or observed.get("from") != projected.get("from")
                    or observed.get("tool") != projected.get("tool")
                    or observed.get("source_frame_idx")
                    != projected.get("source_frame_idx")
                ):
                    raise InputError(
                        f"{location}: observed/DT recipient normalization "
                        "edge가 올바르지 않습니다."
                    )
                kind = "normalized_output"
                label = "DT 계약에서만 수령 역할 정규화"
                reason = self._normalized_reason(
                    operations,
                    output_event_id,
                )
            disposition = {
                "kind": kind,
                "label": label,
                "reason": reason,
                "source_event_ids": source_ids,
                "output_event_id": output_event_id,
            }
            for source_id in source_ids:
                if source_id in observed_dispositions:
                    raise InputError(f"{location}: observed disposition이 중복입니다.")
                observed_disposition = dict(disposition)
                if operation == "collapse_surgeon_scrub_mayo":
                    observed_disposition["kind"] = (
                        "collapsed_output"
                        if source_id == output_event_id
                        else "collapse_source"
                    )
                elif operation == "normalize_unresolved_operative_recipient":
                    observed_disposition["kind"] = "normalization_source"
                observed_dispositions[source_id] = observed_disposition
            dt_dispositions[output_event_id] = dict(disposition)

        exclusion_specs = (
            (
                "excluded_roundtrips",
                "excluded_cleanup",
                "DT 평가 제외 · 스크럽 정리 행동",
            ),
            (
                "excluded_unclosed_direct_returns",
                "excluded_unclosed_direct_return",
                "DT 평가 제외 · Mayo 도착 미관측",
            ),
            (
                "excluded_unresolved_transfers",
                "excluded_unresolved_transfer",
                "DT 평가 제외 · 종류·수량·최종배치 미확정",
            ),
        )
        for key, kind, label in exclusion_specs:
            entries = operations.get(key)
            if key == "excluded_unresolved_transfers" and entries is None:
                entries = []
            if not isinstance(entries, list):
                raise InputError(f"projection {key}가 없습니다.")
            for index, item in enumerate(entries, 1):
                if not isinstance(item, dict):
                    raise InputError(f"projection {key}:{index}: 객체가 필요합니다.")
                values = item.get("source_event_ids")
                if values is None and item.get("source_event_id") is not None:
                    values = [item["source_event_id"]]
                if not isinstance(values, list) or not values:
                    raise InputError(
                        f"projection {key}:{index}: source event가 없습니다."
                    )
                source_ids = [str(value) for value in values]
                for source_id in source_ids:
                    if source_id not in observed_ids:
                        raise InputError(
                            f"projection {key}:{index}: observed event가 없습니다."
                        )
                    if source_id in observed_dispositions:
                        raise InputError(
                            f"projection {key}:{index}: disposition이 중복입니다."
                        )
                    if source_id in dt_ids:
                        raise InputError(
                            f"projection {key}:{index}: 제외 event가 DT에 있습니다."
                        )
                    observed_dispositions[source_id] = {
                        "kind": kind,
                        "label": label,
                        "reason": str(item.get("reason", "")),
                        "source_event_ids": source_ids,
                        "output_event_id": None,
                    }

        if set(observed_dispositions) != observed_ids:
            missing = sorted(observed_ids - set(observed_dispositions))
            raise InputError(f"projection disposition이 없는 observed event: {missing}")
        if set(dt_dispositions) != dt_ids:
            missing = sorted(dt_ids - set(dt_dispositions))
            raise InputError(f"projection mapping이 없는 DT event: {missing}")
        return observed_dispositions, dt_dispositions

    @staticmethod
    def _collapsed_reason(
        operations: dict[str, Any],
        source_ids: list[str],
        output_event_id: str,
    ) -> str:
        collapsed = operations.get("collapsed_returns")
        if not isinstance(collapsed, list):
            raise InputError("projection collapsed_returns가 없습니다.")
        matches = [
            item
            for item in collapsed
            if isinstance(item, dict)
            and [str(value) for value in item.get("source_event_ids", [])]
            == source_ids
            and str(item.get("output_event_id", "")) == output_event_id
        ]
        if len(matches) != 1:
            raise InputError("collapsed return과 source_mapping이 일치하지 않습니다.")
        return str(matches[0].get("reason", ""))

    @staticmethod
    def _normalized_reason(
        operations: dict[str, Any],
        output_event_id: str,
    ) -> str:
        normalized = operations.get("normalized_recipients")
        if not isinstance(normalized, list):
            raise InputError("projection normalized_recipients가 없습니다.")
        matches = [
            item
            for item in normalized
            if isinstance(item, dict)
            and str(item.get("source_event_id", "")) == output_event_id
            and str(item.get("output_event_id", "")) == output_event_id
        ]
        if len(matches) != 1:
            raise InputError(
                "normalized recipient와 source_mapping이 일치하지 않습니다."
            )
        return str(matches[0].get("reason", ""))

    @staticmethod
    def _decorate_events(
        records: list[dict[str, Any]],
        *,
        layer: str,
        dispositions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        for source_record in records:
            record = dict(source_record)
            geometry = (
                "interval"
                if record["event_type"] == "implicit_tool_request"
                else "point"
            )
            record["_final_review"] = {
                "layer": layer,
                "read_only": True,
                "disposition": dispositions[str(record["event_id"])],
                "overlay_cue": {"geometry": geometry},
            }
            decorated.append(record)
        return decorated

    def state(self) -> dict[str, Any]:
        operations = self.report["operations"]
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "available": True,
            "read_only": True,
            "revision": self.revision,
            "source_revision": self.reference.get("source_revision"),
            "adjudication_revision": self.reference.get("adjudication_revision"),
            "layers": {
                "observed": {
                    "events": self.observed,
                    "confirmed_event_count": len(self.observed),
                    "event_type_counts": self.reference["observed_reference"][
                        "event_type_counts"
                    ],
                    "sha256": self.reference["observed_reference"]["sha256"],
                },
                "dt_reference": {
                    "events": self.dt_reference,
                    "confirmed_event_count": len(self.dt_reference),
                    "event_type_counts": self.reference["dt_reference"][
                        "event_type_counts"
                    ],
                    "sha256": self.reference["dt_reference"]["sha256"],
                },
            },
            "projection": {
                "collapsed_returns": operations["collapsed_returns"],
                "excluded_roundtrips": operations["excluded_roundtrips"],
                "excluded_unclosed_direct_returns": operations[
                    "excluded_unclosed_direct_returns"
                ],
                "excluded_unresolved_transfers": operations.get(
                    "excluded_unresolved_transfers",
                    [],
                ),
                "normalized_recipients": operations.get(
                    "normalized_recipients",
                    [],
                ),
                "source_mapping": operations["source_mapping"],
            },
            "context_tracks": {
                "review_attention": {
                    "available": bool(self.review_attention),
                    "authority": (
                        "user_authorized_ai_assistant_reaudit"
                        if self.reaudit_path is not None
                        else None
                    ),
                    "scoring_role": REVIEW_ATTENTION_SCORING_ROLE,
                    "status": (
                        "ambiguous_requires_human_review"
                        if self.review_attention
                        else "not_available"
                    ),
                    "timeline_geometry": "bounded_evidence_interval",
                    "event_count": len(self.review_attention),
                    "sha256": (
                        self.reaudit_sha256
                        if self.reaudit_path is not None
                        else None
                    ),
                    "events": self.review_attention,
                },
                "speech": {
                    "available": self.speech_descriptor is not None,
                    "authority": (
                        self.speech_descriptor["authority"]
                        if self.speech_descriptor is not None
                        else None
                    ),
                    "scoring_role": VOICE_SCORING_ROLE,
                    "timeline_geometry": "point_at_source_timestamp",
                    "schema": self.speech_schema_id,
                    "complete_text_availability": (
                        "available_sec"
                        if self.speech_schema_id
                        == "taskplanner.observable_voice_point.v2"
                        else "legacy_source_timestamp"
                    ),
                    "event_count": len(self.speech_events),
                    "sha256": (
                        self.speech_descriptor["sha256"]
                        if self.speech_descriptor is not None
                        else None
                    ),
                    "events": self.speech_events,
                },
                "phase": {
                    "available": self.phase_descriptor is not None,
                    "authority": (
                        self.phase_descriptor["authority"]
                        if self.phase_descriptor is not None
                        else None
                    ),
                    "scoring_role": PHASE_CONTEXT_SCORING_ROLE,
                    "status": (
                        "provisional_ambiguous"
                        if self.phase_descriptor is not None
                        else "not_available"
                    ),
                    "timeline_geometry": "interval_from_phase_start",
                    "event_count": len(self.phase_events),
                    "ambiguous_event_count": len(self.phase_events),
                    "confirmed_interaction_count_contribution": 0,
                    "candidate_sha256": (
                        self.phase_descriptor.get("candidate_sha256")
                        if self.phase_descriptor is not None
                        else None
                    ),
                    "human_decision_sha256": (
                        self.phase_descriptor.get("human_decision_sha256")
                        if self.phase_descriptor is not None
                        else None
                    ),
                    "provisional_reference_sha256": (
                        self.phase_descriptor.get(
                            "provisional_reference_sha256"
                        )
                        if self.phase_descriptor is not None
                        else None
                    ),
                    "catalog": self.phase_catalog,
                    "events": self.phase_events,
                },
            },
            "policy": {
                "read_only": True,
                "write_api_enabled": False,
                "ground_truth_consumers": ["evaluation_only"],
                "speech_context_is_evaluation_ground_truth": False,
                "phase_context_is_evaluation_ground_truth": False,
            },
        }


class ReviewStore:
    """Loads immutable proposals and appends explicit human decisions."""

    def __init__(
        self,
        *,
        case_dir: Path,
        candidates_path: Path,
        timeline_path: Path,
        decisions_path: Path,
        timeline_actions_path: Path | None = None,
        additional_candidates_paths: Iterable[Path] = (),
        review_media_path: Path | None = None,
        media_duration_sec: float | None = None,
        stream_kind: str = "interaction",
    ) -> None:
        self.case_dir = case_dir.resolve()
        self.candidates_path = candidates_path.resolve()
        self.candidate_paths = [
            self.candidates_path,
            *(path.resolve() for path in additional_candidates_paths),
        ]
        self.timeline_path = timeline_path.resolve()
        self.decisions_path = decisions_path.resolve()
        self.timeline_actions_path = (
            timeline_actions_path.resolve()
            if timeline_actions_path is not None
            else self.case_dir / "human_timeline_actions.v1.jsonl"
        )
        self.review_media_path = (
            review_media_path.resolve() if review_media_path is not None else None
        )
        self.media_duration_sec = (
            float(media_duration_sec) if media_duration_sec is not None else None
        )
        try:
            self.allowed_event_types = STREAM_EVENT_TYPES[stream_kind]
        except KeyError as exc:
            raise InputError(f"지원하지 않는 stream_kind: {stream_kind}") from exc
        self.stream_kind = stream_kind
        self.timeline = load_json_object(self.timeline_path)
        self.case_id = str(self.timeline.get("case_id", ""))
        if not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise InputError("timeline case_id가 올바르지 않습니다.")
        if self.case_dir.name != self.case_id:
            raise InputError(
                f"case 디렉터리 이름({self.case_dir.name})과 timeline "
                f"case_id({self.case_id})가 다릅니다."
            )
        timestamps = self.timeline.get("timestamps_sec")
        if not isinstance(timestamps, list) or not timestamps:
            raise InputError("timeline timestamps_sec가 비어 있습니다.")
        self.timestamps = [float(value) for value in timestamps]
        if any(
            right <= left
            for left, right in zip(self.timestamps, self.timestamps[1:])
        ):
            raise InputError("timeline timestamp가 엄격한 오름차순이 아닙니다.")
        frame_count = self.timeline.get("frame_count")
        if frame_count != len(self.timestamps):
            raise InputError(
                "timeline frame_count와 timestamps_sec 길이가 다릅니다."
            )
        self.lock = threading.RLock()

    @property
    def candidate_source_status(self) -> str:
        if not any(path.exists() for path in self.candidate_paths):
            return "missing"
        if not any(
            path.is_file() and path.read_text(encoding="utf-8").strip()
            for path in self.candidate_paths
        ):
            return "empty"
        return "ready"

    def candidates(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        global_index = 0
        for candidate_path in self.candidate_paths:
            records = load_jsonl(candidate_path, missing_ok=True)
            for record in records:
                global_index += 1
                candidate_id = str(
                    record.get("event_id")
                    or record.get("candidate_id")
                    or f"{self.case_id}-C{global_index:04d}"
                )
                if candidate_id in seen:
                    raise InputError(f"중복 candidate ID: {candidate_id}")
                seen.add(candidate_id)
                if record.get("case_id") not in (None, self.case_id):
                    raise InputError(f"{candidate_id}: case_id 불일치")
                source_frame_idx = record.get("source_frame_idx")
                if record.get("event_type") not in self.allowed_event_types:
                    raise InputError(
                        f"{candidate_id}: {record.get('event_type')}는 "
                        f"{self.stream_kind} stream에서 허용되지 않습니다."
                    )
                if not isinstance(source_frame_idx, int):
                    raise InputError(
                        f"{candidate_id}: source_frame_idx가 정수가 아닙니다."
                    )
                if not 0 <= source_frame_idx < len(self.timestamps):
                    raise InputError(
                        f"{candidate_id}: source_frame_idx가 timeline 범위 밖입니다."
                    )
                canonical_time = self.timestamps[source_frame_idx]
                if abs(float(record.get("time_sec", canonical_time)) - canonical_time) > 5e-10:
                    raise InputError(
                        f"{candidate_id}: time_sec가 canonical timeline과 다릅니다."
                    )
                clean = dict(record)
                clean["_review_ui"] = {
                    "candidate_id": candidate_id,
                    "candidate_sha256": sha256_value(record),
                    "canonical_time_sec": canonical_time,
                    "candidate_source": str(candidate_path),
                }
                normalized.append(clean)
        return sorted(
            normalized,
            key=lambda item: (
                int(item["source_frame_idx"]),
                str(item["_review_ui"]["candidate_id"]),
            ),
        )

    def decisions(self) -> list[dict[str, Any]]:
        records = load_jsonl(self.decisions_path, missing_ok=True)
        candidates_by_id = {
            item["_review_ui"]["candidate_id"]: item
            for item in self.candidates()
        }
        seen_candidate_ids: set[str] = set()
        seen_decision_ids: set[str] = set()
        for index, record in enumerate(records, 1):
            if record.get("schema") != "taskplanner.human_review_decision.v1":
                raise InputError(
                    f"{self.decisions_path}:{index}: 지원하지 않는 decision schema"
                )
            if record.get("case_id") != self.case_id:
                raise InputError(
                    f"{self.decisions_path}:{index}: case_id 불일치"
                )
            candidate_id = str(record.get("candidate_id", ""))
            candidate = candidates_by_id.get(candidate_id)
            if candidate is None:
                raise InputError(
                    f"{self.decisions_path}:{index}: 알 수 없는 candidate_id"
                )
            if candidate_id in seen_candidate_ids:
                raise InputError(
                    f"{self.decisions_path}:{index}: 중복 candidate 판정"
                )
            seen_candidate_ids.add(candidate_id)
            decision_id = str(record.get("decision_id", ""))
            if not decision_id or decision_id in seen_decision_ids:
                raise InputError(
                    f"{self.decisions_path}:{index}: decision_id가 없거나 중복"
                )
            seen_decision_ids.add(decision_id)
            expected_candidate_sha = candidate["_review_ui"]["candidate_sha256"]
            if record.get("candidate_sha256") != expected_candidate_sha:
                raise InputError(
                    f"{self.decisions_path}:{index}: candidate digest 불일치"
                )
            review_status = str(record.get("review_status", ""))
            if review_status not in REVIEW_STATUSES:
                raise InputError(
                    f"{self.decisions_path}:{index}: review_status 불일치"
                )
            fields = self._validated_fields(record.get("adjudicated_fields"))
            if record.get("adjudicated_fields") != fields:
                raise InputError(
                    f"{self.decisions_path}:{index}: canonical 판정 필드 불일치"
                )
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "human"
                or not str(review.get("reviewer_id", "")).strip()
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(
                    f"{self.decisions_path}:{index}: human review provenance 불일치"
                )
            expected_origin = (
                "human_video_review" if review_status == "confirmed" else None
            )
            if record.get("resulting_label_origin") != expected_origin:
                raise InputError(
                    f"{self.decisions_path}:{index}: resulting label origin 불일치"
                )
            semantic_request = {
                "candidate_id": candidate_id,
                "candidate_sha256": expected_candidate_sha,
                "review_status": review_status,
                "reviewer_id": str(review["reviewer_id"]).strip(),
                "notes": str(review.get("notes", "")).strip(),
                "adjudicated_fields": fields,
            }
            if record.get("request_sha256") != sha256_value(semantic_request):
                raise InputError(
                    f"{self.decisions_path}:{index}: request digest 불일치"
                )
        return records

    @staticmethod
    def _review_record_id(record: dict[str, Any]) -> str:
        return str(record.get("action_id") or record.get("decision_id") or "")

    @staticmethod
    def _timeline_action_semantic_request(
        *,
        operation: str,
        annotation_id: str,
        candidate_id: str | None,
        candidate_sha256: str | None,
        supersedes_action_id: str | None,
        client_request_id: str | None,
        review_status: str,
        reviewer_id: str,
        notes: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "annotation_id": annotation_id,
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha256,
            "supersedes_action_id": supersedes_action_id,
            "client_request_id": client_request_id,
            "review_status": review_status,
            "reviewer_id": reviewer_id,
            "notes": notes,
            "adjudicated_fields": fields,
        }

    def timeline_actions(self) -> list[dict[str, Any]]:
        """Validate and return append-only timeline actions in log order."""

        records = load_jsonl(self.timeline_actions_path, missing_ok=True)
        candidates_by_id = {
            item["_review_ui"]["candidate_id"]: item
            for item in self.candidates()
        }
        legacy_by_candidate = {
            str(item["candidate_id"]): item for item in self.decisions()
        }
        active_by_annotation = {
            candidate_id: self._review_record_id(decision)
            for candidate_id, decision in legacy_by_candidate.items()
        }
        event_type_by_annotation = {
            candidate_id: str(candidate["event_type"])
            for candidate_id, candidate in candidates_by_id.items()
        }
        human_annotation_ids: set[str] = set()
        seen_action_ids: set[str] = set()
        seen_client_request_ids: set[str] = set()

        for index, record in enumerate(records, 1):
            location = f"{self.timeline_actions_path}:{index}"
            if record.get("schema") != TIMELINE_ACTION_SCHEMA:
                raise InputError(f"{location}: 지원하지 않는 action schema")
            if record.get("case_id") != self.case_id:
                raise InputError(f"{location}: case_id 불일치")
            action_id = str(record.get("action_id", ""))
            if not action_id or action_id in seen_action_ids:
                raise InputError(f"{location}: action_id가 없거나 중복")
            seen_action_ids.add(action_id)
            operation = str(record.get("operation", ""))
            if operation not in TIMELINE_ACTION_OPERATIONS:
                raise InputError(f"{location}: operation 불일치")
            annotation_id = str(record.get("annotation_id", "")).strip()
            if not annotation_id:
                raise InputError(f"{location}: annotation_id가 없습니다.")
            candidate_id_value = record.get("candidate_id")
            candidate_id = (
                str(candidate_id_value).strip()
                if candidate_id_value is not None
                else None
            )
            candidate_sha_value = record.get("candidate_sha256")
            candidate_sha256 = (
                str(candidate_sha_value).strip()
                if candidate_sha_value is not None
                else None
            )
            supersedes_value = record.get("supersedes_action_id")
            supersedes_action_id = (
                str(supersedes_value).strip()
                if supersedes_value is not None
                else None
            )
            client_request_value = record.get("client_request_id")
            client_request_id = (
                str(client_request_value).strip()
                if client_request_value is not None
                else None
            )
            if client_request_id:
                if client_request_id in seen_client_request_ids:
                    raise InputError(f"{location}: client_request_id 중복")
                seen_client_request_ids.add(client_request_id)

            record_fields = record.get("adjudicated_fields")
            fields = self._validated_fields(
                record_fields,
                request_interval=(
                    isinstance(record_fields, dict)
                    and (
                        "start_source_frame_idx" in record_fields
                        or "end_source_frame_idx" in record_fields
                    )
                ),
            )
            review_status = str(record.get("review_status", ""))
            if review_status not in REVIEW_STATUSES:
                raise InputError(f"{location}: review_status 불일치")
            review = record.get("review")
            if (
                not isinstance(review, dict)
                or review.get("reviewer_kind") != "human"
                or not str(review.get("reviewer_id", "")).strip()
                or not str(review.get("reviewed_at", "")).strip()
            ):
                raise InputError(f"{location}: human review provenance 불일치")
            expected_origin = (
                "human_video_review" if review_status == "confirmed" else None
            )
            if record.get("resulting_label_origin") != expected_origin:
                raise InputError(f"{location}: resulting label origin 불일치")

            if operation == "review_candidate":
                candidate = candidates_by_id.get(candidate_id or "")
                if candidate is None:
                    raise InputError(f"{location}: 알 수 없는 candidate_id")
                if annotation_id != candidate_id:
                    raise InputError(
                        f"{location}: candidate annotation_id 불일치"
                    )
                expected_sha = candidate["_review_ui"]["candidate_sha256"]
                if candidate_sha256 != expected_sha:
                    raise InputError(f"{location}: candidate digest 불일치")
                if fields["event_type"] != candidate["event_type"]:
                    raise InputError(f"{location}: candidate event_type 변경 금지")
            elif operation == "create_annotation":
                if candidate_id is not None or candidate_sha256 is not None:
                    raise InputError(
                        f"{location}: 새 annotation에 candidate를 지정할 수 없습니다."
                    )
                if annotation_id in candidates_by_id or annotation_id in human_annotation_ids:
                    raise InputError(f"{location}: annotation_id 중복")
                if supersedes_action_id is not None:
                    raise InputError(
                        f"{location}: 새 annotation은 이전 action을 "
                        "대체할 수 없습니다."
                    )
                if review_status == "rejected":
                    raise InputError(
                        f"{location}: 새 annotation은 rejected로 만들 수 없습니다."
                    )
                if not client_request_id:
                    raise InputError(
                        f"{location}: 새 annotation에 client_request_id가 필요합니다."
                    )
                human_annotation_ids.add(annotation_id)
                event_type_by_annotation[annotation_id] = fields["event_type"]
            else:
                if candidate_id is not None or candidate_sha256 is not None:
                    raise InputError(
                        f"{location}: human annotation 수정에 candidate를 "
                        "지정할 수 없습니다."
                    )
                if annotation_id not in human_annotation_ids:
                    raise InputError(
                        f"{location}: 수정할 human annotation이 없습니다."
                    )
                if fields["event_type"] != event_type_by_annotation[annotation_id]:
                    raise InputError(f"{location}: event_type 변경 금지")

            current_active = active_by_annotation.get(annotation_id)
            if operation != "create_annotation":
                if current_active != supersedes_action_id:
                    raise InputError(
                        f"{location}: supersedes_action_id가 현재 판정과 다릅니다."
                    )
            active_by_annotation[annotation_id] = action_id

            semantic_request = self._timeline_action_semantic_request(
                operation=operation,
                annotation_id=annotation_id,
                candidate_id=candidate_id,
                candidate_sha256=candidate_sha256,
                supersedes_action_id=supersedes_action_id,
                client_request_id=client_request_id,
                review_status=review_status,
                reviewer_id=str(review["reviewer_id"]).strip(),
                notes=str(review.get("notes", "")).strip(),
                fields=fields,
            )
            if record.get("request_sha256") != sha256_value(semantic_request):
                raise InputError(f"{location}: request digest 불일치")
            if record.get("adjudicated_fields") != fields:
                raise InputError(f"{location}: canonical 판정 필드 불일치")
        return records

    def _resolved_reviews(
        self,
        *,
        decisions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, list[dict[str, Any]]],
        set[str],
    ]:
        active: dict[str, dict[str, Any]] = {}
        history: dict[str, list[dict[str, Any]]] = {}
        human_annotations: set[str] = set()
        for decision in decisions:
            annotation_id = str(decision["candidate_id"])
            active[annotation_id] = decision
            history.setdefault(annotation_id, []).append(decision)
        for action in actions:
            annotation_id = str(action["annotation_id"])
            active[annotation_id] = action
            history.setdefault(annotation_id, []).append(action)
            if action["operation"] == "create_annotation":
                human_annotations.add(annotation_id)
        return active, history, human_annotations

    def revision(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.timeline_path.read_bytes())
        for index, candidate_path in enumerate(self.candidate_paths):
            digest.update(
                sha256_path_or_marker(
                    candidate_path,
                    f"<candidates-{index}-missing>".encode("ascii"),
                )
            )
        digest.update(
            sha256_path_or_marker(
                self.decisions_path,
                b"<human-decisions-missing>",
            )
        )
        digest.update(
            sha256_path_or_marker(
                self.timeline_actions_path,
                b"<human-timeline-actions-missing>",
            )
        )
        return digest.hexdigest()[:16]

    def state(self) -> dict[str, Any]:
        with self.lock:
            candidates = self.candidates()
            decisions = self.decisions()
            actions = self.timeline_actions()
            decisions_by_candidate = {
                str(item["candidate_id"]): item for item in decisions
            }
            effective, history, human_annotation_ids = self._resolved_reviews(
                decisions=decisions,
                actions=actions,
            )
            for candidate in candidates:
                candidate_id = candidate["_review_ui"]["candidate_id"]
                candidate["_review_ui"]["legacy_decision"] = (
                    decisions_by_candidate.get(candidate_id)
                )
                candidate["_review_ui"]["effective_decision"] = effective.get(
                    candidate_id
                )
                candidate["_review_ui"]["human_decision"] = effective.get(
                    candidate_id
                )
                candidate["_review_ui"]["action_history"] = history.get(
                    candidate_id,
                    [],
                )
            human_annotations: list[dict[str, Any]] = []
            for annotation_id in sorted(
                human_annotation_ids,
                key=lambda item: (
                    effective[item]["adjudicated_fields"]["source_frame_idx"],
                    item,
                ),
            ):
                current = effective[annotation_id]
                fields = current["adjudicated_fields"]
                is_request_interval = (
                    fields["event_type"] == "implicit_tool_request"
                    and "start_source_frame_idx" in fields
                    and "end_source_frame_idx" in fields
                )
                human_annotations.append(
                    {
                        "schema": (
                            "taskplanner.observable_interaction_interval.v1"
                            if is_request_interval
                            else "taskplanner.observable_interaction_point.v1"
                        ),
                        "case_id": self.case_id,
                        "event_id": annotation_id,
                        **fields,
                        "review_status": current["review_status"],
                        "label_origin": current["resulting_label_origin"],
                        "_review_ui": {
                            "annotation_id": annotation_id,
                            "candidate_id": None,
                            "candidate_sha256": None,
                            "canonical_time_sec": fields["time_sec"],
                            "legacy_decision": None,
                            "effective_decision": current,
                            "human_decision": current,
                            "action_history": history.get(annotation_id, []),
                            "human_created": True,
                        },
                    }
                )
            counts = {status: 0 for status in REVIEW_STATUSES}
            for candidate in candidates:
                decision = candidate["_review_ui"]["effective_decision"]
                if decision is None:
                    continue
                status = str(decision.get("review_status", ""))
                if status in counts:
                    counts[status] += 1
            decided_ids = {
                candidate["_review_ui"]["candidate_id"]
                for candidate in candidates
                if candidate["_review_ui"]["effective_decision"] is not None
            }
            visual_end_sec = self._visual_end_sec()
            media_exists = (
                self.review_media_path is not None
                and self.review_media_path.is_file()
            )
            return {
                "case_id": self.case_id,
                "candidate_source": {
                    "path": str(self.candidates_path),
                    "status": self.candidate_source_status,
                },
                "candidate_sources": [
                    {
                        "path": str(path),
                        "status": (
                            "ready"
                            if path.is_file()
                            and bool(path.read_text(encoding="utf-8").strip())
                            else "empty" if path.is_file() else "missing"
                        ),
                    }
                    for path in self.candidate_paths
                ],
                "decision_output": str(self.decisions_path),
                "timeline_action_output": str(self.timeline_actions_path),
                "frame_count": len(self.timestamps),
                "timestamps_sec": self.timestamps,
                "source_fps": self.timeline.get("source_fps"),
                "start_sec": self.timestamps[0],
                "end_sec": self.timestamps[-1],
                "visual_end_sec": visual_end_sec,
                "gaps": self.timeline.get("gaps", []),
                "candidates": candidates,
                "human_annotations": human_annotations,
                "effective_annotations": human_annotations,
                "review_status_counts": counts,
                "reviewed_count": len(decided_ids),
                "remaining_count": sum(
                    candidate["_review_ui"]["candidate_id"] not in decided_ids
                    for candidate in candidates
                ),
                "media": {
                    "available": media_exists,
                    "video_url": (
                        "/api/media/review.mp4" if media_exists else None
                    ),
                    "duration_sec": (
                        self.media_duration_sec
                        if self.media_duration_sec is not None
                        else visual_end_sec
                    ),
                    "source_fps": self.timeline.get("source_fps"),
                    "visual_end_sec": visual_end_sec,
                },
                "revision": self.revision(),
                "vocabulary": {
                    "event_types": list(self.allowed_event_types),
                    "transfer_endpoints": list(TRANSFER_ENDPOINTS),
                    "review_statuses": list(REVIEW_STATUSES),
                    "event_geometry": {
                        "implicit_tool_request": "interval",
                        "tool_transfer": "point",
                        "phase_start": "point",
                    },
                },
                "policy": {
                    "candidate_records_are_read_only": True,
                    "ai_proposals_are_never_auto_confirmed": True,
                    "confirmed_label_origin": "human_video_review",
                    "decision_log": "append_only",
                    "timeline_action_log": "append_only_superseding",
                    "ground_truth_consumers": ["evaluation_only"],
                },
            }

    def _candidate_by_id(self, candidate_id: str) -> dict[str, Any]:
        for candidate in self.candidates():
            if candidate["_review_ui"]["candidate_id"] == candidate_id:
                return candidate
        raise InputError(f"후보를 찾을 수 없습니다: {candidate_id}")

    def _visual_end_sec(self) -> float:
        inferred_frame_duration = (
            self.timestamps[-1] - self.timestamps[-2]
            if len(self.timestamps) >= 2
            else 0.0
        )
        return self.timestamps[-1] + inferred_frame_duration

    def _validated_fields(
        self,
        fields: Any,
        *,
        request_interval: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(fields, dict):
            raise InputError("adjudicated_fields 객체가 필요합니다.")
        event_type = str(fields.get("event_type", ""))
        if event_type not in self.allowed_event_types:
            raise InputError(
                f"{self.stream_kind} stream에서 허용하지 않는 event_type입니다."
            )
        source_frame_idx = fields.get(
            "start_source_frame_idx",
            fields.get("source_frame_idx"),
        )
        if not isinstance(source_frame_idx, int):
            raise InputError("source_frame_idx는 정수여야 합니다.")
        if not 0 <= source_frame_idx < len(self.timestamps):
            raise InputError("source_frame_idx가 timeline 범위 밖입니다.")
        interval_keys = (
            "start_source_frame_idx",
            "end_source_frame_idx",
            "start_sec",
            "end_sec",
        )
        if event_type != "implicit_tool_request" and any(
            key in fields for key in interval_keys
        ):
            raise InputError(
                "구간 필드는 implicit_tool_request에만 사용할 수 있습니다."
            )

        tool = str(fields.get("tool") or "").strip() or None
        from_location = str(fields.get("from") or "").strip() or None
        to_location = str(fields.get("to") or "").strip() or None
        phase_id = str(fields.get("phase_id") or "").strip() or None
        end_source_frame_idx: int | None = None
        raw_source_views = fields.get("source_views")
        if raw_source_views is None:
            source_views = ["cam4", "flir"]
        elif (
            not isinstance(raw_source_views, list)
            or not raw_source_views
            or any(not isinstance(value, str) for value in raw_source_views)
            or len(set(raw_source_views)) != len(raw_source_views)
            or any(value not in SOURCE_VIEWS for value in raw_source_views)
        ):
            raise InputError(
                "source_views는 cam1/cam2/cam3/cam4/flir 중 실제 판독에 "
                "사용한 중복 없는 목록이어야 합니다."
            )
        else:
            source_views = [
                value for value in SOURCE_VIEWS if value in raw_source_views
            ]

        if event_type == "tool_transfer":
            if tool is None or not TOOL_ID_PATTERN.fullmatch(tool):
                raise InputError(
                    "tool_transfer에는 canonical tool ID가 필요합니다."
                )
            if from_location not in TRANSFER_ENDPOINTS:
                raise InputError("올바른 from 위치를 선택해 주세요.")
            if to_location not in TRANSFER_ENDPOINTS:
                raise InputError("올바른 to 위치를 선택해 주세요.")
            if from_location == to_location:
                raise InputError("tool_transfer의 from과 to는 달라야 합니다.")
            phase_id = None
        elif event_type == "phase_start":
            if phase_id is None or re.fullmatch(r"P[0-9]{2,}", phase_id) is None:
                raise InputError("phase_start에는 P00 형식 phase_id가 필요합니다.")
            tool = None
            from_location = None
            to_location = None
        else:
            # A visible open-hand signal is not a tool-specific semantic request.
            tool = None
            from_location = None
            to_location = None
            phase_id = None
            if request_interval:
                end_source_frame_idx = fields.get(
                    "end_source_frame_idx",
                    source_frame_idx,
                )
                if not isinstance(end_source_frame_idx, int):
                    raise InputError(
                        "end_source_frame_idx는 정수여야 합니다."
                    )
                if not 0 <= end_source_frame_idx < len(self.timestamps):
                    raise InputError(
                        "end_source_frame_idx가 timeline 범위 밖입니다."
                    )
                if end_source_frame_idx < source_frame_idx:
                    raise InputError(
                        "요청 구간 종료는 시작보다 빠를 수 없습니다."
                    )
                start_time = self.timestamps[source_frame_idx]
                end_time = self.timestamps[end_source_frame_idx]
                for gap in self.timeline.get("gaps", []):
                    before = float(gap["before_time_sec"])
                    after = float(gap["after_time_sec"])
                    if start_time < after and end_time > before:
                        raise InputError(
                            "암묵적 요청 구간은 카메라 gap을 가로지를 수 "
                            "없습니다."
                        )

        canonical = {
            "event_type": event_type,
            "source_frame_idx": source_frame_idx,
            "time_sec": self.timestamps[source_frame_idx],
            "tool": tool,
            "from": from_location,
            "to": to_location,
            "phase_id": phase_id,
            "source_views": source_views,
        }
        if end_source_frame_idx is not None:
            canonical.update(
                {
                    "start_source_frame_idx": source_frame_idx,
                    "end_source_frame_idx": end_source_frame_idx,
                    "start_sec": self.timestamps[source_frame_idx],
                    "end_sec": self.timestamps[end_source_frame_idx],
                }
            )
        return canonical

    def _validate_playhead_observability(self, payload: dict[str, Any]) -> None:
        value = payload.get("playhead_time_sec")
        if value is None:
            return
        try:
            playhead_time = float(value)
        except (TypeError, ValueError) as exc:
            raise InputError("playhead_time_sec는 숫자여야 합니다.") from exc
        for gap in self.timeline.get("gaps", []):
            before = float(gap["before_time_sec"])
            after = float(gap["after_time_sec"])
            if before < playhead_time < after:
                raise InputError(
                    "카메라 gap 내부에서는 시각 이벤트를 만들거나 "
                    "옮길 수 없습니다."
                )
        if (
            playhead_time < self.timestamps[0]
            or playhead_time > self._visual_end_sec()
        ):
            raise InputError(
                "영상 관측 범위 밖에서는 시각 이벤트를 만들거나 "
                "옮길 수 없습니다."
            )

    def _next_annotation_id(
        self,
        *,
        event_type: str,
        actions: list[dict[str, Any]],
    ) -> str:
        prefix = EVENT_ID_PREFIXES[event_type]
        matcher = re.compile(
            rf"^{re.escape(self.case_id)}-{re.escape(prefix)}([0-9]+)$"
        )
        maximum = 0
        identifiers = [
            str(item["_review_ui"]["candidate_id"]) for item in self.candidates()
        ]
        identifiers.extend(
            str(item["annotation_id"])
            for item in actions
            if item.get("operation") == "create_annotation"
        )
        for identifier in identifiers:
            match = matcher.fullmatch(identifier)
            if match is not None:
                maximum = max(maximum, int(match.group(1)))
        return f"{self.case_id}-{prefix}{maximum + 1:04d}"

    @staticmethod
    def _next_action_id(
        case_id: str,
        actions: list[dict[str, Any]],
    ) -> str:
        matcher = re.compile(rf"^{re.escape(case_id)}-A([0-9]+)$")
        maximum = 0
        for action in actions:
            match = matcher.fullmatch(str(action.get("action_id", "")))
            if match is not None:
                maximum = max(maximum, int(match.group(1)))
        return f"{case_id}-A{maximum + 1:04d}"

    def save_timeline_action(self, payload: Any) -> dict[str, Any]:
        """Append a candidate review, new event, or event correction."""

        if not isinstance(payload, dict):
            raise InputError("요청 본문은 JSON 객체여야 합니다.")
        operation = str(payload.get("operation", "review_candidate")).strip()
        if operation not in TIMELINE_ACTION_OPERATIONS:
            raise InputError("operation이 올바르지 않습니다.")
        review_status = str(payload.get("review_status", "")).strip()
        if review_status not in REVIEW_STATUSES:
            raise InputError("검토 결과가 올바르지 않습니다.")
        reviewer_id = str(payload.get("reviewer_id", "")).strip()
        if not reviewer_id:
            raise InputError("검토자 ID를 입력해 주세요.")
        notes = str(payload.get("notes", "")).strip()
        client_request_value = payload.get("client_request_id")
        client_request_id = (
            str(client_request_value).strip()
            if client_request_value is not None
            else None
        )
        if client_request_id is not None and (
            len(client_request_id) > 128
            or re.fullmatch(r"[A-Za-z0-9._:-]+", client_request_id) is None
        ):
            raise InputError("client_request_id 형식이 올바르지 않습니다.")
        if operation == "create_annotation" and not client_request_id:
            raise InputError(
                "새 이벤트 저장에는 client_request_id가 필요합니다."
            )
        fields = self._validated_fields(
            payload.get("adjudicated_fields"),
            request_interval=True,
        )
        self._validate_playhead_observability(payload)
        expected_revision = str(payload.get("revision", ""))

        candidate_id: str | None = None
        candidate_sha256: str | None = None
        annotation_id = str(payload.get("annotation_id", "")).strip()
        if operation == "review_candidate":
            candidate_id = str(payload.get("candidate_id", "")).strip()
            candidate = self._candidate_by_id(candidate_id)
            candidate_sha256 = str(
                payload.get("candidate_sha256", "")
            ).strip()
            expected_candidate_sha = candidate["_review_ui"]["candidate_sha256"]
            if candidate_sha256 != expected_candidate_sha:
                raise ConflictError(
                    "후보 내용이 바뀌었습니다. 새로고침 후 다시 "
                    "검토해 주세요."
                )
            if fields["event_type"] != candidate["event_type"]:
                raise InputError("후보의 event_type은 변경할 수 없습니다.")
            annotation_id = candidate_id
        elif operation == "create_annotation":
            if review_status == "rejected":
                raise InputError("새 이벤트는 rejected로 만들 수 없습니다.")
            if payload.get("candidate_id") is not None:
                raise InputError("새 이벤트에는 candidate_id를 지정할 수 없습니다.")
            if payload.get("candidate_sha256") is not None:
                raise InputError(
                    "새 이벤트에는 candidate_sha256을 지정할 수 없습니다."
                )
        else:
            if not annotation_id:
                raise InputError("수정할 annotation_id가 필요합니다.")
            if payload.get("candidate_id") is not None:
                raise InputError(
                    "사람이 추가한 이벤트 수정에는 candidate_id가 없습니다."
                )
            if payload.get("candidate_sha256") is not None:
                raise InputError(
                    "사람이 추가한 이벤트 수정에는 candidate_sha256이 없습니다."
                )

        supersedes_value = payload.get("supersedes_action_id")
        supersedes_action_id = (
            str(supersedes_value).strip()
            if supersedes_value is not None
            else None
        )

        self.timeline_actions_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        with self.lock:
            try:
                descriptor = os.open(self.timeline_actions_path, flags, 0o640)
            except OSError as exc:
                raise InputError(
                    f"timeline action log를 열 수 없습니다: {exc}"
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                actions = self.timeline_actions()
                decisions = self.decisions()
                effective, _history, human_annotation_ids = (
                    self._resolved_reviews(decisions=decisions, actions=actions)
                )

                if client_request_id:
                    for existing_action in actions:
                        if (
                            existing_action.get("client_request_id")
                            != client_request_id
                        ):
                            continue
                        retry_annotation_id = str(
                            existing_action["annotation_id"]
                        )
                        retry_request = self._timeline_action_semantic_request(
                            operation=operation,
                            annotation_id=retry_annotation_id,
                            candidate_id=candidate_id,
                            candidate_sha256=candidate_sha256,
                            supersedes_action_id=supersedes_action_id,
                            client_request_id=client_request_id,
                            review_status=review_status,
                            reviewer_id=reviewer_id,
                            notes=notes,
                            fields=fields,
                        )
                        if existing_action.get("request_sha256") == sha256_value(
                            retry_request
                        ):
                            return {
                                "ok": True,
                                "idempotent": True,
                                "action": existing_action,
                                "state": self.state(),
                            }
                        raise ConflictError(
                            "같은 client_request_id에 다른 내용이 이미 "
                            "기록되어 있습니다."
                        )

                if operation == "create_annotation":
                    annotation_id = self._next_annotation_id(
                        event_type=fields["event_type"],
                        actions=actions,
                    )
                    if annotation_id in effective:
                        raise ConflictError(
                            "새 annotation ID가 기존 이벤트와 충돌합니다."
                        )
                elif operation == "revise_annotation":
                    if annotation_id not in human_annotation_ids:
                        raise InputError(
                            "사람이 추가한 annotation만 revise_annotation으로 "
                            "수정할 수 있습니다."
                        )
                    previous_fields = effective[annotation_id][
                        "adjudicated_fields"
                    ]
                    if fields["event_type"] != previous_fields["event_type"]:
                        raise InputError("event_type은 수정할 수 없습니다.")

                current_active = effective.get(annotation_id)
                current_active_id = (
                    self._review_record_id(current_active)
                    if current_active is not None
                    else None
                )
                if operation == "create_annotation":
                    if supersedes_action_id is not None:
                        raise InputError(
                            "새 이벤트는 이전 판정을 대체할 수 없습니다."
                        )
                elif current_active_id != supersedes_action_id:
                    raise ConflictError(
                        "정정 대상이 최신 판정과 다릅니다. 새로고침 후 "
                        "현재 판정을 다시 선택해 주세요."
                    )

                semantic_request = self._timeline_action_semantic_request(
                    operation=operation,
                    annotation_id=annotation_id,
                    candidate_id=candidate_id,
                    candidate_sha256=candidate_sha256,
                    supersedes_action_id=supersedes_action_id,
                    client_request_id=client_request_id,
                    review_status=review_status,
                    reviewer_id=reviewer_id,
                    notes=notes,
                    fields=fields,
                )
                request_sha256 = sha256_value(semantic_request)
                for existing_action in actions:
                    if existing_action.get("request_sha256") == request_sha256:
                        return {
                            "ok": True,
                            "idempotent": True,
                            "action": existing_action,
                            "state": self.state(),
                        }

                if expected_revision != self.revision():
                    raise ConflictError(
                        "다른 판정이 먼저 추가되었습니다. 새로고침 후 "
                        "다시 검토해 주세요."
                    )

                action = {
                    "schema": TIMELINE_ACTION_SCHEMA,
                    "case_id": self.case_id,
                    "action_id": self._next_action_id(self.case_id, actions),
                    "operation": operation,
                    "annotation_id": annotation_id,
                    "candidate_id": candidate_id,
                    "candidate_sha256": candidate_sha256,
                    "supersedes_action_id": supersedes_action_id,
                    "client_request_id": client_request_id,
                    "request_sha256": request_sha256,
                    "review_status": review_status,
                    "resulting_label_origin": (
                        "human_video_review"
                        if review_status == "confirmed"
                        else None
                    ),
                    "adjudicated_fields": fields,
                    "review": {
                        "reviewer_kind": "human",
                        "reviewer_id": reviewer_id,
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "notes": notes,
                    },
                }
                data = (canonical_json(action) + "\n").encode("utf-8")
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise InputError(
                            "timeline action log에 전체 레코드를 기록하지 "
                            "못했습니다."
                        )
                    offset += written
                os.fsync(descriptor)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

            return {
                "ok": True,
                "idempotent": False,
                "action": action,
                "state": self.state(),
            }

    def save_decision(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise InputError("요청 본문은 JSON 객체여야 합니다.")
        candidate_id = str(payload.get("candidate_id", "")).strip()
        candidate = self._candidate_by_id(candidate_id)
        candidate_sha256 = str(payload.get("candidate_sha256", "")).strip()
        expected_candidate_sha = candidate["_review_ui"]["candidate_sha256"]
        if candidate_sha256 != expected_candidate_sha:
            raise ConflictError(
                "후보 내용이 바뀌었습니다. 새로고침 후 다시 검토해 주세요."
            )
        review_status = str(payload.get("review_status", ""))
        if review_status not in REVIEW_STATUSES:
            raise InputError("검토 결과가 올바르지 않습니다.")
        reviewer_id = str(payload.get("reviewer_id", "")).strip()
        if not reviewer_id:
            raise InputError("검토자 ID를 입력해 주세요.")
        notes = str(payload.get("notes", "")).strip()
        fields = self._validated_fields(payload.get("adjudicated_fields"))
        semantic_request = {
            "candidate_id": candidate_id,
            "candidate_sha256": expected_candidate_sha,
            "review_status": review_status,
            "reviewer_id": reviewer_id,
            "notes": notes,
            "adjudicated_fields": fields,
        }
        request_sha256 = sha256_value(semantic_request)
        expected_revision = str(payload.get("revision", ""))

        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        with self.lock:
            try:
                descriptor = os.open(self.decisions_path, flags, 0o640)
            except OSError as exc:
                raise InputError(
                    f"decision log를 열 수 없습니다: {exc}"
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                existing = self.decisions()
                for decision in existing:
                    if decision.get("candidate_id") != candidate_id:
                        continue
                    if decision.get("request_sha256") == request_sha256:
                        return {
                            "ok": True,
                            "idempotent": True,
                            "decision": decision,
                            "state": self.state(),
                        }
                    raise ConflictError(
                        "이 후보에는 이미 다른 판정이 기록되어 있습니다. "
                        "append-only 기록은 덮어쓸 수 없습니다."
                    )

                current_revision = self.revision()
                if expected_revision != current_revision:
                    raise ConflictError(
                        "다른 판정이 먼저 추가되었습니다. 새로고침 후 "
                        "다시 검토해 주세요."
                    )

                decision = {
                    "schema": "taskplanner.human_review_decision.v1",
                    "case_id": self.case_id,
                    "decision_id": f"{self.case_id}-H{len(existing) + 1:04d}",
                    "candidate_id": candidate_id,
                    "candidate_sha256": expected_candidate_sha,
                    "request_sha256": request_sha256,
                    "review_status": review_status,
                    "resulting_label_origin": (
                        "human_video_review"
                        if review_status == "confirmed"
                        else None
                    ),
                    "adjudicated_fields": fields,
                    "review": {
                        "reviewer_kind": "human",
                        "reviewer_id": reviewer_id,
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "notes": notes,
                    },
                }
                data = (canonical_json(decision) + "\n").encode("utf-8")
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise InputError(
                            "decision log에 전체 레코드를 기록하지 못했습니다."
                        )
                    offset += written
                os.fsync(descriptor)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

            return {
                "ok": True,
                "idempotent": False,
                "decision": decision,
                "state": self.state(),
            }


def _read_next_record(reader: Any) -> tuple[str, bytes, int]:
    read_next = getattr(reader, "read_next_ext", None) or reader.read_next
    result = read_next()
    if len(result) < 3:
        raise FrameError(f"예상하지 못한 rosbag record shape: {len(result)}")
    return str(result[0]), result[1], int(result[2])


def _close_reader(reader: Any) -> None:
    close = getattr(reader, "close", None)
    if close is not None:
        close()


class RosbagFrameSource:
    """Reads the exact Nth compressed-image message for CAM4 or FLIR."""

    def __init__(
        self,
        *,
        bag_dir: Path,
        cam4_timestamps_sec: list[float],
    ) -> None:
        self.bag_dir = bag_dir.resolve()
        self.cam4_timestamps_sec = cam4_timestamps_sec
        self.lock = threading.RLock()
        self._topic_timestamps: dict[str, list[int]] = {}

    @staticmethod
    def _ros_modules() -> tuple[Any, Any, Any]:
        try:
            import rosbag2_py
            from rclpy.serialization import deserialize_message
            from sensor_msgs.msg import CompressedImage
        except ImportError as exc:
            raise FrameError(
                "ROS 2 Python 환경을 찾지 못했습니다. ROS setup을 source한 "
                "터미널에서 작업대를 실행해 주세요."
            ) from exc
        return rosbag2_py, deserialize_message, CompressedImage

    def _open_reader(self, topic: str) -> Any:
        rosbag2_py, _, _ = self._ros_modules()
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(
                uri=str(self.bag_dir),
                storage_id="mcap",
            ),
            rosbag2_py.ConverterOptions("cdr", "cdr"),
        )
        reader.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
        return reader

    def _timestamps(self, view: str) -> list[int]:
        with self.lock:
            cached = self._topic_timestamps.get(view)
            if cached is not None:
                return cached
            topic = VIEW_TOPICS.get(view)
            if topic is None:
                raise FrameError(f"지원하지 않는 view: {view}")
            reader = self._open_reader(topic)
            timestamps: list[int] = []
            try:
                while reader.has_next():
                    record_topic, _payload, timestamp_ns = _read_next_record(reader)
                    if record_topic == topic:
                        timestamps.append(timestamp_ns)
            finally:
                _close_reader(reader)
            if not timestamps:
                raise FrameError(f"{view} 원본 프레임이 없습니다.")
            if view == "cam4":
                if len(timestamps) != len(self.cam4_timestamps_sec):
                    raise FrameError(
                        "CAM4 timeline frame_count와 원본 메시지 수가 다릅니다."
                    )
                origin = timestamps[0]
                for index, expected in enumerate(self.cam4_timestamps_sec):
                    actual = (timestamps[index] - origin) / 1_000_000_000
                    if abs(actual - expected) > 5e-10:
                        raise FrameError(
                            f"CAM4 timeline 불일치: frame {index}, "
                            f"{expected} != {actual}"
                        )
            self._topic_timestamps[view] = timestamps
            return timestamps

    @lru_cache(maxsize=128)
    def frame(self, view: str, source_frame_idx: int) -> tuple[bytes, str, int]:
        with self.lock:
            timestamps = self._timestamps(view)
            if not 0 <= source_frame_idx < len(timestamps):
                raise FrameError(
                    f"{view} source_frame_idx 범위 밖: {source_frame_idx}"
                )
            target_ns = timestamps[source_frame_idx]
            topic = VIEW_TOPICS[view]
            reader = self._open_reader(topic)
            try:
                reader.seek(target_ns)
                while reader.has_next():
                    record_topic, payload, actual_ns = _read_next_record(reader)
                    if record_topic != topic or actual_ns < target_ns:
                        continue
                    if actual_ns != target_ns:
                        raise FrameError(
                            f"{view} exact frame을 찾지 못했습니다: "
                            f"index={source_frame_idx}"
                        )
                    _, deserialize_message, compressed_image = self._ros_modules()
                    message = deserialize_message(payload, compressed_image)
                    image_format = str(getattr(message, "format", "")).lower()
                    content_type = (
                        "image/png" if "png" in image_format else "image/jpeg"
                    )
                    return bytes(message.data), content_type, actual_ns
            finally:
                _close_reader(reader)
        raise FrameError(
            f"{view} exact frame을 찾지 못했습니다: index={source_frame_idx}"
        )


@dataclass(frozen=True)
class ReviewCaseRuntime:
    """Immutable request-routing bundle for one independently reviewed case."""

    store: ReviewStore
    frames: RosbagFrameSource | Any
    media_path: Path | None
    media_etag: str | None
    media_paths: Mapping[str, Path]
    media_etags: Mapping[str, str]
    composite_media_path: Path | None
    composite_media_etag: str | None
    final_review: FinalReviewBundle | None
    default_review_mode: str
    clinical_store: ClinicalReviewStore | None = None

    @classmethod
    def build(
        cls,
        *,
        store: ReviewStore,
        frames: RosbagFrameSource | Any,
        media_path: Path | None = None,
        media_paths: Mapping[str, Path] | None = None,
        composite_media_path: Path | None = None,
        final_review: FinalReviewBundle | None = None,
        default_review_mode: str = "edit",
        clinical_store: ClinicalReviewStore | None = None,
    ) -> "ReviewCaseRuntime":
        if default_review_mode not in REVIEW_MODES:
            raise InputError(
                f"지원하지 않는 default review mode: {default_review_mode}"
            )
        if default_review_mode in FINAL_REVIEW_MODES and final_review is None:
            raise InputError(
                "final default review mode에는 final manifest가 필요합니다."
            )
        if final_review is not None and final_review.case_id != store.case_id:
            raise InputError("edit store와 final review case_id가 다릅니다.")
        if (
            clinical_store is not None
            and clinical_store.case_id != store.case_id
        ):
            raise InputError("edit store와 clinical review case_id가 다릅니다.")
        resolved_media_paths: dict[str, Path] = {}
        if media_paths is not None:
            unknown_views = set(media_paths) - set(REVIEW_MEDIA_VIEWS)
            if unknown_views:
                raise InputError(
                    "지원하지 않는 검토 영상 view: "
                    + ", ".join(sorted(unknown_views))
                )
            resolved_media_paths.update(
                {
                    view: path.resolve()
                    for view, path in media_paths.items()
                }
            )
        resolved_media_path = (
            media_path.resolve() if media_path is not None else None
        )
        if resolved_media_path is not None:
            existing_master = resolved_media_paths.get("cam4")
            if (
                existing_master is not None
                and existing_master != resolved_media_path
            ):
                raise InputError("CAM4 master 영상 경로가 서로 다릅니다.")
            resolved_media_paths["cam4"] = resolved_media_path
        elif "cam4" in resolved_media_paths:
            resolved_media_path = resolved_media_paths["cam4"]
        if len(resolved_media_paths) > 1 and set(resolved_media_paths) != set(
            REVIEW_MEDIA_VIEWS
        ):
            raise InputError(
                "독립 다중 영상은 CAM4, FLIR, CAM2, CAM3가 모두 필요합니다."
            )
        resolved_media_etags = {
            view: f'"{sha256_file(path)}"'
            for view, path in resolved_media_paths.items()
            if path.is_file()
        }
        media_etag = resolved_media_etags.get("cam4")
        resolved_composite_media_path = (
            composite_media_path.resolve()
            if composite_media_path is not None
            else (
                resolved_media_path
                if set(resolved_media_paths) != set(REVIEW_MEDIA_VIEWS)
                else None
            )
        )
        composite_media_etag = (
            f'"{sha256_file(resolved_composite_media_path)}"'
            if (
                resolved_composite_media_path is not None
                and resolved_composite_media_path.is_file()
            )
            else None
        )
        return cls(
            store=store,
            frames=frames,
            media_path=resolved_media_path,
            media_etag=media_etag,
            media_paths=MappingProxyType(resolved_media_paths),
            media_etags=MappingProxyType(resolved_media_etags),
            composite_media_path=resolved_composite_media_path,
            composite_media_etag=composite_media_etag,
            final_review=final_review,
            default_review_mode=default_review_mode,
            clinical_store=clinical_store,
        )

    @property
    def media_available(self) -> bool:
        return bool(self.media_paths) and all(
            path.is_file() and view in self.media_etags
            for view, path in self.media_paths.items()
        )

    @property
    def multiview_available(self) -> bool:
        return self.media_available and set(self.media_paths) == set(
            REVIEW_MEDIA_VIEWS
        )

    @property
    def composite_media_available(self) -> bool:
        return (
            self.composite_media_path is not None
            and self.composite_media_path.is_file()
            and self.composite_media_etag is not None
        )

    def descriptor(self) -> dict[str, Any]:
        final_count = 0
        if self.final_review is not None:
            final_count = len(self.final_review.dt_reference)
        return {
            "case_id": self.store.case_id,
            "label": self.store.case_id,
            "media_available": self.media_available,
            "multiview_available": self.multiview_available,
            "composite_media_available": self.composite_media_available,
            "final_review_available": self.final_review is not None,
            "final_dt_event_count": final_count,
            "frame_count": len(self.store.timestamps),
            "duration_sec": self.store.media_duration_sec,
            "clinical_review_available": (
                self.clinical_store is not None
                and self.clinical_store.available
            ),
        }


def _case_sort_key(case_id: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"(.*?)([0-9]+)", case_id)
    if match is None:
        return case_id, -1, case_id
    return match.group(1), int(match.group(2)), case_id


def make_handler(
    *,
    store: ReviewStore,
    frames: RosbagFrameSource,
    static_dir: Path,
    media_path: Path | None = None,
    final_review: FinalReviewBundle | None = None,
    default_review_mode: str = "edit",
    case_runtimes: Mapping[str, ReviewCaseRuntime] | None = None,
    default_case_id: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    if case_runtimes is None:
        runtime = ReviewCaseRuntime.build(
            store=store,
            frames=frames,
            media_path=media_path,
            final_review=final_review,
            default_review_mode=default_review_mode,
        )
        runtimes = {store.case_id: runtime}
    else:
        runtimes = dict(case_runtimes)
        if not runtimes:
            raise InputError("case catalog가 비어 있습니다.")
        for case_id, runtime in runtimes.items():
            if not CASE_ID_PATTERN.fullmatch(case_id):
                raise InputError(f"case catalog ID가 올바르지 않습니다: {case_id}")
            if runtime.store.case_id != case_id:
                raise InputError(
                    f"case catalog key와 store case_id가 다릅니다: {case_id}"
                )
    ordered_case_ids = sorted(runtimes, key=_case_sort_key)
    selected_default_case_id = default_case_id or store.case_id
    if selected_default_case_id not in runtimes:
        raise InputError(
            f"default case가 catalog에 없습니다: {selected_default_case_id}"
        )
    multi_case = len(runtimes) > 1
    case_descriptors = [
        runtimes[case_id].descriptor() for case_id in ordered_case_ids
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "TimelineHumanReview/2.0"

        def log_message(self, format: str, *args: object) -> None:
            print(f"[interaction-review] {self.address_string()} {format % args}")

        def _runtime(
            self,
            parsed: Any,
            *,
            require_explicit: bool = False,
        ) -> ReviewCaseRuntime:
            query = parse_qs(parsed.query, keep_blank_values=True)
            case_values = query.get("case", [])
            if len(case_values) > 1:
                raise InputError("case query는 하나만 지정할 수 있습니다.")
            if require_explicit and multi_case and not case_values:
                raise InputError(
                    "다중 영상 서버의 쓰기 요청에는 case query가 필요합니다."
                )
            case_id = (
                str(case_values[0]).strip()
                if case_values
                else selected_default_case_id
            )
            if not CASE_ID_PATTERN.fullmatch(case_id):
                raise InputError("case query 형식이 올바르지 않습니다.")
            runtime = runtimes.get(case_id)
            if runtime is None:
                raise CaseNotFoundError(
                    f"등록되지 않은 검수 영상입니다: {case_id}"
                )
            return runtime

        @staticmethod
        def _case_api_url(path: str, case_id: str) -> str:
            if not multi_case:
                return path
            return f"{path}?{urlencode({'case': case_id})}"

        def _base_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                "media-src 'self' blob:",
            )

        def send_json(
            self,
            value: Any,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._base_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _state_with_review_modes(
            self,
            runtime: ReviewCaseRuntime,
        ) -> dict[str, Any]:
            state = runtime.store.state()
            case_id = runtime.store.case_id
            media = state.get("media")
            if isinstance(media, dict) and runtime.media_available:
                media["video_url"] = self._case_api_url(
                    "/api/media/review.mp4",
                    case_id,
                )
                media["master_view"] = "cam4"
                media["multiview_available"] = runtime.multiview_available
                media["video_views"] = {
                    view: {
                        "video_url": self._case_api_url(
                            f"/api/media/{view}.mp4",
                            case_id,
                        ),
                        "has_audio": view == "cam4",
                    }
                    for view in REVIEW_MEDIA_VIEWS
                    if view in runtime.media_paths
                }
            state.update(
                {
                    "active_case_id": case_id,
                    "available_cases": case_descriptors,
                    "case_selector_enabled": multi_case,
                    "final_review_available": runtime.final_review is not None,
                    "final_review_url": (
                        self._case_api_url("/api/final-review", case_id)
                        if runtime.final_review is not None
                        else None
                    ),
                    "default_review_mode": runtime.default_review_mode,
                }
            )
            return state

        def _clinical_state(
            self,
            runtime: ReviewCaseRuntime,
        ) -> dict[str, Any]:
            case_id = runtime.store.case_id
            if runtime.clinical_store is None:
                state: dict[str, Any] = {
                    "ok": True,
                    "schema": "taskplanner.clinical_review_state.v2",
                    "case_id": case_id,
                    "available": False,
                    "revision": sha256_value(
                        {
                            "schema": "taskplanner.clinical_review_state.v2",
                            "case_id": case_id,
                            "available": False,
                        }
                    ),
                    "manifest": None,
                    "manifest_sha256": None,
                    "candidate_source": {
                        "status": "missing",
                        "file": (
                            "clinical_candidates.codex_5_6_sol.v2.jsonl"
                        ),
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
                        "schema": "taskplanner.clinical_reference.v2",
                        "ready": False,
                        "review_complete": False,
                        "materialized": False,
                        "file": "clinical_reference.final.v2.jsonl",
                        "record_count": 0,
                        "sha256": None,
                        "records": [],
                        "preview_records": [],
                        "excludes_rejected": True,
                        "authority": (
                            "human_reviewed_ai_draft_not_automatic_ground_truth"
                        ),
                    },
                    "policy": {
                        "write_api_enabled": False,
                        "candidate_files_immutable": True,
                        "candidate_sha256_required": True,
                        "optimistic_revision_required": True,
                        "append_only_actions": True,
                        "review_statuses": list(REVIEW_STATUSES),
                        "annotation_schema": (
                            "taskplanner.clinical_video_annotation.v2"
                        ),
                        "editable_annotation_fields": [
                            "observation",
                            "interpretation",
                        ],
                        "interpretation_requires_human_review": True,
                        "annotation_kind_enabled": False,
                        "separate_unobservable_type": False,
                        "candidate_authority": "ai_draft",
                        "confirmed_is_automatic_ground_truth": False,
                        "reference_is_separately_derived": True,
                    },
                }
            else:
                state = runtime.clinical_store.state()

            media: dict[str, Any] = {
                "available": runtime.media_available,
                "multiview_available": runtime.multiview_available,
                "composite_media_available": (
                    runtime.composite_media_available
                ),
                "default_view": "flir",
                "composite_video_url": (
                    self._case_api_url(
                        "/api/media/composite.mp4",
                        case_id,
                    )
                    if runtime.composite_media_available
                    else None
                ),
                "video_views": {},
                "composite_layout": (
                    {
                        "kind": "cam4_flir_side_by_side",
                        "source_width": 1280,
                        "source_height": 360,
                        "flir_crop": {
                            "x": 640,
                            "y": 0,
                            "width": 640,
                            "height": 360,
                        },
                    }
                    if runtime.composite_media_available
                    else None
                ),
            }
            if runtime.media_available:
                media["video_views"] = {
                    view: {
                        "video_url": self._case_api_url(
                            f"/api/media/{view}.mp4",
                            case_id,
                        ),
                        "has_audio": view == "cam4",
                    }
                    for view in REVIEW_MEDIA_VIEWS
                    if view in runtime.media_paths
                }
            state.update(
                {
                    "active_case_id": case_id,
                    "available_cases": case_descriptors,
                    "case_selector_enabled": multi_case,
                    "media": media,
                    "context_api": {
                        "timeline_state_url": self._case_api_url(
                            "/api/state",
                            case_id,
                        ),
                        "final_review_url": (
                            self._case_api_url(
                                "/api/final-review",
                                case_id,
                            )
                            if runtime.final_review is not None
                            else None
                        ),
                    },
                }
            )
            return state

        def _request_review_mode(
            self,
            parsed: Any,
            runtime: ReviewCaseRuntime,
        ) -> str:
            query = parse_qs(parsed.query)
            query_mode = query.get("review_mode", [None])[0]
            header_mode = self.headers.get("X-Review-Mode")
            value = query_mode or header_mode or runtime.default_review_mode
            mode = str(value).strip()
            if mode not in REVIEW_MODES:
                raise InputError(f"지원하지 않는 review_mode: {mode}")
            return mode

        def _reject_read_only_final_review(self) -> None:
            body = json.dumps(
                {
                    "ok": False,
                    "code": "read_only_final_review",
                    "error": "최종 검수 모드는 읽기 전용입니다.",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self._base_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            self.wfile.write(body)

        def _send_media(
            self,
            runtime: ReviewCaseRuntime,
            *,
            view: str = "cam4",
            head_only: bool,
        ) -> None:
            if view == "composite":
                resolved_media_path = runtime.composite_media_path
                media_etag = runtime.composite_media_etag
            else:
                resolved_media_path = runtime.media_paths.get(view)
                media_etag = runtime.media_etags.get(view)
            if (
                resolved_media_path is None
                or not resolved_media_path.is_file()
                or media_etag is None
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            size = resolved_media_path.stat().st_size
            try:
                byte_range = parse_single_byte_range(
                    self.headers.get("Range"),
                    size=size,
                )
            except InputError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._base_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if byte_range is None:
                start, end = 0, size - 1
                status = HTTPStatus.OK
            else:
                start, end = byte_range
                status = HTTPStatus.PARTIAL_CONTENT
            content_length = end - start + 1
            self.send_response(status)
            self._base_headers()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", media_etag)
            self.send_header("Cache-Control", "private, max-age=3600")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{size}",
                )
            self.end_headers()
            if head_only:
                return
            remaining = content_length
            with resolved_media_path.open("rb") as stream:
                stream.seek(start)
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

        def _handle_get_or_head(self, *, head_only: bool) -> None:
            parsed = urlparse(self.path)
            media_match = re.fullmatch(
                r"/api/media/(review|composite|cam4|flir|cam2|cam3)\.mp4",
                parsed.path,
            )
            if media_match is not None:
                requested_view = media_match.group(1)
                view = "cam4" if requested_view == "review" else requested_view
                try:
                    self._send_media(
                        self._runtime(parsed),
                        view=view,
                        head_only=head_only,
                    )
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                except InputError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return
            if head_only:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/cases":
                self.send_json(
                    {
                        "ok": True,
                        "default_case_id": selected_default_case_id,
                        "case_count": len(case_descriptors),
                        "cases": case_descriptors,
                    }
                )
                return
            if parsed.path == "/api/health":
                try:
                    runtime = self._runtime(parsed)
                    self.send_json(
                        {
                            "ok": True,
                            "case_id": runtime.store.case_id,
                            "case_count": len(case_descriptors),
                            "candidate_source_status": (
                                runtime.store.candidate_source_status
                            ),
                            "media_available": (
                                runtime.media_available
                            ),
                            "multiview_available": (
                                runtime.multiview_available
                            ),
                            "composite_media_available": (
                                runtime.composite_media_available
                            ),
                            "final_review_available": (
                                runtime.final_review is not None
                            ),
                            "clinical_review_available": (
                                runtime.clinical_store is not None
                                and runtime.clinical_store.available
                            ),
                            "default_review_mode": (
                                runtime.default_review_mode
                            ),
                        }
                    )
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                except InputError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return
            if parsed.path == "/api/state":
                try:
                    self.send_json(
                        self._state_with_review_modes(self._runtime(parsed))
                    )
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                except InputError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return
            if parsed.path == "/api/clinical-review":
                try:
                    self.send_json(
                        self._clinical_state(self._runtime(parsed))
                    )
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                except ClinicalConflictError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.CONFLICT,
                    )
                except (InputError, ClinicalInputError) as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return
            if parsed.path == "/api/final-review":
                try:
                    runtime = self._runtime(parsed)
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                except InputError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                if runtime.final_review is None:
                    self.send_json(
                        {
                            "ok": False,
                            "error": "최종 검수 reference가 설정되지 않았습니다.",
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                else:
                    self.send_json(runtime.final_review.state())
                return
            if parsed.path == "/api/frame":
                try:
                    runtime = self._runtime(parsed)
                    query = parse_qs(parsed.query)
                    view = query.get("view", [""])[0]
                    source_frame_idx = int(
                        query.get("source_frame_idx", ["-1"])[0]
                    )
                    data, content_type, timestamp_ns = runtime.frames.frame(
                        view,
                        source_frame_idx,
                    )
                    self.send_response(HTTPStatus.OK)
                    self._base_headers()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header(
                        "X-Source-Frame-Idx",
                        str(source_frame_idx),
                    )
                    self.send_header(
                        "X-View-Timestamp-Ns",
                        str(timestamp_ns),
                    )
                    self.send_header("Cache-Control", "private, max-age=60")
                    self.end_headers()
                    self.wfile.write(data)
                except CaseNotFoundError as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.NOT_FOUND,
                    )
                except (FrameError, InputError, ValueError) as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return

            requested = (
                "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            )
            target = (static_dir / requested).resolve()
            static_root = static_dir.resolve()
            if target != static_root and static_root not in target.parents:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            content_type = (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            self.send_response(HTTPStatus.OK)
            self._base_headers()
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._handle_get_or_head(head_only=False)

        def do_HEAD(self) -> None:
            self._handle_get_or_head(head_only=True)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path not in (
                "/api/decision",
                "/api/annotation-action",
                "/api/clinical-action",
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                runtime = self._runtime(parsed, require_explicit=True)
                if (
                    path != "/api/clinical-action"
                    and
                    self._request_review_mode(parsed, runtime)
                    in FINAL_REVIEW_MODES
                ):
                    self._reject_read_only_final_review()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512_000:
                    raise InputError("요청 본문 크기가 올바르지 않습니다.")
                payload = json.loads(
                    self.rfile.read(length),
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(
                            f"비표준 JSON 숫자는 허용되지 않습니다: {value}"
                        )
                    ),
                )
                if not isinstance(payload, dict):
                    raise InputError("요청 본문은 JSON object여야 합니다.")
                payload_case_id = payload.get("case_id")
                if (
                    multi_case
                    and payload_case_id != runtime.store.case_id
                ):
                    raise InputError(
                        "쓰기 요청 본문의 case_id가 선택한 영상과 다릅니다."
                    )
                if (
                    payload_case_id is not None
                    and payload_case_id != runtime.store.case_id
                ):
                    raise InputError(
                        "쓰기 요청 본문의 case_id가 선택한 영상과 다릅니다."
                    )
                if (
                    path != "/api/clinical-action"
                    and
                    payload.get("review_mode") in FINAL_REVIEW_MODES
                ):
                    self._reject_read_only_final_review()
                    return
                if path == "/api/decision":
                    result = runtime.store.save_decision(payload)
                elif path == "/api/annotation-action":
                    result = runtime.store.save_timeline_action(payload)
                else:
                    if runtime.clinical_store is None:
                        raise ClinicalInputError(
                            "이 case에는 임상 검수 저장소가 없습니다."
                        )
                    result = runtime.clinical_store.save_action(payload)
                if isinstance(result, dict) and isinstance(
                    result.get("state"),
                    dict,
                ):
                    result["state"] = (
                        self._clinical_state(runtime)
                        if path == "/api/clinical-action"
                        else self._state_with_review_modes(runtime)
                    )
                self.send_json(result)
            except CaseNotFoundError as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
            except (ConflictError, ClinicalConflictError) as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.CONFLICT,
                )
            except (
                InputError,
                ClinicalInputError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )

        def _reject_non_post_write_method(self) -> None:
            try:
                parsed = urlparse(self.path)
                runtime = self._runtime(parsed, require_explicit=True)
                if (
                    self._request_review_mode(parsed, runtime)
                    in FINAL_REVIEW_MODES
                ):
                    self._reject_read_only_final_review()
                    return
            except CaseNotFoundError as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except InputError as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_PUT(self) -> None:
            self._reject_non_post_write_method()

        def do_PATCH(self) -> None:
            self._reject_non_post_write_method()

        def do_DELETE(self) -> None:
            self._reject_non_post_write_method()

    return Handler


def validate_review_media_proxy(
    *,
    media_path: Path,
    case_id: str,
    timeline_path: Path,
    source_bag: Path,
    annotation_manifest: Mapping[str, Any],
) -> float:
    """Fail closed when a cached review proxy is stale or belongs elsewhere."""

    resolved_media = media_path.resolve()
    sidecar = resolved_media.with_suffix(resolved_media.suffix + ".manifest.json")
    if not resolved_media.is_file():
        raise InputError(f"검수 영상 proxy가 없습니다: {resolved_media}")
    if not sidecar.is_file():
        raise InputError(f"검수 영상 proxy manifest가 없습니다: {sidecar}")
    proxy_manifest = load_json_object(sidecar)
    if proxy_manifest.get("schema") != "taskplanner.review_media_proxy_manifest.v1":
        raise InputError(f"검수 영상 proxy schema가 다릅니다: {sidecar}")
    if proxy_manifest.get("case_id") != case_id:
        raise InputError(f"검수 영상 proxy case_id가 다릅니다: {sidecar}")

    inputs = proxy_manifest.get("inputs")
    output = proxy_manifest.get("output")
    if not isinstance(inputs, dict) or not isinstance(output, dict):
        raise InputError(f"검수 영상 proxy manifest 구조가 잘못되었습니다: {sidecar}")
    timeline_input = inputs.get("timeline")
    source_mcap_input = inputs.get("source_mcap")
    if not isinstance(timeline_input, dict) or not isinstance(
        source_mcap_input,
        dict,
    ):
        raise InputError(f"검수 영상 proxy 입력 정보가 없습니다: {sidecar}")
    resolved_timeline = timeline_path.resolve()
    if Path(str(timeline_input.get("path", ""))).resolve() != resolved_timeline:
        raise InputError(f"검수 영상 proxy timeline 경로가 다릅니다: {sidecar}")
    if timeline_input.get("sha256") != sha256_file(resolved_timeline):
        raise InputError(f"검수 영상 proxy timeline 해시가 다릅니다: {sidecar}")

    source_descriptor = annotation_manifest.get("source_bag")
    if not isinstance(source_descriptor, dict):
        raise InputError("annotation manifest source_bag 정보가 없습니다.")
    manifest_bag_dir = Path(str(source_descriptor.get("directory", ""))).resolve()
    if manifest_bag_dir != source_bag.resolve():
        raise InputError("annotation manifest와 timeline의 source bag이 다릅니다.")
    expected_mcap = (
        manifest_bag_dir / str(source_descriptor.get("mcap_file", ""))
    ).resolve()
    if Path(str(source_mcap_input.get("path", ""))).resolve() != expected_mcap:
        raise InputError(f"검수 영상 proxy MCAP 경로가 다릅니다: {sidecar}")
    if source_mcap_input.get("sha256") != source_descriptor.get("mcap_sha256"):
        raise InputError(f"검수 영상 proxy MCAP 해시가 다릅니다: {sidecar}")

    if Path(str(output.get("path", ""))).resolve() != resolved_media:
        raise InputError(f"검수 영상 proxy 출력 경로가 다릅니다: {sidecar}")
    expected_size = output.get("size_bytes")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size != resolved_media.stat().st_size
    ):
        raise InputError(f"검수 영상 proxy 크기가 다릅니다: {sidecar}")
    if output.get("sha256") != sha256_file(resolved_media):
        raise InputError(f"검수 영상 proxy 해시가 다릅니다: {sidecar}")
    media_probe = output.get("media_probe")
    if not isinstance(media_probe, dict):
        raise InputError(f"검수 영상 proxy probe 정보가 없습니다: {sidecar}")
    duration = media_probe.get("container_duration_sec")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) <= 0
    ):
        raise InputError(f"검수 영상 proxy 길이가 올바르지 않습니다: {sidecar}")
    return float(duration)


def validate_review_multiview_proxy(
    *,
    manifest_path: Path,
    case_id: str,
    timeline_path: Path,
    source_bag: Path,
    annotation_manifest: Mapping[str, Any],
) -> tuple[float, dict[str, Path]]:
    """Validate four independent, CAM4-timeline-aligned review streams."""

    resolved_manifest = manifest_path.resolve()
    if not resolved_manifest.is_file():
        raise InputError(
            f"독립 다중 검토 영상 manifest가 없습니다: {resolved_manifest}"
        )
    proxy_manifest = load_json_object(resolved_manifest)
    if (
        proxy_manifest.get("schema")
        != "taskplanner.review_multiview_proxy_manifest.v1"
    ):
        raise InputError(
            f"독립 다중 검토 영상 schema가 다릅니다: {resolved_manifest}"
        )
    if proxy_manifest.get("case_id") != case_id:
        raise InputError(
            f"독립 다중 검토 영상 case_id가 다릅니다: {resolved_manifest}"
        )
    if proxy_manifest.get("master_view") != "cam4":
        raise InputError(
            f"독립 다중 검토 영상 master가 CAM4가 아닙니다: {resolved_manifest}"
        )
    if proxy_manifest.get("view_order") != list(REVIEW_MEDIA_VIEWS):
        raise InputError(
            f"독립 다중 검토 영상 view 순서가 다릅니다: {resolved_manifest}"
        )

    inputs = proxy_manifest.get("inputs")
    outputs = proxy_manifest.get("outputs")
    if not isinstance(inputs, dict) or not isinstance(outputs, dict):
        raise InputError(
            f"독립 다중 검토 영상 manifest 구조가 잘못되었습니다: "
            f"{resolved_manifest}"
        )
    timeline_input = inputs.get("timeline")
    source_mcap_input = inputs.get("source_mcap")
    if not isinstance(timeline_input, dict) or not isinstance(
        source_mcap_input,
        dict,
    ):
        raise InputError(
            f"독립 다중 검토 영상 입력 정보가 없습니다: {resolved_manifest}"
        )
    resolved_timeline = timeline_path.resolve()
    if Path(str(timeline_input.get("path", ""))).resolve() != resolved_timeline:
        raise InputError(
            f"독립 다중 검토 영상 timeline 경로가 다릅니다: "
            f"{resolved_manifest}"
        )
    if timeline_input.get("sha256") != sha256_file(resolved_timeline):
        raise InputError(
            f"독립 다중 검토 영상 timeline 해시가 다릅니다: "
            f"{resolved_manifest}"
        )

    source_descriptor = annotation_manifest.get("source_bag")
    if not isinstance(source_descriptor, dict):
        raise InputError("annotation manifest source_bag 정보가 없습니다.")
    manifest_bag_dir = Path(str(source_descriptor.get("directory", ""))).resolve()
    if manifest_bag_dir != source_bag.resolve():
        raise InputError("annotation manifest와 timeline의 source bag이 다릅니다.")
    expected_mcap = (
        manifest_bag_dir / str(source_descriptor.get("mcap_file", ""))
    ).resolve()
    if Path(str(source_mcap_input.get("path", ""))).resolve() != expected_mcap:
        raise InputError(
            f"독립 다중 검토 영상 MCAP 경로가 다릅니다: "
            f"{resolved_manifest}"
        )
    if source_mcap_input.get("sha256") != source_descriptor.get("mcap_sha256"):
        raise InputError(
            f"독립 다중 검토 영상 MCAP 해시가 다릅니다: "
            f"{resolved_manifest}"
        )

    resolved_outputs: dict[str, Path] = {}
    master_duration: float | None = None
    for view in REVIEW_MEDIA_VIEWS:
        output = outputs.get(view)
        if not isinstance(output, dict):
            raise InputError(
                f"독립 다중 검토 영상 {view} 출력 정보가 없습니다: "
                f"{resolved_manifest}"
            )
        expected_path = (
            resolved_manifest.parent / f"review_{view}.mp4"
        ).resolve()
        output_path = Path(str(output.get("path", ""))).resolve()
        if output_path != expected_path:
            raise InputError(
                f"독립 다중 검토 영상 {view} 출력 경로가 다릅니다: "
                f"{resolved_manifest}"
            )
        if not output_path.is_file():
            raise InputError(
                f"독립 다중 검토 영상 {view} 파일이 없습니다: {output_path}"
            )
        expected_size = output.get("size_bytes")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size != output_path.stat().st_size
        ):
            raise InputError(
                f"독립 다중 검토 영상 {view} 크기가 다릅니다: "
                f"{resolved_manifest}"
            )
        if output.get("sha256") != sha256_file(output_path):
            raise InputError(
                f"독립 다중 검토 영상 {view} 해시가 다릅니다: "
                f"{resolved_manifest}"
            )
        media_probe = output.get("media_probe")
        if not isinstance(media_probe, dict):
            raise InputError(
                f"독립 다중 검토 영상 {view} probe 정보가 없습니다: "
                f"{resolved_manifest}"
            )
        duration = media_probe.get("container_duration_sec")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0
        ):
            raise InputError(
                f"독립 다중 검토 영상 {view} 길이가 올바르지 않습니다: "
                f"{resolved_manifest}"
            )
        if view == "cam4":
            if output.get("has_audio") is not True:
                raise InputError(
                    f"CAM4 master 검토 영상에 음성이 없습니다: "
                    f"{resolved_manifest}"
                )
            master_duration = float(duration)
        elif output.get("has_audio") is not False:
            raise InputError(
                f"{view} follower 영상의 audio 표시가 잘못되었습니다: "
                f"{resolved_manifest}"
            )
        resolved_outputs[view] = output_path

    assert master_duration is not None
    return master_duration, resolved_outputs


def build_default_case_runtime(
    *,
    case_dir: Path,
    review_media_root: Path | None,
    default_review_mode: str,
) -> ReviewCaseRuntime:
    """Build one catalog entry from canonical case-local artifacts."""

    resolved_case_dir = case_dir.resolve()
    case_id = resolved_case_dir.name
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise InputError(f"case directory 이름이 올바르지 않습니다: {case_id}")
    timeline_path = resolved_case_dir / "cam4_frame_timeline.v1.json"
    annotation_manifest_path = resolved_case_dir / "annotation_manifest.json"
    if not timeline_path.is_file():
        raise InputError(f"case timeline이 없습니다: {timeline_path}")
    if not annotation_manifest_path.is_file():
        raise InputError(
            f"case annotation manifest가 없습니다: {annotation_manifest_path}"
        )
    timeline = load_json_object(timeline_path)
    if timeline.get("case_id") != case_id:
        raise InputError(f"timeline case_id가 다릅니다: {timeline_path}")
    annotation_manifest = load_json_object(annotation_manifest_path)
    if annotation_manifest.get("case_id") != case_id:
        raise InputError(
            f"annotation manifest case_id가 다릅니다: {annotation_manifest_path}"
        )
    declared_source_bag = Path(
        str(timeline.get("source_bag", ""))
    ).resolve()
    source_bag = resolve_source_bag_directory(
        declared_source_bag=declared_source_bag,
        case_id=case_id,
        annotation_manifest=annotation_manifest,
    )

    review_media_path: Path | None = None
    review_media_paths: dict[str, Path] = {}
    composite_media_path: Path | None = None
    media_duration_sec: float | None = None
    if review_media_root is not None:
        resolved_media_root = review_media_root.resolve()
        media_case_dir = (resolved_media_root / case_id).resolve()
        if media_case_dir.parent != resolved_media_root:
            raise InputError("review media case 경로가 root를 벗어났습니다.")
        multiview_manifest_path = (
            media_case_dir / "review_multiview.manifest.json"
        )
        if multiview_manifest_path.is_file():
            media_duration_sec, review_media_paths = (
                validate_review_multiview_proxy(
                    manifest_path=multiview_manifest_path,
                    case_id=case_id,
                    timeline_path=timeline_path,
                    source_bag=declared_source_bag,
                    annotation_manifest=annotation_manifest,
                )
            )
            review_media_path = review_media_paths["cam4"]
            corrected_media_path = media_case_dir / "review_corrected.mp4"
            corrected_manifest_path = corrected_media_path.with_suffix(
                corrected_media_path.suffix + ".manifest.json"
            )
            if (
                corrected_media_path.exists()
                or corrected_manifest_path.exists()
            ):
                validate_review_media_proxy(
                    media_path=corrected_media_path,
                    case_id=case_id,
                    timeline_path=timeline_path,
                    source_bag=declared_source_bag,
                    annotation_manifest=annotation_manifest,
                )
                composite_media_path = corrected_media_path
        else:
            review_media_path = media_case_dir / "review_corrected.mp4"
            media_duration_sec = validate_review_media_proxy(
                media_path=review_media_path,
                case_id=case_id,
                timeline_path=timeline_path,
                source_bag=declared_source_bag,
                annotation_manifest=annotation_manifest,
            )
            composite_media_path = review_media_path
    if media_duration_sec is None:
        duration_value = annotation_manifest.get("duration_sec")
        if isinstance(duration_value, (int, float)) and not isinstance(
            duration_value,
            bool,
        ):
            media_duration_sec = float(duration_value)

    candidates_path = (
        resolved_case_dir / "interaction_candidates.ai_review.v1.jsonl"
    )
    phase_candidates_path = (
        resolved_case_dir / "phase_candidates.ai_review.v1.jsonl"
    )
    store = ReviewStore(
        case_dir=resolved_case_dir,
        candidates_path=candidates_path,
        timeline_path=timeline_path,
        decisions_path=resolved_case_dir / "human_review_decisions.v1.jsonl",
        timeline_actions_path=(
            resolved_case_dir / "human_timeline_actions.v1.jsonl"
        ),
        additional_candidates_paths=[phase_candidates_path],
        review_media_path=review_media_path,
        media_duration_sec=media_duration_sec,
        stream_kind="timeline",
    )
    observable_root = resolved_case_dir.parent.parent
    if (
        resolved_case_dir.parent.name == "cases"
        and observable_root.name == "observable_tool_events"
    ):
        clinical_case_dir = (
            observable_root.parent / "clinical_video" / "cases" / case_id
        )
    else:
        clinical_case_dir = resolved_case_dir / "clinical_video"
    try:
        clinical_store = ClinicalReviewStore(
            case_dir=clinical_case_dir,
            case_id=case_id,
            source_timeline=timeline,
            source_timeline_path=timeline_path,
        )
    except ClinicalInputError as exc:
        raise InputError(f"clinical review 로드 실패: {exc}") from exc
    final_review = FinalReviewBundle(
        manifest_path=annotation_manifest_path,
        expected_timeline_path=timeline_path,
    )
    frames = RosbagFrameSource(
        bag_dir=source_bag,
        cam4_timestamps_sec=store.timestamps,
    )
    return ReviewCaseRuntime.build(
        store=store,
        frames=frames,
        media_path=review_media_path,
        media_paths=review_media_paths or None,
        composite_media_path=composite_media_path,
        final_review=final_review,
        default_review_mode=default_review_mode,
        clinical_store=clinical_store,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review 0704 interaction AI proposals against exact CAM4/FLIR "
            "source-frame indices."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--case-dir", type=Path)
    source_group.add_argument(
        "--cases-root",
        type=Path,
        help=(
            "Serve a validated catalog of case directories from one process. "
            "Use --case-id to restrict discovery."
        ),
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Case ID to expose from --cases-root; repeat for multiple cases.",
    )
    parser.add_argument(
        "--default-case",
        help="Initial case for a multi-case catalog; defaults to the last case.",
    )
    parser.add_argument(
        "--review-media-root",
        type=Path,
        help=(
            "Validated proxy root containing "
            "CASE_ID/review_corrected.mp4 and its manifest."
        ),
    )
    parser.add_argument("--source-bag", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument(
        "--phase-candidates",
        type=Path,
        help="Optional phase-start candidate JSONL merged into the timeline.",
    )
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--timeline-actions", type=Path)
    parser.add_argument(
        "--review-media",
        type=Path,
        help="Corrected bag-time H.264/AAC MP4 served with HTTP Range.",
    )
    parser.add_argument(
        "--media-duration-sec",
        type=float,
        help="Full playback duration including audio-only tail.",
    )
    parser.add_argument(
        "--stream-kind",
        choices=sorted(STREAM_EVENT_TYPES),
        default="timeline",
    )
    parser.add_argument(
        "--final-manifest",
        type=Path,
        help=(
            "Optional finalized annotation manifest. When supplied, its "
            "observed and DT references are exposed read-only."
        ),
    )
    parser.add_argument(
        "--default-review-mode",
        choices=REVIEW_MODES,
        default="edit",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8878)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.cases_root is not None:
        single_case_options = {
            "--source-bag": args.source_bag,
            "--candidates": args.candidates,
            "--phase-candidates": args.phase_candidates,
            "--timeline": args.timeline,
            "--decisions": args.decisions,
            "--timeline-actions": args.timeline_actions,
            "--review-media": args.review_media,
            "--media-duration-sec": args.media_duration_sec,
            "--final-manifest": args.final_manifest,
        }
        incompatible = [
            name for name, value in single_case_options.items() if value is not None
        ]
        if incompatible:
            raise SystemExit(
                "--cases-root와 함께 사용할 수 없는 단일-case 옵션: "
                + ", ".join(incompatible)
            )
        if args.stream_kind != "timeline":
            raise SystemExit("--cases-root는 --stream-kind timeline만 지원합니다.")
        cases_root = args.cases_root.resolve()
        if not cases_root.is_dir():
            raise SystemExit(f"cases root directory가 없습니다: {cases_root}")
        if args.case_ids:
            case_ids = list(dict.fromkeys(args.case_ids))
        else:
            case_ids = [
                path.name
                for path in cases_root.iterdir()
                if path.is_dir()
                and CASE_ID_PATTERN.fullmatch(path.name)
                and (path / "cam4_frame_timeline.v1.json").is_file()
                and (path / "annotation_manifest.json").is_file()
            ]
        case_ids = sorted(case_ids, key=_case_sort_key)
        if not case_ids:
            raise SystemExit("선택 가능한 검수 case가 없습니다.")
        review_media_root = (
            args.review_media_root.resolve()
            if args.review_media_root is not None
            else None
        )
        runtimes: dict[str, ReviewCaseRuntime] = {}
        for case_id in case_ids:
            if not CASE_ID_PATTERN.fullmatch(case_id):
                raise SystemExit(f"case ID 형식이 올바르지 않습니다: {case_id}")
            case_dir = (cases_root / case_id).resolve()
            if case_dir.parent != cases_root:
                raise SystemExit(f"case 경로가 cases root를 벗어났습니다: {case_id}")
            try:
                runtimes[case_id] = build_default_case_runtime(
                    case_dir=case_dir,
                    review_media_root=review_media_root,
                    default_review_mode=args.default_review_mode,
                )
            except InputError as exc:
                raise SystemExit(f"{case_id} catalog 로드 실패: {exc}") from exc

        default_case_id = args.default_case or case_ids[-1]
        if default_case_id not in runtimes:
            raise SystemExit(
                f"default case가 선택한 catalog에 없습니다: {default_case_id}"
            )
        default_runtime = runtimes[default_case_id]
        static_dir = Path(__file__).with_name("web_interaction_review")
        server = ThreadingHTTPServer(
            (args.host, args.port),
            make_handler(
                store=default_runtime.store,
                frames=default_runtime.frames,
                static_dir=static_dir,
                media_path=default_runtime.media_path,
                final_review=default_runtime.final_review,
                default_review_mode=default_runtime.default_review_mode,
                case_runtimes=runtimes,
                default_case_id=default_case_id,
            ),
        )
        print(
            f"Timeline review GUI: http://{args.host}:{args.port}/ "
            f"(cases={','.join(case_ids)}, default_case={default_case_id}, "
            f"media_ready={sum(runtime.media_path is not None for runtime in runtimes.values())}/"
            f"{len(runtimes)}, final_ready="
            f"{sum(runtime.final_review is not None for runtime in runtimes.values())}/"
            f"{len(runtimes)}, default_mode={args.default_review_mode})",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.case_ids or args.default_case or args.review_media_root:
        raise SystemExit(
            "--case-id/--default-case/--review-media-root는 "
            "--cases-root와 함께 사용해야 합니다."
        )
    assert args.case_dir is not None
    case_dir = args.case_dir.resolve()
    timeline_path = (
        args.timeline.resolve()
        if args.timeline
        else case_dir / "cam4_frame_timeline.v1.json"
    )
    candidates_path = (
        args.candidates.resolve()
        if args.candidates
        else case_dir / "interaction_candidates.ai_review.v1.jsonl"
    )
    decisions_path = (
        args.decisions.resolve()
        if args.decisions
        else case_dir / "human_review_decisions.v1.jsonl"
    )
    timeline_actions_path = (
        args.timeline_actions.resolve()
        if args.timeline_actions
        else case_dir / "human_timeline_actions.v1.jsonl"
    )
    additional_candidates_paths: list[Path] = []
    phase_candidates_path = (
        args.phase_candidates.resolve()
        if args.phase_candidates
        else case_dir / "phase_candidates.ai_review.v1.jsonl"
    )
    if args.stream_kind == "timeline":
        additional_candidates_paths.append(phase_candidates_path)
    review_media_path = (
        args.review_media.resolve() if args.review_media else None
    )
    media_duration_sec = args.media_duration_sec
    if media_duration_sec is None:
        manifest_path = case_dir / "annotation_manifest.json"
        if manifest_path.is_file():
            manifest = load_json_object(manifest_path)
            duration_value = manifest.get("duration_sec")
            if isinstance(duration_value, (int, float)):
                media_duration_sec = float(duration_value)
    store = ReviewStore(
        case_dir=case_dir,
        candidates_path=candidates_path,
        timeline_path=timeline_path,
        decisions_path=decisions_path,
        timeline_actions_path=timeline_actions_path,
        additional_candidates_paths=additional_candidates_paths,
        review_media_path=review_media_path,
        media_duration_sec=media_duration_sec,
        stream_kind=args.stream_kind,
    )
    final_review = (
        FinalReviewBundle(
            manifest_path=args.final_manifest.resolve(),
            expected_timeline_path=timeline_path,
        )
        if args.final_manifest
        else None
    )
    if args.default_review_mode in FINAL_REVIEW_MODES and final_review is None:
        raise SystemExit(
            "--default-review-mode final_observed/final_dt에는 "
            "--final-manifest가 필요합니다."
        )
    timeline_source = Path(str(store.timeline.get("source_bag", ""))).resolve()
    source_bag = args.source_bag.resolve() if args.source_bag else timeline_source
    if source_bag != timeline_source:
        raise SystemExit(
            "source bag does not match cam4_frame_timeline.v1.json"
        )
    if not source_bag.is_dir():
        raise SystemExit(f"source bag directory does not exist: {source_bag}")

    static_dir = Path(__file__).with_name("web_interaction_review")
    frames = RosbagFrameSource(
        bag_dir=source_bag,
        cam4_timestamps_sec=store.timestamps,
    )
    observable_root = case_dir.parent.parent
    if (
        case_dir.parent.name == "cases"
        and observable_root.name == "observable_tool_events"
    ):
        clinical_case_dir = (
            observable_root.parent
            / "clinical_video"
            / "cases"
            / store.case_id
        )
    else:
        clinical_case_dir = case_dir / "clinical_video"
    try:
        clinical_store = ClinicalReviewStore(
            case_dir=clinical_case_dir,
            case_id=store.case_id,
            source_timeline=store.timeline,
            source_timeline_path=timeline_path,
        )
    except ClinicalInputError as exc:
        raise SystemExit(f"clinical review 로드 실패: {exc}") from exc
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            store=store,
            frames=frames,
            static_dir=static_dir,
            media_path=review_media_path,
            final_review=final_review,
            default_review_mode=args.default_review_mode,
            case_runtimes={
                store.case_id: ReviewCaseRuntime.build(
                    store=store,
                    frames=frames,
                    media_path=review_media_path,
                    final_review=final_review,
                    default_review_mode=args.default_review_mode,
                    clinical_store=clinical_store,
                )
            },
            default_case_id=store.case_id,
        ),
    )
    print(
        f"Timeline review GUI: http://{args.host}:{args.port}/ "
        f"(case={store.case_id}, candidates={len(store.candidates())}, "
        f"media={'ready' if review_media_path and review_media_path.is_file() else 'missing'}, "
        f"final={'ready' if final_review is not None else 'disabled'}, "
        f"default_mode={args.default_review_mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
