"""Pure mappings for the public controller state and diagnostic snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


_PUBLIC_STATE_BY_INTERNAL = {
    "IDLE": "standby",
    "TAUGHT_READY": "standby",
    "DIRECT_TEACHING": "direct_teach",
    "RETRACTING": "retracting",
    "TOOL_CHANGING": "changing_tool",
    "STOPPING": "moving_to_standby",
    "FAULT": "fault",
}


def public_arm_state(internal_state: object) -> str:
    raw = getattr(internal_state, "value", internal_state)
    normalized = str(raw or "").strip().upper()
    return _PUBLIC_STATE_BY_INTERNAL.get(normalized, "unknown")


@dataclass(frozen=True, slots=True)
class ArmStatus:
    arm_id: str
    role_instance_id: str
    state: str
    reason_code: str = "ok"

    @property
    def direct_teach_active(self) -> bool:
        return self.state == "direct_teach"


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    connected: bool
    internal_state: str
    active_command_id: str
    operation: str
    fault_code: str
    fault_message: str
    pending_count: int
    sensor_fresh: bool
    profile_id: str
    profile_checksum: str
    adapter_mode: str
    extras: Mapping[str, str]

    @property
    def healthy(self) -> bool:
        return bool(
            self.connected
            and self.sensor_fresh
            and not self.fault_code
            and self.internal_state.upper() != "FAULT"
        )

