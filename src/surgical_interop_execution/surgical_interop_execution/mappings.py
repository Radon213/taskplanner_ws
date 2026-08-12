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

GROUP_SUCTION = "suction"
GROUP_RETRACTION = "retraction"

OPERATION_SUCTION_START = "suction_start"
OPERATION_SUCTION_STOP = "suction_stop"
OPERATION_RETRACTION = "retraction"
OPERATION_RELEASE_RETRACTION = "release_retraction"
OPERATION_CHANGE_END_EFFECTOR = "change_end_effector"

PUBLIC_RETRACTION_OPERATIONS = {
    OPERATION_RETRACTION: "MOVE",
    OPERATION_RELEASE_RETRACTION: "RELEASE",
    OPERATION_CHANGE_END_EFFECTOR: "CHANGE_END_EFFECTOR",
}

MAX_RETRACTION_DISTANCE_MM = 30.0


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
    direction: str
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
class RetractionRequest:
    command_id: str
    operation: str
    direction: str
    distance_mm: float
    end_effector_profile: str


@dataclass(frozen=True, slots=True)
class SuctionRequest:
    command_id: str
    enabled: bool


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
) -> RetractionRequest | SuctionRequest:
    """Map a group command to the one public capability it actually requests."""

    if not command.command_id.strip():
        raise MappingFailure("invalid_command_id")

    if command.group_id == GROUP_SUCTION:
        if command.operation == OPERATION_SUCTION_START:
            return SuctionRequest(command_id=command.command_id, enabled=True)
        if command.operation == OPERATION_SUCTION_STOP:
            return SuctionRequest(command_id=command.command_id, enabled=False)
        raise MappingFailure("unsupported_suction_operation")

    if command.group_id != GROUP_RETRACTION:
        raise MappingFailure("unsupported_group")

    public_operation = PUBLIC_RETRACTION_OPERATIONS.get(command.operation)
    if public_operation is None:
        raise MappingFailure("unsupported_retraction_operation")

    direction = command.direction.strip().upper()
    profile = command.end_effector_profile.strip()
    distance_mm = float(command.distance_mm)
    if public_operation == "MOVE":
        if (
            not direction
            or not isfinite(distance_mm)
            or distance_mm <= 0.0
            or distance_mm > MAX_RETRACTION_DISTANCE_MM
        ):
            raise MappingFailure("invalid_retraction_command")
    else:
        direction = ""
        distance_mm = 0.0

    if public_operation == "CHANGE_END_EFFECTOR" and not profile:
        raise MappingFailure("missing_end_effector_profile")
    if public_operation != "CHANGE_END_EFFECTOR":
        profile = ""

    return RetractionRequest(
        command_id=command.command_id,
        operation=public_operation,
        direction=direction,
        distance_mm=distance_mm,
        end_effector_profile=profile,
    )
