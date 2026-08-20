#!/usr/bin/env python3
"""Evaluation-only Mayo instrument prompt benchmark for the 0704_5 reference.

This intentionally sits outside the runtime VLM/ROS/BT path.  It sends only
CAM4 pixels, an allowed instrument vocabulary, and a task contract to NInfer;
the reviewed event label is retained locally and is attached only after a model
response is received for scoring.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import mayo_pixel_preprocess as pixels


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BAG = (
    WORKSPACE_ROOT
    / "annotated_bags/0704_5_reviewed_gt_v2"
)
DEFAULT_EVENTS = (
    WORKSPACE_ROOT
    / "annotations/observable_tool_events/cases/0704_5/tool_events.final.v1.jsonl"
)
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parent / "runs"
CAM4_TOPIC = "/surgery/cam4/color/image/compressed"
PROMPT_VERSION = "mayo-recognition-v3"
PROMPT_VERSION_BY_VARIANT = {
    "baseline": PROMPT_VERSION,
    "optimized": PROMPT_VERSION,
    "optimized_v2": PROMPT_VERSION,
    "optimized_v4": "mayo-recognition-v4",
}
PRE_EVENT_FRAME_OFFSET = 10
POST_EVENT_FRAME_OFFSET = 4
CALIBRATION_ARRIVAL_EVENT_IDS = (
    "0704_5-E0008",
    "0704_5-E0012",
)
FROZEN_CHALLENGE_EVENT_IDS = (
    "0704_5-E0016",
    "0704_5-E0020",
    "0704_5-E0031",
    "0704_5-E0037",
    "0704_5-E0041",
)
DEFAULT_LOCK_PATH = Path("/tmp/taskplanner-ninfer-eval.lock")
DEFAULT_WORKER_BASE_URL = "http://127.0.0.1:8082"
MAX_REQUESTS_PER_FRESH_WORKER_BATCH = 3
IMAGE_PREPROCESS_NONE = "none"
IMAGE_PREPROCESS_LETTERBOX_512_Q95 = "letterbox_512_q95"
MODEL_REQUEST_CONFIG = {
    "temperature": 0.0,
    "top_p": 1.0,
    "max_tokens": 220,
    "reasoning_effort": "none",
    "enable_thinking": False,
    "stream": False,
}
NO_THRESHOLD_POLICY = {
    "confidence_threshold": None,
    "postprocess_filtering": "none",
    "semantic_scoring": "closed_catalog_exact_match_without_confidence_threshold",
}


# This is a closed label vocabulary, not a procedure prior.  The visual cues
# are generic morphology checks that are useful for a high-resolution overhead
# tray view.  The model must still abstain if pixels do not support a label.
TOOL_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "scalpel", "cue": "short flat handle with a small exposed blade; no finger rings"},
    {"id": "adson_forceps", "cue": "small tweezer-style tissue forceps; two spring arms and no finger rings"},
    {"id": "bipolar_forceps", "cue": "tweezer-style bipolar forceps; paired tips and often insulation or cable"},
    {"id": "allis_forceps", "cue": "finger-ringed clamp with broad toothed grasping jaws"},
    {"id": "kocher_retractor", "cue": "thyroid/Middeldorpf retractor or ring-handled retractor with a substantial working end"},
    {"id": "bovie", "cue": "electrosurgical pencil or probe with insulated body and/or attached cable"},
    {"id": "army_navy_retractor", "cue": "double-ended flat handheld retractor; no finger rings"},
    {"id": "senn_miller_retractor", "cue": "small double-ended retractor with a narrow rake or blade end"},
    {"id": "mosquito_forceps", "cue": "small fine finger-ringed hemostat/clamp"},
    {"id": "harmonic_shears", "cue": "powered shears/handpiece with a characteristic shaft or cable"},
    {"id": "yankauer_suction", "cue": "rigid suction tube with a conspicuous open lumen"},
)
ALLOWED_TOOL_IDS = tuple(row["id"] for row in TOOL_CATALOG)


class EvaluationError(RuntimeError):
    """Raised when a local evaluation artifact cannot be used safely."""


@dataclass(frozen=True)
class Sample:
    """One evaluated image request.  ``expected`` is never sent to NInfer."""

    sample_id: str
    mode: str
    frame_indices: tuple[int, ...]
    expected: Any
    bbox_xywh: tuple[int, int, int, int] | None = None


def _require_object(value: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{what} must be a JSON object")
    return value


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise EvaluationError(f"event reference not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"invalid JSONL at {path}:{line_number}") from exc
        rows.append(_require_object(row, what=f"event {line_number}"))
    return rows


def confirmed_initial_inventory(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        event
        for event in events
        if event.get("review_status") == "confirmed"
        and event.get("event_type") == "initial_state"
        and isinstance(event.get("to"), dict)
        and event["to"].get("location") == "mayo_stand"
        and isinstance(event.get("proposal"), dict)
        and isinstance(event["proposal"].get("bbox_xywh_px"), list)
    ]
    if not rows:
        raise EvaluationError("no confirmed 0704_5 Mayo initial-state labels found")
    return rows


def make_calibration_samples(events: Iterable[dict[str, Any]]) -> list[Sample]:
    """Create one full-frame inventory plus localization-conditioned crops.

    The crop requests are explicitly calibration-only: their target rectangle
    comes from the reviewed annotation and therefore cannot be presented as an
    end-to-end accuracy estimate.
    """

    initial = confirmed_initial_inventory(events)
    counts = Counter(
        str(_require_object(row.get("tool"), what="tool").get("id", "")).strip()
        for row in initial
    )
    if not counts or not all(tool_id in ALLOWED_TOOL_IDS for tool_id in counts):
        raise EvaluationError("initial inventory contains a tool outside the closed catalog")
    samples = [
        Sample(
            sample_id="0704_5-initial-inventory-frame-0000",
            mode="inventory",
            frame_indices=(0,),
            expected=dict(sorted(counts.items())),
        )
    ]
    for index, event in enumerate(initial, 1):
        tool = _require_object(event.get("tool"), what="tool")
        proposal = _require_object(event.get("proposal"), what="proposal")
        raw_bbox = proposal.get("bbox_xywh_px")
        if (
            not isinstance(raw_bbox, list)
            or len(raw_bbox) != 4
            or not all(isinstance(value, int) for value in raw_bbox)
        ):
            raise EvaluationError(f"invalid bbox for calibration record {index}")
        source_frame = proposal.get("source_frame_idx")
        if not isinstance(source_frame, int) or source_frame < 0:
            raise EvaluationError(f"invalid source frame for calibration record {index}")
        tool_id = str(tool.get("id", "")).strip()
        if tool_id not in ALLOWED_TOOL_IDS:
            raise EvaluationError(f"unknown calibration tool id: {tool_id}")
        samples.append(
            Sample(
                sample_id=f"0704_5-initial-crop-{index:02d}",
                mode="crop",
                frame_indices=(source_frame,),
                expected=tool_id,
                bbox_xywh=tuple(raw_bbox),
            )
        )
    # Use the first two clear Mayo-arrival events as temporal calibration.  They
    # are earlier than every frozen challenge event and, like t=0 crops, may be
    # examined to improve a later prompt.
    return samples + make_arrival_samples(
        events,
        event_ids=CALIBRATION_ARRIVAL_EVENT_IDS,
        split_name="calibration",
    )


def make_arrival_samples(
    events: Iterable[dict[str, Any]],
    *,
    event_ids: tuple[str, ...],
    split_name: str,
) -> list[Sample]:
    """Build a fixed temporal arrival partition without exposing its labels.

    The reference tool id remains in ``expected`` and is deliberately absent
    from request context and image labels.  The selected IDs make the split
    reproducible if the surrounding annotation directory later changes.
    """

    samples: list[Sample] = []
    for event in events:
        event_id = str(event.get("event_id", "")).strip()
        if event_id not in event_ids:
            continue
        if event.get("review_status") != "confirmed":
            continue
        if event.get("event_type") != "place_on_mayo":
            continue
        if event.get("visibility") != "clear":
            continue
        source_views = event.get("source_views")
        if not isinstance(source_views, list) or "cam4" not in source_views:
            continue
        proposal = _require_object(event.get("proposal"), what="proposal")
        frame = proposal.get("source_frame_idx")
        tool = _require_object(event.get("tool"), what="tool")
        tool_id = str(tool.get("id", "")).strip()
        if not isinstance(frame, int) or frame < PRE_EVENT_FRAME_OFFSET:
            raise EvaluationError("arrival challenge has an invalid source frame")
        if tool_id not in ALLOWED_TOOL_IDS:
            raise EvaluationError(f"{split_name} arrival tool outside catalog: {tool_id}")
        samples.append(
            Sample(
                sample_id=f"0704_5-{split_name}-arrival-{event_id}",
                mode="arrival",
                frame_indices=(frame - PRE_EVENT_FRAME_OFFSET, frame + POST_EVENT_FRAME_OFFSET),
                expected=tool_id,
            )
        )
    prefix = f"0704_5-{split_name}-arrival-"
    by_id = {sample.sample_id.removeprefix(prefix): sample for sample in samples}
    missing = [event_id for event_id in event_ids if event_id not in by_id]
    if missing:
        raise EvaluationError(f"{split_name} arrival reference no longer matches: {missing}")
    return [by_id[event_id] for event_id in event_ids]


def make_frozen_arrival_samples(events: Iterable[dict[str, Any]]) -> list[Sample]:
    """Build the late, pre-registered within-case frozen challenge."""

    return make_arrival_samples(
        events,
        event_ids=FROZEN_CHALLENGE_EVENT_IDS,
        split_name="challenge",
    )


def request_context_for(sample: Sample) -> dict[str, Any]:
    """Return only public, task-specific inference input.

    In particular, it must never contain the review event, expected label,
    source time, source bbox, procedure phase, audio, or digital-twin context.
    """

    if sample.mode == "inventory":
        task = (
            "List the distinct visible instrument types and their instance counts "
            "resting on the blue sterile Mayo surface in this one overhead CAM4 image."
        )
    elif sample.mode == "crop":
        task = (
            "Classify only the instrument inside the outlined rectangle in this "
            "CAM4 crop. The rectangle is a localization aid, not a label."
        )
    elif sample.mode == "arrival":
        task = (
            "Compare the two chronological overhead CAM4 images and list only "
            "instrument types that newly become settled on the blue sterile Mayo "
            "surface in AFTER relative to BEFORE."
        )
    else:
        raise EvaluationError(f"unsupported sample mode: {sample.mode}")
    return {
        "task": task,
        "view": "overhead_CAM4",
        "image_order": (
            ["CAM4_BEFORE", "CAM4_AFTER"]
            if sample.mode == "arrival"
            else ["CAM4_MAYO"]
        ),
        "allowed_tools": list(TOOL_CATALOG),
        "policy": {
            "pixels_only": True,
            "do_not_use_procedure_or_temporal_prior": True,
            "abstain_when_unidentifiable": True,
        },
    }


def assert_request_is_label_free(sample: Sample, context: dict[str, Any]) -> None:
    """Guard the evaluation-only information boundary before an HTTP request."""

    serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
    forbidden_keys = {
        "expected",
        "ground_truth",
        "review_status",
        "event_id",
        "source_frame_idx",
        "time_sec",
        "bbox_xywh",
    }
    if any(f'"{key}"' in serialized for key in forbidden_keys):
        raise EvaluationError("request context includes a ground-truth-only field")
    # The expected class naturally appears in the allowed vocabulary.  The
    # meaningful leakage guard is structural: no target-specific field is sent.
    if sample.mode == "arrival" and len(context.get("image_order", [])) != 2:
        raise EvaluationError("arrival request must contain exactly two ordered images")


def prompt_version_for(variant: str) -> str:
    try:
        return PROMPT_VERSION_BY_VARIANT[variant]
    except KeyError as exc:
        raise EvaluationError(f"unsupported prompt variant: {variant}") from exc


def prompt_for(mode: str, variant: str) -> str:
    prompt_version_for(variant)
    if mode == "inventory":
        output_contract = (
            'Return exactly one JSON object: {"visible":[["tool_id",count,confidence]],'
            '"abstain":false}. visible uses only allowed tool_id values, unique ids, '
            "positive integer counts, and confidence in [0,1]."
        )
    elif mode == "crop":
        output_contract = (
            'Return exactly one JSON object: {"tool_id":"tool_id_or_empty",'
            '"confidence":0.0,"abstain":false}. tool_id is one allowed value or "". '
            "confidence is in [0,1]."
        )
    elif mode == "arrival":
        output_contract = (
            'Return exactly one JSON object: {"newly_on_mayo":[["tool_id",confidence]],'
            '"abstain":false}. Use each allowed tool_id at most once and confidence in [0,1].'
        )
    else:
        raise EvaluationError(f"unsupported prompt mode: {mode}")

    base = (
        "You are an evaluation-only surgical-instrument vision classifier. "
        "Use only visible pixels in the supplied overhead CAM4 image(s). "
        "Do not use surgery stage, likely procedure order, spoken requests, patient anatomy, "
        "or any information not visible in these image(s). Do not guess a tool merely because "
        "it is common. Ignore hands, arms, drapes, cables not attached to a recognizable "
        "instrument, and instruments outside the blue Mayo surface. "
    )
    if variant == "baseline":
        return base + output_contract + " Output JSON only; no Markdown or explanation."

    optimized = (
        "First orient to the blue sterile Mayo surface; the camera can be rotated, so do not "
        "infer identity from screen position. Then inspect morphology at full image "
        "resolution: finger rings, hinge, spring arms, jaw/blade shape, flat retractor blade, "
        "insulation, cable, or suction lumen. For a pair, compare BEFORE and AFTER by visual "
        "difference and count only objects newly settled on the Mayo surface; an object still "
        "in a hand, being carried, or merely moved within the field is not newly settled. "
        "For the outlined crop, classify only the outlined target, not a neighboring tool. "
        "Do not call an isolated cable or unrelated black device a Bovie: require a recognizable "
        "electrosurgical handpiece/probe. When two catalog classes cannot be distinguished from "
        "pixels, leave it out and set abstain true rather than choosing by likelihood. Count a "
        "duplicate only when separate handles/jaws/shafts make two instances visually distinct. "
        "The JSON keys shown in the contract are mandatory, including abstain; emit no other keys. "
    )
    if variant == "optimized":
        return base + optimized + output_contract + " Output JSON only; no Markdown or explanation."

    if variant == "optimized_v4":
        # This text is the approved calibration-only v4 proposal. It contains
        # no event, frame, bbox coordinate, or reviewed label; crop wording
        # describes only the visible magenta localization rectangle.
        optimized_v4 = (
            "Contract self-check before emitting: return every key shown in the mode's JSON contract, "
            "including abstain, and no other keys. If you name one or more tools, still include "
            "\"abstain\":false; if discriminative pixels are absent, use the contract's empty tool "
            "field/list and \"abstain\":true. For an outlined crop, associate the target with the "
            "instrument whose central body and working end are inside the magenta rectangle. Do not "
            "classify a neighbor merely because its shaft, ring, or cable crosses the rectangle. If two "
            "distinct instruments occupy the rectangle, or only ambiguous rings/shaft are visible, abstain. "
            "A target with visible circular finger rings cannot be Adson or bipolar forceps: those are "
            "tweezer-style instruments without finger rings. Distinguish a small fine mosquito clamp from "
            "Allis by visible jaw morphology: call Allis only when broad/serrated grasping jaws support it; "
            "otherwise abstain. A ring-handled Kocher/Middeldorpf retractor needs a substantial retractor "
            "working end, not just clamp-like rings; if that end is not visible, abstain rather than calling "
            "a forceps class. For inventory, scan the blue Mayo surface in fixed strips and count every "
            "separately visible handle, shaft, jaw, or retractor working end once. Do not collapse touching "
            "parallel tools, and do not count a cable as an instrument. A Bovie requires the recognizable "
            "insulated pencil/probe body resting on the cloth, not a loose cable, generic white rod, or "
            "unrelated black device. A Senn-Miller requires a visible narrow rake or blade end. For "
            "BEFORE/AFTER arrivals, first compare the persistent tray layout, then look for a distinct object "
            "newly supported by the Mayo cloth. A tool may be partly covered by a hand if its own body is "
            "visibly resting on the cloth; do not label an object that is entirely hand-held or has no visible "
            "discriminative body. "
        )
        return base + optimized_v4 + output_contract + " Output JSON only; no Markdown or explanation."

    optimized_v2 = (
        "For inventory, scan the Mayo cloth in ordered strips and count each separately visible "
        "parallel handle, shaft, or jaw as a distinct instance; do not collapse a pair merely "
        "because the tools touch or overlap. Conversely, do not count a cable as a separate "
        "instrument. A Bovie requires the actual electrosurgical pencil/probe body to be visibly "
        "resting on the blue Mayo cloth; a white/black cable crossing the cloth or leading off "
        "image is not a Bovie. A target with one or more circular finger rings cannot be Adson "
        "or bipolar forceps, because those are tweezer-style and have no finger rings. Label a "
        "ring-handled target Allis only when the clamp/jaw morphology supports it; if the crop "
        "shows only ambiguous rings, abstain instead of choosing a tweezer class. "
    )
    return (
        base
        + optimized
        + optimized_v2
        + output_contract
        + " Output JSON only; no Markdown or explanation."
    )


def _decode_cam4_frames(bag_dir: Path, wanted_indices: set[int]) -> dict[int, bytes]:
    if not bag_dir.is_dir():
        raise EvaluationError(f"bag directory not found: {bag_dir}")
    if not wanted_indices or min(wanted_indices) < 0:
        raise EvaluationError("requested CAM4 frame indices must be non-negative")
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import CompressedImage
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise EvaluationError("ROS 2 Python image dependencies are unavailable") from exc

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_dir.resolve()), storage_id="mcap"),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    reader.set_filter(rosbag2_py.StorageFilter(topics=[CAM4_TOPIC]))
    decoded: dict[int, bytes] = {}
    index = 0
    try:
        while reader.has_next() and wanted_indices - decoded.keys():
            record = reader.read_next_ext() if hasattr(reader, "read_next_ext") else reader.read_next()
            topic, payload = str(record[0]), record[1]
            if topic != CAM4_TOPIC:
                continue
            if index in wanted_indices:
                message = deserialize_message(payload, CompressedImage)
                decoded[index] = bytes(message.data)
            index += 1
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    missing = sorted(wanted_indices - decoded.keys())
    if missing:
        raise EvaluationError(f"CAM4 frames missing from bag: {missing}")
    return decoded


def _marked_crop(image_bytes: bytes, bbox: tuple[int, int, int, int]) -> bytes:
    """Return a padded, outlined image crop for calibration-only recognition."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise EvaluationError("OpenCV is required for crop calibration") from exc
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise EvaluationError("could not decode CAM4 JPEG for crop calibration")
    x, y, width, height = bbox
    if width <= 0 or height <= 0:
        raise EvaluationError("crop bbox must have positive size")
    image_height, image_width = image.shape[:2]
    if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
        raise EvaluationError("crop bbox lies outside CAM4 image")
    marked = image.copy()
    cv2.rectangle(marked, (x, y), (x + width, y + height), (255, 0, 255), 4)
    padding = max(28, int(max(width, height) * 0.18))
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(image_width, x + width + padding)
    bottom = min(image_height, y + height + padding)
    crop = marked[top:bottom, left:right]
    ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 96])
    if not ok:
        raise EvaluationError("could not encode calibration crop")
    return bytes(encoded)


def images_for(sample: Sample, frames: dict[int, bytes]) -> list[tuple[str, bytes, str]]:
    if sample.mode in {"inventory", "crop"}:
        image = frames[sample.frame_indices[0]]
        if sample.mode == "crop":
            if sample.bbox_xywh is None:
                raise EvaluationError("crop sample has no bbox")
            image = _marked_crop(image, sample.bbox_xywh)
        return [("CAM4_MAYO", image, "image/jpeg")]
    if sample.mode == "arrival":
        before, after = (frames[index] for index in sample.frame_indices)
        return [
            ("CAM4_BEFORE", before, "image/jpeg"),
            ("CAM4_AFTER", after, "image/jpeg"),
        ]
    raise EvaluationError(f"unsupported sample mode: {sample.mode}")


def image_preprocess_policy(image_preprocess: str) -> dict[str, Any]:
    """Describe a label-free input transform that is part of an evaluation run."""

    if image_preprocess == IMAGE_PREPROCESS_NONE:
        return {
            "id": IMAGE_PREPROCESS_NONE,
            "applied_to": "none",
        }
    if image_preprocess == IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        return {
            "id": pixels.LETTERBOX_PREPROCESSOR_ID,
            "requested_flag": IMAGE_PREPROCESS_LETTERBOX_512_Q95,
            "applied_to": "every_model_input_image",
            "target_dimensions_px": [512, 512],
            "aspect_preserving": True,
            "padding_bgr": [0, 0, 0],
            "jpeg_quality": 95,
            "deterministic": True,
            "evaluation_only": True,
        }
    raise EvaluationError(f"unsupported image preprocessor: {image_preprocess}")


def preprocess_images_for_request(
    images: list[tuple[str, bytes, str]], *, image_preprocess: str
) -> tuple[list[tuple[str, bytes, str]], list[dict[str, Any]]]:
    """Transform every request image and emit a label-free integrity manifest."""

    if image_preprocess == IMAGE_PREPROCESS_NONE:
        return images, []
    if image_preprocess != IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        raise EvaluationError(f"unsupported image preprocessor: {image_preprocess}")
    transformed_images: list[tuple[str, bytes, str]] = []
    manifest: list[dict[str, Any]] = []
    for label, image_bytes, mime_type in images:
        result = pixels.fixed_square_letterbox_jpeg(
            image_bytes,
            square_size=512,
            jpeg_quality=95,
            padding_bgr=(0, 0, 0),
        )
        integrity = pixels.validate_fixed_square_letterbox(
            image_bytes,
            result,
            square_size=512,
            jpeg_quality=95,
            padding_bgr=(0, 0, 0),
        )
        if not integrity["passed"]:
            raise EvaluationError(f"normalizer integrity check failed for {label}")
        metadata = result.metadata
        manifest.append(
            {
                "label": label,
                "mime_type": mime_type,
                "preprocessor": pixels.LETTERBOX_PREPROCESSOR_ID,
                "source": metadata["source"],
                "normalized": metadata["target"],
                "geometry": metadata["geometry"],
                "codec": metadata["codec"],
                "runtime_integrity": integrity,
            }
        )
        transformed_images.append((label, result.image_bytes, mime_type))
    return transformed_images, manifest


def run_normalizer_unit_tests(*, image_preprocess: str, run_pytest: bool) -> dict[str, Any]:
    """Record deterministic normalizer checks before any live image POST."""

    if image_preprocess == IMAGE_PREPROCESS_NONE:
        return {"status": "not_requested"}
    if image_preprocess != IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        raise EvaluationError(f"unsupported image preprocessor: {image_preprocess}")
    contract = pixels.letterbox_unit_contract_report()
    if not contract["passed"]:
        raise EvaluationError("letterbox synthetic unit contract failed")
    result: dict[str, Any] = {
        "status": "passed",
        "preprocessor": pixels.LETTERBOX_PREPROCESSOR_ID,
        "synthetic_unit_contract": contract,
    }
    if run_pytest:
        test_path = Path(__file__).with_name("test_mayo_pixel_preprocess.py")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(test_path)],
                cwd=str(Path(__file__).resolve().parent),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvaluationError(f"normalizer pytest preflight could not run: {exc}") from exc
        combined = (completed.stdout + "\n" + completed.stderr).strip()
        pytest_record = {
            "command": [sys.executable, "-m", "pytest", "-q", test_path.name],
            "exit_code": completed.returncode,
            "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
            "output_tail": combined[-1000:],
        }
        result["pytest"] = pytest_record
        if completed.returncode != 0:
            raise EvaluationError("normalizer pytest preflight failed")
    return result


def _data_url(image_bytes: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def build_request_body(
    *,
    sample: Sample,
    variant: str,
    images: list[tuple[str, bytes, str]],
    model_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    context = request_context_for(sample)
    assert_request_is_label_free(sample, context)
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": "Task context JSON:\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))}
    ]
    for label, image_bytes, mime_type in images:
        user_content.extend(
            [
                {"type": "text", "text": f"Image label: {label}"},
                {"type": "image_url", "image_url": {"url": _data_url(image_bytes, mime_type)}},
            ]
        )
    prompt = prompt_for(sample.mode, variant)
    body = {
        "model": model_id,
        **MODEL_REQUEST_CONFIG,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
    }
    return body, context, prompt


def _chat_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise EvaluationError("NInfer base URL is empty")
    return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"


def _models_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise EvaluationError("NInfer base URL is empty")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


@contextmanager
def _exclusive_ninfer_lock(lock_path: Path):
    """Serialize every NInfer inference HTTP request across prompt agents."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def parse_model_health(payload: Any, *, model_id: str) -> dict[str, Any]:
    """Normalize the read-only NInfer catalog response for audit output."""

    root = _require_object(payload, what="NInfer model catalog")
    rows = root.get("data", root.get("models", []))
    if not isinstance(rows, list):
        return {
            "catalog_valid": False,
            "model_present": False,
            "model_loaded": False,
            "capability": "",
            "load_state": "",
        }
    row = next(
        (
            item
            for item in rows
            if isinstance(item, dict)
            and str(item.get("id") or item.get("model") or "").strip() == model_id
        ),
        None,
    )
    if row is None:
        return {
            "catalog_valid": True,
            "model_present": False,
            "model_loaded": False,
            "capability": "",
            "load_state": "",
        }
    raw_state = str(row.get("load_state") or row.get("state") or "").strip().lower()
    loaded = row.get("loaded")
    model_loaded = bool(loaded) if isinstance(loaded, bool) else raw_state in {"loaded", "ready", "running"}
    capability = str(row.get("capability") or "").strip().lower()
    modalities = row.get("modalities")
    if not capability and isinstance(modalities, list):
        capability = "vision" if any(str(value).lower() in {"vision", "image", "multimodal"} for value in modalities) else ""
    return {
        "catalog_valid": True,
        "model_present": True,
        "model_loaded": model_loaded,
        "capability": capability,
        "load_state": raw_state,
    }


def parse_direct_worker_readiness(payload: Any, *, model_id: str) -> dict[str, Any]:
    """Validate that the manager has pointed at a direct worker with the model.

    The direct worker's OpenAI-compatible catalog need only establish that the
    requested model is served there.  It intentionally is not treated as a
    substitute for the manager's authoritative load-state catalog.
    """

    root = _require_object(payload, what="NInfer direct-worker model catalog")
    rows = root.get("data", root.get("models", []))
    if not isinstance(rows, list):
        return {"catalog_valid": False, "worker_model_present": False}
    return {
        "catalog_valid": True,
        "worker_model_present": any(
            isinstance(row, dict)
            and str(row.get("id") or row.get("model") or "").strip() == model_id
            for row in rows
        ),
    }


def _response_text(payload: Any) -> str:
    root = _require_object(payload, what="NInfer response")
    choices = root.get("choices")
    if not isinstance(choices, list) or not choices:
        raise EvaluationError("NInfer response has no choices")
    choice = _require_object(choices[0], what="NInfer choice")
    message = _require_object(choice.get("message"), what="NInfer message")
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(row.get("text", ""))
            for row in content
            if isinstance(row, dict) and row.get("type") == "text"
        )
    return str(content)


class NInferEvalSession:
    """Lifecycle-guarded NInfer evaluation transport.

    The native vision worker is treated as unsafe beyond a small lifetime.
    Every non-dry evaluation batch therefore holds the shared lock, performs a
    fresh manager unload/load, proves the direct worker catalog serves the
    model, and sends at most three inference POSTs before releasing the lock.
    This is intentionally slower than a shared long-lived worker, but keeps a
    worker crash from silently contaminating a frozen accuracy comparison.
    """

    def __init__(
        self,
        *,
        base_url: str,
        worker_base_url: str,
        api_key: str,
        model_id: str,
        timeout_sec: float,
        lifecycle_timeout_sec: float,
        lock_path: Path,
        batch_size: int,
    ) -> None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise EvaluationError("requests is required for NInfer evaluation") from exc
        self._requests = requests
        self.base_url = base_url
        self.worker_base_url = worker_base_url
        self.model_id = model_id
        self.timeout_sec = timeout_sec
        self.lifecycle_timeout_sec = lifecycle_timeout_sec
        self.lock_path = lock_path
        self.batch_size = batch_size
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.health_history: list[dict[str, Any]] = []
        self.worker_health_history: list[dict[str, Any]] = []
        self.batch_history: list[dict[str, Any]] = []
        self.total_inference_requests = 0
        self.requires_manager_reload = False
        self._lock_held = False
        self._in_fresh_batch = False
        self._batch_inference_requests = 0

    def _request(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> Any:
        """Make one HTTP request under the shared lock unless a batch owns it."""

        def send() -> Any:
            kwargs: dict[str, Any] = {
                "headers": self.headers,
                "timeout": self.timeout_sec if timeout_sec is None else timeout_sec,
            }
            if payload is not None:
                kwargs["json"] = payload
            return self._requests.request(method=method, url=url, **kwargs)

        if self._lock_held:
            return send()
        with _exclusive_ninfer_lock(self.lock_path):
            return send()

    def _manager_catalog_record(self, reason: str, *, require_loaded: bool) -> dict[str, Any]:
        started = time.monotonic()
        record: dict[str, Any]
        try:
            response = self._request(method="GET", url=_models_url(self.base_url))
            response.raise_for_status()
            normalized = parse_model_health(response.json(), model_id=self.model_id)
            loaded = bool(normalized["model_loaded"])
            present = bool(normalized["model_present"])
            healthy = loaded if require_loaded else present and normalized["load_state"] != "error"
            record = {
                "reason": reason,
                "latency_sec": round(time.monotonic() - started, 6),
                **normalized,
                "action": "continue" if healthy else "fresh_lifecycle_required",
            }
        except (self._requests.RequestException, EvaluationError, ValueError) as exc:
            record = {
                "reason": reason,
                "latency_sec": round(time.monotonic() - started, 6),
                "catalog_valid": False,
                "model_present": False,
                "model_loaded": False,
                "capability": "",
                "load_state": "",
                "error": str(exc)[:500],
                "action": "fresh_lifecycle_required",
            }
        self.health_history.append(record)
        return record

    def check_health(self, reason: str) -> dict[str, Any]:
        """Check the manager catalog after a request or completed batch."""

        record = self._manager_catalog_record(reason, require_loaded=True)
        self.requires_manager_reload = self.requires_manager_reload or not bool(record["model_loaded"])
        return record

    def require_manager_catalog(self, reason: str) -> None:
        """A preflight that permits an unloaded model because fresh load follows."""

        record = self._manager_catalog_record(reason, require_loaded=False)
        if not record["model_present"] or record["load_state"] == "error":
            self.requires_manager_reload = True
            raise EvaluationError(
                "NInfer manager catalog is unavailable or reports an error state; "
                "cannot begin fresh lifecycle batch"
            )

    def check_direct_worker(self, reason: str) -> dict[str, Any]:
        """Verify that the manager's direct worker actually exposes the model."""

        started = time.monotonic()
        record: dict[str, Any]
        try:
            response = self._request(
                method="GET",
                url=_models_url(self.worker_base_url),
                timeout_sec=min(5.0, self.timeout_sec),
            )
            response.raise_for_status()
            normalized = parse_direct_worker_readiness(response.json(), model_id=self.model_id)
            ready = bool(normalized["worker_model_present"])
            record = {
                "reason": reason,
                "latency_sec": round(time.monotonic() - started, 6),
                **normalized,
                "worker_ready": ready,
                "action": "continue" if ready else "fresh_lifecycle_required",
            }
        except (self._requests.RequestException, EvaluationError, ValueError) as exc:
            record = {
                "reason": reason,
                "latency_sec": round(time.monotonic() - started, 6),
                "catalog_valid": False,
                "worker_model_present": False,
                "worker_ready": False,
                "error": str(exc)[:500],
                "action": "fresh_lifecycle_required",
            }
        self.worker_health_history.append(record)
        return record

    def _wait_for_lifecycle_state(self, expected_state: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        deadline = time.monotonic() + self.lifecycle_timeout_sec
        last_state = ""
        while time.monotonic() < deadline:
            manager = self._manager_catalog_record(
                f"lifecycle_wait_{expected_state}", require_loaded=False
            )
            last_state = str(manager.get("load_state", ""))
            if not manager["model_present"]:
                raise EvaluationError("model disappeared from NInfer manager catalog during lifecycle")
            if last_state == "error":
                raise EvaluationError("NInfer manager entered error state during lifecycle")
            if expected_state == "unloaded":
                if last_state == "unloaded" and not manager["model_loaded"]:
                    return manager, None
            elif expected_state == "loaded":
                if last_state == "loaded" and manager["model_loaded"]:
                    worker = self.check_direct_worker("lifecycle_wait_loaded_direct_worker")
                    if worker["worker_ready"]:
                        return manager, worker
            else:  # pragma: no cover - internal fixed choices only
                raise EvaluationError(f"unsupported expected lifecycle state: {expected_state}")
            time.sleep(0.5)
        raise EvaluationError(
            f"timed out waiting for NInfer manager state {expected_state}; last state={last_state!r}"
        )

    def _post_lifecycle_action(self, action: str) -> None:
        response = self._request(
            method="POST",
            url=f"{self.base_url.rstrip('/')}/manager/{action}",
            payload={"model_id": self.model_id},
            timeout_sec=self.lifecycle_timeout_sec,
        )
        response.raise_for_status()

    def _fresh_reload(self) -> dict[str, Any]:
        """Unload/load under the already-held global evaluation lock."""

        started = time.monotonic()
        record: dict[str, Any] = {
            "guard": "fresh_unload_load_then_direct_worker_readiness",
            "status": "starting",
        }
        try:
            self._post_lifecycle_action("unload")
            unloaded, _unused = self._wait_for_lifecycle_state("unloaded")
            self._post_lifecycle_action("load")
            loaded, worker = self._wait_for_lifecycle_state("loaded")
            record.update(
                {
                    "status": "ready",
                    "unload_manager_state": unloaded["load_state"],
                    "load_manager_state": loaded["load_state"],
                    "direct_worker_ready": bool(worker and worker["worker_ready"]),
                    "latency_sec": round(time.monotonic() - started, 6),
                }
            )
            return record
        except (self._requests.RequestException, EvaluationError, ValueError) as exc:
            record.update(
                {
                    "status": "failed",
                    "error": str(exc)[:500],
                    "latency_sec": round(time.monotonic() - started, 6),
                }
            )
            raise EvaluationError(f"fresh NInfer lifecycle guard failed: {record['error']}") from exc

    @contextmanager
    def fresh_batch(self, *, batch_index: int, sample_ids: list[str]):
        """Own the lock through one fresh worker lifecycle and <=3 POSTs."""

        batch: dict[str, Any] = {
            "batch_index": batch_index,
            "sample_ids": sample_ids,
            "planned_sample_count": len(sample_ids),
            "max_inference_requests": self.batch_size,
            "status": "pending",
        }
        self.batch_history.append(batch)
        with _exclusive_ninfer_lock(self.lock_path):
            self._lock_held = True
            self._in_fresh_batch = True
            self._batch_inference_requests = 0
            try:
                batch["lifecycle"] = self._fresh_reload()
                batch["status"] = "running"
                body_error: EvaluationError | None = None
                try:
                    yield batch
                except EvaluationError as exc:
                    # Still capture end-of-batch readiness before halting the run.
                    body_error = exc
                manager = self.check_health("post_fresh_batch_manager_health")
                worker = self.check_direct_worker("post_fresh_batch_direct_worker_readiness")
                batch["post_batch_readiness"] = {
                    "manager_loaded": bool(manager["model_loaded"]),
                    "direct_worker_ready": bool(worker["worker_ready"]),
                }
                if not manager["model_loaded"] or not worker["worker_ready"]:
                    raise EvaluationError("fresh batch lost manager or direct-worker readiness")
                if body_error is not None:
                    raise body_error
            except EvaluationError as exc:
                self.requires_manager_reload = True
                batch["status"] = "failed"
                batch["error"] = str(exc)[:500]
                raise
            else:
                batch["status"] = "completed"
            finally:
                batch["inference_http_request_count"] = self._batch_inference_requests
                self._in_fresh_batch = False
                self._lock_held = False
                self._batch_inference_requests = 0

    def _post_once(self, body: dict[str, Any]) -> tuple[Any, float]:
        if not self._in_fresh_batch:
            raise EvaluationError("NInfer inference requires a fresh worker batch")
        if self._batch_inference_requests >= self.batch_size:
            raise EvaluationError(
                "fresh worker request budget exhausted; do not exceed three POSTs before reload"
            )
        started = time.monotonic()
        try:
            response = self._request(method="POST", url=_chat_url(self.base_url), payload=body)
        finally:
            self.total_inference_requests += 1
            self._batch_inference_requests += 1
        return response, time.monotonic() - started

    def request_json(self, body: dict[str, Any], *, retries: int) -> tuple[str, float, int]:
        last_error = ""
        for attempt in range(retries + 1):
            try:
                response, latency_sec = self._post_once(body)
                response.raise_for_status()
                raw_text = _response_text(response.json())
                return raw_text, latency_sec, attempt
            except (self._requests.RequestException, EvaluationError, ValueError) as exc:
                detail = ""
                response = getattr(exc, "response", None)
                if response is not None:
                    detail = response.text.replace("\n", " ")[:500]
                last_error = f"NInfer request failed: {exc}{(': ' + detail) if detail else ''}"
                manager = self.check_health("inference_failure_manager_health")
                worker = self.check_direct_worker("inference_failure_direct_worker_readiness")
                if not manager["model_loaded"] or not worker["worker_ready"]:
                    break
                if attempt < retries and self._batch_inference_requests < self.batch_size:
                    time.sleep(min(4.0, 0.75 * (attempt + 1)))
                elif attempt < retries:
                    last_error += "; retry suppressed because fresh-batch POST budget is exhausted"
                    break
        self.requires_manager_reload = True
        raise EvaluationError(last_error or "NInfer request failed")


def parse_model_json(raw_text: str) -> dict[str, Any] | None:
    """Parse one JSON object while tolerating a Markdown fence only."""

    candidate = raw_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    start = candidate.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _allowed_prediction_ids(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    ids: list[str] = []
    for row in rows:
        if isinstance(row, list) and row and isinstance(row[0], str):
            tool_id = row[0].strip()
            if tool_id in ALLOWED_TOOL_IDS and tool_id not in ids:
                ids.append(tool_id)
    return ids


def _is_confidence(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= float(value) <= 1.0
    )


def output_contract_valid(sample: Sample, parsed: dict[str, Any] | None) -> bool:
    """Validate the prompt's compact output contract without normalizing it."""

    if parsed is None or not isinstance(parsed.get("abstain"), bool):
        return False
    if sample.mode == "crop":
        tool_id = parsed.get("tool_id")
        return (
            isinstance(tool_id, str)
            and (not tool_id or tool_id in ALLOWED_TOOL_IDS)
            and _is_confidence(parsed.get("confidence"))
        )
    key = "visible" if sample.mode == "inventory" else "newly_on_mayo"
    rows = parsed.get(key)
    if not isinstance(rows, list):
        return False
    seen: set[str] = set()
    for row in rows:
        expected_length = 3 if sample.mode == "inventory" else 2
        if not isinstance(row, list) or len(row) != expected_length:
            return False
        tool_id = row[0]
        if not isinstance(tool_id, str) or tool_id not in ALLOWED_TOOL_IDS or tool_id in seen:
            return False
        seen.add(tool_id)
        if sample.mode == "inventory":
            count = row[1]
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                return False
            confidence = row[2]
        else:
            confidence = row[1]
        if not _is_confidence(confidence):
            return False
    return True


def score_sample(sample: Sample, parsed: dict[str, Any] | None) -> dict[str, Any]:
    if parsed is None:
        return {"valid_json": False, "contract_valid": False, "mode": sample.mode}
    contract_valid = output_contract_valid(sample, parsed)
    if sample.mode == "crop":
        predicted = str(parsed.get("tool_id", "")).strip()
        return {
            "valid_json": True,
            "contract_valid": contract_valid,
            "mode": sample.mode,
            "expected": sample.expected,
            "predicted": predicted,
            "correct": predicted == sample.expected,
        }
    if sample.mode == "inventory":
        predicted_counts: dict[str, int] = {}
        for row in parsed.get("visible", []):
            if not isinstance(row, list) or len(row) < 2 or not isinstance(row[0], str):
                continue
            tool_id = row[0].strip()
            count = row[1]
            if tool_id in ALLOWED_TOOL_IDS and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                predicted_counts[tool_id] = count
        expected_counts = dict(sample.expected)
        matched = sum(min(predicted_counts.get(tool_id, 0), expected_count) for tool_id, expected_count in expected_counts.items())
        predicted_total = sum(predicted_counts.values())
        expected_total = sum(expected_counts.values())
        return {
            "valid_json": True,
            "contract_valid": contract_valid,
            "mode": sample.mode,
            "expected": expected_counts,
            "predicted": dict(sorted(predicted_counts.items())),
            "matched_instances": matched,
            "expected_instances": expected_total,
            "predicted_instances": predicted_total,
            "precision": matched / predicted_total if predicted_total else 0.0,
            "recall": matched / expected_total if expected_total else 0.0,
            "exact": predicted_counts == expected_counts,
        }
    if sample.mode == "arrival":
        predicted = _allowed_prediction_ids(parsed.get("newly_on_mayo"))
        target = str(sample.expected)
        false_positives = [tool_id for tool_id in predicted if tool_id != target]
        return {
            "valid_json": True,
            "contract_valid": contract_valid,
            "mode": sample.mode,
            "expected": target,
            "predicted": predicted,
            "target_recalled": target in predicted,
            "false_positives": false_positives,
            "exact": predicted == [target],
        }
    raise EvaluationError(f"unsupported sample mode: {sample.mode}")


def summarize(scores: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        by_mode.setdefault(str(score.get("mode", "unknown")), []).append(score)
    summary: dict[str, Any] = {}
    for mode, rows in by_mode.items():
        model_outputs = [
            row
            for row in rows
            if not row.get("transport_error") and not row.get("not_inferred")
        ]
        valid = [row for row in rows if row.get("valid_json")]
        contract_valid = [row for row in rows if row.get("contract_valid")]
        denominator = len(model_outputs)
        transport_errors = sum(bool(row.get("transport_error")) for row in rows)
        not_inferred = sum(bool(row.get("not_inferred")) for row in rows)
        if mode == "crop":
            summary[mode] = {
                "attempted": len(rows),
                "model_outputs": denominator,
                "transport_errors": transport_errors,
                "not_inferred": not_inferred,
                "valid_json": len(valid),
                "contract_valid": len(contract_valid),
                "correct": sum(bool(row.get("correct")) for row in valid),
                "accuracy": (
                    sum(bool(row.get("correct")) for row in valid) / denominator
                    if denominator
                    else None
                ),
                "accepted_correct": sum(
                    bool(row.get("correct")) and bool(row.get("contract_valid"))
                    for row in rows
                ),
                "accepted_accuracy": (
                    sum(
                        bool(row.get("correct")) and bool(row.get("contract_valid"))
                        for row in rows
                    )
                    / denominator
                    if denominator
                    else None
                ),
            }
        elif mode == "inventory":
            summary[mode] = {
                "attempted": len(rows),
                "model_outputs": denominator,
                "transport_errors": transport_errors,
                "not_inferred": not_inferred,
                "valid_json": len(valid),
                "contract_valid": len(contract_valid),
                "exact": sum(bool(row.get("exact")) for row in valid),
                "accepted_exact": sum(
                    bool(row.get("exact")) and bool(row.get("contract_valid"))
                    for row in rows
                ),
                "mean_precision": sum(float(row.get("precision", 0.0)) for row in valid) / denominator if denominator else None,
                "mean_recall": sum(float(row.get("recall", 0.0)) for row in valid) / denominator if denominator else None,
            }
        elif mode == "arrival":
            summary[mode] = {
                "attempted": len(rows),
                "model_outputs": denominator,
                "transport_errors": transport_errors,
                "not_inferred": not_inferred,
                "valid_json": len(valid),
                "contract_valid": len(contract_valid),
                "target_recall": sum(bool(row.get("target_recalled")) for row in valid) / denominator if denominator else None,
                "exact_match": sum(bool(row.get("exact")) for row in valid) / denominator if denominator else None,
                "accepted_target_recall": (
                    sum(
                        bool(row.get("target_recalled")) and bool(row.get("contract_valid"))
                        for row in rows
                    )
                    / denominator
                    if denominator
                    else None
                ),
                "accepted_exact_match": (
                    sum(
                        bool(row.get("exact")) and bool(row.get("contract_valid"))
                        for row in rows
                    )
                    / denominator
                    if denominator
                    else None
                ),
                "false_positive_total": sum(len(row.get("false_positives", [])) for row in valid),
            }
    return summary


def _write_json_new(path: Path, payload: Any) -> None:
    if path.exists():
        raise EvaluationError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value using the stable representation used by frozen locks."""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def frozen_config_for(
    *,
    model_id: str,
    event_reference_sha256: str,
    samples: list[Sample],
) -> dict[str, Any]:
    """Return the immutable configuration that a selected frozen run must match."""

    if not samples or any(sample.mode != "arrival" for sample in samples):
        raise EvaluationError("frozen configuration requires non-empty arrival samples")
    sample_ids = [sample.sample_id for sample in samples]
    expected_ids = [f"0704_5-challenge-arrival-{event_id}" for event_id in FROZEN_CHALLENGE_EVENT_IDS]
    if sample_ids != expected_ids:
        raise EvaluationError("frozen configuration sample IDs do not match the pre-registered challenge")
    arrival_context = request_context_for(samples[0])
    return {
        "model_id": model_id,
        "selected_variant": "optimized_v4",
        "prompt_version": prompt_version_for("optimized_v4"),
        "prompt_sha256_by_mode": {
            "arrival": hashlib.sha256(prompt_for("arrival", "optimized_v4").encode("utf-8")).hexdigest(),
        },
        "tool_catalog_sha256": canonical_json_sha256(TOOL_CATALOG),
        "arrival_request_context_sha256": canonical_json_sha256(arrival_context),
        "image_preprocess_flag": IMAGE_PREPROCESS_LETTERBOX_512_Q95,
        # Return value must not share mutable nested objects with the runtime
        # constants.  A caller mutating a candidate lock must never change the
        # configuration subsequently used to validate that lock.
        "image_preprocess_policy": json.loads(
            json.dumps(image_preprocess_policy(IMAGE_PREPROCESS_LETTERBOX_512_Q95))
        ),
        "model_request_config": dict(MODEL_REQUEST_CONFIG),
        "threshold_policy": dict(NO_THRESHOLD_POLICY),
        "batch_size": 1,
        "retries": 0,
        "score_only_if_complete": True,
        "event_reference_sha256": event_reference_sha256,
        "sample_ids": sample_ids,
    }


def validate_frozen_selection(
    *,
    selection_path: Path | None,
    model_id: str,
    event_reference_sha256: str,
    samples: list[Sample],
) -> dict[str, Any]:
    """Refuse a frozen POST unless the prior calibration selection is locked."""

    if selection_path is None:
        raise EvaluationError("frozen evaluation requires an explicit locked selection artifact")
    try:
        payload = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read frozen selection artifact: {selection_path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError("frozen selection artifact must be a JSON object")
    if payload.get("schema") != "taskplanner.mayo_frozen_selection.v1":
        raise EvaluationError("frozen selection artifact has an unsupported schema")
    if payload.get("selection_status") != "locked":
        raise EvaluationError("frozen selection artifact is not locked")
    expected = frozen_config_for(
        model_id=model_id,
        event_reference_sha256=event_reference_sha256,
        samples=samples,
    )
    actual = payload.get("frozen_config")
    if not isinstance(actual, dict):
        raise EvaluationError("frozen selection artifact has no frozen_config")
    if canonical_json_sha256(actual) != canonical_json_sha256(expected):
        raise EvaluationError("frozen selection artifact does not match current prompt/preprocess/threshold config")
    selection_id = payload.get("selection_id")
    if not isinstance(selection_id, str) or not selection_id:
        raise EvaluationError("frozen selection artifact has no selection ID")
    return {
        "status": "validated_locked_selection",
        "path": str(selection_path),
        "sha256": sha256_file(selection_path),
        "selection_id": selection_id,
        "frozen_config_sha256": canonical_json_sha256(actual),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    events = load_events(args.events)
    samples = (
        make_calibration_samples(events)
        if args.suite == "calibration"
        else make_frozen_arrival_samples(events)
    )
    if args.offset:
        samples = samples[args.offset :]
    if args.max_samples is not None:
        samples = samples[: args.max_samples]
    if not samples:
        raise EvaluationError("no samples selected")
    event_reference_sha256 = sha256_file(args.events)
    frozen_selection_lock = (
        validate_frozen_selection(
            selection_path=args.frozen_selection,
            model_id=args.model_id,
            event_reference_sha256=event_reference_sha256,
            samples=samples,
        )
        if args.suite == "frozen_arrival"
        else {"status": "not_required"}
    )
    wanted_indices = {index for sample in samples for index in sample.frame_indices}
    frames = _decode_cam4_frames(args.bag, wanted_indices)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_root / f"{run_id}_{args.suite}_{args.variant}"
    if output_dir.exists():
        raise EvaluationError(f"output run already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    normalizer_validation = run_normalizer_unit_tests(
        image_preprocess=args.image_preprocess,
        run_pytest=bool(args.run_normalizer_unit_tests),
    )

    api_key = os.environ.get(args.api_key_env, "")
    records: list[dict[str, Any]] = []
    session: NInferEvalSession | None = None
    halt_reason = ""
    if not args.dry_run:
        session = NInferEvalSession(
            base_url=args.base_url,
            worker_base_url=args.worker_base_url,
            api_key=api_key,
            model_id=args.model_id,
            timeout_sec=args.timeout_sec,
            lifecycle_timeout_sec=args.lifecycle_timeout_sec,
            lock_path=args.lock_path,
            batch_size=args.batch_size,
        )
        try:
            # This deliberately permits an unloaded model: the fresh batch
            # lifecycle below owns the load, rather than trusting a prior run.
            session.require_manager_catalog("preflight_manager_catalog")
        except EvaluationError as exc:
            halt_reason = str(exc)

    def evaluate_one(sample: Sample, ordinal: int) -> str:
        source_images = images_for(sample, frames)
        images, image_manifest = preprocess_images_for_request(
            source_images,
            image_preprocess=args.image_preprocess,
        )
        body, context, prompt = build_request_body(
            sample=sample,
            variant=args.variant,
            images=images,
            model_id=args.model_id,
        )
        # Never save base64 image bodies; they are redundant with the immutable bag.
        input_record = {
            "sample_id": sample.sample_id,
            "mode": sample.mode,
            "frame_indices": list(sample.frame_indices),
            "image_labels": [label for label, _data, _mime in images],
            "image_preprocess": image_preprocess_policy(args.image_preprocess),
            "image_manifest": image_manifest,
            "request_context": context,
            "prompt_version": prompt_version_for(args.variant),
            "prompt": prompt,
            "request_sha256": hashlib.sha256(
                json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }
        if args.dry_run:
            raw_text = ""
            latency_sec = 0.0
            parsed = None
            retry_count = 0
            request_error = ""
        else:
            try:
                if session is None:  # pragma: no cover - defensive branch
                    raise EvaluationError("NInfer session was not initialized")
                raw_text, latency_sec, retry_count = session.request_json(body, retries=args.retries)
                parsed = parse_model_json(raw_text)
                request_error = ""
            except EvaluationError as exc:
                raw_text = ""
                latency_sec = 0.0
                retry_count = args.retries
                parsed = None
                request_error = str(exc)
        if args.score_only_if_complete:
            # The normalized calibration profile must not leak a partial
            # metric when a later fresh-worker batch fails.
            score = {
                "mode": sample.mode,
                "scoring_performed": False,
                "transport_error": bool(request_error),
                "not_inferred": bool(args.dry_run),
            }
        else:
            score = score_sample(sample, parsed)
            score["transport_error"] = bool(request_error)
            score["not_inferred"] = bool(args.dry_run)
        records.append(
            {
                "ordinal": ordinal,
                "input": input_record,
                # The following three fields are evaluation-side only.  They are
                # constructed after inference and never enter ``body``.
                "evaluation_reference": sample.expected,
                "raw_model_response": raw_text,
                "parsed_model_response": parsed,
                "latency_sec": round(latency_sec, 6),
                "retry_count": retry_count,
                "request_error": request_error,
                "score": score,
            }
        )
        return request_error

    if args.dry_run:
        for sample in samples:
            evaluate_one(sample, len(records) + 1)
    elif not halt_reason:
        if session is None:  # pragma: no cover - defensive branch
            raise EvaluationError("NInfer session was not initialized")
        for start in range(0, len(samples), args.batch_size):
            batch_samples = samples[start : start + args.batch_size]
            try:
                with session.fresh_batch(
                    batch_index=start // args.batch_size + 1,
                    sample_ids=[sample.sample_id for sample in batch_samples],
                ):
                    for sample in batch_samples:
                        request_error = evaluate_one(sample, len(records) + 1)
                        if request_error:
                            # The record is retained as a transport failure,
                            # then the fresh-batch context records post-failure
                            # readiness and stops before another sample.
                            raise EvaluationError(request_error)
            except EvaluationError as exc:
                halt_reason = str(exc)
                break
    complete_model_run = (
        not args.dry_run and not halt_reason and len(records) == len(samples)
    )
    if args.score_only_if_complete and complete_model_run:
        sample_by_id = {sample.sample_id: sample for sample in samples}
        for record in records:
            input_record = record.get("input") if isinstance(record.get("input"), dict) else {}
            sample_id = str(input_record.get("sample_id", ""))
            sample = sample_by_id.get(sample_id)
            if sample is None:  # pragma: no cover - derived from the selected suite
                raise EvaluationError(f"cannot score an unknown selected sample: {sample_id}")
            score = score_sample(sample, record.get("parsed_model_response"))
            score["transport_error"] = bool(record.get("request_error"))
            score["not_inferred"] = False
            score["scoring_performed"] = True
            record["score"] = score
        summary: dict[str, Any] = summarize([record["score"] for record in records])
        scoring = {
            "performed": True,
            "policy": "score_only_after_all_selected_samples_complete",
            "prior_transport_probes_excluded": True,
        }
    elif args.score_only_if_complete:
        summary = {
            "status": "not_scored",
            "reason": "complete_normalized_calibration_required_before_any_metric",
        }
        scoring = {
            "performed": False,
            "policy": "score_only_after_all_selected_samples_complete",
            "reason": "run_halted_or_not_a_completed_live_inference",
            "prior_transport_probes_excluded": True,
        }
    else:
        summary = summarize([record["score"] for record in records])
        scoring = {
            "performed": not args.dry_run,
            "policy": "per_record_evaluation",
        }

    all_image_integrity_passed = all(
        bool(image.get("runtime_integrity", {}).get("passed"))
        for record in records
        for image in (
            record.get("input", {}).get("image_manifest", [])
            if isinstance(record.get("input"), dict)
            else []
        )
    )
    image_manifest_count = sum(
        len(record.get("input", {}).get("image_manifest", []))
        for record in records
        if isinstance(record.get("input"), dict)
    )
    result = {
        "schema": "taskplanner.mayo_prompt_evaluation.v1",
        "prompt_version": prompt_version_for(args.variant),
        "suite": args.suite,
        "variant": args.variant,
        "model": args.model_id,
        "endpoint": args.base_url.rstrip("/"),
        "dry_run": bool(args.dry_run),
        "execution": {
            "status": "completed" if not halt_reason else "halted",
            "halt_reason": halt_reason,
            "attempted_samples": len(records),
            "unexecuted_sample_ids": [sample.sample_id for sample in samples[len(records) :]],
            "shared_lock_path": str(args.lock_path),
            "max_inference_requests_per_fresh_worker_batch": args.batch_size,
            "manager_lifecycle_policy": "fresh_unload_load_before_each_batch_under_shared_lock",
            "manager_endpoint": args.base_url.rstrip("/"),
            "direct_worker_endpoint": args.worker_base_url.rstrip("/"),
            "lifecycle_invoked": bool(not args.dry_run),
            "health_history": session.health_history if session is not None else [],
            "direct_worker_health_history": session.worker_health_history if session is not None else [],
            "batches": session.batch_history if session is not None else [],
            "inference_http_request_count": session.total_inference_requests if session is not None else 0,
        },
        "source": {
            "bag": str(args.bag),
            "event_reference": str(args.events),
            "event_reference_sha256": event_reference_sha256,
            "case_id": "0704_5",
            "ground_truth_policy": "evaluation_only_never_in_model_request",
            "split_policy": (
                "t0_localization_conditioned_inventory_and_crops_plus_early_arrival_calibration"
                if args.suite == "calibration"
                else "pre_registered_late_time_separated_clear_cam4_arrival_challenge"
            ),
            "temporal_partitions": {
                "calibration_arrival_event_ids": list(CALIBRATION_ARRIVAL_EVENT_IDS),
                "frozen_challenge_event_ids": list(FROZEN_CHALLENGE_EVENT_IDS),
            },
        },
        "sample_selection": {
            "offset": args.offset,
            "max_samples": args.max_samples,
        },
        "image_policy": {
            "cam4_topic": CAM4_TOPIC,
            "arrival_offsets_frames": {
                "before": -PRE_EVENT_FRAME_OFFSET,
                "after": POST_EVENT_FRAME_OFFSET,
            },
            "preprocessor": image_preprocess_policy(args.image_preprocess),
        },
        "normalizer_validation": {
            **normalizer_validation,
            "request_image_manifest_count": image_manifest_count,
            "all_request_image_integrity_checks_passed": all_image_integrity_passed,
            "visual_review_required_after_complete_run": args.image_preprocess
            == IMAGE_PREPROCESS_LETTERBOX_512_Q95,
        },
        "frozen_selection_lock": frozen_selection_lock,
        "scoring": scoring,
        "summary": summary,
        "records": records,
    }
    _write_json_new(output_dir / "result.json", result)
    return {"output_dir": str(output_dir), "summary": result["summary"], "samples": len(records)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("calibration", "frozen_arrival"), required=True)
    parser.add_argument(
        "--variant",
        choices=tuple(PROMPT_VERSION_BY_VARIANT),
        required=True,
    )
    parser.add_argument("--bag", type=Path, default=DEFAULT_BAG)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--model-id", default="qwen3.6-35b-a3b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--worker-base-url", default=DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--batch-size", type=int, default=MAX_REQUESTS_PER_FRESH_WORKER_BATCH)
    parser.add_argument("--max-samples", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--frozen-selection",
        type=Path,
        help="Locked v4 selection artifact required for the one permitted frozen challenge run.",
    )
    parser.add_argument(
        "--image-preprocess",
        choices=(IMAGE_PREPROCESS_NONE, IMAGE_PREPROCESS_LETTERBOX_512_Q95),
        default=IMAGE_PREPROCESS_NONE,
        help="Evaluation-only image normalization; frozen use requires a locked selected-v4 artifact.",
    )
    parser.add_argument(
        "--run-normalizer-unit-tests",
        action="store_true",
        help="Run and record deterministic normalizer tests before a normalized image request.",
    )
    parser.add_argument(
        "--score-only-if-complete",
        action="store_true",
        help="Suppress every metric unless the complete selected live suite succeeds.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_samples is not None and args.max_samples <= 0:
        parser.error("--max-samples must be positive")
    if args.offset < 0:
        parser.error("--offset must be non-negative")
    if args.timeout_sec <= 0 or args.lifecycle_timeout_sec <= 0:
        parser.error("--timeout-sec and --lifecycle-timeout-sec must be positive")
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if not 1 <= args.batch_size <= MAX_REQUESTS_PER_FRESH_WORKER_BATCH:
        parser.error("--batch-size must be between 1 and 3 for the fresh worker lifecycle guard")
    if args.suite == "frozen_arrival":
        if args.variant != "optimized_v4":
            parser.error("frozen_arrival is locked to the selected optimized_v4 prompt")
        if args.image_preprocess != IMAGE_PREPROCESS_LETTERBOX_512_Q95:
            parser.error("frozen_arrival is locked to letterbox_512_q95 preprocessing")
        if args.frozen_selection is None:
            parser.error("frozen_arrival requires --frozen-selection with the locked v4 artifact")
        if args.dry_run:
            parser.error("frozen_arrival must be the one permitted live inference, not a dry-run")
    elif args.frozen_selection is not None:
        parser.error("--frozen-selection is only valid for the frozen_arrival suite")
    if args.image_preprocess == IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        # This narrowly scoped profile exists to make the P2 runtime workaround
        # auditable before it can influence any challenge result.
        allowed = (
            (args.suite == "calibration" and args.variant in {"baseline", "optimized_v4"})
            or (args.suite == "frozen_arrival" and args.variant == "optimized_v4")
        )
        if not allowed:
            parser.error(
                "--image-preprocess letterbox_512_q95 is restricted to calibration baseline/optimized_v4 or locked frozen optimized_v4"
            )
        if args.batch_size != 1:
            parser.error("--image-preprocess letterbox_512_q95 requires --batch-size 1")
        if args.retries != 0:
            parser.error("--image-preprocess letterbox_512_q95 requires --retries 0")
        if args.offset != 0 or args.max_samples is not None:
            parser.error("--image-preprocess letterbox_512_q95 requires the complete selected suite")
        if not args.run_normalizer_unit_tests:
            parser.error("--image-preprocess letterbox_512_q95 requires --run-normalizer-unit-tests")
        args.score_only_if_complete = True
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(list(argv) if argv is not None else sys.argv[1:]))
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
