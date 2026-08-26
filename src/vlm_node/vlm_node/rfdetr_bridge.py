"""ROS bridge for the local RF-DETR preprocessing service."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import json
import re
import threading
import time
from typing import Any

import requests
import rclpy
from hand_keypoint_interfaces.msg import HandKeypoints
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from std_srvs.srv import SetBool
from surgical_msgs.msg import ToolObservation
from surgical_perception_msgs.msg import ToolObservation2D, ToolObservation2DArray

from .rfdetr_contract import (
    Cam4MayoPlacementTracker,
    RFDETR_CAM3_OVERLAY_FRAME_MARKER,
    RFDETR_CAM4_OVERLAY_FRAME_MARKER,
    RFDETR_FLIR_OVERLAY_FRAME_MARKER,
    RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    RFDETR_TOOL_OBSERVATIONS_SCHEMA,
    append_rfdetr_frame_marker,
    parse_cam4_semantics_json,
    summarize_cam4_detections,
)


@dataclass(frozen=True, slots=True)
class BufferedFrame:
    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str
    format: str
    data: bytes

    @property
    def source_stamp_sec(self) -> float:
        return (
            float(self.stamp_sec)
            + float(self.stamp_nanosec) / 1_000_000_000.0
        )


@dataclass(frozen=True, slots=True)
class BufferedHandEvidence:
    """One executed CAM4 hand-detector result retained by source time."""

    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    hand_count: int
    depth_source: str

    @property
    def source_stamp_sec(self) -> float:
        return (
            float(self.stamp_sec)
            + float(self.stamp_nanosec) / 1_000_000_000.0
        )


def closest_aligned_hand_evidence(
    samples: list[BufferedHandEvidence] | deque[BufferedHandEvidence],
    reference_stamp_sec: float,
    *,
    max_skew_sec: float,
    max_receive_age_sec: float,
    now_monotonic: float | None = None,
) -> BufferedHandEvidence | None:
    """Return a fresh nearest CAM4 hand result, never an inferred absence."""

    if not samples:
        return None
    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    fresh = [
        sample
        for sample in samples
        if 0.0 <= now - sample.received_monotonic <= max_receive_age_sec
    ]
    if not fresh:
        return None
    closest = min(
        fresh,
        key=lambda sample: abs(
            sample.source_stamp_sec - float(reference_stamp_sec)
        ),
    )
    if (
        abs(closest.source_stamp_sec - float(reference_stamp_sec))
        > max_skew_sec
    ):
        return None
    return closest


def _parse_normalized_roi(
    value: str,
    *,
    label: str,
) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in str(value).split(","))
    except ValueError as exc:
        raise ValueError(f"{label} must be x0,y0,x1,y1 fractions") from exc
    if (
        len(parts) != 4
        or not all(number == number for number in parts)
        or not (0.0 <= parts[0] < parts[2] <= 1.0)
        or not (0.0 <= parts[1] < parts[3] <= 1.0)
    ):
        raise ValueError(
            f"{label} must be four ordered fractions within [0,1]"
        )
    return parts


def cam4_mayo_semantics_for_region(
    public_semantics: dict[str, Any],
    *,
    diagnostics: Any,
    image: Any,
    roi_norm: tuple[float, float, float, float],
    min_bbox_overlap: float,
) -> dict[str, Any] | None:
    """Project only boxes physically inside the calibrated Mayo zone.

    The full detector summary remains available to the VLM. This private
    projection retains no coordinates and is used only for deterministic Mayo
    placement tracking.
    """

    diagnostic = diagnostics if isinstance(diagnostics, dict) else {}
    image_meta = image if isinstance(image, dict) else {}
    raw_instances = diagnostic.get("instances")
    try:
        width = float(image_meta.get("width", 0))
        height = float(image_meta.get("height", 0))
    except (TypeError, ValueError):
        return None
    if not isinstance(raw_instances, list) or width <= 0.0 or height <= 0.0:
        return None
    x0_roi, y0_roi, x1_roi, y1_roi = roi_norm
    roi_px = (
        x0_roi * width,
        y0_roi * height,
        x1_roi * width,
        y1_roi * height,
    )
    threshold = min(max(float(min_bbox_overlap), 0.0), 1.0)
    filtered: list[dict[str, Any]] = []
    for row in raw_instances:
        if not isinstance(row, dict):
            continue
        box = row.get("xyxy")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(value) for value in box)
        except (TypeError, ValueError):
            continue
        if not (x1 > x0 and y1 > y0):
            continue
        center_x = (x0 + x1) / 2.0
        center_y = (y0 + y1) / 2.0
        overlap_width = max(
            0.0,
            min(x1, roi_px[2]) - max(x0, roi_px[0]),
        )
        overlap_height = max(
            0.0,
            min(y1, roi_px[3]) - max(y0, roi_px[1]),
        )
        overlap_fraction = (
            overlap_width * overlap_height / ((x1 - x0) * (y1 - y0))
        )
        if not (
            roi_px[0] <= center_x <= roi_px[2]
            and roi_px[1] <= center_y <= roi_px[3]
            and overlap_fraction >= threshold
        ):
            continue
        filtered.append(row)
    return summarize_cam4_detections(
        filtered,
        source_stamp_sec=float(public_semantics.get("source_stamp_sec", 0.0)),
        inference_latency_ms=float(
            public_semantics.get("inference_latency_ms", 0.0)
        ),
    )


def closest_aligned_frame(
    frames: list[BufferedFrame] | deque[BufferedFrame],
    reference_stamp_sec: float,
    max_skew_sec: float,
) -> BufferedFrame | None:
    if not frames:
        return None
    closest = min(
        frames,
        key=lambda frame: abs(frame.source_stamp_sec - reference_stamp_sec),
    )
    if abs(closest.source_stamp_sec - reference_stamp_sec) > max_skew_sec:
        return None
    return closest


def _nonnegative_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result < 0.0 or result != result or result in (float("inf"), float("-inf")):
        return 0.0
    return round(result, 3)


def _camera_model_identity(
    diagnostic: dict[str, Any],
    *,
    view: str,
) -> tuple[str, str] | None:
    """Return the service-attested immutable camera-model identity.

    Typed observations are a data-plane contract, not a UI label.  The
    service validates its CAM3/CAM4 inventory checkpoint before startup and
    attaches the resulting run-relative checkpoint id plus full SHA-256 to
    each response.  Do not substitute a mutable ``local-v1`` fallback when a
    response lacks that provenance: the observation is then not attributable
    to the reviewed model and is intentionally withheld.
    """

    raw_model_version = str(diagnostic.get("model_version", "")).strip()
    normalized_model = "".join(
        character
        for character in raw_model_version.casefold()
        if character.isalnum()
    )
    provenance = diagnostic.get("model_provenance")
    if "rfdetr" not in normalized_model or not isinstance(provenance, dict):
        return None
    checkpoint_id = str(provenance.get("checkpoint_id", "")).strip()
    sha256 = str(provenance.get("sha256", "")).strip().lower()
    training_run = str(provenance.get("training_run", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,159}", checkpoint_id):
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", training_run):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None
    # ``_camera_checkpoint_provenance`` emits this path as
    # ``<training_run>/<checkpoint-name>``.  Binding both fields prevents a
    # service from pairing a valid digest with a different named training run.
    if not checkpoint_id.startswith(f"{training_run}/"):
        return None
    model_version = (
        f"{view.replace('_', '')}-rfdetr:{checkpoint_id}@sha256:{sha256}"
    )

    raw_ontology_version = str(diagnostic.get("ontology_version", "")).strip()
    ontology_version = (
        raw_ontology_version[:80]
        if raw_ontology_version
        else "rfdetr-cam3-cam4-tool-v1"
    )
    return model_version, ontology_version


def build_contract_diagnostics(
    raw: Any,
    *,
    cam4: BufferedFrame | None,
    sequence: int,
    source_to_output_latency_ms: float,
) -> dict[str, Any]:
    """Project the local service result onto the CV-team diagnostics schema."""

    payload = raw if isinstance(raw, dict) else {}
    cam4_payload = payload.get("cam4")
    cam4_diag = cam4_payload if isinstance(cam4_payload, dict) else {}
    instances = cam4_diag.get("instances")
    instance_rows = instances if isinstance(instances, list) else []
    if cam4 is None:
        stamp_sec = 0
        stamp_nanosec = 0
        frame_id = ""
        observation_id = ""
        error_code = "NO_ALIGNED_CAM4"
        error_message = "no CAM4 frame satisfied the alignment policy"
    else:
        stamp_sec = int(cam4.stamp_sec)
        stamp_nanosec = int(cam4.stamp_nanosec)
        frame_id = str(cam4.frame_id)
        observation_id = f"cam4:{stamp_sec}:{stamp_nanosec}"
        error_code = ""
        error_message = ""
    return {
        "schema": "pnu.rfdetr_diagnostics.v2",
        "view": "cam4",
        "source_stamp_sec": stamp_sec,
        "source_stamp_nanosec": stamp_nanosec,
        "frame_id": frame_id,
        "observation_id": observation_id,
        "sequence": max(0, int(sequence)),
        "decode_latency_ms": _nonnegative_float(payload.get("decode_latency_ms")),
        "depth_to_xyz_latency_ms": 0.0,
        "inference_latency_ms": _nonnegative_float(
            cam4_diag.get("inference_latency_ms")
        ),
        "pose_latency_ms": 0.0,
        "render_encode_latency_ms": _nonnegative_float(
            payload.get("render_encode_latency_ms")
        ),
        "source_to_output_latency_ms": _nonnegative_float(
            source_to_output_latency_ms
        ),
        "queue_age_ms": 0.0,
        "dropped_frames": 0,
        "instance_count": len(instance_rows),
        "valid_pose_count": 0,
        "endpoint_sign_low_count": 0,
        "model_version": str(
            cam4_diag.get(
                "model_version",
                cam4_diag.get("model", "RFDETRSmall"),
            )
        )[:120],
        "calibration_version": "",
        "error_code": error_code,
        "error_message": error_message,
    }


def build_camera_tool_observations(
    *,
    frame: BufferedFrame,
    view: str,
    sequence: int,
    diagnostics: Any,
    image: Any,
) -> ToolObservation2DArray | None:
    """Create a typed, bounding-box RF-DETR result for one D455 view.

    The local RF-DETR checkpoints are object detectors, not segmenters.  The
    message therefore declares no mask encoding and carries the detected box
    as the valid 2-D evidence.  Consumers deliberately use this bounded
    projection rather than a rendered overlay or invented pixel masks.
    """

    normalized_view = str(view).strip()
    if normalized_view not in {"cam_3", "cam_4"}:
        return None
    diagnostic = diagnostics if isinstance(diagnostics, dict) else {}
    image_meta = image if isinstance(image, dict) else {}
    try:
        width = int(image_meta.get("width", 0))
        height = int(image_meta.get("height", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    raw_instances = diagnostic.get("instances")
    if not isinstance(raw_instances, list):
        return None
    model_identity = _camera_model_identity(
        diagnostic,
        view=normalized_view,
    )
    if model_identity is None:
        return None
    model_version, ontology_version = model_identity

    message = ToolObservation2DArray()
    message.header.stamp.sec = int(frame.stamp_sec)
    message.header.stamp.nanosec = int(frame.stamp_nanosec)
    message.header.frame_id = str(frame.frame_id)
    message.sequence = max(0, int(sequence))
    message.schema_version = RFDETR_TOOL_OBSERVATIONS_SCHEMA
    message.observation_id = (
        f"{normalized_view}:{int(frame.stamp_sec)}:{int(frame.stamp_nanosec)}"
    )
    message.view = normalized_view
    message.image_width = width
    message.image_height = height
    message.model_version = model_version
    message.ontology_version = ontology_version

    instances: list[ToolObservation2D] = []
    for local_id, row in enumerate(raw_instances, start=1):
        if not isinstance(row, dict):
            return None
        box = row.get("xyxy")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return None
        try:
            x0, y0, x1, y1 = (float(value) for value in box)
            confidence = float(row.get("confidence"))
            class_index = int(row.get("class_id"))
        except (TypeError, ValueError):
            return None
        class_name = str(row.get("class_name", "")).strip()
        x0 = min(max(x0, 0.0), float(width))
        y0 = min(max(y0, 0.0), float(height))
        x1 = min(max(x1, 0.0), float(width))
        y1 = min(max(y1, 0.0), float(height))
        if not class_name or not 0.0 < confidence <= 1.0 or x1 <= x0 or y1 <= y0:
            return None
        instance = ToolObservation2D()
        instance.frame_local_instance_id = local_id
        instance.canonical_class_id = max(0, class_index)
        instance.model_class_index = max(0, class_index)
        instance.class_name = class_name
        instance.class_confidence = confidence
        instance.segmentation_confidence = 0.0
        instance.bbox_xyxy_px = [x0, y0, x1, y1]
        instance.mask_bbox_xyxy_px = [x0, y0, x1, y1]
        instance.mask_area_px = 0
        instance.observation_point_valid = False
        instance.observation_point_inside_mask = False
        instance.observation_point_depth_valid = False
        instance.observation_point_selection_mode = "bbox_only_detector"
        instance.mask_encoding = ToolObservation2D.MASK_ENCODING_INVALID
        instances.append(instance)
    message.instances = instances
    return message


class RFDETRBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("rfdetr_perception_bridge")
        self._service_url = str(
            self.declare_parameter(
                "service_url",
                "http://127.0.0.1:8010",
            ).value
        ).rstrip("/")
        self._flir_input_topic = str(
            self.declare_parameter(
                "flir_input_topic",
                "/surgery/images/flir/compressed",
            ).value
        )
        self._cam4_input_topic = str(
            self.declare_parameter(
                "cam4_input_topic",
                "/surgery/images/cam4/compressed",
            ).value
        ).strip()
        self._cam3_input_topic = str(
            self.declare_parameter(
                "cam3_input_topic",
                "",
            ).value
        ).strip()
        self._flir_output_topic = str(
            self.declare_parameter(
                "flir_output_topic",
                "/surgery/images/flir/segmented/compressed",
            ).value
        )
        self._cam4_output_topic = str(
            self.declare_parameter(
                "cam4_output_topic",
                "/surgery/images/cam4/detected/compressed",
            ).value
        )
        self._flir_overlay_topic = str(
            self.declare_parameter(
                "flir_overlay_topic",
                "/surgery/images/flir/segmentation_overlay/compressed",
            ).value
        )
        self._cam4_overlay_topic = str(
            self.declare_parameter(
                "cam4_overlay_topic",
                "/surgery/images/cam4/detection_overlay/compressed",
            ).value
        ).strip()
        self._cam3_overlay_topic = str(
            self.declare_parameter(
                "cam3_overlay_topic",
                "",
            ).value
        ).strip()
        self._cam4_semantics_topic = str(
            self.declare_parameter(
                "cam4_semantics_topic",
                "/surgery/perception/cam4/semantics/json",
            ).value
        )
        self._cam4_mayo_observation_topic = str(
            self.declare_parameter(
                "cam4_mayo_observation_topic",
                "/surgery/perception/cam4/mayo_tool_observations",
            ).value
        )
        self._cam3_tool_observations_topic = str(
            self.declare_parameter(
                "cam3_tool_observations_topic",
                "/taskplanner/internal/rfdetr/cam_3/tool/observations",
            ).value
        ).strip()
        self._cam4_tool_observations_topic = str(
            self.declare_parameter(
                "cam4_tool_observations_topic",
                "/taskplanner/internal/rfdetr/cam_4/tool/observations",
            ).value
        ).strip()
        self._cam4_hand_keypoints_topic = str(
            self.declare_parameter(
                "cam4_hand_keypoints_topic",
                "/perception/cam_4/hand/keypoints",
            ).value
        ).strip()
        self._cam4_hand_alignment_max_skew_sec = max(
            0.0,
            float(
                self.declare_parameter(
                    "cam4_hand_alignment_max_skew_sec",
                    0.10,
                ).value
            ),
        )
        self._cam4_hand_evidence_max_age_sec = max(
            0.1,
            float(
                self.declare_parameter(
                    "cam4_hand_evidence_max_age_sec",
                    1.5,
                ).value
            ),
        )
        self._cam4_mayo_roi_norm = _parse_normalized_roi(
            str(
                self.declare_parameter(
                    "cam4_mayo_roi_norm",
                    # Calibrated from the fixed 1280x720 VIPLab CAM4 view on
                    # 2026-08-24. This encloses the blue Mayo pad while
                    # excluding the floor, robot fixture, and adjacent drape.
                    "0.30,0.22,0.66,0.75",
                ).value
            ),
            label="CAM4 Mayo ROI",
        )
        self._cam4_mayo_roi_version = str(
            self.declare_parameter(
                "cam4_mayo_roi_version",
                "viplab-cam4-mayo-1280x720-20260824",
            ).value
        ).strip()
        self._cam4_mayo_min_bbox_overlap = min(
            1.0,
            max(
                0.0,
                float(
                    self.declare_parameter(
                        "cam4_mayo_min_bbox_overlap",
                        0.50,
                    ).value
                ),
            ),
        )
        self._diagnostics_topic = str(
            self.declare_parameter(
                "diagnostics_topic",
                "/surgery/perception/rfdetr/diagnostics/json",
            ).value
        )
        self._health_topic = str(
            self.declare_parameter(
                "health_topic",
                "/surgery/perception/rfdetr/health",
            ).value
        )
        self._max_source_skew_sec = max(
            0.0,
            float(
                self.declare_parameter(
                    "max_source_skew_sec",
                    0.1,
                ).value
            ),
        )
        self._request_timeout_sec = max(
            0.1,
            float(
                self.declare_parameter(
                    "request_timeout_sec",
                    5.0,
                ).value
            ),
        )
        self._max_rate_hz = max(
            0.1,
            float(self.declare_parameter("max_rate_hz", 15.0).value),
        )
        self._segmented_output_rate_hz = max(
            0.1,
            float(
                self.declare_parameter(
                    "segmented_output_rate_hz",
                    2.0,
                ).value
            ),
        )
        self._enabled = bool(
            self.declare_parameter("enabled", True).value
        )
        self._generation = 0
        # ToolObservation already carries a uint64 epoch/sequence contract.
        # Bind the Mayo stream to one process/run epoch so a bridge restart
        # cannot replay sequence 1 as a continuation of the previous process.
        self._cam4_mayo_source_epoch = max(1, time.time_ns())
        self._cam4_mayo_source_sequence = 0

        contract_overlay_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=2,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        contract_diagnostics_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        contract_health_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self._segmented_flir_pub = self.create_publisher(
            CompressedImage,
            self._flir_output_topic,
            qos_profile_sensor_data,
        )
        self._detected_cam4_pub = self.create_publisher(
            CompressedImage,
            self._cam4_output_topic,
            qos_profile_sensor_data,
        )
        self._flir_overlay_pub = self.create_publisher(
            CompressedImage,
            self._flir_overlay_topic,
            # Rendered overlays are a low-latency, latest-frame visual layer.
            # Keeping only two best-effort frames prevents a slow operating
            # browser from accumulating stale WebP overlays behind the raw
            # camera feed.
            contract_overlay_qos,
        )
        self._cam4_overlay_pub = self.create_publisher(
            CompressedImage,
            self._cam4_overlay_topic,
            contract_overlay_qos,
        )
        self._cam3_overlay_pub = (
            self.create_publisher(
                CompressedImage,
                self._cam3_overlay_topic,
                contract_overlay_qos,
            )
            if self._cam3_overlay_topic
            else None
        )
        self._cam4_semantics_pub = self.create_publisher(
            String,
            self._cam4_semantics_topic,
            10,
        )
        self._cam4_mayo_observation_pub = self.create_publisher(
            ToolObservation,
            self._cam4_mayo_observation_topic,
            30,
        )
        self._cam3_tool_observations_pub = (
            self.create_publisher(
                ToolObservation2DArray,
                self._cam3_tool_observations_topic,
                qos_profile_sensor_data,
            )
            if self._cam3_tool_observations_topic
            else None
        )
        self._cam4_tool_observations_pub = (
            self.create_publisher(
                ToolObservation2DArray,
                self._cam4_tool_observations_topic,
                qos_profile_sensor_data,
            )
            if self._cam4_tool_observations_topic
            else None
        )
        self._diagnostics_pub = self.create_publisher(
            String,
            self._diagnostics_topic,
            contract_diagnostics_qos,
        )
        self._health_pub = self.create_publisher(
            String,
            self._health_topic,
            contract_health_qos,
        )
        self.create_subscription(
            CompressedImage,
            self._flir_input_topic,
            self._on_flir,
            qos_profile_sensor_data,
        )
        self._control_service = self.create_service(
            SetBool,
            "~/set_enabled",
            self._set_enabled,
        )
        self.create_subscription(
            CompressedImage,
            self._cam4_input_topic,
            self._on_cam4,
            qos_profile_sensor_data,
        )
        if self._cam3_input_topic:
            self.create_subscription(
                CompressedImage,
                self._cam3_input_topic,
                self._on_cam3,
                qos_profile_sensor_data,
            )
        if self._cam4_hand_keypoints_topic:
            self.create_subscription(
                HandKeypoints,
                self._cam4_hand_keypoints_topic,
                self._on_cam4_hand_keypoints,
                qos_profile_sensor_data,
            )

        self._cam4_frames: deque[BufferedFrame] = deque(maxlen=48)
        self._cam3_frames: deque[BufferedFrame] = deque(maxlen=48)
        self._cam4_hand_evidence: deque[BufferedHandEvidence] = deque(
            maxlen=64
        )
        self._condition = threading.Condition()
        self._pending_flir: BufferedFrame | None = None
        self._pending_cam4: BufferedFrame | None = None
        self._pending_cam3: BufferedFrame | None = None
        # Camera inference is intentionally latest-only: retaining every RGB
        # frame while inference is slower than the source would create an
        # unbounded latency queue.  Count replacements per view so this
        # bounded backpressure is visible in health telemetry rather than
        # being reported as a request/alignment error.
        self._camera_coalesced_frames = {"cam_3": 0, "cam_4": 0}
        self._running = True
        self._session = requests.Session()
        self._cam4_mayo_tracker = Cam4MayoPlacementTracker()
        self._last_request_started = 0.0
        self._last_success_monotonic = 0.0
        self._last_segmented_requested_monotonic = 0.0
        self._diagnostics_sequence = 0
        self._tool_observation_sequences = {"cam_3": 0, "cam_4": 0}
        # A CAM4 or FLIR callback can select the same aligned CAM3/CAM4
        # frame more than once while requests are coalesced.  Typed evidence
        # is a source-time lease, so publishing that duplicate would correctly
        # be rejected by preflight as a replay.  Keep this per-view, rather
        # than coupling the two camera clocks.
        self._last_tool_observation_source_stamps: dict[
            str, tuple[int, int] | None
        ] = {"cam_3": None, "cam_4": None}
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="rfdetr-bridge-worker",
            daemon=True,
        )
        self._worker.start()
        self._health_timer = self.create_timer(1.0, self._publish_idle_health)

    @staticmethod
    def _buffered(msg: CompressedImage) -> BufferedFrame:
        return BufferedFrame(
            received_monotonic=time.monotonic(),
            stamp_sec=int(msg.header.stamp.sec),
            stamp_nanosec=int(msg.header.stamp.nanosec),
            frame_id=str(msg.header.frame_id),
            format=str(msg.format or "jpeg"),
            data=bytes(msg.data),
        )

    def _on_cam4(self, msg: CompressedImage) -> None:
        with self._condition:
            if not self._enabled:
                return
            frame = self._buffered(msg)
            self._cam4_frames.append(frame)
            if self._pending_cam4 is not None:
                self._camera_coalesced_frames["cam_4"] += 1
            # CAM3 and CAM4 are independent local tool-recognition clocks.
            # A delayed view is processed on its own and is never rejected
            # merely because the other camera has a different source stamp.
            self._pending_cam4 = frame
            self._condition.notify()

    def _on_cam4_hand_keypoints(self, msg: HandKeypoints) -> None:
        """Buffer an executed hand result without treating silence as clear."""

        stamp_sec = int(msg.header.stamp.sec)
        stamp_nanosec = int(msg.header.stamp.nanosec)
        if stamp_sec < 0 or not 0 <= stamp_nanosec < 1_000_000_000:
            return
        if stamp_sec == 0 and stamp_nanosec == 0:
            return
        evidence = BufferedHandEvidence(
            received_monotonic=time.monotonic(),
            stamp_sec=stamp_sec,
            stamp_nanosec=stamp_nanosec,
            hand_count=max(0, len(msg.hands)),
            depth_source=str(msg.depth_source or "").strip(),
        )
        with self._condition:
            if not self._enabled:
                return
            samples = getattr(self, "_cam4_hand_evidence", None)
            if samples is None:
                samples = deque(maxlen=64)
                self._cam4_hand_evidence = samples
            if samples and evidence.source_stamp_sec <= samples[-1].source_stamp_sec:
                # A repeated source frame is not a new release decision. A
                # backwards camera clock stays fenced until explicit reset.
                return
            samples.append(evidence)

    def _on_cam3(self, msg: CompressedImage) -> None:
        with self._condition:
            if not self._enabled:
                return
            frame = self._buffered(msg)
            self._cam3_frames.append(frame)
            if self._pending_cam3 is not None:
                self._camera_coalesced_frames["cam_3"] += 1
            self._pending_cam3 = frame
            self._condition.notify()

    def _on_flir(self, msg: CompressedImage) -> None:
        with self._condition:
            if not self._enabled:
                return
            # Latest-frame coalescing prevents an inference backlog during replay.
            self._pending_flir = self._buffered(msg)
            self._condition.notify()

    def _set_enabled(
        self,
        request: SetBool.Request,
        response: SetBool.Response,
    ) -> SetBool.Response:
        enabled = bool(request.data)
        with self._condition:
            changed = self._enabled != enabled
            self._enabled = enabled
            if changed:
                self._generation += 1
                previous_epoch = max(
                    0,
                    int(getattr(self, "_cam4_mayo_source_epoch", 0)),
                )
                self._cam4_mayo_source_epoch = max(
                    previous_epoch + 1,
                    time.time_ns(),
                )
                self._cam4_mayo_source_sequence = 0
                self._pending_flir = None
                self._pending_cam4 = None
                if hasattr(self, "_pending_cam3"):
                    self._pending_cam3 = None
                self._cam4_frames.clear()
                hand_evidence = getattr(
                    self,
                    "_cam4_hand_evidence",
                    None,
                )
                if hand_evidence is not None:
                    hand_evidence.clear()
                # Keep this method usable by narrow unit-test doubles that
                # model the pre-CAM3 bridge state.
                cam3_frames = getattr(self, "_cam3_frames", None)
                if cam3_frames is not None:
                    cam3_frames.clear()
                source_stamps = getattr(
                    self,
                    "_last_tool_observation_source_stamps",
                    None,
                )
                if isinstance(source_stamps, dict):
                    source_stamps.clear()
                    source_stamps.update({"cam_3": None, "cam_4": None})
                self._last_segmented_requested_monotonic = 0.0
                self._cam4_mayo_tracker.reset()
            self._condition.notify_all()
        response.success = True
        response.message = (
            "RF-DETR object recognition enabled; waiting for fresh frames"
            if enabled
            else "RF-DETR object recognition disabled; raw views remain available"
        )
        self._publish_health(
            connected=False,
            status="waiting_for_frame" if enabled else "disabled",
            latency_ms=0.0,
            pair_skew_sec=None,
            error="",
        )
        return response

    def _generation_is_active(self, generation: int) -> bool:
        with self._condition:
            return self._enabled and self._generation == generation

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        not self._running
                        or (
                            self._enabled
                            and (
                                self._pending_flir is not None
                                or self._pending_cam4 is not None
                                or self._pending_cam3 is not None
                            )
                        )
                    )
                )
                if not self._running:
                    return
                flir = self._pending_flir
                self._pending_flir = None
                triggered_cam4 = self._pending_cam4
                self._pending_cam4 = None
                triggered_cam3 = self._pending_cam3
                self._pending_cam3 = None
                generation = self._generation
                camera_triggered = (
                    triggered_cam4 is not None or triggered_cam3 is not None
                )
                if camera_triggered:
                    # Use the frames that actually triggered this request,
                    # even when their source stamps differ by more than the
                    # FLIR alignment tolerance.  Missing views remain absent
                    # instead of reusing already-processed camera frames.
                    cam4 = triggered_cam4
                    cam3 = triggered_cam3
                    # When FLIR happened to coalesce into the same wake-up,
                    # preserve its old nearest-frame behavior for a camera
                    # view that did not independently trigger.
                    if flir is not None and cam4 is None:
                        cam4 = closest_aligned_frame(
                            self._cam4_frames,
                            flir.source_stamp_sec,
                            self._max_source_skew_sec,
                        )
                    if (
                        flir is not None
                        and cam3 is None
                        and self._cam3_input_topic
                    ):
                        cam3 = closest_aligned_frame(
                            self._cam3_frames,
                            flir.source_stamp_sec,
                            self._max_source_skew_sec,
                        )
                else:
                    # FLIR-only wake-ups retain the original bounded nearest
                    # source-stamp selection for the optional camera context.
                    cam4 = (
                        closest_aligned_frame(
                            self._cam4_frames,
                            flir.source_stamp_sec,
                            self._max_source_skew_sec,
                        )
                        if flir is not None
                        else None
                    )
                    cam3 = (
                        closest_aligned_frame(
                            self._cam3_frames,
                            flir.source_stamp_sec,
                            self._max_source_skew_sec,
                        )
                        if flir is not None and self._cam3_input_topic
                        else None
                    )
            if flir is None and cam4 is None and cam3 is None:
                continue
            minimum_period = 1.0 / self._max_rate_hz
            delay = minimum_period - (
                time.monotonic() - self._last_request_started
            )
            if delay > 0.0:
                time.sleep(delay)
            self._last_request_started = time.monotonic()
            self._process_pair(flir, cam4, cam3, generation)

    def _process_pair(
        self,
        flir: BufferedFrame | None,
        cam4: BufferedFrame | None,
        cam3: BufferedFrame | None,
        generation: int,
    ) -> None:
        now_monotonic = time.monotonic()
        segmented_period = 1.0 / self._segmented_output_rate_hz
        include_flir_segmented = flir is not None and (
            self._last_segmented_requested_monotonic <= 0.0
            or now_monotonic - self._last_segmented_requested_monotonic
            >= segmented_period
        )
        if include_flir_segmented:
            self._last_segmented_requested_monotonic = now_monotonic
        request_payload: dict[str, Any] = {
            "include_flir_segmented_image": include_flir_segmented,
            # The browser composites this service's transparent overlay over
            # the raw CAM4 frame, so another full-frame JPEG is redundant.
            "include_cam4_annotated_image": False,
            # CAM3 uses the same low-bandwidth transparent-overlay path.
            "include_cam3_annotated_image": False,
        }
        if flir is not None:
            request_payload["flir_stamp_sec"] = flir.source_stamp_sec
            request_payload["flir_image_base64"] = base64.b64encode(
                flir.data
            ).decode("ascii")
        pair_skew_sec: float | None = 0.0 if cam4 is not None and flir is None else None
        cam3_pair_skew_sec: float | None = None
        if cam4 is not None:
            request_payload["cam4_stamp_sec"] = cam4.source_stamp_sec
            request_payload["cam4_image_base64"] = base64.b64encode(
                cam4.data
            ).decode("ascii")
            if flir is not None:
                pair_skew_sec = abs(
                    cam4.source_stamp_sec - flir.source_stamp_sec
                )
        if cam3 is not None:
            request_payload["cam3_stamp_sec"] = cam3.source_stamp_sec
            request_payload["cam3_image_base64"] = base64.b64encode(
                cam3.data
            ).decode("ascii")
            if flir is not None:
                cam3_pair_skew_sec = abs(
                    cam3.source_stamp_sec - flir.source_stamp_sec
                )
            elif cam4 is not None:
                cam3_pair_skew_sec = abs(
                    cam3.source_stamp_sec - cam4.source_stamp_sec
                )
            else:
                # An independently delayed CAM3 frame is still a valid CAM3
                # observation; zero denotes no cross-view pairing requirement.
                cam3_pair_skew_sec = 0.0
        started = time.perf_counter()
        try:
            response = self._session.post(
                f"{self._service_url}/v1/perceive",
                json=request_payload,
                timeout=self._request_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("schema")
                != "taskplanner.rfdetr_perception.v1"
            ):
                raise RuntimeError("unexpected RF-DETR service response")
            segmented = payload.get("flir_segmented_image")
            image_bytes: bytes | None = None
            if include_flir_segmented:
                if not isinstance(segmented, dict):
                    raise RuntimeError("RF-DETR response omitted segmented FLIR")
                encoded_image = segmented.get("data_base64")
                if not isinstance(encoded_image, str) or not encoded_image:
                    raise RuntimeError("RF-DETR response has no FLIR image bytes")
                image_bytes = base64.b64decode(encoded_image, validate=True)
            if not self._generation_is_active(generation):
                return

            if image_bytes is not None and flir is not None:
                image_msg = CompressedImage()
                image_msg.header.stamp.sec = flir.stamp_sec
                image_msg.header.stamp.nanosec = flir.stamp_nanosec
                image_msg.header.frame_id = append_rfdetr_frame_marker(
                    flir.frame_id,
                    RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
                )
                image_msg.format = "jpeg"
                image_msg.data = image_bytes
                self._segmented_flir_pub.publish(image_msg)

            flir_overlay = payload.get("flir_overlay_image")
            if flir is not None and isinstance(flir_overlay, dict):
                encoded_overlay = flir_overlay.get("data_base64")
                if isinstance(encoded_overlay, str) and encoded_overlay:
                    overlay_msg = CompressedImage()
                    overlay_msg.header.stamp.sec = flir.stamp_sec
                    overlay_msg.header.stamp.nanosec = flir.stamp_nanosec
                    overlay_msg.header.frame_id = append_rfdetr_frame_marker(
                        flir.frame_id,
                        RFDETR_FLIR_OVERLAY_FRAME_MARKER,
                    )
                    overlay_msg.format = str(
                        flir_overlay.get("mime_type", "image/webp")
                    ).removeprefix("image/")
                    overlay_msg.data = base64.b64decode(
                        encoded_overlay,
                        validate=True,
                    )
                    self._flir_overlay_pub.publish(overlay_msg)

            annotated_cam4 = payload.get("cam4_annotated_image")
            if cam4 is not None and isinstance(annotated_cam4, dict):
                encoded_cam4 = annotated_cam4.get("data_base64")
                if isinstance(encoded_cam4, str) and encoded_cam4:
                    cam4_msg = CompressedImage()
                    cam4_msg.header.stamp.sec = cam4.stamp_sec
                    cam4_msg.header.stamp.nanosec = cam4.stamp_nanosec
                    cam4_msg.header.frame_id = (
                        f"{cam4.frame_id}|rfdetr_bbox"
                        if cam4.frame_id
                        else "cam4_rfdetr_bbox"
                    )
                    cam4_msg.format = "jpeg"
                    cam4_msg.data = base64.b64decode(
                        encoded_cam4,
                        validate=True,
                    )
                    self._detected_cam4_pub.publish(cam4_msg)

            cam4_overlay = payload.get("cam4_overlay_image")
            if cam4 is not None and isinstance(cam4_overlay, dict):
                encoded_cam4_overlay = cam4_overlay.get("data_base64")
                if (
                    isinstance(encoded_cam4_overlay, str)
                    and encoded_cam4_overlay
                ):
                    cam4_overlay_msg = CompressedImage()
                    cam4_overlay_msg.header.stamp.sec = cam4.stamp_sec
                    cam4_overlay_msg.header.stamp.nanosec = cam4.stamp_nanosec
                    cam4_overlay_msg.header.frame_id = append_rfdetr_frame_marker(
                        cam4.frame_id,
                        RFDETR_CAM4_OVERLAY_FRAME_MARKER,
                    )
                    cam4_overlay_msg.format = str(
                        cam4_overlay.get("mime_type", "image/webp")
                    ).removeprefix("image/")
                    cam4_overlay_msg.data = base64.b64decode(
                        encoded_cam4_overlay,
                        validate=True,
                    )
                    self._cam4_overlay_pub.publish(cam4_overlay_msg)

            cam3_overlay = payload.get("cam3_overlay_image")
            if (
                cam3 is not None
                and self._cam3_overlay_pub is not None
                and isinstance(cam3_overlay, dict)
            ):
                encoded_cam3_overlay = cam3_overlay.get("data_base64")
                if (
                    isinstance(encoded_cam3_overlay, str)
                    and encoded_cam3_overlay
                ):
                    cam3_overlay_msg = CompressedImage()
                    cam3_overlay_msg.header.stamp.sec = cam3.stamp_sec
                    cam3_overlay_msg.header.stamp.nanosec = cam3.stamp_nanosec
                    cam3_overlay_msg.header.frame_id = append_rfdetr_frame_marker(
                        cam3.frame_id,
                        RFDETR_CAM3_OVERLAY_FRAME_MARKER,
                    )
                    cam3_overlay_msg.format = str(
                        cam3_overlay.get("mime_type", "image/webp")
                    ).removeprefix("image/")
                    cam3_overlay_msg.data = base64.b64decode(
                        encoded_cam3_overlay,
                        validate=True,
                    )
                    self._cam3_overlay_pub.publish(cam3_overlay_msg)

            semantics = payload.get("cam4_semantics")
            if isinstance(semantics, dict):
                semantics_json = json.dumps(
                    semantics,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                public_semantics = parse_cam4_semantics_json(semantics_json)
                if not public_semantics:
                    raise RuntimeError("invalid CAM4 semantic summary")
                semantics_msg = String()
                semantics_msg.data = semantics_json
                self._cam4_semantics_pub.publish(semantics_msg)
                self._publish_cam4_mayo_observations(
                    public_semantics,
                    diagnostics=(
                        payload.get("diagnostics", {}).get("cam4")
                        if isinstance(payload.get("diagnostics"), dict)
                        else None
                    ),
                    image=payload.get("cam4_overlay_image"),
                )

            self._publish_camera_tool_observations(
                frame=cam3,
                view="cam_3",
                diagnostics=payload.get("diagnostics", {}).get("cam3")
                if isinstance(payload.get("diagnostics"), dict)
                else None,
                image=payload.get("cam3_overlay_image"),
                publisher=self._cam3_tool_observations_pub,
            )
            self._publish_camera_tool_observations(
                frame=cam4,
                view="cam_4",
                diagnostics=payload.get("diagnostics", {}).get("cam4")
                if isinstance(payload.get("diagnostics"), dict)
                else None,
                image=payload.get("cam4_overlay_image"),
                publisher=self._cam4_tool_observations_pub,
            )

            latency_ms = (time.perf_counter() - started) * 1000.0
            # This legacy diagnostics schema is explicitly CAM4-scoped.  A
            # valid CAM3-only request must not manufacture NO_ALIGNED_CAM4 as
            # an error merely because CAM3 arrived later and ran alone.
            if cam4 is not None or flir is not None:
                self._diagnostics_sequence += 1
                diagnostics_msg = String()
                diagnostics_msg.data = json.dumps(
                    build_contract_diagnostics(
                        payload.get("diagnostics", {}),
                        cam4=cam4,
                        sequence=self._diagnostics_sequence,
                        source_to_output_latency_ms=latency_ms,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                self._diagnostics_pub.publish(diagnostics_msg)
            self._last_success_monotonic = time.monotonic()
            self._publish_health(
                connected=True,
                status="ready",
                latency_ms=latency_ms,
                pair_skew_sec=pair_skew_sec,
                cam3_pair_skew_sec=cam3_pair_skew_sec,
                error="",
            )
        except Exception as exc:
            if not self._generation_is_active(generation):
                return
            self._publish_health(
                connected=False,
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                pair_skew_sec=pair_skew_sec,
                cam3_pair_skew_sec=cam3_pair_skew_sec,
                error=str(exc),
            )
            self.get_logger().warning(
                f"RF-DETR preprocessing failed: {exc}",
                throttle_duration_sec=3.0,
            )

    def _publish_camera_tool_observations(
        self,
        *,
        frame: BufferedFrame | None,
        view: str,
        diagnostics: Any,
        image: Any,
        publisher: Any,
    ) -> None:
        """Publish the local CAM3/CAM4 RF-DETR output as typed evidence."""

        if frame is None or publisher is None:
            return
        source_stamp = (int(frame.stamp_sec), int(frame.stamp_nanosec))
        source_stamps = getattr(
            self,
            "_last_tool_observation_source_stamps",
            None,
        )
        if not isinstance(source_stamps, dict):
            source_stamps = {"cam_3": None, "cam_4": None}
            self._last_tool_observation_source_stamps = source_stamps
        previous_stamp = source_stamps.get(view)
        if previous_stamp is not None and source_stamp <= previous_stamp:
            # Do not let a duplicated or delayed worker response poison the
            # consumer's source-time lease.  A genuine camera clock reset
            # remains fail-closed until this bridge is explicitly reset.
            self.get_logger().warning(
                "dropping non-monotonic RF-DETR typed observation "
                f"for {view}: {source_stamp} <= {previous_stamp}",
                throttle_duration_sec=3.0,
            )
            return
        next_sequence = int(self._tool_observation_sequences.get(view, 0)) + 1
        message = build_camera_tool_observations(
            frame=frame,
            view=view,
            sequence=next_sequence,
            diagnostics=diagnostics,
            image=image,
        )
        if message is None:
            self.get_logger().warning(
                f"RF-DETR response omitted valid {view} typed observations",
                throttle_duration_sec=3.0,
            )
            return
        self._tool_observation_sequences[view] = next_sequence
        source_stamps[view] = source_stamp
        publisher.publish(message)

    def _publish_cam4_mayo_observations(
        self,
        public_semantics: dict[str, Any],
        *,
        diagnostics: Any = None,
        image: Any = None,
    ) -> None:
        """Publish hand-released, ROI-bounded Mayo placement episodes."""

        try:
            source_stamp_sec = float(public_semantics["source_stamp_sec"])
        except (KeyError, TypeError, ValueError):
            self._cam4_mayo_tracker.reset()
            return
        condition = getattr(self, "_condition", None)
        if condition is None:
            samples = list(getattr(self, "_cam4_hand_evidence", ()))
        else:
            with condition:
                samples = list(getattr(self, "_cam4_hand_evidence", ()))
        hand_evidence = closest_aligned_hand_evidence(
            samples,
            source_stamp_sec,
            max_skew_sec=float(
                getattr(
                    self,
                    "_cam4_hand_alignment_max_skew_sec",
                    0.10,
                )
            ),
            max_receive_age_sec=float(
                getattr(
                    self,
                    "_cam4_hand_evidence_max_age_sec",
                    1.5,
                )
            ),
        )
        # Missing/misaligned hand output is unknown, never proof of release.
        release_confirmed = bool(
            hand_evidence is not None and hand_evidence.hand_count == 0
        )
        private_semantics = cam4_mayo_semantics_for_region(
            public_semantics,
            diagnostics=diagnostics,
            image=image,
            roi_norm=getattr(
                self,
                "_cam4_mayo_roi_norm",
                (0.30, 0.22, 0.66, 0.75),
            ),
            min_bbox_overlap=float(
                getattr(self, "_cam4_mayo_min_bbox_overlap", 0.50)
            ),
        )
        if private_semantics is None:
            self._cam4_mayo_tracker.reset()
            return
        for placement in self._cam4_mayo_tracker.update(
            private_semantics,
            release_confirmed=release_confirmed,
        ):
            source_epoch = max(
                1,
                int(getattr(self, "_cam4_mayo_source_epoch", 0))
                or time.time_ns(),
            )
            self._cam4_mayo_source_epoch = source_epoch
            source_sequence = max(
                0,
                int(getattr(self, "_cam4_mayo_source_sequence", 0)),
            ) + 1
            self._cam4_mayo_source_sequence = source_sequence
            stamp_sec = int(placement.source_stamp_sec)
            stamp_nanosec = int(
                round(
                    (placement.source_stamp_sec - stamp_sec)
                    * 1_000_000_000
                )
            )
            if stamp_nanosec >= 1_000_000_000:
                stamp_sec += 1
                stamp_nanosec -= 1_000_000_000
            observation = ToolObservation()
            observation.stamp.sec = stamp_sec
            observation.stamp.nanosec = stamp_nanosec
            observation.source = "cam4_rfdetr_mayo_observation"
            observation.source_epoch = source_epoch
            observation.source_sequence = source_sequence
            episode_started_ns = max(
                0,
                int(round(placement.presence_started_stamp_sec * 1_000_000_000)),
            )
            observation.correlation_id = (
                "cam4-rfdetr-mayo:v2:"
                f"{source_epoch}:{source_sequence}:"
                f"{episode_started_ns}:{placement.instrument_name}"
            )
            observation.instrument_id = placement.instrument_name
            observation.location_id = "mayo_stand"
            observation.location_type = "mayo_stand"
            observation.confidence = placement.confidence
            observation.visible = True
            self._cam4_mayo_observation_pub.publish(observation)

    def _publish_health(
        self,
        *,
        connected: bool,
        status: str,
        latency_ms: float,
        pair_skew_sec: float | None,
        error: str,
        cam3_pair_skew_sec: float | None = None,
    ) -> None:
        with self._condition:
            enabled = self._enabled
            coalesced = getattr(
                self,
                "_camera_coalesced_frames",
                {"cam_3": 0, "cam_4": 0},
            ).copy()
        stamp = self.get_clock().now().to_msg()
        cam4_ready = bool(connected and pair_skew_sec is not None)
        cam3_ready = bool(
            self._cam3_input_topic
            and connected
            and cam3_pair_skew_sec is not None
        )
        msg = String()
        msg.data = json.dumps(
            {
                "schema": "pnu.rfdetr_health.v2",
                "stamp_sec": int(stamp.sec),
                "stamp_nanosec": int(stamp.nanosec),
                "node": self.get_name(),
                "state": status if enabled else "disabled",
                "cam4_rgb_ready": cam4_ready,
                "cam4_camera_info_ready": False,
                "cam4_depth_ready": False,
                "cam4_calibration_ready": False,
                "cam4_pose_ready": False,
                "tray_rgb_ready": cam3_ready,
                "tray_camera_info_ready": False,
                "tray_depth_ready": False,
                "tray_calibration_ready": False,
                "tray_model_ready": cam3_ready,
                "tray_pose_ready": False,
                "model_ready": bool(connected),
                "camera_queue_policy": "latest_only",
                "cam3_coalesced_frames": max(
                    0,
                    int(coalesced.get("cam_3", 0)),
                ),
                "cam4_coalesced_frames": max(
                    0,
                    int(coalesced.get("cam_4", 0)),
                ),
                "cam4_mayo_roi_norm": list(
                    getattr(
                        self,
                        "_cam4_mayo_roi_norm",
                        (0.30, 0.22, 0.66, 0.75),
                    )
                ),
                "cam4_mayo_roi_version": str(
                    getattr(self, "_cam4_mayo_roi_version", "")
                ),
                "last_error_code": "RFDETR_REQUEST_FAILED" if error else "",
                "last_error_message": str(error)[:500],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._health_pub.publish(msg)

    def _publish_idle_health(self) -> None:
        with self._condition:
            enabled = self._enabled
        if not enabled:
            self._publish_health(
                connected=False,
                status="disabled",
                latency_ms=0.0,
                pair_skew_sec=None,
                error="",
            )
            return
        if self._last_success_monotonic <= 0.0:
            self._publish_health(
                connected=False,
                status="waiting_for_frame",
                latency_ms=0.0,
                pair_skew_sec=None,
                error="",
            )
            return
        age_sec = time.monotonic() - self._last_success_monotonic
        if age_sec <= 2.0:
            return
        self._publish_health(
            connected=True,
            status="waiting_for_frame",
            latency_ms=0.0,
            pair_skew_sec=None,
            error="",
        )

    def destroy_node(self):
        with self._condition:
            self._running = False
            self._condition.notify_all()
        self._worker.join(timeout=self._request_timeout_sec + 1.0)
        self._session.close()
        return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = RFDETRBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()


if __name__ == "__main__":
    main()
