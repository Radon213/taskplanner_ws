"""Configuration, validation, and deterministic voice parsing for Debug Mode."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
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
RETRACTION_COMMANDS = {
    "start_direct_teach",
    "finish_direct_teach",
    "start_retraction",
    "adjust_retraction",
    "change_tool",
    "stop_retraction",
}
RETRACTION_TARGET_SIDES = {"none", "left", "right"}
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

    service_route = route == "retraction_service"
    if terminal or recovery_required:
        return ""
    if (
        not server_ready
        and server_unavailable_age_sec >= policy["server_loss_grace_sec"]
    ):
        return (
            "service_server_unavailable"
            if service_route
            else "action_server_unavailable"
        )
    if elapsed_sec >= policy["max_duration_sec"]:
        return "action_duration_timeout"
    if state == "submitting" and elapsed_sec >= policy["goal_response_timeout_sec"]:
        return (
            "service_response_timeout"
            if service_route
            else "goal_response_timeout"
        )
    if (
        not service_route
        and state != "submitting"
        and last_update_age_sec >= policy["feedback_timeout_sec"]
    ):
        return "action_update_timeout"
    return ""


def validate_action_recovery_acknowledgement(
    payload: dict[str, Any], active_command_id: str
) -> str:
    """Require exact command identity and an explicit remote-stop confirmation."""

    command_id = str(payload.get("expected_command_id", "")).strip()
    if not active_command_id:
        raise ValueError("there is no active command client state to recover")
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


def validate_retraction_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact Debug Mode payload for the single retractor Service.

    The external service deliberately has no direction/axis/multi-arm or tool-id
    fields.  Reject those legacy fields instead of silently dropping a clinical
    instruction while changing interfaces.
    """

    allowed_fields = {"command", "target_side", "distance_m"}
    unexpected_fields = sorted(set(payload).difference(allowed_fields))
    if unexpected_fields:
        raise ValueError(
            "unsupported retraction command fields: "
            + ", ".join(unexpected_fields)
        )

    command = str(payload.get("command", "")).strip().lower()
    target_side = str(payload.get("target_side", "none")).strip().lower()
    if command not in RETRACTION_COMMANDS:
        raise ValueError("unsupported retraction command")
    if target_side not in RETRACTION_TARGET_SIDES:
        raise ValueError("target_side must be none, left, or right")
    try:
        distance_m = float(payload.get("distance_m", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("distance_m must be numeric") from exc
    if not math.isfinite(distance_m):
        raise ValueError("distance_m must be finite")

    if command == "adjust_retraction":
        if target_side == "none":
            raise ValueError("adjust_retraction requires target_side left or right")
        if distance_m <= 0.0:
            raise ValueError("adjust_retraction requires distance_m greater than 0")
    elif target_side != "none" or distance_m != 0.0:
        raise ValueError(
            f"{command} requires target_side none and distance_m 0"
        )

    return {
        "command": command,
        "target_side": target_side,
        "distance_m": distance_m,
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

    direct_teach = "직접 교시" in normalized or "direct teach" in normalized
    if direct_teach:
        starts = any(token in normalized for token in ("시작", "start"))
        finishes = any(token in normalized for token in ("종료", "끝", "finish", "stop"))
        if starts and finishes:
            return VoiceParse(False, True, reason="ambiguous_direct_teach_command")
        if starts or finishes:
            payload = validate_retraction_command(
                {
                    "command": (
                        "start_direct_teach" if starts else "finish_direct_teach"
                    ),
                    "target_side": "none",
                    "distance_m": 0.0,
                }
            )
            return VoiceParse(
                True,
                False,
                operation="retraction_command",
                payload=payload,
            )
        return VoiceParse(False, False, reason="incomplete_direct_teach_command")

    if any(
        phrase in normalized
        for phrase in ("tool change", "toolchange", "도구 교환", "도구 변경")
    ):
        return VoiceParse(
            True,
            False,
            operation="retraction_command",
            payload=validate_retraction_command(
                {
                    "command": "change_tool",
                    "target_side": "none",
                    "distance_m": 0.0,
                }
            ),
        )

    retraction_terms = (
        "리트랙션",
        "retraction",
        "리트랙터",
        "retractor",
        "견인기",
        "말레어블",
        "malleable",
    )
    is_retractor = any(term in normalized for term in retraction_terms)
    if is_retractor:
        starts = any(token in normalized for token in ("시작", "start"))
        stops = any(token in normalized for token in ("종료", "끝", "stop", "finish"))
        if starts and stops:
            return VoiceParse(False, True, reason="ambiguous_retraction_command")
        if starts or stops:
            return VoiceParse(
                True,
                False,
                operation="retraction_command",
                payload=validate_retraction_command(
                    {
                        "command": "start_retraction" if starts else "stop_retraction",
                        "target_side": "none",
                        "distance_m": 0.0,
                    }
                ),
            )

        target_aliases = {
            "왼쪽": "left",
            "left": "left",
            "오른쪽": "right",
            "right": "right",
        }
        target_sides = {
            side for alias, side in target_aliases.items() if alias in normalized
        }
        distance_match = re.search(
            r"(\d+(?:\.\d+)?)\s*"
            r"(mm|밀리(?:미터)?|cm|센티(?:미터)?|센치(?:미터)?)",
            normalized,
        )
        if len(target_sides) > 1:
            return VoiceParse(False, True, reason="ambiguous_retraction_target_side")
        if len(target_sides) == 1 and distance_match and "더" in normalized:
            distance_m = float(distance_match.group(1))
            if distance_match.group(2) in {"mm", "밀리", "밀리미터"}:
                distance_m /= 1000.0
            else:
                distance_m /= 100.0
            try:
                return VoiceParse(
                    True,
                    False,
                    operation="retraction_command",
                    payload=validate_retraction_command(
                        {
                            "command": "adjust_retraction",
                            "target_side": target_sides.pop(),
                            "distance_m": distance_m,
                        }
                    ),
                )
            except ValueError as exc:
                return VoiceParse(False, False, reason=str(exc))
        return VoiceParse(
            False,
            False,
            reason="unsupported_retraction_command",
        )

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
