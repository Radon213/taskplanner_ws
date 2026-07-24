from __future__ import annotations

from pathlib import Path
import unittest

from or_digital_twin.models import LIFECYCLE_DROPPED_FLOOR, LIFECYCLE_RETURNED_HOME
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import SurgeonActorEvent, SurgeonRequest


SPEC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "procedure_spec"
    / "procedure_spec"
    / "specs"
)


class InterruptPreemptionTest(unittest.TestCase):
    def test_priority_tool_comes_from_each_procedure_bundle(self) -> None:
        cases = {
            "thyroidectomy": ("P06", "T10"),
            "nephrectomy": ("P07", "T10"),
            "inguinal_hernia_repair": ("P07", "T03"),
        }
        for bundle_name, (interrupt_phase, expected_tool) in cases.items():
            with self.subTest(bundle=bundle_name):
                twin = ORDigitalTwin(load_bundle(SPEC_ROOT / bundle_name))
                twin._approve_phase_transition(
                    interrupt_phase,
                    reason="manual interrupt test",
                    confidence=0.99,
                    cue_id="test_interrupt",
                )
                self.assertEqual(twin.state.filtered_phase, interrupt_phase)
                self.assertEqual(twin.state.surgeon_request_tool, expected_tool)
                self.assertEqual(
                    [cue.instrument_id for cue in twin.state.surgeon_request_queue],
                    [expected_tool],
                )

    def test_cancel_request_clears_active_and_queued_requests(self) -> None:
        twin = ORDigitalTwin(load_bundle(SPEC_ROOT / "thyroidectomy"))
        for tool_id in ("T04", "T10"):
            request = SurgeonRequest()
            request.event_type = "voice_request"
            request.requested_tool = tool_id
            request.ready_for_handover = True
            twin.update_surgeon_request(request)

        cancel = SurgeonRequest()
        cancel.event_type = "cancel_request"
        cancel.override = True
        twin.update_surgeon_request(cancel)

        self.assertEqual(twin.state.surgeon_request_tool, "")
        self.assertEqual(list(twin.state.surgeon_request_queue), [])
        self.assertEqual(twin.state.surgeon_intent, "idle")

    def test_floor_drop_blocks_until_human_recovery(self) -> None:
        twin = ORDigitalTwin(load_bundle(SPEC_ROOT / "thyroidectomy"))
        tool_id = twin.spec.list_instrument_ids()[0]
        state = twin.instrument_states[tool_id]
        twin._set_lifecycle(state, LIFECYCLE_DROPPED_FLOOR, confidence=0.99)
        twin._recompute_transient_state()

        self.assertIn("dropped_tool_requires_human", twin.state.safety_flags)
        self.assertEqual(state.next_required_transition, "human_recovery_required")

        event = SurgeonActorEvent()
        event.event_type = "human_recovered_dropped_tool"
        event.tool_id = tool_id
        event.note = "human removed tool and supplied sterile replacement"
        twin.apply_surgeon_actor_event(event)

        self.assertEqual(state.lifecycle_stage, LIFECYCLE_RETURNED_HOME)
        self.assertNotIn("dropped_tool_requires_human", twin.state.safety_flags)


if __name__ == "__main__":
    unittest.main()
