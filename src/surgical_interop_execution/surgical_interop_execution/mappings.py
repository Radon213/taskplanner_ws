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

RETURN_UNUSED_PREPOSITION_ALIASES = frozenset({"return_unused_preposition"})

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
RETURN_UNUSED_PREPOSITION_TRANSITION = (LOCATION_ROBOT, LOCATION_MAYO)
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
# The peer Service has no separate BOTH enum: TARGET_NONE (0) means both arms
# for an adjustment.  Keep this semantic alias so the internal mapper can
# retain the explicit ``both`` intent while serializing the peer-compatible
# wire value.
RETRACTION_TARGET_BOTH = RETRACTION_TARGET_NONE

ARM_1 = "arm_1"
ARM_2 = "arm_2"
ARM_IDS = frozenset({ARM_1, ARM_2})
TOOL_THYROID_RETRACTOR = "thyroid_retractor"
TOOL_ARMY_NAVY_RETRACTOR = "army_navy_retractor"
TARGET_LEFT_MALLEABLE = "left_malleable"
TARGET_RIGHT_MALLEABLE = "right_malleable"
TARGET_BOTH_MALLEABLE = "both_malleable"
TARGET_LEFT_ARMY_NAVY = "left_army_navy"
TARGET_RIGHT_ARMY_NAVY = "right_army_navy"
TARGET_BOTH_ARMY_NAVY = "both_army_navy"
TARGET_RETRACTOR_IDS = frozenset(
    {
        TARGET_LEFT_MALLEABLE,
        TARGET_RIGHT_MALLEABLE,
        TARGET_BOTH_MALLEABLE,
        TARGET_LEFT_ARMY_NAVY,
        TARGET_RIGHT_ARMY_NAVY,
        TARGET_BOTH_ARMY_NAVY,
    }
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
    """Bounded, pure at-most-once ledger for outbound capability requests.

    Command IDs remain globally at-most-once.  An explicit request generation
    is narrower: it is at-most-once per semantic leg so one admitted request
    may intentionally execute multiple controller transitions (for example,
    prepare then hand over) without allowing a replay of either transition.
    """

    _REQUEST_SCOPE = ("__request__", "__request__")

    def __init__(self, max_entries: int = 512) -> None:
        self._max_entries = max(1, int(max_entries))
        self._command_ids: set[str] = set()
        self._command_order: deque[str] = deque()
        self._explicit_generation_legs: set[
            tuple[int, tuple[str, str]]
        ] = set()
        # Rebased/original semantic legs are one logical reservation. Keep
        # their eviction atomic too; otherwise one delayed alternative could
        # become replayable earlier merely because the request used two legs.
        self._generation_leg_groups: deque[
            tuple[tuple[int, tuple[str, str]], ...]
        ] = deque()

    @classmethod
    def _generation_leg(
        cls,
        explicit_request_generation: int | None,
        semantic_leg: tuple[str, str] | None,
    ) -> tuple[int, tuple[str, str]] | None:
        generation = (
            int(explicit_request_generation)
            if explicit_request_generation is not None
            else 0
        )
        if generation <= 0:
            return None
        if semantic_leg is None:
            normalized_leg = cls._REQUEST_SCOPE
        else:
            source_location, target_location = semantic_leg
            normalized_leg = (
                source_location.strip().casefold(),
                target_location.strip().casefold(),
            )
        return generation, normalized_leg

    @classmethod
    def _generation_legs(
        cls,
        explicit_request_generation: int | None,
        semantic_leg: tuple[str, str] | None,
        alternative_semantic_legs: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[int, tuple[str, str]], ...]:
        candidates = (semantic_leg, *alternative_semantic_legs)
        normalized: list[tuple[int, tuple[str, str]]] = []
        for candidate in candidates:
            generation_leg = cls._generation_leg(
                explicit_request_generation,
                candidate,
            )
            if generation_leg is not None and generation_leg not in normalized:
                normalized.append(generation_leg)
        return tuple(normalized)

    def reserve(
        self,
        command_id: str,
        *,
        explicit_request_generation: int | None = None,
        semantic_leg: tuple[str, str] | None = None,
        alternative_semantic_legs: tuple[tuple[str, str], ...] = (),
    ) -> bool:
        """Reserve a command only when it has not already been dispatched."""

        normalized_id = command_id.strip()
        if not normalized_id or normalized_id in self._command_ids:
            return False
        generation_legs = self._generation_legs(
            explicit_request_generation,
            semantic_leg,
            alternative_semantic_legs,
        )
        if any(
            generation_leg in self._explicit_generation_legs
            for generation_leg in generation_legs
        ):
            return False

        self._command_ids.add(normalized_id)
        self._command_order.append(normalized_id)
        while len(self._command_order) > self._max_entries:
            self._command_ids.discard(self._command_order.popleft())

        if generation_legs:
            self._explicit_generation_legs.update(generation_legs)
            self._generation_leg_groups.append(generation_legs)
        while len(self._generation_leg_groups) > self._max_entries:
            for generation_leg in self._generation_leg_groups.popleft():
                self._explicit_generation_legs.discard(generation_leg)
        return True

    def is_reserved(
        self,
        command_id: str,
        *,
        explicit_request_generation: int | None = None,
        semantic_leg: tuple[str, str] | None = None,
    ) -> bool:
        """Return whether an equivalent dispatch has already been consumed.

        Voice-backed corrections may arrive while another Action is active.
        The bridge must check replay/deduplication before it cancels that
        Action; otherwise a replayed voice message could interrupt real work
        even though its replacement Goal would later be suppressed.
        """

        normalized_id = command_id.strip()
        if normalized_id and normalized_id in self._command_ids:
            return True
        generation_leg = self._generation_leg(
            explicit_request_generation,
            semantic_leg,
        )
        return (
            generation_leg is not None
            and generation_leg in self._explicit_generation_legs
        )

    def clear(self) -> None:
        self._command_ids.clear()
        self._command_order.clear()
        self._explicit_generation_legs.clear()
        self._generation_leg_groups.clear()


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
    procedure_run_id: str = ""
    implicit_request_generation: int = 0
    rationale: str = ""
    target_owner: str = ""
    cleaning_required: bool = False
    mode: str = ""
    # Provenance bit set only by the admitted ASR/voice request path.  Do not
    # infer this from ``mode=explicit_request``: non-voice UI or test commands
    # can legitimately use the same policy mode and must not preempt a robot.
    voice_backed: bool = False


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
    elif action in RETURN_UNUSED_PREPOSITION_ALIASES:
        source_location, target_location = RETURN_UNUSED_PREPOSITION_TRANSITION
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
    it rejects the rest instead of discarding safety-relevant detail.  V1's
    parameterless ``CHANGE_TOOL`` means "run the controller's preconfigured
    swap".  It is accepted only when no arm/tool/profile identity is present;
    identity-bearing mount requests remain unrepresentable and are rejected.
    """

    if not command.command_id.strip():
        raise MappingFailure("invalid_command_id")

    if command.group_id != GROUP_RETRACTION:
        raise MappingFailure(
            "suction_arm_removed" if command.group_id == "suction" else "unsupported_group"
        )

    operation = command.operation.strip().casefold()
    if operation == OPERATION_CHANGE_END_EFFECTOR:
        if any(
            str(value).strip()
            for value in (
                command.arm_id,
                command.target_tool_id,
                command.end_effector_profile,
            )
        ):
            raise MappingFailure("retraction_v1_profile_identity_unsupported")
        return RetractionCommandRequest(
            command_id=command.command_id,
            command=RETRACTION_COMMAND_CHANGE_TOOL,
            target_side=RETRACTION_TARGET_NONE,
            distance_m=0.0,
        )
    basic_commands = {
        OPERATION_START_DIRECT_TEACH: RETRACTION_COMMAND_START_DIRECT_TEACH,
        OPERATION_FINISH_DIRECT_TEACH: RETRACTION_COMMAND_FINISH_DIRECT_TEACH,
        OPERATION_START_RETRACTION: RETRACTION_COMMAND_START_RETRACTION,
        OPERATION_STOP_RETRACTION: RETRACTION_COMMAND_STOP_RETRACTION,
        # ``release_retraction`` existed in the internal envelope before the
        # reviewed Service was introduced.  It is a compatible spelling of the
        # new stop command, so preserve it as an explicit compatibility alias.
        OPERATION_RELEASE_RETRACTION: RETRACTION_COMMAND_STOP_RETRACTION,
    }
    if operation in basic_commands:
        target_side = RETRACTION_TARGET_NONE
        if operation == OPERATION_FINISH_DIRECT_TEACH:
            finish_arm = command.arm_id.strip().casefold()
            target_side = {
                "": RETRACTION_TARGET_NONE,
                "none": RETRACTION_TARGET_NONE,
                ARM_1: RETRACTION_TARGET_LEFT,
                ARM_2: RETRACTION_TARGET_RIGHT,
            }.get(finish_arm)
            if target_side is None:
                raise MappingFailure("unsupported_finish_direct_teach_target_arm")
        return RetractionCommandRequest(
            command_id=command.command_id,
            command=basic_commands[operation],
            target_side=target_side,
            distance_m=0.0,
        )
    if operation != OPERATION_RETRACTION:
        raise MappingFailure("unsupported_retraction_operation")

    adjustment_mode = command.adjustment_mode.strip().casefold()
    target_retractor_id = command.target_retractor_id.strip().casefold()
    direction_frame = command.direction_frame.strip().casefold()
    direction = command.direction.strip().casefold()
    axis = command.axis.strip().casefold()
    if direction_frame != DIRECTION_FRAME_SURGEON_VIEW:
        raise MappingFailure("invalid_direction_frame")

    side_by_target = {
        TARGET_LEFT_MALLEABLE: RETRACTION_TARGET_LEFT,
        TARGET_RIGHT_MALLEABLE: RETRACTION_TARGET_RIGHT,
        TARGET_BOTH_MALLEABLE: RETRACTION_TARGET_BOTH,
        TARGET_LEFT_ARMY_NAVY: RETRACTION_TARGET_LEFT,
        TARGET_RIGHT_ARMY_NAVY: RETRACTION_TARGET_RIGHT,
        TARGET_BOTH_ARMY_NAVY: RETRACTION_TARGET_BOTH,
    }
    target_side = side_by_target.get(target_retractor_id)
    if target_side is None:
        raise MappingFailure("unsupported_retraction_target")
    if adjustment_mode == ADJUSTMENT_SINGLE:
        if axis != "none":
            raise MappingFailure("unsupported_retraction_axis")
        # The old action's vector was richer than the new Service.  A side and
        # a matching lateral direction have one unambiguous meaning; every
        # other direction (including up/down or an opposing lateral vector)
        # must stay rejected until the public Service grows a field for it.
        expected_direction = (
            "left" if target_side == RETRACTION_TARGET_LEFT else "right"
        )
        if target_side == RETRACTION_TARGET_BOTH or direction != expected_direction:
            raise MappingFailure("unsupported_retraction_direction_for_service")
    elif adjustment_mode == ADJUSTMENT_MULTI:
        if target_side != RETRACTION_TARGET_BOTH:
            raise MappingFailure("multi_adjustment_requires_both_target")
        # The reviewed public field has no vector/axis slot.  Only a bilateral
        # lateral adjustment can be projected without losing meaning; the
        # equal distance is applied once to each arm by the controller.
        if direction != "none" or axis != "left_right":
            raise MappingFailure("unsupported_bilateral_retraction_axis")
    else:
        raise MappingFailure("unsupported_retraction_adjustment_mode")

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
