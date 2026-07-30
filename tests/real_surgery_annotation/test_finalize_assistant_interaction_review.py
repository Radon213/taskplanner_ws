from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from tools.real_surgery_annotation.finalize_assistant_interaction_review import (
    FinalizationError,
    finalize,
)
from tools.real_surgery_annotation.interaction_review_gui import (
    FinalReviewBundle,
)


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_ROOT = ROOT / "annotations/observable_tool_events"
SCHEMA_ROOT = ANNOTATION_ROOT / "schema"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AssistantInteractionFinalizerTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        annotation_root = self.temp_root / "annotations/observable_tool_events"
        self.case_dir = annotation_root / "cases/case_demo"
        self.report_dir = annotation_root / "reports"
        self.case_dir.mkdir(parents=True)
        self.report_dir.mkdir(parents=True)

        self.timeline_path = self.case_dir / "cam4_frame_timeline.v1.json"
        timestamps = [round(index / 10, 9) for index in range(1001)]
        self._write_json(
            self.timeline_path,
            {
                "schema": "taskplanner.video_frame_timeline.v1",
                "case_id": "case_demo",
                "source_fps": 10.0,
                "frame_count": len(timestamps),
                "start_sec": timestamps[0],
                "end_sec": timestamps[-1],
                "gaps": [],
                "timestamps_sec": timestamps,
            },
        )
        self.raw_evidence_path = self.case_dir / "marlin_raw.fixture.jsonl"
        self.raw_evidence_path.write_text(
            '{"model":"NemoStation/Marlin-2B","fixture":true}\n',
            encoding="utf-8",
        )
        self.voice_path = self.case_dir / "voice_events.fixture.jsonl"
        self.voice_path.write_text(
            '{"event_id":"case_demo-V0001","text":"Adson"}\n',
            encoding="utf-8",
        )

        self.adjudications_path = (
            self.case_dir
            / "assistant_interaction_adjudications.final.v1.jsonl"
        )
        self.adjudications = self._fixture_adjudications()
        self._write_jsonl(self.adjudications_path, self.adjudications)
        self.projection_path = self.case_dir / "dt_projection.explicit.v1.json"
        self.projection = self._fixture_projection()
        self._write_json(self.projection_path, self.projection)

        self.observed_path = (
            self.case_dir / "interaction_events.observed.final.v1.jsonl"
        )
        self.dt_path = (
            self.case_dir / "interaction_events.dt_reference.final.v1.jsonl"
        )
        self.report_path = (
            self.report_dir / "case_demo_assistant_dt_projection.final.v1.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    def _review(self) -> dict:
        return {
            "reviewer_kind": "ai_assistant",
            "reviewer_id": "codex-gpt-5.6-sol",
            "authorized_by": "task-owner",
            "reviewed_at": "2026-07-28T00:00:00+00:00",
            "notes": "Exact-frame assistant adjudication.",
        }

    def _adjudication(
        self,
        *,
        number: int,
        fields: dict,
        status: str = "confirmed",
        video_and_voice: bool = False,
    ) -> dict:
        start_frame = int(
            fields.get("start_source_frame_idx", fields["source_frame_idx"])
        )
        end_frame = int(
            fields.get("end_source_frame_idx", fields["source_frame_idx"])
        )
        evidence = {
            "evidence_kind": (
                "video_and_voice" if video_and_voice else "video_frames"
            ),
            "source_views": ["cam4", "flir"],
            "start_source_frame_idx": start_frame,
            "end_source_frame_idx": end_frame,
            "timeline_file": str(self.timeline_path.resolve()),
            "timeline_sha256": sha256_file(self.timeline_path),
            "notes": "Exact canonical source frames.",
        }
        if video_and_voice:
            evidence.update(
                {
                    "voice_event_ids": ["case_demo-V0001"],
                    "voice_file": str(self.voice_path.resolve()),
                    "voice_file_sha256": sha256_file(self.voice_path),
                }
            )
        return {
            "schema": "taskplanner.assistant_interaction_adjudication.v1",
            "case_id": "case_demo",
            "adjudication_id": f"case_demo-AJ{number:04d}",
            "review_status": status,
            "adjudicated_fields": fields,
            "proposal_refs": [
                {
                    "generator": "NemoStation/Marlin-2B",
                    "model_id": "NemoStation/Marlin-2B",
                    "model_revision": "fixture-revision",
                    "raw_evidence_file": str(
                        self.raw_evidence_path.resolve()
                    ),
                    "raw_evidence_sha256": sha256_file(
                        self.raw_evidence_path
                    ),
                    "anchor_id": f"anchor-{number}",
                    "candidate_start_sec": max(
                        0.0,
                        float(fields["time_sec"]) - 0.5,
                    ),
                    "candidate_end_sec": float(fields["time_sec"]) + 0.5,
                }
            ],
            "evidence_refs": [evidence],
            "review": self._review(),
        }

    @staticmethod
    def _transfer(
        number: int,
        frame: int,
        tool: str,
        from_location: str,
        to_location: str,
    ) -> dict:
        return {
            "event_id": f"case_demo-T{number:04d}",
            "event_type": "tool_transfer",
            "source_frame_idx": frame,
            "time_sec": round(frame / 10, 9),
            "source_views": ["cam4", "flir"],
            "tool": tool,
            "from": from_location,
            "to": to_location,
        }

    def _fixture_adjudications(self) -> list[dict]:
        request = {
            "event_id": "case_demo-R0001",
            "event_type": "implicit_tool_request",
            "source_frame_idx": 1,
            "time_sec": 0.1,
            "start_source_frame_idx": 1,
            "end_source_frame_idx": 2,
            "start_sec": 0.1,
            "end_sec": 0.2,
            "source_views": ["cam4", "flir"],
        }
        fields = [
            request,
            self._transfer(
                1,
                10,
                "bovie",
                "mayo_stand",
                "scrub_nurse",
            ),
            self._transfer(
                2,
                11,
                "bovie",
                "scrub_nurse",
                "mayo_stand",
            ),
            self._transfer(
                3,
                20,
                "bipolar_forceps",
                "surgeon",
                "scrub_nurse",
            ),
            # Deliberately far apart: explicit IDs, not a gap heuristic,
            # authorize this physical return chain.
            self._transfer(
                4,
                900,
                "bipolar_forceps",
                "scrub_nurse",
                "mayo_stand",
            ),
            self._transfer(
                5,
                30,
                "adson_forceps",
                "mayo_stand",
                "scrub_nurse",
            ),
            self._transfer(
                6,
                31,
                "adson_forceps",
                "scrub_nurse",
                "surgeon",
            ),
            self._transfer(
                7,
                40,
                "scalpel",
                "surgeon",
                "mayo_stand",
            ),
        ]
        records = [
            self._adjudication(
                number=index,
                fields=value,
                video_and_voice=index == 1,
            )
            for index, value in enumerate(fields, 1)
        ]
        records.append(
            self._adjudication(
                number=9,
                fields=self._transfer(
                    8,
                    50,
                    "bovie",
                    "mayo_stand",
                    "scrub_nurse",
                ),
                status="ambiguous",
            )
        )
        records.append(
            self._adjudication(
                number=10,
                fields=self._transfer(
                    9,
                    60,
                    "bovie",
                    "mayo_stand",
                    "scrub_nurse",
                ),
                status="rejected",
            )
        )
        return records

    def _fixture_projection(self) -> dict:
        return {
            "schema": "taskplanner.explicit_dt_interaction_projection.v1",
            "case_id": "case_demo",
            "projection_id": "case_demo-DTP0001",
            "authority": (
                "authorized_ai_assistant_explicit_event_id_projection"
            ),
            "source_adjudications": {
                "path": self.adjudications_path.name,
                "sha256": sha256_file(self.adjudications_path),
            },
            "operations": [
                {
                    "operation_id": "case_demo-OP0001",
                    "operation": "keep",
                    "source_event_ids": ["case_demo-R0001"],
                    "reason": "Request interval remains directly scoreable.",
                },
                {
                    "operation_id": "case_demo-OP0002",
                    "operation": "exclude_cleanup_chain",
                    "source_event_ids": [
                        "case_demo-T0001",
                        "case_demo-T0002",
                    ],
                    "physical_chain_id": "case_demo-CHAIN0001",
                    "reason": "Scrub-only Mayo organization is not a DT task.",
                },
                {
                    "operation_id": "case_demo-OP0003",
                    "operation": "collapse_return_chain",
                    "source_event_ids": [
                        "case_demo-T0003",
                        "case_demo-T0004",
                    ],
                    "physical_chain_id": "case_demo-CHAIN0002",
                    "output_event_id": "case_demo-T0004",
                    "timestamp_source_event_id": "case_demo-T0004",
                    "reason": "Explicit surgeon return reaches Mayo via scrub.",
                },
                {
                    "operation_id": "case_demo-OP0004",
                    "operation": "compound_handover_chain",
                    "source_event_ids": [
                        "case_demo-T0005",
                        "case_demo-T0006",
                    ],
                    "physical_chain_id": "case_demo-CHAIN0003",
                    "target_event_id": "case_demo-T0006",
                    "reason": "Explicit Mayo pickup and surgeon arrival.",
                },
                {
                    "operation_id": "case_demo-OP0005",
                    "operation": "keep",
                    "source_event_ids": ["case_demo-T0007"],
                    "reason": "Direct observed DT transition.",
                },
            ],
        }

    def _finalize(self, **overrides: object) -> dict:
        arguments = {
            "case_dir": self.case_dir,
            "adjudications_path": self.adjudications_path,
            "projection_path": self.projection_path,
            "timeline_path": self.timeline_path,
            "adjudication_schema_path": (
                SCHEMA_ROOT
                / "assistant_interaction_adjudication.v1.schema.json"
            ),
            "projection_schema_path": (
                SCHEMA_ROOT
                / "explicit_dt_interaction_projection.v1.schema.json"
            ),
            "point_schema_path": (
                SCHEMA_ROOT / "observable_interaction_point.v1.schema.json"
            ),
            "interval_schema_path": (
                SCHEMA_ROOT / "observable_interaction_interval.v1.schema.json"
            ),
            "tools_path": ANNOTATION_ROOT / "catalogs/tools.yaml",
            "observed_output_path": self.observed_path,
            "dt_output_path": self.dt_path,
            "report_output_path": self.report_path,
        }
        arguments.update(overrides)
        return finalize(**arguments)

    def test_schema_accepts_complete_projection_variants(self) -> None:
        schema = json.loads(
            (
                SCHEMA_ROOT
                / "explicit_dt_interaction_projection.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(self.projection)

    def test_materializes_and_projects_only_explicit_event_ids(self) -> None:
        summary = self._finalize()

        self.assertEqual(8, summary["observed_count"])
        self.assertEqual(5, summary["dt_count"])
        observed = [
            json.loads(line)
            for line in self.observed_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        projected = [
            json.loads(line)
            for line in self.dt_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            {
                "case_demo-R0001",
                "case_demo-T0001",
                "case_demo-T0002",
                "case_demo-T0003",
                "case_demo-T0004",
                "case_demo-T0005",
                "case_demo-T0006",
                "case_demo-T0007",
            },
            {record["event_id"] for record in observed},
        )
        self.assertEqual(
            {
                "case_demo-R0001",
                "case_demo-T0004",
                "case_demo-T0005",
                "case_demo-T0006",
                "case_demo-T0007",
            },
            {record["event_id"] for record in projected},
        )
        collapsed = next(
            record
            for record in projected
            if record["event_id"] == "case_demo-T0004"
        )
        self.assertEqual(90.0, collapsed["time_sec"])
        self.assertEqual("surgeon", collapsed["from"])
        self.assertEqual("mayo_stand", collapsed["to"])

        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "taskplanner.dt_interaction_projection_report.v1",
            report["schema"],
        )
        self.assertEqual(8, report["counts"]["observed_confirmed_count"])
        self.assertEqual(5, report["counts"]["dt_confirmed_count"])
        self.assertFalse(report["projection"]["time_gap_heuristic_used"])
        self.assertFalse(
            report["projection"]["tool_type_pairing_heuristic_used"]
        )
        self.assertEqual(8, len(report["observed_source_mapping"]))
        self.assertEqual(
            5,
            len(report["projection"]["source_mapping"]),
        )
        serialized = json.dumps(report, ensure_ascii=False).lower()
        self.assertNotIn("mixed_human", serialized)
        self.assertNotIn("human_video_review", serialized)

    def test_final_review_bundle_loads_manifest_handoff(self) -> None:
        summary = self._finalize()
        masks_path = self.case_dir / "evaluation_masks.v1.json"
        reconciliation_path = (
            self.case_dir / "policy02_reconciliation_audit.final.v1.json"
        )
        self._write_json(masks_path, {"schema": "fixture.masks.v1"})
        self._write_json(
            reconciliation_path,
            {"schema": "fixture.reconciliation.v1"},
        )
        reference = copy.deepcopy(summary["manifest_evaluation_reference"])
        reference["assistant_adjudication"] = {
            "file": self.adjudications_path.name,
            "sha256": sha256_file(self.adjudications_path),
        }
        reference["projection_policy_file"] = self.projection_path.name
        reference["projection_policy_sha256"] = sha256_file(
            self.projection_path
        )
        reference["evaluation_masks"] = {
            "file": masks_path.name,
            "sha256": sha256_file(masks_path),
        }
        manifest_path = self.case_dir / "annotation_manifest.json"
        self._write_json(
            manifest_path,
            {
                "schema": "taskplanner.observable_annotation_manifest.v1",
                "case_id": "case_demo",
                "minimal_interaction_annotation": {
                    "timeline_file": self.timeline_path.name,
                    "timeline_sha256": sha256_file(self.timeline_path),
                    "assistant_adjudication_file": self.adjudications_path.name,
                    "assistant_adjudication_sha256": sha256_file(
                        self.adjudications_path
                    ),
                    "explicit_projection_file": self.projection_path.name,
                    "explicit_projection_sha256": sha256_file(
                        self.projection_path
                    ),
                    "policy02_reconciliation_file": reconciliation_path.name,
                    "policy02_reconciliation_sha256": sha256_file(
                        reconciliation_path
                    ),
                },
                "evaluation_reference": reference,
            },
        )

        bundle = FinalReviewBundle(manifest_path=manifest_path)

        self.assertEqual(8, len(bundle.observed))
        self.assertEqual(5, len(bundle.dt_reference))
        self.assertTrue(
            all(
                record["_final_review"]["read_only"]
                for record in bundle.observed + bundle.dt_reference
            )
        )

    def test_unmapped_confirmed_event_fails_without_outputs(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["operations"] = projection["operations"][:-1]
        self._write_json(self.projection_path, projection)

        with self.assertRaisesRegex(
            FinalizationError,
            "모든 confirmed observed event",
        ):
            self._finalize()

        self.assertFalse(self.observed_path.exists())
        self.assertFalse(self.dt_path.exists())
        self.assertFalse(self.report_path.exists())

    def test_duplicate_source_mapping_fails(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["operations"].append(
            {
                "operation_id": "case_demo-OP0006",
                "operation": "keep",
                "source_event_ids": ["case_demo-R0001"],
                "reason": "Invalid duplicate mapping.",
            }
        )
        self._write_json(self.projection_path, projection)

        with self.assertRaisesRegex(FinalizationError, "두 번 매핑"):
            self._finalize()

    def test_reference_hash_mismatch_fails_closed(self) -> None:
        adjudications = copy.deepcopy(self.adjudications)
        adjudications[0]["proposal_refs"][0]["raw_evidence_sha256"] = "0" * 64
        self._write_jsonl(self.adjudications_path, adjudications)
        projection = copy.deepcopy(self.projection)
        projection["source_adjudications"]["sha256"] = sha256_file(
            self.adjudications_path
        )
        self._write_json(self.projection_path, projection)

        with self.assertRaisesRegex(FinalizationError, "hash 불일치"):
            self._finalize()

    def test_human_reviewer_is_rejected_by_assistant_contract(self) -> None:
        adjudications = copy.deepcopy(self.adjudications)
        adjudications[0]["review"]["reviewer_kind"] = "human"
        self._write_jsonl(self.adjudications_path, adjudications)
        projection = copy.deepcopy(self.projection)
        projection["source_adjudications"]["sha256"] = sha256_file(
            self.adjudications_path
        )
        self._write_json(self.projection_path, projection)

        with self.assertRaisesRegex(FinalizationError, "ai_assistant"):
            self._finalize()

    def test_create_only_preflight_preserves_all_outputs(self) -> None:
        original = b"do-not-overwrite\n"
        self.dt_path.write_bytes(original)

        with self.assertRaisesRegex(FinalizationError, "refusing to overwrite"):
            self._finalize()

        self.assertEqual(original, self.dt_path.read_bytes())
        self.assertFalse(self.observed_path.exists())
        self.assertFalse(self.report_path.exists())

    def test_optional_phase_is_copied_as_ambiguous_context(self) -> None:
        phase_input = self.case_dir / "phase_context.fixture.v1.jsonl"
        phase_output = (
            self.case_dir / "phase_events.provisional.final.v1.jsonl"
        )
        phase = {
            "schema": "taskplanner.observable_interaction_point.v1",
            "case_id": "case_demo",
            "event_id": "case_demo-PH0001",
            "event_type": "phase_start",
            "time_sec": 0.0,
            "source_frame_idx": 0,
            "source_views": ["cam4", "flir"],
            "phase_id": "P03",
            "phase_boundary_kind": "clip_initial_state",
            "review_status": "ambiguous",
            "label_origin": "assistant_video_adjudication",
            "review": self._review(),
        }
        self._write_jsonl(phase_input, [phase])

        summary = self._finalize(
            phase_context_path=phase_input,
            phase_output_path=phase_output,
        )

        self.assertEqual(1, summary["phase_count"])
        self.assertTrue(phase_output.is_file())
        descriptor = summary["manifest_evaluation_reference"]
        self.assertTrue(descriptor["phase_reference_included"])
        self.assertEqual(
            "context_only_not_ground_truth",
            descriptor["phase_reference"]["scoring_role"],
        )


if __name__ == "__main__":
    unittest.main()
