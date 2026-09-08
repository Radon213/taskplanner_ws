"""Compose a public observer status with the private Debug control owner."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STATUS_SCHEMA = "taskplanner.integration_debug.status.v1"
CONTROL_OWNER_STATUS_MAX_AGE_SEC = 3.0

_CONTROL_FIELDS = (
    "session",
    "action",
    "endpoints",
    "outputs",
    "voice",
    "virtual_robot",
    "asr",
    "operational_asr",
    "surgery_record",
    "recent_events",
    "dispatch",
)


def control_owner_status(value: object) -> dict[str, Any] | None:
    """Accept only the minimum shape needed to project a control owner."""

    if not isinstance(value, Mapping) or value.get("schema") != STATUS_SCHEMA:
        return None
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(row, Mapping)
        and isinstance(row.get("name"), str)
        and isinstance(row.get("enabled"), bool)
        for row in capabilities
    ):
        return None
    if not isinstance(value.get("session"), Mapping):
        return None
    if not isinstance(value.get("runtime"), Mapping):
        return None
    if not isinstance(value.get("action"), Mapping):
        return None
    return dict(value)


def compose_observer_status(
    observer_status: Mapping[str, Any],
    control_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Overlay write-owner state without replacing observer input telemetry."""

    composed = dict(observer_status)
    control = control_owner_status(control_status)
    if control is None:
        return composed

    observer_capabilities = {
        str(row.get("name")): dict(row)
        for row in composed.get("capabilities", [])
        if isinstance(row, Mapping) and str(row.get("name", "")).strip()
    }
    for row in control["capabilities"]:
        assert isinstance(row, Mapping)
        name = str(row["name"])
        if name != "observer":
            observer_capabilities[name] = dict(row)
    composed["capabilities"] = [
        observer_capabilities[name] for name in sorted(observer_capabilities)
    ]

    observer_runtime = composed.get("runtime")
    control_runtime = control["runtime"]
    if isinstance(observer_runtime, Mapping):
        # The observer owns network inspection; the control owner owns all
        # session-admission state shown beside mutable controls.
        merged_runtime = dict(observer_runtime)
        merged_runtime.update(dict(control_runtime))
        if "network" in observer_runtime:
            merged_runtime["network"] = observer_runtime["network"]
        composed["runtime"] = merged_runtime

    for field in _CONTROL_FIELDS:
        if field in control:
            composed[field] = control[field]
    return composed


__all__ = [
    "CONTROL_OWNER_STATUS_MAX_AGE_SEC",
    "compose_observer_status",
    "control_owner_status",
]
