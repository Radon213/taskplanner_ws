from __future__ import annotations

from concurrent.futures import Future
import threading
import time

import pytest
from bt_orchestrator.retractor_voice_interpreter import RetractionVoiceInterpretation
from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
)
from std_msgs.msg import String

from integration_debug.node import InputStats, IntegrationDebugNode


class _FakeRetractionClient:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def service_is_ready(self) -> bool:
        return self.ready


def _harness(
    *,
    armed: bool = True,
    generic_voice: bool = False,
    retraction_voice: bool = False,
    service_ready: bool = True,
    active_command_id: str = "",
    retraction_state: RetractionState = RetractionState.IDLE,
):
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._asr_topic = "/sensors/surgeon/sentence"
    harness._input_stats = {harness._asr_topic: InputStats()}
    harness._last_sentence = ""
    harness._last_voice_parse = {}
    harness._voice_auto_execute = generic_voice
    harness._last_voice_dispatch_text = ""
    harness._last_voice_dispatch_monotonic = 0.0
    harness._retraction_voice_auto_dispatch = retraction_voice
    harness._retraction_voice_generation = 0
    harness._retraction_state = retraction_state
    harness._last_retraction_interpretation = {}
    harness._last_retraction_rejection_reason = ""
    harness._last_retraction_voice_dispatch_text = ""
    harness._last_retraction_voice_dispatch_monotonic = 0.0
    harness._retraction_client = _FakeRetractionClient(service_ready)
    harness._armed = armed
    harness._fault_locked = False
    harness._active_command_id = active_command_id
    harness._config = {"voice": {}}
    harness.recorded_events: list[tuple[str, dict[str, object]]] = []
    harness.dispatched: list[tuple[str, dict[str, object], str]] = []
    harness._record = lambda event_type, payload: harness.recorded_events.append(
        (event_type, payload)
    )

    def dispatch(operation, payload, *, source):
        harness.dispatched.append((operation, payload, source))
        return True, "debug-command-1", "retraction Service request submitted"

    harness._dispatch_action = dispatch
    # Assigning the static serializer directly keeps this minimal harness free
    # of any ROS node or microphone runtime.
    harness._retraction_interpretation = IntegrationDebugNode._retraction_interpretation
    return harness


def _send_final(harness, text: str = "직접 교시 시작") -> None:
    IntegrationDebugNode._on_string_input(
        harness,
        harness._asr_topic,
        String(data=text),
    )


def test_retraction_voice_gate_dispatches_a_final_sentence_without_asr_ownership() -> None:
    harness = _harness(retraction_voice=True)

    _send_final(harness)

    assert harness.dispatched == [
        (
            "retraction_command",
            {
                "command": "start_direct_teach",
                "target_side": "none",
                "distance_m": 0.0,
            },
            "voice",
        )
    ]
    assert harness._last_retraction_interpretation == {
        "transcript": "직접 교시 시작",
        "command": "start_direct_teach",
        "target_side": "none",
        "distance_m": 0.0,
        "confidence": pytest.approx(0.98),
        "reason": "normalized_start_direct_teach",
        "interpreter_source": "shared_deterministic",
        "vlm_invoked": False,
        "detail": "deterministic_normalizer",
    }
    assert harness._last_retraction_rejection_reason == ""
    assert not hasattr(harness, "_asr")
    assert [name for name, _payload in harness.recorded_events] == [
        "sentence_received",
        "retraction_voice_interpretation",
        "retraction_voice_dispatch",
    ]


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"retraction_voice": False}, "voice_mode_buttons_only"),
        ({"armed": False, "retraction_voice": True}, "manual_control_not_armed"),
        (
            {"retraction_voice": True, "service_ready": False},
            "retraction_service_unavailable",
        ),
        (
            {"retraction_voice": True, "active_command_id": "busy-command"},
            "retraction_command_in_flight",
        ),
    ],
)
def test_retraction_voice_gate_reports_why_a_final_sentence_is_not_dispatched(
    settings: dict[str, object], reason: str
) -> None:
    harness = _harness(**settings)

    _send_final(harness)

    assert harness.dispatched == []
    assert harness._last_retraction_rejection_reason == reason
    assert harness._last_retraction_interpretation["command"] == "start_direct_teach"


def test_generic_voice_router_cannot_bypass_the_retraction_voice_gate() -> None:
    harness = _harness(generic_voice=True, retraction_voice=False)

    _send_final(harness)

    assert harness._last_voice_parse["operation"] == "retraction_command"
    assert harness.dispatched == []
    assert harness._last_retraction_rejection_reason == "voice_mode_buttons_only"


def test_debug_voice_mode_runs_text_vlm_asynchronously_before_dispatch() -> None:
    class FakeInterpreter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, RetractionState]] = []

        def interpret(
            self, transcript: str, current_state: RetractionState
        ) -> RetractionVoiceInterpretation:
            self.calls.append((transcript, current_state))
            return RetractionVoiceInterpretation(
                normalized=NormalizedRetractionCommand(
                    command=RetractionCommand.START_DIRECT_TEACH,
                    target_side=RetractionTargetSide.NONE,
                    distance_m=0.0,
                    confidence=0.80,
                    reason="normalized_text_vlm_grounded",
                ),
                interpreter_source="text_vlm",
                vlm_invoked=True,
                detail="text_vlm_normalized",
            )

    class InlineExecutor:
        @staticmethod
        def submit(function, *args) -> Future[RetractionVoiceInterpretation]:
            future: Future[RetractionVoiceInterpretation] = Future()
            future.set_result(function(*args))
            return future

    harness = _harness(retraction_voice=True)
    interpreter = FakeInterpreter()
    harness._retraction_voice_interpreter_mode = "vlm_with_fallback"
    harness._retraction_voice_interpreter = interpreter
    harness._retraction_voice_executor = InlineExecutor()
    harness._pending_retraction_voice_interpretation = None

    _send_final(harness)

    assert harness.dispatched == []
    assert harness._last_retraction_interpretation["interpreter_source"] == (
        "text_vlm_pending"
    )
    assert harness._last_retraction_interpretation["vlm_invoked"] is False
    assert harness._pending_retraction_voice_interpretation is not None

    IntegrationDebugNode._drain_retraction_voice_interpretation(harness)

    assert interpreter.calls == [
        ("직접 교시 시작", RetractionState.IDLE),
    ]
    assert harness._pending_retraction_voice_interpretation is None
    assert harness._last_retraction_interpretation["interpreter_source"] == (
        "text_vlm"
    )
    assert harness._last_retraction_interpretation["vlm_invoked"] is True
    assert harness.dispatched == [
        (
            "retraction_command",
            {
                "command": "start_direct_teach",
                "target_side": "none",
                "distance_m": 0.0,
            },
            "voice",
        )
    ]


def test_async_text_vlm_result_is_rejected_when_debug_state_changed() -> None:
    class FakeInterpreter:
        @staticmethod
        def interpret(
            _transcript: str, _current_state: RetractionState
        ) -> RetractionVoiceInterpretation:
            return RetractionVoiceInterpretation(
                normalized=NormalizedRetractionCommand(
                    command=RetractionCommand.START_DIRECT_TEACH,
                    target_side=RetractionTargetSide.NONE,
                    distance_m=0.0,
                    confidence=0.80,
                    reason="normalized_text_vlm_grounded",
                ),
                interpreter_source="text_vlm",
                vlm_invoked=True,
                detail="text_vlm_normalized",
            )

    class InlineExecutor:
        @staticmethod
        def submit(function, *args) -> Future[RetractionVoiceInterpretation]:
            future: Future[RetractionVoiceInterpretation] = Future()
            future.set_result(function(*args))
            return future

    harness = _harness(retraction_voice=True)
    harness._retraction_voice_interpreter_mode = "vlm_with_fallback"
    harness._retraction_voice_interpreter = FakeInterpreter()
    harness._retraction_voice_executor = InlineExecutor()
    harness._pending_retraction_voice_interpretation = None

    _send_final(harness)
    # START_DIRECT_TEACH is also valid from TAUGHT_READY, so merely checking
    # the returned command against the *new* allowed set would be insufficient.
    harness._retraction_state = RetractionState.TAUGHT_READY
    IntegrationDebugNode._drain_retraction_voice_interpretation(harness)

    assert harness.dispatched == []
    assert harness._last_retraction_rejection_reason == (
        "retraction_state_changed_while_interpreting"
    )


def test_async_text_vlm_result_cannot_survive_voice_mode_toggle_cycle() -> None:
    class FakeInterpreter:
        @staticmethod
        def interpret(
            _transcript: str, _current_state: RetractionState
        ) -> RetractionVoiceInterpretation:
            return RetractionVoiceInterpretation(
                normalized=NormalizedRetractionCommand(
                    command=RetractionCommand.START_DIRECT_TEACH,
                    target_side=RetractionTargetSide.NONE,
                    distance_m=0.0,
                    confidence=0.80,
                    reason="normalized_text_vlm_grounded",
                ),
                interpreter_source="text_vlm",
                vlm_invoked=True,
                detail="text_vlm_normalized",
            )

    class InlineExecutor:
        @staticmethod
        def submit(function, *args) -> Future[RetractionVoiceInterpretation]:
            future: Future[RetractionVoiceInterpretation] = Future()
            future.set_result(function(*args))
            return future

    harness = _harness(retraction_voice=True)
    harness._retraction_voice_interpreter_mode = "vlm_with_fallback"
    harness._retraction_voice_interpreter = FakeInterpreter()
    harness._retraction_voice_executor = InlineExecutor()
    harness._pending_retraction_voice_interpretation = None

    _send_final(harness)
    # Model a buttons-only -> voice-and-buttons cycle while the request is in
    # flight. The current boolean ends up enabled, but its old result is stale.
    harness._retraction_voice_generation += 2
    IntegrationDebugNode._drain_retraction_voice_interpretation(harness)

    assert harness.dispatched == []
    assert harness._last_retraction_rejection_reason == (
        "retraction_voice_authority_changed_while_interpreting"
    )


def test_retraction_voice_configuration_never_touches_microphone_runtime() -> None:
    class NoMicrophoneAccess:
        def __getattr__(self, _name):
            raise AssertionError("retraction voice configuration must not access ASR")

    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._asr = NoMicrophoneAccess()
    harness._retraction_voice_auto_dispatch = False
    harness._retraction_voice_generation = 0
    harness._manual_write_block_reason = lambda: ""

    accepted, _command_id, message = IntegrationDebugNode._configure_retraction_voice(
        harness, {"enabled": True}
    )

    assert accepted is True
    assert harness._retraction_voice_auto_dispatch is True
    assert "final-transcript" in message

    accepted, _command_id, message = IntegrationDebugNode._configure_retraction_voice(
        harness, {"enabled": "true"}
    )
    assert accepted is False
    assert message == "enabled must be a boolean"


def test_service_admission_advances_only_the_local_debug_state() -> None:
    class Harness:
        pass

    events: list[tuple[str, dict[str, object]]] = []
    harness = Harness()
    harness._lock = threading.RLock()
    harness._active_command_id = "debug-command-1"
    harness._active_route = "retraction_service"
    harness._active_goal_handle = None
    harness._retraction_state = RetractionState.TAUGHT_READY
    harness._last_retraction_rejection_reason = ""
    harness._action_status = {
        "command": "start_retraction",
        "source": "voice",
        "started_monotonic": time.monotonic() - 0.1,
    }
    harness._record = lambda event_type, payload: events.append((event_type, payload))

    IntegrationDebugNode._finish_retraction_service_admission(
        harness,
        "debug-command-1",
        request_accepted=True,
        result_code=0,
        state="accepted",
        reason_code="RESULT_ACCEPTED",
        response_message="accepted for controller admission",
    )

    assert harness._retraction_state is RetractionState.RETRACTION_ACTIVE
    assert harness._last_retraction_rejection_reason == ""
    assert harness._action_status["success"] is False
    assert harness._action_status["terminal"] is True
    assert events[0][0] == "retraction_service_response"


def test_direct_retraction_dispatch_rejects_an_out_of_order_command_before_transport() -> None:
    class FakeRetractionClient:
        def __init__(self) -> None:
            self.calls = 0

        def service_is_ready(self) -> bool:
            return True

        def call_async(self, _request):
            self.calls += 1
            raise AssertionError("an out-of-order command must not reach Service transport")

    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._manual_write_block_reason = lambda: ""
    harness._active_command_id = ""
    harness._armed = True
    harness._fault_locked = False
    harness._retraction_voice_auto_dispatch = False
    harness._retraction_state = RetractionState.IDLE
    harness._last_retraction_rejection_reason = ""
    harness._retraction_service_name = "/surgery/retraction/command"
    harness._retraction_client = FakeRetractionClient()

    accepted, command_id, message = IntegrationDebugNode._dispatch_action(
        harness,
        "retraction_command",
        {
            "command": "start_retraction",
            "target_side": "none",
            "distance_m": 0.0,
        },
        source="ui",
    )

    assert accepted is False
    assert command_id == ""
    assert "start_retraction is not allowed in Debug retraction state idle" == message
    assert harness._last_retraction_rejection_reason == (
        "retraction_command_not_allowed_in_debug_state"
    )
    assert harness._retraction_client.calls == 0


def test_concurrent_retraction_dispatch_reserves_only_one_service_request() -> None:
    class DeferredFuture:
        def add_done_callback(self, _callback) -> None:
            # Keep the request in flight so a competing stale UI/API request
            # must be rejected by the active-command reservation.
            return None

    class FakeRetractionClient:
        def __init__(self) -> None:
            self.calls = 0

        def service_is_ready(self) -> bool:
            return True

        def call_async(self, _request):
            self.calls += 1
            return DeferredFuture()

    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._manual_write_block_reason = lambda: ""
    harness._active_command_id = ""
    harness._armed = True
    harness._fault_locked = False
    harness._retraction_voice_auto_dispatch = False
    harness._active_route = ""
    harness._active_goal_handle = None
    harness._action_status = {}
    harness._retraction_state = RetractionState.IDLE
    harness._last_retraction_rejection_reason = ""
    harness._retraction_service_name = "/surgery/retraction/command"
    harness._retraction_client = FakeRetractionClient()
    harness._record = lambda *_args: None
    harness._build_retraction_service_request = (
        IntegrationDebugNode._build_retraction_service_request.__get__(harness)
    )
    harness._start_action_locked = IntegrationDebugNode._start_action_locked.__get__(
        harness
    )
    harness._on_retraction_service_response = lambda *_args: None

    barrier = threading.Barrier(2)
    results: list[tuple[bool, str, str]] = []
    results_lock = threading.Lock()

    def dispatch() -> None:
        barrier.wait()
        result = IntegrationDebugNode._dispatch_action(
            harness,
            "retraction_command",
            {
                "command": "start_direct_teach",
                "target_side": "none",
                "distance_m": 0.0,
            },
            source="ui",
        )
        with results_lock:
            results.append(result)

    first = threading.Thread(target=dispatch)
    second = threading.Thread(target=dispatch)
    first.start()
    second.start()
    first.join()
    second.join()

    assert sum(accepted for accepted, _command_id, _message in results) == 1
    assert any(message == "another command is active" for _, _, message in results)
    assert harness._retraction_client.calls == 1
    assert harness._active_route == "retraction_service"
    assert harness._action_status["command"] == "start_direct_teach"


def test_voice_dispatch_rechecks_arm_gate_before_reserving_service_call() -> None:
    class FakeRetractionClient:
        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def service_is_ready() -> bool:
            return True

        def call_async(self, _request):
            self.calls += 1
            raise AssertionError("disarmed voice request must not reach transport")

    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._armed = True
    harness._fault_locked = False
    harness._retraction_voice_auto_dispatch = True
    harness._active_command_id = ""
    harness._retraction_state = RetractionState.IDLE
    harness._last_retraction_rejection_reason = ""
    harness._retraction_service_name = "/surgery/retraction/command"
    harness._retraction_client = FakeRetractionClient()

    def disarm_during_initial_graph_check() -> str:
        harness._armed = False
        return ""

    harness._manual_write_block_reason = disarm_during_initial_graph_check

    accepted, command_id, message = IntegrationDebugNode._dispatch_action(
        harness,
        "retraction_command",
        {
            "command": "start_direct_teach",
            "target_side": "none",
            "distance_m": 0.0,
        },
        source="voice",
    )

    assert (accepted, command_id, message) == (
        False,
        "",
        "manual control is not armed",
    )
    assert harness._retraction_client.calls == 0


def test_service_submit_exception_releases_slot_without_changing_state() -> None:
    class FakeRetractionClient:
        @staticmethod
        def service_is_ready() -> bool:
            return True

        @staticmethod
        def call_async(_request):
            raise RuntimeError("client context closed")

    class Harness:
        pass

    events: list[tuple[str, dict[str, object]]] = []
    harness = Harness()
    harness._lock = threading.RLock()
    harness._armed = True
    harness._fault_locked = False
    harness._retraction_voice_auto_dispatch = False
    harness._manual_write_block_reason = lambda: ""
    harness._active_command_id = ""
    harness._active_route = ""
    harness._active_goal_handle = None
    harness._action_status = {}
    harness._retraction_state = RetractionState.IDLE
    harness._last_retraction_rejection_reason = ""
    harness._retraction_service_name = "/surgery/retraction/command"
    harness._retraction_client = FakeRetractionClient()
    harness._record = lambda event_type, payload: events.append((event_type, payload))
    harness._build_retraction_service_request = (
        IntegrationDebugNode._build_retraction_service_request.__get__(harness)
    )
    harness._start_action_locked = IntegrationDebugNode._start_action_locked.__get__(
        harness
    )

    accepted, command_id, message = IntegrationDebugNode._dispatch_action(
        harness,
        "retraction_command",
        {
            "command": "start_direct_teach",
            "target_side": "none",
            "distance_m": 0.0,
        },
        source="ui",
    )

    assert accepted is False
    assert command_id == ""
    assert "service_submit_error:RuntimeError" in message
    assert harness._active_command_id == ""
    assert harness._active_route == ""
    assert harness._retraction_state is RetractionState.IDLE
    assert harness._action_status["terminal"] is True
    assert harness._action_status["request_accepted"] is False
    assert harness._action_status["success"] is False
    assert [event_type for event_type, _payload in events] == [
        "command_started",
        "retraction_service_submit_failed",
    ]


def test_retraction_service_recovery_resets_debug_state_after_confirmed_remote_check() -> None:
    class Harness:
        pass

    events: list[tuple[str, dict[str, object]]] = []
    harness = Harness()
    harness._lock = threading.RLock()
    harness._active_command_id = "debug-command-uncertain"
    harness._active_route = "retraction_service"
    harness._active_goal_handle = object()
    harness._retraction_state = RetractionState.UNKNOWN
    harness._last_retraction_rejection_reason = "service_response_timeout"
    harness._fault_locked = True
    harness._last_error = "retraction Service request acceptance is uncertain"
    harness._action_status = {
        "state": "remote_state_unknown",
        "reason_code": "service_response_timeout",
        "started_monotonic": time.monotonic() - 0.1,
        "recovery_required": True,
    }
    harness._disarm_locked = lambda: None
    harness._idle_action_status = lambda: {"state": "idle", "terminal": True}
    harness._release_manual_publishers = lambda: None
    harness._record = lambda event_type, payload: events.append((event_type, payload))

    accepted, command_id, message = IntegrationDebugNode._recover_command_client(
        harness,
        {
            "expected_command_id": "debug-command-uncertain",
            "remote_motion_stopped_confirmed": True,
        },
    )

    assert accepted is True
    assert command_id == "debug-command-uncertain"
    assert message == (
        "retraction Service client recovered to Debug idle; manual control remains disarmed"
    )
    assert harness._retraction_state is RetractionState.IDLE
    assert harness._last_retraction_rejection_reason == ""
    assert harness._active_command_id == ""
    assert events == [
        (
            "command_client_recovered",
            {
                "route": "retraction_service",
                "command_id": "debug-command-uncertain",
                "previous_state": "remote_state_unknown",
                "previous_reason_code": "service_response_timeout",
                "elapsed_sec": pytest.approx(0.1, abs=0.1),
                "remote_motion_stopped_confirmed": True,
                "retraction_state_reset": "idle",
            },
        )
    ]
