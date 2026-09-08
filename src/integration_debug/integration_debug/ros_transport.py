"""Dynamic ROS interface resolution used by the Debug typed transport.

The allowlisted endpoint/type pair remains catalog-owned.  This helper only
turns that already-approved pair into a ROS message/request/goal using the
same ``rosidl_runtime_py`` path as the command router.  It intentionally has
no endpoint selection, lifecycle gate, or browser policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rosidl_runtime_py.set_message import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service


def resolve_interface(kind: str, ros_type: str) -> type[Any]:
    normalized = str(kind).strip().casefold()
    if normalized == "topic":
        return get_message(ros_type)
    if normalized == "service":
        return get_service(ros_type)
    if normalized == "action":
        return get_action(ros_type)
    raise ValueError("unsupported typed ROS dispatch kind")


def build_wire_payload(
    *,
    kind: str,
    ros_type: str,
    payload: Mapping[str, Any],
    fixed_payload: Mapping[str, Any] | None = None,
    command_id: str = "",
    command_id_field: str = "",
) -> Any:
    """Create one ROS wire instance from approved declarative payload data."""

    interface = resolve_interface(kind, ros_type)
    normalized_kind = str(kind).strip().casefold()
    if normalized_kind == "topic":
        message = interface()
    elif normalized_kind == "service":
        message = interface.Request()
    elif normalized_kind == "action":
        message = interface.Goal()
    else:  # ``resolve_interface`` already checks; keep a stable error here.
        raise ValueError("unsupported typed ROS dispatch kind")

    fields = dict(fixed_payload or {})
    fields.update(dict(payload))
    field_name = str(command_id_field).strip()
    if field_name:
        if not command_id:
            raise ValueError("typed physical dispatch requires a command ID")
        available = getattr(message, "get_fields_and_field_types", lambda: {})()
        if field_name not in available:
            raise ValueError(
                f"configured command_id_field {field_name!r} is absent from {ros_type}"
            )
        if field_name in fields and str(fields[field_name]) != command_id:
            raise ValueError("browser payload must not replace the generated command ID")
        fields[field_name] = command_id
    try:
        set_message_fields(message, fields)
    except Exception as exc:
        raise ValueError(f"typed ROS payload does not match {ros_type}: {exc}") from exc
    return message


def action_feedback_fields(message: Any) -> tuple[str, float | None]:
    """Extract conventional feedback when present without requiring a codec."""

    feedback = getattr(message, "feedback", message)
    state = str(getattr(feedback, "state", "") or "executing")
    raw_progress = getattr(feedback, "progress", None)
    try:
        progress = float(raw_progress) if raw_progress is not None else None
    except (TypeError, ValueError):
        progress = None
    if progress is not None and not 0.0 <= progress <= 1.0:
        progress = None
    return state, progress


def action_result_fields(message: Any) -> tuple[bool, str, str]:
    """Extract conventional Action result fields with a conservative fallback."""

    result = getattr(message, "result", message)
    success = bool(getattr(result, "success", False))
    final_state = str(
        getattr(result, "final_state", "") or ("completed" if success else "failed")
    )
    reason_code = str(getattr(result, "reason_code", "") or final_state)
    return success, final_state, reason_code


__all__ = [
    "action_feedback_fields",
    "action_result_fields",
    "build_wire_payload",
    "resolve_interface",
]
