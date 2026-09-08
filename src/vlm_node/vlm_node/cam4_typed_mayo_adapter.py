"""Project CAM4's typed detector observations into Mayo placement evidence.

This is deliberately a small perception-owner adapter.  It consumes the
external 1.7 ``ToolObservation2DArray`` DDS contract directly and publishes
the already-installed ``surgical_msgs/ToolObservation`` type used by the
Digital Twin.  It does not call a VLM or start a local detector.  Active run
identity fences Reset/start boundaries, while the active scenario is read only
to translate the detector's editable class names through that scenario's
instrument aliases.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import time
from typing import Callable, Iterable

from procedure_spec import (
    get_default_spec_dir,
    load_bundle,
    load_scenario_consumer_bundle,
    parse_scenario_config,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, ToolObservation
from surgical_perception_msgs.msg import ToolObservation2DArray


CAM4_TYPED_MAYO_OBSERVATION_SOURCE = "cam4_typed_mayo_observation"
CAM4_TYPED_MAYO_CORRELATION_PREFIX = "cam4-typed-mayo:v1"


@dataclass(frozen=True, slots=True)
class TypedMayoDetection:
    """One scenario-resolved tool occurrence in one typed CAM4 frame."""

    instrument_id: str
    confidence: float
    observed_count: int
    frame_local_instance_id: int
    source_stamp_sec: int
    source_stamp_nanosec: int
    input_sequence: int
    bbox_xyxy_px: tuple[float, float, float, float] | None


@dataclass(slots=True)
class _TypedMayoTrack:
    track_id: int
    instrument_id: str
    bbox_xyxy_px: tuple[float, float, float, float]
    last_seen_sec: float
    candidate_instrument_id: str = ""
    candidate_first_seen_sec: float = 0.0
    candidate_hits: int = 0


@dataclass(slots=True)
class _BaselineVisibilityTrack:
    instrument_id: str
    bbox_xyxy_px: tuple[float, float, float, float] | None
    last_matched_sec: float


def _normalised_view(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").strip().casefold()
        if character.isalnum()
    )


def _source_stamp(array: object) -> tuple[int, int] | None:
    """Return the upstream source stamp without substituting local receipt time."""

    header = getattr(array, "header", None)
    for candidate in (
        getattr(header, "stamp", None),
        getattr(array, "stamp", None),
    ):
        try:
            sec = int(getattr(candidate, "sec", 0))
            nanosec = int(getattr(candidate, "nanosec", 0))
        except (TypeError, ValueError):
            continue
        if sec > 0 and 0 <= nanosec < 1_000_000_000:
            return sec, nanosec
    return None


def _finite_confidence(value: object) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    return min(max(confidence, 0.0), 1.0)


def _finite_bbox(
    value: object,
) -> tuple[float, float, float, float] | None:
    try:
        coordinates = tuple(float(item) for item in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if len(coordinates) != 4 or not all(math.isfinite(item) for item in coordinates):
        return None
    x_min, y_min, x_max, y_max = coordinates
    if x_max <= x_min or y_max <= y_min:
        return None
    return (x_min, y_min, x_max, y_max)


def _bbox_iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection_width = max(
        0.0,
        min(left[2], right[2]) - max(left[0], right[0]),
    )
    intersection_height = max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )
    intersection = intersection_width * intersection_height
    if intersection <= 0.0:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


class TemporalTypedMayoFilter:
    """Suppress short class swaps on the same CAM4 image-space object.

    The detector's instance ID is frame-local, but its bbox is physical image
    evidence.  A different class at a strongly overlapping bbox remains
    ambiguous until it persists; a new non-overlapping object is passed
    through immediately and is still subject to the Twin's placement dwell.
    """

    def __init__(
        self,
        *,
        match_iou: float = 0.35,
        class_switch_min_duration_sec: float = 0.75,
        class_switch_min_hits: int = 4,
        track_timeout_sec: float = 0.75,
    ) -> None:
        self._match_iou = float(match_iou)
        self._class_switch_min_duration_sec = float(
            class_switch_min_duration_sec
        )
        self._class_switch_min_hits = max(2, int(class_switch_min_hits))
        self._track_timeout_sec = float(track_timeout_sec)
        self._tracks: dict[int, _TypedMayoTrack] = {}
        self._next_track_id = 1
        self._last_frame_stamp_sec = 0.0

    def reset(self) -> None:
        self._tracks.clear()
        self._next_track_id = 1
        self._last_frame_stamp_sec = 0.0

    @staticmethod
    def _stamp_sec(detection: TypedMayoDetection) -> float:
        return float(detection.source_stamp_sec) + (
            float(detection.source_stamp_nanosec) / 1_000_000_000.0
        )

    def update(
        self,
        detections: Iterable[TypedMayoDetection],
    ) -> tuple[TypedMayoDetection, ...]:
        rows = tuple(detections)
        if not rows:
            return ()
        frame_stamp_sec = self._stamp_sec(rows[0])
        if (
            self._last_frame_stamp_sec > 0.0
            and frame_stamp_sec < self._last_frame_stamp_sec
        ):
            self.reset()
        self._last_frame_stamp_sec = frame_stamp_sec
        self._tracks = {
            track_id: track
            for track_id, track in self._tracks.items()
            if frame_stamp_sec - track.last_seen_sec <= self._track_timeout_sec
        }

        candidate_pairs: list[tuple[float, int, int]] = []
        for row_index, row in enumerate(rows):
            if row.bbox_xyxy_px is None:
                continue
            for track_id, track in self._tracks.items():
                overlap = _bbox_iou(row.bbox_xyxy_px, track.bbox_xyxy_px)
                if overlap >= self._match_iou:
                    candidate_pairs.append((overlap, row_index, track_id))
        candidate_pairs.sort(reverse=True)
        matched_rows: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _overlap, row_index, track_id in candidate_pairs:
            if row_index in matched_rows or track_id in used_tracks:
                continue
            matched_rows[row_index] = track_id
            used_tracks.add(track_id)

        emitted: list[TypedMayoDetection] = []
        for row_index, row in enumerate(rows):
            track_id = matched_rows.get(row_index)
            if track_id is None or row.bbox_xyxy_px is None:
                if row.bbox_xyxy_px is not None:
                    track_id = self._next_track_id
                    self._next_track_id += 1
                    self._tracks[track_id] = _TypedMayoTrack(
                        track_id=track_id,
                        instrument_id=row.instrument_id,
                        bbox_xyxy_px=row.bbox_xyxy_px,
                        last_seen_sec=frame_stamp_sec,
                    )
                emitted.append(row)
                continue

            track = self._tracks[track_id]
            track.bbox_xyxy_px = row.bbox_xyxy_px
            track.last_seen_sec = frame_stamp_sec
            if row.instrument_id == track.instrument_id:
                track.candidate_instrument_id = ""
                track.candidate_first_seen_sec = 0.0
                track.candidate_hits = 0
                emitted.append(row)
                continue

            if track.candidate_instrument_id != row.instrument_id:
                track.candidate_instrument_id = row.instrument_id
                track.candidate_first_seen_sec = frame_stamp_sec
                track.candidate_hits = 1
                continue
            track.candidate_hits += 1
            if (
                track.candidate_hits < self._class_switch_min_hits
                or frame_stamp_sec - track.candidate_first_seen_sec
                < self._class_switch_min_duration_sec
            ):
                continue
            track.instrument_id = row.instrument_id
            track.candidate_instrument_id = ""
            track.candidate_first_seen_sec = 0.0
            track.candidate_hits = 0
            emitted.append(row)

        counts = Counter(row.instrument_id for row in emitted)
        return tuple(
            replace(row, observed_count=counts[row.instrument_id])
            for row in emitted
        )


class RunStartVisibilityFence:
    """Keep pre-existing CAM4 visibility out of a new run's placement evidence.

    Reset establishes the authored rack state.  Tools already visible when a
    new run begins are therefore a baseline, not evidence that three separate
    placements just happened.  A baseline bbox must disappear for a bounded
    interval before a later appearance at that location can be emitted.
    Non-overlapping new objects remain observable during the same run.
    """

    def __init__(
        self,
        *,
        capture_duration_sec: float = 1.0,
        clear_duration_sec: float = 0.75,
        match_iou: float = 0.35,
    ) -> None:
        self._capture_duration_sec = float(capture_duration_sec)
        self._clear_duration_sec = float(clear_duration_sec)
        self._match_iou = float(match_iou)
        self._capture_until_sec = 0.0
        self._active = False
        self._tracks: list[_BaselineVisibilityTrack] = []

    def reset(self) -> None:
        self._capture_until_sec = 0.0
        self._active = False
        self._tracks.clear()

    def start(self, now_sec: float) -> None:
        self._capture_until_sec = float(now_sec) + self._capture_duration_sec
        self._active = True
        self._tracks.clear()

    def _matches(
        self,
        row: TypedMayoDetection,
        track: _BaselineVisibilityTrack,
    ) -> bool:
        if row.instrument_id != track.instrument_id:
            return False
        if row.bbox_xyxy_px is None or track.bbox_xyxy_px is None:
            return True
        return _bbox_iou(row.bbox_xyxy_px, track.bbox_xyxy_px) >= self._match_iou

    def update(
        self,
        detections: Iterable[TypedMayoDetection],
        *,
        now_sec: float,
    ) -> tuple[TypedMayoDetection, ...]:
        rows = tuple(detections)
        now = float(now_sec)
        if not self._active:
            return rows

        if now <= self._capture_until_sec:
            used_tracks: set[int] = set()
            for row in rows:
                match_index = next(
                    (
                        index
                        for index, track in enumerate(self._tracks)
                        if index not in used_tracks and self._matches(row, track)
                    ),
                    None,
                )
                if match_index is None:
                    self._tracks.append(
                        _BaselineVisibilityTrack(
                            instrument_id=row.instrument_id,
                            bbox_xyxy_px=row.bbox_xyxy_px,
                            last_matched_sec=now,
                        )
                    )
                    used_tracks.add(len(self._tracks) - 1)
                else:
                    track = self._tracks[match_index]
                    track.bbox_xyxy_px = row.bbox_xyxy_px
                    track.last_matched_sec = now
                    used_tracks.add(match_index)
            return ()

        used_tracks: set[int] = set()
        emitted: list[TypedMayoDetection] = []
        for row in rows:
            match_index = next(
                (
                    index
                    for index, track in enumerate(self._tracks)
                    if index not in used_tracks and self._matches(row, track)
                ),
                None,
            )
            if match_index is None:
                emitted.append(row)
                continue
            track = self._tracks[match_index]
            track.bbox_xyxy_px = row.bbox_xyxy_px
            track.last_matched_sec = now
            used_tracks.add(match_index)

        self._tracks = [
            track
            for index, track in enumerate(self._tracks)
            if index in used_tracks
            or now - track.last_matched_sec <= self._clear_duration_sec
        ]
        if not self._tracks:
            self._active = False
        counts = Counter(row.instrument_id for row in emitted)
        return tuple(
            replace(row, observed_count=counts[row.instrument_id])
            for row in emitted
        )


def summarize_typed_mayo_detections(
    array: object,
    *,
    resolve_instrument_alias: Callable[[str], str | None],
    expected_view: str = "cam_4",
    min_confidence: float = 0.60,
) -> tuple[TypedMayoDetection, ...]:
    """Resolve one external typed frame into deterministic Mayo occurrences.

    The producer's 2-D local IDs are unique only within this frame, so they
    are retained for ordering/audit only.  Multiple same-type rows are kept as
    a count: the Digital Twin uses that count to avoid treating a continuing
    Mayo detection as a second surgeon return.
    """

    expected = _normalised_view(expected_view)
    if expected and _normalised_view(getattr(array, "view", "")) != expected:
        return ()
    stamp = _source_stamp(array)
    if stamp is None:
        return ()
    threshold = _finite_confidence(min_confidence)
    if threshold is None:
        return ()
    try:
        input_sequence = max(0, int(getattr(array, "sequence", 0)))
    except (TypeError, ValueError):
        input_sequence = 0

    grouped: dict[
        str,
        list[
            tuple[
                int,
                float,
                tuple[float, float, float, float] | None,
            ]
        ],
    ] = defaultdict(list)
    for ordinal, instance in enumerate(getattr(array, "instances", ()) or ()):
        label = str(getattr(instance, "class_name", "") or "").strip()
        instrument_id = resolve_instrument_alias(label) if label else None
        confidence = _finite_confidence(
            getattr(instance, "class_confidence", None)
        )
        if not instrument_id or confidence is None or confidence < threshold:
            continue
        try:
            local_id = int(
                getattr(instance, "frame_local_instance_id", ordinal + 1)
            )
        except (TypeError, ValueError):
            local_id = ordinal + 1
        grouped[str(instrument_id)].append(
            (
                max(0, local_id),
                confidence,
                _finite_bbox(getattr(instance, "bbox_xyxy_px", None)),
            )
        )

    detections: list[TypedMayoDetection] = []
    for instrument_id in sorted(grouped):
        rows = sorted(grouped[instrument_id], key=lambda row: (row[0], -row[1]))
        observed_count = len(rows)
        for local_id, confidence, bbox_xyxy_px in rows:
            detections.append(
                TypedMayoDetection(
                    instrument_id=instrument_id,
                    confidence=confidence,
                    observed_count=observed_count,
                    frame_local_instance_id=local_id,
                    source_stamp_sec=stamp[0],
                    source_stamp_nanosec=stamp[1],
                    input_sequence=input_sequence,
                    bbox_xyxy_px=bbox_xyxy_px,
                )
            )
    return tuple(detections)


def typed_mayo_correlation_id(
    *,
    source_epoch: int,
    input_sequence: int,
    ordinal: int,
    observed_count: int,
) -> str:
    """Encode only the frame-local multiplicity needed by the Twin reducer."""

    return (
        f"{CAM4_TYPED_MAYO_CORRELATION_PREFIX}:{int(source_epoch)}:"
        f"{max(0, int(input_sequence))}:{max(1, int(ordinal))}:"
        f"{max(1, int(observed_count))}"
    )


class Cam4TypedMayoAdapter(Node):
    """Independent CAM4 typed-observation to Mayo placement adapter."""

    def __init__(self) -> None:
        super().__init__("cam4_typed_mayo_adapter")
        self.declare_parameter(
            "input_topic", "/perception/cam_4/tool/observations"
        )
        self.declare_parameter(
            "output_topic", "/surgery/perception/cam4/mayo_tool_observations"
        )
        self.declare_parameter("scenario_config_topic", "/simulation/scenario_config")
        self.declare_parameter("simulation_state_topic", "/simulation/state")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("expected_view", "cam_4")
        self.declare_parameter("min_confidence", 0.60)
        self.declare_parameter("enabled", True)

        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._spec = load_bundle(Path(self._spec_dir))
        self._scenario_revision = ""
        self._expected_view = str(self.get_parameter("expected_view").value)
        self._min_confidence = float(self.get_parameter("min_confidence").value)
        self._enabled = bool(self.get_parameter("enabled").value)
        self._source_epoch = max(1, time.time_ns())
        self._source_sequence = 0
        self._temporal_filter = TemporalTypedMayoFilter()
        self._run_start_fence = RunStartVisibilityFence()
        self._active_run_id = ""
        self._runtime_active = False

        self._publisher = self.create_publisher(
            ToolObservation,
            str(self.get_parameter("output_topic").value),
            30,
        )
        self.create_subscription(
            ToolObservation2DArray,
            str(self.get_parameter("input_topic").value),
            self._on_tool_observations,
            qos_profile_sensor_data,
        )
        scenario_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("scenario_config_topic").value),
            self._on_scenario_config,
            scenario_qos,
        )
        self.create_subscription(
            SimulationState,
            str(self.get_parameter("simulation_state_topic").value),
            self._on_simulation_state,
            10,
        )
        self.add_on_set_parameters_callback(self._on_parameters_changed)

    def _resolve_instrument(self, label: str) -> str | None:
        return self._spec.resolve_instrument_alias(label)

    def _advance_epoch(self) -> None:
        self._source_epoch = max(self._source_epoch + 1, time.time_ns())
        self._source_sequence = 0
        self._temporal_filter.reset()

    def _on_simulation_state(self, message: SimulationState) -> None:
        run_id = str(message.procedure_run_id or "").strip()
        execution_state = str(message.execution_state or "").strip().casefold()
        active = bool(message.running) and execution_state == "running" and bool(run_id)
        if active and run_id != self._active_run_id:
            self._active_run_id = run_id
            self._runtime_active = True
            self._advance_epoch()
            self._run_start_fence.start(time.monotonic())
            return
        if not active and self._runtime_active:
            self._active_run_id = ""
            self._runtime_active = False
            self._advance_epoch()
            self._run_start_fence.reset()

    def _on_scenario_config(self, message: String) -> None:
        try:
            snapshot = parse_scenario_config(message.data)
            if snapshot.revision == self._scenario_revision:
                return
            bundle = load_scenario_consumer_bundle(
                snapshot,
                fixed_spec_root=Path(self._spec_dir).resolve().parent,
            )
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            self.get_logger().warning(
                f"ignoring invalid CAM4 typed Mayo scenario revision: {exc}",
                throttle_duration_sec=5.0,
            )
            return
        self._spec = bundle.procedure_spec
        self._spec_dir = bundle.spec_dir
        self._scenario_revision = snapshot.revision
        self._advance_epoch()

    def _on_parameters_changed(self, parameters) -> SetParametersResult:
        next_enabled = self._enabled
        next_min_confidence = self._min_confidence
        next_expected_view = self._expected_view
        for parameter in parameters:
            if parameter.name == "enabled":
                if not isinstance(parameter.value, bool):
                    return SetParametersResult(
                        successful=False, reason="enabled must be boolean"
                    )
                next_enabled = parameter.value
            elif parameter.name == "min_confidence":
                value = _finite_confidence(parameter.value)
                if value is None:
                    return SetParametersResult(
                        successful=False,
                        reason="min_confidence must be a finite 0..1 number",
                    )
                next_min_confidence = value
            elif parameter.name == "expected_view":
                value = str(parameter.value or "").strip()
                if not value:
                    return SetParametersResult(
                        successful=False, reason="expected_view must be non-empty"
                    )
                next_expected_view = value
        self._enabled = next_enabled
        self._min_confidence = next_min_confidence
        self._expected_view = next_expected_view
        return SetParametersResult(successful=True)

    def _on_tool_observations(self, array: ToolObservation2DArray) -> None:
        if not self._enabled or not self._runtime_active:
            return
        detections = summarize_typed_mayo_detections(
            array,
            resolve_instrument_alias=self._resolve_instrument,
            expected_view=self._expected_view,
            min_confidence=self._min_confidence,
        )
        detections = self._temporal_filter.update(detections)
        detections = self._run_start_fence.update(
            detections,
            now_sec=time.monotonic(),
        )
        for ordinal, detection in enumerate(detections, start=1):
            self._source_sequence += 1
            observation = ToolObservation()
            observation.stamp.sec = detection.source_stamp_sec
            observation.stamp.nanosec = detection.source_stamp_nanosec
            observation.source = CAM4_TYPED_MAYO_OBSERVATION_SOURCE
            observation.source_epoch = self._source_epoch
            observation.source_sequence = self._source_sequence
            observation.correlation_id = typed_mayo_correlation_id(
                source_epoch=self._source_epoch,
                input_sequence=detection.input_sequence,
                ordinal=ordinal,
                observed_count=detection.observed_count,
            )
            observation.instrument_id = detection.instrument_id
            observation.location_type = "mayo_stand"
            observation.location_id = "mayo_stand"
            observation.confidence = detection.confidence
            observation.visible = True
            self._publisher.publish(observation)


def main(args: Iterable[str] | None = None) -> None:
    rclpy.init(args=args)
    node = Cam4TypedMayoAdapter()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


__all__ = [
    "CAM4_TYPED_MAYO_CORRELATION_PREFIX",
    "CAM4_TYPED_MAYO_OBSERVATION_SOURCE",
    "Cam4TypedMayoAdapter",
    "RunStartVisibilityFence",
    "TemporalTypedMayoFilter",
    "TypedMayoDetection",
    "summarize_typed_mayo_detections",
    "typed_mayo_correlation_id",
]
