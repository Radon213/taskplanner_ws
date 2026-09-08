"""Public contracts for RF-DETR preprocessing ahead of the VLM."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
from typing import Any, Iterable


# The overhead CAM3/CAM4 RF-DETR checkpoint is deliberately a small,
# deployment-specific, bbox-only ontology. Keep these labels and their order
# exactly aligned with the reviewed three-class training run: class indices are
# the checkpoint's ABI and must never be inferred from a historical model.
CAM4_CLASS_NAMES = (
    "Adson",
    "Bovie",
    "Bipolar",
)
CAM4_TOOL_CLASSES = frozenset(CAM4_CLASS_NAMES)
# Hand/request recognition is not part of this bbox checkpoint. Retain an
# explicit empty set so downstream summaries keep their stable tool_request
# shape without silently interpreting a removed detector row.
CAM4_HAND_CLASSES: frozenset[str] = frozenset()
CAMERA_BBOX_ONTOLOGY_VERSION = "mayo-bbox-3class-v1"
# Semantic summaries and debug overlays use the short detector labels above.
# The bounded typed Mayo-placement topic remains procedure-compatible by
# translating only at that boundary, rather than making its instrument IDs a
# function of a model-training label.
CAM4_PROCEDURE_INSTRUMENT_BY_CLASS = {
    "Adson": "Adson forceps",
    "Adson Forceps": "Adson forceps",
    "Bovie": "Bovie surgical cautery",
    "Bovie surgical cautery": "Bovie surgical cautery",
    "Bipolar": "Bipolar cautery",
    "Bipolar Forceps": "Bipolar cautery",
    "Bipolar cautery": "Bipolar cautery",
    "Mosquito": "Mosquito forceps",
    "Mosquito Forceps": "Mosquito forceps",
}
FLIR_CLASS_NAMES = (
    "#15 Scalpel",
    "Adson forceps",
    "Allis clamp forceps",
    "Bovie surgical cautery",
    "Army navy retractor",
    "Thyroid retractor",
    "Bipolar cautery",
    "Mosquito forceps",
    "Harmonic shears",
    "Yankauer suction",
)

# These rasters are intentionally outside the public ``/surgery/images``
# bridge allowlist.  They carry local transparent operator overlays or
# optional Debug preprocessing, not camera feeds intended for external
# sharing. Live VLM receives raw visual panels plus the typed observation
# contract below, never these rendered detector rasters.
LOCAL_RFDETR_FLIR_SEGMENTED_TOPIC = (
    "/taskplanner/internal/rfdetr/flir/segmented/compressed"
)
LOCAL_RFDETR_FLIR_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/flir/segmentation_overlay/compressed"
)
LOCAL_RFDETR_CAM4_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/cam4/detection_overlay/compressed"
)
LOCAL_RFDETR_CAM3_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/cam3/detection_overlay/compressed"
)

# A topic name alone cannot attest to the publisher in a ROS graph.  The
# local bridge tags each rendered frame, so the Live VLM can reject a raw or
# third-party image accidentally routed onto its internal topic.
RFDETR_FLIR_SEGMENTED_FRAME_MARKER = "rfdetr_seg"
RFDETR_FLIR_OVERLAY_FRAME_MARKER = "rfdetr_flir_overlay"
RFDETR_CAM4_OVERLAY_FRAME_MARKER = "rfdetr_cam4_overlay"
RFDETR_CAM3_OVERLAY_FRAME_MARKER = "rfdetr_cam3_overlay"

CAM4_MAYO_MIN_CONFIDENCE = 0.60
CAM4_MAYO_MIN_STABLE_SAMPLES = 2
CAM4_MAYO_MIN_STABLE_DURATION_SEC = 0.18
CAM4_MAYO_MAX_SAMPLE_GAP_SEC = 0.35
CAM4_MAYO_ABSENCE_RELEASE_SEC = 0.75
CAM4_MAYO_LEASE_RENEWAL_SEC = 0.75
CAM4_MAYO_BLOCKING_HAND_STATES = frozenset({"request", "hand_with_tool"})

# ``ToolObservation2DArray`` is the typed, per-frame RF-DETR result published
# by the vision system.  It deliberately carries much more than a VLM should
# receive (notably full instance-mask RLE).  Keep a separate, small projection
# for the VLM: it is enough to relate a detected instrument to a *camera-view*
# location without turning a rendered overlay into an input contract.
RFDETR_TOOL_OBSERVATIONS_SCHEMA = "taskplanner.rfdetr_tool_observations.v1"
RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION = 16_384
RFDETR_TOOL_OBSERVATION_MAX_DEPTH_M = 10.0


def append_rfdetr_frame_marker(frame_id: object, marker: str) -> str:
    """Return a stable, delimiter-bounded local RF-DETR frame provenance tag."""

    base = str(frame_id or "").strip()
    normalized_marker = str(marker or "").strip()
    if not normalized_marker:
        return base
    if frame_id_has_rfdetr_marker(base, normalized_marker):
        return base
    return f"{base}|{normalized_marker}" if base else normalized_marker


def frame_id_has_rfdetr_marker(frame_id: object, marker: str) -> bool:
    """Match a full provenance token rather than a loose substring."""

    normalized_marker = str(marker or "").strip().casefold()
    if not normalized_marker:
        return False
    return normalized_marker in {
        part.strip().casefold()
        for part in str(frame_id or "").split("|")
        if part.strip()
    }


@dataclass(frozen=True, slots=True)
class Cam4MayoPlacement:
    instrument_name: str
    source_stamp_sec: float
    presence_started_stamp_sec: float
    confidence: float
    visible_count: int
    stable_sample_count: int
    stable_duration_sec: float


@dataclass(slots=True)
class _Cam4MayoCandidate:
    first_stamp_sec: float
    last_stamp_sec: float
    sample_count: int
    min_confidence: float
    visible_count: int


class Cam4MayoPlacementTracker:
    """Turn public CAM4 detections into bounded Mayo-placement observations.

    A hand carrying or requesting a tool can pass through the fixed Mayo view.
    Candidate continuity is therefore reset while either public hand state is
    visible, and a placement is emitted only after the hand clears and the tool
    remains visible for a short, source-time-bounded streak.
    """

    def __init__(
        self,
        *,
        min_confidence: float = CAM4_MAYO_MIN_CONFIDENCE,
        min_stable_samples: int = CAM4_MAYO_MIN_STABLE_SAMPLES,
        min_stable_duration_sec: float = CAM4_MAYO_MIN_STABLE_DURATION_SEC,
        max_sample_gap_sec: float = CAM4_MAYO_MAX_SAMPLE_GAP_SEC,
        absence_release_sec: float = CAM4_MAYO_ABSENCE_RELEASE_SEC,
        lease_renewal_sec: float = CAM4_MAYO_LEASE_RENEWAL_SEC,
    ) -> None:
        self._min_confidence = float(min_confidence)
        self._min_stable_samples = max(1, int(min_stable_samples))
        self._min_stable_duration_sec = max(
            0.0,
            float(min_stable_duration_sec),
        )
        self._max_sample_gap_sec = max(0.0, float(max_sample_gap_sec))
        self._absence_release_sec = max(0.0, float(absence_release_sec))
        self._lease_renewal_sec = max(0.0, float(lease_renewal_sec))
        self.reset()

    def reset(self) -> None:
        self._candidates: dict[str, _Cam4MayoCandidate] = {}
        self._published: set[str] = set()
        self._last_published_at: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._last_source_stamp_sec: float | None = None

    def update(
        self,
        summary: dict[str, Any],
        *,
        release_confirmed: bool | None = None,
    ) -> list[Cam4MayoPlacement]:
        if (
            not isinstance(summary, dict)
            or summary.get("schema") != "taskplanner.cam4_semantics.v1"
            or summary.get("source") != "cam4_rfdetr_small"
        ):
            return []
        try:
            source_stamp_sec = float(summary["source_stamp_sec"])
        except (KeyError, TypeError, ValueError):
            return []
        if not math.isfinite(source_stamp_sec):
            return []
        if (
            self._last_source_stamp_sec is not None
            and source_stamp_sec < self._last_source_stamp_sec
        ):
            self.reset()
        elif self._last_source_stamp_sec == source_stamp_sec:
            return []
        self._last_source_stamp_sec = source_stamp_sec

        visible: dict[str, tuple[float, int]] = {}
        rows = summary.get("tools", [])
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", "")).strip()
                confidence = _finite_confidence(
                    row.get("max_confidence")
                )
                try:
                    count = max(0, int(row.get("count", 0)))
                except (TypeError, ValueError):
                    count = 0
                if not name or confidence is None or count <= 0:
                    continue
                previous = visible.get(name)
                if previous is None or confidence > previous[0]:
                    visible[name] = (confidence, count)
                self._last_seen[name] = source_stamp_sec

        for name in tuple(self._published):
            if (
                source_stamp_sec
                - self._last_seen.get(name, -math.inf)
                > self._absence_release_sec
            ):
                self._published.discard(name)
                self._last_published_at.pop(name, None)

        request = summary.get("tool_request", {})
        hand_state = (
            str(request.get("state", "uncertain"))
            if isinstance(request, dict)
            else "uncertain"
        )
        # The active three-class CAM4 checkpoint cannot observe hands.  Its
        # caller must therefore corroborate a release with the independently
        # source-stamped canonical CAM4 HandKeypoints stream. ``None`` retains
        # summary-only compatibility for offline callers; the runtime bridge
        # supplies explicit release evidence from /perception/cam_4/hand/keypoints.
        release_is_clear = (
            hand_state not in CAM4_MAYO_BLOCKING_HAND_STATES
            if release_confirmed is None
            else bool(release_confirmed)
        )
        if not release_is_clear:
            self._candidates.clear()
            return []

        for name in tuple(self._candidates):
            if name not in visible:
                self._candidates.pop(name, None)

        placements: list[Cam4MayoPlacement] = []
        for name, (confidence, count) in visible.items():
            if confidence < self._min_confidence:
                self._candidates.pop(name, None)
                continue
            candidate = self._candidates.get(name)
            if (
                candidate is None
                or source_stamp_sec - candidate.last_stamp_sec
                > self._max_sample_gap_sec
            ):
                candidate = _Cam4MayoCandidate(
                    first_stamp_sec=source_stamp_sec,
                    last_stamp_sec=source_stamp_sec,
                    sample_count=1,
                    min_confidence=confidence,
                    visible_count=count,
                )
                self._candidates[name] = candidate
            else:
                candidate.last_stamp_sec = source_stamp_sec
                candidate.sample_count += 1
                candidate.min_confidence = min(
                    candidate.min_confidence,
                    confidence,
                )
                candidate.visible_count = count

            stable_duration_sec = (
                candidate.last_stamp_sec - candidate.first_stamp_sec
            )
            if (
                candidate.sample_count < self._min_stable_samples
                or stable_duration_sec < self._min_stable_duration_sec
            ):
                continue
            # This is a renewable *visibility* lease, not permission to move
            # Digital Twin inventory.  Preserve the first source stamp of the
            # uninterrupted presence episode on every renewal: the reducer can
            # then prove that a new CAM4 appearance began after the exact
            # controller-confirmed handover, instead of laundering a class that
            # was already visible while the robot still held the instrument.
            last_published_at = self._last_published_at.get(name)
            if (
                last_published_at is not None
                and source_stamp_sec - last_published_at
                < self._lease_renewal_sec
            ):
                continue
            placements.append(
                Cam4MayoPlacement(
                    instrument_name=CAM4_PROCEDURE_INSTRUMENT_BY_CLASS.get(
                        name,
                        name,
                    ),
                    source_stamp_sec=source_stamp_sec,
                    presence_started_stamp_sec=(
                        candidate.first_stamp_sec
                    ),
                    confidence=round(candidate.min_confidence, 4),
                    visible_count=candidate.visible_count,
                    stable_sample_count=candidate.sample_count,
                    stable_duration_sec=round(stable_duration_sec, 6),
                )
            )
            self._published.add(name)
            self._last_published_at[name] = source_stamp_sec

        return placements


def _finite_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    return round(min(max(confidence, 0.0), 1.0), 4)


def _bounded_label(value: Any) -> str:
    return str(value or "").strip()[:80]


def canonical_rfdetr_tool_label(value: object) -> str:
    """Normalize a typed detector label to its clinical instrument name.

    This is a bounded, exact-label adapter at the public typed-observation
    boundary. It does not inspect a detector implementation, class index, or
    model version, and leaves unknown labels untouched for the active
    procedure to decide whether they are usable.
    """

    label = _bounded_label(value)
    normalized = " ".join(label.split()).casefold()
    if not normalized:
        return ""
    for detector_label, clinical_name in CAM4_PROCEDURE_INSTRUMENT_BY_CLASS.items():
        if normalized == " ".join(detector_label.split()).casefold():
            return clinical_name
    return label


def summarize_cam4_detections(
    detections: Iterable[dict[str, Any]],
    *,
    source_stamp_sec: float,
    inference_latency_ms: float,
) -> dict[str, Any]:
    """Reduce CAM4 boxes to tool counts and one conservative hand state.

    Coordinates are deliberately absent from this object. The VLM is allowed to
    consume this summary, but never the CAM4 image or detector annotation.
    """

    grouped: dict[str, list[float]] = defaultdict(list)
    hand_scores: dict[str, float] = {}
    for row in detections:
        if not isinstance(row, dict):
            continue
        class_name = _bounded_label(row.get("class_name"))
        confidence = _finite_confidence(row.get("confidence"))
        if not class_name or confidence is None:
            continue
        if class_name in CAM4_TOOL_CLASSES:
            grouped[class_name].append(confidence)
        elif class_name in CAM4_HAND_CLASSES:
            hand_scores[class_name] = max(
                confidence,
                hand_scores.get(class_name, 0.0),
            )

    tools = [
        {
            "name": name,
            "count": len(confidences),
            "max_confidence": round(max(confidences), 4),
            "mean_confidence": round(
                sum(confidences) / len(confidences),
                4,
            ),
        }
        for name, confidences in sorted(grouped.items())
    ]

    hand_class = max(hand_scores, key=hand_scores.get) if hand_scores else ""
    # The three-class bbox ABI has no hand rows. Keep the legacy public field
    # present, but never synthesize a request decision from a tool detection.
    request_state = "uncertain"
    requested: bool | None = None

    return {
        "schema": "taskplanner.cam4_semantics.v1",
        "source": "cam4_rfdetr_small",
        "source_stamp_sec": round(float(source_stamp_sec), 6),
        "ground_truth": False,
        "cam4_image_forwarded_to_vlm": False,
        "tools": tools,
        "tool_request": {
            "state": request_state,
            "requested": requested,
            "confidence": round(hand_scores.get(hand_class, 0.0), 4),
            "detector_class": hand_class,
        },
        "inference_latency_ms": round(max(0.0, float(inference_latency_ms)), 3),
    }


def parse_cam4_semantics_json(raw_json: str) -> dict[str, Any]:
    """Validate and bound the public CAM4 summary received by the VLM node."""

    try:
        payload = json.loads(str(raw_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("schema", "")) != "taskplanner.cam4_semantics.v1":
        return {}

    try:
        source_stamp_sec = float(payload["source_stamp_sec"])
    except (KeyError, TypeError, ValueError):
        return {}
    if not math.isfinite(source_stamp_sec):
        return {}

    bounded_tools: list[dict[str, Any]] = []
    rows = payload.get("tools", [])
    if isinstance(rows, list):
        for row in rows[:16]:
            if not isinstance(row, dict):
                continue
            name = _bounded_label(row.get("name"))
            try:
                count = max(0, min(16, int(row.get("count", 0))))
            except (TypeError, ValueError):
                count = 0
            maximum = _finite_confidence(row.get("max_confidence"))
            mean = _finite_confidence(row.get("mean_confidence"))
            if not name or count <= 0 or maximum is None or mean is None:
                continue
            bounded_tools.append(
                {
                    "name": name,
                    "count": count,
                    "max_confidence": maximum,
                    "mean_confidence": mean,
                }
            )

    request = payload.get("tool_request", {})
    if not isinstance(request, dict):
        request = {}
    state = str(request.get("state", "uncertain")).strip()
    if state not in {"request", "not_request", "hand_with_tool", "uncertain"}:
        state = "uncertain"
    confidence = _finite_confidence(request.get("confidence"))
    if confidence is None:
        confidence = 0.0
    requested: bool | None
    if state == "request":
        requested = True
    elif state == "not_request":
        requested = False
    else:
        requested = None

    return {
        "schema": "taskplanner.cam4_semantics.v1",
        "source": "cam4_rfdetr_small",
        "source_stamp_sec": round(source_stamp_sec, 6),
        "ground_truth": False,
        "cam4_image_forwarded_to_vlm": False,
        "tools": bounded_tools,
        "tool_request": {
            "state": state,
            "requested": requested,
            "confidence": confidence,
        },
    }


def _message_attr(message: object, name: str, default: Any = None) -> Any:
    """Read generated ROS messages and lightweight test doubles uniformly."""

    return getattr(message, name, default)


def _bounded_uint(value: Any, *, maximum: int = 65_535) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0 or numeric > maximum:
        return None
    return numeric


def _finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _fixed_numeric_vector(
    value: Any,
    *,
    length: int,
) -> tuple[Any, ...] | None:
    """Read a bounded ROS numeric vector without assuming its concrete type.

    Generated ROS Python messages expose fixed primitive fields as either an
    ``array.array`` or a NumPy ``ndarray`` depending on the generated binding
    and middleware path.  Neither is guaranteed to satisfy
    ``collections.abc.Sequence``.  A length-checked iterable is enough for
    the public projection; conversion still happens below and malformed or
    non-finite values remain rejected.
    """

    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        # NumPy exposes the dimensionality of the generated fixed array.  Do
        # not flatten a matrix-shaped payload into a purported 2-D image box.
        if hasattr(value, "ndim") and int(value.ndim) != 1:
            return None
        if len(value) != length:
            return None
        items = tuple(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return items if len(items) == length else None


def _normalized_bbox(
    value: Any,
    *,
    image_width: int,
    image_height: int,
) -> tuple[list[float], list[float]] | None:
    """Return a valid normalized xyxy box and its center, or reject it."""

    items = _fixed_numeric_vector(value, length=4)
    if items is None:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in items)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    # A source may report an edge fraction outside an image by a few pixels.
    # Clamping that edge is safe; a collapsed or reversed box is not evidence.
    x0 = min(max(x0, 0.0), float(image_width))
    x1 = min(max(x1, 0.0), float(image_width))
    y0 = min(max(y0, 0.0), float(image_height))
    y1 = min(max(y1, 0.0), float(image_height))
    if x1 <= x0 or y1 <= y0:
        return None
    box = [
        round(x0 / float(image_width), 6),
        round(y0 / float(image_height), 6),
        round(x1 / float(image_width), 6),
        round(y1 / float(image_height), 6),
    ]
    center = [
        round((box[0] + box[2]) / 2.0, 6),
        round((box[1] + box[3]) / 2.0, 6),
    ]
    return box, center


def _normalized_point(
    value: Any,
    *,
    image_width: int,
    image_height: int,
) -> list[float] | None:
    items = _fixed_numeric_vector(value, length=2)
    if items is None:
        return None
    try:
        u, v = (float(item) for item in items)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(u) or not math.isfinite(v):
        return None
    if not 0.0 <= u <= float(image_width) or not 0.0 <= v <= float(
        image_height
    ):
        return None
    return [
        round(u / float(image_width), 6),
        round(v / float(image_height), 6),
    ]


def _image_region(center_uv_norm: list[float]) -> str:
    """Encode a bounded image-relative location, never a world-frame pose."""

    x, y = center_uv_norm
    horizontal = (
        "left"
        if x < 1.0 / 3.0
        else "right"
        if x > 2.0 / 3.0
        else "center"
    )
    vertical = (
        "upper"
        if y < 1.0 / 3.0
        else "lower"
        if y > 2.0 / 3.0
        else "middle"
    )
    return f"{vertical}_{horizontal}"


def summarize_rfdetr_tool_observations(
    message: object,
    *,
    expected_view: str,
    expected_model_version: str = "",
    max_instances: int = 24,
) -> dict[str, Any]:
    """Project one typed CAM3/CAM4 RF-DETR frame into bounded VLM evidence.

    The input is intentionally duck-typed so this contract remains easy to
    unit test without a ROS executor.  It accepts only a self-identifying
    typed frame for the expected RF-DETR-owned topic and excludes image bytes,
    mask RLE, mask geometry, raw frame IDs, and all unbounded free text.
    """

    view = str(expected_view or "").strip()
    if view not in {"cam_3", "cam_4"}:
        return {}
    if str(_message_attr(message, "view", "")).strip() != view:
        return {}

    schema_version = _bounded_label(
        _message_attr(message, "schema_version", "")
    )
    observation_id = _bounded_label(
        _message_attr(message, "observation_id", "")
    )
    model_version = _bounded_label(
        _message_attr(message, "model_version", "")
    )
    ontology_version = _bounded_label(
        _message_attr(message, "ontology_version", "")
    )
    if (
        not schema_version
        or not observation_id
        or not model_version
        or not ontology_version
    ):
        return {}
    # The configured typed topic and schema establish the provider contract;
    # model_version is mandatory audit metadata, not an implementation-name
    # heuristic.  Empty expected_model_version deliberately permits provider
    # checkpoint rollouts.  A non-empty value is an explicit exact pin.
    pinned_model_version = _bounded_label(expected_model_version)
    if pinned_model_version and model_version != pinned_model_version:
        return {}

    width = _bounded_uint(
        _message_attr(message, "image_width", 0),
        maximum=RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION,
    )
    height = _bounded_uint(
        _message_attr(message, "image_height", 0),
        maximum=RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION,
    )
    if not width or not height:
        return {}
    header = _message_attr(message, "header", None)
    stamp = _message_attr(header, "stamp", None)
    try:
        stamp_sec = float(_message_attr(stamp, "sec", 0)) + float(
            _message_attr(stamp, "nanosec", 0)
        ) / 1_000_000_000.0
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(stamp_sec) or stamp_sec <= 0.0:
        return {}
    sequence = _bounded_uint(
        _message_attr(message, "sequence", 0),
        maximum=2**63 - 1,
    )
    if sequence is None:
        return {}

    raw_instances = _message_attr(message, "instances", [])
    if not isinstance(raw_instances, (list, tuple)):
        return {}
    candidates: list[dict[str, Any]] = []
    malformed_instance_count = 0
    for raw in raw_instances:
        class_name = _bounded_label(_message_attr(raw, "class_name", ""))
        confidence = _finite_confidence(
            _message_attr(raw, "class_confidence", None)
        )
        canonical_class_id = _bounded_uint(
            _message_attr(raw, "canonical_class_id", None)
        )
        model_class_index = _bounded_uint(
            _message_attr(raw, "model_class_index", None)
        )
        local_id = _bounded_uint(
            _message_attr(raw, "frame_local_instance_id", None),
            maximum=2**32 - 1,
        )
        bbox = _normalized_bbox(
            _message_attr(raw, "bbox_xyxy_px", None),
            image_width=width,
            image_height=height,
        )
        if (
            not class_name
            or confidence is None
            or confidence <= 0.0
            or canonical_class_id is None
            or model_class_index is None
            or local_id is None
            or bbox is None
        ):
            malformed_instance_count += 1
            continue
        bbox_xyxy_norm, center_uv_norm = bbox
        observation_point_uv_norm: list[float] | None = None
        if bool(_message_attr(raw, "observation_point_valid", False)):
            observation_point_uv_norm = _normalized_point(
                _message_attr(raw, "observation_point_uv_px", None),
                image_width=width,
                image_height=height,
            )
            if observation_point_uv_norm is None:
                malformed_instance_count += 1
                continue
        instance: dict[str, Any] = {
            "class_name": class_name,
            "canonical_class_id": canonical_class_id,
            "model_class_index": model_class_index,
            "confidence": confidence,
            "bbox_xyxy_norm": bbox_xyxy_norm,
            "center_uv_norm": center_uv_norm,
            "image_region": _image_region(
                observation_point_uv_norm or center_uv_norm
            ),
            "frame_local_instance_id": local_id,
        }
        if observation_point_uv_norm is not None:
            instance["observation_point_uv_norm"] = observation_point_uv_norm
        if bool(_message_attr(raw, "observation_point_depth_valid", False)):
            depth = _finite_float(
                _message_attr(raw, "observation_point_depth_m", None)
            )
            if (
                depth is not None
                and 0.0 < depth <= RFDETR_TOOL_OBSERVATION_MAX_DEPTH_M
            ):
                instance["depth_m"] = round(depth, 4)
            else:
                malformed_instance_count += 1
                continue
        candidates.append(instance)

    # An explicitly empty, otherwise valid array is an executed no-detection
    # observation. A non-empty array whose rows cannot satisfy the typed
    # contract is malformed—not evidence that the view contains no tools.
    if malformed_instance_count:
        return {}

    candidates.sort(
        key=lambda item: (
            -float(item["confidence"]),
            str(item["class_name"]),
            int(item["frame_local_instance_id"]),
        )
    )
    limit = max(0, int(max_instances))
    bounded_instances = candidates[:limit]
    return {
        "schema": RFDETR_TOOL_OBSERVATIONS_SCHEMA,
        "source": "rfdetr_tool_observation_2d",
        "view": view,
        "source_stamp_sec": round(stamp_sec, 6),
        "sequence": sequence,
        "model_version": model_version,
        "ontology_version": ontology_version,
        "instances": bounded_instances,
        "detection_status": (
            "detections" if candidates else "no_detections"
        ),
        "truncated": len(candidates) > len(bounded_instances),
        "ground_truth": False,
        "mask_rle_forwarded_to_vlm": False,
    }
