"""Shared contracts for Taskplanner shadow replay and offline evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


TRACE_SCHEMA = "taskplanner.shadow_trace.v1"
RUN_MANIFEST_SCHEMA = "taskplanner.shadow_run_manifest.v1"
RUN_MODES = {"strict", "reconciled", "oracle"}
GROUND_TRUTH_PREFIX = "/evaluation/ground_truth"

TRACE_LAYERS = {
    "input_image",
    "input_transcript",
    "normalized_input_image",
    "vlm_preprocessed_input_image",
    "vlm_model_input_image",
    "normalized_perception",
    "cam4_semantic_perception",
    "rfdetr_health",
    "rfdetr_diagnostics",
    "shadow_replay_state",
    "runtime_control",
    "runtime_state",
    "vlm_request",
    "vlm_health",
    "vlm_model_raw",
    "vlm_raw",
    "vlm_proposal",
    "vlm_reducer_decision",
    "reducer_event",
    "reducer_fused",
    "bt_decision",
    "skill_command",
    "skill_status",
    "skill_event",
    "shadow_sink",
    "bed_robot_arm_group_request",
    "bed_robot_arm_group_command",
    "bed_robot_arm_group_status",
    "shadow_bed_robot_arm_group_sink",
    "evaluation_observation",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_sha256(payload: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            record["_jsonl_line"] = line_number
            records.append(record)
    return records


def _resolve_case_local_file(
    *,
    case_dir: Path,
    manifest_path: Path,
    relative_path: str,
    field_name: str,
) -> Path:
    value = str(relative_path or "").strip()
    if not value:
        raise ValueError(f"{manifest_path}: {field_name} is required")
    resolved = (case_dir / value).resolve()
    if resolved.parent != case_dir.resolve():
        raise ValueError(
            f"{manifest_path}: {field_name} must stay inside the case directory"
        )
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _evaluation_reference_descriptor(
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    reference = manifest.get("evaluation_reference")
    if not isinstance(reference, dict) or reference.get("complete") is not True:
        return None
    descriptor = reference.get("dt_reference")
    if not isinstance(descriptor, dict):
        return None
    if not str(descriptor.get("file", "")).strip():
        return None
    return descriptor


def resolve_case_reference(case_dir: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = case_dir / "annotation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = _evaluation_reference_descriptor(manifest)
    if descriptor is not None:
        event_path = _resolve_case_local_file(
            case_dir=case_dir,
            manifest_path=manifest_path,
            relative_path=str(descriptor["file"]),
            field_name="evaluation_reference.dt_reference.file",
        )
        expected_sha = str(descriptor.get("sha256", "")).strip()
        if expected_sha and sha256_file(event_path) != expected_sha:
            raise ValueError(
                f"{manifest_path}: evaluation_reference.dt_reference.sha256 mismatch"
            )
        return manifest, event_path

    event_path = _resolve_case_local_file(
        case_dir=case_dir,
        manifest_path=manifest_path,
        relative_path=str(manifest.get("event_file", "")),
        field_name="event_file",
    )
    return manifest, event_path


def resolve_case_evaluation_mask(
    case_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> Path | None:
    """Resolve an optional case-local evaluation-mask sidecar.

    New minimal-interaction references keep scoring metadata out of the
    immutable observable-event schema.  The sidecar may be declared at the
    evaluation-reference level, on its DT descriptor, or by the conventional
    ``evaluation_masks.v1.json`` filename.  Legacy cases remain valid without
    a sidecar.
    """

    manifest_path = case_dir / "annotation_manifest.json"
    payload = (
        manifest
        if manifest is not None
        else json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    reference = payload.get("evaluation_reference")
    candidates: list[tuple[str, str, str]] = []
    if isinstance(reference, dict):
        descriptor = reference.get("dt_reference")
        if isinstance(descriptor, dict):
            for key in ("evaluation_mask_file", "evaluation_masks_file"):
                if str(descriptor.get(key, "")).strip():
                    candidates.append(
                        (
                            str(descriptor[key]),
                            f"evaluation_reference.dt_reference.{key}",
                            str(
                                descriptor.get(
                                    "evaluation_mask_sha256",
                                    descriptor.get("evaluation_masks_sha256", ""),
                                )
                            ).strip(),
                        )
                    )
        for key in ("evaluation_mask_file", "evaluation_masks_file"):
            if str(reference.get(key, "")).strip():
                candidates.append(
                    (
                        str(reference[key]),
                        f"evaluation_reference.{key}",
                        str(
                            reference.get(
                                "evaluation_mask_sha256",
                                reference.get("evaluation_masks_sha256", ""),
                            )
                        ).strip(),
                    )
                )
        for descriptor_key in ("evaluation_mask", "evaluation_masks"):
            mask_descriptor = reference.get(descriptor_key)
            if isinstance(mask_descriptor, dict) and str(
                mask_descriptor.get("file", "")
            ).strip():
                candidates.append(
                    (
                        str(mask_descriptor["file"]),
                        f"evaluation_reference.{descriptor_key}.file",
                        str(mask_descriptor.get("sha256", "")).strip(),
                    )
                )
    for key in ("evaluation_mask_file", "evaluation_masks_file"):
        if str(payload.get(key, "")).strip():
            candidates.append(
                (
                    str(payload[key]),
                    key,
                    str(
                        payload.get(
                            "evaluation_mask_sha256",
                            payload.get("evaluation_masks_sha256", ""),
                        )
                    ).strip(),
                )
            )

    conventional = case_dir / "evaluation_masks.v1.json"
    if not candidates:
        return conventional.resolve() if conventional.is_file() else None

    relative_path, field_name, expected_sha = candidates[0]
    resolved = _resolve_case_local_file(
        case_dir=case_dir,
        manifest_path=manifest_path,
        relative_path=relative_path,
        field_name=field_name,
    )
    if expected_sha and sha256_file(resolved) != expected_sha:
        raise ValueError(f"{manifest_path}: {field_name} sha256 mismatch")
    return resolved


def resolve_case_tool_catalog(
    case_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> Path | None:
    """Resolve the evaluation-only tool identity catalog declared by a case."""

    manifest_path = case_dir / "annotation_manifest.json"
    payload = (
        manifest
        if manifest is not None
        else json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    relative_path = str(payload.get("tool_catalog_path", "")).strip()
    if not relative_path:
        return None
    resolved = (case_dir / relative_path).resolve()
    annotation_root = case_dir.parent.parent.resolve()
    try:
        resolved.relative_to(annotation_root)
    except ValueError as exc:
        raise ValueError(
            f"{manifest_path}: tool_catalog_path must stay inside "
            "annotations/observable_tool_events"
        ) from exc
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    expected_sha = str(payload.get("tool_catalog_sha256", "")).strip()
    if expected_sha and sha256_file(resolved) != expected_sha:
        raise ValueError(f"{manifest_path}: tool_catalog_sha256 mismatch")
    return resolved


def resolve_case_phase_context(
    case_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> Path | None:
    """Resolve an explicitly included provisional, non-scoring Phase track."""

    manifest_path = case_dir / "annotation_manifest.json"
    payload = (
        manifest
        if manifest is not None
        else json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    reference = payload.get("evaluation_reference")
    if (
        not isinstance(reference, dict)
        or reference.get("complete") is not True
        or reference.get("phase_reference_included") is not True
    ):
        return None
    descriptor = reference.get("phase_reference")
    if not isinstance(descriptor, dict):
        raise ValueError(
            f"{manifest_path}: evaluation_reference.phase_reference is required"
        )
    if descriptor.get("scoring_role") != "context_only_not_ground_truth":
        raise ValueError(
            f"{manifest_path}: phase reference must be context-only"
        )
    if descriptor.get("status") != "provisional_ambiguous":
        raise ValueError(
            f"{manifest_path}: phase reference must remain provisional"
        )
    resolved = _resolve_case_local_file(
        case_dir=case_dir,
        manifest_path=manifest_path,
        relative_path=str(descriptor.get("file", "")),
        field_name="evaluation_reference.phase_reference.file",
    )
    expected_sha = str(descriptor.get("sha256", "")).strip()
    if expected_sha and sha256_file(resolved) != expected_sha:
        raise ValueError(
            f"{manifest_path}: evaluation_reference.phase_reference.sha256 mismatch"
        )
    return resolved


def validate_trace_records(records: Iterable[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    expected_sequence = 0
    run_id = ""
    mode = ""
    last_ros_time = -1.0
    for index, record in enumerate(records, 1):
        line = int(record.get("_jsonl_line", index))
        prefix = f"line {line}"
        if record.get("schema") != TRACE_SCHEMA:
            errors.append(f"{prefix}: invalid schema")
        record_run_id = str(record.get("run_id", ""))
        if not record_run_id:
            errors.append(f"{prefix}: run_id is required")
        elif run_id and record_run_id != run_id:
            errors.append(f"{prefix}: run_id changed within one trace")
        else:
            run_id = record_run_id
        record_mode = str(record.get("mode", ""))
        if record_mode not in RUN_MODES:
            errors.append(f"{prefix}: invalid mode {record_mode!r}")
        elif mode and record_mode != mode:
            errors.append(f"{prefix}: mode changed within one trace")
        else:
            mode = record_mode
        sequence = record.get("sequence")
        if sequence != expected_sequence:
            errors.append(
                f"{prefix}: sequence must be {expected_sequence}, got {sequence!r}"
            )
            if isinstance(sequence, int):
                expected_sequence = sequence
        expected_sequence += 1
        layer = str(record.get("layer", ""))
        if layer not in TRACE_LAYERS:
            errors.append(f"{prefix}: invalid layer {layer!r}")
        topic = str(record.get("topic", ""))
        if not topic.startswith("/"):
            errors.append(f"{prefix}: topic must be absolute")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"{prefix}: payload must be an object")
        elif record.get("payload_sha256") != payload_sha256(payload):
            errors.append(f"{prefix}: payload_sha256 mismatch")
        try:
            ros_time = float(record["ros_time_sec"])
            wall_time = float(record["wall_time_sec"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{prefix}: ros_time_sec and wall_time_sec must be numeric")
            continue
        if ros_time < 0.0 or wall_time < 0.0:
            errors.append(f"{prefix}: timestamps must be non-negative")
        if ros_time < last_ros_time:
            errors.append(
                f"{prefix}: ros_time_sec {ros_time} is earlier than {last_ros_time}"
            )
        last_ros_time = max(last_ros_time, ros_time)
    return errors


@dataclass(frozen=True, slots=True)
class TraceRecord:
    run_id: str
    sequence: int
    mode: str
    layer: str
    topic: str
    message_type: str
    ros_time_sec: float
    wall_time_sec: float
    payload: dict[str, Any]
    source_stamp_sec: float | None = None
    correlation_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": TRACE_SCHEMA,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "mode": self.mode,
            "layer": self.layer,
            "topic": self.topic,
            "message_type": self.message_type,
            "ros_time_sec": round(float(self.ros_time_sec), 9),
            "wall_time_sec": round(float(self.wall_time_sec), 9),
            "payload": self.payload,
            "payload_sha256": payload_sha256(self.payload),
        }
        if self.source_stamp_sec is not None:
            record["source_stamp_sec"] = round(float(self.source_stamp_sec), 9)
        if self.correlation_id:
            record["correlation_id"] = self.correlation_id
        return record


class TraceWriter:
    """Append-only JSONL writer with a deterministic in-process sequence."""

    def __init__(self, path: Path, *, run_id: str, mode: str) -> None:
        if mode not in RUN_MODES:
            raise ValueError(f"invalid shadow mode {mode!r}")
        self.path = path
        self.run_id = run_id
        self.mode = mode
        self.sequence = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8")

    def append(
        self,
        *,
        layer: str,
        topic: str,
        message_type: str,
        ros_time_sec: float,
        wall_time_sec: float,
        payload: dict[str, Any],
        source_stamp_sec: float | None = None,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        record = TraceRecord(
            run_id=self.run_id,
            sequence=self.sequence,
            mode=self.mode,
            layer=layer,
            topic=topic,
            message_type=message_type,
            ros_time_sec=ros_time_sec,
            wall_time_sec=wall_time_sec,
            payload=payload,
            source_stamp_sec=source_stamp_sec,
            correlation_id=correlation_id,
        ).as_dict()
        errors = validate_trace_records([record])
        # Single-record validation starts at sequence zero. Validate all fields
        # except global sequence continuity here; the full trace validator checks it.
        errors = [error for error in errors if "sequence must be" not in error]
        if errors:
            raise ValueError("; ".join(errors))
        self._stream.write(canonical_json(record) + "\n")
        self._stream.flush()
        self.sequence += 1
        return record

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.flush()
            self._stream.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def hashed_artifact(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}
