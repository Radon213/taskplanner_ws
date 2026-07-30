from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import sys

from tools.real_surgery_annotation.compare_shadow_determinism import (
    semantic_evaluation_signature,
    semantic_trace_signature,
)


class ShadowDeterminismTest(unittest.TestCase):
    def test_cli_can_be_executed_directly(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        script = (
            repository_root
            / "tools"
            / "real_surgery_annotation"
            / "compare_shadow_determinism.py"
        )
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--run-a", result.stdout)

    def test_repeated_ticks_and_dynamic_ids_do_not_change_signature(self) -> None:
        base = {
            "layer": "skill_command",
            "payload": {
                "command_id": "one",
                "action": "pick_up_and_handover",
                "instrument_id": "T01",
                "arm": "right",
            },
        }
        duplicate = {
            "layer": "skill_command",
            "payload": {**base["payload"], "command_id": "two"},
        }
        first = semantic_trace_signature([base])
        second = semantic_trace_signature([base, duplicate])
        self.assertEqual(first, second)

    def test_terminal_shutdown_health_flag_is_ignored(self) -> None:
        base = {
            "layer": "reducer_fused",
            "payload": {
                "running": False,
                "execution_state": "halted",
                "safety_flags": [],
                "instrument_states": [],
            },
        }
        teardown = {
            "layer": "reducer_fused",
            "payload": {
                **base["payload"],
                "safety_flags": ["vlm_unhealthy"],
            },
        }
        self.assertEqual(
            semantic_trace_signature([base]),
            semantic_trace_signature([teardown]),
        )

    def test_running_health_flag_remains_semantic(self) -> None:
        base = {
            "layer": "reducer_fused",
            "payload": {
                "running": True,
                "execution_state": "running",
                "safety_flags": [],
                "instrument_states": [],
            },
        }
        unsafe = {
            "layer": "reducer_fused",
            "payload": {
                **base["payload"],
                "safety_flags": ["vlm_unhealthy"],
            },
        }
        self.assertNotEqual(
            semantic_trace_signature([base]),
            semantic_trace_signature([unsafe]),
        )

    def test_runtime_latency_is_excluded_but_safety_counts_are_not(self) -> None:
        report = {
            "mode": "strict",
            "status": "complete",
            "confirmed_handover_count": 1,
            "layers": {},
            "phase": {"status": "not_available"},
            "runtime": {
                "vlm_latency_sec": {"median": 1.0},
                "input_image_count": 10,
                "input_transcript_count": 1,
                "vlm_result_count": 5,
                "vlm_unhealthy_count": 0,
                "vlm_parse_retry_count": 0,
                "skill_command_after_completion_count": 0,
                "trace_contract_error_count": 0,
            },
        }
        changed = {
            **report,
            "runtime": {
                **report["runtime"],
                "vlm_latency_sec": {"median": 9.0},
            },
        }
        self.assertEqual(
            semantic_evaluation_signature(report),
            semantic_evaluation_signature(changed),
        )
        changed["runtime"]["vlm_unhealthy_count"] = 1
        self.assertNotEqual(
            semantic_evaluation_signature(report),
            semantic_evaluation_signature(changed),
        )


if __name__ == "__main__":
    unittest.main()
