"""Public contracts for RF-DETR preprocessing ahead of the VLM."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
from typing import Any, Iterable


CAM4_TOOL_CLASSES = {
    "Adson forceps",
    "Allis clamp forceps",
    "Bovie surgical cautery",
    "Army navy retractor",
    "Bipolar Cautery",
    "Mosquito",
    "Thyroid retractor",
}
CAM4_HAND_CLASSES = {
    "Hand_request",
    "Hand_Not_Request",
    "Hand_with_tool",
}
CAM4_CLASS_NAMES = (
    "Adson forceps",
    "Allis clamp forceps",
    "Bovie surgical cautery",
    "Army navy retractor",
    "Bipolar Cautery",
    "Mosquito",
    "Hand_request",
    "Hand_Not_Request",
    "Hand_with_tool",
    "Thyroid retractor",
)
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

CAM4_MAYO_MIN_CONFIDENCE = 0.60
CAM4_MAYO_MIN_STABLE_SAMPLES = 2
CAM4_MAYO_MIN_STABLE_DURATION_SEC = 0.18
CAM4_MAYO_MAX_SAMPLE_GAP_SEC = 0.35
CAM4_MAYO_ABSENCE_RELEASE_SEC = 0.75
CAM4_MAYO_BLOCKING_HAND_STATES = frozenset({"request", "hand_with_tool"})


@dataclass(frozen=True, slots=True)
class Cam4MayoPlacement:
    instrument_name: str
    source_stamp_sec: float
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
    ) -> None:
        self._min_confidence = float(min_confidence)
        self._min_stable_samples = max(1, int(min_stable_samples))
        self._min_stable_duration_sec = max(
            0.0,
            float(min_stable_duration_sec),
        )
        self._max_sample_gap_sec = max(0.0, float(max_sample_gap_sec))
        self._absence_release_sec = max(0.0, float(absence_release_sec))
        self.reset()

    def reset(self) -> None:
        self._candidates: dict[str, _Cam4MayoCandidate] = {}
        self._published: set[str] = set()
        self._last_seen: dict[str, float] = {}
        self._last_source_stamp_sec: float | None = None

    def update(
        self,
        summary: dict[str, Any],
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

        request = summary.get("tool_request", {})
        hand_state = (
            str(request.get("state", "uncertain"))
            if isinstance(request, dict)
            else "uncertain"
        )
        if hand_state in CAM4_MAYO_BLOCKING_HAND_STATES:
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
                name in self._published
                or candidate.sample_count < self._min_stable_samples
                or stable_duration_sec < self._min_stable_duration_sec
            ):
                continue
            placements.append(
                Cam4MayoPlacement(
                    instrument_name=name,
                    source_stamp_sec=source_stamp_sec,
                    confidence=round(candidate.min_confidence, 4),
                    visible_count=candidate.visible_count,
                    stable_sample_count=candidate.sample_count,
                    stable_duration_sec=round(stable_duration_sec, 6),
                )
            )
            self._published.add(name)

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

    hand_class = (
        max(hand_scores, key=hand_scores.get)
        if hand_scores
        else ""
    )
    if hand_class == "Hand_request":
        request_state = "request"
        requested: bool | None = True
    elif hand_class == "Hand_Not_Request":
        request_state = "not_request"
        requested = False
    elif hand_class == "Hand_with_tool":
        request_state = "hand_with_tool"
        requested = None
    else:
        request_state = "uncertain"
        requested = None

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
