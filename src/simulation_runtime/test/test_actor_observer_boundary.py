from __future__ import annotations

import inspect

from simulation_runtime.mock_surgeon import MockSurgeonNode
from simulation_runtime.llm_surgeon_actor import LLMSurgeonActorNode
from simulation_runtime.surgeon_actor import SurgeonActorNode


def test_validation_actors_do_not_subscribe_to_vlm_gesture_evidence() -> None:
    rule_source = inspect.getsource(SurgeonActorNode)
    scripted_source = inspect.getsource(MockSurgeonNode)

    for source in (rule_source, scripted_source):
        assert "/vlm/surgeon_gesture_evidence" not in source
        assert "_on_gesture_evidence" not in source


def test_validation_actors_do_not_turn_vlm_evidence_into_requests() -> None:
    rule_source = inspect.getsource(SurgeonActorNode)
    scripted_source = inspect.getsource(MockSurgeonNode)

    for source in (rule_source, scripted_source):
        assert "_stable_gesture_request" not in source
        assert "_effective_vlm_state" not in source
        assert "_publish_vlm_request_transition" not in source


def test_llm_actor_uses_execution_feedback_not_observer_or_twin_state() -> None:
    source = inspect.getsource(LLMSurgeonActorNode)

    assert '"/skill/status"' in source
    assert '"/vlm/' not in source
    assert '"/twin/' not in source
    assert '"/simulation/state"' not in source
