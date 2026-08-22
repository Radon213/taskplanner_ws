"""Deterministic adapter fakes with one cross-device call trace."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import threading
from typing import Any, Iterable, Mapping, Sequence

from . import (
    AdapterError,
    AdapterUnavailableError,
    ControllerState,
    ForceTorqueSample,
    JointStateSample,
)
from .clock import Clock, FakeClock


@dataclass(frozen=True, slots=True)
class AdapterCall:
    sequence: int
    timestamp_ns: int
    component: str
    method: str
    args: tuple[Any, ...]
    kwargs: tuple[tuple[str, Any], ...]
    command_id: str = ""


class CallTrace:
    """Thread-safe ordered call log shared by multiple fake adapters."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or FakeClock()
        self._records: list[AdapterCall] = []
        self._lock = threading.Lock()
        self._context = threading.local()

    def set_command_context(self, command_id: str) -> None:
        self._context.command_id = str(command_id).strip()

    def clear_command_context(self) -> None:
        self._context.command_id = ""

    def record(
        self,
        component: str,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> AdapterCall:
        with self._lock:
            call = AdapterCall(
                sequence=len(self._records),
                timestamp_ns=self.clock.monotonic_ns(),
                component=str(component),
                method=str(method),
                args=tuple(args),
                kwargs=tuple(sorted(kwargs.items())),
                command_id=str(getattr(self._context, "command_id", "")),
            )
            self._records.append(call)
            return call

    @property
    def records(self) -> tuple[AdapterCall, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def calls(self) -> tuple[AdapterCall, ...]:
        return self.records

    @property
    def method_names(self) -> tuple[str, ...]:
        return tuple(record.method for record in self.records)

    def as_tuples(self) -> tuple[tuple[str, str], ...]:
        return tuple((record.component, record.method) for record in self.records)

    def records_for(self, command_id: str) -> tuple[AdapterCall, ...]:
        normalized = str(command_id).strip()
        return tuple(
            record for record in self.records if record.command_id == normalized
        )

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class _FailureInjectable:
    def __init__(self) -> None:
        self._failures: dict[str, deque[BaseException]] = defaultdict(deque)

    def fail_next(self, method: str, error: BaseException) -> None:
        self._failures[str(method)].append(error)

    def queue_failure(self, method: str, error: BaseException) -> None:
        self.fail_next(method, error)

    def _raise_planned(self, method: str) -> None:
        pending = self._failures.get(method)
        if pending:
            raise pending.popleft()


class FakeIndyDcp3Adapter(_FailureInjectable):
    """Synchronous robot fake whose state changes only after a traced call."""

    component = "indy_dcp3"

    def __init__(
        self,
        *,
        trace: CallTrace | None = None,
        clock: Clock | None = None,
        joint_positions: Mapping[str, Sequence[float]] | None = None,
        connected: bool = True,
    ) -> None:
        super().__init__()
        self.clock = clock or (trace.clock if trace is not None else FakeClock())
        self.trace = trace or CallTrace(self.clock)
        self.connected = bool(connected)
        self.closed = False
        self.direct_teaching = False
        self.custom_gain_enabled = False
        self.custom_gain: Any | None = None
        self.friction_compensation: Any | None = None
        self.motion_active = False
        self.holding = False
        self.fault_code = ""
        self._joint_positions = {
            str(arm_id): tuple(float(value) for value in values)
            for arm_id, values in (joint_positions or {}).items()
        }

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.trace.record(self.component, method, *args, **kwargs)
        self._raise_planned(method)
        if self.closed or not self.connected:
            raise AdapterUnavailableError(
                "robot_unavailable",
                "robot adapter is closed or disconnected",
                component=self.component,
                operation=method,
                retryable=True,
            )

    def set_joint_positions(self, arm_id: str, positions: Sequence[float]) -> None:
        self._joint_positions[str(arm_id)] = tuple(float(value) for value in positions)

    def set_friction_compensation(self, value: Any) -> None:
        self._call("set_friction_compensation", value)
        self.friction_compensation = value

    def set_custom_gain(self, enabled: bool, gain: Any | None = None) -> None:
        self._call("set_custom_gain", bool(enabled), gain)
        self.custom_gain_enabled = bool(enabled)
        self.custom_gain = gain if enabled else None

    def set_direct_teaching(self, enabled: bool) -> None:
        self._call("set_direct_teaching", bool(enabled))
        self.direct_teaching = bool(enabled)

    def is_direct_teaching_enabled(self) -> bool:
        self._call("is_direct_teaching_enabled")
        return self.direct_teaching

    def read_joint_state(self, arm_id: str) -> JointStateSample:
        self._call("read_joint_state", arm_id)
        try:
            positions = self._joint_positions[str(arm_id)]
        except KeyError as exc:
            raise AdapterUnavailableError(
                "joint_state_missing",
                f"no joint state configured for arm {arm_id!r}",
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
        normalized = tuple(float(value) for value in positions)
        self._call(
            "move_joint_positions",
            arm_id,
            normalized,
            waypoint_name=waypoint_name,
        )
        self.motion_active = True
        self._joint_positions[str(arm_id)] = normalized
        self.motion_active = False
        self.holding = False

    def jog_tcp(
        self,
        arm_id: str,
        *,
        axis: str,
        distance_mm: float,
        frame: str,
    ) -> None:
        self._call(
            "jog_tcp",
            arm_id,
            axis=axis,
            distance_mm=float(distance_mm),
            frame=frame,
        )
        self.motion_active = False
        self.holding = False

    def stop_motion(self) -> None:
        self._call("stop_motion")
        self.motion_active = False

    def hold_position(self) -> None:
        self._call("hold_position")
        self.motion_active = False
        self.holding = True

    def controller_state(self) -> ControllerState:
        self._call("controller_state")
        return ControllerState(
            connected=self.connected and not self.closed,
            motion_active=self.motion_active,
            direct_teaching=self.direct_teaching,
            fault_code=self.fault_code,
            source_timestamp_ns=self.clock.monotonic_ns(),
        )

    def close(self) -> None:
        self.trace.record(self.component, "close")
        self._raise_planned("close")
        self.closed = True
        self.connected = False


class FakeAft200Adapter(_FailureInjectable):
    """Force-sensor fake with explicit lifecycle and recording state."""

    component = "aft200"

    def __init__(
        self,
        *,
        trace: CallTrace | None = None,
        clock: Clock | None = None,
        samples: Mapping[str, ForceTorqueSample] | None = None,
    ) -> None:
        super().__init__()
        self.clock = clock or (trace.clock if trace is not None else FakeClock())
        self.trace = trace or CallTrace(self.clock)
        self.running = False
        self.closed = False
        self.recording_session_id = ""
        self._samples = dict(samples or {})

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.trace.record(self.component, method, *args, **kwargs)
        self._raise_planned(method)
        if self.closed:
            raise AdapterUnavailableError(
                "sensor_closed",
                "force sensor adapter is closed",
                component=self.component,
                operation=method,
            )

    def set_sample(self, sample: ForceTorqueSample) -> None:
        self._samples[sample.sensor_id] = sample

    def set_samples(self, samples: Iterable[ForceTorqueSample]) -> None:
        for sample in samples:
            self.set_sample(sample)

    def start(self) -> None:
        self._call("start")
        self.running = True

    def stop(self) -> None:
        self._call("stop")
        self.running = False
        self.recording_session_id = ""

    def begin_recording(self, session_id: str) -> None:
        self._call("begin_recording", session_id)
        if not self.running:
            raise AdapterUnavailableError(
                "sensor_not_running",
                "force sensor reader must be started before recording",
                component=self.component,
                operation="begin_recording",
            )
        if self.recording_session_id:
            raise AdapterError(
                "recording_busy",
                "a force recording is already active",
                component=self.component,
                operation="begin_recording",
            )
        self.recording_session_id = str(session_id)

    def end_recording(self) -> None:
        self._call("end_recording")
        self.recording_session_id = ""

    def latest_sample(self, sensor_id: str) -> ForceTorqueSample:
        self._call("latest_sample", sensor_id)
        if not self.running:
            raise AdapterUnavailableError(
                "sensor_not_running",
                "force sensor reader is not running",
                component=self.component,
                operation="latest_sample",
                retryable=True,
            )
        try:
            sample = self._samples[str(sensor_id)]
        except KeyError as exc:
            raise AdapterUnavailableError(
                "force_sample_missing",
                f"no force sample configured for sensor {sensor_id!r}",
                component=self.component,
                operation="latest_sample",
                retryable=True,
            ) from exc
        if not sample.valid:
            raise AdapterError(
                "force_sample_invalid",
                f"latest sample for {sensor_id!r} is invalid",
                component=self.component,
                operation="latest_sample",
            )
        return sample

    def close(self) -> None:
        self.trace.record(self.component, "close")
        self._raise_planned("close")
        self.closed = True
        self.running = False
        self.recording_session_id = ""


# Short aliases are convenient in tests and do not imply production authority.
FakeRobotAdapter = FakeIndyDcp3Adapter
FakeForceSensorAdapter = FakeAft200Adapter


__all__ = [
    "AdapterCall",
    "CallTrace",
    "FakeAft200Adapter",
    "FakeForceSensorAdapter",
    "FakeIndyDcp3Adapter",
    "FakeRobotAdapter",
]
