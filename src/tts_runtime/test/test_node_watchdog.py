from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest


pytest.importorskip("rclpy")

import tts_runtime.node as node_module
from surgical_interop_msgs.msg import GatewayInfo
from surgical_msgs.msg import HumanoidReply
from tts_runtime.node import TTSRuntimeNode, _create_steady_timer


class _Dispatcher:
    def __init__(self, active_run: str = "run-1") -> None:
        self.active_gateway_instance = "gateway-1" if active_run else ""
        self.active_procedure_run = active_run
        self.scope_calls: list[tuple[str, str]] = []
        self.start_calls = 0

    def set_active_procedure_scope(
        self, gateway_instance_id: str, procedure_run_id: str
    ):
        self.active_gateway_instance = gateway_instance_id
        self.active_procedure_run = procedure_run_id
        self.scope_calls.append((gateway_instance_id, procedure_run_id))
        return []

    def start(self) -> None:
        self.start_calls += 1


class _Correlator:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.evaluate_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def evaluate(self):
        self.evaluate_calls += 1
        return []


class _FixedFeedback:
    def __init__(self) -> None:
        self.gateway_calls: list[dict[str, object]] = []

    def observe_gateway_info(self, **kwargs) -> None:
        self.gateway_calls.append(kwargs)


def _node() -> TTSRuntimeNode:
    node = TTSRuntimeNode.__new__(TTSRuntimeNode)
    node._gateway_info_timeout_sec = 3.0
    node._gateway_info_last_seen_monotonic = 10.0
    node._gateway_info_last_instance_id = "gateway-1"
    node._gateway_info_last_revision = 1
    node._gateway_info_last_source_stamp_ns = 9_000_000_000
    node._test_ros_now_ns = 10_000_000_000
    node._gateway_scope = ("gateway-1", "run-1")
    node._procedure_active = True
    node._scope_stamp_floor_ns = 1
    node._pending_unscoped_replies = deque(maxlen=64)
    node._dispatcher = _Dispatcher()
    node._dispatcher_started = True
    node._correlator = _Correlator()
    node._fixed_feedback = _FixedFeedback()
    node._submit_request = lambda _request: None
    node.get_logger = lambda: SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=node._test_ros_now_ns)
    )
    return node


def _gateway(
    *,
    active: bool = True,
    revision: int = 2,
    stamp_sec: int = 10,
    gateway_instance_id: str = "gateway-1",
) -> GatewayInfo:
    message = GatewayInfo()
    message.stamp.sec = stamp_sec
    message.revision = revision
    message.gateway_instance_id = gateway_instance_id
    message.procedure_run_id = "run-1" if active else ""
    message.procedure_type = "thyroidectomy_demo"
    message.procedure_active = active
    return message


def test_fresh_gateway_heartbeat_does_not_reset_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 12.9)

    node._on_gateway_info_watchdog()

    assert node._gateway_scope == ("gateway-1", "run-1")
    assert node._procedure_active is True
    assert node._dispatcher.scope_calls == []
    assert node._correlator.reset_calls == 0


def test_gateway_timeout_fences_once_then_same_ids_form_new_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    now = [13.1]
    monkeypatch.setattr(node_module.time, "monotonic", lambda: now[0])

    node._on_gateway_info_watchdog()
    node._on_gateway_info_watchdog()

    assert node._gateway_scope == ("", "")
    assert node._procedure_active is False
    assert node._scope_stamp_floor_ns is None
    assert list(node._pending_unscoped_replies) == []
    assert node._dispatcher.scope_calls == [("", "")]
    assert node._correlator.reset_calls == 1
    assert node._fixed_feedback.gateway_calls[-1]["procedure_active"] is False

    now[0] = 14.0
    node._on_gateway_info(_gateway(active=True))

    assert node._gateway_scope == ("gateway-1", "run-1")
    assert node._procedure_active is True
    assert node._gateway_info_last_seen_monotonic == 14.0
    assert node._dispatcher.scope_calls == [
        ("", ""),
        ("gateway-1", "run-1"),
    ]
    assert node._correlator.reset_calls == 2
    assert node._dispatcher.start_calls == 0

    # A normal heartbeat refresh for the same scope is not a scope reset.
    now[0] = 15.0
    node._test_ros_now_ns = 11_000_000_000
    node._on_gateway_info(_gateway(active=True, revision=3, stamp_sec=11))
    assert node._correlator.reset_calls == 2
    assert node._dispatcher.scope_calls == [
        ("", ""),
        ("gateway-1", "run-1"),
    ]


def test_inactive_heartbeat_refreshes_liveness_without_enabling_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    now = [11.0]
    monkeypatch.setattr(node_module.time, "monotonic", lambda: now[0])

    node._on_gateway_info(_gateway(active=False))

    assert node._gateway_info_last_seen_monotonic == 11.0
    assert node._procedure_active is False
    assert node._dispatcher.active_procedure_run == ""
    now[0] = 13.9
    node._on_gateway_info_watchdog()
    assert node._gateway_info_last_seen_monotonic == 11.0
    assert node._dispatcher.scope_calls == [("", "")]


def test_stale_retained_and_malformed_active_gateway_never_enable_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    node._gateway_scope = None
    node._procedure_active = False
    node._dispatcher = _Dispatcher(active_run="")
    node._dispatcher_started = False
    node._gateway_info_last_seen_monotonic = None
    node._gateway_info_last_instance_id = ""
    node._gateway_info_last_revision = -1
    node._gateway_info_last_source_stamp_ns = 0
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 10.0)

    stale = _gateway(revision=1, stamp_sec=1)
    node._on_gateway_info(stale)
    assert node._procedure_active is False
    assert node._dispatcher.start_calls == 0
    assert node._gateway_scope == ("", "")

    malformed = _gateway(revision=2, stamp_sec=10, gateway_instance_id="")
    node._on_gateway_info(malformed)
    assert node._procedure_active is False
    assert node._dispatcher.start_calls == 0
    assert node._dispatcher.active_procedure_run == ""


def test_watchdog_timer_uses_steady_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    captured: dict[str, object] = {}

    class _TimerNode:
        def create_timer(self, period_sec, callback, *, clock):
            captured.update(
                period_sec=period_sec,
                callback=callback,
                clock=clock,
            )
            return "timer"

    def fake_clock(*, clock_type):
        captured["clock_type"] = clock_type
        return marker

    callback = lambda: None
    monkeypatch.setattr(node_module, "Clock", fake_clock)

    clock, timer = _create_steady_timer(_TimerNode(), 0.5, callback)

    assert clock is marker
    assert timer == "timer"
    assert captured["clock"] is marker
    assert captured["clock_type"] == node_module.ClockType.STEADY_TIME
    assert captured["period_sec"] == 0.5
    assert captured["callback"] is callback


def test_humanoid_reply_requires_exact_gateway_and_run_scope() -> None:
    node = _node()
    submitted = []
    node._submit_request = submitted.append

    stale_epoch = HumanoidReply()
    stale_epoch.valid = True
    stale_epoch.speak = True
    stale_epoch.reply_id = "reply-old-gateway"
    stale_epoch.turn_id = "turn-1"
    stale_epoch.utterance_id = "utterance-1"
    stale_epoch.gateway_instance_id = "gateway-old"
    stale_epoch.procedure_run_id = "run-1"
    stale_epoch.text = "이전 게이트웨이 응답"
    stale_epoch.timing = "immediate"
    node._on_reply(stale_epoch)

    current = HumanoidReply()
    current.valid = True
    current.speak = True
    current.reply_id = "reply-current"
    current.turn_id = "turn-2"
    current.utterance_id = "utterance-2"
    current.gateway_instance_id = "gateway-1"
    current.procedure_run_id = "run-1"
    current.text = "현재 응답"
    current.timing = "immediate"
    node._on_reply(current)

    assert [request.reply_id for request in submitted] == ["reply-current"]
    assert submitted[0].gateway_instance_id == "gateway-1"


def test_submit_collision_publishes_terminal_correlated_nack() -> None:
    node = _node()
    rejected: list[tuple[object, str, str]] = []

    class _RejectingDispatcher:
        def submit(self, _request):
            raise ValueError(
                "reply_id collision: gateway_instance_id/"
                "procedure_run_id/utterance_id differ"
            )

        def reject(self, request, *, error_code, message):
            rejected.append((request, error_code, message))

    node._dispatcher = _RejectingDispatcher()
    request = SimpleNamespace(reply_id="reply-collision")

    TTSRuntimeNode._submit_request(node, request)

    assert rejected == [
        (
            request,
            "reply_id_collision",
            "tts_reply_rejected_before_playback",
        )
    ]
