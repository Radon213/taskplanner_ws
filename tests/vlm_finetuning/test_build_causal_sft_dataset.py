from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from tools.vlm_finetuning.build_causal_sft_dataset import (
    DEFAULT_CASES,
    DEFAULT_SEED,
    BuildError,
    _first_future_transfer,
    _resolve_bound_descriptor,
    _surgeon_direction_transfers,
    assign_case_folds,
    build_dataset,
    crosses_gap,
    load_case_sources,
    render_unsloth_row,
)


ROOT = Path(__file__).resolve().parents[2]


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class CurrentManifestBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            case_id: load_case_sources(ROOT, case_id)
            for case_id in DEFAULT_CASES
        }

    def test_all_current_sources_are_manifest_bound(self) -> None:
        self.assertEqual(12, len(self.sources))
        self.assertEqual(
            {
                "observed": 340,
                "dt": 315,
                "phase": 48,
                "voice": 251,
                "clinical": 202,
            },
            {
                "observed": sum(
                    len(source.observed) for source in self.sources.values()
                ),
                "dt": sum(len(source.dt) for source in self.sources.values()),
                "phase": sum(
                    len(source.phases) for source in self.sources.values()
                ),
                "voice": sum(
                    len(source.voices) for source in self.sources.values()
                ),
                "clinical": sum(
                    len(source.clinical) for source in self.sources.values()
                ),
            },
        )
        for case_id, source in self.sources.items():
            with self.subTest(case_id=case_id):
                self.assertEqual(case_id, source.case_id)
                self.assertEqual(8, len(source.bindings))
                self.assertEqual(64, len(source.snapshot_id))
                self.assertEqual(
                    len(source.timestamps),
                    int(source.timeline["frame_count"]),
                )

    def test_wrong_descriptor_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "SHA-256 mismatch"):
                _resolve_bound_descriptor(
                    repo_root=root,
                    base=root,
                    descriptor={
                        "file": "source.json",
                        "sha256": "0" * 64,
                    },
                    label="synthetic source",
                )

    def test_declared_gap_crossing_is_detected(self) -> None:
        gaps = [
            {
                "before_frame_idx": 5,
                "after_frame_idx": 6,
                "before_time_sec": 0.5,
                "after_time_sec": 6.5,
            }
        ]
        self.assertFalse(crosses_gap(0, 5, gaps))
        self.assertFalse(crosses_gap(6, 12, gaps))
        self.assertTrue(crosses_gap(5, 6, gaps))
        self.assertTrue(crosses_gap(0, 12, gaps))


class DatasetArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.temporary.name) / "dataset"
        cls.audit = build_dataset(
            repo_root=ROOT,
            output_dir=cls.output_dir,
            materialize=False,
        )
        cls.rows = load_jsonl(cls.output_dir / "master.jsonl")
        cls.sources = {
            case_id: load_case_sources(ROOT, case_id)
            for case_id in DEFAULT_CASES
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_task_counts_and_authority_are_not_promoted(self) -> None:
        self.assertTrue(self.audit["ok"])
        task_counts = Counter(row["task_type"] for row in self.rows)
        self.assertEqual(207, task_counts["tool_presence_at_transfer"])
        self.assertEqual(133, task_counts["request_intent"])
        self.assertEqual(132, task_counts["current_phase"])
        self.assertEqual(202, task_counts["clinical_observation_interpretation"])
        self.assertGreater(task_counts["next_physical_tool"], 190)
        self.assertGreater(task_counts["tool_presence_pseudo"], 0)

        for row in self.rows:
            with self.subTest(example_id=row["example_id"]):
                if row["task_type"] == "current_phase":
                    self.assertEqual(
                        "provisional_ai_phase_not_scoring_ground_truth",
                        row["authority"]["tier"],
                    )
                if (
                    row["task_type"]
                    == "clinical_observation_interpretation"
                ):
                    self.assertEqual(
                        "silver_ai_draft_needs_surgeon_review",
                        row["authority"]["tier"],
                    )
                if row["task_type"] == "tool_presence_pseudo":
                    self.assertEqual("pseudo", row["authority"]["tier"])
                    self.assertEqual("train", row["split"]["role"])

    def test_no_future_media_or_voice_and_no_gap_crossing(self) -> None:
        for row in self.rows:
            cutoff = float(row["time"]["causal_cutoff_sec"])
            media = row["media"]
            source = self.sources[row["case_id"]]
            with self.subTest(example_id=row["example_id"]):
                self.assertTrue(
                    all(float(item["time_sec"]) <= cutoff + 1e-6 for item in media)
                )
                self.assertTrue(
                    all(
                        float(voice["available_sec"]) <= cutoff + 1e-6
                        for voice in row["causal_context"]["voice"]
                    )
                )
                frames = [int(item["source_frame_idx"]) for item in media]
                self.assertFalse(crosses_gap(min(frames), max(frames), source.gaps))
                self.assertTrue(row["quality"]["no_future_input"])
                self.assertTrue(row["quality"]["gap_safe"])

    def test_tool_recognition_has_no_asr_tool_name_leakage(self) -> None:
        for row in self.rows:
            if row["task_type"] not in (
                "tool_presence_at_transfer",
                "tool_presence_pseudo",
            ):
                continue
            with self.subTest(example_id=row["example_id"]):
                self.assertEqual([], row["causal_context"]["voice"])
                self.assertFalse(
                    row["target"]["exhaustive_visible_tool_inventory"]
                )
                self.assertFalse(row["quality"]["absence_labels_available"])
                self.assertTrue(
                    row["quality"]["tool_name_voice_leakage_blocked"]
                )

    def test_implicit_request_never_backfills_eventual_tool(self) -> None:
        requests = [
            row for row in self.rows if row["task_type"] == "request_intent"
        ]
        self.assertEqual(133, len(requests))
        for row in requests:
            with self.subTest(example_id=row["example_id"]):
                self.assertIsNone(row["target"]["requested_tool"])
                self.assertFalse(
                    row["target"][
                        "tool_identity_inferred_from_later_transfer"
                    ]
                )

    def test_next_tool_is_first_physical_surgeon_direction_transfer(self) -> None:
        for row in self.rows:
            if row["task_type"] != "next_physical_tool":
                continue
            source = self.sources[row["case_id"]]
            first = _first_future_transfer(
                _surgeon_direction_transfers(source),
                cutoff_sec=float(row["time"]["causal_cutoff_sec"]),
                horizon_sec=float(row["time"]["prediction_horizon_sec"]),
            )
            with self.subTest(example_id=row["example_id"]):
                if first is None:
                    self.assertEqual("none", row["target"]["next_transfer_tool"])
                    self.assertIsNone(row["target"]["target_event_id"])
                else:
                    self.assertEqual(
                        first["event_id"], row["target"]["target_event_id"]
                    )
                    self.assertEqual(
                        first["tool"], row["target"]["next_transfer_tool"]
                    )

    def test_rfdetr_pseudo_rows_are_balanced_capped_and_train_only(self) -> None:
        pseudo = [
            row
            for row in self.rows
            if row["task_type"] == "tool_presence_pseudo"
        ]
        counts = Counter(row["target"]["tool"] for row in pseudo)
        self.assertTrue(counts)
        self.assertTrue(all(count <= 24 for count in counts.values()))
        media_keys = [
            (
                row["case_id"],
                row["media"][-1]["view"],
                row["media"][-1]["source_frame_idx"],
            )
            for row in pseudo
        ]
        self.assertEqual(
            len(media_keys),
            len(set(media_keys)),
            "identical pseudo media must not carry multiple target tools",
        )
        for row in pseudo:
            with self.subTest(example_id=row["example_id"]):
                self.assertEqual("train", row["split"]["role"])
                self.assertGreaterEqual(
                    row["quality"]["rfdetr_confidence"], 0.9
                )
                self.assertGreaterEqual(
                    row["quality"]["rfdetr_support_count_in_trailing_5"], 3
                )
                self.assertFalse(
                    row["quality"]["rfdetr_conflicting_bbox_class"]
                )
                self.assertIn(
                    "rfdetr_pseudo_source", row["source_bindings"]
                )

    def test_case_group_split_is_disjoint(self) -> None:
        groups: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for row in self.rows:
            groups[row["split_group_id"]].add(
                (row["split"]["fold_id"], row["split"]["role"])
            )
        self.assertEqual(
            {f"case:{case_id}" for case_id in DEFAULT_CASES}, set(groups)
        )
        self.assertTrue(all(len(values) == 1 for values in groups.values()))
        self.assertTrue(
            all(len(cases) == 3 for cases in self.audit["folds"]["folds"].values())
        )

    def test_metadata_build_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            second_dir = Path(directory) / "dataset"
            build_dataset(
                repo_root=ROOT,
                output_dir=second_dir,
                materialize=False,
            )
            self.assertEqual(
                hashlib.sha256(
                    (self.output_dir / "master.jsonl").read_bytes()
                ).hexdigest(),
                hashlib.sha256(
                    (second_dir / "master.jsonl").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                (self.output_dir / "folds.json").read_bytes(),
                (second_dir / "folds.json").read_bytes(),
            )

    def test_unsloth_renderer_has_absolute_images_and_split(self) -> None:
        row = copy.deepcopy(
            next(
                row
                for row in self.rows
                if row["task_type"] == "tool_presence_at_transfer"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, media in enumerate(row["media"]):
                image = Path(directory) / f"{index}.jpg"
                image.write_bytes(b"synthetic")
                media["path"] = str(image.resolve())
            rendered = render_unsloth_row(row)
        self.assertEqual(row["split"]["role"], rendered["split"])
        self.assertEqual(row["split"]["fold_id"], rendered["fold_id"])
        self.assertEqual(row["split_group_id"], rendered["split_group_id"])
        self.assertEqual(
            ["system", "user", "assistant"],
            [message["role"] for message in rendered["messages"]],
        )
        self.assertEqual(
            len(row["media"]),
            sum(
                item["type"] == "image"
                for item in rendered["messages"][1]["content"]
            ),
        )
        assistant = rendered["messages"][2]["content"][0]["text"]
        self.assertEqual(row["target"], json.loads(assistant))


class FoldFunctionTest(unittest.TestCase):
    def test_assignment_is_deterministic_and_case_grouped(self) -> None:
        first = assign_case_folds(
            DEFAULT_CASES,
            seed=DEFAULT_SEED,
            fold_count=4,
        )
        second = assign_case_folds(
            tuple(reversed(DEFAULT_CASES)),
            seed=DEFAULT_SEED,
            fold_count=4,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            {0: 3, 1: 3, 2: 3, 3: 3},
            dict(sorted(Counter(first.values()).items())),
        )


if __name__ == "__main__":
    unittest.main()
