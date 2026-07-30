from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.interaction_review_gui import sha256_value
from tools.real_surgery_annotation.materialize_human_review_decisions import (
    MaterializationError,
    materialize,
)


ROOT = Path(__file__).resolve().parents[2]
POINT_SCHEMA = (
    ROOT
    / "annotations/observable_tool_events/schema/"
    "observable_interaction_point.v1.schema.json"
)


class MaterializeHumanReviewDecisionsTest(unittest.TestCase):
    def make_fixture(self, root: Path) -> dict[str, Path]:
        timeline = root / "timeline.json"
        tools = root / "tools.yaml"
        candidates = root / "candidates.jsonl"
        decisions = root / "decisions.jsonl"
        output = root / "reviewed.jsonl"
        report = root / "report.json"
        timeline.write_text(
            json.dumps(
                {
                    "schema": "taskplanner.video_frame_timeline.v1",
                    "case_id": "0704_6",
                    "frame_count": 3,
                    "source_fps": 10.0,
                    "start_sec": 0.0,
                    "end_sec": 0.2,
                    "timestamps_sec": [0.0, 0.1, 0.2],
                    "gaps": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        tools.write_text(
            "tools:\n"
            "  - id: adson_forceps\n"
            "  - id: bovie\n",
            encoding="utf-8",
        )
        self.write_jsonl(candidates, self.candidate_records())
        return {
            "timeline": timeline,
            "tools": tools,
            "candidates": candidates,
            "decisions": decisions,
            "output": output,
            "report": report,
        }

    @staticmethod
    def candidate_records() -> list[dict]:
        return [
            {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-R0001",
                "event_type": "implicit_tool_request",
                "time_sec": 0.0,
                "source_frame_idx": 0,
                "source_views": ["cam4"],
                "review_status": "proposed",
                "label_origin": "assistant_visual_proposal",
                "proposal": {
                    "generator": "fixture-generator",
                    "query": "visible open hand",
                },
                "ai_review": {
                    "reviewer_model": "gpt-5.6-sol",
                    "decision": "recommend",
                    "reviewed_at": "2026-07-28T00:00:00+00:00",
                    "evidence": "fixture request evidence",
                },
            },
            {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-T0001",
                "event_type": "tool_transfer",
                "time_sec": 0.1,
                "source_frame_idx": 1,
                "source_views": ["cam4", "flir"],
                "tool": "adson_forceps",
                "from": "scrub_nurse",
                "to": "surgeon",
                "review_status": "proposed",
                "label_origin": "assistant_visual_proposal",
                "proposal": {"generator": "fixture-generator"},
                "ai_review": {
                    "reviewer_model": "gpt-5.6-sol",
                    "decision": "uncertain",
                    "reviewed_at": "2026-07-28T00:00:01+00:00",
                    "evidence": "fixture transfer evidence",
                },
            },
        ]

    @staticmethod
    def write_jsonl(path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    @staticmethod
    def decision(
        candidate: dict,
        *,
        decision_id: str,
        status: str,
        fields: dict,
    ) -> dict:
        review = {
            "reviewer_kind": "human",
            "reviewer_id": "fixture-reviewer",
            "reviewed_at": "2026-07-28T01:00:00+00:00",
            "notes": f"{status} fixture",
        }
        candidate_digest = sha256_value(candidate)
        request = {
            "candidate_id": candidate["event_id"],
            "candidate_sha256": candidate_digest,
            "review_status": status,
            "reviewer_id": review["reviewer_id"],
            "notes": review["notes"],
            "adjudicated_fields": fields,
        }
        return {
            "schema": "taskplanner.human_review_decision.v1",
            "case_id": "0704_6",
            "decision_id": decision_id,
            "candidate_id": candidate["event_id"],
            "candidate_sha256": candidate_digest,
            "request_sha256": sha256_value(request),
            "review_status": status,
            "resulting_label_origin": (
                "human_video_review" if status == "confirmed" else None
            ),
            "adjudicated_fields": fields,
            "review": review,
        }

    def decisions(self) -> list[dict]:
        request, transfer = self.candidate_records()
        return [
            self.decision(
                transfer,
                decision_id="0704_6-H0001",
                status="ambiguous",
                fields={
                    "event_type": "tool_transfer",
                    "source_frame_idx": 1,
                    "time_sec": 0.1,
                    "tool": "adson_forceps",
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "phase_id": None,
                    "source_views": ["cam4", "flir"],
                },
            ),
            self.decision(
                request,
                decision_id="0704_6-H0002",
                status="confirmed",
                fields={
                    "event_type": "tool_transfer",
                    "source_frame_idx": 2,
                    "time_sec": 0.2,
                    "tool": "bovie",
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "phase_id": None,
                    "source_views": ["cam4", "flir"],
                },
            ),
        ]

    def run_materializer(
        self,
        paths: dict[str, Path],
        *,
        require_all: bool,
    ) -> dict:
        return materialize(
            candidates_path=paths["candidates"],
            decisions_path=paths["decisions"],
            schema_path=POINT_SCHEMA,
            timeline_path=paths["timeline"],
            tools_path=paths["tools"],
            case_id="0704_6",
            stream_kind="interaction",
            output_path=paths["output"],
            report_path=paths["report"],
            require_all=require_all,
        )

    def test_success_preserves_evidence_authority_and_reids_changed_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            self.write_jsonl(paths["decisions"], self.decisions())
            summary = self.run_materializer(paths, require_all=True)

            output = [
                json.loads(line)
                for line in paths["output"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(2, len(output))
            ambiguous, confirmed = output
            self.assertEqual("0704_6-T0001", ambiguous["event_id"])
            self.assertEqual(
                "assistant_visual_proposal",
                ambiguous["label_origin"],
            )
            self.assertEqual("human", ambiguous["review"]["reviewer_kind"])
            self.assertEqual(
                "fixture transfer evidence",
                ambiguous["ai_review"]["evidence"],
            )
            self.assertEqual("fixture-generator", ambiguous["proposal"]["generator"])

            # T0001 is already used by the unchanged candidate, so the request
            # changed to tool_transfer deterministically receives T0002.
            self.assertEqual("0704_6-T0002", confirmed["event_id"])
            self.assertEqual("human_video_review", confirmed["label_origin"])
            self.assertEqual(0.2, confirmed["time_sec"])
            self.assertEqual("bovie", confirmed["tool"])
            self.assertEqual("fixture request evidence", confirmed["ai_review"]["evidence"])

            report = json.loads(paths["report"].read_text(encoding="utf-8"))
            self.assertEqual(2, report["counts"]["materialized_count"])
            self.assertEqual(
                {"ambiguous": 1, "confirmed": 1},
                report["counts"]["review_status"],
            )
            self.assertEqual(
                hashlib.sha256(paths["output"].read_bytes()).hexdigest(),
                report["output"]["sha256"],
            )
            changed = {
                item["candidate_id"]: item
                for item in report["event_id_mappings"]
            }["0704_6-R0001"]
            self.assertTrue(changed["event_type_changed"])
            self.assertEqual("0704_6-T0002", changed["event_id"])
            self.assertEqual(2, summary["record_count"])

    def test_partial_materialization_reports_unreviewed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            self.write_jsonl(paths["decisions"], self.decisions()[:1])
            summary = self.run_materializer(paths, require_all=False)
            self.assertEqual(1, summary["record_count"])
            self.assertEqual(1, summary["unreviewed_count"])
            report = json.loads(paths["report"].read_text(encoding="utf-8"))
            self.assertEqual(["0704_6-R0001"], report["unreviewed_candidate_ids"])

    def test_require_all_rejects_partial_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            self.write_jsonl(paths["decisions"], self.decisions()[:1])
            with self.assertRaisesRegex(MaterializationError, "--require-all"):
                self.run_materializer(paths, require_all=True)
            self.assertFalse(paths["output"].exists())
            self.assertFalse(paths["report"].exists())

    def test_candidate_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            decisions = self.decisions()
            decisions[0]["candidate_sha256"] = "0" * 64
            self.write_jsonl(paths["decisions"], decisions)
            with self.assertRaisesRegex(MaterializationError, "digest mismatch"):
                self.run_materializer(paths, require_all=False)
            self.assertFalse(paths["output"].exists())

    def test_duplicate_candidate_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            decisions = self.decisions()
            duplicate = dict(decisions[0])
            duplicate["decision_id"] = "0704_6-H9999"
            self.write_jsonl(paths["decisions"], [decisions[0], duplicate])
            with self.assertRaisesRegex(MaterializationError, "중복 판정"):
                self.run_materializer(paths, require_all=False)
            self.assertFalse(paths["output"].exists())

    def test_create_only_refuses_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            self.write_jsonl(paths["decisions"], self.decisions())
            paths["output"].write_text("sentinel\n", encoding="utf-8")
            with self.assertRaisesRegex(MaterializationError, "refusing to overwrite"):
                self.run_materializer(paths, require_all=True)
            self.assertEqual("sentinel\n", paths["output"].read_text(encoding="utf-8"))
            self.assertFalse(paths["report"].exists())

    def test_phase_materialization_preserves_boundary_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            phase_candidate = {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-PH0001",
                "event_type": "phase_start",
                "phase_id": "P13",
                "phase_boundary_kind": "clip_initial_state",
                "time_sec": 0.0,
                "source_frame_idx": 0,
                "source_views": ["cam4", "flir"],
                "review_status": "proposed",
                "label_origin": "assistant_visual_proposal",
                "ai_review": {
                    "reviewer_model": "gpt-5.6-sol",
                    "decision": "recommend",
                    "reviewed_at": "2026-07-28T00:00:00+00:00",
                    "evidence": "left-censored clip initial state",
                },
            }
            self.write_jsonl(paths["candidates"], [phase_candidate])
            decision = self.decision(
                phase_candidate,
                decision_id="0704_6-H0001",
                status="confirmed",
                fields={
                    "event_type": "phase_start",
                    "source_frame_idx": 0,
                    "time_sec": 0.0,
                    "tool": None,
                    "from": None,
                    "to": None,
                    "phase_id": "P13",
                    "source_views": ["cam4", "flir"],
                },
            )
            self.write_jsonl(paths["decisions"], [decision])

            summary = materialize(
                candidates_path=paths["candidates"],
                decisions_path=paths["decisions"],
                schema_path=POINT_SCHEMA,
                timeline_path=paths["timeline"],
                tools_path=paths["tools"],
                case_id="0704_6",
                stream_kind="phase",
                output_path=paths["output"],
                report_path=paths["report"],
                require_all=True,
            )

            output = json.loads(
                paths["output"].read_text(encoding="utf-8").strip()
            )
            self.assertEqual("clip_initial_state", output["phase_boundary_kind"])
            self.assertEqual("P13", output["phase_id"])
            self.assertEqual("human_video_review", output["label_origin"])
            self.assertEqual(1, summary["record_count"])


if __name__ == "__main__":
    unittest.main()
