#!/usr/bin/env python3
"""Build browser-safe RF-DETR overlay payloads from read-only artifacts.

The source reconstruction JSONL remains authoritative and immutable.  This
importer validates frame/timestamp identity against the annotation timeline,
then publishes only the fields required to draw boxes in the review UI.  Model
paths, NAS paths, segmentation RLE, and other heavyweight provenance are not
exposed to the browser.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any


DEFAULT_RFDETR_ROOT = Path(
    "/mnt/arl/NAS관리/백업/업무/ARPA-H/SurgeryData/갑상샘/0704_RFDETR"
)
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMELINE_ROOT = (
    WORKSPACE_ROOT / "annotations/observable_tool_events/cases"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).with_name("web_interaction_review") / "rfdetr_overlays"
)

SOURCE_SCHEMA = "arpa_h_rfdetr_frame_instances_v1"
OUTPUT_SCHEMA = "taskplanner.rfdetr_overlay_bundle.v1"
INDEX_SCHEMA = "taskplanner.rfdetr_overlay_index.v1"
AUTHORITY = "ai_inference_reference_not_ground_truth"
VIEWS = ("cam4", "flir")
SOURCE_CASE_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z0-9-]+)_(?P<suffix>[0-9]{2})$")


class OverlayImportError(RuntimeError):
    """The RF-DETR source cannot be safely mapped into the review timeline."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OverlayImportError(f"JSON을 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise OverlayImportError(f"JSON object가 아닙니다: {path}")
    return value


def app_case_id(source_case_id: str) -> str:
    match = SOURCE_CASE_PATTERN.fullmatch(source_case_id)
    if match is None:
        raise OverlayImportError(
            f"RF-DETR case ID 형식이 올바르지 않습니다: {source_case_id}"
        )
    return f"{match.group('prefix')}_{int(match.group('suffix'))}"


def source_case_id(case_id: str) -> str:
    match = re.fullmatch(
        r"(?P<prefix>[A-Za-z0-9-]+)_(?P<suffix>[0-9]+)",
        case_id,
    )
    if match is None:
        raise OverlayImportError(
            f"어노테이션 case ID 형식이 올바르지 않습니다: {case_id}"
        )
    return f"{match.group('prefix')}_{int(match.group('suffix')):02d}"


def even_proxy_content_rect(
    source_width: int,
    source_height: int,
    *,
    proxy_width: int = 640,
    proxy_height: int = 360,
) -> list[int]:
    """Match the review proxy's FFmpeg scale+pad transform."""

    scale = min(proxy_width / source_width, proxy_height / source_height)
    scaled_width = max(2, int(math.floor(source_width * scale / 2.0) * 2))
    scaled_height = max(2, int(math.floor(source_height * scale / 2.0) * 2))
    scaled_width = min(proxy_width, scaled_width)
    scaled_height = min(proxy_height, scaled_height)
    return [
        (proxy_width - scaled_width) // 2,
        (proxy_height - scaled_height) // 2,
        scaled_width,
        scaled_height,
    ]


def validate_timestamp_mapping(
    *,
    source_case_dir: Path,
    timeline_path: Path,
    expected_frame_count: int,
) -> tuple[str, list[int]]:
    timestamp_path = source_case_dir / "ros_image_timestamps.json"
    source = load_json_object(timestamp_path)
    if source.get("schema") != "arpa_h_ros_image_timestamps_v1":
        raise OverlayImportError(
            f"지원하지 않는 timestamp schema입니다: {timestamp_path}"
        )
    bag_timestamps = source.get("bag_timestamps_ns")
    if not isinstance(bag_timestamps, dict):
        raise OverlayImportError(
            f"bag_timestamps_ns가 없습니다: {timestamp_path}"
        )
    cam4 = bag_timestamps.get("cam4")
    flir = bag_timestamps.get("flir")
    if not isinstance(cam4, list) or not isinstance(flir, list):
        raise OverlayImportError(
            f"CAM4/FLIR timestamp 배열이 없습니다: {timestamp_path}"
        )
    if cam4 != flir:
        raise OverlayImportError(
            f"CAM4와 FLIR timestamp가 일치하지 않습니다: {timestamp_path}"
        )
    if len(cam4) != expected_frame_count:
        raise OverlayImportError(
            f"timestamp frame 수가 다릅니다: {timestamp_path}"
        )

    timeline = load_json_object(timeline_path)
    timeline_values = timeline.get("timestamps_sec")
    if not isinstance(timeline_values, list):
        raise OverlayImportError(
            f"어노테이션 timeline timestamp가 없습니다: {timeline_path}"
        )
    if timeline.get("frame_count") != expected_frame_count:
        raise OverlayImportError(
            f"어노테이션 timeline frame 수가 다릅니다: {timeline_path}"
        )
    if len(timeline_values) != expected_frame_count:
        raise OverlayImportError(
            f"어노테이션 timestamp 개수가 다릅니다: {timeline_path}"
        )
    for index, (time_sec, timestamp_ns) in enumerate(
        zip(timeline_values, cam4, strict=True)
    ):
        if (
            not isinstance(time_sec, (int, float))
            or not math.isfinite(float(time_sec))
            or not isinstance(timestamp_ns, int)
            or round(float(time_sec) * 1_000_000_000) != timestamp_ns
        ):
            raise OverlayImportError(
                "RF-DETR과 어노테이션 timeline timestamp가 "
                f"frame {index}에서 일치하지 않습니다."
            )
    return sha256_file(timestamp_path), cam4


def video_relative_timestamp_ns(frame_index: int, fps: float) -> int:
    """Reproduce the reconstruction exporter's exact CFR timestamp rounding."""

    fps_fraction = Fraction(str(fps)).limit_denominator(1_000_000)
    numerator = frame_index * 1_000_000_000 * fps_fraction.denominator
    return (numerator + fps_fraction.numerator // 2) // fps_fraction.numerator


def normalized_instance(
    value: Any,
    *,
    width: int,
    height: int,
    source: Path,
    frame_index: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OverlayImportError(
            f"instance 형식이 올바르지 않습니다: {source}:{frame_index}"
        )
    class_id = value.get("class_id")
    class_name = value.get("class_name")
    confidence = value.get("confidence")
    bbox = value.get("bbox_xyxy")
    tracker_id = value.get("tracker_id")
    if not isinstance(class_id, int) or isinstance(class_id, bool):
        raise OverlayImportError(
            f"class_id가 올바르지 않습니다: {source}:{frame_index}"
        )
    if not isinstance(class_name, str) or not class_name.strip():
        raise OverlayImportError(
            f"class_name이 올바르지 않습니다: {source}:{frame_index}"
        )
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise OverlayImportError(
            f"confidence가 올바르지 않습니다: {source}:{frame_index}"
        )
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise OverlayImportError(
            f"bbox가 올바르지 않습니다: {source}:{frame_index}"
        )
    coordinates: list[float] = []
    for coordinate in bbox:
        if (
            not isinstance(coordinate, (int, float))
            or isinstance(coordinate, bool)
            or not math.isfinite(float(coordinate))
        ):
            raise OverlayImportError(
                f"bbox 좌표가 올바르지 않습니다: {source}:{frame_index}"
            )
        coordinates.append(float(coordinate))
    x_min, y_min, x_max, y_max = coordinates
    if not (
        0.0 <= x_min <= x_max <= width
        and 0.0 <= y_min <= y_max <= height
    ):
        raise OverlayImportError(
            f"bbox가 source 범위를 벗어났습니다: {source}:{frame_index}"
        )
    if tracker_id is not None and (
        not isinstance(tracker_id, int) or isinstance(tracker_id, bool)
    ):
        raise OverlayImportError(
            f"tracker_id가 올바르지 않습니다: {source}:{frame_index}"
        )

    result: dict[str, Any] = {
        "class_id": class_id,
        "class_name": class_name.strip(),
        "confidence": round(float(confidence), 6),
        "bbox_xyxy": [round(coordinate, 3) for coordinate in coordinates],
    }
    if tracker_id is not None:
        result["tracker_id"] = tracker_id
    return result


def load_view(
    *,
    source_case_dir: Path,
    view: str,
    expected_frame_count: int,
    expected_timestamps_ns: list[int],
) -> dict[str, Any]:
    view_dir = source_case_dir / view / "reconstruction"
    manifest_path = view_dir / "manifest.json"
    data_path = view_dir / "instances.jsonl.gz"
    manifest = load_json_object(manifest_path)
    if (
        manifest.get("schema") != SOURCE_SCHEMA
        or manifest.get("status") != "complete"
        or manifest.get("view") != view
    ):
        raise OverlayImportError(
            f"완료된 {view} reconstruction manifest가 아닙니다: {manifest_path}"
        )
    video = manifest.get("video")
    if not isinstance(video, dict):
        raise OverlayImportError(f"video metadata가 없습니다: {manifest_path}")
    width = video.get("width")
    height = video.get("height")
    fps = video.get("fps")
    frame_count = video.get("declared_frames")
    if (
        not isinstance(width, int)
        or width <= 0
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(fps, (int, float))
        or isinstance(fps, bool)
        or not math.isfinite(float(fps))
        or float(fps) <= 0
        or frame_count != expected_frame_count
        or len(expected_timestamps_ns) != expected_frame_count
    ):
        raise OverlayImportError(
            f"source video metadata가 timeline과 다릅니다: {manifest_path}"
        )
    expected_sha256 = manifest.get("data_file_sha256")
    if (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or sha256_file(data_path) != expected_sha256
    ):
        raise OverlayImportError(
            f"reconstruction SHA-256이 일치하지 않습니다: {data_path}"
        )

    frames: list[list[dict[str, Any]]] = []
    instance_count = 0
    class_counts: dict[str, int] = {}
    try:
        with gzip.open(data_path, "rt", encoding="utf-8") as stream:
            for expected_index, line in enumerate(stream):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OverlayImportError(
                        f"JSONL frame을 읽을 수 없습니다: {data_path}"
                    ) from exc
                if (
                    not isinstance(record, dict)
                    or record.get("frame_index") != expected_index
                ):
                    raise OverlayImportError(
                        f"frame index가 연속적이지 않습니다: {data_path}"
                    )
                if (
                    record.get("rosbag_timestamp_ns")
                    != expected_timestamps_ns[expected_index]
                ):
                    raise OverlayImportError(
                        "reconstruction과 canonical timeline timestamp가 "
                        f"frame {expected_index}에서 일치하지 않습니다: "
                        f"{data_path}"
                    )
                if (
                    record.get("video_relative_timestamp_ns")
                    != video_relative_timestamp_ns(expected_index, float(fps))
                ):
                    raise OverlayImportError(
                        "reconstruction의 video relative timestamp가 "
                        f"frame {expected_index}에서 올바르지 않습니다: "
                        f"{data_path}"
                    )
                instances = record.get("instances")
                if not isinstance(instances, list):
                    raise OverlayImportError(
                        f"instances가 배열이 아닙니다: {data_path}:{expected_index}"
                    )
                frame_instances = [
                    normalized_instance(
                        instance,
                        width=width,
                        height=height,
                        source=data_path,
                        frame_index=expected_index,
                    )
                    for instance in instances
                ]
                frames.append(frame_instances)
                instance_count += len(frame_instances)
                for instance in frame_instances:
                    class_name = instance["class_name"]
                    class_counts[class_name] = class_counts.get(class_name, 0) + 1
    except OSError as exc:
        raise OverlayImportError(
            f"gzip reconstruction을 읽을 수 없습니다: {data_path}"
        ) from exc
    if len(frames) != expected_frame_count:
        raise OverlayImportError(
            f"reconstruction frame 수가 다릅니다: {data_path}"
        )

    export = manifest.get("export")
    model = export.get("model") if isinstance(export, dict) else None
    return {
        "source_width": width,
        "source_height": height,
        "frame_count": len(frames),
        "instance_count": instance_count,
        "classes": sorted(class_counts),
        "class_counts": dict(sorted(class_counts.items())),
        "model": model if isinstance(model, str) else "RF-DETR",
        "source_sha256": expected_sha256,
        "continuous_proxy": {
            "width": 640,
            "height": 360,
            "content_rect": even_proxy_content_rect(width, height),
        },
        "frames": frames,
    }


def atomic_write_json(path: Path, value: Any, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
                sort_keys=pretty,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def import_case(
    *,
    source_root: Path,
    timeline_root: Path,
    output_dir: Path,
    case_id: str,
) -> dict[str, Any]:
    dataset_case_id = source_case_id(case_id)
    source_case_dir = source_root / "cases" / dataset_case_id
    timeline_path = timeline_root / case_id / "cam4_frame_timeline.v1.json"
    if not source_case_dir.is_dir():
        raise OverlayImportError(
            f"RF-DETR case 폴더가 없습니다: {source_case_dir}"
        )
    if not timeline_path.is_file():
        raise OverlayImportError(
            f"어노테이션 timeline이 없습니다: {timeline_path}"
        )
    timeline = load_json_object(timeline_path)
    frame_count = timeline.get("frame_count")
    if not isinstance(frame_count, int) or frame_count <= 0:
        raise OverlayImportError(
            f"timeline frame_count가 올바르지 않습니다: {timeline_path}"
        )
    timestamp_sha256, expected_timestamps_ns = validate_timestamp_mapping(
        source_case_dir=source_case_dir,
        timeline_path=timeline_path,
        expected_frame_count=frame_count,
    )
    views = {
        view: load_view(
            source_case_dir=source_case_dir,
            view=view,
            expected_frame_count=frame_count,
            expected_timestamps_ns=expected_timestamps_ns,
        )
        for view in VIEWS
    }
    payload = {
        "schema": OUTPUT_SCHEMA,
        "case_id": case_id,
        "dataset_case_id": dataset_case_id,
        "authority": AUTHORITY,
        "read_only": True,
        "frame_index_mapping": "source_frame_idx_one_to_one_zero_based",
        "frame_count": frame_count,
        "timestamp_sha256": timestamp_sha256,
        "views": views,
    }
    output_path = output_dir / f"{case_id}.json"
    atomic_write_json(output_path, payload)
    return {
        "case_id": case_id,
        "dataset_case_id": dataset_case_id,
        "data_url": f"/rfdetr_overlays/{case_id}.json",
        "frame_count": frame_count,
        "authority": AUTHORITY,
        "views": {
            view: {
                key: views[view][key]
                for key in (
                    "source_width",
                    "source_height",
                    "instance_count",
                    "classes",
                    "model",
                )
            }
            for view in VIEWS
        },
        "payload_bytes": output_path.stat().st_size,
        "payload_sha256": sha256_file(output_path),
    }


def discover_case_ids(source_root: Path) -> list[str]:
    cases_root = source_root / "cases"
    if not cases_root.is_dir():
        raise OverlayImportError(f"RF-DETR cases 폴더가 없습니다: {cases_root}")
    result = []
    for path in cases_root.iterdir():
        if path.is_dir() and SOURCE_CASE_PATTERN.fullmatch(path.name):
            result.append(app_case_id(path.name))
    return sorted(
        result,
        key=lambda value: (
            value.rsplit("_", 1)[0],
            int(value.rsplit("_", 1)[1]),
        ),
    )


def build_overlays(
    *,
    source_root: Path,
    timeline_root: Path,
    output_dir: Path,
    case_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    resolved_source_root = source_root.resolve(strict=True)
    resolved_timeline_root = timeline_root.resolve(strict=True)
    selected = list(case_ids or discover_case_ids(resolved_source_root))
    if not selected:
        raise OverlayImportError("가져올 RF-DETR case가 없습니다.")
    cases = [
        import_case(
            source_root=resolved_source_root,
            timeline_root=resolved_timeline_root,
            output_dir=output_dir,
            case_id=case_id,
        )
        for case_id in selected
    ]
    index = {
        "schema": INDEX_SCHEMA,
        "authority": AUTHORITY,
        "read_only": True,
        "case_count": len(cases),
        "cases": cases,
    }
    atomic_write_json(output_dir / "index.json", index, pretty=True)
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RF-DETR 결과를 검수 UI용 읽기 전용 bbox로 가져옵니다."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_RFDETR_ROOT,
        help="0704_RFDETR 루트",
    )
    parser.add_argument(
        "--timeline-root",
        type=Path,
        default=DEFAULT_TIMELINE_ROOT,
        help="어노테이션 case timeline 루트",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="브라우저에 제공할 정규화 payload 폴더",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="가져올 앱 case ID. 반복 가능하며 생략하면 전체를 처리합니다.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        index = build_overlays(
            source_root=args.source_root,
            timeline_root=args.timeline_root,
            output_dir=args.output_dir,
            case_ids=args.case_ids,
        )
    except (OSError, OverlayImportError) as exc:
        raise SystemExit(f"RF-DETR overlay import 실패: {exc}") from exc
    total_instances = sum(
        view["instance_count"]
        for case in index["cases"]
        for view in case["views"].values()
    )
    print(
        "RF-DETR overlay import 완료: "
        f"{index['case_count']} cases, {total_instances} instances, "
        f"{args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
