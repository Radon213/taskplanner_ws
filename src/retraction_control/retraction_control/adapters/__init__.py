"""Typed, side-effect-free hardware boundaries for retraction control.

Importing this package never imports a vendor SDK, opens a socket, or starts a
CAN reader.  Production backends must be constructed explicitly and fakes can
be injected into the same protocols for deterministic tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import math
import os
from pathlib import Path
import stat
from typing import Any, Protocol, Sequence, runtime_checkable


def _finite_tuple(values: Sequence[float], *, length: int | None = None) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if length is not None and len(result) != length:
        raise ValueError(f"expected {length} values, got {len(result)}")
    if not result or not all(math.isfinite(value) for value in result):
        raise ValueError("sample values must be finite and non-empty")
    return result


@dataclass(frozen=True, slots=True)
class JointStateSample:
    """One monotonic-timestamped joint sample from a named arm."""

    timestamp_ns: int
    arm_id: str
    positions: tuple[float, ...]

    def __post_init__(self) -> None:
        if int(self.timestamp_ns) < 0:
            raise ValueError("timestamp_ns must be non-negative")
        if not str(self.arm_id).strip():
            raise ValueError("arm_id must not be empty")
        object.__setattr__(self, "positions", _finite_tuple(self.positions))


@dataclass(frozen=True, slots=True)
class ForceTorqueSample:
    """One calibrated force/torque sample from an AFT200 sensor."""

    timestamp_ns: int
    sensor_id: str
    force_n: tuple[float, float, float]
    torque_nm: tuple[float, float, float]
    calibration_id: str = ""
    valid: bool = True

    def __post_init__(self) -> None:
        if int(self.timestamp_ns) < 0:
            raise ValueError("timestamp_ns must be non-negative")
        if not str(self.sensor_id).strip():
            raise ValueError("sensor_id must not be empty")
        object.__setattr__(self, "force_n", _finite_tuple(self.force_n, length=3))
        object.__setattr__(self, "torque_nm", _finite_tuple(self.torque_nm, length=3))


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Minimal controller observation used to confirm an adapter operation."""

    connected: bool
    motion_active: bool = False
    direct_teaching: bool = False
    fault_code: str = ""
    source_timestamp_ns: int = 0


class AdapterError(RuntimeError):
    """Structured adapter failure that is safe to propagate to diagnostics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        component: str,
        operation: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.component = str(component)
        self.operation = str(operation)
        self.retryable = bool(retryable)


class AdapterUnavailableError(AdapterError):
    """Backend is absent, disconnected, or not explicitly configured."""


class AdapterTimeoutError(AdapterError):
    """Backend did not confirm an operation before its configured deadline."""


class AdapterRejectedError(AdapterError):
    """Backend was reached but rejected the requested operation."""


class OwnershipError(RuntimeError):
    """The configured SDK/CAN authority is already owned by another process."""


class SingleOwnerGuard:
    """Advisory, non-blocking process lock for one hardware worker.

    The lock path is deliberately explicit and absolute.  Releasing a lock does
    not unlink the file, avoiding the inode-replacement race common to lock-file
    implementations.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("single-owner lock path must be absolute")
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OwnershipError(
                f"hardware authority lock must not be a symlink: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OwnershipError(
                    f"hardware authority lock must not be a symlink: {self.path}"
                ) from exc
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OwnershipError(
                    f"hardware authority lock must be a regular file: {self.path}"
                )
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise OwnershipError(
                    f"hardware authority is already owned: {self.path}"
                ) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> SingleOwnerGuard:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@runtime_checkable
class IndyDcp3AdapterProtocol(Protocol):
    """Narrow physical-control API consumed by :class:`CommandExecutor`."""

    def set_friction_compensation(self, value: Any) -> None: ...

    def set_custom_gain(self, enabled: bool, gain: Any | None = None) -> None: ...

    def set_direct_teaching(self, enabled: bool) -> None: ...

    def is_direct_teaching_enabled(self) -> bool: ...

    def read_joint_state(self, arm_id: str) -> JointStateSample: ...

    def move_joint_positions(
        self, arm_id: str, positions: Sequence[float], *, waypoint_name: str = ""
    ) -> None: ...

    def jog_tcp(
        self,
        arm_id: str,
        *,
        axis: str,
        distance_mm: float,
        frame: str,
    ) -> None: ...

    def stop_motion(self) -> None: ...

    def hold_position(self) -> None: ...

    def controller_state(self) -> ControllerState: ...

    def close(self) -> None: ...


@runtime_checkable
class Aft200AdapterProtocol(Protocol):
    """Narrow read-only force/torque API consumed by the executor."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def begin_recording(self, session_id: str) -> None: ...

    def end_recording(self) -> None: ...

    def latest_sample(self, sensor_id: str) -> ForceTorqueSample: ...

    def close(self) -> None: ...


# Readable aliases for callers that should not depend on vendor names.
RobotAdapter = IndyDcp3AdapterProtocol
ForceSensorAdapter = Aft200AdapterProtocol

# These imports are safe: both production shells are inert until explicit
# methods are called, and the fakes import no vendor libraries.
from .aft200 import Aft200Adapter, Aft200ReaderBackend
from .clock import Clock, FakeClock, SystemClock
from .fake import (
    AdapterCall,
    CallTrace,
    FakeAft200Adapter,
    FakeForceSensorAdapter,
    FakeIndyDcp3Adapter,
    FakeRobotAdapter,
)
from .indy_dcp3 import IndyDcp3Adapter
from .shadow import ShadowIndyDcp3Adapter


__all__ = [
    "AdapterError",
    "AdapterCall",
    "AdapterRejectedError",
    "AdapterTimeoutError",
    "AdapterUnavailableError",
    "Aft200AdapterProtocol",
    "Aft200Adapter",
    "Aft200ReaderBackend",
    "CallTrace",
    "Clock",
    "ControllerState",
    "ForceSensorAdapter",
    "ForceTorqueSample",
    "FakeAft200Adapter",
    "FakeClock",
    "FakeForceSensorAdapter",
    "FakeIndyDcp3Adapter",
    "FakeRobotAdapter",
    "IndyDcp3Adapter",
    "IndyDcp3AdapterProtocol",
    "JointStateSample",
    "OwnershipError",
    "RobotAdapter",
    "SingleOwnerGuard",
    "ShadowIndyDcp3Adapter",
    "SystemClock",
]
