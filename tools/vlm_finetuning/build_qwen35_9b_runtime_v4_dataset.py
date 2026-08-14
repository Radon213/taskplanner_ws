#!/usr/bin/env python3
"""Build a leakage-resistant Qwen3.5-9B SFT set for the live schema-v4 VLM.

The builder deliberately uses the same public contract as ``real_vlm``:

* one JPEG composed by ``compose_flir_cam4_for_model`` (FLIR left, CAM4 right),
* the actor-log system and developer prompts from the checked-out runtime,
* ``Compact context JSON`` followed by one image label and one image, and
* the complete schema-v4 JSON response.

Labels remain field-scoped.  A row may carry a complete, well-formed response,
but ``supervision_char_spans`` identifies only the reviewed/allowed fields that
the trainer is permitted to learn from.  This prevents a placeholder in one
task from silently becoming supervision for another task.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import cv2
from PIL import Image, ImageDraw, ImageFont

from procedure_spec import compact_procedure_prompt, load_bundle
from vlm_node.real_vlm import RealVLMNode, compose_flir_cam4_for_model


REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_IDS = tuple(f"0704_{index}" for index in range(6, 18))
SPLITS = {
    "train": {"0704_9", "0704_10", "0704_11", "0704_12", "0704_13", "0704_14", "0704_16"},
    "validation": {"0704_15", "0704_17"},
    "test": {"0704_6", "0704_7", "0704_8"},
}
CASE_TO_SPLIT = {
    case_id: split for split, case_ids in SPLITS.items() for case_id in case_ids
}

TOOL_IDS = {
    "scalpel": "T01",
    "adson_forceps": "T02",
    "allis_forceps": "T03",
    "bovie": "T04",
    "army_navy_retractor": "T05",
    "bipolar_forceps": "T07",
    "mosquito_forceps": "T08",
    "kocher_retractor": "T11",
    "thyroid_retractor": "T11",
}
DETECTOR_TOOL_IDS = {
    "Adson forceps": "T02",
    "Bipolar Cautery": "T07",
    "Bovie surgical cautery": "T04",
    "Thyroid retractor": "T11",
}
TOOL_ALIASES = {
    "T01": ("scalpel", "메스"),
    "T02": ("adson", "애드슨", "에드슨"),
    "T03": ("allis", "알리스"),
    "T04": ("bovie", "보비", "보우비", "cautery", "커터리"),
    "T05": ("army navy", "army-navy", "아미네이비", "아미 네이비"),
    "T07": ("bipolar", "바이폴라", "바이포라"),
    "T08": ("mosquito", "모스키토", "모스키또"),
    "T11": ("kocher", "코처", "thyroid retractor", "갑상선 견인기"),
}
SUPPORTED_TOOL_IDS = frozenset(TOOL_IDS.values())
DEFAULT_CAM4_CROP = (0.32, 0.18, 0.62, 0.78)


@dataclass(frozen=True)
class Anchor:
    case_id: str
    task: str
    time_sec: float
    supervision_fields: tuple[str, ...]
    source_ids: tuple[str, ...]
    authority: str
    forecast_kind: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "output/vlm_finetuning/qwen35_9b_runtime_v4_v1",
    )
    parser.add_argument(
        "--proxy-root",
        type=Path,
        default=Path("/home/arl/.cache/taskplanner_annotation"),
    )
    parser.add_argument(
        "--trace-root",
        type=Path,
        default=REPO_ROOT / "reports/release/20260806-final-clean-12case-v4/runs",
    )
    parser.add_argument(
        "--overlay-root",
        type=Path,
        default=REPO_ROOT / "tools/real_surgery_annotation/web_interaction_review/rfdetr_overlays",
    )
    parser.add_argument("--seed", type=int, default=3509)
    parser.add_argument("--max-side-px", type=int, default=1024)
    parser.add_argument("--summary-stride-sec", type=float, default=12.0)
    parser.add_argument("--mayo-rows-per-train-case", type=int, default=18)
    parser.add_argument("--negative-ratio", type=float, default=1.0)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def version_key(path: Path) -> tuple[int, str]:
    match = re.search(r"\.v(\d+)\.jsonl$", path.name)
    return (int(match.group(1)) if match else -1, path.name)


def newest(case_dir: Path, pattern: str) -> Path:
    candidates = sorted(case_dir.glob(pattern), key=version_key)
    if not candidates:
        raise FileNotFoundError(f"no source matches {case_dir / pattern}")
    return candidates[-1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_text(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={REPO_ROOT}", *args],
        cwd=REPO_ROOT,
        text=True,
    ).strip()


def json_value_spans(serialized: str, fields: Iterable[str]) -> list[list[int]]:
    """Return character spans for complete ``"key":value`` fragments."""

    decoder = json.JSONDecoder()
    spans: list[list[int]] = []
    for field in fields:
        marker = json.dumps(field, ensure_ascii=False) + ":"
        start = serialized.find(marker)
        if start < 0:
            raise ValueError(f"field {field!r} is absent from completion")
        value_start = start + len(marker)
        _, consumed = decoder.raw_decode(serialized[value_start:])
        spans.append([start, value_start + consumed])
    return spans


def frame_for_time(timestamps: list[float], time_sec: float) -> int:
    index = bisect.bisect_left(timestamps, time_sec)
    if index <= 0:
        return 0
    if index >= len(timestamps):
        return len(timestamps) - 1
    before = index - 1
    return before if abs(timestamps[before] - time_sec) <= abs(timestamps[index] - time_sec) else index


def decode_selected(path: Path, wanted: set[int]) -> dict[int, Image.Image]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open proxy video: {path}")
    selected: dict[int, Image.Image] = {}
    wanted_sorted = sorted(wanted)
    cursor = 0
    try:
        for target in wanted_sorted:
            if target != cursor:
                capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"cannot decode frame {target} from {path}")
            cursor = target + 1
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            selected[target] = Image.fromarray(rgb)
    finally:
        capture.release()
    return selected


def jpeg_bytes(image: Image.Image, quality: int = 92) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def color_for_class(name: str) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return (64 + digest[0] % 192, 64 + digest[1] % 192, 64 + digest[2] % 192, 220)


def render_detections(
    image: Image.Image,
    detections: list[dict[str, Any]],
    *,
    source_width: int,
    source_height: int,
    transparent: bool,
) -> Image.Image:
    base = Image.new("RGBA", image.size, (0, 0, 0, 0)) if transparent else image.convert("RGBA")
    draw = ImageDraw.Draw(base, "RGBA")
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    scale_x = image.width / max(1, source_width)
    scale_y = image.height / max(1, source_height)
    for item in detections:
        confidence = float(item.get("confidence", 0.0))
        if confidence < 0.55:
            continue
        box = item.get("bbox_xyxy") or []
        if len(box) != 4:
            continue
        x1, y1, x2, y2 = (
            float(box[0]) * scale_x,
            float(box[1]) * scale_y,
            float(box[2]) * scale_x,
            float(box[3]) * scale_y,
        )
        name = str(item.get("class_name", "object"))
        color = color_for_class(name)
        draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
        label = f"{name} {confidence:.2f}"
        text_box = draw.textbbox((x1, y1), label, font=font)
        draw.rectangle(text_box, fill=(0, 0, 0, 180))
        draw.text((x1, y1), label, fill=color, font=font)
    return base


def extract_trace(trace_path: Path) -> tuple[list[tuple[float, str]], list[tuple[float, dict[str, Any]]]]:
    contexts: list[tuple[float, str]] = []
    teacher: list[tuple[float, dict[str, Any]]] = []
    for row in read_jsonl(trace_path):
        topic = row.get("topic")
        time_sec = float(row.get("ros_time_sec", 0.0) or 0.0)
        payload = row.get("payload") or {}
        if topic == "/context/vlm_request_context":
            compact = str(payload.get("compact_json", "")).strip()
            if compact:
                contexts.append((time_sec, compact))
        elif topic == "/vlm/model_raw_result":
            raw = str(payload.get("raw_json", "")).strip()
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            teacher.append((time_sec, parsed))
    contexts.sort(key=lambda item: item[0])
    teacher.sort(key=lambda item: item[0])
    if not contexts:
        raise RuntimeError(f"trace has no public request context: {trace_path}")
    return contexts, teacher


def nearest_at_or_before(rows: list[tuple[float, Any]], time_sec: float) -> Any:
    times = [row[0] for row in rows]
    index = bisect.bisect_right(times, time_sec) - 1
    if index < 0:
        index = 0
    return rows[index][1]


def nearest(rows: list[tuple[float, Any]], time_sec: float) -> Any | None:
    if not rows:
        return None
    times = [row[0] for row in rows]
    index = bisect.bisect_left(times, time_sec)
    candidates = []
    if index < len(rows):
        candidates.append(rows[index])
    if index > 0:
        candidates.append(rows[index - 1])
    return min(candidates, key=lambda row: abs(row[0] - time_sec))[1]


def tool_id_from_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    aliases = {
        "adson": "adson_forceps",
        "allis": "allis_forceps",
        "bipolar": "bipolar_forceps",
        "bovie_surgical_cautery": "bovie",
        "thyroid_retractor": "thyroid_retractor",
        "kocher": "kocher_retractor",
    }
    return TOOL_IDS.get(aliases.get(normalized, normalized), "")


def tool_id_from_speech(text: str) -> str:
    lowered = text.lower()
    for tool_id, aliases in TOOL_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            return tool_id
    return ""


def active_phase(phases: list[dict[str, Any]], time_sec: float) -> str:
    active = "P03"
    for event in phases:
        if float(event.get("time_sec", 0.0)) <= time_sec:
            candidate = str(event.get("phase_id", ""))
            if re.fullmatch(r"P\d\d", candidate):
                active = candidate
        else:
            break
    return active


def active_request(requests: list[dict[str, Any]], time_sec: float) -> dict[str, Any] | None:
    for event in requests:
        if float(event.get("start_sec", event.get("time_sec", 0.0))) <= time_sec <= float(
            event.get("end_sec", event.get("time_sec", 0.0))
        ):
            return event
    return None


def current_intent(voice: list[dict[str, Any]], time_sec: float) -> tuple[str, str, float]:
    candidates = [
        event
        for event in voice
        if float(event.get("available_sec", event.get("end_sec", 0.0))) <= time_sec
        and time_sec - float(event.get("available_sec", event.get("end_sec", 0.0))) <= 3.0
    ]
    if not candidates:
        return ("none", "", 0.0)
    event = max(candidates, key=lambda row: float(row.get("available_sec", row.get("end_sec", 0.0))))
    tool_id = tool_id_from_speech(str(event.get("text", "")))
    return ("handover", tool_id, 0.92) if tool_id else ("none", "", 0.0)


def visible_mayo(
    detections: list[dict[str, Any]], source_width: int, source_height: int
) -> list[list[Any]]:
    crop_x, crop_y, crop_w, crop_h = DEFAULT_CAM4_CROP
    crop_box = (
        crop_x * source_width,
        crop_y * source_height,
        (crop_x + crop_w) * source_width,
        (crop_y + crop_h) * source_height,
    )
    rows: list[list[Any]] = []
    for item in detections:
        tool_id = DETECTOR_TOOL_IDS.get(str(item.get("class_name", "")), "")
        confidence = float(item.get("confidence", 0.0))
        box = item.get("bbox_xyxy") or []
        if not tool_id or confidence < 0.72 or len(box) != 4:
            continue
        center_x = (float(box[0]) + float(box[2])) / 2.0
        center_y = (float(box[1]) + float(box[3])) / 2.0
        if not (crop_box[0] <= center_x <= crop_box[2] and crop_box[1] <= center_y <= crop_box[3]):
            continue
        rows.append([tool_id, "reuse", round(min(0.95, confidence), 2)])
    return rows


def forecast_target(
    transfers: list[dict[str, Any]], time_sec: float
) -> tuple[list[list[Any]], float, str]:
    future: list[tuple[float, str]] = []
    for event in transfers:
        tool_id = tool_id_from_name(str(event.get("tool", "")))
        if not tool_id:
            continue
        delta = float(event.get("time_sec", 0.0)) - time_sec
        if delta >= 0.0:
            future.append((delta, tool_id))
    if not future:
        return [["T02", 0.05]], 0.72, "no_future_supported_transfer"
    delta, tool_id = min(future)
    if 2.0 <= delta <= 8.0:
        return [[tool_id, 0.9]], 0.2, "imminent_2_8_sec"
    return [[tool_id, 0.12]], 0.62, "outside_2_8_sec"


def choose_summary(teacher_payload: dict[str, Any] | None) -> str:
    if teacher_payload:
        text = str(teacher_payload.get("sum", "")).strip()
        if text:
            return text[:800]
    return "The visible operative field is partially obscured, limiting a specific clinical observation."


def make_target(
    *,
    time_sec: float,
    phases: list[dict[str, Any]],
    transfers: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    voice: list[dict[str, Any]],
    teacher_payload: dict[str, Any] | None,
    cam4_detections: list[dict[str, Any]],
    cam4_source_width: int,
    cam4_source_height: int,
) -> tuple[dict[str, Any], str]:
    phase_id = active_phase(phases, time_sec)
    tool_rows, uncertainty, forecast_kind = forecast_target(transfers, time_sec)
    request = active_request(requests, time_sec)
    gesture = ["request_tool", "", "open_receive", 0.9] if request else ["", "", "", 0.0]
    intent = list(current_intent(voice, time_sec))
    mayo = visible_mayo(cam4_detections, cam4_source_width, cam4_source_height)
    target = {
        "v": "4",
        "phase": [[phase_id, 0.9]],
        "tool": tool_rows,
        "intent": intent,
        "gesture": gesture,
        "mayo": mayo,
        "mayo_retrieve": [mayo[0][0], round(float(mayo[0][2]) * 0.75, 2)] if mayo else ["", 0.0],
        "u": uncertainty,
        "sum": choose_summary(teacher_payload),
        "bed_robot_arm_group": None,
    }
    return target, forecast_kind


def request_before_transfer(requests: list[dict[str, Any]], transfer_time: float) -> dict[str, Any] | None:
    eligible = [
        event
        for event in requests
        if 0.0 <= transfer_time - float(event.get("end_sec", event.get("time_sec", 0.0))) <= 10.0
    ]
    return max(eligible, key=lambda row: float(row.get("end_sec", row.get("time_sec", 0.0)))) if eligible else None


def build_anchors(
    case_id: str,
    duration: float,
    phases: list[dict[str, Any]],
    transfers: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    voice: list[dict[str, Any]],
    cam4_frames: list[list[dict[str, Any]]],
    timestamps: list[float],
    *,
    split: str,
    summary_stride_sec: float,
    mayo_rows_per_train_case: int,
    negative_ratio: float,
    rng: random.Random,
) -> list[Anchor]:
    anchors: list[Anchor] = []

    for index, event in enumerate(phases):
        start = float(event.get("time_sec", 0.0))
        next_start = float(phases[index + 1].get("time_sec", duration)) if index + 1 < len(phases) else duration
        for suffix, time_sec in (("start", min(start + 0.5, duration)), ("mid", min(start + 10.0, (start + next_start) / 2.0))):
            if 0.0 <= time_sec <= duration:
                anchors.append(Anchor(case_id, "phase", time_sec, ("phase",), (str(event.get("event_id", "")),), "provisional_phase_user_authorized", suffix))

    positives: list[Anchor] = []
    for event in transfers:
        transfer_time = float(event.get("time_sec", 0.0))
        tool_id = tool_id_from_name(str(event.get("tool", "")))
        if not tool_id or transfer_time < 2.1:
            continue
        anchor_time = transfer_time - 5.0
        preceding_request = request_before_transfer(requests, transfer_time)
        if preceding_request is not None:
            request_start = float(preceding_request.get("start_sec", preceding_request.get("time_sec", 0.0)))
            anchor_time = min(anchor_time, request_start - 0.35)
        anchor_time = max(0.0, transfer_time - 7.9, anchor_time)
        delta = transfer_time - anchor_time
        if 2.0 <= delta <= 8.0 and active_request(requests, anchor_time) is None:
            positives.append(Anchor(case_id, "forecast", anchor_time, ("tool", "u"), (str(event.get("event_id", "")),), "confirmed_physical_transfer", "positive_2_8_sec"))
    anchors.extend(positives)

    negative_candidates: list[float] = []
    grid = 1.0
    while grid <= duration:
        _, _, kind = forecast_target(transfers, grid)
        if kind != "imminent_2_8_sec" and active_request(requests, grid) is None:
            negative_candidates.append(grid)
        grid += 2.0
    rng.shuffle(negative_candidates)
    negative_count = min(len(negative_candidates), int(math.ceil(len(positives) * negative_ratio)))
    for index, time_sec in enumerate(sorted(negative_candidates[:negative_count])):
        anchors.append(Anchor(case_id, "forecast", time_sec, ("tool", "u"), (f"{case_id}-forecast-negative-{index:03d}",), "derived_no_transfer_in_2_8_sec", "negative_outside_window"))

    for event in requests:
        start = float(event.get("start_sec", event.get("time_sec", 0.0)))
        end = float(event.get("end_sec", start))
        time_sec = min(end, start + min(0.7, max(0.0, (end - start) / 2.0)))
        anchors.append(Anchor(case_id, "gesture", time_sec, ("gesture",), (str(event.get("event_id", "")),), "confirmed_request_interval", "positive_open_receive"))
    gesture_negative_times: list[float] = []
    grid = 0.5
    while grid <= duration and len(gesture_negative_times) < len(requests) * 3:
        if active_request(requests, grid) is None:
            gesture_negative_times.append(grid)
        grid += 2.5
    rng.shuffle(gesture_negative_times)
    for index, time_sec in enumerate(sorted(gesture_negative_times[: len(requests)])):
        anchors.append(Anchor(case_id, "gesture", time_sec, ("gesture",), (f"{case_id}-gesture-negative-{index:03d}",), "derived_outside_confirmed_request_intervals", "negative_no_open_receive"))

    for event in voice:
        available = float(event.get("available_sec", event.get("end_sec", 0.0)))
        if not (0.0 <= available <= duration):
            continue
        tool_id = tool_id_from_speech(str(event.get("text", "")))
        kind = "positive_named_tool" if tool_id else "negative_non_tool_speech"
        anchors.append(Anchor(case_id, "intent", min(duration, available + 0.15), ("intent",), (str(event.get("event_id", "")),), "public_runtime_transcript", kind))

    time_sec = 0.5
    summary_index = 0
    while time_sec <= duration:
        anchors.append(Anchor(case_id, "summary", time_sec, ("sum",), (f"{case_id}-teacher-summary-{summary_index:03d}",), "qwen35_a3b_public_trace_teacher", "silver_distillation"))
        summary_index += 1
        time_sec += summary_stride_sec

    if split == "train" and cam4_frames:
        populated = [index for index, detections in enumerate(cam4_frames) if visible_mayo(detections, 1280, 720)]
        rng.shuffle(populated)
        # Detector silence is not proof that the Mayo is empty.  Only positive,
        # high-confidence instances become weak supervision; no pseudo-absence
        # rows are generated.
        chosen = populated[:mayo_rows_per_train_case]
        for index, frame_idx in enumerate(sorted(set(chosen))):
            anchors.append(Anchor(case_id, "mayo", timestamps[frame_idx], ("mayo", "mayo_retrieve"), (f"{case_id}-rfdetr-{frame_idx:06d}",), "rfdetr_temporal_pseudo_train_only", "weak_positive_inventory"))

    # Preserve task duplicates (different labels at one frame) but remove exact anchor duplicates.
    unique: dict[tuple[str, int, tuple[str, ...]], Anchor] = {}
    for anchor in anchors:
        key = (anchor.task, round(anchor.time_sec * 1000), anchor.source_ids)
        unique[key] = anchor
    return sorted(unique.values(), key=lambda row: (row.time_sec, row.task, row.source_ids))


def build_runtime_prompts(spec_dir: Path) -> tuple[str, str]:
    node = object.__new__(RealVLMNode)
    node._spec = load_bundle(spec_dir)
    node._procedure_prompt = compact_procedure_prompt(spec_dir)
    return node._actor_log_system_prompt(), node._actor_log_developer_instruction()


def prompt_messages(system_prompt: str, developer_prompt: str, context_json: str, image_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": [{"type": "text", "text": system_prompt + "\n\n" + developer_prompt}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compact context JSON:\n" + context_json},
                {"type": "text", "text": "Image label: flir_cam4_composite"},
                {"type": "image", "image": str(image_path)},
            ],
        },
    ]


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    output_dir = args.output_dir.resolve()
    image_root = output_dir / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    image_root.mkdir(parents=True, exist_ok=True)

    spec_dir = REPO_ROOT / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
    system_prompt, developer_prompt = build_runtime_prompts(spec_dir)
    system_sha = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    developer_sha = hashlib.sha256(developer_prompt.encode("utf-8")).hexdigest()

    all_rows: list[dict[str, Any]] = []
    source_manifest: dict[str, Any] = {}
    unsupported_tools: Counter[str] = Counter()

    for case_id in CASE_IDS:
        split = CASE_TO_SPLIT[case_id]
        case_dir = REPO_ROOT / "annotations/observable_tool_events/cases" / case_id
        timeline_path = case_dir / "cam4_frame_timeline.v1.json"
        observed_path = newest(case_dir, "interaction_events.observed.final.v*.jsonl")
        phase_path = newest(case_dir, "phase_events.provisional.final.v*.jsonl")
        voice_path = newest(case_dir, "voice_events.source.v*.jsonl")
        overlay_path = args.overlay_root / f"{case_id}.json"
        trace_path = args.trace_root / f"case-{case_id}" / "shadow_trace.v1.jsonl"

        timeline_payload = json.loads(timeline_path.read_text(encoding="utf-8"))
        timestamps = [float(value) for value in timeline_payload["timestamps_sec"]]
        duration = float(timestamps[-1])
        events = read_jsonl(observed_path)
        transfers = [
            event
            for event in events
            if event.get("event_type") == "tool_transfer"
            and event.get("from") == "scrub_nurse"
            and event.get("to") == "surgeon"
            and event.get("review_status") == "confirmed"
        ]
        for event in transfers:
            if not tool_id_from_name(str(event.get("tool", ""))):
                unsupported_tools[str(event.get("tool", ""))] += 1
        transfers = [event for event in transfers if tool_id_from_name(str(event.get("tool", "")))]
        requests = [
            event
            for event in events
            if "request" in str(event.get("event_type", ""))
            and event.get("review_status") == "confirmed"
        ]
        phases = sorted(read_jsonl(phase_path), key=lambda row: float(row.get("time_sec", 0.0)))
        voice = sorted(read_jsonl(voice_path), key=lambda row: float(row.get("available_sec", row.get("end_sec", 0.0))))
        overlay_payload = json.loads(overlay_path.read_text(encoding="utf-8"))
        cam4_view = overlay_payload["views"]["cam4"]
        flir_view = overlay_payload["views"]["flir"]
        cam4_frames = cam4_view["frames"]
        flir_frames = flir_view["frames"]
        contexts, teacher = extract_trace(trace_path)

        anchors = build_anchors(
            case_id,
            duration,
            phases,
            transfers,
            requests,
            voice,
            cam4_frames,
            timestamps,
            split=split,
            summary_stride_sec=args.summary_stride_sec,
            mayo_rows_per_train_case=args.mayo_rows_per_train_case,
            negative_ratio=args.negative_ratio,
            rng=random.Random(args.seed + int(case_id.split("_")[-1])),
        )
        anchor_frames = {frame_for_time(timestamps, anchor.time_sec) for anchor in anchors}
        cam4_images = decode_selected(args.proxy_root / case_id / "review_cam4.mp4", anchor_frames)
        flir_images = decode_selected(args.proxy_root / case_id / "review_flir.mp4", anchor_frames)
        case_image_dir = image_root / case_id
        case_image_dir.mkdir(parents=True, exist_ok=True)

        rendered_cache: dict[tuple[int, bool], Path] = {}
        for anchor_index, anchor in enumerate(anchors):
            frame_idx = frame_for_time(timestamps, anchor.time_sec)
            # Detector evidence is present on most rows, with deterministic dropout
            # to prevent the adapter from treating an overlay as ground truth.
            overlay_enabled = ((anchor_index + int(case_id.split("_")[-1])) % 5) != 0
            cache_key = (frame_idx, overlay_enabled)
            image_path = rendered_cache.get(cache_key)
            if image_path is None:
                cam4 = cam4_images[frame_idx]
                flir = flir_images[frame_idx]
                cam4_detections = cam4_frames[frame_idx] if frame_idx < len(cam4_frames) else []
                flir_detections = flir_frames[frame_idx] if frame_idx < len(flir_frames) else []
                if overlay_enabled:
                    cam4_overlay = render_detections(
                        cam4,
                        cam4_detections,
                        source_width=int(cam4_view["source_width"]),
                        source_height=int(cam4_view["source_height"]),
                        transparent=True,
                    )
                    flir_input = render_detections(
                        flir,
                        flir_detections,
                        source_width=int(flir_view["source_width"]),
                        source_height=int(flir_view["source_height"]),
                        transparent=False,
                    )
                    overlay_buffer = BytesIO()
                    cam4_overlay.save(overlay_buffer, format="PNG")
                    overlay_bytes = overlay_buffer.getvalue()
                else:
                    flir_input = flir
                    overlay_bytes = None
                composite_bytes, _ = compose_flir_cam4_for_model(
                    jpeg_bytes(flir_input),
                    "image/jpeg",
                    jpeg_bytes(cam4),
                    "image/jpeg",
                    cam4_crop_xywh_norm=DEFAULT_CAM4_CROP,
                    max_side_px=args.max_side_px,
                    cam4_overlay_bytes=overlay_bytes,
                    cam4_overlay_mime_type="image/png" if overlay_bytes else "",
                )
                suffix = "rfdetr" if overlay_enabled else "raw"
                image_path = case_image_dir / f"frame_{frame_idx:06d}_{suffix}.jpg"
                image_path.write_bytes(composite_bytes)
                rendered_cache[cache_key] = image_path

            teacher_payload = nearest(teacher, anchor.time_sec)
            cam4_detections = cam4_frames[frame_idx] if frame_idx < len(cam4_frames) else []
            target, derived_forecast_kind = make_target(
                time_sec=anchor.time_sec,
                phases=phases,
                transfers=transfers,
                requests=requests,
                voice=voice,
                teacher_payload=teacher_payload,
                cam4_detections=cam4_detections,
                cam4_source_width=int(cam4_view["source_width"]),
                cam4_source_height=int(cam4_view["source_height"]),
            )
            context_json = nearest_at_or_before(contexts, anchor.time_sec)
            completion = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
            supervised = tuple(dict.fromkeys(("v", *anchor.supervision_fields, "bed_robot_arm_group")))
            portable_image_path = image_path.relative_to(output_dir)
            row = {
                "schema": "taskplanner.qwen35_runtime_v4_sft.v1",
                "example_id": f"{case_id}:{anchor.task}:{frame_idx:06d}:{anchor_index:04d}",
                "case_id": case_id,
                "split": split,
                "task": anchor.task,
                "time_sec": round(anchor.time_sec, 6),
                "source_frame_idx": frame_idx,
                "image_path": portable_image_path.as_posix(),
                "image_sha256": sha256_file(image_path),
                "input_contract": {
                    "image_count": 1,
                    "image_label": "flir_cam4_composite",
                    "layout": "flir_left_cam4_right",
                    "cam4_crop_xywh_norm": list(DEFAULT_CAM4_CROP),
                    "max_side_px": args.max_side_px,
                    "rfdetr_overlay_forwarded": overlay_enabled,
                    "context_mode": "actor_log",
                    "task_profile": "full",
                },
                "prompt_messages": prompt_messages(system_prompt, developer_prompt, context_json, portable_image_path),
                "completion": completion,
                "completion_json": target,
                "supervision_fields": list(supervised),
                "supervision_char_spans": json_value_spans(completion, supervised),
                "authority": {
                    "tier": anchor.authority,
                    "source_ids": list(anchor.source_ids),
                    "teacher_only_fields": ["sum"] if anchor.task == "summary" else [],
                    "pseudo_only_fields": ["mayo", "mayo_retrieve"] if anchor.task == "mayo" else [],
                },
                "semantic": {
                    "anchor_kind": anchor.forecast_kind,
                    "derived_forecast_kind": derived_forecast_kind,
                    "forecast_horizon_sec": [2.0, 8.0],
                    "tool_is_additional_handover_not_visible_tool": True,
                    "gesture_never_backfills_tool": True,
                },
                "prompt_sha256": {
                    "system": system_sha,
                    "developer": developer_sha,
                },
            }
            all_rows.append(row)

        source_manifest[case_id] = {
            "split": split,
            "timeline": {"path": str(timeline_path.relative_to(REPO_ROOT)), "sha256": sha256_file(timeline_path)},
            "observed": {"path": str(observed_path.relative_to(REPO_ROOT)), "sha256": sha256_file(observed_path)},
            "phase": {"path": str(phase_path.relative_to(REPO_ROOT)), "sha256": sha256_file(phase_path)},
            "voice": {"path": str(voice_path.relative_to(REPO_ROOT)), "sha256": sha256_file(voice_path)},
            "rfdetr": {"path": str(overlay_path.relative_to(REPO_ROOT)), "sha256": sha256_file(overlay_path)},
            "teacher_trace": {"path": str(trace_path.relative_to(REPO_ROOT)), "sha256": sha256_file(trace_path)},
            "row_count": len(anchors),
        }

    all_rows.sort(key=lambda row: (row["split"], row["case_id"], row["time_sec"], row["task"], row["example_id"]))
    paths = {"master": output_dir / "master.jsonl"}
    paths.update({split: output_dir / f"{split}.jsonl" for split in SPLITS})
    handles = {name: path.open("w", encoding="utf-8") for name, path in paths.items()}
    try:
        for row in all_rows:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            handles["master"].write(line)
            handles[row["split"]].write(line)
    finally:
        for handle in handles.values():
            handle.close()

    split_counts = Counter(row["split"] for row in all_rows)
    task_counts = Counter((row["split"], row["task"]) for row in all_rows)
    case_counts = Counter((row["split"], row["case_id"]) for row in all_rows)
    train_cases = {row["case_id"] for row in all_rows if row["split"] == "train"}
    evaluation_cases = {row["case_id"] for row in all_rows if row["split"] != "train"}
    if train_cases & evaluation_cases:
        raise RuntimeError(f"case leakage detected: {sorted(train_cases & evaluation_cases)}")
    if any(row["task"] == "mayo" and row["split"] != "train" for row in all_rows):
        raise RuntimeError("RF-DETR pseudo labels escaped the training split")

    manifest = {
        "schema": "taskplanner.qwen35_runtime_v4_dataset_manifest.v1",
        "repo_head": git_text("rev-parse", "HEAD"),
        "origin_main": git_text("rev-parse", "origin/main"),
        "dirty_worktree_preserved": bool(git_text("status", "--porcelain")),
        "seed": args.seed,
        "runtime_contract": {
            "prompt_profile": "actor_log/full/schema-v4",
            "system_sha256": system_sha,
            "developer_sha256": developer_sha,
            "single_composite_image": True,
            "composition_function": "vlm_node.real_vlm.compose_flir_cam4_for_model",
            "cam4_crop_xywh_norm": list(DEFAULT_CAM4_CROP),
            "max_side_px": args.max_side_px,
        },
        "split_policy": {split: sorted(case_ids) for split, case_ids in SPLITS.items()},
        "split_counts": dict(split_counts),
        "task_counts": {f"{split}/{task}": count for (split, task), count in sorted(task_counts.items())},
        "case_counts": {f"{split}/{case_id}": count for (split, case_id), count in sorted(case_counts.items())},
        "unsupported_transfer_tools_excluded": dict(unsupported_tools),
        "weak_supervision_policy": {
            "rfdetr_mayo": "train-only pseudo labels; not treated as Mayo-location ground truth",
            "summary": "Qwen3.5-35B-A3B public-trace distillation; agreement metric only",
            "phase": "user-authorized provisional boundaries; evaluated separately",
            "gesture_and_transfer": "confirmed observable intervals/points",
            "bed_robot_arm_group": "null-only because no positive reviewed request corpus exists",
        },
        "sources": source_manifest,
        "files": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in paths.items()},
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "output_dir": str(output_dir),
        "rows": len(all_rows),
        "split_counts": dict(split_counts),
        "task_counts": manifest["task_counts"],
        "unsupported_transfer_tools_excluded": dict(unsupported_tools),
        "manifest": str(output_dir / "manifest.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
