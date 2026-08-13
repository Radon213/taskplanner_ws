"""Configuration, validation, and deterministic voice parsing for Debug Mode."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml


CONFIG_SCHEMA = "taskplanner.integration_debug.v1"
VALID_TOOL_TRANSITIONS = {
    ("tray", "robot"),
    ("tray", "surgeon"),
    ("robot", "surgeon"),
    ("robot", "tray"),
    ("mayo", "robot"),
    ("mayo", "tray"),
}
VALID_ARM_IDS = {"arm_1", "arm_2"}
VALID_TARGET_TOOL_IDS = {"thyroid_retractor", "army_navy_retractor"}
VALID_ADJUSTMENT_MODES = {"single", "multi"}
VALID_TARGET_RETRACTOR_IDS = {
    "left_malleable",
    "right_malleable",
    "both_malleable",
}
VALID_RETRACTION_DIRECTIONS = {"up", "down", "left", "right", "none"}
VALID_RETRACTION_AXES = {"left_right", "up_down", "none"}
MAX_RETRACTION_DISTANCE_MM = 30.0
DEFAULT_ACTION_WATCHDOG_POLICY = {
    "goal_response_timeout_sec": 10.0,
    "feedback_timeout_sec": 30.0,
    "max_duration_sec": 300.0,
    "server_loss_grace_sec": 5.0,
}
VALID_BED_ROBOT_ARM_STATES = {
    "standby",
    "direct_teach",
    "retracting",
    "changing_tool",
    "moving_to_standby",
    "fault",
    "protective_stop",
    "unknown",
}
BED_ROBOT_PROCEDURE_LAYOUTS = {
    "thyroidectomy": {"army_navy"},
    "nephrectomy": {"left_malleable", "right_malleable"},
}


@dataclass(frozen=True, slots=True)
class VoiceParse:
    matched: bool
    ambiguous: bool
    operation: str = ""
    payload: dict[str, Any] | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "operation": self.operation,
            "payload": dict(self.payload or {}),
            "reason": self.reason,
        }


def load_config(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError("integration Debug Mode config schema is invalid")
    if not isinstance(payload.get("inputs"), list) or not payload["inputs"]:
        raise ValueError("integration Debug Mode config requires input endpoints")
    if not isinstance(payload.get("outputs"), list) or not payload["outputs"]:
        raise ValueError("integration Debug Mode config requires output endpoints")
    return payload


def decode_payload(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload_json must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload_json must be a JSON object")
    return payload


def load_action_watchdog_policy(config: dict[str, Any]) -> dict[str, float]:
    """Normalize the Action watchdog policy and reject unsafe timeout values."""

    configured = config.get("action_watchdog", {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("action_watchdog must be a mapping")
    policy: dict[str, float] = {}
    for key, default in DEFAULT_ACTION_WATCHDOG_POLICY.items():
        try:
            value = float(configured.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"action_watchdog.{key} must be numeric") from exc
        if value <= 0.0:
            raise ValueError(f"action_watchdog.{key} must be greater than 0")
        policy[key] = value
    if policy["max_duration_sec"] <= policy["goal_response_timeout_sec"]:
        raise ValueError(
            "action_watchdog.max_duration_sec must exceed goal_response_timeout_sec"
        )
    return policy


def action_watchdog_reason(
    *,
    terminal: bool,
    recovery_required: bool,
    state: str,
    route: str,
    elapsed_sec: float,
    last_update_age_sec: float,
    server_ready: bool,
    server_unavailable_age_sec: float,
    policy: dict[str, float],
) -> str:
    """Return a stable reason code when an in-flight command becomes uncertain."""

    if terminal or recovery_required:
        return ""
    if (
        not server_ready
        and server_unavailable_age_sec >= policy["server_loss_grace_sec"]
    ):
        return (
            "service_server_unavailable"
            if route == "tool_change"
            else "action_server_unavailable"
        )
    if elapsed_sec >= policy["max_duration_sec"]:
        return "action_duration_timeout"
    if state == "submitting" and elapsed_sec >= policy["goal_response_timeout_sec"]:
        return (
            "service_response_timeout"
            if route == "tool_change"
            else "goal_response_timeout"
        )
    if state != "submitting" and last_update_age_sec >= policy["feedback_timeout_sec"]:
        return "action_update_timeout"
    return ""


def validate_action_recovery_acknowledgement(
    payload: dict[str, Any], active_command_id: str
) -> str:
    """Require exact command identity and an explicit remote-stop confirmation."""

    command_id = str(payload.get("expected_command_id", "")).strip()
    if not active_command_id:
        raise ValueError("there is no active Action client state to recover")
    if command_id != active_command_id:
        raise ValueError(
            "active command changed; refresh the status before recovering the client"
        )
    if payload.get("remote_motion_stopped_confirmed") is not True:
        raise ValueError(
            "confirm that remote motion stopped or the remote command state was checked"
        )
    return command_id


def validate_planner_coexistence_acknowledgement(
    payload: dict[str, Any], blocked_nodes: Iterable[str]
) -> list[str]:
    """Require an explicit acknowledgement of the exact discovered planner set."""

    expected = sorted(
        {str(node).strip() for node in blocked_nodes if str(node).strip()}
    )
    if not expected:
        return []
    if payload.get("planner_coexistence_confirmed") is not True:
        raise ValueError(
            "confirm that the partner planner is paused before arming manual control"
        )
    acknowledged = payload.get("acknowledged_blocked_nodes")
    if not isinstance(acknowledged, list) or not all(
        isinstance(node, str) and node.strip() for node in acknowledged
    ):
        raise ValueError("acknowledged_blocked_nodes must be a list of node names")
    normalized = sorted({node.strip() for node in acknowledged})
    if normalized != expected:
        raise ValueError(
            "planner node set changed; refresh the status and acknowledge it again"
        )
    return normalized


def manual_write_block_reason(
    *,
    armed: bool,
    fault_locked: bool,
    blocked_nodes: Iterable[str],
    planner_coexistence_allowed: bool,
    acknowledged_blocked_nodes: Iterable[str],
) -> str:
    """Return the fail-closed reason for a Debug Mode ROS write.

    Every ROS write requires an armed session.  Once a Taskplanner runtime is
    discovered, the explicit coexistence policy and an acknowledgement of the
    exact current node set are required as well.
    """

    blocked = sorted(
        {str(node).strip() for node in blocked_nodes if str(node).strip()}
    )
    acknowledged = sorted(
        {
            str(node).strip()
            for node in acknowledged_blocked_nodes
            if str(node).strip()
        }
    )
    if fault_locked:
        return "manual control is fault locked"
    if blocked:
        if not planner_coexistence_allowed:
            return "full Taskplanner nodes are active: " + ", ".join(blocked)
        if armed and acknowledged != blocked:
            return (
                "planner node set changed; refresh the status and arm manual "
                "control with an exact coexistence acknowledgement"
            )
    if not armed:
        return "manual control is not armed"
    return ""


SAFE_STOPPED_EXECUTION_STATES = {
    "idle",
    "halted",
    "stopped",
    "completed",
    "terminated",
}
UNSAFE_OPERATIONAL_ROBOT_STATES = {
    "busy",
    "cleaning",
    "executing",
    "handover_in_progress",
    "handover_ready",
    "moving",
    "picking",
    "ready_to_return",
    "recovery_in_progress",
    "returning_home",
    "stopping",
}


def operational_runtime_stopped(
    *,
    received: bool,
    running: bool,
    execution_state: str,
    active_robot_task_id: str,
    robot_state: str,
    cleaner_busy: bool,
    publisher_trusted: bool,
    age_sec: float | None,
    max_age_sec: float,
) -> bool:
    """Require a fresh, explicit stopped state before integrated manual writes."""

    state = str(execution_state).strip().lower()
    robot = str(robot_state).strip().lower()
    return bool(
        received
        and publisher_trusted
        and age_sec is not None
        and 0.0 <= age_sec <= max_age_sec
        and not running
        and state in SAFE_STOPPED_EXECUTION_STATES
        and not str(active_robot_task_id).strip()
        and robot not in UNSAFE_OPERATIONAL_ROBOT_STATES
        and not cleaner_busy
    )


def operational_state_publisher_trusted(
    publisher_identities: Iterable[str], expected_identity: str
) -> bool:
    """Trust a runtime-stop signal only from one exact publisher identity."""

    publishers = [
        str(identity).strip()
        for identity in publisher_identities
        if str(identity).strip()
    ]
    return len(publishers) == 1 and publishers[0] == str(expected_identity).strip()


def validate_tool_handover(payload: dict[str, Any]) -> dict[str, Any]:
    instrument_id = str(payload.get("instrument_id", "")).strip()
    instance_id = str(payload.get("instrument_instance_id", "")).strip()
    source = str(payload.get("source_location", "")).strip().lower()
    target = str(payload.get("target_location", "")).strip().lower()
    if not instrument_id:
        raise ValueError("instrument_id is required")
    if (source, target) not in VALID_TOOL_TRANSITIONS:
        raise ValueError("unsupported tool handover transition")
    return {
        "instrument_id": instrument_id,
        "instrument_instance_id": instance_id or f"{instrument_id}#1",
        "source_location": source,
        "target_location": target,
    }


def validate_tool_change(payload: dict[str, Any]) -> dict[str, str]:
    arm_id = str(payload.get("arm_id", "")).strip().lower()
    target_tool_id = str(payload.get("target_tool_id", "")).strip().lower()
    if arm_id not in VALID_ARM_IDS:
        raise ValueError("arm_id must be arm_1 or arm_2")
    if target_tool_id not in VALID_TARGET_TOOL_IDS:
        raise ValueError("unsupported target_tool_id")
    return {"arm_id": arm_id, "target_tool_id": target_tool_id}


def validate_retraction_adjustment(payload: dict[str, Any]) -> dict[str, Any]:
    adjustment_mode = str(payload.get("adjustment_mode", "")).strip().lower()
    target_retractor_id = str(payload.get("target_retractor_id", "")).strip().lower()
    direction_frame = str(payload.get("direction_frame", "surgeon_view")).strip().lower()
    direction = str(payload.get("direction", "none")).strip().lower()
    axis = str(payload.get("axis", "none")).strip().lower()
    if adjustment_mode not in VALID_ADJUSTMENT_MODES:
        raise ValueError("adjustment_mode must be single or multi")
    if target_retractor_id not in VALID_TARGET_RETRACTOR_IDS:
        raise ValueError("unsupported target_retractor_id")
    if adjustment_mode == "single" and target_retractor_id == "both_malleable":
        raise ValueError("single adjustment requires one target retractor")
    if adjustment_mode == "multi" and target_retractor_id != "both_malleable":
        raise ValueError("multi adjustment requires both_malleable")
    if direction_frame != "surgeon_view":
        raise ValueError("direction_frame must be surgeon_view")
    if direction not in VALID_RETRACTION_DIRECTIONS:
        raise ValueError("unsupported retraction direction")
    if axis not in VALID_RETRACTION_AXES:
        raise ValueError("unsupported retraction axis")
    if adjustment_mode == "single" and (direction == "none" or axis != "none"):
        raise ValueError("single adjustment requires a direction and axis=none")
    if adjustment_mode == "multi" and (axis == "none" or direction != "none"):
        raise ValueError("multi adjustment requires an axis and direction=none")
    try:
        distance_mm = float(payload.get("distance_mm", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("distance_mm must be numeric") from exc
    if not 0.0 < distance_mm <= MAX_RETRACTION_DISTANCE_MM:
        raise ValueError("distance_mm must be greater than 0 and at most 30")
    return {
        "adjustment_mode": adjustment_mode,
        "target_retractor_id": target_retractor_id,
        "direction_frame": direction_frame,
        "direction": direction,
        "axis": axis,
        "distance_mm": distance_mm,
    }


def validate_bed_robot_arm_status(
    procedure_type: str,
    arms: Iterable[Any],
) -> list[dict[str, Any]]:
    """Validate only the controller-owned fields defined by the public document."""

    normalized_procedure = str(procedure_type).strip().casefold()
    expected_roles = BED_ROBOT_PROCEDURE_LAYOUTS.get(normalized_procedure)
    if expected_roles is None:
        raise ValueError("unsupported bed robot procedure_type")

    normalized: list[dict[str, Any]] = []
    arm_ids: set[str] = set()
    roles: set[str] = set()
    for raw in arms:
        value = (
            (lambda key, default="": raw.get(key, default))
            if isinstance(raw, dict)
            else (lambda key, default="": getattr(raw, key, default))
        )
        arm_id = str(value("arm_id")).strip()
        role = str(value("role")).strip()
        role_instance_id = str(value("role_instance_id")).strip()
        state = str(value("state")).strip()
        direct_teach_active = bool(value("direct_teach_active", False))
        if arm_id not in VALID_ARM_IDS or arm_id in arm_ids:
            raise ValueError("invalid or duplicate bed robot arm_id")
        if role != "retraction":
            raise ValueError("bed robot role must be retraction")
        if role_instance_id not in expected_roles or role_instance_id in roles:
            raise ValueError("invalid or duplicate retraction role_instance_id")
        if state not in VALID_BED_ROBOT_ARM_STATES:
            raise ValueError("unsupported bed robot arm state")
        if direct_teach_active != (state == "direct_teach"):
            raise ValueError("direct_teach_active is inconsistent with state")
        arm_ids.add(arm_id)
        roles.add(role_instance_id)
        normalized.append(
            {
                "arm_id": arm_id,
                "role": role,
                "role_instance_id": role_instance_id,
                "state": state,
                "direct_teach_active": direct_teach_active,
                "reason_code": str(value("reason_code")).strip(),
            }
        )

    if roles != expected_roles or len(normalized) != len(expected_roles):
        raise ValueError("bed robot arm layout does not match procedure_type")
    return normalized


def measured_rate(samples: Iterable[float], now: float, window_sec: float) -> tuple[float, int]:
    recent = [value for value in samples if now - value <= window_sec]
    if len(recent) < 2:
        return 0.0, len(recent)
    span = recent[-1] - recent[0]
    return ((len(recent) - 1) / span if span > 0.0 else 0.0), len(recent)


def _normalized(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣_.]+", " ", text.lower()).strip()


def parse_voice_command(text: str, voice_config: dict[str, Any]) -> VoiceParse:
    normalized = _normalized(text)
    if not normalized:
        return VoiceParse(False, False, reason="empty_sentence")

    axis_aliases = {
        "왼쪽 오른쪽": "left_right",
        "좌우": "left_right",
        "위 아래": "up_down",
        "위아래": "up_down",
        "상하": "up_down",
    }
    axes = {value for alias, value in axis_aliases.items() if alias in normalized}
    target_aliases = {
        "왼쪽 말레어블": "left_malleable",
        "left malleable": "left_malleable",
        "왼쪽 견인기": "left_malleable",
        "left retractor": "left_malleable",
        "오른쪽 말레어블": "right_malleable",
        "right malleable": "right_malleable",
        "오른쪽 견인기": "right_malleable",
        "right retractor": "right_malleable",
    }
    target_matches = {
        value for alias, value in target_aliases.items() if alias in normalized
    }
    is_retractor = bool(
        "리트랙터" in normalized
        or "견인기" in normalized
        or "retractor" in normalized
        or "말레어블" in normalized
        or "malleable" in normalized
        or (axes and any(token in normalized for token in ("당겨", "pull")))
    )
    if is_retractor:
        direction_aliases = {
            "오른쪽": "right", "right": "right", "왼쪽": "left", "left": "left",
            "아래": "down", "down": "down", "위": "up", "up": "up",
        }
        direction_text = normalized
        for alias in target_aliases:
            direction_text = direction_text.replace(alias, " ")
        directions = (
            set()
            if axes
            else {
                value
                for alias, value in direction_aliases.items()
                if alias in direction_text
            }
        )
        distance_match = re.search(
            r"(\d+(?:\.\d+)?)\s*"
            r"(mm|밀리(?:미터)?|cm|센티(?:미터)?|센치(?:미터)?)",
            normalized,
        )
        distance_mm = 0.0
        if distance_match:
            distance_mm = float(distance_match.group(1))
            if distance_match.group(2) in {"cm", "센티", "센티미터", "센치", "센치미터"}:
                distance_mm *= 10.0
        target = (
            "both_malleable"
            if axes
            else next(iter(target_matches))
            if len(target_matches) == 1
            else ""
        )
        if len(directions) == 1 and not axes and distance_match and target:
            payload = {
                "adjustment_mode": "single",
                "target_retractor_id": target,
                "direction_frame": "surgeon_view",
                "direction": directions.pop(),
                "axis": "none",
                "distance_mm": distance_mm,
            }
            try:
                return VoiceParse(
                    True,
                    False,
                    operation="retraction_adjustment",
                    payload=validate_retraction_adjustment(payload),
                )
            except ValueError as exc:
                return VoiceParse(False, False, reason=str(exc))
        if len(axes) == 1 and not directions and distance_match:
            payload = {
                "adjustment_mode": "multi",
                "target_retractor_id": "both_malleable",
                "direction_frame": "surgeon_view",
                "direction": "none",
                "axis": axes.pop(),
                "distance_mm": distance_mm,
            }
            try:
                return VoiceParse(True, False, operation="retraction_adjustment", payload=validate_retraction_adjustment(payload))
            except ValueError as exc:
                return VoiceParse(False, False, reason=str(exc))
        if len(directions) > 1 or len(axes) > 1 or (directions and axes):
            return VoiceParse(False, True, reason="ambiguous_retraction_direction")
        return VoiceParse(False, False, reason="incomplete_retraction_command")

    request_words = ("줘", "주세요", "전달", "건네", "please", "handover", "give")
    if not any(word in normalized for word in request_words):
        return VoiceParse(False, False, reason="no_supported_command")
    aliases = voice_config.get("aliases", {})
    matches = {
        str(public_name).strip()
        for alias, public_name in aliases.items()
        if _normalized(str(alias)) and _normalized(str(alias)) in normalized
    }
    if len(matches) > 1:
        return VoiceParse(False, True, reason="ambiguous_instrument")
    if not matches:
        return VoiceParse(False, False, reason="unknown_instrument")
    instrument_id = matches.pop()
    return VoiceParse(
        True,
        False,
        operation="tool_handover",
        payload={
            "instrument_id": instrument_id,
            "instrument_instance_id": f"{instrument_id}#1",
            "source_location": str(
                voice_config.get("default_source_location", "tray")
            ),
            "target_location": str(
                voice_config.get("default_target_location", "surgeon")
            ),
        },
    )
