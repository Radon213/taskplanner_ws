"""Pure atomic validation for runtime-tunable tracker parameters."""

from __future__ import annotations

from dataclasses import fields, replace
import math
from typing import Mapping

from .core import TrackerConfig


DYNAMIC_TRACKER_PARAMETERS = frozenset(
    field.name for field in fields(TrackerConfig)
)
BOOLEAN_TRACKER_PARAMETERS = frozenset(
    {"exchangeable_instances", "mayo_appearance_assumes_surgeon_return"}
)
DYNAMIC_PARAMETERS = DYNAMIC_TRACKER_PARAMETERS | {
    "enabled",
    "publish_period_sec",
    "health_freshness_sec",
}
RELOAD_PARAMETERS = frozenset({"spec_dir"})
IMMUTABLE_PARAMETERS = frozenset(
    {
        "spec_root",
        "bundle_snapshot_root",
        "cam3_pose_topic",
        "cam4_pose_topic",
        "cam3_health_topic",
        "cam4_health_topic",
        "output_topic",
        "skill_command_topic",
        "skill_status_topic",
        "skill_event_topic",
        "simulation_state_topic",
        "world_state_topic",
    }
)


def validated_runtime_update(
    current_config: TrackerConfig,
    current_publish_period_sec: float,
    current_health_freshness_sec: float,
    updates: Mapping[str, object],
) -> tuple[TrackerConfig, float, float]:
    """Validate all updates before returning a new atomic runtime state."""

    unknown = sorted(
        set(updates) - DYNAMIC_PARAMETERS - RELOAD_PARAMETERS - IMMUTABLE_PARAMETERS
    )
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    immutable = sorted(set(updates) & IMMUTABLE_PARAMETERS)
    if immutable:
        raise ValueError(
            "restart required for immutable parameters: " + ", ".join(immutable)
        )
    if "spec_dir" in updates:
        value = updates["spec_dir"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("spec_dir must be a non-empty string")
    if "enabled" in updates and not isinstance(updates["enabled"], bool):
        raise ValueError("enabled must be boolean")

    config_updates: dict[str, object] = {}
    for name in DYNAMIC_TRACKER_PARAMETERS:
        if name not in updates:
            continue
        value = updates[name]
        if name in BOOLEAN_TRACKER_PARAMETERS:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            config_updates[name] = value
            continue
        if isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        try:
            converted = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not math.isfinite(converted):
            raise ValueError(f"{name} must be finite")
        config_updates[name] = converted
    next_config = replace(current_config, **config_updates)

    next_period = float(current_publish_period_sec)
    if "publish_period_sec" in updates:
        value = updates["publish_period_sec"]
        if isinstance(value, bool):
            raise ValueError("publish_period_sec must be numeric")
        try:
            next_period = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("publish_period_sec must be numeric") from exc
        if not math.isfinite(next_period) or not 0.02 <= next_period <= 10.0:
            raise ValueError("publish_period_sec must be in [0.02, 10.0]")
    next_health_freshness = float(current_health_freshness_sec)
    if "health_freshness_sec" in updates:
        value = updates["health_freshness_sec"]
        if isinstance(value, bool):
            raise ValueError("health_freshness_sec must be numeric")
        try:
            next_health_freshness = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("health_freshness_sec must be numeric") from exc
        if not math.isfinite(next_health_freshness) or not 0.05 <= next_health_freshness <= 30.0:
            raise ValueError("health_freshness_sec must be in [0.05, 30.0]")
    return next_config, next_period, next_health_freshness
