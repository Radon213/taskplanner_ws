#!/usr/bin/env python3
"""Audit which 0704 cases are eligible for Mayo-recognition accuracy metrics.

The audit is deliberately strict: a case is accuracy eligible only if it has a
canonical tool-event reference with confirmed CAM4 Mayo labels *and* a locally
readable CAM4 MCAP.  A missing label is never treated as a negative example.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CASES_ROOT = WORKSPACE_ROOT / "annotations/observable_tool_events/cases"
ANNOTATED_BAGS_ROOT = WORKSPACE_ROOT / "annotated_bags"
RAW_0704_VIDEO_ROOT = Path(
    os.environ.get(
        "TASKPLANNER_0704_RAW_VIDEO_ROOT",
        "/mnt/arl/NAS관리/백업/업무/ARPA-H/SurgeryData/갑상샘/0704_원본영상",
    )
)
CAM4_TOPIC = "/surgery/cam4/color/image/compressed"


class CoverageAuditError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_event_path(case_dir: Path) -> Path | None:
    promoted = case_dir / "tool_events.final.v1.jsonl"
    if promoted.is_file():
        return promoted
    draft = case_dir / "tool_events.v1.jsonl"
    return draft if draft.is_file() else None


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CoverageAuditError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise CoverageAuditError(f"non-object JSONL row at {path}:{line_number}")
        rows.append(row)
    return rows


def local_bag_candidates(case_id: str) -> list[Path]:
    candidates = sorted(ANNOTATED_BAGS_ROOT.glob(f"{case_id}*"))
    return [
        candidate
        for candidate in candidates
        if candidate.is_dir() and any(candidate.glob("*.mcap"))
    ]


def bag_has_cam4_topic(bag_dir: Path) -> bool:
    """Inspect MCAP metadata, rather than assuming every annotated bag has CAM4."""

    try:
        import rosbag2_py
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise CoverageAuditError("ROS 2 Python bindings unavailable for MCAP audit") from exc
    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(
            rosbag2_py.StorageOptions(uri=str(bag_dir.resolve()), storage_id="mcap"),
            rosbag2_py.ConverterOptions("cdr", "cdr"),
        )
        topics = reader.get_all_topics_and_types()
        return any(str(getattr(topic, "name", "")) == CAM4_TOPIC for topic in topics)
    except RuntimeError as exc:
        raise CoverageAuditError(f"cannot open local MCAP {bag_dir}: {exc}") from exc
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def select_local_cam4_bag(case_id: str) -> Path | None:
    candidates = local_bag_candidates(case_id)
    reviewed_v2 = [path for path in candidates if path.name.endswith("reviewed_gt_v2")]
    for candidate in reviewed_v2 + [path for path in candidates if path not in reviewed_v2]:
        if bag_has_cam4_topic(candidate):
            return candidate
    return None


def count_cam4_frames(bag_dir: Path) -> int:
    """Count the exact MCAP frame-index domain used by reviewed proposals."""

    try:
        import rosbag2_py
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise CoverageAuditError("ROS 2 Python bindings unavailable for MCAP audit") from exc
    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(
            rosbag2_py.StorageOptions(uri=str(bag_dir.resolve()), storage_id="mcap"),
            rosbag2_py.ConverterOptions("cdr", "cdr"),
        )
        reader.set_filter(rosbag2_py.StorageFilter(topics=[CAM4_TOPIC]))
        count = 0
        while reader.has_next():
            record = reader.read_next_ext() if hasattr(reader, "read_next_ext") else reader.read_next()
            if str(record[0]) == CAM4_TOPIC:
                count += 1
        return count
    except RuntimeError as exc:
        raise CoverageAuditError(f"cannot count CAM4 frames in {bag_dir}: {exc}") from exc
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def raw_cam4_video(case_id: str) -> Path:
    return RAW_0704_VIDEO_ROOT / case_id / "cam_4" / "rgb.avi"


def probe_raw_cam4_video(path: Path) -> dict[str, Any]:
    """Verify that a raw CAM4 video is actually decodable without copying it."""

    if not path.is_file():
        return {"available": False, "decodable": False, "frame_count": 0, "fps": 0.0}
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise CoverageAuditError("OpenCV unavailable for raw video audit") from exc
    capture = cv2.VideoCapture(str(path))
    try:
        opened = capture.isOpened()
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) if opened else 0
        fps = float(capture.get(cv2.CAP_PROP_FPS)) if opened else 0.0
        ok, _frame = capture.read() if opened else (False, None)
        return {
            "available": True,
            "decodable": bool(ok),
            "frame_count": max(0, frame_count),
            "fps": fps if fps > 0 else 0.0,
        }
    finally:
        capture.release()


def _confirmed_mayo_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("review_status") == "confirmed"
        and isinstance(row.get("to"), dict)
        and row["to"].get("location") == "mayo_stand"
        and isinstance(row.get("source_views"), list)
        and "cam4" in row["source_views"]
        and isinstance(row.get("proposal"), dict)
        and isinstance(row["proposal"].get("source_frame_idx"), int)
    ]


def audit_case(case_dir: Path) -> dict[str, Any]:
    case_id = case_dir.name
    event_path = canonical_event_path(case_dir)
    rows = read_jsonl(event_path)
    confirmed_mayo = _confirmed_mayo_rows(rows)
    local_bag = select_local_cam4_bag(case_id)
    local_frame_count = count_cam4_frames(local_bag) if local_bag else 0
    source_frame_indices = [
        int(row["proposal"]["source_frame_idx"])
        for row in confirmed_mayo
    ]
    unmapped_source_frames = [
        frame_index
        for frame_index in source_frame_indices
        if frame_index < 0 or frame_index >= local_frame_count
    ]
    exact_source_frame_mapping_valid = bool(
        confirmed_mayo and local_bag and local_frame_count and not unmapped_source_frames
    )
    raw_video = raw_cam4_video(case_id)
    raw_video_probe = probe_raw_cam4_video(raw_video)
    manifest_path = case_dir / "annotation_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded
    source_bag = manifest.get("source_bag") if isinstance(manifest.get("source_bag"), dict) else {}
    source_directory = str(source_bag.get("directory", ""))
    event_counts: dict[str, int] = {}
    for row in confirmed_mayo:
        event_type = str(row.get("event_type", ""))
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    return {
        "case_id": case_id,
        "canonical_event_reference": str(event_path) if event_path else "",
        "event_reference_sha256": sha256_file(event_path) if event_path else "",
        "all_tool_event_rows": len(rows),
        "confirmed_mayo_cam4_rows": len(confirmed_mayo),
        "confirmed_mayo_cam4_event_types": dict(sorted(event_counts.items())),
        "local_cam4_mcap": str(local_bag) if local_bag else "",
        "local_cam4_media_available": bool(local_bag),
        "local_cam4_frame_count": local_frame_count,
        "confirmed_mayo_source_frame_min": min(source_frame_indices) if source_frame_indices else None,
        "confirmed_mayo_source_frame_max": max(source_frame_indices) if source_frame_indices else None,
        "unmapped_confirmed_mayo_source_frames": unmapped_source_frames,
        "exact_source_frame_mapping_valid": exact_source_frame_mapping_valid,
        "raw_cam4_video": str(raw_video),
        "raw_cam4_video_available": raw_video_probe["available"],
        "raw_cam4_video_decodable": raw_video_probe["decodable"],
        "raw_cam4_video_frame_count": raw_video_probe["frame_count"],
        "raw_cam4_video_fps": raw_video_probe["fps"],
        "manifest_duration_sec": manifest.get("duration_sec"),
        "manifest_source_bag_present": bool(source_directory and Path(source_directory).exists()),
        # The reviewed labels are mapped to MCAP source-frame indices.  A raw
        # AVI alone is useful coverage evidence but is not enough to claim
        # exact-timestamp Mayo accuracy without a separately audited mapping.
        "accuracy_eligible": exact_source_frame_mapping_valid,
        # An unlabelled frame/case says nothing about absence, so it is never a
        # negative sample for this classifier.
        "negative_eligible": False,
        "exclusion_reason": (
            ""
            if exact_source_frame_mapping_valid
            else "no confirmed CAM4 Mayo GT with exact local MCAP mapping; excluded from all accuracy and negative metrics"
        ),
    }


def audit() -> dict[str, Any]:
    case_dirs = sorted(
        (path for path in CASES_ROOT.glob("0704_*") if path.is_dir()),
        key=lambda path: int(path.name.rsplit("_", 1)[1]),
    )
    cases = [audit_case(case_dir) for case_dir in case_dirs]
    eligible = [case["case_id"] for case in cases if case["accuracy_eligible"]]
    raw_video_covered = [
        case["case_id"] for case in cases if case["raw_cam4_video_decodable"]
    ]
    return {
        "schema": "taskplanner.mayo_coverage_audit.v1",
        "scope": "0704_5-17",
        "metric_policy": {
            "unlabelled_cases_are_negatives": False,
            "accuracy_requires_confirmed_cam4_mayo_gt_and_exact_local_mcap_mapping": True,
            "raw_cam4_video_is_coverage_only_without_audited_timestamp_mapping": True,
        },
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "accuracy_eligible_cases": eligible,
            "cross_case_holdout_possible": len(eligible) >= 2,
            "raw_cam4_video_covered_cases": raw_video_covered,
            "raw_cam4_video_covered_case_count": len(raw_video_covered),
            "excluded_case_count": len(cases) - len(eligible),
        },
    }


def write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise CoverageAuditError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = audit()
        write_new(args.output, payload)
    except (CoverageAuditError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "summary": payload["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
