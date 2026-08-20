"""Injectable clocks used to make timestamps and timeouts deterministic."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic_ns(self) -> int: ...

    def wall_time_ns(self) -> int: ...


class SystemClock:
    """Production clock with no background work."""

    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()

    @staticmethod
    def wall_time_ns() -> int:
        return time.time_ns()


@dataclass(slots=True)
class FakeClock:
    """Thread-safe clock advanced only by the test unless auto-step is set."""

    monotonic_value_ns: int = 0
    wall_value_ns: int = 0
    auto_step_ns: int = 0
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if min(self.monotonic_value_ns, self.wall_value_ns, self.auto_step_ns) < 0:
            raise ValueError("fake clock values must be non-negative")
        self._lock = threading.Lock()

    def monotonic_ns(self) -> int:
        with self._lock:
            value = self.monotonic_value_ns
            self.monotonic_value_ns += self.auto_step_ns
            return value

    def wall_time_ns(self) -> int:
        with self._lock:
            value = self.wall_value_ns
            self.wall_value_ns += self.auto_step_ns
            return value

    def advance(self, nanoseconds: int) -> None:
        if nanoseconds < 0:
            raise ValueError("cannot move a monotonic clock backwards")
        with self._lock:
            self.monotonic_value_ns += int(nanoseconds)
            self.wall_value_ns += int(nanoseconds)


__all__ = ["Clock", "FakeClock", "SystemClock"]
