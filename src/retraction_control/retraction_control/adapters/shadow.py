"""Record-only robot adapter for non-actuating shadow execution."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import AdapterUnavailableError, ControllerState, JointStateSample
from .clock import Clock, FakeClock
from .fake import CallTrace


class ShadowIndyDcp3Adapter:
    """Implements the robot protocol without invoking or mutating motion state.

    Joint reads come from an explicitly supplied observation snapshot.  Every
    control method records an ``intent_*`` entry and returns only a simulated
    acknowledgement required to exercise command sequencing.
    """

    component = "indy_dcp3_shadow"
    record_only = True

    def __init__(
        self,
        *,
        trace: CallTrace,
        clock: Clock | None = None,
        observed_joint_positions: Mapping[str, Sequence[float]],
        connected: bool = True,
    ) -> None:
        self.trace = trace
        self.clock = clock or trace.clock or FakeClock()
        self.connected = bool(connected)
        self.closed = False
        self._planned_direct_teaching = False
        self._observed_joint_positions = {
            str(arm_id): tuple(float(value) for value in values)
            for arm_id, values in observed_joint_positions.items()
        }

    def _ensure_available(self, operation: str) -> None:
        if self.closed or not self.connected:
            raise AdapterUnavailableError(
                "shadow_robot_unavailable",
                "shadow observation source is closed or unavailable",
                component=self.component,
                operation=operation,
                retryable=True,
            )

    def _intent(self, operation: str, *args: Any, **kwargs: Any) -> None:
        self._ensure_available(operation)
        self.trace.record(self.component, f"intent_{operation}", *args, **kwargs)

    def set_friction_compensation(self, value: Any) -> None:
        self._intent("set_friction_compensation", value)

    def set_custom_gain(self, enabled: bool, gain: Any | None = None) -> None:
        self._intent("set_custom_gain", bool(enabled), gain)

    def set_direct_teaching(self, enabled: bool) -> None:
        self._intent("set_direct_teaching", bool(enabled))
        self._planned_direct_teaching = bool(enabled)

    def is_direct_teaching_enabled(self) -> bool:
        self._ensure_available("is_direct_teaching_enabled")
        self.trace.record(self.component, "observe_planned_direct_teaching")
        return self._planned_direct_teaching

    def read_joint_state(self, arm_id: str) -> JointStateSample:
        self._ensure_available("read_joint_state")
        self.trace.record(self.component, "observe_joint_state", arm_id)
        try:
            positions = self._observed_joint_positions[str(arm_id)]
        except KeyError as exc:
            raise AdapterUnavailableError(
                "shadow_joint_state_missing",
                f"no shadow joint observation for arm {arm_id!r}",
                component=self.component,
                operation="read_joint_state",
            ) from exc
        return JointStateSample(
            timestamp_ns=self.clock.monotonic_ns(),
            arm_id=str(arm_id),
            positions=positions,
        )

    def move_joint_positions(
        self, arm_id: str, positions: Sequence[float], *, waypoint_name: str = ""
    ) -> None:
        self._intent(
            "move_joint_positions",
            arm_id,
            tuple(float(value) for value in positions),
            waypoint_name=waypoint_name,
        )

    def jog_tcp(
        self,
        arm_id: str,
        *,
        axis: str,
        distance_mm: float,
        frame: str,
    ) -> None:
        self._intent(
            "jog_tcp",
            arm_id,
            axis=str(axis),
            distance_mm=float(distance_mm),
            frame=str(frame),
        )

    def stop_motion(self) -> None:
        self._intent("stop_motion")

    def hold_position(self) -> None:
        self._intent("hold_position")

    def controller_state(self) -> ControllerState:
        self._ensure_available("controller_state")
        self.trace.record(self.component, "observe_controller_state")
        return ControllerState(
            connected=True,
            motion_active=False,
            direct_teaching=False,
            source_timestamp_ns=self.clock.monotonic_ns(),
        )

    def close(self) -> None:
        self.trace.record(self.component, "close_record_only")
        self.closed = True
        self.connected = False


__all__ = ["ShadowIndyDcp3Adapter"]
