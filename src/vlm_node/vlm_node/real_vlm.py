"""Real/synthetic VLM node with LM Studio integration and compact state fan-out."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    ProcedurePriorScorer,
    compact_procedure_prompt,
    get_default_spec_dir,
    infer_retraction_direction,
    load_bundle,
    normalize_retraction_request,
)
import requests
import rclpy
from model_provider_registry import ModelProviderRegistry
from rcl_interfaces.msg import SetParametersResult
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupStatus,
    BTContextSnapshot,
    BTDecision,
    EventDigest,
    ModelCatalogEntry,
    ModelProviderStatus,
    PhaseEvidence,
    SkillStatus,
    SimulationState,
    SurgeonGestureEvidence,
    ToolObservation,
    TwinEvent,
    VLMHealth,
    VLMRequestContext,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import (
    ControlModelRuntime,
    ListModelCatalog,
    ListModels,
    SelectModelProvider,
)

from .common import compact_json
from .lmstudio_client import LMStudioClient
from .prompt_builder import PromptBuilder
from .rfdetr_contract import parse_cam4_semantics_json
from .schema import (
    SchemaValidationError,
    compact_vlm_json_schema,
    normalize_mayo_semantics,
    normalize_raw_text,
)


PUBLIC_DIGITAL_TWIN_EVENT_TYPES = {
    "RobotTaskStarted",
    "RobotTaskCompleted",
    "RobotGraspedTool",
    "ToolPrepared",
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolCleaningProgress",
    "ToolCleaningCompleted",
    "ToolReturnedToTray",
    "PredictedToolReturnedToRack",
    "ProcedureCompleted",
    "InvariantViolation",
    "InvariantViolationIgnored",
}
ACTOR_LOG_CONTEXT_MAX_CHARS = 3200
ACTOR_LOG_EVIDENCE_LIMITS = {
    "speech": 4,
    "observed_signals": 6,
    "skill_status": 4,
}
ACTOR_LOG_EVENT_LIMIT = 4
HANDOVER_SKILL_ACTIONS = {
    "handover",
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
}


def bound_actor_log_context(
    context: dict[str, Any],
    *,
    max_chars: int = ACTOR_LOG_CONTEXT_MAX_CHARS,
) -> dict[str, Any]:
    """Keep dynamic public evidence bounded below small-model context limits."""
    bounded = dict(context)

    # The same immutable ontology is already present in the system prompt.
    bounded.pop("phases", None)
    bounded.pop("tools", None)

    evidence = dict(bounded.get("evidence_window", {}))
    for key, limit in ACTOR_LOG_EVIDENCE_LIMITS.items():
        rows = evidence.get(key, [])
        if isinstance(rows, list):
            evidence[key] = rows[-limit:]
    bounded["evidence_window"] = evidence

    digital_twin = dict(bounded.get("digital_twin", {}))
    events = digital_twin.get("events", [])
    if isinstance(events, list):
        digital_twin["events"] = events[-ACTOR_LOG_EVENT_LIMIT:]
    bounded["digital_twin"] = digital_twin

    shrink_order = (
        (evidence, "observed_signals"),
        (evidence, "skill_status"),
        (evidence, "speech"),
        (digital_twin, "events"),
    )
    while len(compact_json(bounded)) > max_chars:
        removed = False
        for owner, key in shrink_order:
            rows = owner.get(key, [])
            if isinstance(rows, list) and len(rows) > 1:
                del rows[0]
                removed = True
                break
        if not removed:
            break
    return bounded
PUBLIC_REQUEST_MAX_AGE_SEC = 6.0
DEFAULT_CAM4_CROP_XYWH_NORM = (0.32, 0.18, 0.62, 0.78)
DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX = 512
MAX_PUBLIC_PERCEPTION_INSTANCES = 24
IMAGE_PAIR_BUFFER_LENGTH = 32
PERCEPTION_PAIR_BUFFER_LENGTH = 64
CLINICAL_ANALYSIS_MAX_CHARS = 320
CAM4_MAYO_MIN_CONFIDENCE = 0.65
CAM4_MAYO_MIN_STABLE_SAMPLES = 3
CAM4_MAYO_MIN_STABLE_DURATION_SEC = 0.25
CAM4_MAYO_STABILITY_WINDOW_SEC = 0.75
CAM4_VISUAL_GESTURE_MIN_CONFIDENCE = 0.35
INFERENCE_TRIGGER_PERIODIC_LIVE = "periodic_live"
INFERENCE_TRIGGER_SPEECH = "speech"
INFERENCE_TRIGGER_REPLAY_FRAME = "replay_frame"
INFERENCE_TRIGGER_SOURCE_FRAME = "source_frame_live"
INFERENCE_TRIGGER_FORCED = "forced"
INFERENCE_FAILURE_HISTORY_LENGTH = 32


def normalize_clinical_analysis(value: Any) -> str:
    """Keep the public summary clinical and remove internal reducer markers."""

    text = " ".join(str(value or "").split())
    if not text:
        return (
            "Current visual evidence is insufficient for a definitive "
            "clinical interpretation."
        )
    internal_markers = (
        "candidate-stabilized",
        "public-sequence-anchor=",
        "phase-bootstrap-waiting-for-public-sequence",
        "public-tool-anchor=",
        "actor-log fallback",
    )
    clinical_parts = [
        part.strip()
        for part in text.split(";")
        if part.strip()
        and not any(marker in part for marker in internal_markers)
    ]
    cleaned = "; ".join(clinical_parts).strip()
    if not cleaned:
        cleaned = (
            "Current visual evidence is insufficient for a definitive "
            "clinical interpretation."
        )
    return cleaned[:CLINICAL_ANALYSIS_MAX_CHARS]


def is_model_ready_visual_source(image_source: str) -> bool:
    """Accept detector-assisted or detector-independent FLIR input."""

    return str(image_source).strip() in {
        "flir_rfdetr_segmented",
        "flir_raw_fallback",
    }


@dataclass(frozen=True, slots=True)
class ImageSample:
    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class ModelImage:
    label: str
    data: bytes
    mime_type: str
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str


@dataclass(frozen=True, slots=True)
class InferenceAdmission:
    disposition: str
    trigger: str

    @property
    def started(self) -> bool:
        return self.disposition == "started"


@dataclass(frozen=True, slots=True)
class InferenceFailure:
    sequence: int
    trigger: str
    mode: str
    error: str
    image_source: str
    latency_sec: float
    retry_count: int
    recorded_monotonic: float


class InferenceBackpressure:
    """Run one inference while retaining only the newest pending request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = False
        self._current_trigger = ""
        self._pending_trigger = ""
        self._coalesced_count = 0

    @staticmethod
    def _normalize_trigger(trigger: str) -> str:
        return str(trigger or INFERENCE_TRIGGER_FORCED).strip()

    def queue(self, trigger: str) -> InferenceAdmission:
        """Queue work without executing it in the producer callback."""

        normalized = self._normalize_trigger(trigger)
        with self._lock:
            disposition = "coalesced" if self._pending_trigger else "queued"
            if self._pending_trigger:
                self._coalesced_count += 1
            self._pending_trigger = normalized
            return InferenceAdmission(disposition, normalized)

    def begin(self) -> str | None:
        """Claim queued work for the single inference consumer."""

        with self._lock:
            if self._in_flight or not self._pending_trigger:
                return None
            self._in_flight = True
            self._current_trigger = self._pending_trigger
            self._pending_trigger = ""
            return self._current_trigger

    def request(self, trigger: str) -> InferenceAdmission:
        """Start immediately when idle, otherwise replace pending work."""

        normalized = self._normalize_trigger(trigger)
        with self._lock:
            if not self._in_flight:
                self._in_flight = True
                self._current_trigger = normalized
                self._pending_trigger = ""
                return InferenceAdmission("started", normalized)
            disposition = "coalesced" if self._pending_trigger else "queued"
            if self._pending_trigger:
                self._coalesced_count += 1
            self._pending_trigger = normalized
            return InferenceAdmission(disposition, normalized)

    def complete(self) -> str | None:
        with self._lock:
            if not self._in_flight:
                return None
            if self._pending_trigger:
                self._current_trigger = self._pending_trigger
                self._pending_trigger = ""
                return self._current_trigger
            self._in_flight = False
            self._current_trigger = ""
            return None

    def clear_pending(self) -> None:
        with self._lock:
            self._pending_trigger = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "current_trigger": self._current_trigger,
                "pending_trigger": self._pending_trigger,
                "coalesced_count": self._coalesced_count,
            }


def image_sample_stamp_sec(sample: ImageSample) -> float:
    return (
        float(sample.stamp_sec)
        + float(sample.stamp_nanosec) / 1_000_000_000.0
    )


def image_samples_are_aligned(
    first: ImageSample,
    second: ImageSample,
    *,
    max_skew_sec: float,
) -> bool:
    return (
        abs(image_sample_stamp_sec(first) - image_sample_stamp_sec(second))
        <= max(0.0, float(max_skew_sec)) + 1.0e-9
    )


def should_trigger_replay_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
    publish_period_sec: float,
) -> bool:
    """Use source-image time, rather than executor timing, for replay cadence."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    return (
        current_stamp_sec - last_stamp_sec + 1.0e-9
        >= max(publish_period_sec, 0.0)
    )


def should_run_periodic_live_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
) -> bool:
    """Reject repeated timer inference over the same recorded frame."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    return current_stamp_sec > last_stamp_sec + 1.0e-9


def should_trigger_source_time_live_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
    publish_period_sec: float,
) -> bool:
    """Tolerate camera cadence jitter around the requested live period."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    period_sec = max(0.0, float(publish_period_sec))
    tolerance_sec = min(0.05, period_sec * 0.05)
    return (
        current_stamp_sec - last_stamp_sec + tolerance_sec + 1.0e-9
        >= period_sec
    )


def source_frame_is_fresh(
    now_sec: float,
    image_stamp_sec: float,
    max_lag_sec: float,
) -> bool:
    """Reject visual evidence that has fallen behind the replay source clock."""

    if max_lag_sec <= 0.0:
        return True
    return now_sec - image_stamp_sec <= max_lag_sec + 1.0e-9


def should_use_open_set_phase_bootstrap(
    observation_count: int,
    max_observations: int,
    explicit_start_phase: bool,
) -> bool:
    """Temporarily remove temporal priors when the camera joins mid-procedure."""

    return (
        not explicit_start_phase
        and max(0, int(max_observations)) > 0
        and max(0, int(observation_count)) < max(0, int(max_observations))
    )


def bound_image_for_model(
    image_bytes: bytes,
    mime_type: str,
    max_side_px: int,
) -> tuple[bytes, str]:
    """Bound vision-token cost while preserving the published source frame."""

    limit = max(0, int(max_side_px))
    if limit <= 0:
        return image_bytes, mime_type
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            if max(source.size) <= limit:
                return image_bytes, mime_type
            resized = source.copy()
            resized.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            output = BytesIO()
            if mime_type == "image/png":
                resized.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png"
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            resized.save(
                output,
                format="JPEG",
                quality=88,
                optimize=True,
            )
            return output.getvalue(), "image/jpeg"
    except (OSError, ValueError):
        return image_bytes, mime_type


def normalize_crop_xywh(
    crop_xywh_norm: tuple[float, float, float, float] | list[float],
) -> tuple[float, float, float, float]:
    """Clamp a normalized crop to a non-empty region inside the source image."""

    if len(crop_xywh_norm) != 4:
        raise ValueError("CAM4 crop must contain x, y, width, height")
    values = tuple(float(value) for value in crop_xywh_norm)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("CAM4 crop values must be finite")
    x, y, width, height = values
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    width = min(max(width, 1.0e-6), 1.0 - x)
    height = min(max(height, 1.0e-6), 1.0 - y)
    if width <= 0.0 or height <= 0.0:
        raise ValueError("CAM4 crop must overlap the source image")
    return x, y, width, height


def crop_image_normalized(
    image: Image.Image,
    crop_xywh_norm: tuple[float, float, float, float] | list[float],
) -> Image.Image:
    """Crop an image using normalized coordinates with deterministic rounding."""

    x, y, width, height = normalize_crop_xywh(crop_xywh_norm)
    left = min(image.width - 1, max(0, math.floor(x * image.width)))
    top = min(image.height - 1, max(0, math.floor(y * image.height)))
    right = min(image.width, max(left + 1, math.ceil((x + width) * image.width)))
    bottom = min(
        image.height,
        max(top + 1, math.ceil((y + height) * image.height)),
    )
    return image.crop((left, top, right, bottom))


def dynamic_cam4_crop_xywh(
    perception_summaries: list[dict[str, Any]],
    *,
    fallback_xywh_norm: tuple[float, float, float, float] | list[float],
    padding_norm: float = 0.08,
    minimum_width_norm: float = 0.45,
    minimum_height_norm: float = 0.55,
) -> tuple[float, float, float, float]:
    """Frame a padded union of observable instances without using annotations."""

    boxes: list[tuple[float, float, float, float]] = []
    for summary in perception_summaries:
        if not isinstance(summary, dict):
            continue
        rows = summary.get("instances", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            bbox = _bounded_number_list(
                row.get("bbox_xywh_norm"),
                length=4,
                minimum=0.0,
                maximum=1.0,
            )
            if not bbox:
                continue
            x, y, width, height = bbox
            if width <= 0.0 or height <= 0.0:
                continue
            boxes.append((x, y, min(1.0, x + width), min(1.0, y + height)))
    if not boxes:
        return normalize_crop_xywh(fallback_xywh_norm)

    padding = min(max(float(padding_norm), 0.0), 0.5)
    left = max(0.0, min(box[0] for box in boxes) - padding)
    top = max(0.0, min(box[1] for box in boxes) - padding)
    right = min(1.0, max(box[2] for box in boxes) + padding)
    bottom = min(1.0, max(box[3] for box in boxes) + padding)
    minimum_width = min(max(float(minimum_width_norm), 0.05), 1.0)
    minimum_height = min(max(float(minimum_height_norm), 0.05), 1.0)

    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    width = max(right - left, minimum_width)
    height = max(bottom - top, minimum_height)
    left = min(max(center_x - width / 2.0, 0.0), 1.0 - width)
    top = min(max(center_y - height / 2.0, 0.0), 1.0 - height)
    return normalize_crop_xywh((left, top, width, height))


def _fit_panel(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    contained = ImageOps.contain(source.convert("RGB"), size, Image.Resampling.LANCZOS)
    panel = Image.new("RGB", size, "black")
    panel.paste(
        contained,
        (
            (size[0] - contained.width) // 2,
            (size[1] - contained.height) // 2,
        ),
    )
    return panel


def compose_flir_cam4_for_model(
    flir_bytes: bytes,
    flir_mime_type: str,
    cam4_bytes: bytes,
    cam4_mime_type: str,
    *,
    cam4_crop_xywh_norm: tuple[float, float, float, float] | list[float],
    max_side_px: int,
) -> tuple[bytes, str]:
    """Build one bounded, labeled image from FLIR and a configured CAM4 crop."""

    del flir_mime_type, cam4_mime_type
    limit = max(320, int(max_side_px or 0))
    with (
        Image.open(BytesIO(flir_bytes)) as flir_source,
        Image.open(BytesIO(cam4_bytes)) as cam4_source,
    ):
        flir = flir_source.convert("RGB")
        cam4_crop = crop_image_normalized(
            cam4_source.convert("RGB"),
            cam4_crop_xywh_norm,
        )
        canvas_width = limit
        label_height = max(24, min(42, canvas_width // 32))
        flir_width = max(1, round(canvas_width * 0.68))
        cam4_width = canvas_width - flir_width
        flir_aspect = flir.height / max(flir.width, 1)
        content_height = max(180, round(flir_width * flir_aspect))
        flir_panel = _fit_panel(flir, (flir_width, content_height))
        cam4_panel = _fit_panel(cam4_crop, (cam4_width, content_height))
        canvas = Image.new(
            "RGB",
            (canvas_width, label_height + content_height),
            "black",
        )
        canvas.paste(flir_panel, (0, label_height))
        canvas.paste(cam4_panel, (flir_width, label_height))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype(
                "DejaVuSans-Bold.ttf",
                max(14, label_height - 12),
            )
        except OSError:
            font = ImageFont.load_default()
        draw.text((10, 5), "FLIR surgical field", fill="white", font=font)
        draw.text(
            (flir_width + 10, 5),
            "CAM4 context crop",
            fill="white",
            font=font,
        )
        output = BytesIO()
        canvas.save(output, format="JPEG", quality=88, optimize=True)
        return bound_image_for_model(
            output.getvalue(),
            "image/jpeg",
            limit,
        )


def crop_cam4_for_model(
    cam4_bytes: bytes,
    *,
    cam4_crop_xywh_norm: tuple[float, float, float, float] | list[float],
    max_side_px: int,
) -> tuple[bytes, str]:
    """Encode the configured CAM4 crop for single-view fallback."""

    with Image.open(BytesIO(cam4_bytes)) as source:
        cropped = crop_image_normalized(
            source.convert("RGB"),
            cam4_crop_xywh_norm,
        )
        # Keep JPEG dimensions aligned for stricter FFmpeg-backed vision
        # decoders while preserving every observed pixel in the crop.
        aligned_width = max(16, math.ceil(cropped.width / 16) * 16)
        aligned_height = max(16, math.ceil(cropped.height / 16) * 16)
        if (aligned_width, aligned_height) != cropped.size:
            cropped = ImageOps.expand(
                cropped,
                border=(
                    0,
                    0,
                    aligned_width - cropped.width,
                    aligned_height - cropped.height,
                ),
                fill="black",
            )
        output = BytesIO()
        cropped.save(output, format="JPEG", quality=88, optimize=True)
    return bound_image_for_model(
        output.getvalue(),
        "image/jpeg",
        max_side_px,
    )


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bounded_number_list(
    value: Any,
    *,
    length: int,
    minimum: float,
    maximum: float,
) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        return []
    numbers = [_finite_float(item) for item in value]
    if any(item is None for item in numbers):
        return []
    return [
        round(min(max(float(item), minimum), maximum), 6)
        for item in numbers
        if item is not None
    ]


def summarize_public_perception_json(
    raw_json: str,
    *,
    kind: str,
    max_instances: int = MAX_PUBLIC_PERCEPTION_INSTANCES,
) -> dict[str, Any]:
    """Bound public detector output and intentionally discard full mask RLE."""

    try:
        payload = json.loads(str(raw_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    frame = payload.get("frame", {})
    if not isinstance(frame, dict):
        frame = {}
    image = payload.get("image", {})
    if not isinstance(image, dict):
        image = {}
    width = max(0, int(_finite_float(image.get("width")) or 0))
    height = max(0, int(_finite_float(image.get("height")) or 0))
    rows = frame.get("instances", [])
    if not isinstance(rows, list):
        rows = []
    bounded_rows: list[dict[str, Any]] = []
    for row in rows[: max(0, int(max_instances))]:
        if not isinstance(row, dict):
            continue
        summary: dict[str, Any] = {}
        class_name = str(row.get("class_name", "")).strip()[:80]
        if class_name:
            summary["class_name"] = class_name
        for key in ("class_id", "track_id", "instance_id"):
            value = _finite_float(row.get(key))
            if value is not None:
                summary[key] = int(value)
        bbox = _bounded_number_list(
            row.get("bbox_xywh_norm"),
            length=4,
            minimum=0.0,
            maximum=1.0,
        )
        if bbox:
            summary["bbox_xywh_norm"] = bbox
        if kind == "segmentation":
            area_px = _finite_float(row.get("mask_area_px"))
            if area_px is not None and area_px >= 0.0:
                summary["mask_area_px"] = int(area_px)
                if width > 0 and height > 0:
                    summary["mask_area_norm"] = round(
                        min(max(area_px / float(width * height), 0.0), 1.0),
                        6,
                    )
            centroid = _bounded_number_list(
                row.get("mask_centroid_norm"),
                length=2,
                minimum=0.0,
                maximum=1.0,
            )
            if centroid:
                summary["mask_centroid_norm"] = centroid
        if summary:
            bounded_rows.append(summary)
    timestamp = _finite_float(payload.get("bag_timestamp_sec"))
    if timestamp is None:
        timestamp = _finite_float(frame.get("source_timestamp_sec"))
    result: dict[str, Any] = {
        "kind": kind,
        "source": "cam4_public_detector",
        "frame_id": str(frame.get("frame_id", "")).strip()[:120],
        "image": {"width": width, "height": height},
        "instances": bounded_rows,
        "truncated": len(rows) > len(bounded_rows),
    }
    if timestamp is not None:
        result["timestamp_sec"] = round(float(timestamp), 6)
    if kind == "bboxes":
        result["bbox_format"] = "xywh_norm"
        result["confidence_available"] = bool(
            payload.get("confidence_available", False)
        )
    else:
        result["segmentation_summary_only"] = True
        result["full_mask_rle_included"] = False
    return result


class RealVLMNode(Node):
    def _causal_now_sec(self) -> float:
        """Return ROS/source time for evidence ordering, wall time in unit stubs."""

        try:
            return float(self.get_clock().now().nanoseconds) / 1_000_000_000.0
        except Exception:
            return time.time()

    def __init__(self) -> None:
        super().__init__("real_vlm_node")
        self._state_callback_group = MutuallyExclusiveCallbackGroup()
        self._visual_callback_group = MutuallyExclusiveCallbackGroup()
        self._inference_callback_group = MutuallyExclusiveCallbackGroup()
        self._inference_backpressure = InferenceBackpressure()
        self._inference_wakeup = threading.Event()
        self._inference_shutdown = threading.Event()
        self._inference_worker: threading.Thread | None = None
        self._inference_failures: deque[InferenceFailure] = deque(
            maxlen=INFERENCE_FAILURE_HISTORY_LENGTH
        )
        self._inference_failure_count = 0
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("base_url", "http://127.0.0.1:8001")
        self.declare_parameter("provider_id", os.environ.get("VLM_PROVIDER_ID", "vllm"))
        self.declare_parameter("api_key", "")
        self.declare_parameter("model_id", "unsloth/gemma-4-E4B-it-NVFP4")
        self.declare_parameter("api_mode", "openai_compat")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("max_output_tokens", 320)
        self.declare_parameter("temperature", 0.0)
        self.declare_parameter("top_p", 1.0)
        self.declare_parameter("generation_seed", 0)
        self.declare_parameter("response_format", "none")
        self.declare_parameter("reasoning_effort", "")
        self.declare_parameter("retry_count", 2)
        self.declare_parameter("publish_period_sec", 2.0)
        self.declare_parameter("response_mode", "live")
        self.declare_parameter("source_time_triggered_live", True)
        self.declare_parameter("model_input_max_source_lag_sec", 0.0)
        self.declare_parameter("replay_response_path", "")
        self.declare_parameter("output_prefix", "/vlm")
        self.declare_parameter("context_prefix", "/context")
        self.declare_parameter("field_image_topic", "/surgery/images/field/compressed")
        self.declare_parameter("raw_field_image_topic", "")
        self.declare_parameter("cam4_image_topic", "")
        self.declare_parameter("tray_image_topic", "/surgery/images/tray/compressed")
        self.declare_parameter("synthetic_image_topic", "/surgery/images/synthetic/compressed")
        self.declare_parameter(
            "composite_image_topic",
            "/surgery/images/vlm/composite/compressed",
        )
        self.declare_parameter("cam4_dynamic_crop", True)
        self.declare_parameter("require_cam4_image", False)
        self.declare_parameter("multiview_max_skew_sec", 0.1)
        self.declare_parameter("cam4_crop_x_norm", DEFAULT_CAM4_CROP_XYWH_NORM[0])
        self.declare_parameter("cam4_crop_y_norm", DEFAULT_CAM4_CROP_XYWH_NORM[1])
        self.declare_parameter("cam4_crop_width_norm", DEFAULT_CAM4_CROP_XYWH_NORM[2])
        self.declare_parameter("cam4_crop_height_norm", DEFAULT_CAM4_CROP_XYWH_NORM[3])
        self.declare_parameter("cam4_crop_padding_norm", 0.08)
        self.declare_parameter("cam4_crop_min_width_norm", 0.45)
        self.declare_parameter("cam4_crop_min_height_norm", 0.55)
        self.declare_parameter("perception_bboxes_topic", "")
        self.declare_parameter("perception_segmentation_topic", "")
        self.declare_parameter("cam4_semantics_topic", "")
        self.declare_parameter("perception_stale_sec", 3.0)
        self.declare_parameter("perception_image_max_skew_sec", 0.2)
        self.declare_parameter(
            "perception_max_instances",
            MAX_PUBLIC_PERCEPTION_INSTANCES,
        )
        self.declare_parameter("image_stale_sec", 5.0)
        self.declare_parameter("image_max_side_px", 1280)
        self.declare_parameter(
            "multiview_image_max_side_px",
            DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX,
        )
        self.declare_parameter("require_field_image", True)
        self.declare_parameter("context_mode", "world")
        self.declare_parameter("open_set_phase_bootstrap_observations", 0)

        self._prompt_builder = PromptBuilder()
        self._active = False
        self._world: WorldState | None = None
        self._simulation: SimulationState | None = None
        self._latest_bt: BTDecision | None = None
        self._recent_events: deque[EventDigest] = deque(maxlen=6)
        self._latest_images: dict[str, ImageSample] = {}
        self._image_buffers: dict[str, deque[ImageSample]] = {}
        self._latest_perception: dict[str, tuple[float, dict[str, Any]]] = {}
        self._perception_buffers: dict[
            str,
            deque[tuple[float, dict[str, Any]]],
        ] = {}
        self._perception_enabled = True
        self._perception_generation = 0
        self._current_visual_input: dict[str, Any] = {}
        self._current_image_input_error = ""
        self._current_perception_reference_stamp_sec: float | None = None
        self._last_good_raw = ""
        self._last_good_payload: dict[str, Any] | None = None
        self._replay_payload: dict[str, Any] | None = None
        self._recent_observed_signals: deque[dict[str, Any]] = deque(maxlen=12)
        self._recent_speech: deque[dict[str, Any]] = deque(maxlen=10)
        self._recent_skill_statuses: deque[dict[str, Any]] = deque(maxlen=8)
        self._latest_bed_robot_arm_group_request: BedRobotArmGroupRequest | None = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._last_simulation_bundle = ""
        self._last_vlm_phase = ""
        self._last_authoritative_phase = ""
        self._phase_bootstrap_id = ""
        self._phase_bootstrap_observation_count = 0
        self._phase_bootstrap_explicit = False
        self._phase_entered_wall_sec = self._causal_now_sec()
        self._last_replay_image_stamp_sec: float | None = None
        self._last_periodic_live_image_stamp_sec: float | None = None
        self._last_submitted_live_image_stamp_sec: float | None = None
        self._last_source_live_trigger_stamp_sec: float | None = None
        self._source_live_trigger_pending = False
        self._source_live_trigger_lock = threading.Lock()
        self._visual_frame_generation = 0
        self._last_submitted_visual_generation = -1
        self._fast_cam4_mayo_published_tools: set[str] = set()
        self._fast_cam4_mayo_last_seen_stamp_sec: dict[str, float] = {}
        self._oracle_scenario = []
        self._oracle_scenario_length = 0
        self._oracle_bootstrap_tick = 0
        self._oracle_tick = 0
        self._developer_instruction = (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"sg\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If gesture is absent, sg must be exactly [\"\",\"\",\"\",0.0]. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

        self._provider_model_selections: dict[str, str] = {}
        initial_base_url = str(self.get_parameter("base_url").value)
        initial_api_key = str(
            self.get_parameter("api_key").value or os.environ.get("VLM_API_KEY", "")
        )
        self._provider_registry = ModelProviderRegistry.from_environment(
            legacy_base_url=initial_base_url,
            legacy_api_key=initial_api_key,
        )
        self._load_parameters()
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._phase_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "phase_summary"), 10)
        self._tool_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "tool_lifecycle_summary"), 10)
        self._event_digest_pub = self.create_publisher(EventDigest, self._topic(self._context_prefix, "event_digest"), 20)
        self._bt_snapshot_pub = self.create_publisher(BTContextSnapshot, self._topic(self._context_prefix, "bt_context_snapshot"), 10)
        self._request_context_pub = self.create_publisher(VLMRequestContext, self._topic(self._context_prefix, "vlm_request_context"), 10)
        self._result_pub = self.create_publisher(VLMResult, self._topic(self._output_prefix, "result"), 10)
        self._model_raw_result_pub = self.create_publisher(
            VLMResult,
            self._topic(self._output_prefix, "model_raw_result"),
            10,
        )
        self._health_pub = self.create_publisher(VLMHealth, self._topic(self._output_prefix, "health"), 10)
        self._phase_pub = self.create_publisher(PhaseEvidence, self._topic(self._output_prefix, "phase_evidence"), 10)
        self._tool_pub = self.create_publisher(ToolObservation, self._topic(self._output_prefix, "tool_observations"), 30)
        self._gesture_pub = self.create_publisher(SurgeonGestureEvidence, self._topic(self._output_prefix, "surgeon_gesture_evidence"), 10)
        self._composite_image_pub = self.create_publisher(
            CompressedImage,
            self._composite_image_topic,
            4,
        )
        self._bed_robot_arm_group_proposal_pub = self.create_publisher(
            BedRobotArmGroupActionProposal,
            self._topic(self._output_prefix, "bed_robot_arm_group_proposal"),
            10,
        )
        self._model_catalog_service = self.create_service(ListModels, "~/list_models", self._on_list_models)
        self._provider_catalog_service = self.create_service(
            ListModelCatalog,
            "~/list_model_catalog",
            self._on_list_model_catalog,
        )
        self._provider_select_service = self.create_service(
            SelectModelProvider,
            "~/select_model_provider",
            self._on_select_model_provider,
        )
        self._provider_control_service = self.create_service(
            ControlModelRuntime,
            "~/control_model_runtime",
            self._on_control_model_runtime,
        )

        state_group = self._state_callback_group
        visual_group = self._visual_callback_group
        self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            TwinEvent,
            "/twin/events",
            self._on_event,
            50,
            callback_group=state_group,
        )
        self.create_subscription(
            SkillStatus,
            "/skill/status",
            self._on_skill_status,
            50,
            callback_group=state_group,
        )
        self.create_subscription(
            String,
            "/surgery/audio/request_text",
            self._on_request_text,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_bed_robot_arm_group_status,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            CompressedImage,
            self._field_image_topic,
            self._make_image_cb("field"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        if self._raw_field_image_topic:
            self.create_subscription(
                CompressedImage,
                self._raw_field_image_topic,
                self._make_image_cb("raw_field"),
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        if self._cam4_image_topic:
            self.create_subscription(
                CompressedImage,
                self._cam4_image_topic,
                self._make_image_cb("cam4"),
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        self.create_subscription(
            CompressedImage,
            self._tray_image_topic,
            self._make_image_cb("tray"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        self.create_subscription(
            CompressedImage,
            self._synthetic_image_topic,
            self._make_image_cb("synthetic"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        if self._perception_bboxes_topic:
            self.create_subscription(
                String,
                self._perception_bboxes_topic,
                self._make_perception_cb("bboxes"),
                20,
                callback_group=visual_group,
            )
        if self._perception_segmentation_topic:
            self.create_subscription(
                String,
                self._perception_segmentation_topic,
                self._make_perception_cb("segmentation"),
                20,
                callback_group=visual_group,
            )
        if self._cam4_semantics_topic:
            self.create_subscription(
                String,
                self._cam4_semantics_topic,
                self._make_perception_cb("cam4_semantics"),
                20,
                callback_group=visual_group,
            )
        self.create_subscription(
            String,
            "/surgery/perception/rfdetr/health",
            self._on_perception_health,
            20,
            callback_group=visual_group,
        )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=state_group,
        )

        self._timer = self.create_timer(
            self._publish_period_sec,
            self._tick,
            callback_group=self._inference_callback_group,
            clock=Clock(clock_type=ClockType.STEADY_TIME),
        )
        self._inference_worker = threading.Thread(
            target=self._inference_worker_loop,
            name="real-vlm-latest-frame",
            daemon=True,
        )
        self._inference_worker.start()

    def _load_parameters(self, overrides: dict[str, Any] | None = None) -> None:
        override_values = overrides or {}

        def param_value(name: str) -> Any:
            return override_values.get(name, self.get_parameter(name).value)

        self._spec_dir = str(param_value("spec_dir"))
        self._spec = load_bundle(self._spec_dir)
        configured_base_url = str(param_value("base_url"))
        configured_api_key = str(
            param_value("api_key") or os.environ.get("VLM_API_KEY", "")
        )
        provider = self._provider_registry.resolve(
            str(param_value("provider_id")),
            fallback_base_url=configured_base_url,
            fallback_api_key=configured_api_key,
        )
        self._provider_id = provider.provider_id
        self._base_url = provider.base_url
        self._api_key = provider.api_key
        self._model_id = str(param_value("model_id"))
        self._provider_model_selections[self._provider_id] = self._model_id
        self._api_mode = str(param_value("api_mode"))
        self._request_timeout_sec = float(param_value("request_timeout_sec"))
        self._max_output_tokens = int(param_value("max_output_tokens"))
        self._temperature = float(param_value("temperature"))
        self._top_p = float(param_value("top_p"))
        generation_seed = int(param_value("generation_seed"))
        self._generation_seed = generation_seed if generation_seed >= 0 else None
        self._response_format = str(param_value("response_format")).strip().lower()
        self._reasoning_effort = str(param_value("reasoning_effort")).strip()
        self._retry_count = int(param_value("retry_count"))
        self._publish_period_sec = float(param_value("publish_period_sec"))
        self._response_mode = str(param_value("response_mode"))
        self._source_time_triggered_live = bool(
            param_value("source_time_triggered_live")
        )
        self._model_input_max_source_lag_sec = max(
            0.0,
            float(param_value("model_input_max_source_lag_sec")),
        )
        self._replay_response_path = str(param_value("replay_response_path"))
        self._output_prefix = str(param_value("output_prefix")).rstrip("/")
        self._context_prefix = str(param_value("context_prefix")).rstrip("/")
        self._field_image_topic = str(param_value("field_image_topic"))
        self._raw_field_image_topic = str(
            param_value("raw_field_image_topic")
        ).strip()
        self._cam4_image_topic = str(param_value("cam4_image_topic")).strip()
        self._tray_image_topic = str(param_value("tray_image_topic"))
        self._synthetic_image_topic = str(param_value("synthetic_image_topic"))
        self._composite_image_topic = str(param_value("composite_image_topic"))
        self._cam4_dynamic_crop = bool(param_value("cam4_dynamic_crop"))
        self._require_cam4_image = bool(param_value("require_cam4_image"))
        self._multiview_max_skew_sec = max(
            0.0,
            float(param_value("multiview_max_skew_sec")),
        )
        self._cam4_crop_xywh_norm = normalize_crop_xywh(
            (
                float(param_value("cam4_crop_x_norm")),
                float(param_value("cam4_crop_y_norm")),
                float(param_value("cam4_crop_width_norm")),
                float(param_value("cam4_crop_height_norm")),
            )
        )
        self._cam4_crop_padding_norm = float(
            param_value("cam4_crop_padding_norm")
        )
        self._cam4_crop_min_width_norm = float(
            param_value("cam4_crop_min_width_norm")
        )
        self._cam4_crop_min_height_norm = float(
            param_value("cam4_crop_min_height_norm")
        )
        self._perception_bboxes_topic = str(
            param_value("perception_bboxes_topic")
        ).strip()
        self._perception_segmentation_topic = str(
            param_value("perception_segmentation_topic")
        ).strip()
        self._cam4_semantics_topic = str(
            param_value("cam4_semantics_topic")
        ).strip()
        self._perception_stale_sec = max(
            0.0,
            float(param_value("perception_stale_sec")),
        )
        self._perception_image_max_skew_sec = max(
            0.0,
            float(param_value("perception_image_max_skew_sec")),
        )
        self._perception_max_instances = max(
            0,
            int(param_value("perception_max_instances")),
        )
        self._image_stale_sec = float(param_value("image_stale_sec"))
        self._image_max_side_px = max(
            0,
            int(param_value("image_max_side_px")),
        )
        self._multiview_image_max_side_px = max(
            320,
            int(param_value("multiview_image_max_side_px")),
        )
        self._require_field_image = bool(param_value("require_field_image"))
        self._context_mode = str(param_value("context_mode")).strip().lower()
        self._open_set_phase_bootstrap_observations = max(
            0,
            int(param_value("open_set_phase_bootstrap_observations")),
        )
        if self._context_mode != "actor_log" and self._response_mode == "live":
            self.get_logger().warning(
                "live VLM context is restricted to public evidence; "
                f"ignoring context_mode={self._context_mode!r}"
            )
            self._context_mode = "actor_log"
        if self._context_mode == "actor_log":
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            self._system_prompt = self._actor_log_system_prompt()
            self._developer_instruction = self._actor_log_developer_instruction()
            self._json_schema = compact_vlm_json_schema("4")
        else:
            self._context_mode = "world"
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            self._system_prompt = self._prompt_builder.build(self._spec_dir)
            self._developer_instruction = self._world_developer_instruction()
            self._json_schema = compact_vlm_json_schema("1")
        self._oracle_scenario = list(self._spec.get_mock_perception_stages())
        self._oracle_scenario_length = sum(stage.duration_ticks for stage in self._oracle_scenario)
        self._oracle_bootstrap_tick = int(self._spec.get_mock_perception_bootstrap_tick())
        self._client = LMStudioClient(
            base_url=self._base_url,
            timeout_sec=self._request_timeout_sec,
            api_key=self._api_key,
            provider_id=self._provider_id,
        )
        self._replay_payload = self._load_replay_payload(self._replay_response_path)

    def _load_replay_payload(self, replay_path: str) -> dict[str, Any] | None:
        if not replay_path.strip():
            return None
        path = Path(replay_path)
        if not path.is_file():
            self.get_logger().warning(f"Replay response path does not exist: {replay_path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.get_logger().warning(f"Failed to read replay response payload: {exc}")
            return None

    def _topic(self, prefix: str, suffix: str) -> str:
        return f"{prefix}/{suffix}".replace("//", "/")

    def _world_developer_instruction(self) -> str:
        return (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"sg\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If gesture is absent, sg must be exactly [\"\",\"\",\"\",0.0]. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

    def _actor_log_system_prompt(self) -> str:
        phases = [
            {
                "id": phase.id,
                "next": list(phase.possible_next),
                "tools": list(phase.expected_instruments),
            }
            for phase in self._spec.bundle.phases
        ]
        tools = [
            {
                "id": instrument.id,
                "name": instrument.display_name,
                "role": instrument.role,
                "requestable": bool(getattr(instrument, "requestable", True)),
            }
            for instrument in self._spec.bundle.instruments
        ]
        return (
            "You are the visual evidence interpreter for a surgical task planner. "
            "Inputs are a FLIR surgical-field view plus a synchronized overhead CAM4 crop for surgeon-hand and Mayo observation. Object recognition may be absent. "
            "Evidence order is mandatory: inspect CAM4 pixels first, freeze gesture and Mayo observations, then use public speech, signals, skill/twin events, FLIR, and procedure candidates to infer phase and next tool. observable_perception is optional advice, never a prerequisite or ground truth. "
            "For CAM4, inspect the center-right interior request zone and then the whole crop. Judge each hand independently and inventory every distinct Mayo silhouette. Visible geometry can outweigh missing detector rows; a search zone alone is not evidence. "
            "Use only externally observable evidence. Never assume hidden surgeon-actor phase, plans, timers, or private logs. Previous predictions are weak temporal context, not facts. "
            "Outputs are observations with confidence only. They never authorize handover, recovery, cleanup, ownership, lifecycle, or robot action; the digital twin validates facts and the Behavior Tree applies policy. "
            "Separate a currently used/held tool from the next tool likely to be requested. An open-palm request may be tool-unknown. "
            "For phase, open_set means compare all phases because replay may start mid-procedure; temporal_prior means current phase plus allowed next phases. Prefer visible anatomy and persistent operative activity. Procedure order is a prior, neither required nor sufficient. If evidence is weak, retain low-confidence candidates instead of inventing unseen progress. "
            "Field-event text is an intermittent event overlay, not the phase. Shared tool use alone does not prove a phase; honor each doctor-prompt cue and exclusion. "
            "For next tool, use context.candidates as a weak prior and reorder only with public evidence. "
            "Mayo recover/reuse is advisory: reuse means visible/public evidence supports near-term reuse; otherwise recover remains low confidence. Ambiguity favors low-confidence reuse. mayo_retrieve names at most the strongest advisory recover candidate. "
            "The doctor prompt may use Pxx/Txx ids. Apply runtime_phase and tools[*].rt crosswalks and emit only runtime ids; never emit an empty-runtime-id tool. "
            "For a pending logical retraction-group request, emit a direction only with supporting speech or visual evidence. Never infer physical arm identity/count, and never emit suction. Otherwise bed_robot_arm_group is null. "
            "Return exactly one schema-v4 JSON object and no other text. "
            f"Runtime ontology: {json.dumps({'phases': phases, 'tools': tools}, separators=(',', ':'))}. "
            f"Doctor procedure prompt: {json.dumps(self._procedure_prompt, separators=(',', ':'))}"
        )

    def _actor_log_developer_instruction(self) -> str:
        return (
            "Return schema v4 only: "
            "{\"v\":\"4\",\"gesture\":[event_type,tool_id,hand_pose,confidence],"
            "\"mayo\":[[tool_id,\"recover|reuse\",confidence]],\"mayo_retrieve\":[tool_id,confidence],"
            "\"phase\":[[phase_id,confidence]],\"tool\":[[tool_id,confidence]],"
            "\"intent\":[intent_type,tool_id,confidence],\"u\":uncertainty,\"sum\":\"brief clinical analysis\","
            "\"bed_robot_arm_group\":null_or_retraction_object}. "
            "Use exact runtime ids. Confidence and u are numeric 0.0-1.0. "
            "Populate gesture and mayo from CAM4 pixels first; then infer phase, next tool, and intent. Priors must not overwrite a directly visible CAM4 observation. "
            "Mandatory CAM4 visual pass, independent of observable_perception: "
            "(1) Gesture. Use the supplied CAM4 crop as-is. In this fixed view the request area is the covered-patient region to the right of the blue Mayo work surface. Inspect that center-right interior zone, then the full crop. The zone is a search prior only; visually compare all visible hands and inspect each independently. One operating hand never cancels another open hand. "
            "Trace each candidate wrist to its source. A skin-toned hand continuing through an elastic cuff into a blue or green sterile gown sleeve is a staff glove; an extreme-edge hand connected to an exposed forearm or patient wristband is a patient hand. Ignore bare patient hands at table edges and bottom-border-clipped scrub hands. "
            "The common positive appearance is that skin-toned staff glove lying open on the covered patient or sterile drape inside the center-right interior request zone. Positive geometry is a broad complete palmar surface and several relaxed uncurled fingers, empty and not gripping. Confirm the receiving surface from palm/thenar-pad and finger-pad geometry; visible knuckles or a dorsal-only hand are insufficient. Fingers may point in any direction; do not require spread fingers, motion, speech, transfer, or known wearer. Medical gloves may be beige, pink, white, or blue. "
            "Mere contact with the body is not stabilization; require visible pressure, bracing, tissue traction, or active manipulation. Use 0.80-1.00 for a clear complete palm. Use 0.45-0.79 only when the complete empty palm is clear but wearer role, wrist orientation, or cuff visibility remains uncertain. If the palm, several fingers, or open-versus-gripping state is not directly visible, emit no gesture. "
            "gesture is one flat four-item array: strongest positive is [\"request_tool\",tool_id_or_empty,\"open_receive\",confidence], otherwise [\"\",\"\",\"\",0.0]. "
            "Tool identity is a separate question. With no public named request, use empty tool_id and intent must be exactly [\"none\",\"\",0.0]; request_tool is a gesture event, not an intent value. An open palm alone never names a tool; a nearby or Mayo tool is not a directed cue. Never copy a tool from Mayo, held/active tools, procedure candidates, or next-tool prediction into gesture or intent. "
            "Hard negatives: bare patient hand; a palm or multiple fingers clipped at the bottom or side frame edge; fingertips or knuckles without a complete visible palm; dorsal-only hand; gripping; active manipulation; blur; severe occlusion. Mayo placement/pickup requires visible hand contact or grip on a tool, so require visible hand contact or grip rather than proximity alone. A nearby tool or cable is not placement/pickup by itself and must not cancel an otherwise qualifying request palm. Detector absence must not erase clear pixel evidence. "
            "(2) Mayo. First sweep the fixed stand left-to-right and top-to-bottom, including edges and occlusion. Split touching items by handles, rings, shafts, tips, and cables; emit one mayo row per visible instance, preserving duplicates. "
            "Identify by morphology before procedure likelihood: tweezer-shaped thumb forceps have no finger rings, so a ring-handled tool can never be Adson; clamps and scissors have paired rings and a hinge. A small slim ring-handled clamp with long narrow straight jaws is a Mosquito hemostat, not Allis. Allis is bulkier and requires directly visible short broad toothed grasping jaws; ring handles alone are insufficient for Allis. If broad teeth are not resolved, prefer Mosquito at lower confidence over inventing Allis. Retractors have broad or hooked blades; energy instruments often have insulation or a cable; suction instruments have a hollow tube. "
            "Complete and freeze this geometry-based inventory before phase/next-tool priors. Confidence: 0.80-1.00 clear shape; 0.45-0.79 partial but distinctive. Match only runtime ontology and omit unidentifiable silhouettes. Scan even when object recognition is disabled or empty. "
            "Every mayo row requires a distinct tool silhouette directly visible in CAM4. Never copy a tool from FLIR, procedure order, candidates, memory, or a field-manipulated instrument. Mayo rows are perceptual evidence only, never a robot command or lifecycle transition. "
            "Exclude tools publicly located in robot hands, cleaner, rack/tray, prepositioning, recovery, or cleaning. Label visible tools reuse only with public near-term reuse evidence; otherwise recover at low confidence. mayo_retrieve is the strongest advisory recover candidate or [\"\",0.0]. "
            "(3) Phase/tool/intent. Emit 1-4 phase and 1-4 next-request tool rows. Visibility alone does not make a held, Mayo, or cleaning tool the next tool. For open_set compare all phases; for temporal_prior use current_phase and allowed_next. Procedure-start speech does not prove phase one. A tool-unknown gesture is valid, but handover intent requires a direct public cue naming the tool. "
            "(4) Bed group. Without a supported pending retraction request and direction evidence, bed_robot_arm_group is null. Otherwise copy request_id/end_effector_profile, set group_id=operation=retraction, and never name a physical arm. "
            "Direction is one of UP, DOWN, LEFT, RIGHT, LEFT_RIGHT, UP_DOWN. Korean aliases include 위/위쪽/상방, 아래/아래쪽/하방, 왼쪽/좌측, 오른쪽/우측, 좌우/양쪽, 상하/위와 아래. "
            "Distance: explicit mm/cm wins (cm x10); unitless number means mm; 미세하게=1, 살짝=5, 조금=10, 많이=20, 최대한=30, absent=10 mm. Preserve explicit value, raw phrase, and distance_origin. "
            "Final CAM4 audit immediately before JSON: inspect only CAM4 for gesture and Mayo. The expected request appearance is a complete empty staff palm over the covered-patient area to the right of the blue Mayo surface. Mandatory positive rule: when its palmar surface and several uncurled fingers are visible, gesture MUST be request_tool; never reject it because it is stationary, touching the body, or another hand is operating. Being near a tool is also not disqualifying. One positive candidate overrides unrelated negative hands. No speech or transfer is required. With no named request, tool_id stays empty and intent none. Emit Mayo rows only for separate CAM4-visible silhouettes, never FLIR or priors. "
            "sum is one or two concise factual clinical sentences describing visible field, likely phase, active manipulation, and change; no internal logic, invented findings, markdown, or extra text."
        )

    def _on_parameters_changed(self, params):
        reload_required = False
        overrides = {parameter.name: parameter.value for parameter in params}
        spec_changed = "spec_dir" in overrides
        for parameter in params:
            if parameter.name in {
                "spec_dir",
                "base_url",
                "provider_id",
                "api_key",
                "model_id",
                "api_mode",
                "request_timeout_sec",
                "max_output_tokens",
                "temperature",
                "top_p",
                "generation_seed",
                "response_format",
                "reasoning_effort",
                "retry_count",
                "publish_period_sec",
                "response_mode",
                "source_time_triggered_live",
                "model_input_max_source_lag_sec",
                "replay_response_path",
                "cam4_dynamic_crop",
                "require_cam4_image",
                "multiview_max_skew_sec",
                "cam4_crop_x_norm",
                "cam4_crop_y_norm",
                "cam4_crop_width_norm",
                "cam4_crop_height_norm",
                "cam4_crop_padding_norm",
                "cam4_crop_min_width_norm",
                "cam4_crop_min_height_norm",
                "perception_stale_sec",
                "perception_image_max_skew_sec",
                "perception_max_instances",
                "image_stale_sec",
                "image_max_side_px",
                "multiview_image_max_side_px",
                "require_field_image",
                "context_mode",
                "open_set_phase_bootstrap_observations",
            }:
                reload_required = True
        if reload_required:
            try:
                self._load_parameters(overrides)
                if spec_changed:
                    self._recent_events.clear()
                    self._last_good_raw = ""
                    self._last_good_payload = None
                    self._recent_observed_signals.clear()
                    self._recent_speech.clear()
                    self._recent_skill_statuses.clear()
                    self._latest_bed_robot_arm_group_request = None
                    self._last_bed_robot_arm_group_proposal_request_id = ""
                    self._last_vlm_phase = ""
                    self._last_authoritative_phase = ""
                    self._phase_bootstrap_id = ""
                    self._phase_bootstrap_observation_count = 0
                    self._phase_bootstrap_explicit = False
                    self._phase_entered_wall_sec = self._causal_now_sec()
                    self._oracle_tick = 0
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        return SetParametersResult(successful=True)

    def _on_list_models(self, _request, response):
        try:
            model_ids = self._client.list_models()
            if self._model_id and self._model_id not in model_ids:
                model_ids.insert(0, self._model_id)
            response.model_ids = model_ids
            response.success = True
            response.message = f"{len(response.model_ids)} model(s) from {self._base_url}"
        except Exception as exc:
            response.success = False
            response.model_ids = []
            response.message = f"Model catalog unavailable at {self._base_url}: {exc}"
        return response

    def _on_list_model_catalog(self, _request, response):
        probes = self._provider_registry.probe_all()
        online_count = sum(1 for probe in probes if probe.reachable)
        model_count = 0
        response.active_provider_id = self._provider_id
        response.active_model_id = self._model_id
        response.providers = []
        response.models = []

        for probe in probes:
            provider_status = ModelProviderStatus()
            provider_status.provider_id = probe.provider.provider_id
            provider_status.provider_name = probe.provider.display_name
            provider_status.endpoint = probe.provider.base_url
            provider_status.reachable = probe.reachable
            provider_status.status = probe.status
            provider_status.detail = probe.detail
            provider_status.latency_sec = float(probe.latency_sec)
            provider_status.model_count = len(probe.models)
            response.providers.append(provider_status)

            for model in probe.models:
                entry = ModelCatalogEntry()
                entry.provider_id = model.provider_id
                entry.provider_name = model.provider_name
                entry.model_id = model.model_id
                entry.display_name = model.display_name
                entry.capability = model.capability
                entry.load_state = model.load_state
                entry.selectable = model.selectable
                entry.detail = model.detail
                entry.runtime_managed = model.runtime_managed
                entry.available_actions = list(model.available_actions)
                response.models.append(entry)
                model_count += 1

        advertised = {
            (str(entry.provider_id), str(entry.model_id))
            for entry in response.models
        }
        reachable_by_provider = {
            probe.provider.provider_id: probe.reachable for probe in probes
        }
        for remembered_provider_id, remembered_model_id in self._provider_model_selections.items():
            if not remembered_model_id or (
                remembered_provider_id,
                remembered_model_id,
            ) in advertised:
                continue
            provider = self._provider_registry.get_provider(remembered_provider_id)
            configured = ModelCatalogEntry()
            configured.provider_id = remembered_provider_id
            configured.provider_name = (
                provider.display_name
                if provider is not None
                else remembered_provider_id
            )
            configured.model_id = remembered_model_id
            configured.display_name = remembered_model_id
            configured.capability = "unknown"
            matching_entry = next(
                (
                    entry
                    for entry in response.models
                    if str(entry.provider_id) == remembered_provider_id
                    and self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        str(entry.model_id),
                    )
                    == self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        remembered_model_id,
                    )
                ),
                None,
            )
            override = self._provider_registry.runtime_state(
                remembered_provider_id,
                remembered_model_id,
            )
            if override is not None:
                configured.load_state, configured.detail = override
            elif matching_entry is not None:
                configured.load_state = str(matching_entry.load_state)
                configured.detail = str(matching_entry.detail)
            else:
                configured.load_state = "configured"
                configured.detail = "Configured model was not returned by /v1/models"
            configured.selectable = reachable_by_provider.get(
                remembered_provider_id,
                False,
            )
            configured.runtime_managed = bool(
                provider is not None and provider.runtime_commands
            )
            configured.available_actions = list(
                self._provider_registry.available_actions(
                    remembered_provider_id,
                    configured.load_state,
                )
            )
            response.models.insert(0, configured)
            model_count += 1

        response.success = online_count > 0 or model_count > 0
        response.message = (
            f"{online_count}/{len(probes)} providers online; {model_count} model(s)"
        )
        return response

    def _on_select_model_provider(self, request, response):
        provider_id = str(request.provider_id).strip().lower()
        model_id = str(request.model_id).strip()
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        if not provider_id or not model_id:
            response.success = False
            response.message = "provider_id and model_id are required"
            return response
        provider = self._provider_registry.get_provider(provider_id)
        if provider is None:
            response.success = False
            response.message = f"Unknown model provider: {provider_id}"
            return response
        probe = self._provider_registry.probe(provider_id)
        available_models = {model.model_id: model for model in probe.models}
        remembered_model = self._provider_model_selections.get(provider_id, "")
        catalog_model = available_models.get(model_id)
        if catalog_model is None:
            catalog_model = self._provider_registry.matching_model(
                provider_id,
                model_id,
                probe.models,
            )
        if catalog_model is None and model_id != remembered_model:
            response.success = False
            response.message = (
                f"{model_id} is not advertised by {provider.display_name}"
            )
            return response
        runtime_note = ""
        can_start_offline = bool(
            catalog_model is not None
            and catalog_model.installed
            and catalog_model.available
            and catalog_model.runtime_managed
        )
        if not probe.reachable and not can_start_offline:
            response.success = False
            response.message = (
                f"{provider.display_name} is unavailable: {probe.status} "
                f"({probe.detail})"
            )
            return response
        if (
            provider.managed
            and catalog_model is not None
            and catalog_model.runtime_managed
        ):
            runtime = self._provider_registry.ensure_runtime_ready(
                provider_id,
                catalog_model,
                requested_model_id=model_id,
            )
            if not runtime.success:
                response.success = False
                response.message = runtime.message
                return response
            runtime_note = f"; runtime {runtime.state}"

        result = self.set_parameters_atomically(
            [
                Parameter("provider_id", value=provider_id),
                Parameter("model_id", value=model_id),
            ]
        )
        if not result.successful:
            response.success = False
            response.message = result.reason or "Provider selection was rejected"
            return response
        response.success = True
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        response.message = (
            f"VLM provider set to {provider.display_name}; model set to "
            f"{model_id}{runtime_note}"
        )
        return response

    def _on_control_model_runtime(self, request, response):
        result = self._provider_registry.control_runtime(
            str(request.provider_id),
            str(request.model_id),
            str(request.command),
        )
        response.success = result.success
        response.provider_id = result.provider_id
        response.model_id = result.model_id
        response.state = result.state
        response.message = result.message
        return response

    def _make_image_cb(self, label: str):
        def _cb(msg: CompressedImage) -> None:
            image_format = str(msg.format or "").lower()
            mime_type = (
                "image/jpeg"
                if "jpeg" in image_format or "jpg" in image_format
                else "image/png"
            )
            sample = ImageSample(
                received_monotonic=self._causal_now_sec(),
                stamp_sec=int(msg.header.stamp.sec),
                stamp_nanosec=int(msg.header.stamp.nanosec),
                frame_id=str(msg.header.frame_id),
                data=bytes(msg.data),
                mime_type=mime_type,
            )
            self._latest_images[label] = sample
            self._image_buffers.setdefault(
                label,
                deque(maxlen=IMAGE_PAIR_BUFFER_LENGTH),
            ).append(sample)
            if label in {"field", "raw_field", "cam4"}:
                self._visual_frame_generation += 1
                if self._response_mode == "replay":
                    self._maybe_trigger_replay_image_tick()
                else:
                    self._queue_source_time_live_frame(sample)

        return _cb

    def _queue_source_time_live_frame(self, sample: ImageSample) -> None:
        if (
            not getattr(self, "_source_time_triggered_live", False)
            or self._response_mode != "live"
            or not self._active
        ):
            return
        stamp_sec = image_sample_stamp_sec(sample)
        with self._source_live_trigger_lock:
            self._last_source_live_trigger_stamp_sec = stamp_sec
            self._source_live_trigger_pending = True
        self._queue_inference(
            INFERENCE_TRIGGER_SOURCE_FRAME
        )

    def _drain_source_time_live_frame(self) -> None:
        """Wake the latest-frame consumer; retained for internal compatibility."""

        if not self._active:
            return
        with self._source_live_trigger_lock:
            self._source_live_trigger_pending = False
        wakeup = getattr(self, "_inference_wakeup", None)
        if wakeup is not None:
            wakeup.set()
            return
        current_trigger = self._inference_backpressure.begin()
        if current_trigger is not None:  # pragma: no cover - unit stub fallback
            self._run_inference_chain(current_trigger)

    def _queue_inference(self, trigger: str) -> InferenceAdmission:
        admission = self._inference_backpressure.queue(trigger)
        wakeup = getattr(self, "_inference_wakeup", None)
        if wakeup is not None:
            wakeup.set()
        return admission

    def _inference_worker_loop(self) -> None:
        while not self._inference_shutdown.is_set():
            self._inference_wakeup.wait()
            self._inference_wakeup.clear()
            if self._inference_shutdown.is_set():
                break
            current_trigger = self._inference_backpressure.begin()
            if current_trigger is None:
                continue
            self._run_inference_chain(current_trigger)

    def destroy_node(self):
        shutdown = getattr(self, "_inference_shutdown", None)
        wakeup = getattr(self, "_inference_wakeup", None)
        worker = getattr(self, "_inference_worker", None)
        if shutdown is not None:
            shutdown.set()
        if wakeup is not None:
            wakeup.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        return super().destroy_node()

    def _reset_source_time_live_trigger(self, *, reset_stamp: bool) -> None:
        lock = getattr(self, "_source_live_trigger_lock", None)
        if lock is None:
            self._source_live_trigger_pending = False
            if reset_stamp:
                self._last_source_live_trigger_stamp_sec = None
            return
        with lock:
            self._source_live_trigger_pending = False
            if reset_stamp:
                self._last_source_live_trigger_stamp_sec = None

    def _reset_fast_cam4_mayo_observations(self) -> None:
        getattr(
            self,
            "_fast_cam4_mayo_published_tools",
            set(),
        ).clear()
        getattr(
            self,
            "_fast_cam4_mayo_last_seen_stamp_sec",
            {},
        ).clear()

    def _maybe_trigger_replay_image_tick(self) -> None:
        if self._response_mode != "replay" or not self._active:
            return
        now = self._causal_now_sec()
        field = self._fresh_image("field", now)
        if field is None:
            return
        stamp_sec = image_sample_stamp_sec(field)
        self._last_replay_image_stamp_sec = stamp_sec
        self._queue_inference(
            INFERENCE_TRIGGER_REPLAY_FRAME
        )

    def _make_perception_cb(self, kind: str):
        def _cb(msg: String) -> None:
            if kind == "cam4_semantics":
                summary = parse_cam4_semantics_json(msg.data)
            else:
                summary = summarize_public_perception_json(
                    msg.data,
                    kind=kind,
                    max_instances=self._perception_max_instances,
                )
            if summary:
                sample = (
                    self._causal_now_sec(),
                    summary,
                )
                self._latest_perception[kind] = sample
                self._perception_buffers.setdefault(
                    kind,
                    deque(maxlen=PERCEPTION_PAIR_BUFFER_LENGTH),
                ).append(sample)

        return _cb

    def _publish_fast_cam4_mayo_observations(
        self,
        summary: dict[str, Any],
    ) -> None:
        """Publish stable fixed-camera Mayo presence without waiting for VLM."""

        if (
            not self._active
            or not getattr(self, "_perception_enabled", True)
            or summary.get("schema") != "taskplanner.cam4_semantics.v1"
            or summary.get("source") != "cam4_rfdetr_small"
        ):
            return
        source_stamp_sec = _finite_float(summary.get("source_stamp_sec"))
        if source_stamp_sec is None:
            return

        published = getattr(
            self,
            "_fast_cam4_mayo_published_tools",
            set(),
        )
        last_seen = getattr(
            self,
            "_fast_cam4_mayo_last_seen_stamp_sec",
            {},
        )
        self._fast_cam4_mayo_published_tools = published
        self._fast_cam4_mayo_last_seen_stamp_sec = last_seen

        for row in summary.get("tools", []):
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            if tool_id:
                last_seen[tool_id] = float(source_stamp_sec)
        for tool_id in tuple(published):
            if (
                float(source_stamp_sec) - last_seen.get(tool_id, -math.inf)
                > CAM4_MAYO_STABILITY_WINDOW_SEC
            ):
                published.discard(tool_id)

        request = summary.get("tool_request", {})
        if (
            isinstance(request, dict)
            and str(request.get("state", "")) == "request"
        ):
            return

        enriched = self._enrich_cam4_tool_stability(
            summary,
            reference_stamp_sec=float(source_stamp_sec),
        )
        publisher = getattr(self, "_tool_pub", None)
        if publisher is None:
            return
        for row in enriched.get("tools", []):
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            confidence = _finite_float(row.get("max_confidence"))
            if (
                not tool_id
                or tool_id in published
                or confidence is None
                or confidence < CAM4_MAYO_MIN_CONFIDENCE
                or int(row.get("stable_sample_count", 0) or 0)
                < CAM4_MAYO_MIN_STABLE_SAMPLES
                or float(row.get("stable_duration_sec", 0.0) or 0.0)
                < CAM4_MAYO_MIN_STABLE_DURATION_SEC
            ):
                continue
            observation = ToolObservation()
            stamp_sec = int(math.floor(source_stamp_sec))
            stamp_nanosec = int(
                round((float(source_stamp_sec) - stamp_sec) * 1_000_000_000)
            )
            if stamp_nanosec >= 1_000_000_000:
                stamp_sec += 1
                stamp_nanosec -= 1_000_000_000
            observation.stamp.sec = stamp_sec
            observation.stamp.nanosec = stamp_nanosec
            observation.instrument_id = tool_id
            observation.location_id = "mayo_stand"
            observation.location_type = "mayo_stand"
            observation.confidence = float(confidence)
            observation.visible = True
            publisher.publish(observation)
            published.add(tool_id)

    def _on_perception_health(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "taskplanner.rfdetr_health.v1"
        ):
            return
        enabled = bool(payload.get("enabled"))
        if enabled == self._perception_enabled:
            return
        self._perception_enabled = enabled
        self._perception_generation += 1
        if enabled:
            return
        self._latest_images.pop("field", None)
        self._image_buffers.pop("field", None)
        self._latest_perception.clear()
        getattr(self, "_perception_buffers", {}).clear()
        self._current_visual_input = {}
        self._current_perception_reference_stamp_sec = None
        self._last_good_raw = ""
        self._last_good_payload = None
        self._last_periodic_live_image_stamp_sec = None
        self._last_submitted_live_image_stamp_sec = None
        self._reset_source_time_live_trigger(reset_stamp=True)
        self._reset_fast_cam4_mayo_observations()
        self._publish_health(
            image_source="flir_raw_fallback",
            latency_sec=0.0,
            prompt_chars=0,
            output_chars=0,
            parse_retry_count=0,
            last_error="",
            mode="raw_visual_fallback_pending",
            healthy=True,
            connected=True,
        )

    def _oracle_stage_for_tick(self, tick: int):
        if not self._oracle_scenario:
            return None
        if self._oracle_scenario_length <= 0:
            return self._oracle_scenario[0]
        cycle_tick = tick % self._oracle_scenario_length
        for stage in self._oracle_scenario:
            if cycle_tick < stage.duration_ticks:
                return stage
            cycle_tick -= stage.duration_ticks
        return self._oracle_scenario[-1]

    def _on_world(self, msg: WorldState) -> None:
        self._world = msg
        self._track_authoritative_phase()
        self._publish_context_summaries()

    def _on_simulation(self, msg: SimulationState) -> None:
        active_bundle = str(getattr(msg, "active_bundle", "") or "")
        if active_bundle and active_bundle != self._last_simulation_bundle:
            self._last_simulation_bundle = active_bundle
            self._reset_public_evidence()
        self._simulation = msg
        self._track_authoritative_phase()
        self._publish_context_summaries()

    def _on_event(self, msg: TwinEvent) -> None:
        digest = EventDigest()
        digest.stamp = msg.stamp
        digest.event_type = msg.event_type
        digest.instrument_id = msg.instrument_id
        digest.anchor_id = msg.target_location_id or msg.location_id or msg.target_location_type
        detail = {}
        if msg.detail_json:
            try:
                detail = json.loads(msg.detail_json)
            except json.JSONDecodeError:
                detail = {"detail_json": msg.detail_json}
        digest.reason = (
            str(
                detail.get("note")
                or detail.get("voice_text")
                or msg.mode
                or msg.status
                or msg.event_type
            )
        )
        digest.detail = compact_json(
            {
                "tool": msg.instrument_id,
                "loc": msg.location_id,
                "target": msg.target_location_id,
                "arm": msg.arm,
                "mode": msg.mode,
            }
        )
        self._recent_events.append(digest)
        self._ingest_public_twin_event(msg, detail)
        self._event_digest_pub.publish(digest)
        self._publish_context_summaries()

    def _on_bt_decision(self, msg: BTDecision) -> None:
        self._latest_bt = msg
        self._bt_snapshot_pub.publish(self._bt_snapshot_msg())
        self._publish_context_summaries()

    def _append_public_speech(self, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        now = round(self._causal_now_sec(), 2)
        if self._recent_speech:
            last = self._recent_speech[-1]
            try:
                last_age = now - float(last.get("at", 0.0))
            except (TypeError, ValueError):
                last_age = 999.0
            if str(last.get("text", "")) == clean[:240] and last_age <= 1.0:
                return False
        self._recent_speech.append({"text": clean[:240], "at": now})
        return True

    def _append_public_signal(
        self,
        *,
        signal_type: str,
        tool_id: str = "",
        hand_pose: str = "",
        speech_text: str = "",
    ) -> None:
        if signal_type in {"advance_phase", "advance_phase_cue"}:
            return
        signal: dict[str, Any] = {}
        if signal_type in {
            "request_tool",
            "voice_request",
            "place_on_mayo",
            "continue_using",
            "small_talk",
            "request_procedure_completion",
            "complete_procedure",
        }:
            signal["type"] = signal_type
        if hand_pose:
            signal["hand"] = hand_pose
        if tool_id:
            signal["tool"] = tool_id
        if signal:
            signal["at"] = round(self._causal_now_sec(), 2)
            self._recent_observed_signals.append(signal)

    def _ingest_public_twin_event(self, msg: TwinEvent, detail: dict[str, Any]) -> None:
        event_type = str(msg.event_type or "")
        tool_id = str(msg.instrument_id or detail.get("requested_tool") or detail.get("tool") or "")
        voice_text = str(detail.get("voice_text") or detail.get("text") or "")
        if event_type == "VoiceTranscriptObserved":
            if self._append_public_speech(voice_text):
                self._trigger_inference_for_public_speech()

    def _on_request_text(self, msg: String) -> None:
        if self._append_public_speech(msg.data):
            self._trigger_inference_for_public_speech()

    def _trigger_inference_for_public_speech(self) -> None:
        if not self._active or self._response_mode != "live":
            return
        # Speech remains in the public evidence window. Queue it for the same
        # single consumer as visual frames so callbacks never block on model
        # latency and the latest evidence is evaluated after the active call.
        self._queue_inference(INFERENCE_TRIGGER_SPEECH)

    def _on_bed_robot_arm_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        """Track only VLM-routed logical retraction requests.

        Suction start/stop is intentionally absent here: the BT group lane
        routes those operations deterministically without asking the VLM.
        """
        self._append_public_speech(msg.voice_text)
        if str(msg.group_id) != "retraction" or str(msg.operation) != "retraction":
            return
        request_id = str(msg.request_id).strip()
        if not request_id:
            self.get_logger().warning("Ignoring retraction request without request_id")
            return
        if (
            self._latest_bed_robot_arm_group_request is not None
            and self._latest_bed_robot_arm_group_request.request_id == request_id
        ):
            return
        if self._latest_bed_robot_arm_group_request is not None:
            self.get_logger().warning(
                "Ignoring retraction request %s while request %s is still pending"
                % (
                    request_id,
                    self._latest_bed_robot_arm_group_request.request_id,
                )
            )
            return
        self._latest_bed_robot_arm_group_request = msg
        self._last_bed_robot_arm_group_proposal_request_id = ""

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        pending = self._latest_bed_robot_arm_group_request
        if pending is None or str(msg.request_id) != str(pending.request_id):
            return
        if bool(msg.terminal):
            self._latest_bed_robot_arm_group_request = None

    def _bed_robot_arm_group_state_rows(self) -> list[dict[str, Any]]:
        if self._world is None:
            return []
        rows: list[dict[str, Any]] = []
        for state in getattr(self._world, "bed_robot_arm_groups", []):
            group_id = str(getattr(state, "group_id", ""))
            if group_id not in {"suction", "retraction"}:
                continue
            rows.append(
                {
                    "group_id": group_id,
                    "connected": bool(getattr(state, "connected", False)),
                    "state": str(getattr(state, "state", "")),
                    "operation": str(getattr(state, "operation", "")),
                    "direction": str(getattr(state, "direction", "")),
                    "distance_mm": round(float(getattr(state, "distance_mm", 0.0)), 3),
                    "distance_origin": str(getattr(state, "distance_origin", "")),
                    "end_effector_profile": str(
                        getattr(state, "end_effector_profile", "")
                    ),
                    "active_request_id": str(getattr(state, "active_request_id", "")),
                    "progress": round(float(getattr(state, "progress", 0.0)), 3),
                    "error_code": str(getattr(state, "error_code", "")),
                    "rejection_reason": str(getattr(state, "rejection_reason", "")),
                }
            )
        return rows

    def _pending_bed_robot_arm_group_request_context(self) -> dict[str, Any] | None:
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": str(request.group_id),
            "operation": str(request.operation),
            "voice_text": str(request.voice_text),
            "procedure_id": str(request.procedure_id),
            "end_effector_profile": str(request.end_effector_profile),
            "source": str(request.source),
        }

    def _on_skill_status(self, msg: SkillStatus) -> None:
        if msg.state != "completed":
            return
        self._recent_skill_statuses.append(
            {
                "at": round(self._causal_now_sec(), 2),
                "action": msg.action,
                "tool": msg.instrument_id,
                "state": msg.state,
                "success": bool(msg.success),
                "message": msg.message,
            }
        )

    def _on_control(self, msg: String) -> None:
        command, _, start_phase_id = msg.data.strip().partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()
        if command in {"start", "start_actors"}:
            self._reset_public_evidence()
            self._last_replay_image_stamp_sec = None
            self._last_periodic_live_image_stamp_sec = None
            self._last_submitted_live_image_stamp_sec = None
            self._reset_source_time_live_trigger(reset_stamp=True)
            self._reset_fast_cam4_mayo_observations()
            self._phase_bootstrap_observation_count = 0
            self._phase_bootstrap_explicit = bool(
                start_phase_id and start_phase_id in self._spec.phase_ids
            )
            self._phase_bootstrap_id = (
                start_phase_id
                if start_phase_id in self._spec.phase_ids
                else self._spec.default_phase_id
            )
            self._last_authoritative_phase = self._phase_bootstrap_id
            self._active = True
        elif command == "pause":
            self._active = False
            self._inference_backpressure.clear_pending()
            self._reset_source_time_live_trigger(reset_stamp=False)
        elif command == "resume":
            self._active = True
        elif command == "stop":
            self._active = False
            self._inference_backpressure.clear_pending()
            self._last_replay_image_stamp_sec = None
            self._last_periodic_live_image_stamp_sec = None
            self._last_submitted_live_image_stamp_sec = None
            self._reset_source_time_live_trigger(reset_stamp=True)
            self._reset_fast_cam4_mayo_observations()
        elif command == "reset":
            self._active = False
            self._inference_backpressure.clear_pending()
            self._world = None
            self._simulation = None
            self._last_vlm_phase = ""
            self._last_authoritative_phase = ""
            self._phase_bootstrap_id = ""
            self._phase_bootstrap_observation_count = 0
            self._phase_bootstrap_explicit = False
            self._phase_entered_wall_sec = self._causal_now_sec()
            self._last_good_raw = ""
            self._last_good_payload = None
            self._reset_public_evidence()
            self._oracle_tick = 0
            self._last_replay_image_stamp_sec = None
            self._last_periodic_live_image_stamp_sec = None
            self._last_submitted_live_image_stamp_sec = None
            self._reset_source_time_live_trigger(reset_stamp=True)
            self._reset_fast_cam4_mayo_observations()

    def _reset_public_evidence(self) -> None:
        self._recent_events.clear()
        self._reset_transient_public_evidence()
        self._latest_bed_robot_arm_group_request = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._phase_entered_wall_sec = self._causal_now_sec()

    def _reset_transient_public_evidence(self) -> None:
        self._recent_observed_signals.clear()
        self._recent_speech.clear()
        self._recent_skill_statuses.clear()

    def _authoritative_runtime_phase(self) -> str:
        for runtime_state in (
            getattr(self, "_world", None),
            getattr(self, "_simulation", None),
        ):
            phase_id = str(getattr(runtime_state, "filtered_phase", "") or "")
            if phase_id in self._spec.phase_ids:
                return phase_id
        bootstrap_phase = str(getattr(self, "_phase_bootstrap_id", "") or "")
        if bootstrap_phase in self._spec.phase_ids:
            return bootstrap_phase
        return self._spec.default_phase_id

    def _track_authoritative_phase(self) -> None:
        phase_id = self._authoritative_runtime_phase()
        if phase_id == getattr(self, "_last_authoritative_phase", ""):
            return
        self._last_authoritative_phase = phase_id
        self._phase_entered_wall_sec = self._causal_now_sec()

    def _bt_snapshot_msg(self) -> BTContextSnapshot:
        snapshot = BTContextSnapshot()
        if self._latest_bt is None:
            return snapshot
        snapshot.stamp = self._latest_bt.stamp
        snapshot.procedure_id = self._world.procedure_id if self._world is not None else ""
        snapshot.filtered_phase = self._world.filtered_phase if self._world is not None else ""
        snapshot.decision = self._latest_bt.decision
        snapshot.selected_tool = self._latest_bt.selected_tool
        snapshot.selected_tool_lifecycle = self._latest_bt.selected_tool_lifecycle
        snapshot.next_required_transition = self._latest_bt.next_required_transition
        snapshot.blocking_guard = self._latest_bt.blocking_guard
        snapshot.decision_reason = self._latest_bt.decision_reason
        snapshot.rationale = self._latest_bt.rationale
        return snapshot

    def _publish_context_summaries(self) -> None:
        if self._world is None or self._simulation is None:
            return
        context_msg, context_dict = self._assemble_context()
        if self._context_mode != "actor_log":
            self._request_context_pub.publish(context_msg)

        phase_summary = String()
        phase_summary.data = compact_json(
            {
                "proc": self._world.procedure_id,
                "ph": self._world.filtered_phase,
                "conf": round(float(self._world.phase_confidence), 3),
                "unc": bool(self._world.phase_uncertain),
                "req": self._world.surgeon_request_tool or self._world.explicit_request_tool,
            }
        )
        self._phase_summary_pub.publish(phase_summary)

        tool_summary = String()
        tool_summary.data = compact_json(
            {
                "active": context_dict["tools"],
                "pending": context_dict["pending"],
            }
        )
        self._tool_summary_pub.publish(tool_summary)

    def _assemble_context(self) -> tuple[VLMRequestContext, dict[str, Any]]:
        assert self._world is not None
        assert self._simulation is not None
        active_tools: list[str] = []
        non_home_tools: list[str] = []
        tool_rows: list[dict[str, Any]] = []
        for instrument in self._world.instrument_states:
            at_home = (
                instrument.location_id == instrument.home_location_id
                and instrument.location_type == instrument.home_location_type
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
            )
            is_context_relevant = (
                not at_home
                or instrument.instrument_id in set(self._world.expected_instruments)
                or instrument.instrument_id in set(self._world.pending_transition_tools)
                or instrument.instrument_id
                in {
                    self._world.right_hand_tool,
                    self._world.left_hand_tool,
                    self._world.prepositioned_tool,
                    self._world.surgeon_request_tool,
                    self._world.explicit_request_tool,
                }
            )
            if at_home:
                continue
            non_home_tools.append(instrument.instrument_id)
            if is_context_relevant:
                active_tools.append(instrument.instrument_id)
                tool_rows.append(
                    {
                        "id": instrument.instrument_id,
                        "lc": instrument.lifecycle_stage,
                        "loc": instrument.location_id,
                        "lt": instrument.location_type,
                        "nx": instrument.next_required_transition,
                        "own": instrument.owner,
                    }
                )

        recent_events = list(self._recent_events)[-6:]
        bt_snapshot = self._bt_snapshot_msg()
        context_dict = {
            "proc": self._world.procedure_id,
            "ph": {
                "id": self._world.filtered_phase,
                "c": round(float(self._world.phase_confidence), 3),
                "u": bool(self._world.phase_uncertain),
            },
            "rq": {
                "exp": self._world.explicit_request_tool,
                "sg": self._world.surgeon_request_tool,
                "intent": self._world.surgeon_intent,
            },
            "hands": {
                "rh": self._world.right_hand_tool,
                "lh": self._world.left_hand_tool,
                "pre": self._world.prepositioned_tool,
                "cb": bool(self._world.cleaner_busy),
                "ct": round(float(self._world.cleaner_remaining_sec), 2),
            },
            "exp": list(self._world.expected_instruments),
            "tools": tool_rows,
            "pending": list(self._world.pending_transition_tools),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "ev": [
                {
                    "t": event.event_type,
                    "tool": event.instrument_id,
                    "a": event.anchor_id,
                    "r": event.reason,
                }
                for event in recent_events
            ],
            "bt": {
                "d": bt_snapshot.decision,
                "tool": bt_snapshot.selected_tool,
                "lc": bt_snapshot.selected_tool_lifecycle,
                "nx": bt_snapshot.next_required_transition,
                "why": bt_snapshot.decision_reason or bt_snapshot.rationale,
                "blk": bt_snapshot.blocking_guard,
            },
        }

        msg = VLMRequestContext()
        msg.stamp = self._world.stamp
        msg.procedure_id = self._world.procedure_id
        msg.filtered_phase = self._world.filtered_phase
        msg.phase_confidence = float(self._world.phase_confidence)
        msg.phase_uncertain = bool(self._world.phase_uncertain)
        msg.explicit_request_tool = self._world.explicit_request_tool
        msg.surgeon_request_tool = self._world.surgeon_request_tool
        msg.surgeon_intent = self._world.surgeon_intent
        msg.right_hand_tool = self._world.right_hand_tool
        msg.left_hand_tool = self._world.left_hand_tool
        msg.prepositioned_tool = self._world.prepositioned_tool
        msg.cleaner_busy = bool(self._world.cleaner_busy)
        msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
        msg.phase_expected_tools = list(self._world.expected_instruments)
        msg.active_tool_ids = active_tools
        msg.non_home_tool_ids = non_home_tools
        msg.pending_transition_tools = list(self._world.pending_transition_tools)
        msg.recent_events = recent_events
        msg.bt_snapshot = bt_snapshot
        msg.bed_robot_arm_groups = list(
            getattr(self._world, "bed_robot_arm_groups", [])
        )
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_json(context_dict)
        return msg, context_dict

    def _public_event_digests(self) -> list[EventDigest]:
        return [event for event in self._recent_events if event.event_type in PUBLIC_DIGITAL_TWIN_EVENT_TYPES][-6:]

    def _public_digital_twin_context(self) -> dict[str, Any]:
        if self._world is None or self._simulation is None:
            return {}
        _, world_context = self._assemble_context()
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        return {
            "hands": world_context.get("hands", {}),
            "tools": world_context.get("tools", []),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "events": public_events[-6:],
        }

    def _perception_samples(
        self,
        kind: str,
    ) -> list[tuple[float, dict[str, Any]]]:
        buffer = getattr(self, "_perception_buffers", {}).get(kind)
        samples = list(buffer) if buffer else []
        latest = self._latest_perception.get(kind)
        if latest is not None and (
            not samples or samples[-1] is not latest
        ):
            samples.append(latest)
        return samples

    def _closest_perception_sample(
        self,
        kind: str,
        *,
        reference_stamp_sec: float | None,
        stamp_key: str,
    ) -> tuple[float, dict[str, Any]] | None:
        now = self._causal_now_sec()
        fresh = [
            sample
            for sample in self._perception_samples(kind)
            if max(0.0, now - float(sample[0]))
            <= self._perception_stale_sec
        ]
        if not fresh:
            return None
        if reference_stamp_sec is None:
            return fresh[-1]

        stamped: list[tuple[float, dict[str, Any], float]] = []
        for received_at, summary in fresh:
            stamp_sec = _finite_float(summary.get(stamp_key))
            if stamp_sec is not None:
                stamped.append((received_at, summary, stamp_sec))
        if not stamped:
            return fresh[-1]
        received_at, summary, _ = min(
            stamped,
            key=lambda sample: abs(
                sample[2] - float(reference_stamp_sec)
            ),
        )
        return received_at, summary

    def _enrich_cam4_tool_stability(
        self,
        summary: dict[str, Any],
        *,
        reference_stamp_sec: float,
    ) -> dict[str, Any]:
        """Add bounded dwell evidence from source-aligned CAM4 summaries."""

        start_sec = (
            float(reference_stamp_sec) - CAM4_MAYO_STABILITY_WINDOW_SEC
        )
        histories: dict[str, list[tuple[float, float]]] = {}
        for _received_at, sample in self._perception_samples(
            "cam4_semantics"
        ):
            stamp_sec = _finite_float(sample.get("source_stamp_sec"))
            if (
                stamp_sec is None
                or stamp_sec < start_sec
                or stamp_sec
                > float(reference_stamp_sec)
                + self._perception_image_max_skew_sec
            ):
                continue
            request = sample.get("tool_request", {})
            if (
                isinstance(request, dict)
                and str(request.get("state", "")) == "request"
            ):
                continue
            for row in sample.get("tools", []):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", "")).strip()
                confidence = _finite_float(
                    row.get("max_confidence")
                )
                if name and confidence is not None:
                    histories.setdefault(name, []).append(
                        (stamp_sec, confidence)
                    )

        enriched = dict(summary)
        enriched_tools: list[dict[str, Any]] = []
        for row in summary.get("tools", []):
            if not isinstance(row, dict):
                continue
            bounded = dict(row)
            history = histories.get(str(row.get("name", "")).strip(), [])
            bounded["stable_sample_count"] = len(history)
            bounded["stable_duration_sec"] = round(
                max(0.0, history[-1][0] - history[0][0])
                if history
                else 0.0,
                6,
            )
            enriched_tools.append(bounded)
        enriched["tools"] = enriched_tools
        return enriched

    def _public_perception_context(
        self,
        image_stamp_sec: float | None = None,
    ) -> dict[str, Any]:
        now = self._causal_now_sec()
        reference_stamp_sec = (
            image_stamp_sec
            if image_stamp_sec is not None
            else getattr(
                self,
                "_current_perception_reference_stamp_sec",
                None,
            )
        )
        cam4_semantics_topic = str(
            getattr(self, "_cam4_semantics_topic", "")
        ).strip()
        if cam4_semantics_topic:
            payload = self._closest_perception_sample(
                "cam4_semantics",
                reference_stamp_sec=reference_stamp_sec,
                stamp_key="source_stamp_sec",
            )
            if payload is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {"status": "missing"},
                }
            received_monotonic, summary = payload
            received_age_sec = max(0.0, now - received_monotonic)
            if received_age_sec > self._perception_stale_sec:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_receive_stale",
                        "received_age_sec": round(received_age_sec, 3),
                    },
                }
            if reference_stamp_sec is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {"status": "omitted_no_flir_reference"},
                }
            summary_stamp_sec = _finite_float(
                summary.get("source_stamp_sec")
            )
            if summary_stamp_sec is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_missing_source_timestamp"
                    },
                }
            offset_sec = float(summary_stamp_sec) - float(reference_stamp_sec)
            if (
                abs(offset_sec)
                > self._perception_image_max_skew_sec + 1.0e-9
            ):
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_source_timestamp_misaligned",
                        "detector_stamp_sec": round(summary_stamp_sec, 6),
                        "offset_sec": round(offset_sec, 6),
                    },
                }
            aligned_summary = self._enrich_cam4_tool_stability(
                summary,
                reference_stamp_sec=float(reference_stamp_sec),
            )
            return {
                **aligned_summary,
                "flir_reference_stamp_sec": round(
                    float(reference_stamp_sec),
                    6,
                ),
                "max_source_skew_sec": (
                    self._perception_image_max_skew_sec
                ),
                "alignment": {
                    "status": "aligned",
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                    "offset_sec": round(offset_sec, 6),
                },
            }

        summaries: dict[str, Any] = {}
        alignment: dict[str, dict[str, Any]] = {}
        configured_topics = {
            "bboxes": str(
                getattr(self, "_perception_bboxes_topic", "")
            ).strip(),
            "segmentation": str(
                getattr(self, "_perception_segmentation_topic", "")
            ).strip(),
        }
        for kind in ("bboxes", "segmentation"):
            payload = self._closest_perception_sample(
                kind,
                reference_stamp_sec=reference_stamp_sec,
                stamp_key="timestamp_sec",
            )
            if payload is None:
                if configured_topics[kind]:
                    alignment[kind] = {"status": "missing"}
                continue
            received_monotonic, summary = payload
            received_age_sec = max(0.0, now - received_monotonic)
            if received_age_sec > self._perception_stale_sec:
                alignment[kind] = {
                    "status": "omitted_receive_stale",
                    "received_age_sec": round(received_age_sec, 3),
                }
                continue
            bounded_summary = dict(summary)
            if reference_stamp_sec is None:
                alignment[kind] = {
                    "status": "omitted_no_cam4_reference",
                }
                continue
            summary_stamp_sec = _finite_float(
                bounded_summary.get("timestamp_sec")
            )
            if summary_stamp_sec is None:
                alignment[kind] = {
                    "status": "omitted_missing_source_timestamp",
                }
                continue
            offset_sec = float(summary_stamp_sec) - float(reference_stamp_sec)
            skew_sec = abs(offset_sec)
            if skew_sec > self._perception_image_max_skew_sec + 1.0e-9:
                alignment[kind] = {
                    "status": "omitted_source_timestamp_misaligned",
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                    "offset_sec": round(offset_sec, 6),
                }
                continue
            bounded_summary["image_stamp_skew_sec"] = round(
                skew_sec,
                6,
            )
            summaries[kind] = bounded_summary
            alignment[kind] = {
                "status": "aligned",
                "detector_stamp_sec": round(summary_stamp_sec, 6),
                "offset_sec": round(offset_sec, 6),
            }
        if not summaries and not alignment:
            return {}
        return {
            "source": "cam4_public_detector",
            "bounded": True,
            "ground_truth": False,
            "cam4_reference_stamp_sec": (
                round(float(reference_stamp_sec), 6)
                if reference_stamp_sec is not None
                else None
            ),
            "max_source_skew_sec": self._perception_image_max_skew_sec,
            "alignment": alignment,
            **summaries,
        }

    def _actor_log_request_context_msg(
        self,
        context: dict[str, Any],
        compact_context: str,
        observation_stamp,
    ) -> VLMRequestContext:
        msg = VLMRequestContext()
        msg.stamp = observation_stamp
        msg.procedure_id = self._spec.procedure_id
        if self._world is not None:
            msg.right_hand_tool = self._world.right_hand_tool
            msg.left_hand_tool = self._world.left_hand_tool
            msg.prepositioned_tool = self._world.prepositioned_tool
            msg.cleaner_busy = bool(self._world.cleaner_busy)
            msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
            msg.active_tool_ids = [str(row.get("id", "")) for row in context.get("digital_twin", {}).get("tools", []) if row.get("id")]
            msg.recent_events = self._public_event_digests()
            msg.bed_robot_arm_groups = list(
                getattr(self._world, "bed_robot_arm_groups", [])
            )
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_context
        return msg

    def _resolve_tool_mention(self, text: str) -> str:
        lowered = str(text or "").lower().replace("\xa0", " ").strip()
        if not lowered:
            return ""

        correction_parts = re.split(
            r"\b(?:not|instead(?:\s+of)?|rather(?:\s+than)?)\b|"
            r"(?:아니(?:야|고|라)?|말고)",
            lowered,
        )
        search_parts = (
            [correction_parts[-1]]
            if len(correction_parts) > 1
            else [lowered]
        )
        for part in search_parts:
            tokens = re.findall(r"[0-9a-z_가-힣#]+", part)
            spans = [
                (
                    start + width,
                    width,
                    " ".join(tokens[start : start + width]),
                )
                for width in range(1, len(tokens) + 1)
                for start in range(0, len(tokens) - width + 1)
            ]
            spans.sort(reverse=True)
            for _, _, candidate in spans:
                resolved = self._spec.resolve_instrument_alias(candidate)
                if resolved:
                    return resolved
        return ""

    def _tool_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> str:
        now = self._causal_now_sec()
        for row in reversed(speech_rows):
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            try:
                speech_at = float(row.get("at", 0.0))
            except (TypeError, ValueError):
                speech_at = 0.0
            if speech_at <= 0.0 or now - speech_at > PUBLIC_REQUEST_MAX_AGE_SEC:
                continue
            resolved = self._resolve_tool_mention(text)
            if not resolved:
                if re.search(
                    r"\b(?:cancel|no|never\s+mind)\b|"
                    r"(?:아니(?:야|요)?|취소)",
                    text.lower(),
                ):
                    return ""
                continue
            completed_after_request = any(
                str(status.get("state", "")) == "completed"
                and bool(status.get("success", False))
                and str(status.get("action", "")).lower()
                in HANDOVER_SKILL_ACTIONS
                and self._canonical_tool_id(status.get("tool", "")) == resolved
                and float(status.get("at", 0.0) or 0.0) >= speech_at
                for status in self._recent_skill_statuses
                if isinstance(status, dict)
            )
            if completed_after_request:
                return ""
            return resolved
        return ""

    def _tools_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> list[str]:
        tools: list[str] = []
        for row in speech_rows:
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            resolved = self._resolve_tool_mention(text)
            if resolved and resolved not in tools:
                tools.append(resolved)
        return tools

    def _canonical_tool_id(self, raw_tool: object) -> str:
        tool_id = str(raw_tool or "").strip()
        if not tool_id:
            return ""
        resolved = self._spec.resolve_instrument_alias(tool_id)
        if resolved:
            return resolved
        cleaned = tool_id.strip(" \t\r\n,;:'\"`[]{}()")
        resolved = self._spec.resolve_instrument_alias(cleaned)
        if resolved:
            return resolved
        match = re.search(r"(?i)\bt[\s_-]*0*\d+\b", cleaned)
        return self._spec.resolve_instrument_alias(match.group(0)) if match else ""

    def _canonical_phase_id(self, raw_phase: object) -> str:
        phase_id = str(raw_phase or "").strip()
        if not phase_id:
            return ""
        resolved = self._spec.resolve_phase_id(phase_id)
        if resolved:
            return resolved
        cleaned = phase_id.strip(" \t\r\n,;:'\"`[]{}()")
        resolved = self._spec.resolve_phase_id(cleaned)
        if resolved:
            return resolved
        match = re.search(r"(?i)\bp[\s_-]*0*\d+\b", cleaned)
        return self._spec.resolve_phase_id(match.group(0)) if match else ""

    @staticmethod
    def _canonical_intent_type(raw_intent: object) -> str:
        cleaned = str(raw_intent or "").strip().strip(" \t\r\n,;:'\"`[]{}()").lower()
        aliases = {
            "handover": "handover",
            "request": "request_tool",
            "request_tool": "request_tool",
            "voice_request": "request_tool",
            "return": "return_tool",
            "return_tool": "return_tool",
            "recover": "return_tool",
            "none": "none",
            "no_intent": "none",
            "": "none",
        }
        return aliases.get(cleaned, "none")

    def _canonicalize_payload_ids(self, payload: dict[str, Any]) -> dict[str, Any]:
        canonical = json.loads(json.dumps(payload))
        version = str(canonical.get("v", ""))
        if version in {"3", "4"}:
            canonical["phase"] = [
                [phase_id, float(row[1])]
                for row in canonical.get("phase", [])
                if isinstance(row, list)
                and len(row) == 2
                and (phase_id := self._canonical_phase_id(row[0]))
            ]
            canonical["tool"] = [
                [tool_id, float(row[1])]
                for row in canonical.get("tool", [])
                if isinstance(row, list)
                and len(row) == 2
                and (tool_id := self._canonical_tool_id(row[0]))
            ]
        elif version == "2":
            phase = canonical.get("phase", ["", 0.0])
            tool = canonical.get("tool", ["", 0.0])
            phase_id = (
                self._canonical_phase_id(phase[0])
                if isinstance(phase, list) and len(phase) == 2
                else ""
            )
            tool_id = (
                self._canonical_tool_id(tool[0])
                if isinstance(tool, list) and len(tool) == 2
                else ""
            )
            canonical["phase"] = [
                phase_id,
                float(phase[1]) if phase_id else 0.0,
            ]
            canonical["tool"] = [
                tool_id,
                float(tool[1]) if tool_id else 0.0,
            ]
        else:
            canonical["ph"] = [
                [phase_id, float(row[1])]
                for row in canonical.get("ph", [])
                if isinstance(row, list)
                and len(row) == 2
                and (phase_id := self._canonical_phase_id(row[0]))
            ]
            canonical["to"] = [
                [tool_id, str(row[1]), str(row[2]), float(row[3])]
                for row in canonical.get("to", [])
                if isinstance(row, list)
                and len(row) == 4
                and (tool_id := self._canonical_tool_id(row[0]))
            ]

        intent_key = "intent" if version in {"2", "3", "4"} else "sg"
        intent = canonical.get(intent_key, ["none", "", 0.0] if intent_key == "intent" else ["", "", "", 0.0])
        if intent_key == "intent" and isinstance(intent, list) and len(intent) == 3:
            intent_type = self._canonical_intent_type(intent[0])
            intent_tool = self._canonical_tool_id(intent[1])
            if intent_type == "none" or not intent_tool:
                canonical[intent_key] = ["none", "", 0.0]
            else:
                canonical[intent_key] = [
                    intent_type,
                    intent_tool,
                    float(intent[2]),
                ]
        elif intent_key == "sg" and isinstance(intent, list) and len(intent) == 4:
            event_type = self._canonical_intent_type(intent[0])
            intent_tool = self._canonical_tool_id(intent[1])
            if event_type == "none" or not intent_tool:
                canonical[intent_key] = ["", "", "", 0.0]
            else:
                canonical[intent_key] = [
                    event_type,
                    intent_tool,
                    str(intent[2]),
                    float(intent[3]),
                ]
        if version == "4":
            gesture = canonical.get("gesture", ["", "", "", 0.0])
            if isinstance(gesture, list) and len(gesture) == 4:
                gesture_type = self._canonical_intent_type(gesture[0])
                gesture_tool = self._canonical_tool_id(gesture[1])
                hand_pose = str(gesture[2]).strip().lower()
                try:
                    confidence = float(gesture[3])
                except (TypeError, ValueError):
                    confidence = 0.0
                if (
                    gesture_type in {"handover", "request_tool"}
                    and hand_pose == "open_receive"
                    and confidence > 0.0
                ):
                    canonical["gesture"] = [
                        "request_tool",
                        gesture_tool,
                        "open_receive",
                        confidence,
                    ]
                else:
                    canonical["gesture"] = ["", "", "", 0.0]
            else:
                canonical["gesture"] = ["", "", "", 0.0]

        mayo_rows = []
        for row in canonical.get("mayo", []):
            if not isinstance(row, list) or len(row) != 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            decision = str(row[1]).strip().strip(" \t\r\n,;:'\"`[]{}()").lower()
            if tool_id and decision in {"recover", "reuse"}:
                mayo_rows.append([tool_id, decision, float(row[2])])
        canonical["mayo"] = mayo_rows
        retrieve = canonical.get("mayo_retrieve", ["", 0.0])
        if isinstance(retrieve, list) and len(retrieve) == 2:
            retrieve_tool = self._canonical_tool_id(retrieve[0])
            canonical["mayo_retrieve"] = [
                retrieve_tool,
                float(retrieve[1]) if retrieve_tool else 0.0,
            ]
        canonical["mayo"], canonical["mayo_retrieve"] = normalize_mayo_semantics(
            canonical.get("mayo", []),
            canonical.get("mayo_retrieve", ["", 0.0]),
        )
        return canonical

    def _actor_log_prior_evidence(
        self,
        *,
        open_set_phase_search: bool = False,
    ) -> dict[str, Any]:
        speech_rows = self._fresh_rows(
            self._recent_speech,
            max_age_sec=35.0 if open_set_phase_search else 18.0,
        )
        observed_signals = self._fresh_rows(
            self._recent_observed_signals,
            max_age_sec=35.0 if open_set_phase_search else 8.0,
        )
        skill_statuses = self._fresh_rows(
            self._recent_skill_statuses,
            max_age_sec=35.0 if open_set_phase_search else 22.0,
        )
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        hand_tools: list[str] = []
        recent_tools: list[str] = []
        # A model hypothesis is not a phase boundary. Only the authoritative
        # digital twin (or the operator-selected startup phase before its first
        # state arrives) may constrain the temporal candidate window.
        current_phase = self._authoritative_runtime_phase()
        mayo_tools: list[str] = []
        if self._world is not None:
            # For next-request prediction, only surgeon-owned tools count as
            # already progressed. Robot-held/prepositioned tools are visible
            # context, but the surgeon may still ask for them next.
            hand_tools = [
                instrument.instrument_id
                for instrument in self._world.instrument_states
                if instrument.lifecycle_stage == "surgeon_owned"
            ]
            mayo_tools = [
                instrument.instrument_id
                for instrument in self._world.instrument_states
                if instrument.location_type == "mayo"
                or instrument.lifecycle_stage in {"mayo_reuse", "mayo_recovery"}
            ]
        return {
            "current_phase": current_phase,
            # Model hypotheses are not phase boundaries. Until the open-set
            # bootstrap closes, keep the whole bounded evidence window even if
            # successive image-only guesses disagree.
            "phase_entered_sec": (
                0.0
                if open_set_phase_search
                else self._phase_entered_wall_sec
            ),
            "speech": speech_rows,
            "speech_tools": self._tools_from_public_speech([row for row in speech_rows if isinstance(row, dict)]),
            "recent_tools": recent_tools,
            "observed_signals": observed_signals,
            "skill_status": skill_statuses,
            "mayo_tools": mayo_tools,
            "hand_tools": hand_tools,
            "events": public_events,
        }

    def _fresh_rows(self, rows: deque[dict[str, Any]], *, max_age_sec: float) -> list[dict[str, Any]]:
        now = self._causal_now_sec()
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                at = float(row.get("at", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            if at <= 0.0 or now - at <= max_age_sec:
                fresh.append(dict(row))
        return fresh

    def _assemble_actor_log_context_dict(self) -> dict[str, Any]:
        open_set_phase_search = should_use_open_set_phase_bootstrap(
            getattr(self, "_phase_bootstrap_observation_count", 0),
            getattr(self, "_open_set_phase_bootstrap_observations", 0),
            getattr(self, "_phase_bootstrap_explicit", False),
        )
        evidence = self._actor_log_prior_evidence(
            open_set_phase_search=open_set_phase_search,
        )
        if open_set_phase_search:
            candidates = self._prior_scorer.score_open_set(evidence)
        else:
            candidates = self._prior_scorer.score(evidence)
            candidates = dict(candidates)
            candidate_evidence = dict(candidates.get("evidence", {}))
            candidate_evidence["phase_search_mode"] = "temporal_prior"
            candidates["evidence"] = candidate_evidence
        previous = {
            "phase": self._last_good_payload.get("phase", ["", 0.0]) if self._last_good_payload else ["", 0.0],
            "tool": self._last_good_payload.get("tool", ["", 0.0]) if self._last_good_payload else ["", 0.0],
        }
        if open_set_phase_search:
            previous = {"phase": [], "tool": []}
        context = {
            "proc": self._spec.procedure_id,
            "procedure_prompt_id": str(self._procedure_prompt.get("id", "")) if isinstance(self._procedure_prompt, dict) else "",
            "phase_search_mode": (
                "open_set" if open_set_phase_search else "temporal_prior"
            ),
            "phases": [
                {"id": phase.id, "tools": list(phase.expected_instruments), "next": list(phase.possible_next)}
                for phase in self._spec.bundle.phases
            ],
            "tools": [
                {
                    "id": instrument.id,
                    "role": instrument.role,
                    "requestable": bool(getattr(instrument, "requestable", True)),
                }
                for instrument in self._spec.bundle.instruments
            ],
            "evidence_window": {
                "speech": list(evidence.get("speech", [])),
                "observed_signals": list(evidence.get("observed_signals", [])),
                "skill_status": list(evidence.get("skill_status", [])),
            },
            "visual_input": dict(self._current_visual_input),
            "observable_perception": self._public_perception_context(),
            "digital_twin": self._public_digital_twin_context(),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "candidates": candidates,
            "previous": previous,
        }
        return bound_actor_log_context(context)

    def _assemble_actor_log_context(self) -> str:
        return compact_json(self._assemble_actor_log_context_dict())

    def _primary_payload_phase(self, payload: dict[str, Any]) -> str:
        version = str(payload.get("v", ""))
        rows = payload.get("phase", []) if version in {"2", "3", "4"} else payload.get("ph", [])
        raw_phase = ""
        if version == "2" and isinstance(rows, list) and len(rows) == 2:
            raw_phase = rows[0]
        elif isinstance(rows, list) and rows and isinstance(rows[0], list):
            raw_phase = rows[0][0] if rows[0] else ""
        return self._canonical_phase_id(raw_phase)

    def _fresh_image(self, label: str, now: float) -> ImageSample | None:
        sample = self._latest_images.get(label)
        if sample is None:
            return None
        if now - sample.received_monotonic > self._image_stale_sec:
            return None
        return sample

    def _fresh_images(self, label: str, now: float) -> list[ImageSample]:
        buffer = getattr(self, "_image_buffers", {}).get(label)
        samples = list(buffer) if buffer else []
        latest = self._latest_images.get(label)
        if latest is not None and (
            not samples or samples[-1] is not latest
        ):
            samples.append(latest)
        return [
            sample
            for sample in samples
            if now - sample.received_monotonic <= self._image_stale_sec
        ]

    def _fresh_multiview_pair(
        self,
        now: float,
    ) -> tuple[ImageSample | None, ImageSample | None, float | None]:
        fields = self._fresh_images("field", now)
        cam4_samples = self._fresh_images("cam4", now)
        if not fields or not cam4_samples:
            return (
                fields[-1] if fields else None,
                cam4_samples[-1] if cam4_samples else None,
                None,
            )

        pairs = [
            (
                field,
                cam4,
                abs(
                    image_sample_stamp_sec(field)
                    - image_sample_stamp_sec(cam4)
                ),
            )
            for field in fields
            for cam4 in cam4_samples
        ]
        aligned = [
            pair
            for pair in pairs
            if pair[2] <= self._multiview_max_skew_sec + 1.0e-9
        ]
        if aligned:
            return max(
                aligned,
                key=lambda pair: (
                    min(
                        image_sample_stamp_sec(pair[0]),
                        image_sample_stamp_sec(pair[1]),
                    ),
                    -pair[2],
                ),
            )
        return min(pairs, key=lambda pair: pair[2])

    def _current_cam4_crop(
        self,
        cam4: ImageSample,
    ) -> tuple[float, float, float, float]:
        if not self._cam4_dynamic_crop:
            return self._cam4_crop_xywh_norm
        perception = self._public_perception_context(
            image_sample_stamp_sec(cam4)
        )
        summaries = [
            perception.get(kind, {})
            for kind in ("bboxes", "segmentation")
            if isinstance(perception.get(kind), dict)
        ]
        return dynamic_cam4_crop_xywh(
            summaries,
            fallback_xywh_norm=self._cam4_crop_xywh_norm,
            padding_norm=self._cam4_crop_padding_norm,
            minimum_width_norm=self._cam4_crop_min_width_norm,
            minimum_height_norm=self._cam4_crop_min_height_norm,
        )

    def _select_images(
        self,
    ) -> tuple[list[tuple[str, bytes, str]], str, ModelImage | None]:
        now = self._causal_now_sec()
        perception_enabled = bool(
            getattr(self, "_perception_enabled", True)
        )
        segmented_field = (
            self._fresh_image("field", now)
            if perception_enabled
            else None
        )
        raw_field = self._fresh_image("raw_field", now)
        field = segmented_field or raw_field
        if field is None and not self._raw_field_image_topic:
            # Simulation/no-image configurations may intentionally publish
            # their raw visual stream on the legacy field topic.
            field = self._fresh_image("field", now)
        self._current_image_input_error = ""
        if field is None:
            self._current_image_input_error = (
                "missing fresh FLIR image (segmented and raw fallback unavailable)"
            )
            self._current_perception_reference_stamp_sec = None
            self._current_visual_input = {
                "image_source": "missing(flir_visual)",
                "model_ready_topic": self._composite_image_topic,
                "sources": [],
                "preprocessing": "unavailable",
                "cam4_image_forwarded_to_vlm": False,
                "detector_advisory": perception_enabled,
                "input_error": self._current_image_input_error,
            }
            return [], "missing(flir_visual)", None

        cam4: ImageSample | None = None
        cam4_skew_sec: float | None = None
        cam4_samples = self._fresh_images("cam4", now)
        if cam4_samples:
            nearest_cam4 = min(
                cam4_samples,
                key=lambda sample: abs(
                    image_sample_stamp_sec(sample)
                    - image_sample_stamp_sec(field)
                ),
            )
            cam4_skew_sec = abs(
                image_sample_stamp_sec(nearest_cam4)
                - image_sample_stamp_sec(field)
            )
            if cam4_skew_sec <= self._multiview_max_skew_sec + 1.0e-9:
                cam4 = nearest_cam4

        image_max_side_px = self._image_max_side_px
        if cam4 is not None:
            multiview_limit = getattr(
                self,
                "_multiview_image_max_side_px",
                DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX,
            )
            image_max_side_px = (
                min(image_max_side_px, multiview_limit)
                if image_max_side_px > 0
                else multiview_limit
            )

        field_bytes, field_mime = bound_image_for_model(
            field.data,
            field.mime_type,
            image_max_side_px,
        )
        using_segmented_field = field is segmented_field
        model_image = ModelImage(
            label=(
                "RFDETR-segmented FLIR surgical field"
                if using_segmented_field
                else "Raw FLIR surgical field (detector-independent fallback)"
            ),
            data=field_bytes,
            mime_type=field_mime,
            stamp_sec=field.stamp_sec,
            stamp_nanosec=field.stamp_nanosec,
            frame_id=field.frame_id,
        )
        image_source = (
            "flir_rfdetr_segmented"
            if using_segmented_field
            else "flir_raw_fallback"
        )
        self._current_perception_reference_stamp_sec = image_sample_stamp_sec(
            field
        )
        images: list[tuple[str, bytes, str]] = [
            (
                model_image.label,
                model_image.data,
                model_image.mime_type,
            )
        ]
        sources = [
            {
                "role": (
                    "flir_segmented"
                    if using_segmented_field
                    else "flir_raw"
                ),
                "topic": (
                    self._field_image_topic
                    if using_segmented_field
                    else (
                        self._raw_field_image_topic
                        or self._field_image_topic
                    )
                ),
                "stamp_sec": round(
                    image_sample_stamp_sec(field),
                    9,
                ),
                "frame_id": field.frame_id,
            }
        ]
        cam4_forwarded = False
        if cam4 is not None:
            cam4_bytes, cam4_mime = crop_cam4_for_model(
                cam4.data,
                cam4_crop_xywh_norm=self._current_cam4_crop(cam4),
                max_side_px=image_max_side_px,
            )
            images.append(
                (
                    "CAM4 context crop for Mayo and surgeon-hand observation; inspect pixels directly",
                    cam4_bytes,
                    cam4_mime,
                )
            )
            sources.append(
                {
                    "role": "cam4_context_crop",
                    "topic": self._cam4_image_topic,
                    "stamp_sec": round(
                        image_sample_stamp_sec(cam4),
                        9,
                    ),
                    "frame_id": cam4.frame_id,
                    "offset_sec": round(
                        image_sample_stamp_sec(cam4)
                        - image_sample_stamp_sec(field),
                        9,
                    ),
                }
            )
            cam4_forwarded = True
        if self._require_cam4_image and not cam4_forwarded:
            self._current_image_input_error = (
                "missing synchronized raw CAM4 Mayo/hand image"
            )
        self._current_visual_input = {
            "image_source": image_source,
            "model_ready_topic": self._composite_image_topic,
            "perception_image_max_skew_sec": (
                self._perception_image_max_skew_sec
            ),
            "sources": sources,
            "preprocessing": (
                "RFDETRSegSmall overlay; CAM4 context crop inspected independently"
                if using_segmented_field
                else "raw FLIR fallback; CAM4 context crop inspected independently"
            ),
            "cam4_image_forwarded_to_vlm": cam4_forwarded,
            "cam4_alignment_skew_sec": (
                round(cam4_skew_sec, 9)
                if cam4_skew_sec is not None
                else None
            ),
            "detector_advisory": perception_enabled,
            "input_error": self._current_image_input_error,
        }
        return images, image_source, model_image

    def _publish_model_ready_image(self, model_image: ModelImage | None) -> None:
        if model_image is None:
            return
        msg = CompressedImage()
        msg.header.stamp.sec = int(model_image.stamp_sec)
        msg.header.stamp.nanosec = int(model_image.stamp_nanosec)
        msg.header.frame_id = (
            f"{model_image.frame_id}|vlm_model_ready"
            if model_image.frame_id
            else "vlm_model_ready"
        )
        msg.format = (
            "jpeg"
            if model_image.mime_type == "image/jpeg"
            else "png"
        )
        msg.data = model_image.data
        self._composite_image_pub.publish(msg)

    def _oracle_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        assert self._world is not None
        stage = self._oracle_stage_for_tick(self._oracle_tick)
        if stage is None:
            phases = [
                [
                    self._world.filtered_phase,
                    round(float(self._world.phase_confidence or 0.9), 3),
                ]
            ]
            observations = []
            for instrument in self._world.instrument_states:
                include = (
                    instrument.instrument_id in context_dict["exp"]
                    or instrument.instrument_id in context_dict["pending"]
                    or instrument.instrument_id in {
                        self._world.right_hand_tool,
                        self._world.left_hand_tool,
                        self._world.prepositioned_tool,
                        self._world.surgeon_request_tool,
                        self._world.explicit_request_tool,
                    }
                    or instrument.location_id != instrument.home_location_id
                    or instrument.location_type != instrument.home_location_type
                )
                if not include:
                    continue
                observations.append(
                    [
                        instrument.instrument_id,
                        instrument.location_id,
                        instrument.location_type,
                        0.97,
                    ]
                )
            gesture = ["", "", "", 0.0]
            uncertainty = 0.34 if self._world.phase_uncertain else 0.08
            summary = (
                f"phase={self._world.filtered_phase}; "
                f"request={self._world.surgeon_request_tool or self._world.explicit_request_tool or 'none'}; "
                f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
            )
            return {
                "v": "1",
                "ph": phases,
                "to": observations,
                "sg": gesture,
                "u": uncertainty,
                "sum": summary,
            }

        phases = [
            [hypothesis.phase_id, round(float(hypothesis.confidence), 3)]
            for hypothesis in stage.phase_hypotheses
        ] or [[self._world.filtered_phase, round(float(self._world.phase_confidence or 0.9), 3)]]
        observations = [
            [
                observation.instrument_id,
                observation.location_id,
                observation.location_type,
                round(float(observation.confidence), 3),
            ]
            for observation in stage.observations
            if observation.visible
        ]
        gesture = ["", "", "", 0.0]
        if stage.surgeon_gesture is not None:
            gesture = [
                stage.surgeon_gesture.event_type,
                stage.surgeon_gesture.requested_tool,
                stage.surgeon_gesture.hand_pose,
                round(float(stage.surgeon_gesture.confidence), 3),
            ]
        uncertainty = float(stage.uncertainty)
        current_request = self._world.surgeon_request_tool or self._world.explicit_request_tool or "none"
        summary = (
            f"oracle_stage={stage.name}; "
            f"phase={phases[0][0] if phases else self._world.filtered_phase}; "
            f"request={current_request}; "
            f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
        )
        return {
            "v": "1",
            "ph": phases,
            "to": observations,
            "sg": gesture,
            "u": uncertainty,
            "sum": summary,
        }

    def _actor_log_fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        digital_twin = context_dict.get("digital_twin", {}) if isinstance(context_dict, dict) else {}
        digital_twin_tools = (
            digital_twin.get("tools", [])
            if isinstance(digital_twin, dict)
            else []
        )
        mayo = [
            str(row.get("id", ""))
            for row in digital_twin_tools
            if isinstance(row, dict)
            and (
                str(row.get("lt", "")) == "mayo"
                or str(row.get("lc", "")) in {"mayo_reuse", "mayo_recovery"}
            )
            and str(row.get("id", ""))
        ]
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])
        observed_signals = evidence_window.get("observed_signals", []) if isinstance(evidence_window, dict) else []
        latest_signal = observed_signals[-1] if isinstance(observed_signals, list) and observed_signals else {}
        has_request_signal = isinstance(latest_signal, dict) and str(latest_signal.get("type", "")) in {"request_tool", "voice_request"}
        intent_type = "handover" if requested_tool and has_request_signal else "none"
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        phase_rows = list(candidates.get("phase", [])) if isinstance(candidates, dict) else []
        tool_rows = list(candidates.get("tool", [])) if isinstance(candidates, dict) else []
        if not phase_rows:
            phase_rows = [[self._spec.default_phase_id, 0.35]]
        if requested_tool and not any(row[0] == requested_tool for row in tool_rows if isinstance(row, list) and row):
            tool_rows = [[requested_tool, 0.72], *tool_rows[:3]]
        if not tool_rows:
            tool_rows = [["", 0.0]]
        immediate_reuse_tool = ""
        for row in tool_rows:
            if isinstance(row, list) and row and str(row[0]):
                immediate_reuse_tool = str(row[0])
                break
        mayo_entries: list[list[Any]] = []
        recover_candidates: list[tuple[str, float]] = []
        for tool_id in (mayo if isinstance(mayo, list) else []):
            tool_id = str(tool_id)
            if not tool_id:
                continue
            if tool_id == requested_tool or tool_id == immediate_reuse_tool:
                mayo_entries.append([tool_id, "reuse", 0.62])
            else:
                confidence = 0.62 if requested_tool else 0.56
                mayo_entries.append([tool_id, "recover", confidence])
                recover_candidates.append((tool_id, confidence))
        mayo_retrieve = ["", 0.0]
        if recover_candidates:
            mayo_retrieve = [recover_candidates[0][0], recover_candidates[0][1]]
        return {
            "v": "4",
            "phase": phase_rows[:4],
            "tool": tool_rows[:4],
            "intent": [intent_type, requested_tool if intent_type != "none" else "", 0.7 if intent_type != "none" else 0.0],
            "mayo": mayo_entries,
            "mayo_retrieve": mayo_retrieve,
            "u": 0.55,
            "sum": "actor-log fallback",
            "bed_robot_arm_group": self._fallback_bed_robot_arm_group_proposal(),
        }

    def _fallback_bed_robot_arm_group_proposal(self) -> dict[str, Any] | None:
        """Build a safe oracle fallback only when speech states direction.

        A missing spoken direction cannot be recovered from the fallback path,
        because that path has no trustworthy visual reasoning.  Returning null
        turns into an explicit invalid proposal and therefore no group action.
        """
        request = self._latest_bed_robot_arm_group_request
        if request is None or not infer_retraction_direction(request.voice_text):
            return None
        try:
            normalized = normalize_retraction_request(request.voice_text)
        except BedRobotArmGroupNormalizationError:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": "retraction",
            "operation": "retraction",
            "direction": normalized.direction,
            "distance_mm": float(normalized.distance_mm),
            "distance_origin": normalized.distance_origin,
            "raw_distance_text": normalized.raw_distance_text,
            "end_effector_profile": str(request.end_effector_profile),
            "rationale": "direction and distance normalized from explicit speech",
            "confidence": 0.76,
        }

    def _fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        if self._context_mode == "actor_log":
            return self._actor_log_fallback_payload(context_dict)
        return self._oracle_payload(context_dict)

    def _candidate_rows(self, context_dict: dict[str, Any], key: str) -> list[list[Any]]:
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        rows = candidates.get(key, []) if isinstance(candidates, dict) else []
        cleaned: list[list[Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            cleaned.append([item_id, max(0.0, min(1.0, confidence))])
        return cleaned

    def _merge_ranked_rows(
        self,
        primary: list[list[Any]],
        fallback: list[list[Any]],
        *,
        limit: int,
    ) -> list[list[Any]]:
        def confidence(row: list[Any]) -> float:
            try:
                return float(row[1])
            except (IndexError, TypeError, ValueError):
                return -1.0

        merged: list[list[Any]] = []
        seen: set[str] = set()
        # Model rows remain authoritative over procedure candidates, but an
        # OpenAI-compatible backend may return internally inconsistent array
        # order. Normalize each ranked set by its own confidence first.
        ranked_primary = sorted(primary, key=confidence, reverse=True)
        ranked_fallback = sorted(fallback, key=confidence, reverse=True)
        for row in [*ranked_primary, *ranked_fallback]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id or item_id in seen:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            seen.add(item_id)
            merged.append([item_id, round(max(0.0, min(1.0, confidence)), 3)])
            if len(merged) >= limit:
                break
        return merged

    def _normal_phase_rows(self, rows: list[list[Any]], *, fallback_phase: str = "") -> list[list[Any]]:
        normal_rows = [
            row
            for row in rows
            if isinstance(row, list)
            and row
            and not self._spec.is_interrupt_phase(str(row[0]))
        ]
        if normal_rows:
            return normal_rows
        if fallback_phase and not self._spec.is_interrupt_phase(fallback_phase):
            return [[fallback_phase, 0.35]]
        return []

    def _stabilize_actor_log_payload(self, payload: dict[str, Any], context_dict: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("v", "")) not in {"3", "4"}:
            return payload
        stabilized = dict(payload)
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        visual = evidence_window.get("visual", {}) if isinstance(evidence_window, dict) else {}
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        observed_signals = evidence_window.get("observed_signals", []) if isinstance(evidence_window, dict) else []
        latest_signal = observed_signals[-1] if isinstance(observed_signals, list) and observed_signals else {}
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])
        has_request_signal = isinstance(latest_signal, dict) and str(latest_signal.get("type", "")) in {
            "request_tool",
            "voice_request",
        }

        phase_candidates = self._candidate_rows(context_dict, "phase")
        candidate_evidence = context_dict.get("candidates", {}).get("evidence", {}) if isinstance(context_dict, dict) else {}
        fallback_phase = str(candidate_evidence.get("current_phase", "") if isinstance(candidate_evidence, dict) else "")
        model_phase_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("phase", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        phase_search_mode = str(
            context_dict.get(
                "phase_search_mode",
                candidate_evidence.get("phase_search_mode", ""),
            )
        )
        tool_sequence_open_set_anchor_allowed = bool(
            candidate_evidence.get(
                "tool_sequence_open_set_anchor_allowed",
                True,
            )
        )
        public_sequence_anchor = ""
        if (
            phase_search_mode == "open_set"
            and tool_sequence_open_set_anchor_allowed
            and phase_candidates
        ):
            top_prior_phase = str(phase_candidates[0][0])
            alignment = candidate_evidence.get("sequence_alignment", {})
            top_alignment = (
                alignment.get(top_prior_phase, {})
                if isinstance(alignment, dict)
                else {}
            )
            try:
                matches = int(top_alignment.get("matches", 0))
                adjacent = int(top_alignment.get("adjacent", 0))
            except (TypeError, ValueError):
                matches = 0
                adjacent = 0
            if matches >= 2 and (adjacent >= 1 or matches >= 3):
                public_sequence_anchor = top_prior_phase
                anchor_confidence = min(
                    0.99,
                    0.80 + matches * 0.05 + adjacent * 0.03,
                )
                model_phase_rows = [
                    [top_prior_phase, anchor_confidence],
                    *[
                        row
                        for row in model_phase_rows
                        if str(row[0]) != top_prior_phase
                    ],
                ]
        if phase_search_mode == "temporal_prior":
            valid_temporal_phases = {
                str(candidate_evidence.get("current_phase", "")),
                *(
                    str(phase_id)
                    for phase_id in candidate_evidence.get(
                        "allowed_next",
                        [],
                    )
                ),
            }
            valid_temporal_phases.discard("")
            if valid_temporal_phases:
                model_phase_rows = [
                    row
                    for row in model_phase_rows
                    if str(row[0]) in valid_temporal_phases
                ]
        if (
            phase_search_mode == "open_set"
            and not tool_sequence_open_set_anchor_allowed
        ):
            # This procedure defines Phase from visible functional state.
            # Tool-order candidates may inform interpretation and next-tool
            # prediction, but they cannot bootstrap a detailed Phase. Trust
            # only the current image-backed model rows here.
            stabilized["phase"] = self._normal_phase_rows(
                model_phase_rows,
            )[:4]
        elif phase_search_mode == "open_set" and not public_sequence_anchor:
            # The default runtime phase is only a placeholder during open-set
            # startup. A close-up image can resemble several later phases, so
            # do not irreversibly bootstrap the reducer until public tool
            # sequence evidence supplies a stable procedure-local anchor.
            stabilized["phase"] = []
        elif phase_candidates:
            stabilized["phase"] = self._normal_phase_rows(
                self._merge_ranked_rows(model_phase_rows, phase_candidates, limit=4),
                fallback_phase=fallback_phase,
            )[:4]
        elif model_phase_rows:
            stabilized["phase"] = self._normal_phase_rows(
                self._merge_ranked_rows(model_phase_rows, [], limit=4),
                fallback_phase=fallback_phase,
            )[:4]

        tool_candidates = self._candidate_rows(context_dict, "tool")
        model_tool_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("tool", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        public_tool_anchor = ""
        if requested_tool:
            public_tool_anchor = requested_tool
            model_tool_rows = [
                [requested_tool, 1.0],
                *[
                    row
                    for row in model_tool_rows
                    if str(row[0]) != requested_tool
                ],
            ]
        if tool_candidates:
            stabilized["tool"] = self._merge_ranked_rows(
                model_tool_rows,
                tool_candidates,
                limit=4,
            )
        elif model_tool_rows:
            stabilized["tool"] = self._merge_ranked_rows(
                model_tool_rows,
                [],
                limit=4,
            )

        visual_input = (
            context_dict.get("visual_input", {})
            if isinstance(context_dict, dict)
            else {}
        )
        direct_cam4 = bool(
            isinstance(visual_input, dict)
            and visual_input.get("cam4_image_forwarded_to_vlm")
        )
        gesture = stabilized.get("gesture", ["", "", "", 0.0])
        if not isinstance(gesture, list) or len(gesture) != 4:
            gesture = ["", "", "", 0.0]
        gesture_type = self._canonical_intent_type(gesture[0])
        gesture_tool = self._canonical_tool_id(gesture[1])
        gesture_pose = str(gesture[2]).strip().lower()
        try:
            gesture_confidence = float(gesture[3])
        except (TypeError, ValueError):
            gesture_confidence = 0.0
        if (
            direct_cam4
            and gesture_type in {"handover", "request_tool"}
            and gesture_pose == "open_receive"
            and gesture_confidence >= CAM4_VISUAL_GESTURE_MIN_CONFIDENCE
        ):
            stabilized["gesture"] = [
                "request_tool",
                gesture_tool,
                "open_receive",
                round(max(0.0, min(1.0, gesture_confidence)), 3),
            ]
        else:
            stabilized["gesture"] = ["", "", "", 0.0]

        intent = stabilized.get("intent", ["none", "", 0.0])
        if not isinstance(intent, list) or len(intent) < 3:
            intent = ["none", "", 0.0]
        if requested_tool:
            intent = ["handover", requested_tool, max(0.72, min(1.0, float(intent[2]) if len(intent) > 2 else 0.0))]
        elif has_request_signal:
            signal_tool = self._canonical_tool_id(
                latest_signal.get("tool", "")
                if isinstance(latest_signal, dict)
                else ""
            )
            if signal_tool:
                intent = [
                    "handover",
                    signal_tool,
                    max(
                        0.72,
                        min(
                            1.0,
                            float(intent[2]) if len(intent) > 2 else 0.0,
                        ),
                    ),
                ]
            else:
                intent = ["none", "", 0.0]
        elif (
            stabilized["gesture"][0] == "request_tool"
            and stabilized["gesture"][1]
        ):
            intent = [
                "handover",
                stabilized["gesture"][1],
                stabilized["gesture"][3],
            ]
        else:
            intent = ["none", "", 0.0]
        stabilized["intent"] = [str(intent[0]), str(intent[1]), round(max(0.0, min(1.0, float(intent[2]))), 3)]
        self._corroborate_mayo_with_cam4_semantics(
            stabilized,
            context_dict,
        )
        self._suppress_non_mayo_recovery_candidates(stabilized, context_dict)

        stabilized["sum"] = normalize_clinical_analysis(
            stabilized.get("sum", "")
        )
        return stabilized

    def _corroborate_mayo_with_cam4_semantics(
        self,
        payload: dict[str, Any],
        context_dict: dict[str, Any],
    ) -> None:
        """Use CAM4 pixels as evidence and detector rows as optional support."""

        perception = (
            context_dict.get("observable_perception", {})
            if isinstance(context_dict, dict)
            else {}
        )
        if not isinstance(perception, dict):
            perception = {}
        alignment = perception.get("alignment", {})
        aligned = (
            perception.get("schema") == "taskplanner.cam4_semantics.v1"
            and perception.get("source") == "cam4_rfdetr_small"
            and isinstance(alignment, dict)
            and alignment.get("status") == "aligned"
        )
        visual_input = (
            context_dict.get("visual_input", {})
            if isinstance(context_dict, dict)
            else {}
        )
        direct_cam4 = bool(
            isinstance(visual_input, dict)
            and visual_input.get("cam4_image_forwarded_to_vlm")
        )
        if not aligned and direct_cam4:
            raw_rows: list[list[Any]] = []
            for row in payload.get("mayo", []):
                if not isinstance(row, list) or len(row) < 3:
                    continue
                tool_id = self._canonical_tool_id(row[0])
                if not tool_id or str(row[1]) not in {"recover", "reuse"}:
                    continue
                try:
                    confidence = float(row[2])
                except (TypeError, ValueError):
                    continue
                raw_rows.append(
                    [
                        tool_id,
                        str(row[1]),
                        round(max(0.0, min(1.0, confidence)), 4),
                    ]
                )
            payload["mayo"] = raw_rows
            raw_recovery_tools = {
                str(row[0])
                for row in raw_rows
                if str(row[1]) == "recover"
            }
            retrieve = payload.get("mayo_retrieve", ["", 0.0])
            retrieve_tool = (
                self._canonical_tool_id(retrieve[0])
                if isinstance(retrieve, list) and len(retrieve) >= 2
                else ""
            )
            if retrieve_tool not in raw_recovery_tools:
                payload["mayo_retrieve"] = ["", 0.0]
            else:
                payload["mayo_retrieve"] = [
                    retrieve_tool,
                    retrieve[1],
                ]
            return
        if not aligned:
            payload["mayo"] = []
            payload["mayo_retrieve"] = ["", 0.0]
            return

        detected: dict[str, dict[str, Any]] = {}
        rows = perception.get("tools", []) if aligned else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            try:
                count = int(row.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if tool_id and count > 0:
                try:
                    confidence = float(row.get("max_confidence", 0.0))
                    stable_samples = int(
                        row.get("stable_sample_count", 0)
                    )
                    stable_duration_sec = float(
                        row.get("stable_duration_sec", 0.0)
                    )
                except (TypeError, ValueError):
                    confidence = 0.0
                    stable_samples = 0
                    stable_duration_sec = 0.0
                previous = detected.get(tool_id, {})
                detected[tool_id] = {
                    "count": min(
                        16,
                        int(previous.get("count", 0))
                        + min(count, 16),
                    ),
                    "confidence": max(
                        float(previous.get("confidence", 0.0)),
                        min(1.0, max(0.0, confidence)),
                    ),
                    "stable": bool(
                        stable_samples >= CAM4_MAYO_MIN_STABLE_SAMPLES
                        and stable_duration_sec
                        >= CAM4_MAYO_MIN_STABLE_DURATION_SEC
                    ),
                }

        request = perception.get("tool_request", {}) if aligned else {}
        active_hand_request = bool(
            isinstance(request, dict)
            and request.get("state") == "request"
        )
        digital_twin = (
            context_dict.get("digital_twin", {})
            if isinstance(context_dict, dict)
            else {}
        )
        field_tools = {
            str(row.get("id", ""))
            for row in (
                digital_twin.get("tools", [])
                if isinstance(digital_twin, dict)
                else []
            )
            if isinstance(row, dict)
            and (
                str(row.get("lt", ""))
                in {"surgical_field", "bed_fixed_tool"}
                or str(row.get("lc", "")) == "surgeon_owned"
            )
        }

        remaining = {
            tool_id: int(evidence["count"])
            for tool_id, evidence in detected.items()
        }
        corroborated_rows: list[list[Any]] = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            if not tool_id or remaining.get(tool_id, 0) <= 0:
                continue
            evidence = detected[tool_id]
            if (
                tool_id in field_tools
                and (
                    active_hand_request
                    or not bool(evidence["stable"])
                    or float(evidence["confidence"])
                    < CAM4_MAYO_MIN_CONFIDENCE
                )
            ):
                continue
            corroborated_rows.append(
                [
                    tool_id,
                    row[1],
                    min(float(row[2]), float(evidence["confidence"])),
                ]
            )
            remaining[tool_id] -= 1

        # CAM4 is a fixed Mayo view. Stable detector presence is sufficient to
        # update location, but not to order recovery. Missing model rows are
        # therefore inserted as fail-closed reuse observations.
        for tool_id, count in remaining.items():
            evidence = detected[tool_id]
            if (
                count <= 0
                or active_hand_request
                or not bool(evidence["stable"])
                or float(evidence["confidence"])
                < CAM4_MAYO_MIN_CONFIDENCE
            ):
                continue
            corroborated_rows.append(
                [
                    tool_id,
                    "reuse",
                    round(float(evidence["confidence"]), 4),
                ]
            )
        payload["mayo"] = corroborated_rows

        corroborated_recovery_tools = {
            str(row[0])
            for row in corroborated_rows
            if len(row) >= 2 and str(row[1]) == "recover"
        }
        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        retrieve_tool = (
            self._canonical_tool_id(retrieve[0])
            if isinstance(retrieve, list) and len(retrieve) >= 2
            else ""
        )
        if retrieve_tool not in corroborated_recovery_tools:
            payload["mayo_retrieve"] = ["", 0.0]
        else:
            payload["mayo_retrieve"] = [
                retrieve_tool,
                retrieve[1],
            ]

    def _suppress_non_mayo_recovery_candidates(
        self,
        payload: dict[str, Any],
        context_dict: dict[str, Any],
    ) -> None:
        candidates = (
            context_dict.get("candidates", {})
            if isinstance(context_dict, dict)
            else {}
        )
        candidate_evidence = (
            candidates.get("evidence", {})
            if isinstance(candidates, dict)
            else {}
        )
        current_phase = self._canonical_phase_id(
            candidate_evidence.get("current_phase", "")
            if isinstance(candidate_evidence, dict)
            else ""
        )
        if not current_phase:
            current_phase = self._authoritative_runtime_phase()
        remaining_procedure_tools = set(
            self._spec.get_remaining_expected_instruments(
                current_phase,
                include_current=True,
            )
        )

        # Recovery is irreversible from the planner's perspective. A visual
        # model may correctly see a tool on Mayo while guessing the wrong
        # lifecycle. Keep the location observation, but fail closed to reuse
        # whenever the authored procedure still references that tool.
        protected_mayo_rows: list[list[Any]] = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            if not tool_id:
                continue
            semantic = str(row[1])
            if semantic == "recover" and tool_id in remaining_procedure_tools:
                semantic = "reuse"
            protected_mayo_rows.append([tool_id, semantic, row[2]])
        payload["mayo"] = protected_mayo_rows

        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        retrieve_tool = (
            self._canonical_tool_id(retrieve[0])
            if isinstance(retrieve, list) and retrieve
            else ""
        )
        if retrieve_tool in remaining_procedure_tools:
            payload["mayo_retrieve"] = ["", 0.0]

        digital_twin = context_dict.get("digital_twin", {}) if isinstance(context_dict, dict) else {}
        hands = digital_twin.get("hands", {}) if isinstance(digital_twin, dict) else {}
        tools = digital_twin.get("tools", []) if isinstance(digital_twin, dict) else []
        stable_cam4_tools = {
            self._canonical_tool_id(row.get("name", ""))
            for row in (
                context_dict.get("observable_perception", {}).get(
                    "tools",
                    [],
                )
                if isinstance(context_dict, dict)
                and isinstance(
                    context_dict.get("observable_perception", {}),
                    dict,
                )
                else []
            )
            if isinstance(row, dict)
            and int(row.get("stable_sample_count", 0) or 0)
            >= CAM4_MAYO_MIN_STABLE_SAMPLES
            and float(row.get("stable_duration_sec", 0.0) or 0.0)
            >= CAM4_MAYO_MIN_STABLE_DURATION_SEC
            and float(row.get("max_confidence", 0.0) or 0.0)
            >= CAM4_MAYO_MIN_CONFIDENCE
        }
        stable_cam4_tools.discard("")
        visual_input = (
            context_dict.get("visual_input", {})
            if isinstance(context_dict, dict)
            else {}
        )
        if (
            isinstance(visual_input, dict)
            and visual_input.get("cam4_image_forwarded_to_vlm")
        ):
            stable_cam4_tools.update(
                self._canonical_tool_id(row[0])
                for row in payload.get("mayo", [])
                if isinstance(row, list) and row
            )
            stable_cam4_tools.discard("")
        blocked_tools = {
            str(hands.get(key, ""))
            for key in ("rh", "lh", "pre")
            if str(hands.get(key, ""))
        }
        for row in tools if isinstance(tools, list) else []:
            if not isinstance(row, dict):
                continue
            tool_id = str(row.get("id", ""))
            lifecycle = str(row.get("lc", ""))
            location_type = str(row.get("lt", ""))
            if not tool_id:
                continue
            if (
                lifecycle in {"home_rack", "returned_home"}
                and tool_id not in stable_cam4_tools
            ):
                blocked_tools.add(tool_id)
            if lifecycle in {
                "prepositioned_right",
                "recovering_left",
                "cleaning_left",
                "cleaned_left",
                "dropped_floor",
            }:
                blocked_tools.add(tool_id)
            if location_type in {
                "tray_slot",
                "robot_right_hand",
                "robot_left_hand",
                "cleaner_slot",
                "floor_zone",
            }:
                blocked_tools.add(tool_id)
            if (
                location_type in {"surgical_field", "bed_fixed_tool"}
                and tool_id not in stable_cam4_tools
            ):
                blocked_tools.add(tool_id)
        if not blocked_tools:
            return

        mayo_rows = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            if str(row[0]) in blocked_tools:
                continue
            mayo_rows.append(row)
        payload["mayo"] = mayo_rows

        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        if isinstance(retrieve, list) and retrieve and str(retrieve[0]) in blocked_tools:
            payload["mayo_retrieve"] = ["", 0.0]

    def _run_model(
        self,
        context_json: str,
        images: list[tuple[str, bytes, str]],
    ) -> tuple[str, dict[str, Any] | None, float, str, int, str]:
        retries_used = 0
        if self._response_mode == "replay":
            if self._replay_payload is None:
                raise RuntimeError("response_mode=replay requires replay_response_path")
            raw = json.dumps(self._replay_payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = normalize_raw_text(raw)
            return normalized_raw, payload, 0.0, "replay", retries_used, ""
        if self._response_mode == "oracle":
            payload = self._fallback_payload(json.loads(context_json))
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = normalize_raw_text(raw)
            return normalized_raw, payload, 0.0, "oracle", retries_used, ""

        last_error = ""
        transport_failed = False
        started_monotonic = time.monotonic()
        for attempt in range(self._retry_count + 1):
            developer_prompt = self._developer_instruction
            if attempt > 0:
                developer_prompt += " Previous response was invalid. Re-emit schema only."
            try:
                response = self._client.request_json(
                    system_prompt=self._system_prompt,
                    developer_prompt=developer_prompt,
                    user_context_json=context_json,
                    images=images,
                    model_id=self._model_id,
                    temperature=self._temperature,
                    top_p=self._top_p,
                    max_output_tokens=self._max_output_tokens,
                    api_mode=self._api_mode,
                    response_format=self._response_format,
                    json_schema=self._json_schema,
                    reasoning_effort=self._reasoning_effort,
                    generation_seed=self._generation_seed,
                )
                normalized_raw, payload = normalize_raw_text(response.raw_text)
                return normalized_raw, payload, response.latency_sec, response.mode, attempt, ""
            except (requests.RequestException, SchemaValidationError, json.JSONDecodeError, RuntimeError, ValueError) as exc:  # type: ignore[name-defined]
                last_error = str(exc)
                transport_failed = transport_failed or isinstance(
                    exc,
                    requests.RequestException,
                )
                retries_used = attempt + 1
        failure_mode = (
            "inference_transport_failed"
            if transport_failed
            else "inference_response_failed"
        )
        return (
            "",
            None,
            max(0.0, time.monotonic() - started_monotonic),
            failure_mode,
            retries_used,
            last_error or "VLM inference failed without an error message",
        )

    def _cacheable_payload(
        self,
        raw_json: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Remove one-shot group commands from temporal phase/tool memory."""
        if str(payload.get("v", "")) != "4" or payload.get("bed_robot_arm_group") is None:
            return raw_json, payload
        cache_payload = dict(payload)
        cache_payload["bed_robot_arm_group"] = None
        return (
            json.dumps(cache_payload, separators=(",", ":"), sort_keys=True),
            cache_payload,
        )

    def _tick(
        self,
        force: bool = False,
        inference_trigger: str = "",
    ) -> bool:
        trigger = str(inference_trigger).strip()
        if not trigger:
            trigger = (
                INFERENCE_TRIGGER_FORCED
                if force
                else INFERENCE_TRIGGER_PERIODIC_LIVE
            )
        if (
            trigger == INFERENCE_TRIGGER_PERIODIC_LIVE
            and (
                getattr(self, "_response_mode", "live") == "replay"
                or (
                    getattr(self, "_response_mode", "live") == "live"
                    and getattr(
                        self,
                        "_source_time_triggered_live",
                        False,
                    )
                )
            )
        ):
            return False
        admission = self._inference_backpressure.request(trigger)
        if not admission.started:
            return False
        self._run_inference_chain(trigger)
        return True

    def _run_inference_chain(self, initial_trigger: str) -> None:
        """Drain the newest pending frame immediately after each completion."""

        current_trigger: str | None = initial_trigger
        while current_trigger is not None:
            try:
                self._tick_once(
                    force=current_trigger != INFERENCE_TRIGGER_PERIODIC_LIVE,
                    inference_trigger=current_trigger,
                )
            except Exception as exc:  # pragma: no cover - final node boundary
                self._record_inference_failure(
                    trigger=current_trigger,
                    mode="unhandled_exception",
                    error=str(exc),
                    image_source="",
                    latency_sec=0.0,
                    prompt_chars=0,
                    retry_count=0,
                    connected=False,
                )
            finally:
                current_trigger = self._inference_backpressure.complete()

    def _tick_once(
        self,
        force: bool = False,
        inference_trigger: str = INFERENCE_TRIGGER_PERIODIC_LIVE,
    ) -> None:
        if not force and not self._active:
            return
        if self._response_mode == "replay" and not force:
            return
        perception_generation = getattr(
            self,
            "_perception_generation",
            0,
        )
        visual_generation = getattr(
            self,
            "_visual_frame_generation",
            0,
        )
        images, image_source, model_image = self._select_images()
        model_image_stamp_sec: float | None = None
        if model_image is not None:
            model_image_stamp_sec = (
                float(model_image.stamp_sec)
                + float(model_image.stamp_nanosec) / 1_000_000_000.0
            )
        if (
            model_image is not None
            and visual_generation > 0
            and visual_generation
            == getattr(self, "_last_submitted_visual_generation", -1)
        ):
            return
        context_stamp = self.get_clock().now().to_msg()
        if model_image is not None:
            context_stamp.sec = int(model_image.stamp_sec)
            context_stamp.nanosec = int(model_image.stamp_nanosec)
        if self._context_mode == "actor_log":
            context_dict = self._assemble_actor_log_context_dict()
            request_context_json = compact_json(context_dict)
            request_context_msg = self._actor_log_request_context_msg(
                context_dict,
                request_context_json,
                context_stamp,
            )
            context_stamp = request_context_msg.stamp
            self._request_context_pub.publish(request_context_msg)
        else:
            if self._world is None or self._simulation is None:
                return
            request_context, context_dict = self._assemble_context()
            context_dict["visual_input"] = dict(self._current_visual_input)
            request_context.compact_json = compact_json(context_dict)
            request_context.stamp = context_stamp
            request_context_json = request_context.compact_json
            context_stamp = request_context.stamp
        prompt_chars = len(self._system_prompt) + len(self._developer_instruction) + len(request_context_json)
        raw_json = ""
        payload: dict[str, Any] | None = None
        latency_sec = 0.0
        mode = self._response_mode
        parse_retry_count = 0
        last_error = ""
        healthy = True
        connected = True
        if self._current_image_input_error:
            self._publish_health(
                image_source=image_source,
                latency_sec=0.0,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=0,
                last_error=self._current_image_input_error,
                mode="missing_visual_input",
                healthy=False,
                connected=True,
            )
            return
        if (
            self._response_mode == "live"
            and self._require_field_image
            and not is_model_ready_visual_source(image_source)
        ):
            self._publish_health(
                image_source=image_source,
                latency_sec=0.0,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=0,
                last_error="no fresh segmented or raw FLIR image",
                mode="no_fresh_image",
                healthy=False,
                connected=True,
            )
            return
        if model_image_stamp_sec is not None:
            self._last_submitted_live_image_stamp_sec = model_image_stamp_sec
        if visual_generation > 0:
            self._last_submitted_visual_generation = visual_generation
        self._publish_model_ready_image(model_image)
        try:
            raw_json, payload, latency_sec, mode, parse_retry_count, last_error = self._run_model(
                request_context_json,
                images,
            )
        except Exception as exc:  # pragma: no cover - safety net
            last_error = str(exc)
            healthy = False
            connected = False
            payload = None
            raw_json = ""
            mode = "unhandled_model_exception"
        if mode == "inference_transport_failed":
            connected = False
        if (
            self._require_field_image
            and perception_generation
            != getattr(self, "_perception_generation", 0)
        ):
            self._publish_health(
                image_source="",
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=parse_retry_count,
                last_error="visual assistance mode changed during inference",
                mode="visual_contract_changed",
                healthy=False,
                connected=connected,
            )
            return
        if self._response_mode == "live" and (
            payload is None
            or not healthy
            or last_error
            or mode in {"last_good", "oracle_fallback"}
        ):
            self._record_inference_failure(
                trigger=inference_trigger,
                mode=mode,
                error=last_error or f"unsafe VLM fallback mode: {mode}",
                image_source=image_source,
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                retry_count=parse_retry_count,
                connected=connected,
            )
            return
        if payload is None:
            return
        payload = self._canonicalize_payload_ids(payload)
        model_raw_json = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        published_mode = (
            f"{mode}:{inference_trigger}"
            if self._response_mode == "live"
            else mode
        )
        if mode not in {"last_good", "oracle_fallback"}:
            self._publish_model_raw_result(
                payload,
                model_raw_json,
                observation_stamp=context_stamp,
                mode=published_mode,
            )
        raw_json = model_raw_json
        if self._context_mode == "actor_log":
            payload = self._stabilize_actor_log_payload(payload, context_dict)
            payload["mayo"], payload["mayo_retrieve"] = normalize_mayo_semantics(
                payload.get("mayo", []),
                payload.get("mayo_retrieve", ["", 0.0]),
            )
            raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            predicted_phase = self._primary_payload_phase(payload)
            if predicted_phase and predicted_phase != self._last_vlm_phase:
                self._last_vlm_phase = predicted_phase
        if mode not in {"last_good", "oracle_fallback"}:
            self._last_good_raw, self._last_good_payload = self._cacheable_payload(
                raw_json,
                payload,
            )

        self._publish_vlm_outputs(
            payload,
            raw_json,
            observation_stamp=context_stamp,
            image_source=image_source,
            latency_sec=latency_sec,
            prompt_chars=prompt_chars,
            parse_retry_count=parse_retry_count,
            last_error=last_error,
            mode=published_mode,
            healthy=healthy,
            connected=connected,
        )
        if model_image_stamp_sec is not None:
            self._last_periodic_live_image_stamp_sec = model_image_stamp_sec
        if self._context_mode == "actor_log" and mode not in {
            "last_good",
            "oracle_fallback",
        }:
            self._phase_bootstrap_observation_count += 1
        if self._response_mode == "oracle":
            self._oracle_tick += 1

    def _validate_bed_robot_arm_group_proposal(
        self,
        proposal: dict[str, Any],
        request: BedRobotArmGroupRequest,
    ) -> str:
        if str(proposal.get("request_id", "")) != str(request.request_id):
            return "request_id does not match the pending retraction request"
        if str(proposal.get("group_id", "")) != "retraction":
            return "VLM proposal must target the retraction group"
        if str(proposal.get("operation", "")) != "retraction":
            return "VLM proposal operation must be retraction"

        requested_profile = str(request.end_effector_profile).strip()
        proposed_profile = str(proposal.get("end_effector_profile", "")).strip()
        if requested_profile and proposed_profile != requested_profile:
            return "end_effector_profile does not match the pending request"

        distance_origin = str(proposal.get("distance_origin", ""))
        raw_distance_text = str(proposal.get("raw_distance_text", "")).strip()
        voice_text = str(request.voice_text).strip()
        if distance_origin == "defaulted" and raw_distance_text:
            return "defaulted distance must have empty raw_distance_text"
        if raw_distance_text:
            compact_raw = "".join(raw_distance_text.split())
            compact_voice = "".join(voice_text.split())
            if compact_raw not in compact_voice:
                return "raw_distance_text is not present in the source request"

        try:
            distance_mm = float(proposal.get("distance_mm", 0.0))
            normalized = normalize_retraction_request(
                voice_text,
                vlm_direction=str(proposal.get("direction", "")),
                qualitative_distance_mm=(
                    distance_mm if distance_origin == "qualitative_inferred" else None
                ),
            )
        except (BedRobotArmGroupNormalizationError, TypeError, ValueError) as exc:
            return f"deterministic request normalization failed: {exc}"

        if normalized.direction != str(proposal.get("direction", "")):
            return (
                "direction contradicts the source request: "
                f"expected {normalized.direction}"
            )
        if normalized.distance_origin != distance_origin:
            return (
                "distance_origin contradicts the source request: "
                f"expected {normalized.distance_origin}"
            )
        if abs(float(normalized.distance_mm) - distance_mm) > 1e-6:
            return (
                "distance_mm contradicts deterministic source normalization: "
                f"expected {normalized.distance_mm:g} mm"
            )
        return ""

    def _publish_bed_robot_arm_group_proposal(
        self,
        payload: dict[str, Any],
        raw_json: str,
        stamp,
    ) -> None:
        if str(payload.get("v", "")) != "4":
            return
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return
        request_id = str(request.request_id)
        if request_id == self._last_bed_robot_arm_group_proposal_request_id:
            return

        group_payload = payload.get("bed_robot_arm_group")
        message = BedRobotArmGroupActionProposal()
        message.stamp = stamp
        message.schema_version = "4"
        message.raw_json = raw_json
        command = BedRobotArmGroupCommand()
        command.stamp = stamp
        command.request_id = request_id
        command.command_id = f"vlm-{request_id}"
        command.group_id = "retraction"
        command.operation = "retraction"
        command.end_effector_profile = str(request.end_effector_profile)

        if not isinstance(group_payload, dict):
            message.valid = False
            message.validation_error = (
                "VLM declined retraction: insufficient direction evidence"
            )
        else:
            command.request_id = str(group_payload.get("request_id", request_id))
            command.command_id = f"vlm-{command.request_id}"
            command.group_id = str(group_payload.get("group_id", ""))
            command.operation = str(group_payload.get("operation", ""))
            command.direction = str(group_payload.get("direction", ""))
            command.distance_mm = float(group_payload.get("distance_mm", 0.0))
            command.distance_origin = str(group_payload.get("distance_origin", ""))
            command.raw_distance_text = str(group_payload.get("raw_distance_text", ""))
            command.end_effector_profile = str(
                group_payload.get("end_effector_profile", "")
            )
            command.rationale = str(group_payload.get("rationale", ""))
            command.confidence = float(group_payload.get("confidence", 0.0))
            message.validation_error = self._validate_bed_robot_arm_group_proposal(
                group_payload,
                request,
            )
            message.valid = not bool(message.validation_error)

        message.command = command
        self._bed_robot_arm_group_proposal_pub.publish(message)
        self._last_bed_robot_arm_group_proposal_request_id = request_id

    def _publish_health(
        self,
        *,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        output_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
    ) -> None:
        health = VLMHealth()
        health.stamp = self.get_clock().now().to_msg()
        health.connected = bool(connected)
        health.healthy = bool(healthy and not last_error)
        health.model_id = self._model_id
        health.image_source = image_source
        health.latency_sec = float(latency_sec)
        health.prompt_chars = int(prompt_chars)
        health.output_chars = int(output_chars)
        health.parse_retry_count = int(parse_retry_count)
        health.last_error = last_error
        health.last_mode = mode
        self._health_pub.publish(health)

    def _record_inference_failure(
        self,
        *,
        trigger: str,
        mode: str,
        error: str,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        retry_count: int,
        connected: bool,
    ) -> None:
        self._inference_failure_count += 1
        failure = InferenceFailure(
            sequence=self._inference_failure_count,
            trigger=str(trigger),
            mode=str(mode),
            error=str(error),
            image_source=str(image_source),
            latency_sec=max(0.0, float(latency_sec)),
            retry_count=max(0, int(retry_count)),
            recorded_monotonic=time.monotonic(),
        )
        self._inference_failures.append(failure)
        self.get_logger().error(
            "VLM inference failed "
            f"[{failure.trigger}/{failure.mode}]: {failure.error}"
        )
        self._publish_health(
            image_source=failure.image_source,
            latency_sec=failure.latency_sec,
            prompt_chars=max(0, int(prompt_chars)),
            output_chars=0,
            parse_retry_count=failure.retry_count,
            last_error=failure.error,
            mode=(
                f"inference_failed:{failure.trigger}:{failure.mode}"
            ),
            healthy=False,
            connected=connected,
        )

    def _publish_model_raw_result(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        observation_stamp,
        mode: str,
    ) -> None:
        """Publish the parsed model response before runtime stabilization.

        This audit-only topic must not feed the reducer or BT. IDs have passed
        canonical ontology normalization, but no speech anchoring, temporal
        prior merge, Mayo corroboration, or intent suppression has run.
        """
        publisher = getattr(self, "_model_raw_result_pub", None)
        if publisher is None:
            return

        schema_version = str(payload.get("v", "1"))
        phase_rows: list[list[Any]]
        observed_rows: list[list[Any]]
        gesture_row: list[Any]
        if schema_version in {"3", "4"}:
            phase_rows = [
                [str(item[0]), float(item[1])]
                for item in payload.get("phase", [])
                if isinstance(item, list)
                and len(item) == 2
                and str(item[0])
            ]
            observed_rows = [
                [
                    str(item[0]),
                    "mayo_stand",
                    "mayo_stand",
                    float(item[2]),
                ]
                for item in payload.get("mayo", [])
                if isinstance(item, list)
                and len(item) == 3
                and str(item[0])
            ]
            if schema_version == "4":
                gesture = payload.get("gesture", ["", "", "", 0.0])
                gesture_row = (
                    list(gesture)
                    if isinstance(gesture, list) and len(gesture) == 4
                    else ["", "", "", 0.0]
                )
            else:
                gesture_row = ["", "", "", 0.0]
        elif schema_version == "2":
            phase = payload.get("phase", ["", 0.0])
            phase_rows = (
                [[str(phase[0]), float(phase[1])]]
                if isinstance(phase, list)
                and len(phase) == 2
                and str(phase[0])
                else []
            )
            observed_rows = [
                [
                    str(item[0]),
                    "mayo_stand",
                    "mayo_stand",
                    float(item[2]),
                ]
                for item in payload.get("mayo", [])
                if isinstance(item, list)
                and len(item) == 3
                and str(item[0])
            ]
            intent = payload.get("intent", ["none", "", 0.0])
            gesture_row = (
                [str(intent[0]), str(intent[1]), "", float(intent[2])]
                if isinstance(intent, list) and len(intent) >= 3
                else ["none", "", "", 0.0]
            )
        else:
            phase_rows = [
                list(item)
                for item in payload.get("ph", [])
                if isinstance(item, list) and len(item) == 2
            ]
            observed_rows = [
                list(item)
                for item in payload.get("to", [])
                if isinstance(item, list) and len(item) == 4
            ]
            gesture = payload.get("sg", ["", "", "", 0.0])
            gesture_row = (
                list(gesture)
                if isinstance(gesture, list) and len(gesture) == 4
                else ["", "", "", 0.0]
            )

        result = VLMResult()
        result.stamp = observation_stamp
        procedure_id = str(
            getattr(getattr(self, "_spec", None), "procedure_id", "unknown")
        )
        result.source = f"real_vlm_model_raw:{procedure_id}:{mode}"
        result.schema_version = schema_version
        result.raw_json = raw_json
        result.summary = str(payload.get("sum", ""))
        result.phase_ids = [str(item[0]) for item in phase_rows]
        result.phase_confidences = [
            float(item[1]) for item in phase_rows
        ]
        result.observed_tool_ids = [
            str(item[0]) for item in observed_rows
        ]
        result.observed_location_ids = [
            str(item[1]) for item in observed_rows
        ]
        result.observed_location_types = [
            str(item[2]) for item in observed_rows
        ]
        result.observed_confidences = [
            float(item[3]) for item in observed_rows
        ]
        result.gesture_event_type = str(gesture_row[0])
        result.gesture_requested_tool = str(gesture_row[1])
        result.gesture_hand_pose = str(gesture_row[2])
        result.gesture_confidence = float(gesture_row[3])
        result.uncertainty = float(payload.get("u", 0.0))
        publisher.publish(result)

    def _publish_vlm_outputs(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        observation_stamp,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
    ) -> None:
        stamp = observation_stamp
        schema_version = str(payload.get("v", "1"))
        if schema_version in {"3", "4"}:
            phase_rows = [
                [str(item[0]), float(item[1])]
                for item in payload.get("phase", [])
                if isinstance(item, list) and len(item) == 2 and str(item[0])
            ]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if isinstance(item, list) and len(item) == 3 and str(item[0])
            ]
            if schema_version == "4":
                gesture = payload.get("gesture", ["", "", "", 0.0])
                gesture_row = (
                    list(gesture)
                    if isinstance(gesture, list) and len(gesture) == 4
                    else ["", "", "", 0.0]
                )
            else:
                gesture_row = ["", "", "", 0.0]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        elif schema_version == "2":
            phase_rows = [[str(payload["phase"][0]), float(payload["phase"][1])]]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if str(item[0])
            ]
            intent = payload.get("intent", ["", "", 0.0])
            intent_type = str(intent[0])
            gesture_type = "request_tool" if intent_type in {"handover", "request_tool"} else intent_type
            hand_pose = "open_receive" if gesture_type == "request_tool" else ""
            gesture_row = [gesture_type, str(intent[1]), hand_pose, float(intent[2])]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        else:
            phase_rows = list(payload["ph"])
            observed_rows = list(payload["to"])
            gesture_row = list(payload["sg"])
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))

        summary = normalize_clinical_analysis(summary)
        phase_rows = [
            [self._canonical_phase_id(item[0]), item[1]]
            for item in phase_rows
            if item and self._canonical_phase_id(item[0])
        ]
        observed_rows = [
            [self._canonical_tool_id(item[0]), item[1], item[2], item[3]]
            for item in observed_rows
            if item and self._canonical_tool_id(item[0])
        ]
        if len(gesture_row) >= 2:
            gesture_row[1] = self._canonical_tool_id(gesture_row[1])

        phase_evidence = PhaseEvidence()
        phase_evidence.stamp = stamp
        phase_evidence.source = f"real_vlm:{self._spec.procedure_id}:{mode}"
        phase_evidence.phase_ids = [item[0] for item in phase_rows]
        phase_evidence.phase_confidences = [float(item[1]) for item in phase_rows]
        phase_evidence.visible_instrument_ids = [item[0] for item in observed_rows]
        phase_evidence.visible_instrument_confidences = [float(item[3]) for item in observed_rows]
        phase_evidence.scene_summary = summary
        phase_evidence.uncertainty = uncertainty
        self._phase_pub.publish(phase_evidence)

        # Schema v2+ Mayo rows have already passed CAM4 semantic
        # corroboration. Publish those public observations to the reducer just
        # like legacy v1 rows so DT Mayo state and confidence can actually
        # advance.
        for tool_id, location_id, location_type, confidence in observed_rows:
            observation = ToolObservation()
            observation.stamp = stamp
            observation.instrument_id = tool_id
            observation.location_id = location_id
            observation.location_type = location_type
            observation.confidence = float(confidence)
            observation.visible = True
            self._tool_pub.publish(observation)

        gesture = SurgeonGestureEvidence()
        gesture.stamp = stamp
        gesture.procedure_id = self._spec.procedure_id
        gesture.phase_id = phase_evidence.phase_ids[0] if phase_evidence.phase_ids else ""
        gesture.event_type = str(gesture_row[0])
        gesture.requested_tool = str(gesture_row[1])
        gesture.hand_pose = str(gesture_row[2])
        gesture.confidence = float(gesture_row[3])
        gesture.note = summary
        self._gesture_pub.publish(gesture)

        result = VLMResult()
        result.stamp = stamp
        result.source = phase_evidence.source
        result.schema_version = schema_version
        result.raw_json = raw_json
        result.summary = summary
        result.phase_ids = list(phase_evidence.phase_ids)
        result.phase_confidences = list(phase_evidence.phase_confidences)
        result.observed_tool_ids = [item[0] for item in observed_rows]
        result.observed_location_ids = [item[1] for item in observed_rows]
        result.observed_location_types = [item[2] for item in observed_rows]
        result.observed_confidences = [float(item[3]) for item in observed_rows]
        result.gesture_event_type = gesture.event_type
        result.gesture_requested_tool = gesture.requested_tool
        result.gesture_hand_pose = gesture.hand_pose
        result.gesture_confidence = gesture.confidence
        result.uncertainty = uncertainty
        self._result_pub.publish(result)

        self._publish_bed_robot_arm_group_proposal(
            payload,
            raw_json,
            stamp,
        )

        self._publish_health(
            image_source=image_source,
            latency_sec=latency_sec,
            prompt_chars=prompt_chars,
            output_chars=len(raw_json),
            parse_retry_count=parse_retry_count,
            last_error=last_error,
            mode=mode,
            healthy=healthy,
            connected=connected,
        )


def main() -> None:
    rclpy.init()
    node = RealVLMNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            executor.remove_node(node)
            executor.shutdown()
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
