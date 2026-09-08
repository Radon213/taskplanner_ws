from __future__ import annotations

from collections import deque
import json
from types import SimpleNamespace

import pytest


pytest.importorskip("rclpy")

from builtin_interfaces.msg import Time
from std_msgs.msg import String
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
        self.finishing = False

    @property
    def active_scope(self) -> tuple[str, str]:
        return (self.active_gateway_instance, self.active_procedure_run)

    def set_active_procedure_scope(
        self, gateway_instance_id: str, procedure_run_id: str
    ):
        self.active_gateway_instance = gateway_instance_id
        self.active_procedure_run = procedure_run_id
        self.scope_calls.append((gateway_instance_id, procedure_run_id))
        return []

    def start(self) -> None:
        self.start_calls += 1

    def is_procedure_finishing(
        self, gateway_instance_id: str, procedure_run_id: str
    ) -> bool:
        return bool(
            self.finishing
            and (gateway_instance_id, procedure_run_id) == self.active_scope
        )


class _Correlator:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.evaluate_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def evaluate(self):
        self.evaluate_calls += 1
        return []


class _Announcements:
    def __init__(self) -> None:
        self.gateway_calls: list[dict[str, object]] = []
        self.duplicate_voice_reply = False

    def observe_gateway_info(self, **kwargs) -> None:
        self.gateway_calls.append(kwargs)

    def observe_voice_intent(self, **_kwargs):
        return ()

    def observe_typed_voice_tool_intent(self, **_kwargs):
        return ()

    def is_duplicate_voice_reply(self, **_kwargs) -> bool:
        return self.duplicate_voice_reply


class _Now:
    def __init__(self, nanoseconds: int) -> None:
        self.nanoseconds = nanoseconds

    def to_msg(self) -> Time:
        stamp = Time()
        stamp.sec = self.nanoseconds // 1_000_000_000
        stamp.nanosec = self.nanoseconds % 1_000_000_000
        return stamp


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
    node._last_procedure_scope = ("gateway-1", "run-1")
    node._last_record_announcement_request_id = ""
    node._scope_stamp_floor_ns = 1
    node._pending_unscoped_replies = deque(maxlen=64)
    node._pending_execution_announcements = deque(maxlen=64)
    node._pending_procedure_lifecycle_events = deque(maxlen=16)
    node._announcement_aliases = {"t02": "애드슨", "t04": "보비"}
    node._dispatcher = _Dispatcher()
    node._dispatcher_started = True
    node._correlator = _Correlator()
    node._announcements = _Announcements()
    node._tts_voice_id = "F1"
    node._tts_output_device = "default"
    node._suppressed_reply_sequence = 0
    node._status_messages = []
    node._status_publisher = SimpleNamespace(publish=node._status_messages.append)
    node._submitted_requests = []
    node._submit_request = node._submitted_requests.append
    node.get_parameter = lambda name: SimpleNamespace(
        value=True if name == "enable_vlm_free_speech" else None
    )
    node.get_logger = lambda: SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: _Now(node._test_ros_now_ns)
    )
    return node


def _gateway(
    *,
    active: bool = True,
    revision: int = 2,
    stamp_sec: int = 10,
    gateway_instance_id: str = "gateway-1",
    procedure_run_id: str = "run-1",
) -> GatewayInfo:
    message = GatewayInfo()
    message.stamp.sec = stamp_sec
    message.revision = revision
    message.gateway_instance_id = gateway_instance_id
    message.procedure_run_id = procedure_run_id if active else ""
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
    assert node._announcements.gateway_calls[-1]["procedure_active"] is False

    now[0] = 14.0
    node._on_gateway_info(_gateway(active=True))

    assert node._gateway_scope == ("gateway-1", "run-1")
    assert node._procedure_active is True
    assert node._gateway_info_last_seen_monotonic == 14.0
    assert node._dispatcher.scope_calls == [
        ("", ""),
        ("gateway-1", "run-1"),
    ]
    assert node._submitted_requests == []
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


def test_inactive_heartbeat_speaks_stop_and_keeps_last_run_for_record_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    now = [11.0]
    monkeypatch.setattr(node_module.time, "monotonic", lambda: now[0])

    node._on_gateway_info(_gateway(active=False))

    assert node._gateway_info_last_seen_monotonic == 11.0
    assert node._procedure_active is False
    assert node._dispatcher.active_procedure_run == "run-1"
    assert [request.text for request in node._submitted_requests] == [
        "수술을 종료합니다."
    ]
    now[0] = 13.9
    node._on_gateway_info_watchdog()
    assert node._gateway_info_last_seen_monotonic == 11.0
    assert node._dispatcher.scope_calls == []


def test_new_active_run_speaks_start_but_same_run_reconnect_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    node._gateway_scope = ("gateway-1", "")
    node._procedure_active = False
    node._dispatcher = _Dispatcher(active_run="run-1")
    node._last_procedure_scope = ("gateway-1", "run-1")
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 11.0)

    node._on_gateway_info(
        _gateway(procedure_run_id="run-2", revision=2, stamp_sec=10)
    )

    assert node._dispatcher.active_scope == ("gateway-1", "run-2")
    assert [request.text for request in node._submitted_requests] == [
        "수술을 시작합니다."
    ]


def test_finish_started_event_speaks_immediately_for_active_run() -> None:
    node = _node()

    node._on_procedure_lifecycle_event(
        String(
            data=json.dumps(
                {
                    "schema": "taskplanner.simulation.lifecycle_event.v1",
                    "event": "procedure_finishing",
                    "procedure_run_id": "run-1",
                }
            )
        )
    )

    assert [request.text for request in node._submitted_requests] == [
        "기구를 정리한 후 수술을 마무리하겠습니다."
    ]


def test_finish_started_event_waits_for_authoritative_active_scope() -> None:
    node = _node()
    node._gateway_scope = None
    node._procedure_active = False
    node._dispatcher_started = False

    event = String(
        data=json.dumps(
            {
                "schema": "taskplanner.simulation.lifecycle_event.v1",
                "event": "procedure_finishing",
                "procedure_run_id": "run-1",
            }
        )
    )
    node._on_procedure_lifecycle_event(event)

    assert node._submitted_requests == []
    assert len(node._pending_procedure_lifecycle_events) == 1

    node._gateway_scope = ("gateway-1", "run-1")
    node._procedure_active = True
    node._dispatcher_started = True
    node._flush_pending_procedure_lifecycle_events()

    assert [request.text for request in node._submitted_requests] == [
        "기구를 정리한 후 수술을 마무리하겠습니다."
    ]


def test_finish_started_event_is_dropped_for_known_inactive_scope() -> None:
    node = _node()
    node._gateway_scope = ("gateway-1", "")
    node._procedure_active = False

    node._on_procedure_lifecycle_event(
        String(
            data=json.dumps(
                {
                    "schema": "taskplanner.simulation.lifecycle_event.v1",
                    "event": "procedure_finishing",
                    "procedure_run_id": "run-1",
                }
            )
        )
    )

    assert node._submitted_requests == []
    assert len(node._pending_procedure_lifecycle_events) == 0


def test_pending_execution_announcement_follows_procedure_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    node._gateway_scope = ("gateway-1", "")
    node._procedure_active = False
    node._dispatcher = _Dispatcher(active_run="run-1")
    node._last_procedure_scope = ("gateway-1", "run-1")
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 11.0)

    node._on_execution_announcement(
        String(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_announcement.v1",
                    "command_id": "prepare-before-gateway",
                    "procedure_run_id": "run-2",
                    "route": "retraction",
                    "retraction_command": 3,
                }
            )
        )
    )
    assert len(node._pending_execution_announcements) == 1

    node._on_gateway_info(
        _gateway(procedure_run_id="run-2", revision=2, stamp_sec=10)
    )

    assert [request.text for request in node._submitted_requests] == [
        "수술을 시작합니다.",
        "리트랙션을 시작합니다.",
    ]


def test_finishing_accepts_only_execution_owned_completion_cleanup() -> None:
    node = _node()
    node._dispatcher.finishing = True

    def fact(command_id: str, action: str, *, completion_cleanup: bool) -> String:
        return String(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_announcement.v1",
                    "command_id": command_id,
                    "procedure_run_id": "run-1",
                    "route": "tool_transfer",
                    "action": action,
                    "instrument_id": "T02",
                    "request_generation": 0,
                    "voice_backed": False,
                    "completion_cleanup": completion_cleanup,
                }
            )
        )

    node._on_execution_announcement(
        fact("finish-retrieve-adson", "retrieve_from_mayo", completion_cleanup=True)
    )
    node._on_execution_announcement(
        fact("late-handover-adson", "pick_up_and_handover", completion_cleanup=False)
    )

    assert [request.text for request in node._submitted_requests] == [
        "애드슨을 회수하겠습니다."
    ]


def test_execution_fact_before_active_gateway_is_flushed_for_its_exact_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    # A retained inactive heartbeat is common just before start.  The first
    # Action/Service admission must not be dropped merely because it arrived
    # before the active GatewayInfo edge.
    node._gateway_scope = ("gateway-1", "")
    node._procedure_active = False
    node._dispatcher_started = True
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 11.0)
    node._on_execution_announcement(
        String(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_announcement.v1",
                    "command_id": "retraction-before-gateway",
                    "procedure_run_id": "run-1",
                    "route": "retraction",
                    "retraction_command": 3,
                }
            )
        )
    )

    assert len(node._pending_execution_announcements) == 1
    node._on_gateway_info(_gateway(procedure_run_id="run-1", revision=2))

    assert len(node._pending_execution_announcements) == 0
    assert "리트랙션을 시작합니다." in [
        request.text for request in node._submitted_requests
    ]


def test_confirmed_record_completion_speaks_once_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    monkeypatch.setattr(node_module.time, "monotonic", lambda: 11.0)
    node._on_gateway_info(_gateway(active=False))
    node._submitted_requests.clear()
    payload = {
        "schema": "taskplanner.operational_surgery_record.status.v1",
        "request_id": "record-request-1",
        "procedure_run_id": "run-1",
        "submit_state": "SUCCEEDED",
        "success": True,
        "completed_at": "2026-08-29T16:00:00+09:00",
    }
    message = String(data=json.dumps(payload))

    node._on_surgery_record_status(message)
    node._on_surgery_record_status(message)

    assert [request.text for request in node._submitted_requests] == [
        "수술기록지를 생성하였습니다."
    ]


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


def test_duplicate_execution_acknowledgement_is_acked_without_second_playback() -> None:
    node = _node()
    submitted = []
    node._submit_request = submitted.append
    node._announcements.duplicate_voice_reply = True

    message = HumanoidReply()
    message.valid = True
    message.speak = True
    message.reply_id = "reply-duplicate"
    message.turn_id = "turn-duplicate"
    message.utterance_id = "utterance-duplicate"
    message.gateway_instance_id = "gateway-1"
    message.procedure_run_id = "run-1"
    message.text = "Mosquito 전달드리겠습니다."
    message.timing = "immediate"

    node._on_reply(message)

    assert submitted == []
    assert len(node._status_messages) == 1
    status = node._status_messages[0]
    assert status.reply_id == "reply-duplicate"
    assert status.state == "duplicate_suppressed"
    assert status.terminal is True
    assert status.success is True


def test_vlm_free_speech_disabled_is_acked_without_playback() -> None:
    node = _node()
    submitted = []
    node._submit_request = submitted.append
    node.get_parameter = lambda name: SimpleNamespace(
        value=False if name == "enable_vlm_free_speech" else None
    )

    message = HumanoidReply()
    message.valid = True
    message.speak = True
    message.reply_id = "reply-free-speech-disabled"
    message.turn_id = "turn-free-speech-disabled"
    message.utterance_id = "utterance-free-speech-disabled"
    message.gateway_instance_id = "gateway-1"
    message.procedure_run_id = "run-1"
    message.text = "수술장면이 보이지 않습니다."
    message.timing = "immediate"

    node._on_reply(message)

    assert submitted == []
    assert len(node._status_messages) == 1
    status = node._status_messages[0]
    assert status.reply_id == "reply-free-speech-disabled"
    assert status.state == "duplicate_suppressed"
    assert status.terminal is True
    assert status.success is True
    assert status.message == "suppressed_vlm_free_speech_disabled"


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
