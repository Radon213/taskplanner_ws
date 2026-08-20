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
MAYO_PREPARE_TRANSITION = (LOCATION_MAYO, LOCATION_ROBOT)
ROBOT_HANDOVER_TRANSITION = (LOCATION_ROBOT, LOCATION_SURGEON)
RETURN_TO_TRAY_TRANSITION = (LOCATION_ROBOT, LOCATION_TRAY)
RETRIEVE_TRANSITION = (LOCATION_MAYO, LOCATION_TRAY)

GROUP_RETRACTION = "retraction"

OPERATION_RETRACTION = "retraction"
OPERATION_RELEASE_RETRACTION = "release_retraction"
OPERATION_CHANGE_END_EFFECTOR = "change_end_effector"
OPERATION_START_DIRECT_TEACH = "start_direct_teach"
OPERATION_FINISH_DIRECT_TEACH = "finish_direct_teach"
OPERATION_START_RETRACTION = "start_retraction"
OPERATION_STOP_RETRACTION = "stop_retraction"

# The reviewed single-service command supports the clinically requested 5 cm
# adjustment.  Deployments may set a stricter value through the bridge
# parameter, but the default must not make the documented command impossible.
MAX_RETRACTION_DISTANCE_MM = 50.0

# Keep the public-service values in this pure module so validation and mapping
# remain testable without generated ROS interfaces.  They intentionally match
# surgical_interop_msgs/srv/ExecuteRetractionCommand.srv.
RETRACTION_PROTOCOL_VERSION_V1 = 1
RETRACTION_COMMAND_START_DIRECT_TEACH = 1
RETRACTION_COMMAND_FINISH_DIRECT_TEACH = 2
RETRACTION_COMMAND_START_RETRACTION = 3
RETRACTION_COMMAND_ADJUST_RETRACTION = 4
RETRACTION_COMMAND_CHANGE_TOOL = 5
RETRACTION_COMMAND_STOP_RETRACTION = 6
RETRACTION_TARGET_NONE = 0
RETRACTION_TARGET_LEFT = 1
RETRACTION_TARGET_RIGHT = 2

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
class RetractionCommandRequest:
    """The complete controller-facing content of the unified Service request.

    ``source_id`` is deliberately a bridge configuration value, not a planner
    command field.  Planner rationale, old controller-specific direction/axis
    fields, arm IDs, and tool IDs are not represented by the reviewed Service
    and must never be silently projected onto it.
    """

    command_id: str
    command: int
    target_side: int
    distance_m: float


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
        source_is_tray = "tray" in source_hint or "rack" in source_hint
        source_is_mayo = "mayo" in source_hint and "recovery" not in source_hint
        if source_is_tray == source_is_mayo:
            raise MappingFailure("invalid_prepare_source_location")
        source_location, target_location = (
            MAYO_PREPARE_TRANSITION
            if source_is_mayo
            else TRAY_PREPARE_TRANSITION
        )
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
) -> RetractionCommandRequest:
    """Project one internal command onto the single reviewed Service contract.

    The previous action accepted controller-specific direction, axis, multi-arm,
    arm-ID, and tool-ID fields.  The replacement Service intentionally does
    not.  This mapper therefore accepts only legacy commands whose meaning is
    losslessly expressible by ``command``, ``target_side``, and ``distance_m``;
    it rejects the rest instead of discarding safety-relevant detail.
    """

    if not command.command_id.strip():
        raise MappingFailure("invalid_command_id")

    if command.group_id != GROUP_RETRACTION:
        raise MappingFailure(
            "suction_arm_removed" if command.group_id == "suction" else "unsupported_group"
        )

    operation = command.operation.strip().casefold()
    basic_commands = {
        OPERATION_START_DIRECT_TEACH: RETRACTION_COMMAND_START_DIRECT_TEACH,
        OPERATION_FINISH_DIRECT_TEACH: RETRACTION_COMMAND_FINISH_DIRECT_TEACH,
        OPERATION_START_RETRACTION: RETRACTION_COMMAND_START_RETRACTION,
        OPERATION_STOP_RETRACTION: RETRACTION_COMMAND_STOP_RETRACTION,
        # ``release_retraction`` existed in the internal envelope before the
        # reviewed Service was introduced.  It is a compatible spelling of the
        # new stop command, so preserve it as an explicit compatibility alias.
        OPERATION_RELEASE_RETRACTION: RETRACTION_COMMAND_STOP_RETRACTION,
        OPERATION_CHANGE_END_EFFECTOR: RETRACTION_COMMAND_CHANGE_TOOL,
    }
    if operation in basic_commands:
        return RetractionCommandRequest(
            command_id=command.command_id,
            command=basic_commands[operation],
            target_side=RETRACTION_TARGET_NONE,
            distance_m=0.0,
        )
    if operation != OPERATION_RETRACTION:
        raise MappingFailure("unsupported_retraction_operation")

    adjustment_mode = command.adjustment_mode.strip().casefold()
    target_retractor_id = command.target_retractor_id.strip().casefold()
    direction_frame = command.direction_frame.strip().casefold()
    direction = command.direction.strip().casefold()
    axis = command.axis.strip().casefold()
    if adjustment_mode != ADJUSTMENT_SINGLE:
        raise MappingFailure("unsupported_retraction_adjustment_mode")
    if direction_frame != DIRECTION_FRAME_SURGEON_VIEW:
        raise MappingFailure("invalid_direction_frame")
    if axis != "none":
        raise MappingFailure("unsupported_retraction_axis")

    side_by_target = {
        TARGET_LEFT_MALLEABLE: RETRACTION_TARGET_LEFT,
        TARGET_RIGHT_MALLEABLE: RETRACTION_TARGET_RIGHT,
    }
    target_side = side_by_target.get(target_retractor_id)
    if target_side is None:
        raise MappingFailure("unsupported_retraction_target")
    # The old action's vector was richer than the new Service.  A side and a
    # matching lateral direction have one unambiguous meaning; every other
    # direction (including up/down or an opposing lateral vector) must stay
    # rejected until the public Service grows a field for it.
    expected_direction = (
        "left" if target_side == RETRACTION_TARGET_LEFT else "right"
    )
    if direction != expected_direction:
        raise MappingFailure("unsupported_retraction_direction_for_service")

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

    return RetractionCommandRequest(
        command_id=command.command_id,
        command=RETRACTION_COMMAND_ADJUST_RETRACTION,
        target_side=target_side,
        distance_m=distance_mm / 1000.0,
    )
