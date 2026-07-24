from __future__ import annotations

from pathlib import Path
import unittest

from procedure_spec import load_bundle
from vlm_node.real_vlm import RealVLMNode


SPEC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "procedure_spec"
    / "procedure_spec"
    / "specs"
)


class RealVLMFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.node = object.__new__(RealVLMNode)
        self.node._spec = load_bundle(SPEC_ROOT / "thyroidectomy")

    def test_unknown_phase_and_tool_ids_are_removed(self) -> None:
        payload = {
            "v": "3",
            "phase": [["P01,", 0.9], ["P99", 0.8]],
            "tool": [["T01.", 0.85], ["bone_saw", 0.75]],
            "intent": ["request", "T04", 0.7],
            "mayo": [["T02", "reuse", 0.6], ["bone_saw", "recover", 0.9]],
            "mayo_retrieve": ["bone_saw", 0.9],
            "u": 0.2,
            "sum": "test",
        }

        canonical = self.node._canonicalize_payload_ids(payload)

        self.assertEqual(canonical["phase"], [["P01", 0.9]])
        self.assertEqual(canonical["tool"], [["T01", 0.85]])
        self.assertEqual(canonical["intent"], ["request_tool", "T04", 0.7])
        self.assertEqual(canonical["mayo"], [["T02", "reuse", 0.6]])
        self.assertEqual(canonical["mayo_retrieve"], ["", 0.0])

    def test_non_mayo_lifecycles_cannot_be_recovery_candidates(self) -> None:
        payload = {
            "mayo": [
                ["T01", "recover", 0.8],
                ["T02", "recover", 0.75],
                ["T03", "reuse", 0.6],
            ],
            "mayo_retrieve": ["T01", 0.8],
        }
        context = {
            "digital_twin": {
                "hands": {"rh": "T01", "lh": "", "pre": ""},
                "tools": [
                    {"id": "T02", "lc": "cleaning_left", "lt": "cleaner_slot"},
                    {"id": "T03", "lc": "mayo_reuse", "lt": "mayo_reuse_zone"},
                ],
            }
        }

        self.node._suppress_non_mayo_recovery_candidates(payload, context)

        self.assertEqual(payload["mayo"], [["T03", "reuse", 0.6]])
        self.assertEqual(payload["mayo_retrieve"], ["", 0.0])

    def test_intent_with_unknown_tool_is_cleared(self) -> None:
        payload = {
            "v": "3",
            "phase": [["P01", 0.9]],
            "tool": [],
            "intent": ["handover", "bone_saw", 0.95],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "u": 0.3,
            "sum": "test",
        }

        canonical = self.node._canonicalize_payload_ids(payload)

        self.assertEqual(canonical["intent"], ["none", "", 0.0])


if __name__ == "__main__":
    unittest.main()
