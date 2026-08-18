"""Interactive, source-time-aware playback for strict shadow evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from rclpy.serialization import deserialize_message
import rosbag2_py
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_msgs.msg import (
    ShadowReplayState,
    SkillStatus,
    VLMHealth,
    VLMResult,
)
from surgical_msgs.srv import ControlShadowReplay, SelectShadowCase


RUNNING_SKILL_STATES = {
    "accepted",
    "active",
    "cleaning",
    "executing",
    "pending",
    "running",
    "started",
}
TERMINAL_SKILL_STATES = {
    "aborted",
    "canceled",
    "cancelled",
    "completed",
    "failed",
    "rejected",
    "succeeded",
}
TRANSIENT_BED_ROBOT_ARM_STATES = {
    "changing_tool",
    "moving_to_standby",
}
VALID_MODES = {"realtime_1x", "elastic_demo"}
ACTIVE_REPLAY_STATES = frozenset({"running", "held", "draining"})
ACTIVE_TICK_PERIOD_SEC = 0.02
IDLE_TICK_PERIOD_SEC = 1.0
ACTIVE_CONTROL_HEARTBEAT_SEC = 0.5
IDLE_CONTROL_HEARTBEAT_SEC = 2.0
ACTIVE_GROUND_TRUTH_PERIOD_SEC = 0.05
IDLE_GROUND_TRUTH_PERIOD_SEC = 1.0
NO_INPUT_VLM_ERRORS = {
    "missing fresh rfdetr-segmented flir image",
    "no fresh field image",
    "no fresh rfdetr-segmented flir image",
}
RFDETR_HEALTH_SCHEMA = "taskplanner.rfdetr_health.v1"
NORMALIZED_CAMERA_TOPICS = {
    "cam1": "/surgery/images/cam1/compressed",
    "cam2": "/surgery/images/cam2/compressed",
    "cam3": "/surgery/images/cam3/compressed",
    "cam4": "/surgery/images/cam4/compressed",
    "flir": "/surgery/images/flir/compressed",
}
FIELD_IMAGE_COMPATIBILITY_TOPIC = "/surgery/images/field/compressed"
NORMALIZED_BBOX_TOPIC = "/surgery/perception/cam4/tools/bboxes/json"
NORMALIZED_SEGMENTATION_TOPIC = (
    "/surgery/perception/cam4/tools/segmentation/json"
)
ALLOWED_SHADOW_CASE_IDS = tuple(
    f"0704_{case_number}" for case_number in range(6, 18)
)
SELECTABLE_REPLAY_STATES = {
    "ready",
    "stopped",
    "completed",
    "timed_out",
    "blocked",
    "error",
}
_VERSIONED_JSONL_RE = re.compile(r"\.v(?P<version>\d+)\.jsonl$")


@dataclass(frozen=True, slots=True)
class ReplaySyncDecision:
    hold_reason: str = ""
    playback_rate_factor: float = 1.0
    vlm_lag_sec: float = 0.0
    hard_hold_active: bool = False
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class ReplayDrainDecision:
    completed: bool
    timed_out: bool
    hold_reason: str
    pending_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImplicitRequestInterval:
    event_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True, slots=True)
class PhaseGroundTruthEvent:
    event_id: str
    phase_id: str
    time_sec: float
    review_status: str
    boundary_kind: str


@dataclass(frozen=True, slots=True)
class ShadowCaseAsset:
    case_id: str
    bag_path: Path
    request_events_path: Path
    phase_events_path: Path


def _versioned_jsonl_key(path: Path) -> tuple[int, str]:
    match = _VERSIONED_JSONL_RE.search(path.name)
    version = int(match.group("version")) if match else -1
    return version, path.name


def _annotation_cases_root(
    *,
    annotation_cases_root: str = "",
    workspace_path: str = "",
) -> Path | None:
    configured = str(annotation_cases_root).strip()
    if configured:
        return Path(configured).expanduser()
    workspace = str(workspace_path).strip() or os.environ.get(
        "TASKPLANNER_WS",
        "",
    )
    if not workspace:
        return None
    return (
        Path(workspace).expanduser()
        / "annotations"
        / "observable_tool_events"
        / "cases"
    )


def resolve_ground_truth_events_path(
    *,
    case_id: str,
    configured_path: str = "",
    workspace_path: str = "",
    annotation_cases_root: str = "",
) -> Path | None:
    """Resolve the newest reviewed observable-event file for UI evaluation."""

    explicit = Path(str(configured_path).strip()).expanduser()
    if str(configured_path).strip():
        return explicit if explicit.is_file() else None
    cases_root = _annotation_cases_root(
        annotation_cases_root=annotation_cases_root,
        workspace_path=workspace_path,
    )
    if cases_root is None:
        return None
    case_dir = cases_root / str(case_id).strip()
    candidates = list(
        case_dir.glob("interaction_events.observed.final.v*.jsonl")
    )
    return max(candidates, key=_versioned_jsonl_key) if candidates else None


def resolve_ground_truth_phase_path(
    *,
    case_id: str,
    configured_path: str = "",
    workspace_path: str = "",
    annotation_cases_root: str = "",
) -> Path | None:
    """Resolve the newest phase reference without selecting draft proposals."""

    explicit = Path(str(configured_path).strip()).expanduser()
    if str(configured_path).strip():
        return explicit if explicit.is_file() else None
    cases_root = _annotation_cases_root(
        annotation_cases_root=annotation_cases_root,
        workspace_path=workspace_path,
    )
    if cases_root is None:
        return None
    case_dir = cases_root / str(case_id).strip()
    finalized = list(
        case_dir.glob("phase_events.provisional.final.v*.jsonl")
    )
    if finalized:
        return max(finalized, key=_versioned_jsonl_key)
    candidates = list(case_dir.glob("phase_events*.jsonl"))
    return max(candidates, key=_versioned_jsonl_key) if candidates else None


def resolve_shadow_case_catalog(
    *,
    current_bag_path: str | Path,
    annotation_cases_root: str = "",
    workspace_path: str = "",
) -> dict[str, ShadowCaseAsset]:
    """Catalog only allow-listed cases with local bags and both GT timelines."""

    current = Path(current_bag_path).expanduser().resolve()
    bag_root = current.parent.resolve()
    cases_root = _annotation_cases_root(
        annotation_cases_root=annotation_cases_root,
        workspace_path=workspace_path,
    )
    if cases_root is None:
        return {}
    cases_root = cases_root.resolve()
    catalog: dict[str, ShadowCaseAsset] = {}
    for case_id in ALLOWED_SHADOW_CASE_IDS:
        bag_path = (bag_root / case_id).resolve()
        if bag_path.parent != bag_root:
            continue
        if (
            not bag_path.is_dir()
            or not (bag_path / "metadata.yaml").is_file()
        ):
            continue
        request_path = resolve_ground_truth_events_path(
            case_id=case_id,
            annotation_cases_root=str(cases_root),
        )
        phase_path = resolve_ground_truth_phase_path(
            case_id=case_id,
            annotation_cases_root=str(cases_root),
        )
        if request_path is None or phase_path is None:
            continue
        request_path = request_path.resolve()
        phase_path = phase_path.resolve()
        case_annotation_dir = (cases_root / case_id).resolve()
        if (
            request_path.parent != case_annotation_dir
            or phase_path.parent != case_annotation_dir
        ):
            continue
        catalog[case_id] = ShadowCaseAsset(
            case_id=case_id,
            bag_path=bag_path,
            request_events_path=request_path,
            phase_events_path=phase_path,
        )
    return catalog


def validate_shadow_case_selection(
    *,
    case_id: str,
    replay_state: str,
    catalog: dict[str, ShadowCaseAsset],
) -> ShadowCaseAsset:
    """Fail closed before opening any alternate replay data."""

    normalized = str(case_id).strip()
    if replay_state not in SELECTABLE_REPLAY_STATES:
        raise ValueError(
            "shadow case can only be selected while replay is not active or paused"
        )
    if normalized not in ALLOWED_SHADOW_CASE_IDS:
        raise ValueError(f"shadow case '{normalized}' is not allow-listed")
    asset = catalog.get(normalized)
    if asset is None:
        raise ValueError(
            f"shadow case '{normalized}' is unavailable or incomplete"
        )
    return asset


def replay_timer_periods(state: str) -> tuple[float, float]:
    """Return deterministic tick and late-join heartbeat periods."""

    if str(state).strip().lower() in ACTIVE_REPLAY_STATES:
        return ACTIVE_TICK_PERIOD_SEC, ACTIVE_CONTROL_HEARTBEAT_SEC
    return IDLE_TICK_PERIOD_SEC, IDLE_CONTROL_HEARTBEAT_SEC


def replay_ground_truth_timer_period(state: str) -> float:
    """Return the semantic ground-truth polling period for replay activity."""

    if str(state).strip().lower() in ACTIVE_REPLAY_STATES:
        return ACTIVE_GROUND_TRUTH_PERIOD_SEC
    return IDLE_GROUND_TRUTH_PERIOD_SEC


def load_implicit_request_intervals(
    path: str | Path,
) -> tuple[ImplicitRequestInterval, ...]:
    """Load only confirmed request intervals; never expose tool labels."""

    intervals: list[ImplicitRequestInterval] = []
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid ground-truth JSON at {source}:{line_number}"
                ) from exc
            if (
                not isinstance(row, dict)
                or row.get("event_type") != "implicit_tool_request"
                or row.get("review_status") != "confirmed"
            ):
                continue
            try:
                start_sec = float(row["start_sec"])
                end_sec = float(row["end_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid implicit request interval at "
                    f"{source}:{line_number}"
                ) from exc
            if (
                not math.isfinite(start_sec)
                or not math.isfinite(end_sec)
                or start_sec < 0.0
                or end_sec < start_sec
            ):
                raise ValueError(
                    f"invalid implicit request bounds at "
                    f"{source}:{line_number}"
                )
            intervals.append(
                ImplicitRequestInterval(
                    event_id=str(row.get("event_id", "")).strip()
                    or f"implicit-request-{line_number}",
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
    return tuple(sorted(intervals, key=lambda item: item.start_sec))


def load_phase_ground_truth_events(
    path: str | Path,
) -> tuple[PhaseGroundTruthEvent, ...]:
    """Load phase starts only; phase references may be explicitly provisional."""

    events: list[PhaseGroundTruthEvent] = []
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid phase ground-truth JSON at "
                    f"{source}:{line_number}"
                ) from exc
            if (
                not isinstance(row, dict)
                or row.get("event_type") != "phase_start"
                or row.get("review_status") == "rejected"
            ):
                continue
            phase_id = str(row.get("phase_id", "")).strip()
            try:
                time_sec = float(row["time_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid phase timestamp at {source}:{line_number}"
                ) from exc
            if not phase_id or not math.isfinite(time_sec) or time_sec < 0.0:
                raise ValueError(
                    f"invalid phase event at {source}:{line_number}"
                )
            events.append(
                PhaseGroundTruthEvent(
                    event_id=str(row.get("event_id", "")).strip()
                    or f"phase-{line_number}",
                    phase_id=phase_id,
                    time_sec=time_sec,
                    review_status=str(
                        row.get("review_status", "unknown")
                    ).strip(),
                    boundary_kind=str(
                        row.get("phase_boundary_kind", "")
                    ).strip(),
                )
            )
    return tuple(sorted(events, key=lambda item: item.time_sec))


def ground_truth_request_state(
    intervals: tuple[ImplicitRequestInterval, ...],
    source_time_sec: float,
) -> tuple[ImplicitRequestInterval | None, bool]:
    """Return the active interval or most recent interval at source time."""

    current: ImplicitRequestInterval | None = None
    active = False
    for interval in intervals:
        if interval.start_sec > source_time_sec:
            break
        current = interval
        active = source_time_sec <= interval.end_sec
    return current, active


def ground_truth_phase_state(
    events: tuple[PhaseGroundTruthEvent, ...],
    source_time_sec: float,
) -> PhaseGroundTruthEvent | None:
    """Return the latest annotated phase start at source time."""

    current: PhaseGroundTruthEvent | None = None
    for event in events:
        if event.time_sec > source_time_sec:
            break
        current = event
    return current


def ground_truth_state_payload(
    *,
    run_id: str,
    case_id: str,
    source_time_sec: float,
    duration_sec: float = 0.0,
    request_intervals: tuple[ImplicitRequestInterval, ...],
    phase_events: tuple[PhaseGroundTruthEvent, ...],
) -> dict[str, Any]:
    """Build the evaluation-only payload without leaking tool identities."""

    request_interval, request_active = ground_truth_request_state(
        request_intervals,
        source_time_sec,
    )
    phase_event = ground_truth_phase_state(phase_events, source_time_sec)
    phase_end_sec = 0.0
    if phase_event is not None:
        phase_index = phase_events.index(phase_event)
        if phase_index + 1 < len(phase_events):
            phase_end_sec = phase_events[phase_index + 1].time_sec
        elif duration_sec > phase_event.time_sec:
            phase_end_sec = float(duration_sec)
    return {
        "schema": "taskplanner.shadow_ground_truth.v2",
        "evaluation_only": True,
        "run_id": str(run_id),
        "case_id": str(case_id),
        "source_time_sec": round(float(source_time_sec), 6),
        "available": bool(request_intervals or phase_events),
        "implicit_tool_request": {
            "available": bool(request_intervals),
            "active": bool(request_active),
            "event_id": (
                request_interval.event_id if request_interval else ""
            ),
            "start_sec": (
                round(request_interval.start_sec, 6)
                if request_interval
                else 0.0
            ),
            "end_sec": (
                round(request_interval.end_sec, 6)
                if request_interval
                else 0.0
            ),
        },
        "phase": {
            "available": bool(phase_events),
            "active": phase_event is not None,
            "phase_id": phase_event.phase_id if phase_event else "",
            "event_id": phase_event.event_id if phase_event else "",
            "start_sec": (
                round(phase_event.time_sec, 6) if phase_event else 0.0
            ),
            "end_sec": round(phase_end_sec, 6),
            "review_status": (
                phase_event.review_status if phase_event else ""
            ),
            "boundary_kind": (
                phase_event.boundary_kind if phase_event else ""
            ),
        },
    }


def parse_control_command(value: str) -> tuple[str, str]:
    """Parse an optional caller reason without changing the service schema."""

    raw = str(value or "").strip()
    for separator in ("|", ":"):
        if separator in raw:
            command, reason = raw.split(separator, 1)
            return command.strip().lower(), reason.strip()
    return raw.lower(), ""


def coalesce_stateful_records(
    records: list[tuple[str, Any, float]],
    *,
    stateful_topics: set[str],
) -> tuple[list[tuple[str, Any, float]], int]:
    """Keep the newest stateful sample per topic and every event-like record."""

    last_index_by_topic = {
        topic: index
        for index, (topic, _, _) in enumerate(records)
        if topic in stateful_topics
    }
    kept = [
        record
        for index, record in enumerate(records)
        if record[0] not in stateful_topics
        or last_index_by_topic[record[0]] == index
    ]
    return kept, max(0, len(records) - len(kept))


def replay_state_change_key(payload: dict[str, Any]) -> str:
    """Return a semantic state key, excluding high-frequency clock counters."""

    change_fields = (
        "run_id",
        "state",
        "mode",
        "hold_reason",
        "last_error",
        "control_reason",
        "degraded_mode",
        "drain_timed_out",
        "completed_vlm_count",
        "failed_vlm_count",
        "pending_vlm_count",
        "active_skill_count",
        "active_cleanup_count",
        "published_transcript_count",
    )
    return json.dumps(
        {key: payload.get(key) for key in change_fields},
        separators=(",", ":"),
        sort_keys=True,
    )


def replay_drain_decision(
    *,
    require_vlm: bool,
    pending_vlm_count: int,
    active_skill_count: int,
    active_cleanup_count: int,
    elapsed_sec: float,
    timeout_sec: float,
) -> ReplayDrainDecision:
    # VLM completion is observational and must not extend media playback.
    # Keep the arguments for wire/helper compatibility and continue exposing
    # the pending count through replay state metadata.
    _ = require_vlm, pending_vlm_count
    pending: list[str] = []
    if active_skill_count > 0:
        pending.append("skill")
    if active_cleanup_count > 0:
        pending.append("cleanup")
    if not pending:
        return ReplayDrainDecision(True, False, "", ())
    if elapsed_sec >= max(0.0, timeout_sec):
        return ReplayDrainDecision(
            True,
            True,
            "drain_timeout",
            tuple(pending),
        )
    return ReplayDrainDecision(
        False,
        False,
        f"draining:{'+'.join(pending)}",
        tuple(pending),
    )


def unresolvable_vlm_tail_count(
    *,
    require_vlm: bool,
    image_input_active: bool,
    pending_vlm_count: int,
    drain_elapsed_sec: float,
    grace_sec: float,
) -> int:
    """Fail VLM slots that can no longer receive a post-media image."""

    if (
        not require_vlm
        or image_input_active
        or pending_vlm_count <= 0
        or drain_elapsed_sec < max(0.0, grace_sec)
    ):
        return 0
    return max(0, int(pending_vlm_count))


def vlm_watchdog_error(
    *,
    require_vlm: bool,
    image_input_active: bool,
    timeout_sec: float,
    source_time_sec: float,
    pending_vlm_count: int,
    hold_reason: str,
    no_input_elapsed_sec: float | None,
    response_wait_elapsed_sec: float | None,
    last_input_error: str,
) -> str:
    if not require_vlm or not image_input_active:
        return ""
    timeout = max(0.0, float(timeout_sec))
    if (
        no_input_elapsed_sec is not None
        and no_input_elapsed_sec >= timeout
    ):
        detail = last_input_error or "no usable VLM image input"
        return (
            "VLM input unavailable for "
            f"{no_input_elapsed_sec:.1f}s at source "
            f"{source_time_sec:.3f}s: {detail}"
        )
    if (
        pending_vlm_count > 0
        and response_wait_elapsed_sec is not None
        and response_wait_elapsed_sec >= timeout
    ):
        reason = hold_reason or "observation_backlog"
        return (
            "VLM response timeout after "
            f"{response_wait_elapsed_sec:.1f}s at source "
            f"{source_time_sec:.3f}s "
            f"(pending={pending_vlm_count}, reason={reason})"
        )
    return ""


def public_replay_topic_routes(
    *,
    source_cam1_topic: str,
    source_cam2_topic: str,
    source_cam3_topic: str,
    source_cam4_topic: str,
    source_flir_topic: str,
    source_bbox_topic: str,
    source_segmentation_topic: str,
    field_image_topic: str = FIELD_IMAGE_COMPATIBILITY_TOPIC,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Build the strict public-input routes without evaluation topics."""

    image_routes = {
        str(source_cam1_topic): (NORMALIZED_CAMERA_TOPICS["cam1"],),
        str(source_cam2_topic): (NORMALIZED_CAMERA_TOPICS["cam2"],),
        str(source_cam3_topic): (NORMALIZED_CAMERA_TOPICS["cam3"],),
        str(source_cam4_topic): (
            NORMALIZED_CAMERA_TOPICS["cam4"],
            str(field_image_topic),
        ),
        str(source_flir_topic): (NORMALIZED_CAMERA_TOPICS["flir"],),
    }
    json_routes = {
        str(source_bbox_topic): NORMALIZED_BBOX_TOPIC,
        str(source_segmentation_topic): NORMALIZED_SEGMENTATION_TOPIC,
    }
    return image_routes, json_routes


def advance_replay_elapsed(
    elapsed_sec: float,
    last_tick_at: float,
    now: float,
    *,
    active: bool,
) -> tuple[float, float, float]:
    """Accumulate wall time only while replay execution is active."""

    raw_delta = max(0.0, float(now) - float(last_tick_at))
    next_elapsed = max(0.0, float(elapsed_sec))
    if active:
        next_elapsed += raw_delta
    return next_elapsed, float(now), min(0.25, raw_delta)


def advance_replay_source_time(
    source_time_sec: float,
    delta_sec: float,
    playback_rate: float,
    duration_sec: float,
    *,
    advancing: bool,
) -> float:
    """Advance source time only for an actively playing, unheld replay."""

    source = max(0.0, float(source_time_sec))
    if not advancing:
        return source
    return min(
        max(0.0, float(duration_sec)),
        source + max(0.0, float(delta_sec)) * max(0.0, float(playback_rate)),
    )


def replay_clock_publish_due(
    *,
    source_time_sec: float,
    last_source_time_sec: float | None,
    now_monotonic: float,
    last_publish_monotonic: float,
    idle_heartbeat_sec: float,
    force: bool = False,
) -> bool:
    """Publish every advancing clock tick, but bound an unchanged heartbeat."""

    if force or last_source_time_sec is None:
        return True
    if float(source_time_sec) != float(last_source_time_sec):
        return True
    return (
        float(now_monotonic) - float(last_publish_monotonic)
        >= max(0.1, float(idle_heartbeat_sec))
    )


def _seconds_from_stamp(stamp: Any) -> float:
    return float(getattr(stamp, "sec", 0)) + float(
        getattr(stamp, "nanosec", 0)
    ) / 1_000_000_000.0


def _is_no_input_vlm_health(msg: VLMHealth) -> bool:
    return bool(
        msg.connected
        and str(msg.last_error or "").strip().lower() in NO_INPUT_VLM_ERRORS
    )


def perception_enabled_from_health(msg: String) -> bool | None:
    """Return the operator-selected RF-DETR state from a valid health frame."""

    try:
        payload = json.loads(str(msg.data or ""))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != RFDETR_HEALTH_SCHEMA
    ):
        return None
    return bool(payload.get("enabled"))


def vlm_result_slot_id(stamp: Any, period_sec: float) -> int | None:
    """Map a successful result to its periodic source-time inference slot."""

    period = max(0.001, float(period_sec))
    stamp_sec = _seconds_from_stamp(stamp)
    slot_id = int(math.floor((stamp_sec + 1.0e-6) / period))
    return slot_id if slot_id >= 1 else None


def vlm_completion_watermark(
    completed_slots: set[int],
    expected_slot: int,
) -> int:
    """Return the newest successful state estimate up to the expected slot."""

    return max(
        (
            slot_id
            for slot_id in completed_slots
            if 1 <= slot_id <= max(0, int(expected_slot))
        ),
        default=0,
    )


def vlm_source_slot_id(source_time_sec: float, period_sec: float) -> int | None:
    """Map an actual public image frame to its source-time inference slot."""

    period = max(0.001, float(period_sec))
    slot_id = int(
        math.floor((max(0.0, float(source_time_sec)) + 1.0e-6) / period)
    )
    return slot_id if slot_id >= 1 else None


def vlm_observation_id(source_time_sec: float) -> int | None:
    """Return an exact source-frame key suitable for request/result matching."""

    stamp_ns = int(round(max(0.0, float(source_time_sec)) * 1_000_000_000.0))
    return stamp_ns if stamp_ns > 0 else None


def vlm_result_observation_id(stamp: Any) -> int | None:
    """Return the exact source-frame key carried by a VLM result."""

    return vlm_observation_id(_seconds_from_stamp(stamp))


def record_vlm_input_obligation(
    *,
    stamp_sec: float,
    period_sec: float,
    image_duration_sec: float,
    expected_slots: set[int],
    expected_slot_times: dict[int, float],
) -> int | None:
    """Record each exact frame that reached the real VLM input topic."""

    bounded_stamp = max(0.0, float(stamp_sec))
    if bounded_stamp > max(0.0, float(image_duration_sec)):
        return None
    observation_id = vlm_observation_id(bounded_stamp)
    if observation_id is None:
        return None
    expected_slots.add(observation_id)
    expected_slot_times[observation_id] = bounded_stamp
    return observation_id


def actual_vlm_progress(
    expected_slots: set[int],
    completed_slots: set[int],
) -> tuple[int, int]:
    """Count exact model-input frames with matching VLM results."""

    completed = len(expected_slots & completed_slots)
    pending = max(0, len(expected_slots) - completed)
    return completed, pending


def vlm_obligation_progress(
    expected_slots: set[int],
    completed_slots: set[int],
    failed_slots: set[int],
) -> tuple[int, int, int]:
    """Resolve exact model-input obligations without hiding failed inference."""

    successful = expected_slots & completed_slots
    failed = (expected_slots & failed_slots) - successful
    pending = expected_slots - successful - failed
    return len(successful), len(failed), len(pending)


def pending_vlm_source_lag_sec(
    *,
    source_time_sec: float,
    expected_slots: set[int],
    expected_slot_times: dict[int, float],
    completed_slots: set[int],
    failed_slots: set[int],
    period_sec: float,
) -> float:
    """Measure actual source lead over the oldest unresolved VLM frame."""

    pending = [
        observation_id
        for observation_id in expected_slots
        if observation_id not in completed_slots
        and observation_id not in failed_slots
    ]
    if not pending:
        return 0.0
    oldest_slot = min(
        pending,
        key=lambda observation_id: expected_slot_times.get(
            observation_id,
            float(observation_id) / 1_000_000_000.0,
        ),
    )
    expected_at = expected_slot_times.get(
        oldest_slot,
        float(oldest_slot) / 1_000_000_000.0,
    )
    return max(0.0, float(source_time_sec) - float(expected_at))


def _time_message(seconds: float):
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    nanosec = int(round((seconds - whole) * 1_000_000_000.0))
    if nanosec >= 1_000_000_000:
        whole += 1
        nanosec -= 1_000_000_000
    from builtin_interfaces.msg import Time

    return Time(sec=whole, nanosec=nanosec)


@dataclass(slots=True)
class ElasticReplayGate:
    """Hold only for causal physical work while observing VLM progress."""

    vlm_period_sec: float = 1.0
    max_pending_vlm: int = 1
    require_vlm: bool = False
    soft_lag_sec: float = 0.5
    hard_lag_sec: float = 2.5
    hard_release_lag_sec: float = 1.0
    min_rate_factor: float = 0.25
    max_visual_lead_sec: float = 0.35
    _hard_hold_active: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.vlm_period_sec = max(0.001, float(self.vlm_period_sec))
        self.max_pending_vlm = max(0, int(self.max_pending_vlm))
        self.soft_lag_sec = max(0.0, float(self.soft_lag_sec))
        self.hard_lag_sec = max(
            self.soft_lag_sec + 0.001,
            float(self.hard_lag_sec),
        )
        self.hard_release_lag_sec = min(
            self.hard_lag_sec - 0.001,
            max(0.0, float(self.hard_release_lag_sec)),
        )
        self.min_rate_factor = min(
            1.0,
            max(0.05, float(self.min_rate_factor)),
        )
        self.max_visual_lead_sec = max(
            0.05,
            float(self.max_visual_lead_sec),
        )

    def reset(self) -> None:
        self._hard_hold_active = False

    def expected_vlm_count(
        self,
        *,
        source_time_sec: float,
        image_duration_sec: float,
        published_image_count: int,
    ) -> int:
        if published_image_count <= 0 or self.vlm_period_sec <= 0.0:
            return 0
        bounded = min(
            max(0.0, source_time_sec),
            max(0.0, image_duration_sec),
        )
        # The real VLM timer emits its first request after one full period.
        # Counting a synthetic t=0 request leaves elastic playback held forever.
        return int(math.floor(bounded / self.vlm_period_sec))

    def pending_vlm_count(
        self,
        *,
        source_time_sec: float,
        image_duration_sec: float,
        published_image_count: int,
        completed_vlm_count: int,
    ) -> int:
        return max(
            0,
            self.expected_vlm_count(
                source_time_sec=source_time_sec,
                image_duration_sec=image_duration_sec,
                published_image_count=published_image_count,
            )
            - max(0, completed_vlm_count),
        )

    def vlm_lag_sec(
        self,
        *,
        source_time_sec: float,
        image_duration_sec: float,
        published_image_count: int,
        completed_vlm_count: int,
        observed_pending_vlm_count: int | None = None,
    ) -> float:
        pending = (
            max(0, int(observed_pending_vlm_count))
            if observed_pending_vlm_count is not None
            else self.pending_vlm_count(
                source_time_sec=source_time_sec,
                image_duration_sec=image_duration_sec,
                published_image_count=published_image_count,
                completed_vlm_count=completed_vlm_count,
            )
        )
        return pending * self.vlm_period_sec

    def sync_decision(
        self,
        *,
        mode: str,
        source_time_sec: float,
        image_duration_sec: float,
        published_image_count: int,
        completed_vlm_count: int,
        active_skill_count: int,
        active_cleanup_count: int,
        vlm_ready: bool,
        vlm_grace_elapsed: bool,
        observed_pending_vlm_count: int | None = None,
        observed_vlm_lag_sec: float | None = None,
        require_vlm: bool | None = None,
    ) -> ReplaySyncDecision:
        vlm_required = (
            self.require_vlm
            if require_vlm is None
            else bool(require_vlm)
        )
        pending_count = (
            max(0, int(observed_pending_vlm_count))
            if observed_pending_vlm_count is not None
            else self.pending_vlm_count(
                source_time_sec=source_time_sec,
                image_duration_sec=image_duration_sec,
                published_image_count=published_image_count,
                completed_vlm_count=completed_vlm_count,
            )
        )
        lag_sec = (
            max(0.0, float(observed_vlm_lag_sec))
            if observed_vlm_lag_sec is not None
            else self.vlm_lag_sec(
                source_time_sec=source_time_sec,
                image_duration_sec=image_duration_sec,
                published_image_count=published_image_count,
                completed_vlm_count=completed_vlm_count,
                observed_pending_vlm_count=observed_pending_vlm_count,
            )
        )
        # Elastic playback keeps recorded media causally aligned with simulated
        # physical actions. Realtime playback deliberately leaves the source
        # clock independent so downstream skill/cleanup latency stays visible.
        synchronize_actions = mode == "elastic_demo"
        if synchronize_actions and active_cleanup_count > 0:
            self._hard_hold_active = False
            return ReplaySyncDecision(
                hold_reason="cleanup_execution",
                playback_rate_factor=0.0,
                vlm_lag_sec=lag_sec,
                hard_hold_active=False,
                degraded=not vlm_required or not vlm_ready,
            )
        if synchronize_actions and active_skill_count > 0:
            self._hard_hold_active = False
            return ReplaySyncDecision(
                hold_reason="skill_execution",
                playback_rate_factor=0.0,
                vlm_lag_sec=lag_sec,
                hard_hold_active=False,
                degraded=not vlm_required or not vlm_ready,
            )
        # VLM is asynchronous visual evidence. Its readiness, backlog, and
        # frame alignment remain visible in metadata but never alter the media
        # clock. Legacy thresholds remain accepted so existing launch files and
        # callers do not break.
        _ = vlm_grace_elapsed, pending_count
        self._hard_hold_active = False
        return ReplaySyncDecision(
            playback_rate_factor=1.0,
            vlm_lag_sec=lag_sec,
            hard_hold_active=False,
            degraded=not vlm_required or not vlm_ready,
        )

    def hold_reason(
        self,
        *,
        mode: str,
        source_time_sec: float,
        image_duration_sec: float,
        published_image_count: int,
        completed_vlm_count: int,
        active_skill_count: int,
        vlm_ready: bool,
        vlm_grace_elapsed: bool,
        active_cleanup_count: int = 0,
        observed_pending_vlm_count: int | None = None,
        observed_vlm_lag_sec: float | None = None,
        require_vlm: bool | None = None,
    ) -> str:
        return self.sync_decision(
            mode=mode,
            source_time_sec=source_time_sec,
            image_duration_sec=image_duration_sec,
            published_image_count=published_image_count,
            completed_vlm_count=completed_vlm_count,
            active_skill_count=active_skill_count,
            active_cleanup_count=active_cleanup_count,
            vlm_ready=vlm_ready,
            vlm_grace_elapsed=vlm_grace_elapsed,
            observed_pending_vlm_count=observed_pending_vlm_count,
            observed_vlm_lag_sec=observed_vlm_lag_sec,
            require_vlm=require_vlm,
        ).hold_reason


class FilteredBagSource:
    """Sequentially expose only the public camera and transcript topics."""

    def __init__(
        self,
        bag_path: str | Path,
        topics: list[str],
        storage_id: str = "mcap",
    ) -> None:
        self.bag_path = Path(bag_path)
        self.topics = list(topics)
        self.storage_id = storage_id
        self.reader: rosbag2_py.SequentialReader | None = None
        self.start_ns = 0
        self.duration_ns = 0
        self.next_record: tuple[str, Any, int] | None = None
        self.open()

    @property
    def duration_sec(self) -> float:
        return float(self.duration_ns) / 1_000_000_000.0

    def open(self, seek_sec: float = 0.0) -> None:
        if not self.bag_path.is_dir():
            raise FileNotFoundError(f"shadow bag not found: {self.bag_path}")
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(
                uri=str(self.bag_path),
                storage_id=self.storage_id,
            ),
            rosbag2_py.ConverterOptions(
                input_serialization_format="cdr",
                output_serialization_format="cdr",
            ),
        )
        metadata = reader.get_metadata()
        self.start_ns = int(metadata.starting_time.nanoseconds)
        self.duration_ns = int(metadata.duration.nanoseconds)
        reader.set_filter(rosbag2_py.StorageFilter(topics=self.topics))
        if seek_sec > 0.0:
            reader.seek(self.start_ns + int(seek_sec * 1_000_000_000.0))
        self.reader = reader
        self.next_record = None
        self._prime()

    def close(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
        self.reader = None
        self.next_record = None

    def _prime(self) -> None:
        if self.reader is None or not self.reader.has_next():
            self.next_record = None
            return
        topic, serialized, received_ns = self.reader.read_next()
        self.next_record = (str(topic), serialized, int(received_ns))

    def pop_due(self, source_time_sec: float) -> list[tuple[str, Any, float]]:
        due: list[tuple[str, Any, float]] = []
        target_ns = self.start_ns + int(
            max(0.0, source_time_sec) * 1_000_000_000.0
        )
        while self.next_record is not None and self.next_record[2] <= target_ns:
            topic, serialized, received_ns = self.next_record
            due.append(
                (
                    topic,
                    serialized,
                    float(received_ns - self.start_ns) / 1_000_000_000.0,
                )
            )
            self._prime()
        return due

    def exhausted(self) -> bool:
        return self.next_record is None


class InteractiveReplayControllerNode(Node):
    def __init__(self) -> None:
        super().__init__("interactive_shadow_replay_controller")
        self.declare_parameter("case_id", "0704_6")
        self.declare_parameter("run_id", "")
        self.declare_parameter("procedure_id", "thyroidectomy_demo")
        self.declare_parameter("bag_path", "")
        self.declare_parameter(
            "source_image_topic",
            "/surgery/cam4/color/image/compressed",
        )
        self.declare_parameter("source_cam4_topic", "")
        self.declare_parameter(
            "source_cam1_topic",
            "/surgery/cam1/color/image/compressed",
        )
        self.declare_parameter(
            "source_cam2_topic",
            "/surgery/cam2/color/image/compressed",
        )
        self.declare_parameter(
            "source_cam3_topic",
            "/surgery/cam3/color/image/compressed",
        )
        self.declare_parameter(
            "source_flir_topic",
            "/surgery/flir/image/compressed",
        )
        self.declare_parameter(
            "source_bbox_topic",
            "/surgery/cam4/tools/bboxes/json",
        )
        self.declare_parameter(
            "source_segmentation_topic",
            "/surgery/cam4/tools/segmentation/json",
        )
        self.declare_parameter(
            "output_image_topic",
            FIELD_IMAGE_COMPATIBILITY_TOPIC,
        )
        self.declare_parameter("transcript_topic", "/surgery/transcript")
        self.declare_parameter("storage_id", "mcap")
        self.declare_parameter("mode", "elastic_demo")
        self.declare_parameter("playback_rate", 1.0)
        self.declare_parameter("image_duration_sec", 138.4284)
        self.declare_parameter("vlm_period_sec", 1.0)
        self.declare_parameter("max_pending_vlm", 1)
        self.declare_parameter("require_vlm", False)
        self.declare_parameter("vlm_startup_grace_sec", 8.0)
        self.declare_parameter("vlm_health_timeout_sec", 5.0)
        self.declare_parameter("vlm_wait_timeout_sec", 20.0)
        self.declare_parameter("vlm_transient_failure_grace_sec", 8.0)
        self.declare_parameter("vlm_max_consecutive_failures", 2)
        self.declare_parameter("vlm_soft_lag_sec", 0.5)
        self.declare_parameter("vlm_hard_lag_sec", 0.0)
        self.declare_parameter("vlm_hard_release_lag_sec", 1.0)
        self.declare_parameter("vlm_min_rate_factor", 0.25)
        self.declare_parameter("vlm_max_visual_lead_sec", 0.35)
        self.declare_parameter(
            "vlm_input_image_topic",
            "/surgery/images/vlm/composite/compressed",
        )
        self.declare_parameter("drain_timeout_sec", 30.0)
        self.declare_parameter("drain_settle_sec", 1.25)
        self.declare_parameter("state_heartbeat_sec", 1.0)
        self.declare_parameter("idle_clock_heartbeat_sec", 1.0)
        self.declare_parameter("ground_truth_events_path", "")
        self.declare_parameter("ground_truth_phase_path", "")
        self.declare_parameter("annotation_cases_root", "")
        self.declare_parameter("auto_start", False)

        self._lock = threading.RLock()
        self._case_id = str(self.get_parameter("case_id").value)
        self._bag_path = str(self.get_parameter("bag_path").value)
        self._annotation_cases_root = str(
            self.get_parameter("annotation_cases_root").value
        ).strip()
        self._case_catalog = resolve_shadow_case_catalog(
            current_bag_path=self._bag_path,
            annotation_cases_root=self._annotation_cases_root,
        )
        initial_asset = self._case_catalog.get(self._case_id)
        self._ground_truth_events_path = resolve_ground_truth_events_path(
            case_id=self._case_id,
            configured_path=str(
                self.get_parameter("ground_truth_events_path").value
            ),
            annotation_cases_root=self._annotation_cases_root,
        )
        if (
            self._ground_truth_events_path is None
            and initial_asset is not None
        ):
            self._ground_truth_events_path = (
                initial_asset.request_events_path
            )
        self._ground_truth_phase_path = resolve_ground_truth_phase_path(
            case_id=self._case_id,
            configured_path=str(
                self.get_parameter("ground_truth_phase_path").value
            ),
            annotation_cases_root=self._annotation_cases_root,
        )
        if self._ground_truth_phase_path is None and initial_asset is not None:
            self._ground_truth_phase_path = initial_asset.phase_events_path
        self._implicit_request_intervals: tuple[
            ImplicitRequestInterval, ...
        ] = ()
        self._phase_ground_truth_events: tuple[
            PhaseGroundTruthEvent, ...
        ] = ()
        if self._ground_truth_events_path is not None:
            try:
                self._implicit_request_intervals = (
                    load_implicit_request_intervals(
                        self._ground_truth_events_path
                    )
                )
            except (OSError, ValueError) as exc:
                self.get_logger().warning(
                    f"ground-truth timeline unavailable: {exc}"
                )
        if self._ground_truth_phase_path is not None:
            try:
                self._phase_ground_truth_events = (
                    load_phase_ground_truth_events(
                        self._ground_truth_phase_path
                    )
                )
            except (OSError, ValueError) as exc:
                self.get_logger().warning(
                    f"phase ground-truth timeline unavailable: {exc}"
                )
        self._configured_run_id = str(
            self.get_parameter("run_id").value
        ).strip()
        self._run_sequence = 0
        self._procedure_id = str(self.get_parameter("procedure_id").value)
        self._source_image_topic = str(
            self.get_parameter("source_image_topic").value
        )
        self._source_cam4_topic = (
            str(self.get_parameter("source_cam4_topic").value).strip()
            or self._source_image_topic
        )
        self._source_flir_topic = str(
            self.get_parameter("source_flir_topic").value
        )
        self._output_image_topic = str(
            self.get_parameter("output_image_topic").value
        )
        (
            self._image_topic_routes,
            self._json_topic_routes,
        ) = public_replay_topic_routes(
            source_cam1_topic=str(
                self.get_parameter("source_cam1_topic").value
            ),
            source_cam2_topic=str(
                self.get_parameter("source_cam2_topic").value
            ),
            source_cam3_topic=str(
                self.get_parameter("source_cam3_topic").value
            ),
            source_cam4_topic=self._source_cam4_topic,
            source_flir_topic=self._source_flir_topic,
            source_bbox_topic=str(
                self.get_parameter("source_bbox_topic").value
            ),
            source_segmentation_topic=str(
                self.get_parameter("source_segmentation_topic").value
            ),
            field_image_topic=self._output_image_topic,
        )
        self._transcript_topic = str(
            self.get_parameter("transcript_topic").value
        )
        self._storage_id = str(self.get_parameter("storage_id").value)
        self._mode = str(self.get_parameter("mode").value)
        if self._mode not in VALID_MODES:
            self._mode = "elastic_demo"
        self._playback_rate = max(
            0.1,
            float(self.get_parameter("playback_rate").value),
        )
        self._image_duration_sec = max(
            0.0,
            float(self.get_parameter("image_duration_sec").value),
        )
        self._vlm_input_image_topic = str(
            self.get_parameter("vlm_input_image_topic").value
        ).strip()
        vlm_period_sec = max(
            0.1,
            float(self.get_parameter("vlm_period_sec").value),
        )
        max_pending_vlm = max(
            0,
            int(self.get_parameter("max_pending_vlm").value),
        )
        configured_hard_lag_sec = float(
            self.get_parameter("vlm_hard_lag_sec").value
        )
        hard_lag_sec = (
            configured_hard_lag_sec
            if configured_hard_lag_sec > 0.0
            else (max_pending_vlm + 1.5) * vlm_period_sec
        )
        self._gate = ElasticReplayGate(
            vlm_period_sec=vlm_period_sec,
            max_pending_vlm=max_pending_vlm,
            require_vlm=bool(self.get_parameter("require_vlm").value),
            soft_lag_sec=max(
                0.0,
                float(self.get_parameter("vlm_soft_lag_sec").value),
            ),
            hard_lag_sec=hard_lag_sec,
            hard_release_lag_sec=max(
                0.0,
                float(
                    self.get_parameter(
                        "vlm_hard_release_lag_sec"
                    ).value
                ),
            ),
            min_rate_factor=float(
                self.get_parameter("vlm_min_rate_factor").value
            ),
            max_visual_lead_sec=float(
                self.get_parameter("vlm_max_visual_lead_sec").value
            ),
        )
        self._vlm_startup_grace_sec = max(
            0.0,
            float(self.get_parameter("vlm_startup_grace_sec").value),
        )
        self._vlm_health_timeout_sec = max(
            0.5,
            float(self.get_parameter("vlm_health_timeout_sec").value),
        )
        self._vlm_wait_timeout_sec = max(
            0.5,
            float(self.get_parameter("vlm_wait_timeout_sec").value),
        )
        self._vlm_transient_failure_grace_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "vlm_transient_failure_grace_sec"
                ).value
            ),
        )
        self._vlm_max_consecutive_failures = max(
            0,
            int(
                self.get_parameter(
                    "vlm_max_consecutive_failures"
                ).value
            ),
        )
        self._drain_timeout_sec = max(
            0.0,
            float(self.get_parameter("drain_timeout_sec").value),
        )
        self._drain_settle_sec = max(
            0.0,
            float(self.get_parameter("drain_settle_sec").value),
        )
        self._state_heartbeat_sec = max(
            0.25,
            float(self.get_parameter("state_heartbeat_sec").value),
        )
        self._idle_clock_heartbeat_sec = max(
            0.1,
            float(self.get_parameter("idle_clock_heartbeat_sec").value),
        )

        self._source: FilteredBagSource | None = None
        self._state = "loading"
        self._run_id = ""
        self._last_error = ""
        self._source_time_sec = 0.0
        self._wall_elapsed_sec = 0.0
        self._last_tick_at = time.monotonic()
        self._elastic_hold_sec = 0.0
        self._hold_reason = ""
        self._effective_playback_rate = self._playback_rate
        self._published_image_count = 0
        self._published_transcript_count = 0
        self._coalesced_stateful_count = 0
        self._expected_vlm_slots: set[int] = set()
        self._expected_vlm_slot_times: dict[int, float] = {}
        self._completed_vlm_slots: set[int] = set()
        self._failed_vlm_slots: set[int] = set()
        self._active_skills: set[str] = set()
        self._active_cleanup_skills: set[str] = set()
        self._active_bed_robot_arms: set[str] = set()
        self._vlm_healthy = False
        self._vlm_connected = False
        self._vlm_health_source_sec = -1.0
        self._vlm_health_received_at = -1.0
        self._last_vlm_health_error = ""
        self._last_vlm_progress_at = time.monotonic()
        self._last_vlm_failure_signature = ""
        self._consecutive_vlm_failures = 0
        self._vlm_failure_grace_until = -1.0
        self._vlm_wait_started_at: float | None = None
        self._no_input_since_at: float | None = None
        self._drain_started_at: float | None = None
        self._drain_clear_since_at: float | None = None
        self._drain_timed_out = False
        self._vlm_resume_grace_until = -1.0
        self._perception_enabled = True
        self._perception_health_received = False
        self._state_before_manual_pause = "running"
        self._last_control_command = ""
        self._control_reason = ""
        self._last_state_change_key = ""
        self._last_state_publish_at = float("-inf")
        self._force_state_publish = True
        self._last_ground_truth_key = ""
        self._last_clock_publish_at = float("-inf")
        self._last_clock_source_time_sec: float | None = None

        state_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        image_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=64,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._clock_publisher = self.create_publisher(Clock, "/clock", 10)
        self._image_publishers = {
            output_topic: self.create_publisher(
                CompressedImage,
                output_topic,
                image_qos,
            )
            for output_topics in self._image_topic_routes.values()
            for output_topic in output_topics
        }
        self._json_publishers = {
            source_topic: self.create_publisher(
                String,
                output_topic,
                50,
            )
            for source_topic, output_topic in self._json_topic_routes.items()
        }
        self._transcript_publisher = self.create_publisher(
            String,
            self._transcript_topic,
            50,
        )
        self._state_publisher = self.create_publisher(
            ShadowReplayState,
            "/shadow/replay_state",
            state_qos,
        )
        self._runtime_control_publisher = self.create_publisher(
            String,
            "/simulation/control_state",
            state_qos,
        )
        self._ground_truth_publisher = self.create_publisher(
            String,
            "/shadow/ground_truth/state",
            state_qos,
        )
        self.create_subscription(
            VLMResult,
            "/vlm/result",
            self._on_vlm_result,
            50,
        )
        self.create_subscription(
            CompressedImage,
            self._vlm_input_image_topic,
            self._on_vlm_input_image,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            VLMHealth,
            "/vlm/health",
            self._on_vlm_health,
            20,
        )
        self.create_subscription(
            String,
            "/surgery/perception/rfdetr/health",
            self._on_perception_health,
            20,
        )
        self.create_subscription(
            SkillStatus,
            "/skill/status",
            self._on_skill_status,
            50,
        )
        self.create_subscription(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            self._on_bed_robot_arm_status,
            50,
        )
        self.create_service(
            ControlShadowReplay,
            "/shadow/control_replay",
            self._on_control,
        )
        self.create_service(
            SelectShadowCase,
            "/shadow/select_case",
            self._on_select_case,
        )

        self._load_source()
        # Seed the transient-local ground-truth snapshot immediately. The
        # adaptive idle timer must not add up to one second of startup latency.
        self._publish_ground_truth(force=True)
        # This node intentionally uses wall time. Other shadow nodes consume
        # the /clock published here as their source-time clock.
        tick_period_sec, control_heartbeat_sec = replay_timer_periods(
            self._state
        )
        self._tick_timer = self.create_timer(tick_period_sec, self._tick)
        self._ground_truth_timer = self.create_timer(
            replay_ground_truth_timer_period(self._state),
            self._publish_ground_truth,
        )
        self.create_timer(0.1, self._publish_state)
        self._runtime_control_timer = self.create_timer(
            control_heartbeat_sec,
            self._publish_runtime_control_heartbeat,
        )
        if bool(self.get_parameter("auto_start").value) and self._source:
            self._start(reset=True)

    def _load_source(self, seek_sec: float = 0.0) -> None:
        try:
            self._source = FilteredBagSource(
                self._bag_path,
                [
                    *self._image_topic_routes,
                    *self._json_topic_routes,
                    self._transcript_topic,
                ],
                self._storage_id,
            )
            if seek_sec > 0.0:
                self._source.open(seek_sec)
            self._state = "ready"
            self._effective_playback_rate = 0.0
            self._last_error = ""
            self._force_state_publish = True
            self.get_logger().info(
                f"loaded shadow case {self._case_id} "
                f"({self._source.duration_sec:.3f}s)"
            )
        except Exception as exc:
            self._source = None
            self._state = "error"
            self._last_error = str(exc)
            self._force_state_publish = True
            self._publish_runtime_control("stop")
            self.get_logger().error(self._last_error)

    def _publish_runtime_control(self, command: str) -> None:
        message = String()
        message.data = str(command).strip().lower()
        self._runtime_control_publisher.publish(message)

    def _publish_discontinuous_clock(self) -> None:
        if getattr(self, "_clock_publisher", None) is not None:
            self._publish_clock(force=True)

    @staticmethod
    def _reschedule_timer(timer, period_sec: float) -> bool:
        if timer is None:
            return False
        period_ns = int(round(float(period_sec) * 1_000_000_000.0))
        if int(getattr(timer, "timer_period_ns", period_ns)) == period_ns:
            return False
        timer.cancel()
        timer.timer_period_ns = period_ns
        timer.reset()
        return True

    def _sync_activity_timers_locked(self) -> None:
        tick_period_sec, control_heartbeat_sec = replay_timer_periods(
            self._state
        )
        self._reschedule_timer(
            getattr(self, "_tick_timer", None),
            tick_period_sec,
        )
        self._reschedule_timer(
            getattr(self, "_runtime_control_timer", None),
            control_heartbeat_sec,
        )
        self._reschedule_timer(
            getattr(self, "_ground_truth_timer", None),
            replay_ground_truth_timer_period(self._state),
        )

    def _publish_runtime_control_heartbeat(self) -> None:
        with self._lock:
            if self._state in ACTIVE_REPLAY_STATES:
                command = "start"
            elif self._state == "paused":
                command = "pause"
            else:
                command = "stop"
        self._publish_runtime_control(command)

    def _reset_counters(
        self,
        seek_sec: float = 0.0,
        *,
        reopen_source: bool = True,
    ) -> None:
        if self._source is None:
            self._load_source(seek_sec)
        elif reopen_source:
            self._source.open(seek_sec)
        self._run_sequence += 1
        if self._configured_run_id:
            self._run_id = (
                self._configured_run_id
                if self._run_sequence == 1
                else f"{self._configured_run_id}.restart{self._run_sequence - 1}"
            )
        else:
            self._run_id = f"{self._case_id}-{uuid.uuid4().hex[:10]}"
        self._source_time_sec = max(0.0, seek_sec)
        now = time.monotonic()
        self._wall_elapsed_sec = 0.0
        self._last_tick_at = now
        self._elastic_hold_sec = 0.0
        self._hold_reason = ""
        self._effective_playback_rate = self._playback_rate
        self._last_error = ""
        self._published_image_count = 0
        self._published_transcript_count = 0
        self._coalesced_stateful_count = 0
        self._expected_vlm_slots.clear()
        self._expected_vlm_slot_times.clear()
        self._completed_vlm_slots.clear()
        self._failed_vlm_slots.clear()
        self._active_skills.clear()
        self._active_cleanup_skills.clear()
        self._active_bed_robot_arms.clear()
        self._gate.reset()
        self._vlm_healthy = False
        self._vlm_connected = False
        self._vlm_health_source_sec = -1.0
        self._vlm_health_received_at = -1.0
        self._last_vlm_health_error = ""
        self._last_vlm_progress_at = now
        self._last_vlm_failure_signature = ""
        self._consecutive_vlm_failures = 0
        self._vlm_failure_grace_until = -1.0
        self._vlm_wait_started_at = None
        self._no_input_since_at = None
        self._drain_started_at = None
        self._drain_clear_since_at = None
        self._drain_timed_out = False
        self._vlm_resume_grace_until = -1.0
        self._state_before_manual_pause = "running"
        self._force_state_publish = True
        self._last_ground_truth_key = ""

    def _start(self, *, reset: bool) -> tuple[bool, str]:
        if self._source is None:
            self._load_source()
        if self._source is None:
            return False, self._last_error or "shadow source is unavailable"
        source_clock_reset = reset or self._state in {
            "blocked",
            "completed",
            "timed_out",
            "stopped",
            "error",
        }
        if source_clock_reset:
            self._reset_counters()
        elif not self._run_id:
            self._reset_counters(self._source_time_sec)
            source_clock_reset = True
        self._state = "running"
        self._hold_reason = ""
        self._effective_playback_rate = self._playback_rate
        self._last_tick_at = time.monotonic()
        self._force_state_publish = True
        self._sync_activity_timers_locked()
        if source_clock_reset:
            self._publish_discontinuous_clock()
        self._publish_runtime_control("start")
        return True, f"shadow replay running in {self._mode}"

    def _capture_wall_elapsed(self, now: float | None = None) -> None:
        active = self._state in {"running", "held", "draining"}
        (
            self._wall_elapsed_sec,
            self._last_tick_at,
            _,
        ) = advance_replay_elapsed(
            self._wall_elapsed_sec,
            self._last_tick_at,
            time.monotonic() if now is None else now,
            active=active,
        )

    def _state_payload(self) -> dict[str, Any]:
        return {
            "run_id": self._run_id,
            "case_id": self._case_id,
            "procedure_id": self._procedure_id,
            "state": self._state,
            "mode": self._mode,
            "loaded": self._source is not None,
            "running": self._state in {
                "running",
                "held",
                "draining",
            },
            "paused": self._state == "paused",
            "completed": self._state == "completed",
            "source_time_sec": round(self._source_time_sec, 6),
            "duration_sec": round(
                self._source.duration_sec if self._source else 0.0,
                6,
            ),
            "image_duration_sec": round(self._image_duration_sec, 6),
            "wall_elapsed_sec": round(self._wall_elapsed_sec, 6),
            "playback_rate": round(self._effective_playback_rate, 6),
            "requested_playback_rate": self._playback_rate,
            "effective_playback_rate": round(
                self._effective_playback_rate,
                6,
            ),
            "elastic_hold_sec": round(self._elastic_hold_sec, 6),
            "hold_reason": self._hold_reason,
            "last_error": self._last_error,
            "last_control_command": self._last_control_command,
            "control_reason": self._control_reason,
            "degraded_mode": (
                "asynchronous_vlm"
                if not self._vlm_required()
                else (
                    "vlm_transient_failure"
                    if self._vlm_failure_grace_active()
                    else ""
                )
            ),
            "drain_timed_out": self._drain_timed_out,
            "published_image_count": self._published_image_count,
            "published_transcript_count": self._published_transcript_count,
            "coalesced_stateful_count": (
                self._coalesced_stateful_count
            ),
            "expected_vlm_count": len(self._expected_vlm_slots),
            "completed_vlm_count": self._completed_vlm_count(),
            "failed_vlm_count": self._failed_vlm_count(),
            "pending_vlm_count": self._pending_vlm_count(),
            "consecutive_vlm_failures": self._consecutive_vlm_failures,
            "vlm_last_error": self._last_vlm_health_error,
            "active_skill_count": self._active_skill_count(),
            "active_cleanup_count": self._active_cleanup_count(),
            "vlm_lag_sec": round(self._vlm_lag_sec(), 6),
            "available_case_ids": [
                case_id
                for case_id in ALLOWED_SHADOW_CASE_IDS
                if case_id in self._case_catalog
            ],
            "ground_truth_request_file": (
                self._ground_truth_events_path.name
                if self._ground_truth_events_path is not None
                else ""
            ),
            "ground_truth_phase_file": (
                self._ground_truth_phase_path.name
                if self._ground_truth_phase_path is not None
                else ""
            ),
        }

    def _on_control(
        self,
        request: ControlShadowReplay.Request,
        response: ControlShadowReplay.Response,
    ):
        command, caller_reason = parse_control_command(request.command)
        with self._lock:
            try:
                default_reason = f"operator_{command}" if command else ""
                self._last_control_command = command
                self._control_reason = caller_reason or default_reason
                if request.mode:
                    requested_mode = str(request.mode).strip()
                    if requested_mode not in VALID_MODES:
                        raise ValueError(
                            "mode must be realtime_1x or elastic_demo"
                        )
                    self._mode = requested_mode
                if float(request.playback_rate) > 0.0:
                    self._playback_rate = max(
                        0.1,
                        min(4.0, float(request.playback_rate)),
                    )

                if command == "start":
                    success, message = self._start(
                        reset=self._state in {
                            "blocked",
                            "completed",
                            "timed_out",
                            "stopped",
                            "error",
                            "ready",
                        }
                    )
                elif command == "pause":
                    if self._state == "paused":
                        success = True
                        message = "shadow replay already paused"
                    elif self._state in {
                        "running",
                        "held",
                        "draining",
                    }:
                        self._capture_wall_elapsed()
                        self._state_before_manual_pause = self._state
                        self._state = "paused"
                        self._hold_reason = self._control_reason
                        self._effective_playback_rate = 0.0
                        # Drain any GT boundary crossed by the last active tick
                        # before lowering its polling timer to the idle cadence.
                        self._publish_ground_truth()
                        self._publish_runtime_control("pause")
                        success = True
                        message = "shadow replay paused"
                    else:
                        success = False
                        message = f"cannot pause from {self._state}"
                elif command == "resume":
                    if self._state == "paused":
                        self._state = (
                            "draining"
                            if self._state_before_manual_pause == "draining"
                            else "running"
                        )
                        self._hold_reason = ""
                        self._effective_playback_rate = self._playback_rate
                        self._last_tick_at = time.monotonic()
                        self._grant_vlm_resume_grace(self._last_tick_at)
                        self._sync_activity_timers_locked()
                        self._publish_runtime_control("resume")
                        success = True
                        message = "shadow replay resumed"
                    elif self._state in {"running", "held", "draining"}:
                        success = True
                        message = "shadow replay already active"
                    else:
                        success = False
                        message = f"cannot resume from {self._state}"
                elif command == "restart":
                    self._reset_counters()
                    self._state = "ready"
                    self._effective_playback_rate = 0.0
                    self._publish_discontinuous_clock()
                    self._publish_ground_truth(force=True)
                    self._publish_runtime_control("reset")
                    success = True
                    message = "shadow replay rewound; start when runtime is ready"
                elif command == "stop":
                    self._capture_wall_elapsed()
                    self._state = "stopped"
                    self._hold_reason = self._control_reason
                    self._effective_playback_rate = 0.0
                    self._expected_vlm_slots.clear()
                    self._expected_vlm_slot_times.clear()
                    self._completed_vlm_slots.clear()
                    self._failed_vlm_slots.clear()
                    self._active_skills.clear()
                    self._active_cleanup_skills.clear()
                    self._active_bed_robot_arms.clear()
                    self._vlm_wait_started_at = None
                    self._no_input_since_at = None
                    self._publish_ground_truth()
                    self._publish_runtime_control("stop")
                    success = True
                    message = "shadow replay stopped"
                elif command == "seek":
                    if self._state in {"running", "held"}:
                        raise ValueError("pause replay before seeking")
                    seek_sec = max(
                        0.0,
                        min(
                            float(request.seek_sec),
                            self._source.duration_sec if self._source else 0.0,
                        ),
                    )
                    self._reset_counters(seek_sec)
                    self._state = "paused"
                    self._state_before_manual_pause = "running"
                    self._hold_reason = self._control_reason
                    self._effective_playback_rate = 0.0
                    self._publish_discontinuous_clock()
                    self._publish_ground_truth(force=True)
                    self._publish_runtime_control("reset")
                    success = True
                    message = f"shadow replay seeked to {seek_sec:.2f}s"
                elif command == "status":
                    success = True
                    message = "shadow replay status"
                else:
                    success = False
                    message = f"unsupported shadow replay command '{command}'"
            except Exception as exc:
                success = False
                message = str(exc)
                self._last_error = message
            self._force_state_publish = True
            self._sync_activity_timers_locked()
            if getattr(self, "_state_publisher", None) is not None:
                self._publish_state(force=True)
            response.success = bool(success)
            response.message = message
            response.state_json = json.dumps(
                self._state_payload(),
                separators=(",", ":"),
                sort_keys=True,
            )
            return response

    def _on_select_case(
        self,
        request: SelectShadowCase.Request,
        response: SelectShadowCase.Response,
    ):
        new_source: FilteredBagSource | None = None
        old_source: FilteredBagSource | None = None
        with self._lock:
            try:
                refreshed_catalog = resolve_shadow_case_catalog(
                    current_bag_path=self._bag_path,
                    annotation_cases_root=self._annotation_cases_root,
                )
                asset = validate_shadow_case_selection(
                    case_id=request.case_id,
                    replay_state=self._state,
                    catalog=refreshed_catalog,
                )
                request_intervals = load_implicit_request_intervals(
                    asset.request_events_path
                )
                phase_events = load_phase_ground_truth_events(
                    asset.phase_events_path
                )
                if not phase_events:
                    raise ValueError(
                        f"shadow case '{asset.case_id}' has no phase events"
                    )
                new_source = FilteredBagSource(
                    asset.bag_path,
                    [
                        *self._image_topic_routes,
                        *self._json_topic_routes,
                        self._transcript_topic,
                    ],
                    self._storage_id,
                )

                old_source = self._source
                self._source = new_source
                new_source = None
                self._case_catalog = refreshed_catalog
                self._case_id = asset.case_id
                self._bag_path = str(asset.bag_path)
                self._ground_truth_events_path = (
                    asset.request_events_path
                )
                self._ground_truth_phase_path = asset.phase_events_path
                self._implicit_request_intervals = request_intervals
                self._phase_ground_truth_events = phase_events
                self._image_duration_sec = self._source.duration_sec
                self._run_sequence = 0
                self._run_id = ""
                self._reset_counters(reopen_source=False)
                self._state = "ready"
                self._hold_reason = ""
                self._effective_playback_rate = 0.0
                self._last_control_command = "select_case"
                self._control_reason = "case_selected"
                self._last_error = ""
                self._force_state_publish = True
                self._last_ground_truth_key = ""
                self._publish_discontinuous_clock()
                self._publish_ground_truth(force=True)
                self._publish_runtime_control("reset")
                self._sync_activity_timers_locked()
                response.success = True
                response.message = (
                    f"shadow case {asset.case_id} loaded and rewound"
                )
            except Exception as exc:
                if new_source is not None:
                    new_source.close()
                response.success = False
                response.message = str(exc)

            if old_source is not None:
                old_source.close()
            response.state_json = json.dumps(
                self._state_payload(),
                separators=(",", ":"),
                sort_keys=True,
            )
            self._publish_state(force=True)
            return response

    def _on_vlm_health(self, msg: VLMHealth) -> None:
        with self._lock:
            now = time.monotonic()
            self._vlm_health_received_at = now
            self._vlm_connected = bool(msg.connected)
            if not self._vlm_required():
                # Optional VLM means the media clock does not wait for visual
                # inference. It does not mean that perception is disabled or
                # that image/result observations should be discarded.
                self._vlm_healthy = bool(
                    msg.connected and msg.healthy and not msg.last_error
                )
                self._vlm_health_source_sec = self._source_time_sec
                self._last_vlm_health_error = str(msg.last_error or "")
                if self._vlm_healthy:
                    self._last_vlm_progress_at = now
                    self._no_input_since_at = None
                self._force_state_publish = True
                return
            if _is_no_input_vlm_health(msg):
                # Allow short source-video gaps to advance, but do not let a
                # permanently missing perception pipeline look healthy.
                self._vlm_healthy = True
                self._vlm_health_source_sec = self._source_time_sec
                self._last_vlm_health_error = str(msg.last_error or "")
                if self._no_input_since_at is None:
                    self._no_input_since_at = time.monotonic()
                return
            self._vlm_healthy = bool(
                msg.connected and msg.healthy and not msg.last_error
            )
            self._vlm_health_source_sec = self._source_time_sec
            self._last_vlm_health_error = str(msg.last_error or "")
            if self._vlm_healthy:
                self._no_input_since_at = None
                return
            if msg.connected and msg.last_error:
                signature = (
                    f"{_seconds_from_stamp(msg.stamp):.9f}:"
                    f"{str(msg.last_error).strip()}"
                )
                if signature != self._last_vlm_failure_signature:
                    self._last_vlm_failure_signature = signature
                    self._consecutive_vlm_failures += 1
                    self._mark_oldest_pending_vlm_failed()
                    if (
                        self._consecutive_vlm_failures
                        <= self._vlm_max_consecutive_failures
                    ):
                        self._vlm_failure_grace_until = (
                            now + self._vlm_transient_failure_grace_sec
                        )
                    else:
                        self._vlm_failure_grace_until = -1.0
                    self._force_state_publish = True

    def _on_perception_health(self, msg: String) -> None:
        enabled = perception_enabled_from_health(msg)
        if enabled is None:
            return
        with self._lock:
            previous = self._perception_enabled
            self._perception_health_received = True
            self._perception_enabled = enabled
            if enabled == previous:
                return
            now = time.monotonic()
            self._gate.reset()
            self._vlm_wait_started_at = None
            self._no_input_since_at = None
            self._last_vlm_progress_at = now
            self._consecutive_vlm_failures = 0
            self._vlm_failure_grace_until = -1.0
            self._last_vlm_failure_signature = ""
            if not enabled:
                self._vlm_healthy = False
                self._vlm_health_received_at = -1.0
                self._last_vlm_health_error = (
                    "waiting for detector-independent raw visual inference"
                )
            else:
                self._vlm_healthy = False
                self._vlm_health_received_at = -1.0
                self._last_vlm_health_error = (
                    "waiting for fresh RF-DETR frame"
                )
                self._vlm_resume_grace_until = (
                    now + self._vlm_health_timeout_sec
                )
            self._force_state_publish = True

    def _mark_vlm_slot_complete(self, stamp: Any) -> None:
        slot_id = vlm_result_observation_id(stamp)
        if slot_id is None or slot_id in self._completed_vlm_slots:
            return
        self._completed_vlm_slots.add(slot_id)
        self._force_state_publish = True

    def _on_vlm_input_image(self, msg: CompressedImage) -> None:
        stamp_sec = _seconds_from_stamp(msg.header.stamp)
        with self._lock:
            if self._state not in {"running", "held", "paused", "draining"}:
                return
            slot_id = record_vlm_input_obligation(
                stamp_sec=stamp_sec,
                period_sec=self._gate.vlm_period_sec,
                image_duration_sec=self._image_duration_sec,
                expected_slots=self._expected_vlm_slots,
                expected_slot_times=self._expected_vlm_slot_times,
            )
            if slot_id is not None:
                self._force_state_publish = True

    def _mark_oldest_pending_vlm_failed(self) -> int | None:
        unresolved = sorted(
            (
                slot_id
                for slot_id in self._expected_vlm_slots
                if slot_id not in self._completed_vlm_slots
                and slot_id not in self._failed_vlm_slots
            ),
            key=lambda slot_id: self._expected_vlm_slot_times.get(
                slot_id,
                float(slot_id) / 1_000_000_000.0,
            ),
        )
        if not unresolved:
            return None
        failed_slot = unresolved[0]
        self._failed_vlm_slots.add(failed_slot)
        return failed_slot

    def _on_vlm_result(self, msg: VLMResult) -> None:
        with self._lock:
            if self._state not in {
                "running",
                "held",
                "paused",
                "draining",
            }:
                return
            result_time = _seconds_from_stamp(msg.stamp)
            if result_time > self._source_time_sec + 5.0:
                return
            self._mark_vlm_slot_complete(msg.stamp)
            self._vlm_healthy = True
            self._vlm_connected = True
            self._vlm_health_source_sec = self._source_time_sec
            self._vlm_health_received_at = time.monotonic()
            self._last_vlm_progress_at = time.monotonic()
            self._consecutive_vlm_failures = 0
            self._vlm_failure_grace_until = -1.0
            self._last_vlm_failure_signature = ""
            self._vlm_wait_started_at = None
            self._no_input_since_at = None
            self._last_vlm_health_error = ""

    def _on_skill_status(self, msg: SkillStatus) -> None:
        command_id = str(msg.command_id or "").strip()
        if not command_id:
            return
        state = str(msg.state or "").strip().lower()
        action = str(msg.action or "").strip().lower()
        is_cleanup = bool(
            "clean" in action
            or "clean" in state
            or str(msg.target_owner or "").strip().lower() == "cleaner"
        )
        with self._lock:
            before = (
                command_id in self._active_skills,
                command_id in self._active_cleanup_skills,
            )
            if state in TERMINAL_SKILL_STATES or bool(msg.success):
                self._active_skills.discard(command_id)
                self._active_cleanup_skills.discard(command_id)
            elif state in RUNNING_SKILL_STATES:
                if is_cleanup:
                    self._active_cleanup_skills.add(command_id)
                    self._active_skills.discard(command_id)
                else:
                    self._active_skills.add(command_id)
                    self._active_cleanup_skills.discard(command_id)
            after = (
                command_id in self._active_skills,
                command_id in self._active_cleanup_skills,
            )
            if before != after:
                if before != (False, False) and after == (False, False):
                    self._grant_vlm_resume_grace(time.monotonic())
                self._force_state_publish = True

    def _on_bed_robot_arm_status(self, msg: BedRobotArmStateArray) -> None:
        active = {
            str(arm.arm_id).strip()
            for arm in msg.arms
            if str(arm.arm_id).strip()
            and str(arm.state).strip().lower() in TRANSIENT_BED_ROBOT_ARM_STATES
        }
        with self._lock:
            previous = self._active_bed_robot_arms
            if previous != active:
                if previous and not active:
                    self._grant_vlm_resume_grace(time.monotonic())
                self._active_bed_robot_arms = active
                self._force_state_publish = True

    def _active_skill_count(self) -> int:
        return (
            len(self._active_skills)
            + len(self._active_cleanup_skills)
            + len(self._active_bed_robot_arms)
        )

    def _active_non_cleanup_count(self) -> int:
        return len(self._active_skills) + len(self._active_bed_robot_arms)

    def _active_cleanup_count(self) -> int:
        return len(self._active_cleanup_skills)

    def _completed_vlm_count(self) -> int:
        completed, _, _ = vlm_obligation_progress(
            self._expected_vlm_slots,
            self._completed_vlm_slots,
            self._failed_vlm_slots,
        )
        return completed

    def _failed_vlm_count(self) -> int:
        _, failed, _ = vlm_obligation_progress(
            self._expected_vlm_slots,
            self._completed_vlm_slots,
            self._failed_vlm_slots,
        )
        return failed

    def _pending_vlm_count(self) -> int:
        _, _, pending = vlm_obligation_progress(
            self._expected_vlm_slots,
            self._completed_vlm_slots,
            self._failed_vlm_slots,
        )
        return pending

    def _vlm_lag_sec(self) -> float:
        return pending_vlm_source_lag_sec(
            source_time_sec=self._source_time_sec,
            expected_slots=self._expected_vlm_slots,
            expected_slot_times=self._expected_vlm_slot_times,
            completed_slots=self._completed_vlm_slots,
            failed_slots=self._failed_vlm_slots,
            period_sec=self._gate.vlm_period_sec,
        )

    def _vlm_required(self) -> bool:
        return bool(self._gate.require_vlm)

    def _grant_vlm_resume_grace(self, now: float) -> None:
        if not self._vlm_healthy:
            return
        self._vlm_resume_grace_until = max(
            self._vlm_resume_grace_until,
            float(now) + self._vlm_health_timeout_sec,
        )
        self._vlm_health_received_at = float(now)
        self._last_vlm_progress_at = float(now)
        self._vlm_wait_started_at = None
        self._no_input_since_at = None

    def _vlm_ready(self) -> bool:
        now = time.monotonic()
        return bool(
            self._vlm_healthy
            and self._vlm_health_received_at >= 0.0
            and (
                now <= self._vlm_resume_grace_until
                or now - self._vlm_health_received_at
                <= self._vlm_health_timeout_sec
            )
        )

    def _vlm_failure_grace_active(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        return bool(
            self._vlm_connected
            and 0 < self._consecutive_vlm_failures
            <= self._vlm_max_consecutive_failures
            and current <= self._vlm_failure_grace_until
        )

    def _vlm_ready_for_sync(self, now: float) -> bool:
        return self._vlm_ready() or self._vlm_failure_grace_active(now)

    def _publish_clock(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not replay_clock_publish_due(
            source_time_sec=self._source_time_sec,
            last_source_time_sec=self._last_clock_source_time_sec,
            now_monotonic=now,
            last_publish_monotonic=self._last_clock_publish_at,
            idle_heartbeat_sec=self._idle_clock_heartbeat_sec,
            force=force,
        ):
            return False
        clock = Clock()
        clock.clock = _time_message(self._source_time_sec)
        self._clock_publisher.publish(clock)
        self._last_clock_publish_at = now
        self._last_clock_source_time_sec = float(self._source_time_sec)
        return True

    def _image_input_active(self) -> bool:
        return bool(
            self._published_image_count > 0
            and self._source_time_sec <= self._image_duration_sec
        )

    def _vlm_timeout_error(
        self,
        *,
        now: float,
        hold_reason: str,
    ) -> str:
        _ = hold_reason
        require_vlm = self._vlm_required()
        if not require_vlm or not self._image_input_active():
            self._vlm_wait_started_at = None
            return ""
        no_input_elapsed: float | None = None
        pending_vlm_count = self._pending_vlm_count()
        if (
            self._no_input_since_at is not None
            and pending_vlm_count > 0
        ):
            no_input_elapsed = now - self._no_input_since_at
        if pending_vlm_count <= 0:
            self._vlm_wait_started_at = None
            wait_elapsed = None
        else:
            if self._vlm_wait_started_at is None:
                self._vlm_wait_started_at = now
            progress_reference = max(
                self._vlm_wait_started_at,
                self._last_vlm_progress_at,
            )
            wait_elapsed = now - progress_reference
        return vlm_watchdog_error(
            require_vlm=require_vlm,
            image_input_active=self._image_input_active(),
            timeout_sec=self._vlm_wait_timeout_sec,
            source_time_sec=self._source_time_sec,
            pending_vlm_count=pending_vlm_count,
            hold_reason="observation_backlog",
            no_input_elapsed_sec=no_input_elapsed,
            response_wait_elapsed_sec=wait_elapsed,
            last_input_error=self._last_vlm_health_error,
        )

    def _block(self, message: str, *, reason: str) -> None:
        self._state = "blocked"
        self._hold_reason = reason
        self._last_error = message
        self._effective_playback_rate = 0.0
        self._force_state_publish = True
        self._publish_ground_truth()
        self._publish_runtime_control("stop")
        self.get_logger().error(message)

    def _enter_draining(self, now: float) -> None:
        if self._state == "draining":
            return
        self._state = "draining"
        self._hold_reason = "draining"
        self._effective_playback_rate = 0.0
        self._drain_started_at = now
        self._drain_clear_since_at = None
        self._force_state_publish = True

    def _tick_draining(self, now: float) -> None:
        if self._drain_started_at is None:
            self._drain_started_at = now
        drain_elapsed_sec = max(0.0, now - self._drain_started_at)
        decision = replay_drain_decision(
            require_vlm=self._vlm_required(),
            pending_vlm_count=self._pending_vlm_count(),
            active_skill_count=self._active_non_cleanup_count(),
            active_cleanup_count=self._active_cleanup_count(),
            elapsed_sec=drain_elapsed_sec,
            timeout_sec=self._drain_timeout_sec,
        )
        self._hold_reason = decision.hold_reason
        if not decision.completed:
            self._drain_clear_since_at = None
            return
        self._drain_timed_out = decision.timed_out
        if decision.timed_out:
            self._state = "timed_out"
            pending = ",".join(decision.pending_labels)
            self._last_error = (
                "shadow replay drain timeout after "
                f"{self._drain_timeout_sec:.1f}s; pending={pending}"
            )
            self._force_state_publish = True
            self._publish_ground_truth()
            self._publish_runtime_control("stop")
            return
        if self._drain_clear_since_at is None:
            self._drain_clear_since_at = now
            self._hold_reason = "draining:settle"
            return
        if now - self._drain_clear_since_at < self._drain_settle_sec:
            self._hold_reason = "draining:settle"
            return
        self._state = "completed"
        self._hold_reason = ""
        self._force_state_publish = True
        self._publish_ground_truth()
        self._publish_runtime_control("stop")

    def _publish_due_records(self) -> None:
        if self._source is None:
            return
        due_records, coalesced_count = coalesce_stateful_records(
            self._source.pop_due(self._source_time_sec),
            # Camera frames are an ordered media stream, not state snapshots.
            # Publish every due frame and let the independent VLM backpressure
            # path choose which observations it can process.
            stateful_topics=set(self._json_topic_routes),
        )
        self._coalesced_stateful_count += coalesced_count
        for topic, serialized, source_sec in due_records:
            try:
                if topic in self._image_topic_routes:
                    image = deserialize_message(serialized, CompressedImage)
                    for output_topic in self._image_topic_routes[topic]:
                        self._image_publishers[output_topic].publish(image)
                    if topic == self._source_flir_topic:
                        self._published_image_count += 1
                elif topic in self._json_topic_routes:
                    payload = deserialize_message(serialized, String)
                    self._json_publishers[topic].publish(payload)
                elif topic == self._transcript_topic:
                    transcript = deserialize_message(serialized, String)
                    self._transcript_publisher.publish(transcript)
                    self._published_transcript_count += 1
            except Exception as exc:
                self._last_error = (
                    f"failed to publish {topic} at {source_sec:.3f}s: {exc}"
                )
                self.get_logger().error(self._last_error)

    def _tick(self) -> None:
        with self._lock:
            now = time.monotonic()
            (
                self._wall_elapsed_sec,
                self._last_tick_at,
                delta,
            ) = advance_replay_elapsed(
                self._wall_elapsed_sec,
                self._last_tick_at,
                now,
                active=self._state in {"running", "held", "draining"},
            )

            if self._state in {"running", "held"}:
                # Publish records already due at the current source time before
                # deciding whether perception/VLM backlog should slow playback.
                # This keeps the backlog ledger tied to real public frames and
                # avoids inventing work inside recorded frame gaps.
                self._publish_due_records()
                grace_elapsed = (
                    self._wall_elapsed_sec >= self._vlm_startup_grace_sec
                )
                sync = self._gate.sync_decision(
                    mode=self._mode,
                    source_time_sec=self._source_time_sec,
                    image_duration_sec=self._image_duration_sec,
                    published_image_count=self._published_image_count,
                    completed_vlm_count=self._completed_vlm_count(),
                    active_skill_count=self._active_non_cleanup_count(),
                    active_cleanup_count=self._active_cleanup_count(),
                    vlm_ready=self._vlm_ready_for_sync(now),
                    vlm_grace_elapsed=grace_elapsed,
                    observed_pending_vlm_count=self._pending_vlm_count(),
                    observed_vlm_lag_sec=self._vlm_lag_sec(),
                    require_vlm=self._vlm_required(),
                )
                timeout_error = self._vlm_timeout_error(
                    now=now,
                    hold_reason=sync.hold_reason,
                )
                if timeout_error:
                    if timeout_error != self._last_vlm_health_error:
                        self._last_vlm_health_error = timeout_error
                        self._force_state_publish = True
                if sync.hold_reason:
                    self._state = "held"
                    self._hold_reason = sync.hold_reason
                    self._effective_playback_rate = 0.0
                    self._elastic_hold_sec += delta
                else:
                    self._state = "running"
                    self._hold_reason = ""
                    self._effective_playback_rate = (
                        self._playback_rate
                        * sync.playback_rate_factor
                    )
                    duration = (
                        self._source.duration_sec if self._source else 0.0
                    )
                    self._source_time_sec = advance_replay_source_time(
                        self._source_time_sec,
                        delta,
                        self._effective_playback_rate,
                        duration,
                        advancing=True,
                    )
                self._publish_clock()
                self._publish_due_records()
                if (
                    self._source is not None
                    and self._source_time_sec >= self._source.duration_sec
                    and self._source.exhausted()
                ):
                    self._enter_draining(now)
                    self._tick_draining(now)
            elif self._state == "draining":
                self._effective_playback_rate = 0.0
                self._publish_clock()
                self._publish_due_records()
                self._tick_draining(now)
            elif self._state in {"paused", "ready"}:
                self._effective_playback_rate = 0.0
                # The adaptive idle timer is already the 1 Hz throttle.  A
                # second elapsed-time gate here can see a 0.999s callback and
                # skip until the next tick, degrading /clock to about 0.5 Hz.
                self._publish_clock(force=True)
            else:
                # Keep source time visible to late-joining consumers while a
                # replay is stopped, completed, blocked, or unavailable.
                self._effective_playback_rate = 0.0
                self._publish_clock(force=True)
            self._sync_activity_timers_locked()
            if (
                self._force_state_publish
                and getattr(self, "_state_publisher", None) is not None
            ):
                self._publish_state(force=True)

    def _publish_state(self, *, force: bool = False) -> None:
        with self._lock:
            payload = self._state_payload()
            now = time.monotonic()
            change_key = replay_state_change_key(payload)
            changed = change_key != self._last_state_change_key
            heartbeat_due = (
                now - self._last_state_publish_at
                >= self._state_heartbeat_sec
            )
            if not (
                force
                or self._force_state_publish
                or changed
                or heartbeat_due
            ):
                return
            msg = ShadowReplayState()
            msg.stamp = self.get_clock().now().to_msg()
            msg.run_id = str(payload["run_id"])
            msg.case_id = str(payload["case_id"])
            msg.procedure_id = str(payload["procedure_id"])
            msg.state = str(payload["state"])
            msg.mode = str(payload["mode"])
            msg.loaded = self._source is not None
            msg.running = self._state in {
                "running",
                "held",
                "draining",
            }
            msg.paused = self._state == "paused"
            msg.completed = self._state == "completed"
            msg.source_time_sec = float(payload["source_time_sec"])
            msg.duration_sec = float(payload["duration_sec"])
            msg.image_duration_sec = float(payload["image_duration_sec"])
            msg.wall_elapsed_sec = float(payload["wall_elapsed_sec"])
            msg.playback_rate = float(payload["effective_playback_rate"])
            msg.elastic_hold_sec = float(payload["elastic_hold_sec"])
            msg.hold_reason = str(payload["hold_reason"])
            msg.last_error = str(payload["last_error"])
            msg.published_image_count = int(payload["published_image_count"])
            msg.published_transcript_count = int(
                payload["published_transcript_count"]
            )
            msg.completed_vlm_count = int(payload["completed_vlm_count"])
            msg.pending_vlm_count = int(payload["pending_vlm_count"])
            msg.active_skill_count = int(payload["active_skill_count"])
            self._state_publisher.publish(msg)
            self._last_state_change_key = change_key
            self._last_state_publish_at = now
            self._force_state_publish = False

    def _publish_ground_truth(self, *, force: bool = False) -> None:
        with self._lock:
            payload = ground_truth_state_payload(
                run_id=self._run_id,
                case_id=self._case_id,
                source_time_sec=self._source_time_sec,
                duration_sec=(
                    self._source.duration_sec if self._source else 0.0
                ),
                request_intervals=self._implicit_request_intervals,
                phase_events=self._phase_ground_truth_events,
            )
            change_key = json.dumps(
                {
                    "run_id": payload["run_id"],
                    "available": payload["available"],
                    "event_id": payload["implicit_tool_request"][
                        "event_id"
                    ],
                    "active": payload["implicit_tool_request"]["active"],
                    "phase_event_id": payload["phase"]["event_id"],
                    "phase_id": payload["phase"]["phase_id"],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            if not force and change_key == self._last_ground_truth_key:
                return
            msg = String()
            msg.data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._ground_truth_publisher.publish(msg)
            self._last_ground_truth_key = change_key


def main() -> None:
    rclpy.init()
    node = InteractiveReplayControllerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
