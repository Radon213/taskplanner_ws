from __future__ import annotations

import time
from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode
from surgical_msgs.msg import VLMHealth


class _Twin:
    def __init__(self, *, running: bool) -> None:
        self.state = SimpleNamespace(
            running=running,
            execution_state="running" if running else "idle",
            safety_flags=[],
        )
        self.clear_calls = 0

    def set_safety_flag(self, flag: str, active: bool) -> None:
        if active and flag not in self.state.safety_flags:
            self.state.safety_flags.append(flag)
        elif not active:
            self.state.safety_flags = [
                current for current in self.state.safety_flags if current != flag
            ]

    def clear_perception_evidence(self) -> None:
        self.clear_calls += 1


def _node(*, running: bool) -> tuple[ORDigitalTwinNode, _Twin]:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    twin = _Twin(running=running)
    node._twin = twin
    node._vlm_mode = "real"
    node._vlm_health_timeout_sec = 6.0
    node._vlm_health_by_topic = {}
    # Result freshness is intentionally stale before a scenario requests its
    # first VLM inference.  It must not be mistaken for model failure.
    node._input_source_status_by_id = {
        "vlm": SimpleNamespace(healthy=False, state="STALE")
    }
    node._vlm_evidence_blocked = False
    node._vlm_health_run_started_monotonic = (
        time.monotonic() if running else None
    )
    return node, twin


def _health(*, healthy: bool, connected: bool, error: str = "") -> VLMHealth:
    value = VLMHealth()
    value.healthy = healthy
    value.connected = connected
    value.last_error = error
    return value


def test_idle_or_first_request_wait_does_not_latch_vlm_unhealthy() -> None:
    idle, idle_twin = _node(running=False)
    idle._refresh_vlm_safety_flags()
    assert idle_twin.state.safety_flags == []

    starting, starting_twin = _node(running=True)
    starting._refresh_vlm_safety_flags()
    assert starting_twin.state.safety_flags == []


def test_stale_vlm_result_freshness_is_not_a_model_fault() -> None:
    node, twin = _node(running=True)
    node._vlm_health_run_started_monotonic = time.monotonic() - 10.0
    node._vlm_health_by_topic = {
        "/vlm/health": (_health(healthy=True, connected=True), time.monotonic())
    }

    node._refresh_vlm_safety_flags()

    assert twin.state.safety_flags == []
    assert twin.clear_calls == 0


def test_actual_vlm_failure_still_latches_and_withdraws_visual_evidence() -> None:
    node, twin = _node(running=True)
    node._vlm_health_by_topic = {
        "/vlm/health": (
            _health(healthy=False, connected=True, error="provider timeout"),
            time.monotonic(),
        )
    }

    node._refresh_vlm_safety_flags()

    assert twin.state.safety_flags == ["vlm_unhealthy"]
    assert twin.clear_calls == 1
