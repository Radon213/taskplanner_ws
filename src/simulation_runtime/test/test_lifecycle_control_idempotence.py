from types import SimpleNamespace

from simulation_runtime.llm_surgeon_actor import LLMSurgeonActorNode
from simulation_runtime.mock_surgeon import MockSurgeonNode
from simulation_runtime.speech_input_adapter import SpeechInputAdapterNode
from simulation_runtime.surgeon_actor import SurgeonActorNode


class _ClearCounter:
    def __init__(self) -> None:
        self.count = 0

    def clear(self) -> None:
        self.count += 1


def test_speech_start_heartbeat_is_idempotent_and_reset_reopens_start() -> None:
    node = SpeechInputAdapterNode.__new__(SpeechInputAdapterNode)
    node._last_lifecycle_control_signature = None
    node._lifecycle_control_state = "stopped"
    node._recent_ids = _ClearCounter()
    node._recent_sentences = _ClearCounter()
    node._epoch = 0
    node._received_count = 9
    node._accepted_count = 8
    node._rejected_count = 1
    node._last_source = "test"
    node._last_observation_stamp = object()
    node._last_accepted_monotonic = 1.0
    node._last_detail = "active"
    node._waiting_detail = lambda: "waiting"
    node._publish_status = lambda: None

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert node._recent_ids.count == 4
    assert node._recent_sentences.count == 4
    assert node._epoch == 2
    assert node._lifecycle_control_state == "running"


def test_rule_actor_reset_is_repeatable_and_reopens_start() -> None:
    node = SurgeonActorNode.__new__(SurgeonActorNode)
    node._last_lifecycle_control_signature = None
    node._active = False
    node._phase_hint = None
    node._current_phase_id = "P01"
    node._spec = SimpleNamespace(default_phase_id="P01")
    node._coerce_phase_id = lambda phase: phase
    clock_calls: list[bool] = []
    node._current_time_sec = lambda: clock_calls.append(True) or 1.0
    node._manual_override_mute_until_sec = 0.0
    node._world = None
    node._active_voice_text = ""
    node._voice_hold_ticks = 0
    node._override_queue = _ClearCounter()
    reset_calls: list[bool] = []
    node._clear_active_override = lambda: reset_calls.append(True)
    node._published_request_signature = ""
    node._published_actor_signature = ""
    node._cancel_cooldown_ticks = 0
    node._last_requested_tool_sec = {}
    node._last_lifecycle_by_tool = {}
    node._surgeon_tool_received_sec = {}

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert len(reset_calls) == 2
    assert len(clock_calls) == 4
    assert node._active is True


def test_llm_actor_reset_is_repeatable_and_reopens_start() -> None:
    node = LLMSurgeonActorNode.__new__(LLMSurgeonActorNode)
    node._last_lifecycle_control_signature = None
    node._control_running = False
    node._active = False
    node._enabled = True
    node._current_phase_id = "P01"
    node._phase_used_tools = set()
    node._manual_override_mute_until_sec = 0.0
    node._now = lambda: 1.0
    interrupt_calls: list[bool] = []
    node._clear_interrupt_state = lambda **_kwargs: interrupt_calls.append(True)
    node._schedule_interrupt_for_phase = lambda *_args: None
    decision_calls: list[float] = []
    node._schedule_next_decision = decision_calls.append
    reset_calls: list[str] = []

    def reset_runtime(start_phase_id: str = "") -> None:
        reset_calls.append(start_phase_id)
        node._control_running = False
        node._active = False

    node._reset_runtime = reset_runtime

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert reset_calls == ["", ""]
    assert len(interrupt_calls) == 2
    assert decision_calls == [0.2, 0.2]
    assert node._control_running is True


def test_mock_actor_reset_is_repeatable_and_reopens_start() -> None:
    node = MockSurgeonNode.__new__(MockSurgeonNode)
    node._last_lifecycle_control_signature = None
    node._active = False
    node._current_phase_id = "P01"
    node._tick = 0
    node._last_stage_name = ""
    node._active_voice_text = ""
    node._voice_hold_ticks = 0
    node._override_queue = _ClearCounter()
    node._clear_active_override = lambda: None
    schedule_calls: list[bool] = []
    node._schedule_next_random_voice = lambda: schedule_calls.append(True)

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert schedule_calls == [True, True]
    assert node._active is True
