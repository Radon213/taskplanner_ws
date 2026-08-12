"""Pure, auditable mappings from internal command envelopes to public requests.

The public requests deliberately select only robot-control-relevant fields.
Planning rationale, policy mode, confidence, raw distance text, and ownership
metadata never leave this module's output dataclasses.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from math import isfinite


PREPARE_ALIASES = frozenset(
    {
        "predict_tool",
        "prepare_tool",
        "tool_predict",
    }
)

TRAY_HANDOVER_ALIASES = frozenset(
    {
        "pick_up_and_handover",
        "tool_handover",
    }
)

ROBOT_HANDOVER_ALIASES = frozenset(
    {
        "direct_handover",
        "predicted_tool_handover",
    }
)

RETURN_TO_TRAY_ALIASES = frozenset({"return_unused_preposition"})

RETRIEVE_ALIASES = frozenset(
    {
        "retrieve_from_mayo",
        "tool_retrieve",
    }
)

LOCATION_TRAY = "tray"
LOCATION_MAYO = "mayo"
LOCATION_ROBOT = "robot"
LOCATION_SURGEON = "surgeon"
PUBLIC_TOOL_LOCATIONS = frozenset(
    {LOCATION_TRAY, LOCATION_MAYO, LOCATION_ROBOT, LOCATION_SURGEON}
)
TRAY_HANDOVER_TRANSITION = (LOCATION_TRAY, LOCATION_SURGEON)
TRAY_PREPARE_TRANSITION = (LOCATION_TRAY, LOCATION_ROBOT)
ROBOT_HANDOVER_TRANSITION = (LOCATION_ROBOT, LOCATION_SURGEON)
RETURN_TO_TRAY_TRANSITION = (LOCATION_ROBOT, LOCATION_TRAY)
RETRIEVE_TRANSITION = (LOCATION_MAYO, LOCATION_TRAY)

GROUP_RETRACTION = "retraction"

OPERATION_RETRACTION = "retraction"
OPERATION_RELEASE_RETRACTION = "release_retraction"
OPERATION_CHANGE_END_EFFECTOR = "change_end_effector"

MAX_RETRACTION_DISTANCE_MM = 30.0

ARM_IDS = frozenset({"arm_1", "arm_2"})
TOOL_THYROID_RETRACTOR = "thyroid_retractor"
TOOL_ARMY_NAVY_RETRACTOR = "army_navy_retractor"
TARGET_LEFT_MALLEABLE = "left_malleable"
TARGET_RIGHT_MALLEABLE = "right_malleable"
TARGET_BOTH_MALLEABLE = "both_malleable"
TARGET_RETRACTOR_IDS = frozenset(
    {TARGET_LEFT_MALLEABLE, TARGET_RIGHT_MALLEABLE, TARGET_BOTH_MALLEABLE}
)

ADJUSTMENT_SINGLE = "single"
ADJUSTMENT_MULTI = "multi"
DIRECTION_FRAME_SURGEON_VIEW = "surgeon_view"
CARDINAL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
ADJUSTMENT_AXES = frozenset({"left_right", "up_down"})

TARGET_TOOL_IDS = frozenset(
    {TOOL_THYROID_RETRACTOR, TOOL_ARMY_NAVY_RETRACTOR}
)


class MappingFailure(ValueError):
    """A stable, machine-readable reason that an internal command cannot map."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DispatchLedger:
    """Bounded, pure at-most-once ledger for outbound capability requests."""

    def __init__(self, max_entries: int = 512) -> None:
        self._max_entries = max(1, int(max_entries))
        self._command_ids: set[str] = set()
        self._command_order: deque[str] = deque()
        self._explicit_generations: set[int] = set()
        self._generation_order: deque[int] = deque()

    def reserve(
        self, command_id: str, *, explicit_request_generation: int | None = None
    ) -> bool:
        """Reserve a command only when it has not already been dispatched."""

        normalized_id = command_id.strip()
        if not normalized_id or normalized_id in self._command_ids:
            return False
        generation = (
            int(explicit_request_generation)
            if explicit_request_generation is not None
            else 0
        )
        if generation > 0 and generation in self._explicit_generations:
            return False

        self._command_ids.add(normalized_id)
        self._command_order.append(normalized_id)
        while len(self._command_order) > self._max_entries:
            self._command_ids.discard(self._command_order.popleft())

        if generation > 0:
            self._explicit_generations.add(generation)
            self._generation_order.append(generation)
            while len(self._generation_order) > self._max_entries:
                self._explicit_generations.discard(self._generation_order.popleft())
        return True

    def clear(self) -> None:
        self._command_ids.clear()
        self._command_order.clear()
        self._explicit_generations.clear()
        self._generation_order.clear()


@dataclass(frozen=True, slots=True)
class InternalSkillCommand:
    command_id: str
    action: str
    instrument_id: str
    instrument_instance_id: str
    source_location_type: str
    source_location_id: str
    target_location_type: str
    target_location_id: str
    arm: str
    request_generation: int = 0
    rationale: str = ""
    target_owner: str = ""
    cleaning_required: bool = False
    mode: str = ""


@dataclass(frozen=True, slots=True)
class InternalGroupCommand:
    request_id: str
    command_id: str
    group_id: str
    operation: str
    arm_id: str
    target_tool_id: str
    adjustment_mode: str
    target_retractor_id: str
    direction_frame: str
    direction: str
    axis: str
    distance_mm: float
    end_effector_profile: str
    distance_origin: str = ""
    raw_distance_text: str = ""
    rationale: str = ""
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolHandoverRequest:
    command_id: str
    instrument_id: str
    instrument_instance_id: str
    source_location: str
    target_location: str


@dataclass(frozen=True, slots=True)
class RetractionAdjustmentRequest:
    command_id: str
    adjustment_mode: str
    target_retractor_id: str
    direction_frame: str
    direction: str
    axis: str
    distance_mm: float


@dataclass(frozen=True, slots=True)
class ToolChangeRequest:
    command_id: str
    arm_id: str
    target_tool_id: str


def public_instrument_instance_id(
    *,
    internal_instrument_id: str,
    internal_instance_id: str,
    instrument_name: str,
) -> str:
    """Replace a private catalog prefix such as ``T04`` with the real name."""

    internal_id = internal_instrument_id.strip()
    instance_id = internal_instance_id.strip()
    public_name = instrument_name.strip()
    if not internal_id or not instance_id or not public_name:
        return ""
    if instance_id.casefold() == internal_id.casefold():
        return public_name
    for separator in ("#", "-", "_"):
        prefix, found, suffix = instance_id.partition(separator)
        if found and prefix.casefold() == internal_id.casefold() and suffix.strip():
            return f"{public_name}#{suffix.strip()}"
    return instance_id


def map_skill_to_tool_handover(
    command: InternalSkillCommand,
    *,
    instrument_name: str,
    instrument_instance_id: str,
) -> ToolHandoverRequest:
    """Map an internal tool command to one minimal public transfer request."""

    action = command.action.strip()
    if action in PREPARE_ALIASES:
        source_hint = " ".join(
            (command.source_location_type, command.source_location_id)
        ).casefold()
        if "tray" not in source_hint and "rack" not in source_hint:
            raise MappingFailure("invalid_prepare_source_location")
        source_location, target_location = TRAY_PREPARE_TRANSITION
    elif action in TRAY_HANDOVER_ALIASES:
        source_location, target_location = TRAY_HANDOVER_TRANSITION
    elif action in ROBOT_HANDOVER_ALIASES:
        source_location, target_location = ROBOT_HANDOVER_TRANSITION
    elif action in RETURN_TO_TRAY_ALIASES:
        source_location, target_location = RETURN_TO_TRAY_TRANSITION
    elif action in RETRIEVE_ALIASES:
        source_location, target_location = RETRIEVE_TRANSITION
    else:
        raise MappingFailure("unsupported_skill_action")

    public_name = instrument_name.strip()
    public_instance_id = instrument_instance_id.strip()
    required = (
        command.command_id,
        public_name,
        public_instance_id,
    )
    if not all(value.strip() for value in required):
        raise MappingFailure("invalid_tool_transfer_command")

    private_code = re.compile(r"(?<![A-Za-z0-9])T0*\d+(?![A-Za-z0-9])", re.IGNORECASE)
    if private_code.search(public_name) or private_code.search(public_instance_id):
        raise MappingFailure("private_instrument_code")

    return ToolHandoverRequest(
        command_id=command.command_id,
        instrument_id=public_name,
        instrument_instance_id=public_instance_id,
        source_location=source_location,
        target_location=target_location,
    )


def map_group_command(
    command: InternalGroupCommand,
    *,
    max_retraction_distance_mm: float = MAX_RETRACTION_DISTANCE_MM,
) -> RetractionAdjustmentRequest | ToolChangeRequest:
    """Validate and project the internal envelope onto the reviewed contract."""

    if not command.command_id.strip():
        raise MappingFailure("invalid_command_id")

    if command.group_id != GROUP_RETRACTION:
        raise MappingFailure(
            "suction_arm_removed" if command.group_id == "suction" else "unsupported_group"
        )

    if command.operation == OPERATION_CHANGE_END_EFFECTOR:
        arm_id = command.arm_id.strip().casefold()
        if arm_id not in ARM_IDS:
            raise MappingFailure("invalid_arm_id")
        target_tool_id = command.target_tool_id.strip().casefold()
        if target_tool_id not in TARGET_TOOL_IDS:
            raise MappingFailure("invalid_target_tool")
        return ToolChangeRequest(
            command_id=command.command_id,
            arm_id=arm_id,
            target_tool_id=target_tool_id,
        )

    if command.operation != OPERATION_RETRACTION:
        raise MappingFailure("unsupported_retraction_operation")

    adjustment_mode = command.adjustment_mode.strip().casefold()
    target_retractor_id = command.target_retractor_id.strip().casefold()
    direction_frame = command.direction_frame.strip().casefold()
    direction = command.direction.strip().casefold()
    axis = command.axis.strip().casefold()
    if direction_frame != DIRECTION_FRAME_SURGEON_VIEW:
        raise MappingFailure("invalid_direction_frame")
    distance_mm = float(command.distance_mm)
    maximum = float(max_retraction_distance_mm)
    if (
        not isfinite(distance_mm)
        or distance_mm <= 0.0
        or not isfinite(maximum)
        or maximum <= 0.0
        or distance_mm > maximum
    ):
        raise MappingFailure("invalid_retraction_distance")

    if adjustment_mode == ADJUSTMENT_SINGLE:
        if target_retractor_id not in {
            TARGET_LEFT_MALLEABLE,
            TARGET_RIGHT_MALLEABLE,
        }:
            raise MappingFailure("invalid_target_retractor")
        if direction not in CARDINAL_DIRECTIONS or axis != "none":
            raise MappingFailure("invalid_single_adjustment")
    elif adjustment_mode == ADJUSTMENT_MULTI:
        if target_retractor_id != TARGET_BOTH_MALLEABLE:
            raise MappingFailure("invalid_target_retractor")
        if direction != "none" or axis not in ADJUSTMENT_AXES:
            raise MappingFailure("invalid_multi_adjustment")
    else:
        raise MappingFailure("invalid_adjustment_mode")

    return RetractionAdjustmentRequest(
        command_id=command.command_id,
        adjustment_mode=adjustment_mode,
        target_retractor_id=target_retractor_id,
        direction_frame=direction_frame,
        direction=direction,
        axis=axis,
        distance_mm=distance_mm,
    )
