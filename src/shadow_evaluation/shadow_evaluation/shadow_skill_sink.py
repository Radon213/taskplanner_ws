"""Observe BT skill commands without executing or mutating the digital twin."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupCommand,
    BedRobotArmGroupStatus,
    ShadowReplayState,
    SkillCommand,
    SkillStatus,
    TwinEvent,
    WorldState,
)

from .message_conversion import message_payload


HANDOVER_ACTIONS = {
    "handover",
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
}
PREPARE_ACTIONS = {"predict_tool", "prepare_tool"}
RECOVERY_ACTIONS = {"retrieve_from_mayo", "recover", "recovery"}
FEEDBACK_ELIGIBLE_STATUSES = {
    "admissible",
}
MAYO_STAGES = {"mayo_reuse", "mayo_recovery"}
HANDOVER_STAGES = {
    "home_rack",
    "returned_home",
    "prepositioned_right",
    "mayo_reuse",
    "mayo_recovery",
}

ACTION_STAGE_FRACTIONS = {
    "predict_tool": (1.0,),
    "prepare_tool": (1.0,),
    "direct_handover": (1.0,),
    "handover": (0.45, 1.0),
    "pick_up_and_handover": (0.45, 1.0),
    "pick_up_from_mayo_and_handover": (0.45, 1.0),
    "put_down_and_handover": (0.25, 0.6, 1.0),
    "retrieve_from_mayo": (0.22, 0.42, 0.78, 1.0),
    "recover": (0.22, 0.42, 0.78, 1.0),
    "recovery": (0.22, 0.42, 0.78, 1.0),
    "return_unused_preposition": (1.0,),
}


@dataclass(slots=True)
class PendingShadowAction:
    payload: dict[str, Any]
    admission_status: str
    events: list[dict[str, Any]]
    started_at: float
    duration_sec: float
    next_event_index: int = 0
    awaiting_event: dict[str, Any] | None = None
    awaiting_fingerprint: str = ""
    awaiting_since: float = 0.0

    @property
    def action(self) -> str:
        return str(self.payload.get("action", "") or "").strip().lower()

    @property
    def command_id(self) -> str:
        return str(self.payload.get("command_id", "") or "").strip()

    def progress(self, now: float) -> float:
        if self.duration_sec <= 0.0:
            return 1.0
        return min(1.0, max(0.0, (now - self.started_at) / self.duration_sec))

    def due_events(self, now: float) -> list[dict[str, Any]]:
        progress = self.progress(now)
        fractions = ACTION_STAGE_FRACTIONS.get(
            self.action,
            tuple(
                (index + 1) / max(1, len(self.events))
                for index in range(len(self.events))
            ),
        )
        due: list[dict[str, Any]] = []
        while self.next_event_index < len(self.events):
            fraction = (
                fractions[self.next_event_index]
                if self.next_event_index < len(fractions)
                else 1.0
            )
            if progress + 1e-6 < fraction:
                break
            due.append(self.events[self.next_event_index])
            self.next_event_index += 1
        return due

    def next_due_event(self, now: float) -> dict[str, Any] | None:
        if self.awaiting_event is not None or self.next_event_index >= len(self.events):
            return None
        progress = self.progress(now)
        fractions = ACTION_STAGE_FRACTIONS.get(
            self.action,
            tuple(
                (index + 1) / max(1, len(self.events))
                for index in range(len(self.events))
            ),
        )
        fraction = (
            fractions[self.next_event_index]
            if self.next_event_index < len(fractions)
            else 1.0
        )
        if progress + 1e-6 < fraction:
            return None
        event = self.events[self.next_event_index]
        self.next_event_index += 1
        return event

    def complete(self, now: float) -> bool:
        return (
            self.progress(now) >= 1.0
            and self.next_event_index >= len(self.events)
            and self.awaiting_event is None
        )

    def shift_clock(self, paused_duration_sec: float) -> None:
        delta = max(0.0, float(paused_duration_sec))
        self.started_at += delta
        if self.awaiting_since > 0.0:
            self.awaiting_since += delta


class SemanticCommandLedger:
    """Bound repeated semantic commands without hiding the first deadlock."""

    def __init__(self) -> None:
        self._admissions: dict[str, tuple[str, float]] = {}
        self._reported_deadlocks: dict[str, float] = {}

    def prune(self, cutoff: float) -> None:
        self._admissions = {
            key: value
            for key, value in self._admissions.items()
            if value[1] >= cutoff
        }
        self._reported_deadlocks = {
            key: observed_at
            for key, observed_at in self._reported_deadlocks.items()
            if observed_at >= cutoff
        }

    def reset(self) -> None:
        self._admissions.clear()
        self._reported_deadlocks.clear()

    def forget_preparations(
        self,
        instrument_id: str,
        instance_id: str = "",
    ) -> None:
        """Start a new episode after a preparation is consumed or released."""
        tool_id = str(instrument_id or "").strip()
        tool_instance = str(instance_id or "").strip()
        if not tool_id:
            return

        forgotten_keys = {
            key
            for key in self._admissions
            if _semantic_key_matches_preparation(
                key,
                tool_id,
                tool_instance,
            )
        }
        if not forgotten_keys:
            return
        for key in forgotten_keys:
            self._admissions.pop(key, None)
        self._reported_deadlocks = {
            key: observed_at
            for key, observed_at in self._reported_deadlocks.items()
            if not any(
                key.startswith(f"{semantic_key}\n")
                for semantic_key in forgotten_keys
            )
        }

    def forget_returns(
        self,
        instrument_id: str,
        instance_id: str = "",
    ) -> None:
        """Allow one return in a newly observed preparation episode."""
        tool_id = str(instrument_id or "").strip()
        tool_instance = str(instance_id or "").strip()
        if not tool_id:
            return

        forgotten_keys = {
            key
            for key in self._admissions
            if _semantic_key_matches_return(
                key,
                tool_id,
                tool_instance,
            )
        }
        if not forgotten_keys:
            return
        for key in forgotten_keys:
            self._admissions.pop(key, None)
        self._reported_deadlocks = {
            key: observed_at
            for key, observed_at in self._reported_deadlocks.items()
            if not any(
                key.startswith(f"{semantic_key}\n")
                for semantic_key in forgotten_keys
            )
        }

    def begin_preparation_episode(
        self,
        instrument_id: str,
        instance_id: str = "",
    ) -> None:
        """Allow one return command for a newly admitted preparation."""

        tool_id = str(instrument_id or "").strip()
        tool_instance = str(instance_id or "").strip()
        if not tool_id:
            return
        forgotten_keys = {
            key
            for key in self._admissions
            if _semantic_key_matches_unused_return(
                key,
                tool_id,
                tool_instance,
            )
        }
        for key in forgotten_keys:
            self._admissions.pop(key, None)
        self._reported_deadlocks = {
            key: observed_at
            for key, observed_at in self._reported_deadlocks.items()
            if not any(
                key.startswith(f"{semantic_key}\n")
                for semantic_key in forgotten_keys
            )
        }

    def previous_fingerprint(self, semantic_key: str) -> str:
        previous = self._admissions.get(semantic_key)
        return previous[0] if previous else ""

    def record_admission(
        self,
        semantic_key: str,
        fingerprint: str,
        observed_at: float,
    ) -> None:
        self._admissions[semantic_key] = (fingerprint, observed_at)

    def should_report_deadlock(
        self,
        semantic_key: str,
        fingerprint: str,
        observed_at: float,
    ) -> bool:
        key = f"{semantic_key}\n{fingerprint}"
        if key in self._reported_deadlocks:
            return False
        self._reported_deadlocks[key] = observed_at
        return True


def shadow_group_terminal_state(operation: str) -> str:
    return {
        "suction_start": "suctioning",
        "suction_stop": "standby",
        "retraction": "holding",
        "release_retraction": "standby",
        "change_end_effector": "standby",
    }.get(str(operation or "").strip(), "standby")


def _instance_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("instrument_instance_id")
        or payload.get("instance_id")
        or ""
    ).strip()


def _semantic_key_matches_preparation(
    semantic_key: str,
    instrument_id: str,
    instance_id: str,
) -> bool:
    fields = str(semantic_key or "").split("|")
    if len(fields) < 3 or fields[0] not in PREPARE_ACTIONS:
        return False
    if fields[1] != instrument_id:
        return False
    return not instance_id or fields[2] in {"", instance_id}


def _semantic_key_matches_return(
    semantic_key: str,
    instrument_id: str,
    instance_id: str,
) -> bool:
    fields = str(semantic_key or "").split("|")
    if (
        len(fields) < 3
        or fields[0] != "return_unused_preposition"
    ):
        return False
    if fields[1] != instrument_id:
        return False
    return not instance_id or fields[2] in {"", instance_id}


def _semantic_key_matches_unused_return(
    semantic_key: str,
    instrument_id: str,
    instance_id: str,
) -> bool:
    fields = str(semantic_key or "").split("|")
    if len(fields) < 3 or fields[0] != "return_unused_preposition":
        return False
    if fields[1] != instrument_id:
        return False
    return not instance_id or fields[2] in {"", instance_id}


def _instrument_lifecycle_index(
    world: dict[str, Any] | None,
) -> dict[tuple[str, str], str]:
    if world is None:
        return {}
    result: dict[tuple[str, str], str] = {}
    for state in world.get("instrument_states", []):
        if not isinstance(state, dict):
            continue
        tool_id = str(state.get("instrument_id", "") or "").strip()
        instance_id = str(state.get("instance_id", "") or "").strip()
        if not tool_id:
            continue
        result[(tool_id, instance_id)] = str(
            state.get("lifecycle_stage", "") or ""
        ).strip()
    return result


def returned_home_instrument_instances(
    previous_world: dict[str, Any] | None,
    current_world: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    previous = _instrument_lifecycle_index(previous_world)
    current = _instrument_lifecycle_index(current_world)
    home_stages = {"home_rack", "returned_home"}
    return {
        identity
        for identity, lifecycle in current.items()
        if lifecycle in home_stages
        and identity in previous
        and previous[identity] not in home_stages
    }


def completed_preposition_instrument_instances(
    previous_world: dict[str, Any] | None,
    current_world: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    """Find preparations consumed by handover or released to a source."""

    previous = _instrument_lifecycle_index(previous_world)
    current = _instrument_lifecycle_index(current_world)
    return {
        identity
        for identity, lifecycle in current.items()
        if identity in previous
        and previous[identity] == "prepositioned_right"
        and lifecycle != "prepositioned_right"
    }


def newly_prepositioned_instrument_instances(
    previous_world: dict[str, Any] | None,
    current_world: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    previous = _instrument_lifecycle_index(previous_world)
    current = _instrument_lifecycle_index(current_world)
    return {
        identity
        for identity, lifecycle in current.items()
        if lifecycle == "prepositioned_right"
        and identity in previous
        and previous[identity] != "prepositioned_right"
    }


def departed_prepositioned_instrument_instances(
    previous_world: dict[str, Any] | None,
    current_world: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    previous = _instrument_lifecycle_index(previous_world)
    current = _instrument_lifecycle_index(current_world)
    return {
        identity
        for identity, lifecycle in current.items()
        if identity in previous
        and previous[identity] == "prepositioned_right"
        and lifecycle != "prepositioned_right"
    }


def _world_instrument_state(
    world: dict[str, Any] | None,
    instrument_id: str,
    instance_id: str = "",
) -> dict[str, Any]:
    if world is None:
        return {}
    states = [
        item
        for item in world.get("instrument_states", [])
        if isinstance(item, dict)
        and str(item.get("instrument_id", "") or "").strip() == instrument_id
    ]
    if instance_id:
        exact = next(
            (
                item
                for item in states
                if str(item.get("instance_id", "") or "").strip() == instance_id
            ),
            None,
        )
        if exact is not None:
            return exact
        return {}
    return states[0] if states else {}


def _right_hand_state(
    world: dict[str, Any] | None,
) -> dict[str, Any]:
    if world is None:
        return {}
    right_hand_tool = str(world.get("right_hand_tool", "") or "").strip()
    right_hand_instance = str(
        world.get("right_hand_tool_instance_id")
        or world.get("right_hand_instance_id")
        or ""
    ).strip()
    if right_hand_tool:
        return _world_instrument_state(
            world,
            right_hand_tool,
            right_hand_instance,
        )
    return next(
        (
            item
            for item in world.get("instrument_states", [])
            if isinstance(item, dict)
            and (
                str(item.get("lifecycle_stage", "") or "").strip()
                == "prepositioned_right"
                or str(item.get("owner", "") or "").strip()
                == "robot_right_hand"
            )
        ),
        {},
    )


def classify_shadow_command(
    command: dict[str, Any],
    world: dict[str, Any] | None,
    *,
    allow_type_instance_assumption: bool = False,
) -> tuple[str, str]:
    if world is None:
        return ("blocked", "no_world_state")
    action = str(command.get("action", "") or "").strip().lower()
    if not bool(world.get("running")):
        return ("blocked", "runtime_not_running")
    execution_state = str(world.get("execution_state", ""))
    if execution_state not in {"running", "finishing"}:
        return ("blocked", "runtime_not_in_running_state")
    if (
        execution_state == "finishing"
        and action not in RECOVERY_ACTIONS
        and action != "return_unused_preposition"
    ):
        return ("blocked", "runtime_finishing_cleanup_only")
    if str(world.get("active_robot_task_id", "")):
        return ("blocked", "another_robot_task_is_active")

    tool_id = str(command.get("instrument_id", "") or "").strip()
    supported_actions = (
        HANDOVER_ACTIONS
        | PREPARE_ACTIONS
        | RECOVERY_ACTIONS
        | {"return_unused_preposition"}
    )
    if action not in supported_actions:
        return ("blocked", f"unsupported_shadow_action:{action or 'empty'}")
    state = _world_instrument_state(world, tool_id, _instance_id(command))
    if not tool_id or state is None:
        return ("physically_impossible", "instrument_not_in_active_bundle")
    if not state:
        return ("physically_impossible", "instrument_not_in_active_bundle")

    lifecycle = str(state.get("lifecycle_stage", ""))
    owner = str(state.get("owner", ""))
    additional_instance_assumed = bool(
        action in HANDOVER_ACTIONS
        and allow_type_instance_assumption
        and world.get("surgeon_request_additional_instance_assumed")
        and (lifecycle == "surgeon_owned" or owner == "surgeon")
    )
    if action == "direct_handover" and lifecycle != "prepositioned_right":
        return (
            "physically_impossible",
            f"direct_handover_requires_prepositioned_tool:{lifecycle or 'unknown'}",
        )
    right_hand = _right_hand_state(world)
    right_hand_tool = str(
        right_hand.get("instrument_id", "") or world.get("right_hand_tool", "")
    ).strip()
    if action == "put_down_and_handover":
        if not right_hand_tool or right_hand_tool == tool_id:
            return (
                "blocked",
                "put_down_and_handover_requires_different_right_hand_tool",
            )
        right_lifecycle = str(
            right_hand.get("lifecycle_stage", "") or ""
        ).strip()
        right_owner = str(right_hand.get("owner", "") or "").strip()
        if (
            right_lifecycle != "prepositioned_right"
            and right_owner != "robot_right_hand"
        ):
            return (
                "blocked",
                f"right_hand_tool_not_prepositioned:{right_lifecycle or 'unknown'}",
            )
        if not (
            str(
                right_hand.get("preposition_origin_location_id", "")
                or right_hand.get("home_location_id", "")
                or ""
            ).strip()
        ):
            return ("blocked", "right_hand_tool_return_location_unknown")
    elif (
        action in HANDOVER_ACTIONS
        and right_hand_tool
        and right_hand_tool != tool_id
    ):
        return ("blocked", f"robot_right_hand_busy:{right_hand_tool}")
    if action in HANDOVER_ACTIONS:
        if lifecycle == "surgeon_owned" or owner == "surgeon":
            if additional_instance_assumed:
                return (
                    "instance_resolution_assumed",
                    "tool_type_already_owned_instance_inventory_unmodeled",
                )
            return ("physically_impossible", "instrument_already_owned_by_surgeon")
        if lifecycle not in HANDOVER_STAGES:
            return (
                "physically_impossible",
                f"instrument_not_handoverable_from_{lifecycle or 'unknown'}",
            )
        if bool(state.get("contaminated")) and lifecycle not in MAYO_STAGES:
            return ("unsafe", "instrument_contaminated")
    elif action in RECOVERY_ACTIONS:
        if lifecycle not in MAYO_STAGES:
            return (
                "physically_impossible",
                f"instrument_not_on_mayo:{lifecycle or 'unknown'}",
            )
        left_hand_tool = str(world.get("left_hand_tool", "") or "").strip()
        if left_hand_tool and left_hand_tool != tool_id:
            return ("blocked", f"robot_left_hand_busy:{left_hand_tool}")
        if bool(world.get("cleaner_busy")):
            return ("blocked", "cleaner_busy")
    elif action == "return_unused_preposition":
        if lifecycle != "prepositioned_right":
            return ("physically_impossible", "instrument_not_prepositioned")
        return_location_type = str(
            command.get("target_location_type", "") or ""
        ).strip()
        if (
            bool(state.get("contaminated"))
            and return_location_type
            not in {"mayo_stand", "mayo_reuse_zone"}
        ):
            return ("unsafe", "instrument_contaminated")
    elif action in PREPARE_ACTIONS:
        if (
            lifecycle not in {"home_rack", "returned_home", "mayo_reuse"}
        ):
            return (
                "physically_impossible",
                f"instrument_not_preparable_from_{lifecycle or 'unknown'}",
            )
        if bool(state.get("contaminated")) and lifecycle != "mayo_reuse":
            return ("unsafe", "instrument_contaminated")
    return ("admissible", "shadow_only_no_execution")


def _instrument_state(
    command: dict[str, Any],
    world: dict[str, Any] | None,
) -> dict[str, Any]:
    tool_id = str(command.get("instrument_id", "") or "").strip()
    return _world_instrument_state(world, tool_id, _instance_id(command))


def semantic_command_key(command: dict[str, Any]) -> str:
    return "|".join(
        (
            str(command.get("action", "") or "").strip().lower(),
            str(command.get("instrument_id", "") or "").strip(),
            _instance_id(command),
            str(int(command.get("request_generation", 0) or 0)),
            str(command.get("arm", "") or "").strip(),
            str(command.get("source_location_id", "") or "").strip(),
            str(command.get("target_location_id", "") or "").strip(),
        )
    )


def command_world_fingerprint(
    command: dict[str, Any],
    world: dict[str, Any] | None,
) -> str:
    if world is None:
        return "no_world_state"
    instrument_state = _instrument_state(command, world)
    right_hand_state = _right_hand_state(world)
    relevant = {
        "procedure_id": str(world.get("procedure_id", "")),
        "running": bool(world.get("running")),
        "execution_state": str(world.get("execution_state", "")),
        "filtered_phase": str(world.get("filtered_phase", "")),
        "explicit_request_tool": str(world.get("explicit_request_tool", "")),
        "surgeon_request_tool": str(world.get("surgeon_request_tool", "")),
        "surgeon_request_generation": int(
            world.get("surgeon_request_generation", 0) or 0
        ),
        "surgeon_request_additional_instance_assumed": bool(
            world.get("surgeon_request_additional_instance_assumed")
        ),
        "surgeon_intent": str(world.get("surgeon_intent", "")),
        "active_robot_task_id": str(world.get("active_robot_task_id", "")),
        "active_robot_task_type": str(world.get("active_robot_task_type", "")),
        "active_robot_task_tool_id": str(
            world.get("active_robot_task_tool_id", "")
        ),
        "right_hand_tool": str(world.get("right_hand_tool", "")),
        "right_hand_instance_id": str(
            world.get("right_hand_tool_instance_id")
            or world.get("right_hand_instance_id")
            or ""
        ),
        "instrument": {
            key: instrument_state.get(key)
            for key in (
                "active",
                "contaminated",
                "instance_id",
                "lifecycle_stage",
                "location_id",
                "location_type",
                "owner",
                "status",
            )
        },
        "right_hand_instrument": {
            key: right_hand_state.get(key)
            for key in (
                "active",
                "contaminated",
                "home_location_id",
                "home_location_type",
                "instance_id",
                "instrument_id",
                "lifecycle_stage",
                "location_id",
                "location_type",
                "owner",
                "status",
            )
        },
    }
    return json.dumps(relevant, separators=(",", ":"), sort_keys=True)


def classify_shadow_command_attempt(
    command: dict[str, Any],
    world: dict[str, Any] | None,
    *,
    previous_admissible_fingerprint: str = "",
    allow_type_instance_assumption: bool = False,
) -> tuple[str, str, str]:
    status, reason = classify_shadow_command(
        command,
        world,
        allow_type_instance_assumption=allow_type_instance_assumption,
    )
    fingerprint = command_world_fingerprint(command, world)
    if (
        status in FEEDBACK_ELIGIBLE_STATUSES
        and previous_admissible_fingerprint
        and previous_admissible_fingerprint == fingerprint
    ):
        return (
            "deadlock_rejected",
            "same_semantic_command_and_generation_without_world_state_change",
            fingerprint,
        )
    return (status, reason, fingerprint)


def counterfactual_event_payloads(
    command: dict[str, Any],
    world: dict[str, Any] | None,
    *,
    instance_resolution_assumed: bool = False,
) -> list[dict[str, Any]]:
    action = str(command.get("action", "") or "").strip().lower()
    tool_id = str(command.get("instrument_id", "") or "").strip()
    arm = str(command.get("arm", "") or "").strip()
    state = _instrument_state(command, world)
    lifecycle = str(state.get("lifecycle_stage", "") or "").strip()
    source_location_id = str(
        command.get("source_location_id")
        or state.get("location_id")
        or ""
    )
    source_location_type = str(
        command.get("source_location_type")
        or state.get("location_type")
        or ""
    )
    home_location_id = str(
        state.get("home_location_id")
        or command.get("target_location_id")
        or ""
    )
    home_location_type = str(
        state.get("home_location_type")
        or command.get("target_location_type")
        or "tray_slot"
    )
    common = {
        "instrument_id": tool_id,
        "instance_id": _instance_id(command),
        "confidence": 1.0,
        "mode": "shadow_counterfactual",
        "detail": {
            "command_id": str(command.get("command_id", "") or ""),
            "instance_id": _instance_id(command),
            "request_generation": int(
                command.get("request_generation", 0) or 0
            ),
            "ground_truth_used": False,
            "physical_execution_attempted": False,
            "transport": "shadow_counterfactual_feedback",
        },
    }

    if action in PREPARE_ACTIONS:
        return [
            {
                **common,
                "event_type": "ToolPrepared",
                "location_id": "robot_right_hand",
                "location_type": "robot_right_hand",
                "owner": "robot_right_hand",
                "status": "prepared",
                "arm": arm or "right",
                "source_location_id": source_location_id,
                "source_location_type": source_location_type,
                "target_location_id": "robot_right_hand",
                "target_location_type": "robot_right_hand",
                "target_owner": "robot_right_hand",
            }
        ]

    if action in HANDOVER_ACTIONS:
        if instance_resolution_assumed:
            return [
                {
                    **common,
                    "event_type": "ShadowAdditionalToolHandoverCompleted",
                    "location_id": "surgeon_hand",
                    "location_type": "surgeon_hand",
                    "owner": "surgeon",
                    "status": "handed_over",
                    "arm": arm or "right",
                    "source_location_id": source_location_id,
                    "source_location_type": source_location_type,
                    "target_location_id": (
                        str(command.get("target_location_id", "") or "")
                        or "surgeon_receive_zone"
                    ),
                    "target_location_type": (
                        str(command.get("target_location_type", "") or "")
                        or "handover_zone"
                    ),
                    "target_owner": "surgeon",
                    "detail": {
                        **common["detail"],
                        "shadow_assumption": "additional_tool_instance",
                    },
                }
            ]
        events: list[dict[str, Any]] = []
        if action == "put_down_and_handover":
            displaced_state = _right_hand_state(world)
            displaced_tool_id = str(
                displaced_state.get("instrument_id", "") or ""
            ).strip()
            displaced_instance_id = str(
                displaced_state.get("instance_id", "") or ""
            ).strip()
            displaced_return_id = str(
                displaced_state.get("preposition_origin_location_id", "")
                or displaced_state.get("home_location_id", "")
                or ""
            ).strip()
            displaced_return_type = str(
                displaced_state.get("preposition_origin_location_type", "")
                or displaced_state.get("home_location_type", "")
                or "tray_slot"
            ).strip()
            displaced_return_lifecycle = str(
                displaced_state.get(
                    "preposition_origin_lifecycle_stage", ""
                )
                or "returned_home"
            ).strip()
            if not displaced_tool_id or not displaced_return_id:
                return []
            events.append(
                {
                    "event_type": "UnusedPrepositionReturned",
                    "instrument_id": displaced_tool_id,
                    "instance_id": displaced_instance_id,
                    "confidence": 1.0,
                    "mode": "shadow_counterfactual",
                    "location_id": displaced_return_id,
                    "location_type": displaced_return_type,
                    "owner": "none",
                    "status": "available",
                    "arm": arm or "right",
                    "source_location_id": "robot_right_hand",
                    "source_location_type": "robot_right_hand",
                    "target_location_id": displaced_return_id,
                    "target_location_type": displaced_return_type,
                    "target_owner": "none",
                    "detail": {
                        **common["detail"],
                        "instance_id": displaced_instance_id,
                        "displaced_instrument_id": displaced_tool_id,
                        "displaced_instance_id": displaced_instance_id,
                        "incoming_instrument_id": tool_id,
                        "incoming_instance_id": _instance_id(command),
                        "semantic_step": "put_down_existing_right_hand_tool",
                        "target_lifecycle_stage": (
                            displaced_return_lifecycle
                        ),
                    },
                }
            )
        if lifecycle != "prepositioned_right":
            events.append(
                {
                    **common,
                    "event_type": "RobotGraspedTool",
                    "location_id": "robot_right_hand",
                    "location_type": "robot_right_hand",
                    "owner": "robot_right_hand",
                    "status": "held",
                    "arm": arm or "right",
                    "source_location_id": source_location_id,
                    "source_location_type": source_location_type,
                    "target_location_id": "robot_right_hand",
                    "target_location_type": "robot_right_hand",
                    "target_owner": "robot_right_hand",
                }
            )
        events.append(
            {
                **common,
                "event_type": "ToolHandoverCompleted",
                "location_id": "surgeon_hand",
                "location_type": "surgeon_hand",
                "owner": "surgeon",
                "status": "handed_over",
                "arm": arm or "right",
                "source_location_id": "robot_right_hand",
                "source_location_type": "robot_right_hand",
                "target_location_id": (
                    str(command.get("target_location_id", "") or "")
                    or "surgeon_receive_zone"
                ),
                "target_location_type": (
                    str(command.get("target_location_type", "") or "")
                    or "handover_zone"
                ),
                "target_owner": (
                    str(command.get("target_owner", "") or "")
                    or "surgeon"
                ),
            }
        )
        return events

    if action in RECOVERY_ACTIONS:
        return [
            {
                **common,
                "event_type": "ToolReceivedFromSurgeon",
                "location_id": "robot_left_hand",
                "location_type": "robot_left_hand",
                "owner": "robot_left_hand",
                "status": "received_return",
                "arm": arm or "left",
                "source_location_id": source_location_id or "mayo_recovery_zone",
                "source_location_type": (
                    source_location_type or "mayo_recovery_zone"
                ),
                "target_location_id": "robot_left_hand",
                "target_location_type": "robot_left_hand",
                "target_owner": "robot_left_hand",
                "cleaning_required": True,
            },
            {
                **common,
                "event_type": "ToolSentToCleaner",
                "location_id": "cleaner_slot",
                "location_type": "cleaner_slot",
                "owner": "none",
                "status": "cleaning",
                "arm": arm or "left",
                "source_location_id": "robot_left_hand",
                "source_location_type": "robot_left_hand",
                "target_location_id": "cleaner_slot",
                "target_location_type": "cleaner_slot",
                "target_owner": "none",
                "cleaning_required": True,
            },
            {
                **common,
                "event_type": "ToolCleaningCompleted",
                "location_id": "cleaner_slot",
                "location_type": "cleaner_slot",
                "owner": "none",
                "status": "ready_to_return",
                "arm": arm or "left",
                "source_location_id": "cleaner_slot",
                "source_location_type": "cleaner_slot",
                "target_location_id": "cleaner_slot",
                "target_location_type": "cleaner_slot",
                "target_owner": "none",
            },
            {
                **common,
                "event_type": "ToolReturnedToTray",
                "location_id": home_location_id,
                "location_type": home_location_type,
                "owner": "none",
                "status": "available",
                "arm": arm or "left",
                "source_location_id": "cleaner_slot",
                "source_location_type": "cleaner_slot",
                "target_location_id": home_location_id,
                "target_location_type": home_location_type,
                "target_owner": "none",
            },
        ]

    if action == "return_unused_preposition":
        return_location_id = str(
            command.get("target_location_id")
            or state.get("preposition_origin_location_id")
            or home_location_id
            or ""
        )
        return_location_type = str(
            command.get("target_location_type")
            or state.get("preposition_origin_location_type")
            or home_location_type
            or ""
        )
        return_lifecycle = str(
            state.get("preposition_origin_lifecycle_stage")
            or (
                "mayo_reuse"
                if return_location_type
                in {"mayo_stand", "mayo_reuse_zone"}
                else "returned_home"
            )
        )
        return [
            {
                **common,
                "event_type": "UnusedPrepositionReturned",
                "location_id": return_location_id,
                "location_type": return_location_type,
                "owner": "none",
                "status": "available",
                "arm": arm or "right",
                "source_location_id": source_location_id or "robot_right_hand",
                "source_location_type": (
                    source_location_type or "robot_right_hand"
                ),
                "target_location_id": return_location_id,
                "target_location_type": return_location_type,
                "target_owner": "none",
                "detail": {
                    **common["detail"],
                    "target_lifecycle_stage": return_lifecycle,
                },
            }
        ]
    return []


def counterfactual_event_world_fingerprint(
    event: dict[str, Any],
    world: dict[str, Any] | None,
) -> str:
    tool_id = str(event.get("instrument_id", "") or "").strip()
    instance_id = _instance_id(event)
    state = _world_instrument_state(world, tool_id, instance_id)
    payload = {
        "instrument_id": tool_id,
        "instance_id": instance_id,
        "lifecycle_stage": str(state.get("lifecycle_stage", "") or ""),
        "location_id": str(state.get("location_id", "") or ""),
        "location_type": str(state.get("location_type", "") or ""),
        "owner": str(state.get("owner", "") or ""),
        "status": str(state.get("status", "") or ""),
        "right_hand_tool": str((world or {}).get("right_hand_tool", "") or ""),
        "right_hand_instance_id": str(
            (world or {}).get("right_hand_tool_instance_id")
            or (world or {}).get("right_hand_instance_id")
            or ""
        ),
        "left_hand_tool": str((world or {}).get("left_hand_tool", "") or ""),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def counterfactual_event_matches_world(
    event: dict[str, Any],
    world: dict[str, Any] | None,
) -> bool:
    if world is None:
        return False
    event_type = str(event.get("event_type", "") or "").strip()
    tool_id = str(event.get("instrument_id", "") or "").strip()
    state = _world_instrument_state(world, tool_id, _instance_id(event))
    if not state:
        return False
    lifecycle = str(state.get("lifecycle_stage", "") or "").strip()
    owner = str(state.get("owner", "") or "").strip()
    location_id = str(state.get("location_id", "") or "").strip()
    expected_location = str(
        event.get("target_location_id", "")
        or event.get("location_id", "")
        or ""
    ).strip()

    if event_type in {"RobotGraspedTool", "ToolPrepared"}:
        return (
            lifecycle == "prepositioned_right"
            and str(world.get("right_hand_tool", "") or "").strip() == tool_id
        )
    if event_type == "ToolHandoverCompleted":
        return lifecycle == "surgeon_owned" and owner == "surgeon"
    if event_type in {
        "PredictedToolReturnedToRack",
        "UnusedPrepositionReturned",
        "ToolReturnedToTray",
    }:
        expected_lifecycles = {"home_rack", "returned_home"}
        if (
            event_type == "UnusedPrepositionReturned"
            and str(
                event.get("target_location_type", "")
                or event.get("location_type", "")
                or ""
            ).strip()
            in {"mayo_stand", "mayo_reuse_zone"}
        ):
            expected_lifecycles = {"mayo_reuse"}
        return (
            lifecycle in expected_lifecycles
            and owner in {"", "none"}
            and (not expected_location or location_id == expected_location)
            and str(world.get("right_hand_tool", "") or "").strip() != tool_id
        )
    if event_type == "ToolReceivedFromSurgeon":
        return (
            lifecycle == "recovering_left"
            and str(world.get("left_hand_tool", "") or "").strip() == tool_id
        )
    if event_type in {"ToolSentToCleaner", "ToolCleaningProgress"}:
        return lifecycle == "cleaning_left"
    if event_type == "ToolCleaningCompleted":
        return lifecycle == "cleaned_left"
    return False


def counterfactual_terminal_matches_world(
    command: dict[str, Any],
    events: list[dict[str, Any]],
    world: dict[str, Any] | None,
) -> bool:
    if world is None or not events:
        return False
    action = str(command.get("action", "") or "").strip().lower()
    final_event = events[-1]
    if not counterfactual_event_matches_world(final_event, world):
        return False
    if action != "put_down_and_handover":
        return True
    displaced = next(
        (
            event
            for event in events
            if str(event.get("detail", {}).get("semantic_step", ""))
            == "put_down_existing_right_hand_tool"
        ),
        None,
    )
    return bool(
        displaced
        and counterfactual_event_matches_world(displaced, world)
    )


def counterfactual_task_boundary_payload(
    command: dict[str, Any],
    *,
    started: bool,
    duration_sec: float,
) -> dict[str, Any]:
    command_id = str(command.get("command_id", "") or "").strip()
    action = str(command.get("action", "") or "").strip().lower()
    source_location_id = str(
        command.get("source_location_id", "") or ""
    ).strip()
    target_location_id = str(
        command.get("target_location_id", "") or ""
    ).strip()
    return {
        "event_type": "RobotTaskStarted" if started else "RobotTaskCompleted",
        "instrument_id": str(command.get("instrument_id", "") or "").strip(),
        "instance_id": _instance_id(command),
        "confidence": 1.0,
        "arm": str(command.get("arm", "") or "").strip(),
        "source_location_id": source_location_id,
        "source_location_type": str(
            command.get("source_location_type", "") or source_location_id
        ).strip(),
        "target_location_id": target_location_id,
        "target_location_type": str(
            command.get("target_location_type", "") or target_location_id
        ).strip(),
        "detail": {
            "command_id": command_id,
            "instance_id": _instance_id(command),
            "request_generation": int(
                command.get("request_generation", 0) or 0
            ),
            "task_id": command_id,
            "task_type": action,
            "duration_sec": float(duration_sec if started else 0.0),
            "source_anchor_id": source_location_id,
            "target_anchor_id": target_location_id,
            "ground_truth_used": False,
            "physical_execution_attempted": False,
            "transport": "shadow_counterfactual_feedback",
        },
    }


def counterfactual_task_failure_payload(
    command: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    payload = counterfactual_task_boundary_payload(
        command,
        started=False,
        duration_sec=0.0,
    )
    payload["event_type"] = "RobotTaskFailed"
    payload["status"] = "failed"
    payload["detail"] = {
        **payload["detail"],
        "outcome": "failed",
        "reason": reason,
    }
    return payload


class ShadowSkillSinkNode(Node):
    def __init__(self) -> None:
        super().__init__("shadow_skill_sink")
        self.declare_parameter("dedupe_retention_sec", 300.0)
        self.declare_parameter("counterfactual_success_feedback", False)
        self.declare_parameter("allow_type_instance_assumption", False)
        self.declare_parameter("prepare_duration_sec", 1.8)
        self.declare_parameter("handover_duration_sec", 2.6)
        self.declare_parameter("recovery_duration_sec", 6.0)
        self.declare_parameter("return_duration_sec", 1.8)
        self.declare_parameter("group_action_duration_sec", 2.0)
        self.declare_parameter("feedback_confirmation_timeout_sec", 1.5)
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self._retention_sec = max(
            1.0,
            float(self.get_parameter("dedupe_retention_sec").value),
        )
        self._counterfactual_success_feedback = bool(
            self.get_parameter("counterfactual_success_feedback").value
        )
        self._allow_type_instance_assumption = bool(
            self.get_parameter("allow_type_instance_assumption").value
        )
        self._action_durations = {
            **{
                action: max(
                    0.2,
                    float(self.get_parameter("prepare_duration_sec").value),
                )
                for action in PREPARE_ACTIONS
            },
            **{
                action: max(
                    0.2,
                    float(self.get_parameter("handover_duration_sec").value),
                )
                for action in HANDOVER_ACTIONS
            },
            **{
                action: max(
                    0.2,
                    float(self.get_parameter("recovery_duration_sec").value),
                )
                for action in RECOVERY_ACTIONS
            },
            "return_unused_preposition": max(
                0.2,
                float(self.get_parameter("return_duration_sec").value),
            ),
        }
        self._group_action_duration_sec = max(
            0.2,
            float(self.get_parameter("group_action_duration_sec").value),
        )
        self._feedback_confirmation_timeout_sec = max(
            0.2,
            float(
                self.get_parameter(
                    "feedback_confirmation_timeout_sec"
                ).value
            ),
        )
        self._world: dict[str, Any] | None = None
        self._replay_run_id = ""
        self._replay_state = ""
        self._replay_paused_at: float | None = None
        self._seen_command_ids: dict[str, float] = {}
        self._seen_group_command_ids: dict[str, float] = {}
        self._semantic_ledger = SemanticCommandLedger()
        self._pending_actions: dict[str, PendingShadowAction] = {}
        self._pending_group_actions: dict[
            str,
            tuple[BedRobotArmGroupCommand, float],
        ] = {}
        spec = load_bundle(str(self.get_parameter("spec_dir").value))
        group_spec = spec.get_bed_robot_arm_group_spec()
        self._group_profiles = {
            group.id: group.initial_end_effector_profile
            for group in (group_spec.groups if group_spec is not None else [])
            if group.enabled
        }
        self._group_states = {
            group_id: "standby" for group_id in self._group_profiles
        }
        self._publisher = self.create_publisher(
            String,
            "/shadow/skill_outcome",
            50,
        )
        self._skill_status_publisher = self.create_publisher(
            SkillStatus,
            "/skill/status",
            50,
        )
        self._skill_event_publisher = self.create_publisher(
            TwinEvent,
            "/skill/events",
            50,
        )
        self._group_outcome_publisher = self.create_publisher(
            String,
            "/shadow/bed_robot_arm_group_outcome",
            50,
        )
        self._group_status_publisher = self.create_publisher(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            50,
        )
        self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world,
            50,
        )
        self.create_subscription(
            SkillCommand,
            "/bt/skill_command",
            self._on_command,
            50,
        )
        self.create_subscription(
            BedRobotArmGroupCommand,
            "/bt/bed_robot_arm_group_command",
            self._on_group_command,
            50,
        )
        replay_state_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            ShadowReplayState,
            "/shadow/replay_state",
            self._on_replay_state,
            replay_state_qos,
        )
        steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.create_timer(
            1.0,
            self._publish_group_health,
            clock=steady_clock,
        )
        self.create_timer(
            0.1,
            self._advance_pending_actions,
            clock=steady_clock,
        )

    def _on_world(self, msg: WorldState) -> None:
        current_world = message_payload(msg)
        for tool_id, instance_id in departed_prepositioned_instrument_instances(
            self._world,
            current_world,
        ):
            self._semantic_ledger.forget_preparations(tool_id, instance_id)
        for tool_id, instance_id in completed_preposition_instrument_instances(
            self._world,
            current_world,
        ):
            self._semantic_ledger.forget_preparations(tool_id, instance_id)
        for tool_id, instance_id in newly_prepositioned_instrument_instances(
            self._world,
            current_world,
        ):
            self._semantic_ledger.forget_returns(tool_id, instance_id)
        self._world = current_world

    def _reset_replay_runtime(self, run_id: str) -> None:
        self._replay_run_id = str(run_id or "")
        self._replay_paused_at = None
        self._world = None
        self._seen_command_ids.clear()
        self._seen_group_command_ids.clear()
        self._semantic_ledger.reset()
        self._pending_actions.clear()
        self._pending_group_actions.clear()
        for group_id in self._group_states:
            self._group_states[group_id] = "standby"

    def _on_replay_state(self, msg: ShadowReplayState) -> None:
        run_id = str(msg.run_id or "")
        state = str(msg.state or "").strip().lower()
        previous_state = self._replay_state
        run_changed = bool(run_id and run_id != self._replay_run_id)
        reset_state = state != self._replay_state and state in {
            "loading",
            "ready",
            "stopped",
            "timed_out",
        }
        if run_changed or reset_state:
            self._reset_replay_runtime(run_id)
        elif state == "paused" and previous_state != "paused":
            self._replay_paused_at = time.monotonic()
        elif previous_state == "paused" and state != "paused":
            now = time.monotonic()
            paused_at = self._replay_paused_at
            if paused_at is not None:
                paused_duration_sec = max(0.0, now - paused_at)
                for action in self._pending_actions.values():
                    action.shift_clock(paused_duration_sec)
                self._pending_group_actions = {
                    command_id: (command, started_at + paused_duration_sec)
                    for command_id, (command, started_at) in (
                        self._pending_group_actions.items()
                    )
                }
            self._replay_paused_at = None
        self._replay_state = state

    def _on_command(self, msg: SkillCommand) -> None:
        payload = message_payload(msg)
        now = time.monotonic()
        cutoff = now - self._retention_sec
        self._seen_command_ids = {
            key: observed_at
            for key, observed_at in self._seen_command_ids.items()
            if observed_at >= cutoff
        }
        self._semantic_ledger.prune(cutoff)

        command_id = str(payload.get("command_id", "") or "").strip()
        semantic_key = semantic_command_key(payload)
        if command_id and command_id in self._seen_command_ids:
            return
        previous_fingerprint = self._semantic_ledger.previous_fingerprint(
            semantic_key
        )
        if command_id:
            self._seen_command_ids[command_id] = now
        if self._pending_actions:
            status = "blocked"
            reason = "shadow_skill_pipeline_busy"
            fingerprint = command_world_fingerprint(payload, self._world)
        else:
            status, reason, fingerprint = classify_shadow_command_attempt(
                payload,
                self._world,
                previous_admissible_fingerprint=previous_fingerprint,
                allow_type_instance_assumption=(
                    self._allow_type_instance_assumption
                ),
            )
        if status == "deadlock_rejected" and not (
            self._semantic_ledger.should_report_deadlock(
                semantic_key,
                fingerprint,
                now,
            )
        ):
            return
        if status in FEEDBACK_ELIGIBLE_STATUSES:
            if (
                str(payload.get("action", "") or "").strip().lower()
                in PREPARE_ACTIONS
            ):
                self._semantic_ledger.begin_preparation_episode(
                    str(payload.get("instrument_id", "") or ""),
                    _instance_id(payload),
                )
            self._semantic_ledger.record_admission(
                semantic_key,
                fingerprint,
                now,
            )
        outcome = {
            "command_id": command_id,
            "action": str(payload.get("action", "")),
            "instrument_id": str(payload.get("instrument_id", "")),
            "instance_id": _instance_id(payload),
            "request_generation": int(payload.get("request_generation", 0) or 0),
            "arm": str(payload.get("arm", "")),
            "semantic_command_key": semantic_key,
            "status": status,
            "reason": reason,
            "execution_attempted": False,
            "counterfactual_feedback_published": bool(
                status in FEEDBACK_ELIGIBLE_STATUSES
                and self._counterfactual_success_feedback
            ),
            "ground_truth_used": False,
            "world_phase": (
                str(self._world.get("filtered_phase", ""))
                if self._world is not None
                else ""
            ),
            "world_execution_state": (
                str(self._world.get("execution_state", ""))
                if self._world is not None
                else ""
            ),
        }
        result = String()
        result.data = json.dumps(outcome, separators=(",", ":"), sort_keys=True)
        self._publisher.publish(result)
        if (
            status in FEEDBACK_ELIGIBLE_STATUSES
            and self._counterfactual_success_feedback
        ):
            self._schedule_counterfactual_feedback(payload, status=status)
        else:
            terminal_reason = (
                reason
                if status not in FEEDBACK_ELIGIBLE_STATUSES
                else "counterfactual_feedback_disabled"
            )
            self._publish_skill_progress(
                payload,
                progress=0.0,
                state="rejected",
                success=False,
                message=(
                    f"shadow command not executed: {terminal_reason}"
                ),
            )

    def _publish_group_health(self) -> None:
        for group_id, profile in self._group_profiles.items():
            status = BedRobotArmGroupStatus()
            status.stamp = self.get_clock().now().to_msg()
            status.request_id = f"health-shadow-{group_id}"
            status.group_id = group_id
            status.state = self._group_states.get(group_id, "standby")
            status.outcome = "available"
            status.terminal = True
            status.success = True
            status.message = "shadow counterfactual group controller available"
            status.end_effector_profile = profile
            status.confidence = 1.0
            status.progress = 1.0
            self._group_status_publisher.publish(status)

    def _on_group_command(self, msg: BedRobotArmGroupCommand) -> None:
        payload = message_payload(msg)
        now = time.monotonic()
        cutoff = now - self._retention_sec
        self._seen_group_command_ids = {
            key: observed_at
            for key, observed_at in self._seen_group_command_ids.items()
            if observed_at >= cutoff
        }
        command_id = str(payload.get("command_id", "") or "").strip()
        duplicate = bool(
            command_id and command_id in self._seen_group_command_ids
        )
        if command_id:
            self._seen_group_command_ids[command_id] = now
        enabled = str(payload.get("group_id", "")) in self._group_profiles
        status_name = (
            "duplicate_suppressed"
            if duplicate
            else ("admissible" if enabled else "blocked")
        )
        reason = (
            "duplicate_command_id"
            if duplicate
            else (
                "shadow_only_no_execution"
                if enabled
                else "group_not_enabled_in_procedure"
            )
        )
        outcome = {
            "request_id": str(payload.get("request_id", "")),
            "command_id": command_id,
            "group_id": str(payload.get("group_id", "")),
            "operation": str(payload.get("operation", "")),
            "status": status_name,
            "reason": reason,
            "execution_attempted": False,
            "counterfactual_feedback_published": bool(
                enabled
                and not duplicate
                and self._counterfactual_success_feedback
            ),
            "ground_truth_used": False,
        }
        result = String()
        result.data = json.dumps(outcome, separators=(",", ":"), sort_keys=True)
        self._group_outcome_publisher.publish(result)
        if (
            enabled
            and not duplicate
            and self._counterfactual_success_feedback
        ):
            self._schedule_group_completion(msg)

    def _schedule_group_completion(
        self,
        command: BedRobotArmGroupCommand,
    ) -> None:
        command_id = str(command.command_id or command.request_id or "").strip()
        if not command_id:
            command_id = f"shadow-group-{time.monotonic_ns()}"
        self._pending_group_actions[command_id] = (
            command,
            time.monotonic(),
        )
        self._publish_group_progress(
            command,
            progress=0.0,
            terminal=False,
        )

    def _publish_group_progress(
        self,
        command: BedRobotArmGroupCommand,
        *,
        progress: float,
        terminal: bool,
    ) -> None:
        state = shadow_group_terminal_state(command.operation)
        if terminal:
            self._group_states[command.group_id] = state
        if terminal and command.end_effector_profile:
            self._group_profiles[command.group_id] = (
                command.end_effector_profile
            )
        status = BedRobotArmGroupStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.request_id = command.request_id
        status.command_id = command.command_id
        status.group_id = command.group_id
        status.operation = command.operation
        status.state = state if terminal else "executing"
        status.outcome = "succeeded" if terminal else "running"
        status.terminal = terminal
        status.success = terminal
        status.message = (
            "counterfactual shadow completion; no physical execution attempted"
        )
        status.direction = command.direction
        status.distance_mm = float(command.distance_mm)
        status.distance_origin = command.distance_origin
        status.raw_distance_text = command.raw_distance_text
        status.end_effector_profile = self._group_profiles.get(
            command.group_id,
            command.end_effector_profile,
        )
        status.confidence = float(command.confidence)
        status.progress = float(progress)
        status.elapsed_sec = float(progress * self._group_action_duration_sec)
        status.remaining_sec = float(
            max(0.0, (1.0 - progress) * self._group_action_duration_sec)
        )
        self._group_status_publisher.publish(status)

    def _schedule_counterfactual_feedback(
        self,
        payload: dict[str, Any],
        *,
        status: str,
    ) -> None:
        command_id = str(payload.get("command_id", "") or "").strip()
        if not command_id:
            command_id = f"shadow-skill-{time.monotonic_ns()}"
            payload = {**payload, "command_id": command_id}
        events = counterfactual_event_payloads(
            payload,
            self._world,
            instance_resolution_assumed=(status == "instance_resolution_assumed"),
        )
        if not events:
            reason = "counterfactual_lifecycle_plan_unavailable"
            self._publish_event(
                counterfactual_task_failure_payload(payload, reason=reason)
            )
            self._publish_skill_progress(
                payload,
                progress=0.0,
                state="failed",
                success=False,
                message=f"shadow counterfactual failed: {reason}",
            )
            self._publish_terminal_outcome(
                payload,
                status="failed",
                reason=reason,
            )
            return
        self._pending_actions[command_id] = PendingShadowAction(
            payload=dict(payload),
            admission_status=status,
            events=events,
            started_at=time.monotonic(),
            duration_sec=self._action_durations.get(
                str(payload.get("action", "") or "").strip().lower(),
                2.0,
            ),
        )
        self._publish_event(
            counterfactual_task_boundary_payload(
                payload,
                started=True,
                duration_sec=self._pending_actions[command_id].duration_sec,
            )
        )
        self._publish_skill_progress(
            payload,
            progress=0.0,
            state="running",
            success=False,
        )

    def _publish_event(self, event_payload: dict[str, Any]) -> None:
        event = TwinEvent()
        event.stamp = self.get_clock().now().to_msg()
        event.event_type = str(event_payload.get("event_type", ""))
        event.instrument_id = str(event_payload.get("instrument_id", ""))
        if hasattr(event, "instance_id"):
            event.instance_id = _instance_id(event_payload)
        event.phase_id = (
            str(self._world.get("filtered_phase", ""))
            if self._world is not None
            else ""
        )
        event.location_id = str(event_payload.get("location_id", ""))
        event.location_type = str(event_payload.get("location_type", ""))
        event.owner = str(event_payload.get("owner", ""))
        event.status = str(event_payload.get("status", ""))
        event.confidence = float(event_payload.get("confidence", 1.0))
        event.detail_json = json.dumps(
            event_payload.get("detail", {}),
            separators=(",", ":"),
            sort_keys=True,
        )
        event.arm = str(event_payload.get("arm", ""))
        event.source_location_id = str(
            event_payload.get("source_location_id", "")
        )
        event.source_location_type = str(
            event_payload.get("source_location_type", "")
        )
        event.target_location_id = str(
            event_payload.get("target_location_id", "")
        )
        event.target_location_type = str(
            event_payload.get("target_location_type", "")
        )
        event.target_owner = str(event_payload.get("target_owner", ""))
        event.cleaning_required = bool(
            event_payload.get("cleaning_required", False)
        )
        event.mode = "shadow_counterfactual"
        self._skill_event_publisher.publish(event)

    def _publish_skill_progress(
        self,
        payload: dict[str, Any],
        *,
        progress: float,
        state: str,
        success: bool,
        message: str = "",
    ) -> None:
        status = SkillStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.command_id = str(payload.get("command_id", "") or "")
        status.action = str(payload.get("action", "") or "")
        status.instrument_id = str(payload.get("instrument_id", "") or "")
        if hasattr(status, "instance_id"):
            status.instance_id = _instance_id(payload)
        if hasattr(status, "request_generation"):
            status.request_generation = int(
                payload.get("request_generation", 0) or 0
            )
        status.state = state
        status.success = success
        status.message = message or (
            "counterfactual shadow completion; no physical execution attempted"
        )
        status.arm = str(payload.get("arm", "") or "")
        status.source_location_id = str(
            payload.get("source_location_id", "") or ""
        )
        status.source_location_type = str(
            payload.get("source_location_type", "") or ""
        )
        status.target_location_id = str(
            payload.get("target_location_id", "") or ""
        )
        status.target_location_type = str(
            payload.get("target_location_type", "") or ""
        )
        status.target_owner = str(payload.get("target_owner", "") or "")
        status.cleaning_required = bool(
            payload.get("cleaning_required", False)
        )
        status.mode = "shadow_counterfactual"
        duration = self._action_durations.get(
            str(payload.get("action", "") or "").strip().lower(),
            2.0,
        )
        status.progress = float(progress)
        status.elapsed_sec = float(progress * duration)
        terminal = state in {
            "completed",
            "failed",
            "cancelled",
            "rejected",
        }
        status.remaining_sec = float(
            0.0 if terminal else max(0.0, (1.0 - progress) * duration)
        )
        self._skill_status_publisher.publish(status)

    def _publish_terminal_outcome(
        self,
        payload: dict[str, Any],
        *,
        status: str,
        reason: str,
    ) -> None:
        outcome = {
            "command_id": str(payload.get("command_id", "") or ""),
            "action": str(payload.get("action", "") or ""),
            "instrument_id": str(payload.get("instrument_id", "") or ""),
            "instance_id": _instance_id(payload),
            "request_generation": int(
                payload.get("request_generation", 0) or 0
            ),
            "semantic_command_key": semantic_command_key(payload),
            "status": status,
            "reason": reason,
            "execution_attempted": False,
            "counterfactual_feedback_published": True,
            "ground_truth_used": False,
        }
        result = String()
        result.data = json.dumps(
            outcome,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._publisher.publish(result)

    def _fail_pending_action(
        self,
        command_id: str,
        action: PendingShadowAction,
        *,
        reason: str,
    ) -> None:
        self._publish_event(
            counterfactual_task_failure_payload(
                action.payload,
                reason=reason,
            )
        )
        self._publish_skill_progress(
            action.payload,
            progress=action.progress(time.monotonic()),
            state="failed",
            success=False,
            message=f"shadow counterfactual failed: {reason}",
        )
        self._publish_terminal_outcome(
            action.payload,
            status="failed",
            reason=reason,
        )
        self._pending_actions.pop(command_id, None)

    def _advance_pending_actions(self) -> None:
        if self._replay_state == "paused":
            return
        now = time.monotonic()
        for command_id, action in list(self._pending_actions.items()):
            if action.awaiting_event is not None:
                current_fingerprint = counterfactual_event_world_fingerprint(
                    action.awaiting_event,
                    self._world,
                )
                if (
                    current_fingerprint != action.awaiting_fingerprint
                    and counterfactual_event_matches_world(
                        action.awaiting_event,
                        self._world,
                    )
                ):
                    action.awaiting_event = None
                    action.awaiting_fingerprint = ""
                    action.awaiting_since = 0.0
                elif (
                    now - action.awaiting_since
                    >= self._feedback_confirmation_timeout_sec
                ):
                    event_type = str(
                        action.awaiting_event.get("event_type", "") or ""
                    )
                    self._fail_pending_action(
                        command_id,
                        action,
                        reason=(
                            "counterfactual_event_not_reflected_in_world:"
                            f"{event_type or 'unknown'}"
                        ),
                    )
                    continue
                else:
                    self._publish_skill_progress(
                        action.payload,
                        progress=action.progress(now),
                        state="running",
                        success=False,
                        message="waiting for digital twin lifecycle confirmation",
                    )
                    continue

            event_payload = action.next_due_event(now)
            if event_payload is not None:
                action.awaiting_event = event_payload
                action.awaiting_fingerprint = (
                    counterfactual_event_world_fingerprint(
                        event_payload,
                        self._world,
                    )
                )
                action.awaiting_since = now
                self._publish_event(event_payload)
                self._publish_skill_progress(
                    action.payload,
                    progress=action.progress(now),
                    state="running",
                    success=False,
                    message="waiting for digital twin lifecycle confirmation",
                )
                continue

            progress = action.progress(now)
            if action.complete(now):
                if not counterfactual_terminal_matches_world(
                    action.payload,
                    action.events,
                    self._world,
                ):
                    self._fail_pending_action(
                        command_id,
                        action,
                        reason="terminal_lifecycle_not_confirmed",
                    )
                    continue
                self._publish_event(
                    counterfactual_task_boundary_payload(
                        action.payload,
                        started=False,
                        duration_sec=0.0,
                    )
                )
                self._publish_skill_progress(
                    action.payload,
                    progress=1.0,
                    state="completed",
                    success=True,
                )
                self._pending_actions.pop(command_id, None)
            else:
                self._publish_skill_progress(
                    action.payload,
                    progress=progress,
                    state="running",
                    success=False,
                )

        for command_id, (command, started_at) in list(
            self._pending_group_actions.items()
        ):
            progress = min(
                1.0,
                max(
                    0.0,
                    (now - started_at) / self._group_action_duration_sec,
                ),
            )
            self._publish_group_progress(
                command,
                progress=progress,
                terminal=progress >= 1.0,
            )
            if progress >= 1.0:
                self._pending_group_actions.pop(command_id, None)


def main() -> None:
    rclpy.init()
    node = ShadowSkillSinkNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
