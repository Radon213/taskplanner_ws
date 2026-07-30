from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from tools.real_surgery_annotation.merge_candidate_proposals import (
    CandidateMergeError,
    main,
    merge_candidate_files,
    write_new_outputs,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    ROOT
    / "annotations/observable_tool_events/schema/"
    "observable_tool_event.v1.schema.json"
)
TOOLS_PATH = ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
TOOLS = yaml.safe_load(TOOLS_PATH.read_text(encoding="utf-8"))


def proposal(event_id: str, time_sec: float) -> dict:
    suffix = event_id.rsplit("P", 1)[-1]
    return {
        "schema": "taskplanner.observable_tool_event.v1",
        "case_id": "0704_5",
        "event_id": event_id,
        "event_type": "tool_transfer",
        "time_sec": time_sec,
        "candidate_start_sec": max(0.0, time_sec - 1.0),
        "candidate_end_sec": time_sec,
        "tool": {
            "id": f"unknown_tool_{suffix}",
            "name": "Unidentified surgical instrument",
            "instance_id": f"0704_5-tool-fixture_{suffix}",
        },
        "from": {"holder": "unknown", "location": "unknown"},
        "to": {"holder": "unknown", "location": "unknown"},
        "derived_action": "relocate",
        "source_views": ["cam4", "flir"],
        "visibility": "partial",
        "review_status": "proposed",
        "label_origin": "temporal_grounding_model",
        "proposal": {
            "generator": "fixture",
            "model_version": "fixture-v1",
        },
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class CandidateProposalMergeTest(unittest.TestCase):
    def merge(self, paths: list[Path]) -> tuple[list[dict], dict]:
        return merge_candidate_files(
            input_paths=paths,
            schema=SCHEMA,
            tool_catalog=TOOLS,
            case_id="0704_5",
            duration_sec=163.1,
        )

    def test_merges_multiple_inputs_in_deterministic_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            write_jsonl(first, [proposal("0704_5-P1002", 12.0)])
            write_jsonl(
                second,
                [
                    proposal("0704_5-P1001", 5.0),
                    proposal("0704_5-P1003", 12.0),
                ],
            )

            records, report = self.merge([first, second])

            self.assertEqual(
                ["0704_5-P1001", "0704_5-P1002", "0704_5-P1003"],
                [record["event_id"] for record in records],
            )
            self.assertEqual(3, report["merged_candidate_count"])
            self.assertEqual(0, report["ground_truth_event_count"])
            self.assertTrue(report["human_review_required"])
            self.assertTrue(report["all_candidates_remain_proposed"])

    def test_identical_duplicate_event_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            record = proposal("0704_5-P1001", 5.0)
            write_jsonl(first, [record])
            write_jsonl(second, [record])

            with self.assertRaisesRegex(
                CandidateMergeError, r"duplicate event_id '0704_5-P1001'"
            ):
                self.merge([first, second])

    def test_conflicting_duplicate_event_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            original = proposal("0704_5-P1001", 5.0)
            changed = copy.deepcopy(original)
            changed["time_sec"] = 6.0
            changed["candidate_end_sec"] = 6.0
            write_jsonl(first, [original])
            write_jsonl(second, [changed])

            with self.assertRaisesRegex(
                CandidateMergeError, r"conflicting event_id '0704_5-P1001'"
            ):
                self.merge([first, second])

    def test_schema_and_tool_catalog_validation_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.jsonl"
            record = proposal("0704_5-P1001", 5.0)
            record["tool"] = {
                "id": "invented_tool",
                "name": "Invented tool",
                "instance_id": "0704_5-tool-invented",
            }
            write_jsonl(path, [record])

            with self.assertRaisesRegex(
                CandidateMergeError, r"tool.id 'invented_tool' is not canonical"
            ):
                self.merge([path])

    def test_non_proposed_record_is_rejected_instead_of_becoming_gt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "confirmed.jsonl"
            record = proposal("0704_5-P1001", 5.0)
            record["review_status"] = "ambiguous"
            write_jsonl(path, [record])

            with self.assertRaisesRegex(
                CandidateMergeError, r"review_status must be 'proposed'"
            ):
                self.merge([path])

    def test_writer_refuses_to_overwrite_either_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [proposal("0704_5-P1001", 5.0)]
            report = {
                "ground_truth_event_count": 0,
                "human_review_required": True,
            }
            for existing_name in ("output.jsonl", "report.json"):
                with self.subTest(existing_name=existing_name):
                    output = root / f"{existing_name}.output.jsonl"
                    report_path = root / f"{existing_name}.report.json"
                    existing = output if existing_name == "output.jsonl" else report_path
                    existing.write_text("preserve-me\n", encoding="utf-8")

                    with self.assertRaisesRegex(
                        FileExistsError, "refusing to overwrite"
                    ):
                        write_new_outputs(
                            output_path=output,
                            report_path=report_path,
                            records=records,
                            report=report,
                        )

                    self.assertEqual(
                        "preserve-me\n", existing.read_text(encoding="utf-8")
                    )
                    other = report_path if existing == output else output
                    self.assertFalse(other.exists())

    def test_cli_writes_jsonl_and_report_without_touching_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            output = root / "merged.jsonl"
            report_path = root / "merge-report.json"
            write_jsonl(first, [proposal("0704_5-P1002", 9.0)])
            write_jsonl(second, [proposal("0704_5-P1001", 3.0)])
            first_before = first.read_bytes()
            second_before = second.read_bytes()

            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(
                    [
                        "--input",
                        str(first),
                        "--input",
                        str(second),
                        "--case-id",
                        "0704_5",
                        "--duration-sec",
                        "163.1",
                        "--schema",
                        str(SCHEMA_PATH),
                        "--tools",
                        str(TOOLS_PATH),
                        "--output",
                        str(output),
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, return_code, stderr.getvalue())
            self.assertEqual(first_before, first.read_bytes())
            self.assertEqual(second_before, second.read_bytes())
            merged_ids = [
                json.loads(line)["event_id"]
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(["0704_5-P1001", "0704_5-P1002"], merged_ids)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(0, report["ground_truth_event_count"])
            self.assertTrue(report["human_review_required"])
            self.assertIn('"ok": true', stdout.getvalue())
            self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
