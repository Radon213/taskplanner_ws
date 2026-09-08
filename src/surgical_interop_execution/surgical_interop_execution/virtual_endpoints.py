"""Explicit, isolated endpoint names and route state for integration runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# Keep virtual Action/Service servers off the public controller names.  An
# Action or Service client targeting this prefix cannot select a real endpoint
# that advertises the reviewed production `/surgery/...` name on the same DDS
# domain.
VIRTUAL_ENDPOINT_PREFIX = "/integration/virtual/"
VIRTUAL_TOOL_HANDOVER_ENDPOINT = "/integration/virtual/surgery/tool_handover"
VIRTUAL_RETRACTION_SERVICE_ENDPOINT = "/integration/virtual/surgery/retraction/command"
EXTERNAL_TOOL_HANDOVER_ENDPOINT = "/surgery/tool_handover"
EXTERNAL_RETRACTION_SERVICE_ENDPOINT = "/surgery/retraction/command"
EXTERNAL_CONTROLLER_CONTRACT_TOPIC = "/surgery/controller_contract"
VIRTUAL_CONTROLLER_CONTRACT_TOPIC = "/integration/virtual/surgery/controller_contract"

# Command producers never select a physical or virtual controller endpoint.
# They call these stable execution-owner proxy endpoints instead.  The
# execution bridge remains the only owner of route selection; the proxy keeps
# an in-flight request bound to the selected route until it reaches a terminal
# Service receipt or Action result.
EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT = "/taskplanner/execution/tool_handover"
EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT = (
    "/taskplanner/execution/retraction/command"
)

# This compact, latched JSON projection is deliberately read-only.  It gives
# the operator UI the independently selected Action and Service sources without
# teaching the browser how to infer controller identity from endpoint strings.
EXECUTION_ROUTE_STATE_SCHEMA = "taskplanner.execution_route_state.v1"
EXECUTION_ROUTE_STATE_TOPIC = "/integration/execution_route/state"
EXECUTION_ROUTE_COMMAND_SERVICE = "/integration/execution_route/command"
EXTERNAL_ENDPOINT_SOURCE = "external"
VIRTUAL_ENDPOINT_SOURCE = "virtual"
_ENDPOINT_SOURCES = frozenset(
    {EXTERNAL_ENDPOINT_SOURCE, VIRTUAL_ENDPOINT_SOURCE}
)

_ROUTE_STATE_INITIALIZATION_STATES = frozenset(
    {
        "launch_default",
        "initializing",
        "initialized",
        "running",
        "stopped",
        "reset",
    }
)
_ROUTE_STATE_MAX_TEXT_CHARS = 192


@dataclass(frozen=True, slots=True)
class ExecutionRouteState:
    """Validated read-only route state shared by bridge, preflight, and UI.

    ``selected_source`` retains the tool-handover source for V1 consumers.
    ``retraction_source`` is independently selected so one unavailable
    controller endpoint can be replaced with the isolated virtual endpoint
    without diverting the other operation. During an active run both source
    fields are latched and no source change is accepted.
    """

    revision: int
    initialization_revision: int
    selected_source: str
    run_endpoint_source: str
    retraction_source: str
    run_retraction_source: str
    initialization_state: str
    tool_handover_endpoint: str
    retraction_service_name: str
    controller_contract_topic: str
    expected_controller_contract_id: str
    expected_capability_policy_id: str
    retraction_controller_contract_topic: str
    retraction_expected_controller_contract_id: str
    require_bed_robot_status: bool
    require_physical_stop_confirmation: bool
    retraction_state_machine_suppressed: bool


def _route_state_text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > _ROUTE_STATE_MAX_TEXT_CHARS:
        raise ValueError(f"execution route state {field} is invalid")
    return text


def _route_state_optional_text(value: object) -> str:
    """Return bounded diagnostic metadata without making it authoritative."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if len(text) <= _ROUTE_STATE_MAX_TEXT_CHARS else ""


def _route_state_revision(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"execution route state {field} is invalid")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"execution route state {field} is invalid"
        ) from exc
    if revision < 0:
        raise ValueError(f"execution route state {field} is invalid")
    return revision


def parse_execution_route_state(
    payload: object,
    *,
    allow_external_retraction_state_machine_suppression: bool = False,
) -> ExecutionRouteState:
    """Validate the bounded route-state projection before applying it.

    The state is an intra-process coordination signal, not an authority
    channel.  Strict endpoint/source validation makes a malformed or stale
    projection fail closed in preflight instead of accidentally selecting a
    physical controller from the virtual path. Controller-contract fields are
    bounded diagnostics only: their absence or format never changes the
    selected Action/Service route or blocks its acknowledgement.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("execution route state must be an object")
    if payload.get("schema") != EXECUTION_ROUTE_STATE_SCHEMA:
        raise ValueError("execution route state schema mismatch")
    selected_source = normalize_robot_endpoint_source(
        _route_state_text(payload.get("selected_source"), field="selected_source")
    )
    raw_run_source = str(payload.get("run_endpoint_source") or "").strip()
    if raw_run_source:
        run_endpoint_source = normalize_robot_endpoint_source(raw_run_source)
        if run_endpoint_source != selected_source:
            raise ValueError("execution route state run source mismatch")
    else:
        run_endpoint_source = ""
    retraction_source = normalize_robot_endpoint_source(
        _route_state_text(
            payload.get("retraction_source") or selected_source,
            field="retraction_source",
        )
    )
    raw_run_retraction_source = str(
        payload.get("run_retraction_source") or ""
    ).strip()
    if raw_run_retraction_source:
        run_retraction_source = normalize_robot_endpoint_source(
            raw_run_retraction_source
        )
        if run_retraction_source != retraction_source:
            raise ValueError("execution route state retraction run source mismatch")
    else:
        run_retraction_source = ""
    initialization_state = _route_state_text(
        payload.get("initialization_state"), field="initialization_state"
    )
    if initialization_state not in _ROUTE_STATE_INITIALIZATION_STATES:
        raise ValueError("execution route state initialization_state is invalid")
    require_bed_robot_status = payload.get("require_bed_robot_status")
    require_physical_stop_confirmation = payload.get(
        "require_physical_stop_confirmation"
    )
    retraction_state_machine_suppressed = payload.get(
        "retraction_state_machine_suppressed"
    )
    if not all(
        isinstance(value, bool)
        for value in (
            require_bed_robot_status,
            require_physical_stop_confirmation,
            retraction_state_machine_suppressed,
        )
    ):
        raise ValueError("execution route state boolean field is invalid")
    controller_contract_topic = _route_state_optional_text(
        payload.get("controller_contract_topic")
    )
    expected_controller_contract_id = _route_state_optional_text(
        payload.get("expected_controller_contract_id")
    )
    expected_capability_policy_id = _route_state_optional_text(
        payload.get("expected_capability_policy_id")
    )
    retraction_controller_contract_topic = _route_state_optional_text(
        payload.get("retraction_controller_contract_topic")
    ) or controller_contract_topic
    retraction_expected_controller_contract_id = _route_state_optional_text(
        payload.get("retraction_expected_controller_contract_id")
    ) or expected_controller_contract_id
    state = ExecutionRouteState(
        revision=_route_state_revision(payload.get("revision"), field="revision"),
        initialization_revision=_route_state_revision(
            payload.get("initialization_revision"),
            field="initialization_revision",
        ),
        selected_source=selected_source,
        run_endpoint_source=run_endpoint_source,
        retraction_source=retraction_source,
        run_retraction_source=run_retraction_source,
        initialization_state=initialization_state,
        tool_handover_endpoint=_route_state_text(
            payload.get("tool_handover_endpoint"),
            field="tool_handover_endpoint",
        ),
        retraction_service_name=_route_state_text(
            payload.get("retraction_service_name"),
            field="retraction_service_name",
        ),
        controller_contract_topic=controller_contract_topic,
        expected_controller_contract_id=expected_controller_contract_id,
        expected_capability_policy_id=expected_capability_policy_id,
        retraction_controller_contract_topic=(
            retraction_controller_contract_topic
        ),
        retraction_expected_controller_contract_id=(
            retraction_expected_controller_contract_id
        ),
        require_bed_robot_status=require_bed_robot_status,
        require_physical_stop_confirmation=require_physical_stop_confirmation,
        retraction_state_machine_suppressed=retraction_state_machine_suppressed,
    )
    validate_endpoint_source(
        source=state.selected_source,
        endpoint=state.tool_handover_endpoint,
        endpoint_kind="tool handover",
    )
    validate_endpoint_source(
        source=state.retraction_source,
        endpoint=state.retraction_service_name,
        endpoint_kind="retraction service",
    )
    if (
        state.retraction_source == VIRTUAL_ENDPOINT_SOURCE
        and (
            state.require_physical_stop_confirmation
            or not state.retraction_state_machine_suppressed
        )
    ):
        raise ValueError("execution route virtual state is inconsistent")
    if (
        state.retraction_source == EXTERNAL_ENDPOINT_SOURCE
        and state.retraction_state_machine_suppressed
        and not allow_external_retraction_state_machine_suppression
    ):
        raise ValueError("execution route external state is inconsistent")
    if (
        state.retraction_source == VIRTUAL_ENDPOINT_SOURCE
        and state.require_bed_robot_status
    ):
        raise ValueError("virtual retraction route cannot require bed robot status")
    return state


def normalize_robot_endpoint_source(value: str) -> str:
    """Validate the launch-lifetime controller endpoint source selector."""

    source = str(value).strip().casefold()
    if source not in _ENDPOINT_SOURCES:
        raise ValueError(
            "robot_endpoint_source must be 'external' or 'virtual'"
        )
    return source


def is_isolated_virtual_endpoint(endpoint: str) -> bool:
    """Return whether an endpoint is inside the dedicated virtual namespace."""

    return str(endpoint).strip().startswith(VIRTUAL_ENDPOINT_PREFIX)


def validate_endpoint_source(
    *,
    source: str,
    endpoint: str,
    endpoint_kind: str,
) -> str:
    """Ensure one operation resolves only to its reviewed endpoint family."""

    normalized_source = normalize_robot_endpoint_source(source)
    virtual = is_isolated_virtual_endpoint(endpoint)
    if normalized_source == VIRTUAL_ENDPOINT_SOURCE and not virtual:
        raise ValueError(
            f"virtual route requires an isolated {endpoint_kind} endpoint"
        )
    if normalized_source == EXTERNAL_ENDPOINT_SOURCE and virtual:
        raise ValueError(
            f"external endpoint mode cannot use an isolated {endpoint_kind} endpoint"
        )
    return normalized_source


def validate_virtual_endpoint_configuration(
    *,
    robot_endpoint_source: str,
    tool_handover_endpoint: str,
    retraction_service_name: str,
    require_bed_robot_status: bool,
) -> str:
    """Reject a virtual mode that could reach the real controller contract."""

    source = validate_endpoint_source(
        source=robot_endpoint_source,
        endpoint=tool_handover_endpoint,
        endpoint_kind="tool handover",
    )
    validate_endpoint_source(
        source=source,
        endpoint=retraction_service_name,
        endpoint_kind="retraction service",
    )
    if require_bed_robot_status:
        if source == VIRTUAL_ENDPOINT_SOURCE:
            raise ValueError(
                "virtual endpoint mode is Service-only and cannot require bed robot status"
            )
    return source
